"""``/azure …`` — lifecycle of the VM that hosts Crafty."""

from __future__ import annotations

import logging

import discord
from discord import app_commands

from bot.client import CraftyBot
from bot.cogs.base import ServiceCog, server_autocomplete
from bot.errors import BotError, OperationTimeout
from bot.permissions import Tier
from bot.services.orchestrator import Step, StepState
from bot.ui import embeds
from bot.ui.views import ConfirmView

logger = logging.getLogger(__name__)


class AzureCog(ServiceCog):
    """Azure commands are administrator/Azure-operator territory."""

    group = app_commands.Group(name="azure", description="Control the Azure VM hosting Crafty")

    @group.command(name="status", description="Power state of the Azure VM")
    async def status(self, interaction: discord.Interaction) -> None:
        if not await self.guard(interaction, Tier.EVERYONE):
            return
        if not await self.require_azure(interaction):
            return
        await interaction.response.defer()

        try:
            vm = await self.azure.get_vm_status(use_cache=False)
            public_ip = await self.azure.get_public_ip() if vm.is_running else None
        except BotError as exc:
            await self._fail(interaction, exc)
            return

        await interaction.edit_original_response(embed=embeds.azure_embed(vm, public_ip))

    @group.command(name="ip", description="Show the VM's IP addresses")
    async def ip(self, interaction: discord.Interaction) -> None:
        if not await self.guard(interaction, Tier.EVERYONE):
            return
        if not await self.require_azure(interaction):
            return
        await interaction.response.defer()

        try:
            vm = await self.azure.get_vm_status()
            public_ip = await self.azure.get_public_ip()
            private_ip = await self.azure.get_private_ip()
        except BotError as exc:
            await self._fail(interaction, exc)
            return

        embed = embeds.azure_embed(vm, public_ip, private_ip)
        if not public_ip:
            embed.add_field(
                name="No public IP",
                value="The VM has no public address. Players connect through a VPN, "
                "a load balancer or a DNS name instead.",
                inline=False,
            )
        await interaction.edit_original_response(embed=embed)

    # ------------------------------------------------------------------ #
    @group.command(name="start", description="Start the VM, then wait for Crafty and Minecraft")
    @app_commands.describe(
        start_minecraft="Also start the Minecraft server once Crafty answers (default: yes)",
        server="Crafty server (defaults to the configured one)",
    )
    @app_commands.autocomplete(server=server_autocomplete)
    async def start(
        self,
        interaction: discord.Interaction,
        start_minecraft: bool = True,
        server: str | None = None,
    ) -> None:
        if not await self.guard(interaction, Tier.AZURE):
            return
        if not await self.require_azure(interaction):
            return

        if start_minecraft:
            await self.run_workflow(
                interaction,
                "Starting Minecraft infrastructure",
                lambda progress: self.orchestrator.start_infrastructure(
                    server, on_progress=progress
                ),
                success_message="Infrastructure is ready.",
            )
            return

        await interaction.response.defer()
        try:
            vm = await self.azure.get_vm_status(use_cache=False)
            if vm.is_running:
                await interaction.edit_original_response(
                    embed=embeds.success_embed(
                        "VM already running", f"`{vm.name}` is already up."
                    )
                )
                return
            await self.azure.start_vm()
            running = await self.azure.wait_for_running(timeout=self.config.start_timeout)
        except BotError as exc:
            await self._fail(interaction, exc)
            return

        if running is None:
            await interaction.edit_original_response(
                embed=embeds.error_embed(
                    "VM start timed out", "Azure did not report the VM as running in time."
                )
            )
            return
        logger.info("User %s started Azure VM %s", interaction.user.id, self.azure.vm_name)
        await interaction.edit_original_response(embed=embeds.azure_embed(running))

    # ------------------------------------------------------------------ #
    @group.command(
        name="stop", description="Stop Minecraft gracefully, then deallocate the VM"
    )
    @app_commands.describe(
        force="Administrators only: deallocate even if Minecraft is still running",
        server="Crafty server (defaults to the configured one)",
    )
    @app_commands.autocomplete(server=server_autocomplete)
    async def stop(
        self,
        interaction: discord.Interaction,
        force: bool = False,
        server: str | None = None,
    ) -> None:
        if not await self.guard(interaction, Tier.AZURE):
            return
        if force and not await self.guard(interaction, Tier.ADMIN):
            return
        if not await self.require_azure(interaction):
            return

        detail = (
            "Minecraft will be stopped through Crafty and the Azure VM will then be "
            "**deallocated**, which also stops compute billing."
        )
        if force:
            detail = (
                "⚠️ **Forced shutdown.** The VM will be deallocated *without* stopping "
                "Minecraft first. Unsaved world changes may be lost."
            )

        async def confirmed(button_interaction: discord.Interaction) -> None:
            await button_interaction.response.defer()
            logger.info(
                "User %s stopping infrastructure (force=%s)", button_interaction.user.id, force
            )
            await self.run_workflow(
                button_interaction,
                "Stopping Minecraft infrastructure",
                lambda progress: self.orchestrator.stop_infrastructure(
                    server, force=force, on_progress=progress
                ),
                success_message="The VM is deallocated. Compute billing has stopped.",
            )

        view = ConfirmView(
            checker=self.bot.permissions,
            owner_id=interaction.user.id,
            tier=Tier.ADMIN if force else Tier.AZURE,
            on_confirm=confirmed,
            confirm_label="Stop everything",
        )
        await interaction.response.send_message(
            embed=embeds.confirm_embed("Stop the Minecraft infrastructure?", detail),
            view=view,
        )
        view.message = await interaction.original_response()

    # ------------------------------------------------------------------ #
    @group.command(name="restart", description="Reboot the Azure VM (Minecraft is stopped first)")
    @app_commands.describe(server="Crafty server (defaults to the configured one)")
    @app_commands.autocomplete(server=server_autocomplete)
    async def restart(
        self, interaction: discord.Interaction, server: str | None = None
    ) -> None:
        if not await self.guard(interaction, Tier.ADMIN):
            return
        if not await self.require_azure(interaction):
            return

        async def confirmed(button_interaction: discord.Interaction) -> None:
            await button_interaction.response.defer()

            async def runner(progress):
                workflow = await self.orchestrator.stop_minecraft(
                    server, shutdown_vm=False, on_progress=progress
                )
                step = Step("vm", "Azure VM", StepState.ACTIVE, "Rebooting…")
                workflow.steps.append(step)
                await workflow.push()

                await self.azure.restart_vm()
                running = await self.azure.wait_for_running(timeout=self.config.start_timeout)
                if running is None:
                    step.state = StepState.FAILED
                    step.detail = "Timed out"
                    await workflow.push()
                    raise OperationTimeout("The VM did not come back up in time.")

                step.state = StepState.DONE
                step.detail = "Rebooted"
                await workflow.push()
                return workflow

            await self.run_workflow(button_interaction, "Rebooting the Azure VM", runner)

        view = ConfirmView(
            checker=self.bot.permissions,
            owner_id=interaction.user.id,
            tier=Tier.ADMIN,
            on_confirm=confirmed,
            confirm_label="Reboot",
        )
        await interaction.response.send_message(
            embed=embeds.confirm_embed(
                "Reboot the Azure VM?",
                "Minecraft will be stopped through Crafty first, then the VM reboots. "
                "The VM stays allocated, so billing continues.",
            ),
            view=view,
        )
        view.message = await interaction.original_response()

    # ------------------------------------------------------------------ #
    async def _fail(self, interaction: discord.Interaction, error: BotError) -> None:
        embed = embeds.error_embed("Azure operation failed", error.user_message)
        try:
            await interaction.edit_original_response(embed=embed, view=None)
        except discord.HTTPException:
            await interaction.followup.send(embed=embed, ephemeral=True)


async def setup(bot: CraftyBot) -> None:
    await bot.add_cog(AzureCog(bot))
