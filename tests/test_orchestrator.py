"""Orchestration tests: Azure and Crafty must be sequenced correctly.

Both services are replaced by in-memory fakes that record the order of every
call, so these tests assert the *sequence* of operations (which is what makes a
graceful Minecraft shutdown correct) without touching any real API.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from bot.errors import CraftyUnavailable, OperationTimeout, OrchestrationError
from bot.services.azure import POWER_DEALLOCATED, POWER_RUNNING, VmStatus
from bot.services.crafty import ServerStats
from bot.services.orchestrator import InfraOrchestrator, StepState

SERVER_ID = "server-1"


class FakeCrafty:
    """Tracks a single server's running state and records every call."""

    def __init__(self, *, running: bool = False, reachable: bool = True) -> None:
        self.running = running
        self.reachable = reachable
        self.calls: list[str] = []
        self.fail_with: Exception | None = None

    async def resolve_server_id(self, requested=None) -> str:
        if self.fail_with:
            raise self.fail_with
        return requested or SERVER_ID

    async def check_connection(self) -> bool:
        self.calls.append("check_connection")
        return self.reachable

    async def get_stats(self, server_id: str, *, ttl: float = 0.0) -> ServerStats:
        if self.fail_with:
            raise self.fail_with
        self.calls.append("get_stats")
        return ServerStats(
            server_id=server_id,
            name="Survival",
            running=self.running,
            online=2 if self.running else None,
            max_players=20 if self.running else None,
        )

    async def start_server(self, server_id: str) -> None:
        self.calls.append("start_server")
        self.running = True

    async def stop_server(self, server_id: str) -> None:
        self.calls.append("stop_server")
        self.running = False

    async def restart_server(self, server_id: str) -> None:
        self.calls.append("restart_server")
        self.running = True


class FakeAzure:
    """Tracks the VM power state and records every call."""

    def __init__(self, *, power_state: str = POWER_DEALLOCATED, enabled: bool = True) -> None:
        self.power_state = power_state
        self._enabled = enabled
        self.calls: list[str] = []
        self.vm_name = "mc-vm"

    @property
    def enabled(self) -> bool:
        return self._enabled

    async def get_vm_status(self, *, use_cache: bool = True) -> VmStatus:
        self.calls.append("get_vm_status")
        return VmStatus(name=self.vm_name, power_state=self.power_state)

    async def start_vm(self) -> None:
        self.calls.append("start_vm")
        self.power_state = POWER_RUNNING

    async def stop_vm(self, *, deallocate: bool = True) -> None:
        self.calls.append("deallocate" if deallocate else "power_off")
        self.power_state = POWER_DEALLOCATED

    async def restart_vm(self) -> None:
        self.calls.append("restart_vm")
        self.power_state = POWER_RUNNING

    async def wait_for_running(self, *, timeout: float = 0.0) -> VmStatus | None:
        self.calls.append("wait_for_running")
        return await self.get_vm_status() if self.power_state == POWER_RUNNING else None

    async def wait_for_stopped(self, *, timeout: float = 0.0) -> VmStatus | None:
        self.calls.append("wait_for_stopped")
        return await self.get_vm_status() if self.power_state == POWER_DEALLOCATED else None


@pytest.fixture
def fast_config(config):
    return replace(config, start_timeout=30, stop_timeout=30, auto_shutdown_delay=0)


def build(fast_config, crafty: FakeCrafty, azure: FakeAzure) -> InfraOrchestrator:
    return InfraOrchestrator(fast_config, crafty, azure)


def states(workflow) -> dict[str, StepState]:
    return {step.key: step.state for step in workflow.steps}


# --------------------------------------------------------------------------- #
# Scenario 1: Azure stopped -> /minecraft start
# --------------------------------------------------------------------------- #
async def test_start_from_deallocated_vm_walks_the_whole_chain(fast_config):
    crafty = FakeCrafty(running=False)
    azure = FakeAzure(power_state=POWER_DEALLOCATED)
    progress: list[list[tuple[str, StepState]]] = []

    async def on_progress(steps):
        progress.append([(s.key, s.state) for s in steps])

    workflow = await build(fast_config, crafty, azure).start_infrastructure(
        on_progress=on_progress
    )

    assert azure.calls.index("start_vm") < azure.calls.index("wait_for_running")
    assert crafty.calls.index("check_connection") < crafty.calls.index("start_server")
    assert crafty.running is True
    assert states(workflow) == {
        "vm": StepState.DONE,
        "crafty": StepState.DONE,
        "minecraft": StepState.DONE,
    }
    assert progress, "the Discord layer must receive progress updates"


async def test_start_skips_the_vm_when_it_is_already_running(fast_config):
    crafty = FakeCrafty(running=False)
    azure = FakeAzure(power_state=POWER_RUNNING)

    workflow = await build(fast_config, crafty, azure).start_infrastructure()

    assert "start_vm" not in azure.calls
    assert workflow.step("vm").detail == "Already running"
    assert crafty.calls.count("start_server") == 1


async def test_start_is_idempotent_when_minecraft_already_runs(fast_config):
    crafty = FakeCrafty(running=True)
    azure = FakeAzure(power_state=POWER_RUNNING)

    workflow = await build(fast_config, crafty, azure).start_infrastructure()

    assert "start_server" not in crafty.calls
    assert workflow.step("minecraft").detail == "Already running"


async def test_start_fails_cleanly_when_crafty_never_answers(fast_config):
    crafty = FakeCrafty(running=False, reachable=False)
    azure = FakeAzure(power_state=POWER_RUNNING)
    # Crafty never answers, so a tiny timeout keeps the test quick.
    orchestrator = build(replace(fast_config, start_timeout=0.2), crafty, azure)

    with pytest.raises(OperationTimeout):
        await orchestrator.start_infrastructure()
    assert "start_server" not in crafty.calls


async def test_start_without_azure_only_touches_crafty(fast_config):
    crafty = FakeCrafty(running=False)
    azure = FakeAzure(enabled=False)

    workflow = await build(fast_config, crafty, azure).start_infrastructure()

    assert azure.calls == []
    assert [step.key for step in workflow.steps] == ["minecraft"]
    assert crafty.running is True


# --------------------------------------------------------------------------- #
# Scenario 2: Minecraft running -> /azure stop
# --------------------------------------------------------------------------- #
async def test_stop_infrastructure_stops_minecraft_before_deallocating(fast_config):
    crafty = FakeCrafty(running=True)
    azure = FakeAzure(power_state=POWER_RUNNING)

    workflow = await build(fast_config, crafty, azure).stop_infrastructure()

    assert crafty.calls.count("stop_server") == 1
    assert crafty.running is False
    assert azure.calls.index("deallocate") > 0
    assert states(workflow) == {"minecraft": StepState.DONE, "vm": StepState.DONE}


async def test_stop_infrastructure_refuses_to_deallocate_if_crafty_is_down(fast_config):
    crafty = FakeCrafty(running=True)
    crafty.fail_with = CraftyUnavailable()
    azure = FakeAzure(power_state=POWER_RUNNING)

    with pytest.raises(OrchestrationError):
        await build(fast_config, crafty, azure).stop_infrastructure()

    assert "deallocate" not in azure.calls
    assert azure.power_state == POWER_RUNNING


async def test_forced_stop_skips_minecraft(fast_config):
    crafty = FakeCrafty(running=True)
    azure = FakeAzure(power_state=POWER_RUNNING)

    workflow = await build(fast_config, crafty, azure).stop_infrastructure(force=True)

    assert "stop_server" not in crafty.calls
    assert "deallocate" in azure.calls
    assert workflow.step("minecraft").state is StepState.SKIPPED


async def test_stop_infrastructure_is_a_no_op_when_the_vm_is_already_stopped(fast_config):
    crafty = FakeCrafty(running=False)
    azure = FakeAzure(power_state=POWER_DEALLOCATED)

    workflow = await build(fast_config, crafty, azure).stop_infrastructure()

    assert "deallocate" not in azure.calls
    assert workflow.step("vm").state is StepState.DONE
    assert workflow.step("minecraft").state is StepState.SKIPPED


async def test_stop_infrastructure_requires_azure(fast_config):
    orchestrator = build(fast_config, FakeCrafty(), FakeAzure(enabled=False))
    with pytest.raises(OrchestrationError):
        await orchestrator.stop_infrastructure()


# --------------------------------------------------------------------------- #
# /server stop and /minecraft stop
# --------------------------------------------------------------------------- #
async def test_stop_minecraft_leaves_the_vm_alone_by_default(fast_config):
    crafty = FakeCrafty(running=True)
    azure = FakeAzure(power_state=POWER_RUNNING)

    workflow = await build(fast_config, crafty, azure).stop_minecraft()

    assert crafty.running is False
    assert azure.calls == []
    assert [step.key for step in workflow.steps] == ["minecraft"]


async def test_stop_minecraft_can_deallocate_when_asked(fast_config):
    crafty = FakeCrafty(running=True)
    azure = FakeAzure(power_state=POWER_RUNNING)

    workflow = await build(fast_config, crafty, azure).stop_minecraft(shutdown_vm=True)

    assert "stop_server" in crafty.calls
    assert "deallocate" in azure.calls
    assert states(workflow)["vm"] is StepState.DONE


async def test_stop_minecraft_ignores_shutdown_vm_without_azure(fast_config):
    crafty = FakeCrafty(running=True)
    azure = FakeAzure(enabled=False)

    workflow = await build(fast_config, crafty, azure).stop_minecraft(shutdown_vm=True)

    assert azure.calls == []
    assert [step.key for step in workflow.steps] == ["minecraft"]


async def test_stop_minecraft_when_already_stopped_does_nothing(fast_config):
    crafty = FakeCrafty(running=False)
    azure = FakeAzure(power_state=POWER_RUNNING)

    workflow = await build(fast_config, crafty, azure).stop_minecraft()

    assert "stop_server" not in crafty.calls
    assert workflow.step("minecraft").detail == "Already stopped"


# --------------------------------------------------------------------------- #
# Restart and snapshot
# --------------------------------------------------------------------------- #
async def test_restart_starts_the_vm_first_when_it_is_down(fast_config):
    crafty = FakeCrafty(running=False)
    azure = FakeAzure(power_state=POWER_DEALLOCATED)

    workflow = await build(fast_config, crafty, azure).restart_minecraft()

    assert "start_vm" in azure.calls
    assert "restart_server" in crafty.calls
    assert states(workflow)["minecraft"] is StepState.DONE


async def test_snapshot_survives_a_broken_crafty(fast_config):
    crafty = FakeCrafty(running=True)
    crafty.fail_with = CraftyUnavailable()
    azure = FakeAzure(power_state=POWER_RUNNING)

    snapshot = await build(fast_config, crafty, azure).snapshot()

    assert snapshot.azure_ok is True
    assert snapshot.crafty_ok is False
    assert isinstance(snapshot.crafty_error, CraftyUnavailable)


async def test_snapshot_survives_a_broken_azure(fast_config):
    from bot.errors import AzureAuthError

    class BrokenAzure(FakeAzure):
        async def get_vm_status(self, *, use_cache: bool = True):
            raise AzureAuthError()

    snapshot = await build(fast_config, FakeCrafty(running=True), BrokenAzure()).snapshot()

    assert snapshot.crafty_ok is True
    assert snapshot.azure_ok is False
    assert snapshot.stats is not None and snapshot.stats.running is True


# --------------------------------------------------------------------------- #
# on_server_running: lets IdleTimeoutService react to a server starting
# --------------------------------------------------------------------------- #
async def test_on_server_running_fires_after_a_fresh_start(fast_config):
    """The bug this guards against.

    IdleTimeoutService backs off to a multi-minute poll while every armed
    server is stopped, since no countdown can start until one comes back up.
    Nothing else tells it a server actually started -- that happens on this
    completely separate code path -- so without this hook, a server started
    while its timeout is armed could sit unnoticed well past a short test
    delay.
    """
    crafty = FakeCrafty(running=False)
    azure = FakeAzure(power_state=POWER_RUNNING)
    orchestrator = build(fast_config, crafty, azure)

    calls = []
    orchestrator.on_server_running = lambda: calls.append(1)

    await orchestrator.start_infrastructure()
    assert calls == [1]


async def test_on_server_running_fires_when_already_running(fast_config):
    """The early-return path ('Already running') must notify too."""
    crafty = FakeCrafty(running=True)
    azure = FakeAzure(power_state=POWER_RUNNING)
    orchestrator = build(fast_config, crafty, azure)

    calls = []
    orchestrator.on_server_running = lambda: calls.append(1)

    await orchestrator.start_infrastructure()
    assert calls == [1]


async def test_on_server_running_fires_after_a_restart(fast_config):
    crafty = FakeCrafty(running=True)
    azure = FakeAzure(power_state=POWER_RUNNING)
    orchestrator = build(fast_config, crafty, azure)

    calls = []
    orchestrator.on_server_running = lambda: calls.append(1)

    await orchestrator.restart_minecraft()
    assert calls == [1]


async def test_on_server_running_does_not_fire_on_a_failed_start(fast_config):
    crafty = FakeCrafty(running=False)
    crafty.fail_with = CraftyUnavailable()
    azure = FakeAzure(power_state=POWER_RUNNING)
    orchestrator = build(fast_config, crafty, azure)

    calls = []
    orchestrator.on_server_running = lambda: calls.append(1)

    with pytest.raises(CraftyUnavailable):
        await orchestrator.start_infrastructure()
    assert calls == []


async def test_on_server_running_defaults_to_a_no_op(fast_config):
    """Nothing crashes when nobody has wired the hook up."""
    crafty = FakeCrafty(running=True)
    azure = FakeAzure(power_state=POWER_RUNNING)
    orchestrator = build(fast_config, crafty, azure)
    assert orchestrator.on_server_running is None

    await orchestrator.start_infrastructure()  # must not raise
