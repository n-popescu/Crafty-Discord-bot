"""``/schedule …`` — drive Crafty's own task scheduler.

Crafty already has a scheduler with task chains, so the bot never re-implements
one; it only triggers and inspects Crafty's tasks.

Listing tasks is intentionally absent: ``GET /api/v2/servers/{id}/tasks`` and
``/tasks/{id}/children`` are stub handlers in Crafty 4.10.8, so there is no
supported way to enumerate schedules over the API. Task IDs are visible in the
Crafty panel under *Server → Schedule*.
"""

from __future__ import annotations

import logging

import discord
from discord import app_commands

from bot.client import CraftyBot
from bot.cogs.base import ServiceCog, server_autocomplete
from bot.errors import BotError
from bot.permissions import Tier
from bot.ui import embeds

logger = logging.getLogger(__name__)


class ScheduleCog(ServiceCog):
    """Read and trigger Crafty schedules. Requires the SCHEDULE API permission."""

    group = app_commands.Group(name="schedule", description="Crafty task scheduler")

    @group.command(name="info", description="Show one scheduled task from Crafty")
    @app_commands.describe(
        task_id="Numeric task ID from the Crafty panel (Server → Schedule)",
        server="Crafty server (defaults to the configured one)",
    )
    @app_commands.autocomplete(server=server_autocomplete)
    async def info(
        self,
        interaction: discord.Interaction,
        task_id: app_commands.Range[int, 1, 10**9],
        server: str | None = None,
    ) -> None:
        if not await self.guard(interaction, Tier.SERVER):
            return
        await interaction.response.defer(ephemeral=True)
        try:
            server_id = await self.resolve(server)
            task = await self.crafty.get_task(server_id, str(task_id))
        except BotError as exc:
            await self._fail(interaction, exc)
            return

        embed = embeds.panel_embed(f"🗓️ Task {task.task_id}")
        embed.add_field(name="Name", value=task.name or embeds.NA, inline=True)
        embed.add_field(name="Action", value=f"`{task.action}`" if task.action else embeds.NA, inline=True)
        embed.add_field(name="Enabled", value="Yes" if task.enabled else "No", inline=True)
        if task.interval:
            embed.add_field(name="Interval", value=task.interval, inline=True)
        if task.cron:
            embed.add_field(name="Cron", value=f"`{task.cron}`", inline=True)
        if task.next_run:
            embed.add_field(name="Next run", value=task.next_run, inline=True)
        await interaction.edit_original_response(embed=embed)

    @group.command(name="run", description="Run a Crafty scheduled task immediately")
    @app_commands.describe(
        task_id="Numeric task ID from the Crafty panel (Server → Schedule)",
        cascade="Also run the reaction tasks chained to this one",
        server="Crafty server (defaults to the configured one)",
    )
    @app_commands.autocomplete(server=server_autocomplete)
    async def run(
        self,
        interaction: discord.Interaction,
        task_id: app_commands.Range[int, 1, 10**9],
        cascade: bool = False,
        server: str | None = None,
    ) -> None:
        if not await self.guard(interaction, Tier.SERVER):
            return
        await interaction.response.defer()
        try:
            server_id = await self.resolve(server)
            await self.crafty.run_task(server_id, str(task_id), cascade=cascade)
        except BotError as exc:
            await self._fail(interaction, exc)
            return

        logger.info("User %s triggered task %s", interaction.user.id, task_id)
        detail = f"Crafty queued task `{task_id}` for immediate execution."
        if cascade:
            detail += "\nChained reaction tasks will run too."
        await interaction.edit_original_response(
            embed=embeds.success_embed("Task triggered", detail)
        )

    async def _fail(self, interaction: discord.Interaction, error: BotError) -> None:
        embed = embeds.error_embed("Scheduler request failed", error.user_message)
        try:
            await interaction.edit_original_response(embed=embed, view=None)
        except discord.HTTPException:
            await interaction.followup.send(embed=embed, ephemeral=True)


async def setup(bot: CraftyBot) -> None:
    await bot.add_cog(ScheduleCog(bot))
