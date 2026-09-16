"""The ``/timeout`` inactivity watcher.

Every test drives :meth:`IdleTimeoutService._tick` directly and fakes the clock,
so a 90-minute countdown is exercised without waiting 90 minutes.
"""

from __future__ import annotations

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

    clock.advance(60 * 60)
    await service._tick()
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
    clock.advance(20 * 60)
    await service._tick()
    assert service.get(SERVER_ID).idle_seconds == pytest.approx(20 * 60)

    crafty.online = 1
    await service._tick()
    assert service.get(SERVER_ID).counting is False

    crafty.online = 0
    await service._tick()
    assert service.get(SERVER_ID).idle_seconds == 0.0


async def test_a_stopped_server_counts_as_idle(service, crafty, clock, orchestrator):
    """A stopped server is still billing the VM, so the timer runs."""
    service.arm(SERVER_ID, 30, shutdown_vm=True)
    crafty.running = False
    await service._tick()
    clock.advance(31 * 60)
    await service._tick()
    assert orchestrator.stops == [(SERVER_ID, True, 300)]


# --------------------------------------------------------------------------- #
# Firing
# --------------------------------------------------------------------------- #
async def test_the_timeout_fires_after_the_configured_delay(
    service, crafty, clock, orchestrator
):
    service.arm(SERVER_ID, 90, shutdown_vm=True)
    await service._tick()

    clock.advance(89 * 60)
    await service._tick()
    assert orchestrator.stops == []

    clock.advance(2 * 60)
    await service._tick()
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


async def test_the_timeout_disarms_itself_after_firing(service, clock, orchestrator):
    service.arm(SERVER_ID, 1, shutdown_vm=False)
    await service._tick()
    clock.advance(120)
    await service._tick()

    assert service.get(SERVER_ID) is None
    # A second tick must not stop the server all over again.
    await service._tick()
    assert len(orchestrator.stops) == 1


async def test_a_failed_shutdown_does_not_re_fire_every_tick(
    service, clock, orchestrator
):
    orchestrator.error = CraftyAPIError("Crafty said no.")
    service.arm(SERVER_ID, 1, shutdown_vm=False)
    await service._tick()
    clock.advance(120)
    await service._tick()

    assert service.get(SERVER_ID) is None
    assert "Crafty said no." in service.last_result


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
async def test_a_transient_crafty_failure_leaves_the_countdown_alone(
    service, crafty, clock, orchestrator
):
    """A failed check must neither reset the timer nor fire it."""
    service.arm(SERVER_ID, 30, shutdown_vm=False)
    await service._tick()
    anchor = service.get(SERVER_ID).empty_since

    crafty.error = CraftyUnavailable()
    clock.advance(60 * 60)
    await service._tick()

    state = service.get(SERVER_ID)
    assert state is not None
    # Still armed, anchored where it was, and nothing was stopped on a tick
    # that learned nothing.
    assert state.empty_since == anchor
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


async def test_a_powered_off_vm_cancels_the_timeout(service, crafty, orchestrator):
    """Nothing left to shut down, so the timer retires quietly."""
    service.arm(SERVER_ID, 30, shutdown_vm=True)
    crafty.error = CraftyHostOffline()
    await service._tick()

    assert service.get(SERVER_ID) is None
    assert orchestrator.stops == []


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
    config, crafty, orchestrator, tmp_path, clock
):
    """The bot was not watching while it was down, so it cannot claim idleness."""
    path = tmp_path / "state.json"
    first = IdleTimeoutService(config, crafty, orchestrator, state_path=path)
    first.arm(SERVER_ID, 45, shutdown_vm=True)
    first.get(SERVER_ID).empty_since = clock.now

    second = IdleTimeoutService(config, crafty, orchestrator, state_path=path)
    second._restore()
    assert second.get(SERVER_ID).counting is False


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
def test_remaining_is_unknown_while_the_server_is_active(clock):
    state = TimeoutState(server_id=SERVER_ID, minutes=30, shutdown_vm=True)
    assert state.remaining_seconds is None
    assert state.deadline_unix is None
    assert state.expired is False


def test_remaining_counts_down(clock):
    state = TimeoutState(
        server_id=SERVER_ID, minutes=30, shutdown_vm=True, empty_since=clock.now
    )
    clock.advance(10 * 60)
    assert state.remaining_seconds == pytest.approx(20 * 60)
    assert state.expired is False
    clock.advance(21 * 60)
    assert state.remaining_seconds == 0.0
    assert state.expired is True
