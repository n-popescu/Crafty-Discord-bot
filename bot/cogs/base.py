"""Shared plumbing for every cog: permission guards, autocomplete, workflows."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Awaitable, Callable, Sequence

import discord
from discord import app_commands
from discord.ext import commands

from bot.client import CraftyBot
from bot.config import Config
from bot.errors import BotError
from bot.permissions import Tier
from bot.services.azure import AzureService
from bot.services.crafty import CraftyService
from bot.services.orchestrator import InfraOrchestrator, Step, Workflow
from bot.ui import embeds

logger = logging.getLogger(__name__)

#: Minimum delay between progress edits, to stay far away from rate limits.
EDIT_INTERVAL = 1.5


async def server_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[str]]:
    """Autocomplete Crafty servers by name; the value sent back is the server id.

    Registered on every ``server`` option, which is what keeps multi-server
    support possible without hard-coding ids.
    """
    crafty = getattr(interaction.client, "crafty", None)
    if crafty is None:
        return []
    try:
        servers = await crafty.list_servers()
    except BotError:
        return []
    needle = current.casefold()
    return [
        app_commands.Choice(name=server.name[:100], value=server.server_id)
        for server in servers
        if needle in server.name.casefold() or needle in server.server_id
    ][:25]


class ServiceCog(commands.Cog):
    """Base class giving cogs typed access to the bot's services."""

    def __init__(self, bot: CraftyBot) -> None:
        self.bot = bot

    @property
    def crafty(self) -> CraftyService:
        return self.bot.crafty

    @property
    def azure(self) -> AzureService:
        return self.bot.azure

    @property
    def orchestrator(self) -> InfraOrchestrator:
        return self.bot.orchestrator

    @property
    def config(self) -> Config:
        return self.bot.config

    # ------------------------------------------------------------------ #
    # Guards
    # ------------------------------------------------------------------ #
    async def guard(self, interaction: discord.Interaction, tier: Tier) -> bool:
        """Send an ephemeral refusal and return ``False`` when not allowed."""
        reason = self.bot.permissions.denial_reason(
            interaction.user, tier, interaction.guild_id
        )
        if reason is None:
            return True
        embed = embeds.error_embed("Not allowed", reason)
        if interaction.response.is_done():
            await interaction.followup.send(embed=embed, ephemeral=True)
        else:
            await interaction.response.send_message(embed=embed, ephemeral=True)
        return False

    async def require_azure(self, interaction: discord.Interaction) -> bool:
        if self.azure.enabled:
            return True
        embed = embeds.warning_embed(
            "Azure is not configured",
            "Set `AZURE_SUBSCRIPTION_ID`, `AZURE_RESOURCE_GROUP` and `AZURE_VM_NAME` "
            "to enable VM commands.",
        )
        if interaction.response.is_done():
            await interaction.followup.send(embed=embed, ephemeral=True)
        else:
            await interaction.response.send_message(embed=embed, ephemeral=True)
        return False

    # ------------------------------------------------------------------ #
    async def resolve(self, server: str | None) -> str:
        return await self.crafty.resolve_server_id(server)

    async def server_label(self, server_id: str) -> str:
        """Human-friendly name for a server id, falling back to the id itself."""
        try:
            for candidate in await self.crafty.list_servers():
                if candidate.server_id == server_id:
                    return candidate.name
        except BotError:
            pass
        return server_id[:8]

    # ------------------------------------------------------------------ #
    # Workflows
    # ------------------------------------------------------------------ #
    async def run_workflow(
        self,
        interaction: discord.Interaction,
        title: str,
        runner: Callable[[Callable[[Sequence[Step]], Awaitable[None]]], Awaitable[Workflow]],
        *,
        success_message: str = "",
    ) -> None:
        """Run a multi-step workflow, editing a single message as it progresses."""
        if not interaction.response.is_done():
            await interaction.response.defer()

        state = {"last": 0.0}

        async def on_progress(steps: Sequence[Step]) -> None:
            now = time.monotonic()
            if now - state["last"] < EDIT_INTERVAL:
                return
            state["last"] = now
            try:
                await interaction.edit_original_response(
                    embed=embeds.workflow_embed(title, steps), view=None
                )
            except discord.HTTPException:
                logger.debug("Progress edit dropped")

        try:
            workflow = await runner(on_progress)
        except BotError as exc:
            await self._finish(interaction, title, exc)
            return
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - one failing call must not kill the bot
            logger.exception("Workflow %s crashed", title, exc_info=exc)
            await self._finish(interaction, title, exc)
            return

        embed = embeds.workflow_embed(title, workflow.steps, finished=True)
        if success_message:
            embed.add_field(name="Result", value=success_message, inline=False)
        await self._edit(interaction, embed)

    async def _finish(
        self, interaction: discord.Interaction, title: str, error: Exception
    ) -> None:
        message = (
            error.user_message
            if isinstance(error, BotError)
            else "The bot hit an internal error. Check the logs."
        )
        embed = embeds.error_embed(f"{title} failed", message)
        if isinstance(error, BotError):
            hint = await self.bot.context_hint(error)
            if hint:
                embed.add_field(name="What to check", value=hint, inline=False)
        await self._edit(interaction, embed)

    @staticmethod
    async def _edit(interaction: discord.Interaction, embed: discord.Embed) -> None:
        try:
            await interaction.edit_original_response(embed=embed, view=None)
        except discord.HTTPException:
            try:
                await interaction.followup.send(embed=embed, ephemeral=True)
            except discord.HTTPException:
                logger.debug("Could not deliver the final workflow embed")


__all__ = ["ServiceCog", "server_autocomplete"]
