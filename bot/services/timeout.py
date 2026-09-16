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
    #: Monotonic timestamp of when the server was first seen empty. ``None``
    #: means somebody is online (or nothing has been observed yet), which is
    #: exactly the state in which no countdown is running.
    empty_since: float | None = None
    #: Player count at the last check, for display.
    last_online: int | None = None

    @property
    def limit_seconds(self) -> float:
        return self.minutes * 60.0

    @property
    def counting(self) -> bool:
        """``True`` while the countdown is actually running."""
        return self.empty_since is not None

    @property
    def idle_seconds(self) -> float:
        if self.empty_since is None:
            return 0.0
        return max(0.0, time.monotonic() - self.empty_since)

    @property
    def remaining_seconds(self) -> float | None:
        """Seconds left, or ``None`` while the server is active."""
        if self.empty_since is None:
            return None
        return max(0.0, self.limit_seconds - self.idle_seconds)

    @property
    def deadline_unix(self) -> float | None:
        """Wall-clock deadline, for Discord's relative timestamp markup."""
        remaining = self.remaining_seconds
        return None if remaining is None else time.time() + remaining

    @property
    def expired(self) -> bool:
        return self.counting and self.remaining_seconds == 0.0

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

    async def _sleep_until_next_check(self) -> None:
        """Sleep until the next check is due, or until something is armed.

        With nothing armed this costs no API calls at all; with a timer running
        it never overshoots the deadline by more than one check interval.
        """
        delay = IDLE_SLEEP
        if self._states:
            interval = float(self._config.timeout_check_interval)
            remaining = [
                state.remaining_seconds
                for state in self._states.values()
                if state.remaining_seconds is not None
            ]
            delay = min([interval] + [max(r, 1.0) for r in remaining])

        self._wake.clear()
        try:
            await asyncio.wait_for(self._wake.wait(), timeout=delay)
        except asyncio.TimeoutError:
            pass

    # ------------------------------------------------------------------ #
    # The check itself
    # ------------------------------------------------------------------ #
    async def _tick(self) -> None:
        """Update every armed countdown and fire the ones that have expired."""
        for server_id in list(self._states):
            state = self._states.get(server_id)
            if state is None:
                continue
            try:
                await self._observe(state)
            except CraftyHostOffline:
                # The VM is already off, so there is nothing left to shut down.
                logger.info(
                    "Idle timeout for server %s cancelled: the VM is already off",
                    server_id,
                )
                self._states.pop(server_id, None)
                self._persist()
            except BotError as exc:
                # A transient Crafty failure must not advance *or* reset the
                # countdown: we simply do not know what the player count is.
                logger.debug(
                    "Idle timeout check skipped for server %s: %s",
                    server_id,
                    exc.user_message,
                )

    async def _observe(self, state: TimeoutState) -> None:
        stats = await self._crafty.get_stats(
            state.server_id, ttl=self._config.status_cache_ttl
        )
        online = stats.online or 0
        state.last_online = online

        # Any player at all counts as activity, however static the number is.
        if stats.running and online > 0:
            if state.counting:
                logger.debug(
                    "Idle timeout for server %s reset: %d player(s) online",
                    state.server_id,
                    online,
                )
            state.empty_since = None
            return

        if state.empty_since is None:
            state.empty_since = time.monotonic()
            return

        if state.expired:
            await self._fire(state)

    async def _fire(self, state: TimeoutState) -> None:
        """Run the graceful stop workflow and disarm."""
        # Disarm first: a shutdown that fails should not re-fire on every tick.
        self._states.pop(state.server_id, None)
        self._persist()

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
