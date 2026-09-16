"""Orchestration of the Azure VM and the Minecraft server.

The orchestrator owns every multi-step workflow so that the Discord layer only
has to render progress. Each workflow reports progress through a list of
:class:`Step` objects, which the Discord layer turns into a single, repeatedly
edited message instead of a flood of new ones.

The golden rule of ``stop``: Minecraft is always shut down *through Crafty* and
confirmed stopped before the VM is deallocated, unless an administrator
explicitly forces it.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from enum import Enum
from typing import Awaitable, Callable, Sequence

from bot.config import Config
from bot.errors import BotError, OperationTimeout, OrchestrationError
from bot.services.azure import POWER_RUNNING, AzureService, VmStatus
from bot.services.crafty import CraftyService, ServerStats
from bot.utils import poll_until

logger = logging.getLogger(__name__)

ProgressCallback = Callable[[Sequence["Step"]], Awaitable[None]]


class StepState(Enum):
    PENDING = "pending"
    ACTIVE = "active"
    DONE = "done"
    SKIPPED = "skipped"
    FAILED = "failed"


@dataclass
class Step:
    """One stage of a workflow, rendered as a single line in Discord."""

    key: str
    label: str
    state: StepState = StepState.PENDING
    detail: str = ""


@dataclass
class Workflow:
    """A sequence of steps plus a throttled progress callback."""

    steps: list[Step]
    on_progress: ProgressCallback | None = None

    def step(self, key: str) -> Step:
        for step in self.steps:
            if step.key == key:
                return step
        raise KeyError(key)

    async def mark(
        self, key: str, state: StepState, detail: str = "", *, push: bool = True
    ) -> None:
        step = self.step(key)
        step.state = state
        step.detail = detail
        if push:
            await self.push()

    async def push(self) -> None:
        if self.on_progress is not None:
            await self.on_progress(self.steps)


@dataclass(frozen=True)
class InfraSnapshot:
    """Everything ``/status`` and ``/health`` need, gathered concurrently."""

    vm: VmStatus | None = None
    vm_error: BotError | None = None
    stats: ServerStats | None = None
    crafty_error: BotError | None = None
    server_id: str = ""
    server_name: str = ""

    @property
    def crafty_ok(self) -> bool:
        return self.crafty_error is None and self.stats is not None

    @property
    def azure_ok(self) -> bool:
        return self.vm_error is None and self.vm is not None


class InfraOrchestrator:
    """Coordinates :class:`AzureService` and :class:`CraftyService`."""

    def __init__(self, config: Config, crafty: CraftyService, azure: AzureService) -> None:
        self._config = config
        self._crafty = crafty
        self._azure = azure
        #: Called whenever a workflow confirms a server is running. Set after
        #: construction (by ``CraftyBot``) rather than injected here, to avoid
        #: a circular dependency: IdleTimeoutService itself depends on this
        #: orchestrator. It exists so that watcher can react immediately to a
        #: server starting instead of waiting out its own poll backoff --
        #: which can be minutes long while every armed server is stopped, since
        #: nothing else tells it a stopped server just came back up.
        self.on_server_running: Callable[[], None] | None = None

    @property
    def azure_enabled(self) -> bool:
        return self._azure.enabled

    def _notify_running(self) -> None:
        if self.on_server_running is not None:
            self.on_server_running()

    # ------------------------------------------------------------------ #
    # Read-only snapshot
    # ------------------------------------------------------------------ #
    async def snapshot(self, server_id: str | None = None) -> InfraSnapshot:
        """Collect Azure and Crafty state in parallel, tolerating either failing."""
        vm_task = asyncio.create_task(self._safe_vm_status())
        crafty_task = asyncio.create_task(self._safe_stats(server_id))
        vm, vm_error = await vm_task
        stats, crafty_error, resolved_id = await crafty_task

        return InfraSnapshot(
            vm=vm,
            vm_error=vm_error,
            stats=stats,
            crafty_error=crafty_error,
            server_id=resolved_id,
            server_name=(stats.name if stats and stats.name else ""),
        )

    async def _safe_vm_status(self) -> tuple[VmStatus | None, BotError | None]:
        if not self._azure.enabled:
            return None, None
        try:
            return await self._azure.get_vm_status(), None
        except BotError as exc:
            return None, exc

    async def _safe_stats(
        self, server_id: str | None
    ) -> tuple[ServerStats | None, BotError | None, str]:
        try:
            resolved = await self._crafty.resolve_server_id(server_id)
            stats = await self._crafty.get_stats(
                resolved, ttl=self._config.status_cache_ttl
            )
            return stats, None, resolved
        except BotError as exc:
            return None, exc, server_id or ""

    # ------------------------------------------------------------------ #
    # Start workflow
    # ------------------------------------------------------------------ #
    def build_start_workflow(self, on_progress: ProgressCallback | None) -> Workflow:
        steps = []
        if self._azure.enabled:
            steps.append(Step("vm", "Azure VM"))
            steps.append(Step("crafty", "Crafty Controller"))
        steps.append(Step("minecraft", "Minecraft server"))
        return Workflow(steps=steps, on_progress=on_progress)

    async def start_infrastructure(
        self,
        server_id: str | None = None,
        *,
        on_progress: ProgressCallback | None = None,
    ) -> Workflow:
        """Bring the whole stack up: VM -> Crafty reachable -> Minecraft running."""
        workflow = self.build_start_workflow(on_progress)
        try:
            if self._azure.enabled:
                await self._ensure_vm_running(workflow)
                await self._ensure_crafty_reachable(workflow)

            await workflow.mark("minecraft", StepState.ACTIVE, "Resolving server…")
            resolved = await self._crafty.resolve_server_id(server_id)
            stats = await self._crafty.get_stats(resolved)
            if stats.running:
                await workflow.mark("minecraft", StepState.DONE, "Already running")
                self._notify_running()
                return workflow

            await self._crafty.start_server(resolved)
            await workflow.mark("minecraft", StepState.ACTIVE, "Starting…")
            started = await poll_until(
                lambda: self._is_running(resolved),
                timeout=self._config.start_timeout,
                initial_interval=3.0,
                max_interval=10.0,
            )
            if not started:
                await workflow.mark("minecraft", StepState.FAILED, "Timed out")
                raise OperationTimeout("Minecraft did not report itself running in time.")
            await workflow.mark("minecraft", StepState.DONE, "Running")
            self._notify_running()
            return workflow
        except BotError:
            await self._fail_active(workflow)
            raise

    async def _ensure_vm_running(self, workflow: Workflow) -> None:
        await workflow.mark("vm", StepState.ACTIVE, "Checking power state…")
        status = await self._azure.get_vm_status(use_cache=False)
        if status.is_running:
            await workflow.mark("vm", StepState.DONE, "Already running")
            return

        await self._azure.start_vm()
        await workflow.mark("vm", StepState.ACTIVE, "Starting…")
        running = await self._azure.wait_for_running(timeout=self._config.start_timeout)
        if running is None:
            await workflow.mark("vm", StepState.FAILED, "Timed out")
            raise OperationTimeout("The Azure VM did not reach the running state in time.")
        await workflow.mark("vm", StepState.DONE, "Running")

    async def _ensure_crafty_reachable(self, workflow: Workflow) -> None:
        await workflow.mark("crafty", StepState.ACTIVE, "Waiting for connection…")
        reachable = await poll_until(
            self._crafty.check_connection,
            timeout=self._config.start_timeout,
            initial_interval=5.0,
            max_interval=20.0,
        )
        if not reachable:
            await workflow.mark("crafty", StepState.FAILED, "Unreachable")
            raise OperationTimeout("Crafty did not become reachable in time.")
        await workflow.mark("crafty", StepState.DONE, "Connected")

    # ------------------------------------------------------------------ #
    # Stop workflows
    # ------------------------------------------------------------------ #
    def build_stop_workflow(
        self, *, shutdown_vm: bool, on_progress: ProgressCallback | None
    ) -> Workflow:
        steps = [Step("minecraft", "Minecraft server")]
        if shutdown_vm and self._azure.enabled:
            steps.append(Step("vm", "Azure VM"))
        return Workflow(steps=steps, on_progress=on_progress)

    async def stop_minecraft(
        self,
        server_id: str | None = None,
        *,
        shutdown_vm: bool = False,
        delay: int | None = None,
        on_progress: ProgressCallback | None = None,
    ) -> Workflow:
        """Stop Minecraft gracefully, optionally deallocating the VM afterwards."""
        shutdown_vm = shutdown_vm and self._azure.enabled
        workflow = self.build_stop_workflow(shutdown_vm=shutdown_vm, on_progress=on_progress)
        try:
            resolved = await self._crafty.resolve_server_id(server_id)
            await self._stop_minecraft_step(workflow, resolved)

            if shutdown_vm:
                wait_for = self._config.auto_shutdown_delay if delay is None else delay
                await self._deallocate_step(workflow, delay=wait_for)
            return workflow
        except BotError:
            await self._fail_active(workflow)
            raise

    async def stop_infrastructure(
        self,
        server_id: str | None = None,
        *,
        force: bool = False,
        on_progress: ProgressCallback | None = None,
    ) -> Workflow:
        """Deallocate the VM, shutting Minecraft down first unless ``force``."""
        if not self._azure.enabled:
            raise OrchestrationError("Azure integration is not configured.")

        workflow = self.build_stop_workflow(shutdown_vm=True, on_progress=on_progress)
        try:
            status = await self._azure.get_vm_status(use_cache=False)
            if status.is_stopped:
                await workflow.mark("minecraft", StepState.SKIPPED, "VM already stopped")
                await workflow.mark("vm", StepState.DONE, f"Already {status.power_state}")
                return workflow

            if force:
                await workflow.mark(
                    "minecraft", StepState.SKIPPED, "Skipped (forced shutdown)"
                )
            else:
                try:
                    resolved = await self._crafty.resolve_server_id(server_id)
                    await self._stop_minecraft_step(workflow, resolved)
                except BotError as exc:
                    # Crafty being unreachable must not strand a running VM, but
                    # we make it explicit rather than silently deallocating.
                    await workflow.mark(
                        "minecraft", StepState.FAILED, exc.user_message
                    )
                    raise OrchestrationError(
                        "Minecraft could not be stopped through Crafty, so the VM was "
                        "left running. Use the force option to deallocate anyway."
                    ) from exc

            await self._deallocate_step(workflow, delay=0)
            return workflow
        except BotError:
            await self._fail_active(workflow)
            raise

    async def _stop_minecraft_step(self, workflow: Workflow, server_id: str) -> None:
        await workflow.mark("minecraft", StepState.ACTIVE, "Checking state…")
        stats = await self._crafty.get_stats(server_id)
        if not stats.running:
            await workflow.mark("minecraft", StepState.DONE, "Already stopped")
            return

        await self._crafty.stop_server(server_id)
        await workflow.mark("minecraft", StepState.ACTIVE, "Saving and stopping…")
        stopped = await poll_until(
            lambda: self._is_stopped(server_id),
            timeout=self._config.stop_timeout,
            initial_interval=3.0,
            max_interval=10.0,
        )
        if not stopped:
            await workflow.mark("minecraft", StepState.FAILED, "Timed out")
            raise OperationTimeout(
                "Minecraft did not stop in time; the VM was left running."
            )
        await workflow.mark("minecraft", StepState.DONE, "Stopped")

    async def _deallocate_step(self, workflow: Workflow, *, delay: int) -> None:
        if delay > 0:
            await workflow.mark(
                "vm", StepState.ACTIVE, f"Waiting {delay}s before deallocating…"
            )
            await asyncio.sleep(delay)

        await workflow.mark("vm", StepState.ACTIVE, "Deallocating…")
        await self._azure.stop_vm(deallocate=True)
        stopped = await self._azure.wait_for_stopped(timeout=self._config.stop_timeout)
        if stopped is None:
            await workflow.mark("vm", StepState.FAILED, "Timed out")
            raise OperationTimeout("The Azure VM did not confirm deallocation in time.")
        await workflow.mark("vm", StepState.DONE, "Deallocated")

    # ------------------------------------------------------------------ #
    # Restart
    # ------------------------------------------------------------------ #
    async def restart_minecraft(
        self,
        server_id: str | None = None,
        *,
        on_progress: ProgressCallback | None = None,
    ) -> Workflow:
        """Restart Minecraft through Crafty, starting the VM first if needed."""
        workflow = Workflow(steps=[Step("minecraft", "Minecraft server")], on_progress=on_progress)
        if self._azure.enabled:
            workflow.steps.insert(0, Step("vm", "Azure VM"))
            workflow.steps.insert(1, Step("crafty", "Crafty Controller"))

        try:
            if self._azure.enabled:
                status = await self._azure.get_vm_status(use_cache=False)
                if not status.is_running:
                    await self._ensure_vm_running(workflow)
                    await self._ensure_crafty_reachable(workflow)
                else:
                    await workflow.mark("vm", StepState.DONE, "Running")
                    await workflow.mark("crafty", StepState.DONE, "Connected")

            resolved = await self._crafty.resolve_server_id(server_id)
            await workflow.mark("minecraft", StepState.ACTIVE, "Restarting…")
            await self._crafty.restart_server(resolved)
            running = await poll_until(
                lambda: self._is_running(resolved),
                timeout=self._config.start_timeout,
                initial_interval=5.0,
                max_interval=10.0,
            )
            if not running:
                await workflow.mark("minecraft", StepState.FAILED, "Timed out")
                raise OperationTimeout("Minecraft did not come back up in time.")
            await workflow.mark("minecraft", StepState.DONE, "Running")
            self._notify_running()
            return workflow
        except BotError:
            await self._fail_active(workflow)
            raise

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #
    async def _is_running(self, server_id: str) -> bool:
        try:
            stats = await self._crafty.get_stats(server_id)
        except BotError:
            return False
        return stats.running

    async def _is_stopped(self, server_id: str) -> bool:
        try:
            stats = await self._crafty.get_stats(server_id)
        except BotError:
            # A Crafty that stopped answering cannot confirm the shutdown.
            return False
        return not stats.running

    @staticmethod
    async def _fail_active(workflow: Workflow) -> None:
        for step in workflow.steps:
            if step.state is StepState.ACTIVE:
                step.state = StepState.FAILED
            elif step.state is StepState.PENDING:
                step.state = StepState.SKIPPED
        await workflow.push()

    async def vm_power_state(self) -> str:
        """Best-effort power state string for error messages."""
        if not self._azure.enabled:
            return "not configured"
        try:
            status = await self._azure.get_vm_status()
        except BotError:
            return "unknown"
        return status.power_state

    async def is_vm_running(self) -> bool:
        if not self._azure.enabled:
            return False
        try:
            status = await self._azure.get_vm_status()
        except BotError:
            return False
        return status.power_state == POWER_RUNNING
