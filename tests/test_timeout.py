"""The ``/timeout`` inactivity watcher.

Every test drives :meth:`IdleTimeoutService._tick` directly and fakes the clock,
so a 90-minute countdown is exercised without waiting 90 minutes.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from bot.errors import CraftyAPIError, CraftyHostOffline, CraftyUnavailable
from bot.services.crafty import ServerStats, ServerSummary
from bot.services.timeout import MAX_MINUTES, IdleTimeoutService, TimeoutState

SERVER_ID = "server-1"
OTHER_ID = "server-2"


class FakeCrafty:
    """Answers ``get_stats`` from a scripted player count."""

    def __init__(self) -> None:
        self.online = 0
        self.running = True
        self.error: Exception | None = None
        self.servers = (ServerSummary(server_id=SERVER_ID, name="Survival"),)
        self.other_running = False
        self.stats_calls = 0

    async def get_stats(self, server_id: str, *, ttl: float = 0.0) -> ServerStats:
        self.stats_calls += 1
        if self.error is not None:
            raise self.error
        if server_id != SERVER_ID:
            return ServerStats(server_id=server_id, running=self.other_running)
        return ServerStats(
            server_id=server_id, running=self.running, online=self.online
        )

    async def list_servers(self, *, force_refresh: bool = False):
        return self.servers


class FakeOrchestrator:
    """Records the stop workflow instead of performing it."""

    def __init__(self, *, azure_enabled: bool = True) -> None:
        self.azure_enabled = azure_enabled
        self.stops: list[tuple[str, bool, int]] = []
        self.error: Exception | None = None

    async def stop_minecraft(self, server_id, *, shutdown_vm=False, delay=None, **_):
        if self.error is not None:
            raise self.error
        self.stops.append((server_id, shutdown_vm, delay))


class Clock:
    """A monotonic clock the tests advance by hand."""

    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock(monkeypatch):
    fake = Clock()
    monkeypatch.setattr("bot.services.timeout.time.monotonic", fake)
    return fake


async def elapse(service, clock, seconds: float, *, step: float = 60.0) -> None:
    """Advance time the way the running watcher does: in check-sized steps.

    Jumping the clock an hour and ticking once is not what the real service
    ever sees, and it now trips the blind-spot guard -- correctly, since a
    single reading an hour after the last one proves nothing about the hour in
    between. Tests that mean "an hour passed while the watcher was running"
    must therefore say so by ticking through it.
    """
    remaining = seconds
    while remaining > 0:
        clock.advance(min(step, remaining))
        await service._tick()
        remaining -= step


@pytest.fixture
def crafty():
    return FakeCrafty()


@pytest.fixture
def orchestrator():
    return FakeOrchestrator()


@pytest.fixture
def service(config, crafty, orchestrator, tmp_path):
    return IdleTimeoutService(
        config,
        crafty,
        orchestrator,
        state_path=tmp_path / "timeout_state.json",
    )


# --------------------------------------------------------------------------- #
# Arming
# --------------------------------------------------------------------------- #
def test_nothing_is_armed_by_default(service):
    assert service.get(SERVER_ID) is None
    assert service.active() == ()
    assert service.any_armed is False


def test_arming_records_the_choice(service):
    state = service.arm(SERVER_ID, 90, shutdown_vm=True, armed_by=42)
    assert state.minutes == 90
    assert state.shutdown_vm is True
    assert state.armed_by == 42
    assert service.get(SERVER_ID) is state
    assert service.any_armed is True


def test_arming_rejects_an_impossible_delay(service):
    with pytest.raises(ValueError):
        service.arm(SERVER_ID, 0, shutdown_vm=True)
    with pytest.raises(ValueError):
        service.arm(SERVER_ID, MAX_MINUTES + 1, shutdown_vm=True)


def test_disarming_reports_whether_anything_was_armed(service):
    assert service.disarm(SERVER_ID) is False
    service.arm(SERVER_ID, 30, shutdown_vm=False)
    assert service.disarm(SERVER_ID) is True
    assert service.get(SERVER_ID) is None


async def test_rearming_restarts_the_countdown(service, crafty, clock):
    service.arm(SERVER_ID, 90, shutdown_vm=False)
    await service._tick()
    clock.advance(60 * 60)

    service.arm(SERVER_ID, 30, shutdown_vm=False)
    state = service.get(SERVER_ID)
    assert state.counting is False
    assert state.idle_seconds == 0.0


# --------------------------------------------------------------------------- #
# What counts as inactivity
# --------------------------------------------------------------------------- #
async def test_the_countdown_only_runs_while_the_server_is_empty(
    service, crafty, clock, orchestrator
):
    service.arm(SERVER_ID, 30, shutdown_vm=False)
    crafty.online = 2

    await service._tick()
    assert service.get(SERVER_ID).counting is False

    await elapse(service, clock, 60 * 60)
    # An hour with players online must not have advanced anything.
    assert service.get(SERVER_ID).counting is False
    assert orchestrator.stops == []


async def test_a_static_player_count_still_counts_as_active(
    service, crafty, clock, orchestrator
):
    """One player who never moves keeps the server active."""
    service.arm(SERVER_ID, 30, shutdown_vm=False)
    crafty.online = 1
    for _ in range(10):
        clock.advance(10 * 60)
        await service._tick()
    assert orchestrator.stops == []
    assert service.get(SERVER_ID) is not None


async def test_a_player_joining_resets_a_running_countdown(service, crafty, clock):
    service.arm(SERVER_ID, 30, shutdown_vm=False)
    await service._tick()
    await elapse(service, clock, 20 * 60)
    assert service.get(SERVER_ID).idle_seconds == pytest.approx(20 * 60)

    crafty.online = 1
    await service._tick()
    assert service.get(SERVER_ID).counting is False

    crafty.online = 0
    await service._tick()
    assert service.get(SERVER_ID).idle_seconds == 0.0


async def test_a_stopped_server_does_not_count_down(service, crafty, clock, orchestrator):
    """The countdown measures an idle *running* server.

    A stopped server is not idle, it is already stopped -- so the timer waits
    for it to come back up instead of firing a shutdown at nothing.
    """
    service.arm(SERVER_ID, 30, shutdown_vm=True)
    crafty.running = False
    await service._tick()
    await elapse(service, clock, 31 * 60)

    assert orchestrator.stops == []
    state = service.get(SERVER_ID)
    assert state is not None
    assert state.counting is False


async def test_the_countdown_starts_when_the_server_comes_back_up(
    service, crafty, clock, orchestrator
):
    service.arm(SERVER_ID, 30, shutdown_vm=True)
    crafty.running = False
    await service._tick()
    await elapse(service, clock, 60 * 60)
    assert orchestrator.stops == []

    crafty.running = True
    await service._tick()
    await elapse(service, clock, 31 * 60)
    assert orchestrator.stops == [(SERVER_ID, True, 300)]


async def test_stopping_the_server_mid_countdown_resets_it(
    service, crafty, clock, orchestrator
):
    service.arm(SERVER_ID, 30, shutdown_vm=True)
    await service._tick()
    await elapse(service, clock, 20 * 60)
    assert service.get(SERVER_ID).idle_seconds == pytest.approx(20 * 60)

    crafty.running = False
    await service._tick()
    assert service.get(SERVER_ID).counting is False


# --------------------------------------------------------------------------- #
# Firing
# --------------------------------------------------------------------------- #
async def test_the_timeout_fires_after_the_configured_delay(
    service, crafty, clock, orchestrator
):
    service.arm(SERVER_ID, 90, shutdown_vm=True)
    await service._tick()

    await elapse(service, clock, 89 * 60)
    assert orchestrator.stops == []

    await elapse(service, clock, 2 * 60)
    assert orchestrator.stops == [(SERVER_ID, True, 300)]


async def test_firing_uses_the_graceful_stop_workflow(service, clock, orchestrator):
    """Never a kill: the timeout goes through the same stop as `/server stop`."""
    service.arm(SERVER_ID, 1, shutdown_vm=True)
    await service._tick()
    clock.advance(120)
    await service._tick()

    server_id, shutdown_vm, delay = orchestrator.stops[0]
    assert server_id == SERVER_ID
    assert shutdown_vm is True
    # The VM is deallocated only after the configured grace period.
    assert delay == 300


async def test_the_timeout_stays_armed_after_firing(service, crafty, clock, orchestrator):
    """It is a standing rule: only /timeout minutes:0 cancels it."""
    service.arm(SERVER_ID, 1, shutdown_vm=False)
    await service._tick()
    clock.advance(120)
    await service._tick()
    assert orchestrator.stops == [(SERVER_ID, False, 0)]

    state = service.get(SERVER_ID)
    assert state is not None
    assert state.counting is False

    # The server is stopped now, so a second tick must not stop it again...
    crafty.running = False
    await service._tick()
    clock.advance(120)
    await service._tick()
    assert len(orchestrator.stops) == 1

    # ...but the next session is covered without re-arming.
    crafty.running = True
    await service._tick()
    clock.advance(120)
    await service._tick()
    assert len(orchestrator.stops) == 2


async def test_a_failed_shutdown_does_not_re_fire_every_tick(
    service, clock, orchestrator
):
    """A failure has to wait out the full idle period again, not retry in a loop."""
    orchestrator.error = CraftyAPIError("Crafty said no.")
    service.arm(SERVER_ID, 1, shutdown_vm=False)
    await service._tick()
    clock.advance(120)
    await service._tick()
    assert "Crafty said no." in service.last_result

    # The server never stopped, so it is still running and empty -- but the
    # countdown restarted, so the next tick does not try again.
    await service._tick()
    assert len(orchestrator.stops) == 0
    assert service.get(SERVER_ID).idle_seconds < 60


async def test_the_vm_is_kept_when_another_server_is_still_running(
    service, crafty, clock, orchestrator
):
    """One VM can host several servers; one idle timer must not strand them."""
    crafty.servers = (
        ServerSummary(server_id=SERVER_ID, name="Survival"),
        ServerSummary(server_id=OTHER_ID, name="Creative"),
    )
    crafty.other_running = True

    service.arm(SERVER_ID, 1, shutdown_vm=True)
    await service._tick()
    clock.advance(120)
    await service._tick()

    assert orchestrator.stops == [(SERVER_ID, False, 0)]


async def test_an_unknown_neighbour_keeps_the_vm_up(service, crafty, clock, orchestrator):
    """When the check itself fails, err towards paying rather than surprising."""
    crafty.servers = (
        ServerSummary(server_id=SERVER_ID, name="Survival"),
        ServerSummary(server_id=OTHER_ID, name="Creative"),
    )
    service.arm(SERVER_ID, 1, shutdown_vm=True)
    await service._tick()
    clock.advance(120)

    crafty.error = CraftyUnavailable()
    # The expiry itself is already known, so only the neighbour check fails.
    state = service.get(SERVER_ID)
    await service._fire(state)
    assert orchestrator.stops == [(SERVER_ID, False, 0)]


async def test_vm_shutdown_is_skipped_when_azure_is_disabled(config, crafty, clock, tmp_path):
    orchestrator = FakeOrchestrator(azure_enabled=False)
    service = IdleTimeoutService(
        config, crafty, orchestrator, state_path=tmp_path / "state.json"
    )
    service.arm(SERVER_ID, 1, shutdown_vm=True)
    await service._tick()
    clock.advance(120)
    await service._tick()
    assert orchestrator.stops == [(SERVER_ID, False, 0)]


# --------------------------------------------------------------------------- #
# Failure handling
# --------------------------------------------------------------------------- #
async def test_a_transient_crafty_failure_freezes_the_countdown(
    service, crafty, clock, orchestrator
):
    """A failed check must neither advance the timer nor fire it."""
    service.arm(SERVER_ID, 30, shutdown_vm=False)
    await service._tick()
    await elapse(service, clock, 10 * 60)
    counted = service.get(SERVER_ID).idle_seconds

    crafty.error = CraftyUnavailable()
    await elapse(service, clock, 60 * 60)

    state = service.get(SERVER_ID)
    assert state is not None
    # Still armed, frozen at what was actually observed, nothing stopped.
    assert state.idle_seconds == counted
    assert state.stalled is True
    assert orchestrator.stops == []


async def test_firing_always_follows_a_fresh_empty_observation(
    service, crafty, clock, orchestrator
):
    """The deadline can pass unseen, but the shutdown still needs a live check.

    If Crafty is unreachable while players are online, the elapsed time keeps
    accruing -- but the next successful check sees those players and resets the
    countdown, so nothing is ever stopped out from under them.
    """
    service.arm(SERVER_ID, 30, shutdown_vm=False)
    await service._tick()

    crafty.error = CraftyUnavailable()
    clock.advance(60 * 60)
    await service._tick()
    assert orchestrator.stops == []

    crafty.error = None
    crafty.online = 3
    await service._tick()
    assert orchestrator.stops == []
    assert service.get(SERVER_ID).counting is False


async def test_a_powered_off_vm_pauses_the_timeout_without_cancelling_it(
    service, crafty, clock, orchestrator
):
    """The VM going down must not silently throw away what somebody armed."""
    service.arm(SERVER_ID, 30, shutdown_vm=True)
    await service._tick()
    await elapse(service, clock, 20 * 60)

    crafty.error = CraftyHostOffline()
    await service._tick()
    state = service.get(SERVER_ID)
    assert state is not None
    assert state.counting is False
    assert orchestrator.stops == []

    # And it picks up again once the VM and the server are back.
    crafty.error = None
    await service._tick()
    await elapse(service, clock, 31 * 60)
    assert orchestrator.stops == [(SERVER_ID, True, 300)]


async def test_the_watcher_backs_off_while_nothing_is_running(service, crafty):
    """No point polling at full rate when no countdown can start."""
    from bot.services.timeout import DORMANT_SLEEP

    service.arm(SERVER_ID, 30, shutdown_vm=True)
    # Freshly armed and never checked: do not mistake "unknown" for "stopped".
    assert service.get(SERVER_ID).last_running is None

    crafty.running = False
    await service._tick()
    assert service.get(SERVER_ID).last_running is False
    assert DORMANT_SLEEP > service._config.timeout_check_interval


# --------------------------------------------------------------------------- #
# Persistence
# --------------------------------------------------------------------------- #
def test_an_armed_timeout_survives_a_restart(config, crafty, orchestrator, tmp_path):
    path = tmp_path / "state.json"
    first = IdleTimeoutService(config, crafty, orchestrator, state_path=path)
    first.arm(SERVER_ID, 45, shutdown_vm=True, armed_by=7)

    second = IdleTimeoutService(config, crafty, orchestrator, state_path=path)
    second._restore()
    state = second.get(SERVER_ID)
    assert state is not None
    assert (state.minutes, state.shutdown_vm, state.armed_by) == (45, True, 7)


def test_a_restored_countdown_starts_again_from_zero(
    config, crafty, orchestrator, tmp_path
):
    """The bot was not watching while it was down, so it cannot claim idleness."""
    path = tmp_path / "state.json"
    first = IdleTimeoutService(config, crafty, orchestrator, state_path=path)
    first.arm(SERVER_ID, 45, shutdown_vm=True)
    first.get(SERVER_ID).counting = True
    first.get(SERVER_ID).idle_seconds = 30 * 60

    second = IdleTimeoutService(config, crafty, orchestrator, state_path=path)
    second._restore()
    assert second.get(SERVER_ID).counting is False
    assert second.get(SERVER_ID).idle_seconds == 0.0


def test_disarming_is_persisted(config, crafty, orchestrator, tmp_path):
    path = tmp_path / "state.json"
    first = IdleTimeoutService(config, crafty, orchestrator, state_path=path)
    first.arm(SERVER_ID, 45, shutdown_vm=True)
    first.disarm(SERVER_ID)

    second = IdleTimeoutService(config, crafty, orchestrator, state_path=path)
    second._restore()
    assert second.get(SERVER_ID) is None


def test_a_corrupt_state_file_is_ignored(config, crafty, orchestrator, tmp_path):
    path = tmp_path / "state.json"
    path.write_text("{not json at all", encoding="utf-8")
    service = IdleTimeoutService(config, crafty, orchestrator, state_path=path)
    service._restore()
    assert service.active() == ()


def test_junk_entries_are_dropped_but_good_ones_survive(
    config, crafty, orchestrator, tmp_path
):
    path = tmp_path / "state.json"
    path.write_text(
        json.dumps(
            [
                {"server_id": SERVER_ID, "minutes": 30, "shutdown_vm": True},
                {"server_id": "", "minutes": 30},
                {"server_id": "x", "minutes": 0},
                {"server_id": "y", "minutes": MAX_MINUTES + 1},
                "not even an object",
            ]
        ),
        encoding="utf-8",
    )
    service = IdleTimeoutService(config, crafty, orchestrator, state_path=path)
    service._restore()
    assert [state.server_id for state in service.active()] == [SERVER_ID]


def test_an_unwritable_state_path_does_not_break_arming(
    config, crafty, orchestrator, tmp_path
):
    """Failing to save a timeout must never take the bot down."""
    blocked = tmp_path / "a-file"
    blocked.write_text("", encoding="utf-8")
    service = IdleTimeoutService(
        config, crafty, orchestrator, state_path=blocked / "nested" / "state.json"
    )
    state = service.arm(SERVER_ID, 30, shutdown_vm=False)
    assert state.minutes == 30


def test_persistence_can_be_switched_off(config, crafty, orchestrator):
    service = IdleTimeoutService(
        config, crafty, orchestrator, state_path=""
    )
    service.arm(SERVER_ID, 30, shutdown_vm=False)
    service._restore()
    assert service.get(SERVER_ID) is not None


# --------------------------------------------------------------------------- #
# Derived values used by the embeds
# --------------------------------------------------------------------------- #
def test_remaining_is_unknown_while_the_server_is_active():
    state = TimeoutState(server_id=SERVER_ID, minutes=30, shutdown_vm=True)
    assert state.remaining_seconds is None
    assert state.deadline_unix is None
    assert state.expired is False


def test_remaining_counts_down_with_observed_time():
    state = TimeoutState(
        server_id=SERVER_ID, minutes=30, shutdown_vm=True, counting=True
    )
    assert state.remaining_seconds == pytest.approx(30 * 60)

    state.idle_seconds = 10 * 60
    assert state.remaining_seconds == pytest.approx(20 * 60)
    assert state.expired is False

    state.idle_seconds = 31 * 60
    assert state.remaining_seconds == 0.0
    assert state.expired is True


def test_pausing_forgets_the_partial_total():
    state = TimeoutState(
        server_id=SERVER_ID,
        minutes=30,
        shutdown_vm=True,
        counting=True,
        idle_seconds=25 * 60,
    )
    state.pause()
    assert state.counting is False
    assert state.idle_seconds == 0.0
    assert state.remaining_seconds is None


# --------------------------------------------------------------------------- #
# Idle time is only trustworthy while the watcher was actually watching
# --------------------------------------------------------------------------- #
async def test_a_blind_spot_restarts_the_countdown(
    service, crafty, clock, orchestrator
):
    """The case this guard exists for.

    The server is 80 minutes into a 90-minute countdown when Crafty goes away.
    An hour later it answers again and reports zero players -- but those players
    could have arrived and left five minutes ago. Firing on the accrued total
    would shut down a server that was busy moments before, so the count starts
    again from this first trustworthy reading.
    """
    service.arm(SERVER_ID, 90, shutdown_vm=False)
    await service._tick()
    await elapse(service, clock, 80 * 60)
    assert service.get(SERVER_ID).idle_seconds == pytest.approx(80 * 60)

    crafty.error = CraftyUnavailable()
    await elapse(service, clock, 60 * 60)

    crafty.error = None
    await service._tick()
    assert orchestrator.stops == []
    assert service.get(SERVER_ID).idle_seconds == 0.0

    # And a full idle period from here still fires normally.
    await elapse(service, clock, 91 * 60)
    assert orchestrator.stops == [(SERVER_ID, False, 0)]


async def test_an_ordinary_gap_between_checks_does_not_restart_it(
    service, crafty, clock, orchestrator
):
    """One slow or missed check must not keep resetting a legitimate countdown."""
    service.arm(SERVER_ID, 30, shutdown_vm=False)
    await service._tick()
    await elapse(service, clock, 10 * 60)

    # A single skipped check at the 60 s cadence is well inside the grace.
    clock.advance(120)
    await service._tick()
    assert service.get(SERVER_ID).idle_seconds == pytest.approx(12 * 60)


async def test_a_blind_spot_over_a_stopped_server_changes_nothing(
    service, crafty, clock, orchestrator
):
    """Nothing to invalidate: a stopped server was not counting in the first place."""
    service.arm(SERVER_ID, 30, shutdown_vm=False)
    crafty.running = False
    await service._tick()

    crafty.error = CraftyUnavailable()
    await elapse(service, clock, 60 * 60)

    crafty.error = None
    crafty.running = True
    await service._tick()
    assert service.get(SERVER_ID).idle_seconds == 0.0
    assert orchestrator.stops == []


async def test_the_grace_scales_with_the_check_interval(config, crafty, orchestrator):
    """A slower configured cadence must not look like a blind spot."""
    from dataclasses import replace

    from bot.services.timeout import OBSERVATION_GRACE

    slow = IdleTimeoutService(
        replace(config, timeout_check_interval=600), crafty, orchestrator, state_path=""
    )
    assert slow._observation_gap == 600 * OBSERVATION_GRACE

    # A fast cadence still gets a sane floor rather than a 45 s trip wire.
    fast = IdleTimeoutService(
        replace(config, timeout_check_interval=15), crafty, orchestrator, state_path=""
    )
    assert fast._observation_gap == 180.0


# --------------------------------------------------------------------------- #
# A VM that is up without a working Crafty must not advance the countdown
# --------------------------------------------------------------------------- #
async def test_a_vm_up_without_crafty_does_not_advance_the_countdown(
    service, crafty, clock, orchestrator
):
    """The case this guard exists for.

    Starting the VM makes the host gate open, so requests are actually
    attempted -- but if Crafty itself is not up, every one of them fails. The
    countdown must freeze at what was observed rather than run on elapsed wall
    time and shut down a server nobody ever confirmed was empty.
    """
    service.arm(SERVER_ID, 30, shutdown_vm=True)
    await service._tick()
    await elapse(service, clock, 5 * 60)
    observed = service.get(SERVER_ID).idle_seconds
    assert observed == pytest.approx(5 * 60)

    # VM on, Crafty not answering: not CraftyHostOffline, a plain failure.
    crafty.error = CraftyUnavailable()
    await elapse(service, clock, 10 * 60 * 60)

    state = service.get(SERVER_ID)
    assert state.idle_seconds == observed
    assert state.stalled is True
    assert orchestrator.stops == []


async def test_a_frozen_countdown_restarts_once_crafty_answers(
    service, crafty, clock, orchestrator
):
    """The freeze is a blind spot, so the total it held cannot be trusted either."""
    service.arm(SERVER_ID, 30, shutdown_vm=False)
    await service._tick()
    await elapse(service, clock, 25 * 60)

    crafty.error = CraftyUnavailable()
    await elapse(service, clock, 30 * 60)

    crafty.error = None
    await service._tick()
    state = service.get(SERVER_ID)
    assert state.stalled is False
    assert state.idle_seconds == 0.0
    assert orchestrator.stops == []

    # A full idle period from the first trustworthy reading still fires.
    await elapse(service, clock, 31 * 60)
    assert orchestrator.stops == [(SERVER_ID, False, 0)]


async def test_a_stalled_watcher_backs_off(service, crafty, clock):
    """A Crafty that is not answering is not worth polling every minute."""
    from bot.services.timeout import DORMANT_SLEEP, STALL_BACKOFF_AFTER

    service.arm(SERVER_ID, 30, shutdown_vm=False)
    await service._tick()
    assert service._next_delay() <= service._config.timeout_check_interval

    crafty.error = CraftyUnavailable()
    for _ in range(STALL_BACKOFF_AFTER):
        await service._tick()

    assert service.get(SERVER_ID).failures >= STALL_BACKOFF_AFTER
    assert service._next_delay() == DORMANT_SLEEP


async def test_recovery_clears_the_stall(service, crafty, clock):
    service.arm(SERVER_ID, 30, shutdown_vm=False)
    crafty.error = CraftyUnavailable()
    await service._tick()
    assert service.get(SERVER_ID).stalled is True

    crafty.error = None
    await service._tick()
    state = service.get(SERVER_ID)
    assert state.stalled is False
    assert state.failures == 0


async def test_a_powered_off_vm_is_a_definite_answer_not_a_stall(service, crafty):
    """CraftyHostOffline means "the VM is off", which is knowledge, not a blind spot."""
    service.arm(SERVER_ID, 30, shutdown_vm=True)
    crafty.error = CraftyHostOffline()
    await service._tick()

    state = service.get(SERVER_ID)
    assert state.stalled is False
    assert state.last_running is False
    assert state.counting is False


# --------------------------------------------------------------------------- #
# One server firing must not block the check of any other armed server
# --------------------------------------------------------------------------- #
async def test_firing_one_server_does_not_block_checking_another(config, crafty):
    """The bug this guards against.

    Firing blocks for the graceful stop plus AUTO_SHUTDOWN_DELAY (five minutes
    by default) before the VM is deallocated. A tick that checked servers one
    at a time would let that block every other armed server's check for the
    whole time -- long enough to miss their real deadline and, worse, trip the
    blind-spot guard on them the moment the loop got back around. Servers are
    checked concurrently precisely to prevent that.

    This test intentionally does not use the `clock` fixture: it needs real
    asyncio scheduling (an Event a second coroutine can wait on), which a
    frozen `time.monotonic` would break.
    """
    crafty.servers = (
        ServerSummary(server_id=SERVER_ID, name="A"),
        ServerSummary(server_id=OTHER_ID, name="B"),
    )
    crafty.other_running = True

    release = asyncio.Event()

    class BlockingOrchestrator(FakeOrchestrator):
        async def stop_minecraft(self, server_id, *, shutdown_vm=False, delay=None, **_):
            await release.wait()
            self.stops.append((server_id, shutdown_vm, delay))

    orchestrator = BlockingOrchestrator()
    service = IdleTimeoutService(config, crafty, orchestrator, state_path="")

    service.arm(SERVER_ID, 1, shutdown_vm=True, armed_by=1)
    service.arm(OTHER_ID, 1, shutdown_vm=False, armed_by=1)

    import time as real_time

    now = real_time.monotonic()  # this test uses the real clock, not `clock`
    a = service.get(SERVER_ID)
    a.counting, a.idle_seconds, a.last_checked = True, 61.0, now
    b = service.get(OTHER_ID)
    b.counting, b.idle_seconds, b.last_checked = True, 30.0, now

    tick = asyncio.create_task(service._tick())
    # Let both _check_one coroutines run until A blocks inside the fake stop
    # workflow; B has nothing to block on, so its own observation completes.
    for _ in range(5):
        await asyncio.sleep(0)

    assert not tick.done()  # still blocked on A's release
    assert service.get(OTHER_ID).idle_seconds > 30.0
    assert orchestrator.stops == []

    release.set()
    await tick
    # Server B is still running, so the existing cross-server guard correctly
    # declines to deallocate the VM out from under it -- shutdown_vm is False
    # even though this timeout was armed with shutdown_vm=True.
    assert orchestrator.stops == [(SERVER_ID, False, 0)]


# --------------------------------------------------------------------------- #
# wake(): lets an external event (a server starting) cut short a backoff
# --------------------------------------------------------------------------- #
async def test_wake_interrupts_a_sleep_immediately(service):
    """wake() must not require waiting out whatever delay was already chosen."""
    task = asyncio.create_task(service._sleep_until_next_check())
    await asyncio.sleep(0)
    assert not task.done()

    service.wake()
    await asyncio.wait_for(task, timeout=1.0)  # would hang if wake() did nothing


async def test_a_server_starting_while_dormant_is_noticed_promptly(
    service, crafty, clock, orchestrator
):
    """The exact bug reported: armed while stopped, then started, then nothing.

    IdleTimeoutService backs off to DORMANT_SLEEP while every armed server is
    stopped. Simulating that backoff and then calling wake() (as the
    orchestrator hook does the moment a start succeeds) must make the very
    next tick see the server running, rather than only noticing on whatever
    tick the stale multi-minute backoff would eventually deliver.
    """
    from bot.services.timeout import DORMANT_SLEEP

    service.arm(SERVER_ID, 1, shutdown_vm=False)
    crafty.running = False
    await service._tick()
    assert service._next_delay() == DORMANT_SLEEP  # confirms the backoff kicked in

    # The server starts; the orchestrator hook calls wake() at this point.
    crafty.running = True
    crafty.online = 0
    service.wake()
    await service._tick()

    state = service.get(SERVER_ID)
    assert state.last_running is True
    assert state.counting is True
    # And the backoff is gone now that the server is up.
    assert service._next_delay() < DORMANT_SLEEP
