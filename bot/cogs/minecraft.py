"""``/minecraft …`` — one-shot commands that do the right thing end to end."""

from __future__ import annotations

import logging

import discord
from discord import app_commands

from bot.client import CraftyBot
from bot.cogs.base import ServiceCog, server_autocomplete
from bot.permissions import Tier
from bot.ui import embeds
from bot.ui.views import ConfirmView

logger = logging.getLogger(__name__)


class MinecraftCog(ServiceCog):
    """Combined Azure + Crafty workflows for people who just want to play."""

    group = app_commands.Group(
        name="minecraft", description="Start or stop the whole Minecraft stack"
    )

    @group.command(name="start", description="Start the VM if needed, then start Minecraft")
    @app_commands.describe(server="Crafty server (defaults to the configured one)")
    @app_commands.autocomplete(server=server_autocomplete)
    async def start(
        self, interaction: discord.Interaction, server: str | None = None
    ) -> None:
        if not await self.guard(interaction, Tier.SERVER):
            return

        # Starting the VM costs money, so it needs the Azure tier.
        if self.azure.enabled and not await self.orchestrator.is_vm_running():
            if not await self.guard(interaction, Tier.AZURE):
                return

        logger.info("User %s starting the Minecraft stack", interaction.user.id)
        await self.run_workflow(
            interaction,
            "Starting Minecraft infrastructure",
            lambda progress: self.orchestrator.start_infrastructure(
                server, on_progress=progress
            ),
            success_message="Infrastructure is ready — have fun!",
        )

    @group.command(name="stop", description="Stop Minecraft, optionally deallocating the VM")
    @app_commands.describe(
        shutdown_vm="Also deallocate the Azure VM afterwards",
        server="Crafty server (defaults to the configured one)",
    )
    @app_commands.autocomplete(server=server_autocomplete)
    async def stop(
        self,
        interaction: discord.Interaction,
        shutdown_vm: bool | None = None,
        server: str | None = None,
    ) -> None:
        if not await self.guard(interaction, Tier.SERVER):
            return

        stop_vm = self.config.auto_shutdown_vm if shutdown_vm is None else shutdown_vm
        stop_vm = stop_vm and self.azure.enabled
        if stop_vm and not self.bot.permissions.allows(interaction.user, Tier.AZURE):
            if shutdown_vm:
                await self.guard(interaction, Tier.AZURE)
                return
            stop_vm = False

        async def run(target_interaction: discord.Interaction) -> None:
            delay = self.config.auto_shutdown_delay if stop_vm else None
            await self.run_workflow(
                target_interaction,
                "Stopping Minecraft" + (" and the VM" if stop_vm else ""),
                lambda progress: self.orchestrator.stop_minecraft(
                    server, shutdown_vm=stop_vm, delay=delay, on_progress=progress
                ),
            )

        if not stop_vm:
            await run(interaction)
            return

        async def confirmed(button_interaction: discord.Interaction) -> None:
            await button_interaction.response.defer()
            await run(button_interaction)

        view = ConfirmView(
            checker=self.bot.permissions,
            owner_id=interaction.user.id,
            tier=Tier.AZURE,
            on_confirm=confirmed,
            confirm_label="Stop everything",
        )
        wait_note = (
            f"\nThe VM is deallocated {self.config.auto_shutdown_delay}s after Minecraft stops."
            if self.config.auto_shutdown_delay
            else ""
        )
        await interaction.response.send_message(
            embed=embeds.confirm_embed(
                "Stop Minecraft and the Azure VM?",
                "Minecraft will be shut down gracefully through Crafty, then the VM "
                "will be deallocated." + wait_note,
            ),
            view=view,
        )
        view.message = await interaction.original_response()

    @group.command(name="restart", description="Restart Minecraft (starting the VM if needed)")
    @app_commands.describe(server="Crafty server (defaults to the configured one)")
    @app_commands.autocomplete(server=server_autocomplete)
    async def restart(
        self, interaction: discord.Interaction, server: str | None = None
    ) -> None:
        if not await self.guard(interaction, Tier.SERVER):
            return
        await self.run_workflow(
            interaction,
            "Restarting Minecraft",
            lambda progress: self.orchestrator.restart_minecraft(server, on_progress=progress),
        )


async def setup(bot: CraftyBot) -> None:
    await bot.add_cog(MinecraftCog(bot))
