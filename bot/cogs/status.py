"""``/status`` and ``/health``: the infrastructure overview and diagnostics."""

from __future__ import annotations

import asyncio
import logging

import discord
from discord import app_commands

from bot.client import CraftyBot
from bot.cogs.base import ServiceCog, server_autocomplete
from bot.errors import BotError
from bot.permissions import Tier
from bot.services.crafty import HostStats
from bot.ui import embeds
from bot.services.timeout import MAX_MINUTES
from bot.ui.views import StatusView, TimeoutModal
from bot.utils import clock_offset_hours

logger = logging.getLogger(__name__)


class StatusCog(ServiceCog):
    """The centrepiece command plus a health probe."""

    # ------------------------------------------------------------------ #
    @app_commands.command(
        name="status", description="Overview of the Minecraft server and its Azure VM"
    )
    @app_commands.describe(server="Crafty server (defaults to the configured one)")
    @app_commands.autocomplete(server=server_autocomplete)
    async def status(
        self, interaction: discord.Interaction, server: str | None = None
    ) -> None:
        if not await self.guard(interaction, Tier.EVERYONE):
            return
        await interaction.response.defer()
        await self._render(interaction, server)

    async def _render(self, interaction: discord.Interaction, server: str | None) -> None:
        """Fetch everything concurrently and (re)draw the single status message."""
        snapshot, host = await asyncio.gather(
            self.orchestrator.snapshot(server), self._host_stats()
        )
        timeout = self.bot.timeouts.get(snapshot.server_id)
        embed = embeds.status_embed(
            snapshot,
            host,
            clock_offset=clock_offset_hours(self.config.crafty_utc_offset),
            timeout=timeout,
        )

        running = bool(snapshot.stats and snapshot.stats.running)
        # Without a resolved server there is nothing to arm a timeout against,
        # so the switch is left off the message entirely.
        toggle = None
        if snapshot.server_id:
            resolved_id = snapshot.server_id

            async def toggle(i: discord.Interaction) -> None:
                await self._on_toggle_timeout(i, server, resolved_id)

        view = StatusView(
            checker=self.bot.permissions,
            owner_id=interaction.user.id,
            refresh=lambda i: self._on_refresh(i, server),
            start=lambda i: self._on_action(i, server, "start"),
            stop=lambda i: self._on_action(i, server, "stop"),
            restart=lambda i: self._on_action(i, server, "restart"),
            running=running,
            may_control=self.bot.permissions.allows(interaction.user, Tier.SERVER),
            timeout_armed=timeout is not None,
            toggle_timeout=toggle,
        )
        message = await interaction.edit_original_response(embed=embed, view=view)
        view.message = message

    async def _host_stats(self) -> HostStats | None:
        """Crafty host CPU/RAM/disk; ``None`` when unavailable or not permitted."""
        try:
            return await self.crafty.get_host_stats()
        except BotError:
            return None

    # ------------------------------------------------------------------ #
    # Button handlers -- all of them edit the message they came from.
    # ------------------------------------------------------------------ #
    async def _on_refresh(self, interaction: discord.Interaction, server: str | None) -> None:
        await interaction.response.defer()
        await self._render(interaction, server)

    async def _on_action(
        self, interaction: discord.Interaction, server: str | None, action: str
    ) -> None:
        await interaction.response.defer()
        titles = {
            "start": "Starting Minecraft",
            "stop": "Stopping Minecraft",
            "restart": "Restarting Minecraft",
        }

        async def runner(on_progress):
            if action == "start":
                return await self.orchestrator.start_infrastructure(
                    server, on_progress=on_progress
                )
            if action == "stop":
                shutdown_vm = self.config.auto_shutdown_vm and self.bot.permissions.allows(
                    interaction.user, Tier.AZURE
                )
                return await self.orchestrator.stop_minecraft(
                    server, shutdown_vm=shutdown_vm, on_progress=on_progress
                )
            return await self.orchestrator.restart_minecraft(server, on_progress=on_progress)

        await self.run_workflow(interaction, titles[action], runner)
        # Leave the user with a fresh status panel rather than a stale workflow.
        await self._render(interaction, server)

    async def _on_toggle_timeout(
        self, interaction: discord.Interaction, server: str | None, server_id: str
    ) -> None:
        """The switch: one click disarms, one click opens the modal to arm."""
        if self.bot.timeouts.get(server_id) is not None:
            await interaction.response.defer()
            self.bot.timeouts.disarm(server_id)
            logger.info(
                "User %s disarmed the timeout for server %s from /status",
                interaction.user.id,
                server_id,
            )
            await self._render(interaction, server)
            return

        async def submitted(modal_interaction: discord.Interaction, minutes: int) -> None:
            await modal_interaction.response.defer()
            # Deallocating the VM costs the Azure tier, exactly as it does for
            # `/timeout` and `/server stop`.
            shutdown_vm = self.azure.enabled and self.bot.permissions.allows(
                modal_interaction.user, Tier.AZURE
            )
            self.bot.timeouts.arm(
                server_id,
                minutes,
                shutdown_vm=shutdown_vm,
                armed_by=modal_interaction.user.id,
            )
            logger.info(
                "User %s armed a %d min timeout for server %s from /status",
                modal_interaction.user.id,
                minutes,
                server_id,
            )
            await self._render(modal_interaction, server)

        await interaction.response.send_modal(
            TimeoutModal(
                on_submit=submitted,
                max_minutes=MAX_MINUTES,
                default=self.config.idle_shutdown_minutes,
            )
        )

    # ------------------------------------------------------------------ #
    @app_commands.command(
        name="servers", description="Every server Crafty knows about, at a glance"
    )
    async def servers(self, interaction: discord.Interaction) -> None:
        """One request for all servers, instead of one request per server.

        ``GET /servers/status`` is Crafty's own public dashboard endpoint, so
        this stays cheap however many servers are configured.
        """
        if not await self.guard(interaction, Tier.EVERYONE):
            return
        await interaction.response.defer()
        try:
            lines = await self.crafty.list_server_status()
        except BotError as exc:
            embed = embeds.error_embed(
                "Could not list servers",
                exc.user_message,
                hint=await self.bot.context_hint(exc),
            )
            await interaction.edit_original_response(embed=embed)
            return
        await interaction.edit_original_response(
            embed=embeds.servers_overview_embed(
                lines, {state.server_id: state for state in self.bot.timeouts.active()}
            )
        )

    # ------------------------------------------------------------------ #
    @app_commands.command(name="health", description="Check Discord, Crafty, Azure and Minecraft")
    async def health(self, interaction: discord.Interaction) -> None:
        if not await self.guard(interaction, Tier.EVERYONE):
            return
        await interaction.response.defer(ephemeral=True)

        reachable = await self.crafty.check_connection()
        authenticated = await self.crafty.check_authentication() if reachable else False

        azure_state = "⚪ Not configured"
        if self.azure.enabled:
            try:
                status = await self.azure.get_vm_status(use_cache=False)
                emoji = embeds.STATE_EMOJI.get(status.power_state, "⚪")
                azure_state = f"{emoji} `{status.name}` — {status.power_state}"
            except BotError as exc:
                azure_state = f"🔴 {exc.user_message}"

        minecraft_state = "⚪ Unknown"
        timeout = None
        if authenticated:
            try:
                server_id = await self.resolve(None)
                timeout = self.bot.timeouts.get(server_id)
                stats = await self.crafty.get_stats(server_id, ttl=self.config.status_cache_ttl)
                emoji = embeds.STATE_EMOJI.get(stats.state, "⚪")
                players = (
                    f" — {stats.online}/{stats.max_players or '?'} players"
                    if stats.running
                    else ""
                )
                minecraft_state = f"{emoji} {stats.state}{players}"
            except BotError as exc:
                minecraft_state = f"🔴 {exc.user_message}"

        embed = embeds.health_embed(
            discord_ok=True,
            crafty_reachable=reachable,
            crafty_authenticated=authenticated,
            azure_state=azure_state,
            minecraft_state=minecraft_state,
            latency_ms=self.bot.latency * 1000 if self.bot.latency else None,
            timeout=timeout,
        )
        await interaction.edit_original_response(embed=embed)


async def setup(bot: CraftyBot) -> None:
    await bot.add_cog(StatusCog(bot))
