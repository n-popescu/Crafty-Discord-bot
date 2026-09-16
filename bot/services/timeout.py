"""Runtime-controlled inactivity timeout: stop Minecraft, then free the VM.

This is the ``/timeout`` feature. A user arms a timer for a server ("stop
everything after 90 minutes with nobody online"); the service watches the player
count and, when the server has been empty for that long, runs the ordinary stop
workflow -- a **graceful** Crafty shutdown that saves the world, waits for the
server to confirm it has stopped, and only then deallocates the Azure VM.

Two details drive the whole design:

* *Inactivity* means **zero players online**. Any player at all makes the server
  active, even if the count never changes, so the countdown restarts the moment
  somebody joins and only starts again once the last one leaves.
* Nothing here kills a process. Firing the timeout calls the same
  :meth:`InfraOrchestrator.stop_minecraft` that ``/server stop`` uses, so a
  timed shutdown and a manual one are the same shutdown.

The watcher sleeps until it has something to do, which matters on a Raspberry Pi
Zero W: with no timeout armed it makes no API calls at all, and with one armed it
polls at ``TIMEOUT_CHECK_INTERVAL`` (60 s by default) -- and not even that while
the Azure VM is powered off, because :class:`CraftyService` skips those requests.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from bot.config import Config
from bot.errors import BotError, CraftyHostOffline
from bot.services.crafty import CraftyService
from bot.services.orchestrator import InfraOrchestrator

logger = logging.getLogger(__name__)

#: How long the loop waits when nothing is armed. It is woken immediately by
#: :meth:`IdleTimeoutService.arm`, so this is only a safety net.
IDLE_SLEEP = 300.0

#: How many check intervals may pass between two observations before the
#: accrued idle time is thrown away. Idle time is only trustworthy while the
#: watcher was actually looking: across a longer blind spot the server could
#: have been busy and emptied again, and firing on that stale total would shut
#: down a server that filled and emptied a minute ago.
OBSERVATION_GRACE = 3.0

#: Consecutive failed checks after which the watcher slows down. A Crafty that
#: is not answering usually is not about to, and each attempt costs retries and
#: a connection timeout on a Pi Zero W.
STALL_BACKOFF_AFTER = 3

#: Sleep between checks while every armed server is known to be stopped. The
#: countdown cannot start until one comes back up, so polling at full rate would
#: just burn Azure and Crafty calls; noticing a restart a few minutes late only
#: delays the *start* of a countdown measured in tens of minutes.
DORMANT_SLEEP = 300.0

#: Upper bound accepted for a timeout, in minutes (24 hours).
MAX_MINUTES = 1440


@dataclass
class TimeoutState:
    """One armed timeout, plus what the watcher has observed so far."""

    server_id: str
    minutes: int
    shutdown_vm: bool
    armed_by: int = 0
    armed_at: float = 0.0
    #: Idle time accumulated from **confirmed observations only**. Deriving it
    #: from the wall clock instead would keep the countdown running through any
    #: period the watcher could not see -- a stopped Crafty, a dropped network,
    #: a VM that is up but has nothing listening -- and shut the server down on
    #: time that was assumed rather than measured.
    idle_seconds: float = 0.0
    #: Whether the last successful check found the server up and empty.
    counting: bool = False
    #: Set when the most recent check could not reach Crafty at all. The
    #: countdown is frozen, not running, until contact is restored.
    stalled: bool = False
    #: Player count at the last check, for display.
    last_online: int | None = None
    #: Whether the server was running at the last check. ``None`` means the
    #: watcher has not looked yet.
    last_running: bool | None = None
    #: Monotonic timestamp of the last *successful* observation, used both to
    #: measure the interval to add and to detect a blind spot.
    last_checked: float | None = None
    #: Consecutive failed checks, used to back off a hopeless poll.
    failures: int = 0

    @property
    def limit_seconds(self) -> float:
        return self.minutes * 60.0

    @property
    def remaining_seconds(self) -> float | None:
        """Seconds left, or ``None`` while the countdown is not running."""
        if not self.counting:
            return None
        return max(0.0, self.limit_seconds - self.idle_seconds)

    @property
    def deadline_unix(self) -> float | None:
        """Wall-clock deadline, for Discord's relative timestamp markup."""
        remaining = self.remaining_seconds
        return None if remaining is None else time.time() + remaining

    @property
    def expired(self) -> bool:
        return self.counting and self.idle_seconds >= self.limit_seconds

    def pause(self) -> None:
        """Stop the countdown and forget the partial total."""
        self.counting = False
        self.idle_seconds = 0.0

    def to_json(self) -> dict[str, Any]:
        """Only the arming decision is persisted -- never the live countdown."""
        return {
            "server_id": self.server_id,
            "minutes": self.minutes,
            "shutdown_vm": self.shutdown_vm,
            "armed_by": self.armed_by,
            "armed_at": self.armed_at,
        }

    @classmethod
    def from_json(cls, raw: Mapping[str, Any]) -> TimeoutState | None:
        try:
            minutes = int(raw["minutes"])
            server_id = str(raw["server_id"])
        except (KeyError, TypeError, ValueError):
            return None
        if not server_id or not 1 <= minutes <= MAX_MINUTES:
            return None
        return cls(
            server_id=server_id,
            minutes=minutes,
            shutdown_vm=bool(raw.get("shutdown_vm", True)),
            armed_by=int(raw.get("armed_by") or 0),
            armed_at=float(raw.get("armed_at") or 0.0),
        )


class IdleTimeoutService:
    """Owns every armed timeout and the single loop that watches them."""

    def __init__(
        self,
        config: Config,
        crafty: CraftyService,
        orchestrator: InfraOrchestrator,
        *,
        state_path: Path | str | None = None,
    ) -> None:
        self._config = config
        self._crafty = crafty
        self._orchestrator = orchestrator
        self._states: dict[str, TimeoutState] = {}
        self._wake = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._closing = False
        #: Human-readable note about the last shutdown this service performed,
        #: shown by ``/timeout`` so a fired timer does not vanish without trace.
        self.last_result: str = ""

        configured = state_path if state_path is not None else config.timeout_state_file
        self._state_path = Path(configured) if configured else None

    # ------------------------------------------------------------------ #
    # Reads
    # ------------------------------------------------------------------ #
    def get(self, server_id: str | None) -> TimeoutState | None:
        if not server_id:
            return None
        return self._states.get(server_id)

    def active(self) -> tuple[TimeoutState, ...]:
        return tuple(self._states.values())

    @property
    def any_armed(self) -> bool:
        return bool(self._states)

    # ------------------------------------------------------------------ #
    # Writes
    # ------------------------------------------------------------------ #
    def arm(
        self,
        server_id: str,
        minutes: int,
        *,
        shutdown_vm: bool,
        armed_by: int = 0,
    ) -> TimeoutState:
        """Arm (or re-arm) the timeout for one server.

        Re-arming restarts the countdown from scratch, which is what someone
        changing 90 minutes to 30 expects.
        """
        if not 1 <= minutes <= MAX_MINUTES:
            raise ValueError(f"minutes must be between 1 and {MAX_MINUTES}")

        state = TimeoutState(
            server_id=server_id,
            minutes=minutes,
            shutdown_vm=shutdown_vm,
            armed_by=armed_by,
            armed_at=time.time(),
        )
        self._states[server_id] = state
        self._persist()
        self._wake.set()
        logger.info(
            "Idle timeout armed for server %s: %d min (VM shutdown: %s)",
            server_id,
            minutes,
            shutdown_vm,
        )
        return state

    def disarm(self, server_id: str) -> bool:
        """Cancel the timeout for a server. Returns ``False`` if none was armed."""
        if self._states.pop(server_id, None) is None:
            return False
        self._persist()
        self._wake.set()
        logger.info("Idle timeout disarmed for server %s", server_id)
        return True

    def wake(self) -> None:
        """Force the next check to happen now instead of waiting out a backoff.

        The watcher backs off to :data:`DORMANT_SLEEP` (five minutes by
        default) while every armed server is stopped, since no countdown can
        start until one comes back up. Nothing about that backoff itself
        notices a server actually starting again -- that happens on a
        completely separate code path (:class:`InfraOrchestrator`) the
        watcher never talks to -- so without this, a server started while its
        timeout is armed could sit unnoticed for up to five minutes, long
        past a short test delay. ``CraftyBot`` wires this to
        ``InfraOrchestrator.on_server_running``.
        """
        self._wake.set()

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #
    def start(self) -> None:
        """Restore any persisted timeouts and start the watcher."""
        self._restore()
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="idle-timeout")

    async def close(self) -> None:
        self._closing = True
        self._wake.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _run(self) -> None:
        while not self._closing:
            try:
                await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - the watcher must outlive one bad tick
                logger.exception("Idle timeout check failed")

            await self._sleep_until_next_check()

    def _next_delay(self) -> float:
        """How long to wait before the next round of checks.

        Kept separate from the sleep itself so it can be reasoned about -- and
        tested -- without anybody actually waiting.
        """
        if not self._states:
            return IDLE_SLEEP

        interval = float(self._config.timeout_check_interval)
        # `last_running is None` means "not checked yet", which must not be
        # mistaken for "stopped" -- a freshly armed timeout gets a prompt
        # first check.
        dormant = all(state.last_running is False for state in self._states.values())
        # A Crafty that has refused several checks in a row is not worth
        # hammering; nothing can be counted until it answers again anyway.
        stalled = all(
            state.failures >= STALL_BACKOFF_AFTER for state in self._states.values()
        )
        if dormant or stalled:
            interval = max(interval, DORMANT_SLEEP)

        remaining = [
            state.remaining_seconds
            for state in self._states.values()
            if state.remaining_seconds is not None
        ]
        return min([interval] + [max(value, 1.0) for value in remaining])

    async def _sleep_until_next_check(self) -> None:
        """Sleep until the next check is due, or until something is armed."""
        self._wake.clear()
        try:
            await asyncio.wait_for(self._wake.wait(), timeout=self._next_delay())
        except asyncio.TimeoutError:
            pass

    # ------------------------------------------------------------------ #
    # The check itself
    # ------------------------------------------------------------------ #
    async def _tick(self) -> None:
        """Update every armed countdown and fire the ones that have expired.

        Each server is checked as its own task, run concurrently. Firing a
        timeout blocks for as long as the graceful stop takes plus
        ``AUTO_SHUTDOWN_DELAY`` (five minutes by default) before the VM is
        deallocated, and a sequential loop would let that one shutdown stall
        every other armed server's check for the whole time -- long enough to
        both miss their real deadline and trip the blind-spot guard on them
        the moment the loop finally got back around.
        """
        await asyncio.gather(
            *(self._check_one(server_id) for server_id in list(self._states))
        )

    async def _check_one(self, server_id: str) -> None:
        """Observe and, if due, fire one server's timeout.

        Failures are caught here rather than left to propagate to
        :meth:`_tick`'s caller, so that one server behaving unexpectedly can
        never prevent the others in the same tick from being checked.
        """
        state = self._states.get(server_id)
        if state is None:
            return
        try:
            await self._observe(state)
        except CraftyHostOffline:
            # The VM is off, so the server is off: a definite answer, not a
            # blind spot. The timeout stays armed for the next time the
            # server comes up.
            if state.counting:
                logger.debug(
                    "Idle timeout for server %s paused: the VM is off", server_id
                )
            state.pause()
            state.stalled = False
            state.last_running = False
            state.last_checked = None
        except BotError as exc:
            # Crafty could not be reached or could not answer -- most often
            # a VM that is up while Crafty itself is not. We learned
            # nothing, so the countdown freezes where it is rather than
            # advancing on time nobody watched.
            state.stalled = True
            state.failures += 1
            logger.debug(
                "Idle timeout check failed for server %s (%d in a row): %s",
                server_id,
                state.failures,
                exc.user_message,
            )
        except Exception:  # noqa: BLE001 - one server's bug must not sink the rest
            logger.exception("Idle timeout check crashed for server %s", server_id)

    async def _observe(self, state: TimeoutState) -> None:
        stats = await self._crafty.get_stats(
            state.server_id, ttl=self._config.status_cache_ttl
        )
        now = time.monotonic()
        online = stats.online or 0
        # Only a successful observation updates these; a failed check must never
        # be mistaken for having seen the server.
        gap = None if state.last_checked is None else now - state.last_checked
        state.last_checked = now
        state.last_online = online
        state.last_running = stats.running
        state.stalled = False
        state.failures = 0

        # The countdown measures an idle *running* server. A stopped one is not
        # idle, it is already stopped, so the timer neither runs nor fires --
        # it simply waits for the server to come back up.
        #
        # Any player at all counts as activity, however static the number is.
        if not stats.running or online > 0:
            if state.counting:
                logger.debug(
                    "Idle timeout for server %s reset: %s",
                    state.server_id,
                    f"{online} player(s) online" if stats.running else "server stopped",
                )
            state.pause()
            return

        # The server is up and empty *now*. Start a fresh count if it was not
        # already running, or if we lost sight of the server for longer than a
        # few check intervals: across a blind spot it may have been busy and
        # emptied moments ago, so the time in between was never idle at all.
        if not state.counting:
            state.counting = True
            state.idle_seconds = 0.0
            return
        if gap is None or gap > self._observation_gap:
            logger.info(
                "Idle timeout for server %s restarted: no reading for %.0fs, so the "
                "%.0fs already counted cannot be trusted",
                state.server_id,
                gap or 0.0,
                state.idle_seconds,
            )
            state.idle_seconds = 0.0
            return

        # Only the interval we actually watched is added.
        state.idle_seconds += gap
        if state.expired:
            await self._fire(state)

    @property
    def _observation_gap(self) -> float:
        """How long a blind spot invalidates the accrued idle time."""
        return max(self._config.timeout_check_interval * OBSERVATION_GRACE, 180.0)

    async def _fire(self, state: TimeoutState) -> None:
        """Run the graceful stop workflow, keeping the timeout armed.

        The timeout is a standing rule, not a one-shot: it stays armed until
        somebody cancels it with ``/timeout minutes:0``. Only the countdown is
        reset, which is also what stops a failed shutdown from re-firing on the
        very next tick -- it has to go through the full idle period again.
        """
        state.pause()

        shutdown_vm = state.shutdown_vm and self._orchestrator.azure_enabled
        if shutdown_vm and await self._other_servers_running(state.server_id):
            logger.info(
                "Idle timeout: another Crafty server is still running, "
                "so the VM will be left up"
            )
            shutdown_vm = False

        logger.info(
            "Idle timeout reached for server %s after %d min empty: stopping%s",
            state.server_id,
            state.minutes,
            " and deallocating the VM" if shutdown_vm else "",
        )
        try:
            await self._orchestrator.stop_minecraft(
                state.server_id,
                shutdown_vm=shutdown_vm,
                delay=self._config.auto_shutdown_delay if shutdown_vm else 0,
            )
        except BotError as exc:
            self.last_result = (
                f"Timeout fired but the shutdown failed: {exc.user_message}"
            )
            logger.warning("Idle timeout shutdown failed: %s", exc.user_message)
        else:
            self.last_result = (
                f"Stopped after {state.minutes} min with nobody online"
                + (" and deallocated the VM." if shutdown_vm else ".")
            )
            logger.info("Idle timeout shutdown complete for server %s", state.server_id)

    async def _other_servers_running(self, server_id: str) -> bool:
        """Is any *other* Crafty server still up on this VM?

        One VM can host several servers, so an expired timer for one of them
        must not pull the floor out from under the others. When this cannot be
        determined the answer is "yes", which keeps the VM running: an extra
        hour of compute is cheaper than an unannounced shutdown.
        """
        try:
            servers = await self._crafty.list_servers()
        except BotError as exc:
            logger.warning(
                "Could not list servers before deallocating (%s); leaving the VM up",
                exc.user_message,
            )
            return True

        for server in servers:
            if server.server_id == server_id:
                continue
            try:
                stats = await self._crafty.get_stats(server.server_id)
            except BotError:
                return True
            if stats.running:
                return True
        return False

    # ------------------------------------------------------------------ #
    # Persistence
    # ------------------------------------------------------------------ #
    def _persist(self) -> None:
        """Write the armed timeouts to disk, atomically and best-effort.

        Losing a timeout to a restart would leave a VM billing overnight, which
        is the exact cost this feature exists to avoid. Failing to *write* it,
        on the other hand, must never take the bot down.
        """
        if self._state_path is None:
            return
        payload = [state.to_json() for state in self._states.values()]
        temporary = self._state_path.with_suffix(self._state_path.suffix + ".tmp")
        try:
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            temporary.write_text(json.dumps(payload), encoding="utf-8")
            os.replace(temporary, self._state_path)
        except OSError as exc:
            logger.warning("Could not save the timeout state: %s", exc)

    def _restore(self) -> None:
        """Reload timeouts armed before a restart.

        The countdown deliberately starts again from zero: the bot was not
        watching while it was down, so it cannot claim the server stayed empty.
        """
        if self._state_path is None or not self._state_path.exists():
            return
        try:
            raw = json.loads(self._state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            logger.warning("Ignoring an unreadable timeout state file: %s", exc)
            return
        if not isinstance(raw, list):
            return

        for entry in raw:
            if not isinstance(entry, Mapping):
                continue
            state = TimeoutState.from_json(entry)
            if state is not None:
                self._states[state.server_id] = state
                logger.info(
                    "Restored idle timeout for server %s: %d min",
                    state.server_id,
                    state.minutes,
                )
