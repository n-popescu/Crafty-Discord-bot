"""``/schedule …`` — drive Crafty's own task scheduler.

Crafty already has a scheduler with task chains, so the bot never re-implements
one; it only triggers and inspects Crafty's tasks.

Listing tasks is intentionally absent: ``GET /api/v2/servers/{id}/tasks`` and
``/tasks/{id}/children`` are stub handlers in Crafty 4.10.8, so there is no
supported way to enumerate schedules over the API. Creating, inspecting,
pausing, deleting and running individual tasks all work, and `/schedule create`
reports the new task's ID so it can be used straight away; IDs are also visible
in the Crafty panel under *Server → Schedule*.

Handing scheduling to Crafty rather than running timers in the bot is what keeps
a Pi Zero W out of the loop entirely: a nightly restart or backup fires from the
Crafty host whether or not the bot is even running.
"""

from __future__ import annotations

import logging

import discord
from discord import app_commands

from bot.client import CraftyBot
from bot.cogs.base import ServiceCog, server_autocomplete
from bot.errors import BotError
from bot.permissions import Tier
from bot.services.crafty import TASK_ACTIONS
from bot.ui import embeds
from bot.ui.views import ConfirmView

logger = logging.getLogger(__name__)

#: One choice per :data:`bot.services.crafty.TASK_ACTIONS` entry.
ACTION_LABELS = {
    "start": "Start the server",
    "stop": "Stop the server",
    "restart": "Restart the server",
    "backup": "Run a backup (needs backup_id)",
    "command": "Send a console command (needs command)",
}
ACTION_CHOICES = [
    app_commands.Choice(name=ACTION_LABELS[action], value=action)
    for action in TASK_ACTIONS
]


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

    @group.command(name="create", description="Create a Crafty schedule (cron)")
    @app_commands.describe(
        action="What the task should do",
        cron="Five-field cron, e.g. `0 5 * * *` for 05:00 every day",
        name="A label for the task in Crafty",
        command="Console command to run (only for the `command` action)",
        backup_id="Backup configuration to run (only for the `backup` action)",
        server="Crafty server (defaults to the configured one)",
    )
    @app_commands.choices(action=ACTION_CHOICES)
    @app_commands.autocomplete(server=server_autocomplete)
    async def create(
        self,
        interaction: discord.Interaction,
        action: app_commands.Choice[str],
        cron: app_commands.Range[str, 5, 100],
        name: app_commands.Range[str, 1, 60] = "Discord bot task",
        command: str | None = None,
        backup_id: str | None = None,
        server: str | None = None,
    ) -> None:
        if not await self.guard(interaction, Tier.ADMIN):
            return
        await interaction.response.defer(ephemeral=True)
        try:
            server_id = await self.resolve(server)
            task_id = await self.crafty.create_task(
                server_id,
                name=name,
                action=action.value,
                cron=cron.strip(),
                command=command,
                action_id=backup_id,
            )
            label = await self.server_label(server_id)
        except BotError as exc:
            await self._fail(interaction, exc)
            return

        logger.info("User %s created schedule on server %s", interaction.user.id, server_id)
        await interaction.edit_original_response(
            embed=embeds.success_embed(
                "Schedule created",
                f"**{name}** will `{action.value}` on **{label}** at `{cron.strip()}`."
                + (f"\nTask ID: `{task_id}`" if task_id else "")
                + "\n\nCrafty runs this on its own host, so it fires even when the "
                "bot is offline.",
            )
        )

    @group.command(name="toggle", description="Pause or resume a Crafty schedule")
    @app_commands.describe(
        task_id="Numeric task ID from the Crafty panel (Server → Schedule)",
        enabled="Whether Crafty should run this task",
        server="Crafty server (defaults to the configured one)",
    )
    @app_commands.autocomplete(server=server_autocomplete)
    async def toggle(
        self,
        interaction: discord.Interaction,
        task_id: app_commands.Range[int, 1, 10**9],
        enabled: bool,
        server: str | None = None,
    ) -> None:
        if not await self.guard(interaction, Tier.ADMIN):
            return
        await interaction.response.defer(ephemeral=True)
        try:
            server_id = await self.resolve(server)
            await self.crafty.set_task_enabled(server_id, str(task_id), enabled)
        except BotError as exc:
            await self._fail(interaction, exc)
            return
        await interaction.edit_original_response(
            embed=embeds.success_embed(
                "Schedule updated",
                f"Task `{task_id}` is now **{'enabled' if enabled else 'paused'}**.",
            )
        )

    @group.command(name="delete", description="Delete a Crafty schedule")
    @app_commands.describe(
        task_id="Numeric task ID from the Crafty panel (Server → Schedule)",
        server="Crafty server (defaults to the configured one)",
    )
    @app_commands.autocomplete(server=server_autocomplete)
    async def delete(
        self,
        interaction: discord.Interaction,
        task_id: app_commands.Range[int, 1, 10**9],
        server: str | None = None,
    ) -> None:
        if not await self.guard(interaction, Tier.ADMIN):
            return

        async def confirmed(button_interaction: discord.Interaction) -> None:
            await button_interaction.response.defer()
            try:
                server_id = await self.resolve(server)
                await self.crafty.delete_task(server_id, str(task_id))
            except BotError as exc:
                await self._fail(button_interaction, exc)
                return
            await button_interaction.edit_original_response(
                embed=embeds.success_embed(
                    "Schedule deleted", f"Crafty removed task `{task_id}`."
                ),
                view=None,
            )

        view = ConfirmView(
            checker=self.bot.permissions,
            owner_id=interaction.user.id,
            tier=Tier.ADMIN,
            on_confirm=confirmed,
            confirm_label="Delete",
        )
        await interaction.response.send_message(
            embed=embeds.confirm_embed(
                "Delete this schedule?",
                f"Task `{task_id}` and any reaction tasks chained to it will stop "
                "running. This cannot be undone from Discord.",
            ),
            view=view,
            ephemeral=True,
        )
        view.message = await interaction.original_response()

    async def _fail(self, interaction: discord.Interaction, error: BotError) -> None:
        embed = embeds.error_embed("Scheduler request failed", error.user_message)
        try:
            await interaction.edit_original_response(embed=embed, view=None)
        except discord.HTTPException:
            await interaction.followup.send(embed=embed, ephemeral=True)


async def setup(bot: CraftyBot) -> None:
    await bot.add_cog(ScheduleCog(bot))
