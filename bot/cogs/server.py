"""``/server …`` — everything the Crafty v2 API exposes for one server."""

from __future__ import annotations

import logging

import discord
from discord import app_commands

from bot.client import CraftyBot
from bot.cogs.base import ServiceCog, server_autocomplete
from bot.errors import BotError, CraftyNotFound
from bot.permissions import Tier
from bot.ui import embeds
from bot.ui.views import BackupSelectView, ConfirmView
from bot.utils import clock_offset_hours

logger = logging.getLogger(__name__)

#: Console commands that are worth a confirmation click before they run.
DANGEROUS_COMMANDS = frozenset(
    {
        "stop",
        "restart",
        "reload",
        "kill",
        "ban",
        "ban-ip",
        "pardon",
        "op",
        "deop",
        "whitelist",
        "save-off",
        "difficulty",
        "gamerule",
    }
)

#: ``value -> (embed title, file name, hint shown when the list is empty)``
ROSTER_FILES = {
    "whitelist": ("✅ Whitelist", "whitelist.json", "nobody has been whitelisted yet."),
    "ops": ("🛡️ Operators", "ops.json", "nobody has been opped yet."),
    "bans": ("🚫 Banned players", "banned-players.json", "nobody is banned."),
}

ROSTER_CHOICES = [
    app_commands.Choice(name="Whitelist", value="whitelist"),
    app_commands.Choice(name="Operators", value="ops"),
    app_commands.Choice(name="Banned players", value="bans"),
]

LOG_SOURCES = [
    app_commands.Choice(name="Console buffer (live terminal)", value="terminal"),
    app_commands.Choice(name="Server log file (latest.log)", value="file"),
]


class ServerCog(ServiceCog):
    """Read-only commands are open to everyone; control commands need a role."""

    @property
    def clock_offset(self) -> float:
        """Hours between the Crafty host's clock and this bot's, for uptimes."""
        return clock_offset_hours(self.config.crafty_utc_offset)

    group = app_commands.Group(name="server", description="Manage the Minecraft server through Crafty")

    # ------------------------------------------------------------------ #
    # Reads
    # ------------------------------------------------------------------ #
    @group.command(name="status", description="Detailed status of a Minecraft server")
    @app_commands.describe(server="Crafty server (defaults to the configured one)")
    @app_commands.autocomplete(server=server_autocomplete)
    async def status(
        self, interaction: discord.Interaction, server: str | None = None
    ) -> None:
        if not await self.guard(interaction, Tier.EVERYONE):
            return
        await interaction.response.defer()
        try:
            server_id = await self.resolve(server)
            stats = await self.crafty.get_stats(server_id, ttl=self.config.status_cache_ttl)
        except BotError as exc:
            await self._fail(interaction, exc)
            return
        await interaction.edit_original_response(
            embed=embeds.server_embed(stats, clock_offset=self.clock_offset)
        )

    @group.command(name="players", description="Who is online right now")
    @app_commands.describe(server="Crafty server (defaults to the configured one)")
    @app_commands.autocomplete(server=server_autocomplete)
    async def players(
        self, interaction: discord.Interaction, server: str | None = None
    ) -> None:
        if not await self.guard(interaction, Tier.EVERYONE):
            return
        await interaction.response.defer()
        try:
            server_id = await self.resolve(server)
            stats = await self.crafty.get_stats(server_id, ttl=self.config.status_cache_ttl)
        except BotError as exc:
            await self._fail(interaction, exc)
            return
        await interaction.edit_original_response(embed=embeds.players_embed(stats))

    @group.command(name="info", description="Configuration of a Crafty server")
    @app_commands.describe(server="Crafty server (defaults to the configured one)")
    @app_commands.autocomplete(server=server_autocomplete)
    async def info(
        self, interaction: discord.Interaction, server: str | None = None
    ) -> None:
        if not await self.guard(interaction, Tier.EVERYONE):
            return
        await interaction.response.defer()
        try:
            server_id = await self.resolve(server)
            data = await self.crafty.get_server(server_id)
        except BotError as exc:
            await self._fail(interaction, exc)
            return

        embed = embeds.panel_embed(
            f"⚙️ {data.get('server_name') or await self.server_label(server_id)}"
        )
        embed.add_field(name="Server ID", value=f"`{server_id}`", inline=False)
        embed.add_field(name="Type", value=str(data.get("type") or embeds.NA), inline=True)
        embed.add_field(
            name="Address",
            value=f"{data.get('server_ip') or embeds.NA}:{data.get('server_port') or '?'}",
            inline=True,
        )
        embed.add_field(
            name="Autostart", value="Yes" if data.get("auto_start") else "No", inline=True
        )
        embed.add_field(
            name="Crash detection",
            value="Yes" if data.get("crash_detection") else "No",
            inline=True,
        )
        embed.add_field(
            name="Stop command", value=f"`{data.get('stop_command') or 'stop'}`", inline=True
        )
        status = data.get("status") or {}
        if isinstance(status, dict) and status:
            embed.add_field(
                name="Update available",
                value="Yes — run `/server update`" if status.get("update_available") else "No",
                inline=True,
            )
            embed.add_field(
                name="Backing up now",
                value="Yes" if status.get("backing_up") else "No",
                inline=True,
            )
            # Crafty names this field `last_backup`, but it holds
            # `last_backup_failed` -- a boolean, not a date.
            if status.get("last_backup"):
                embed.add_field(
                    name="Last backup",
                    value="⚠️ Failed — check Crafty",
                    inline=True,
                )
        await interaction.edit_original_response(embed=embed)

    @group.command(name="history", description="CPU, RAM and player charts for the last hour")
    @app_commands.describe(server="Crafty server (defaults to the configured one)")
    @app_commands.autocomplete(server=server_autocomplete)
    async def history(
        self, interaction: discord.Interaction, server: str | None = None
    ) -> None:
        if not await self.guard(interaction, Tier.EVERYONE):
            return
        await interaction.response.defer()
        try:
            server_id = await self.resolve(server)
            samples = await self.crafty.get_history(server_id)
            name = await self.server_label(server_id)
        except BotError as exc:
            await self._fail(interaction, exc)
            return
        await interaction.edit_original_response(
            embed=embeds.history_embed(samples, name)
        )

    @group.command(name="properties", description="Read server.properties")
    @app_commands.describe(server="Crafty server (defaults to the configured one)")
    @app_commands.autocomplete(server=server_autocomplete)
    async def properties(
        self, interaction: discord.Interaction, server: str | None = None
    ) -> None:
        if not await self.guard(interaction, Tier.SERVER):
            return
        await interaction.response.defer(ephemeral=True)
        try:
            server_id = await self.resolve(server)
            props = await self.crafty.read_properties(server_id)
            name = await self.server_label(server_id)
        except BotError as exc:
            await self._fail(interaction, exc)
            return
        await interaction.edit_original_response(
            embed=embeds.properties_embed(props, name)
        )

    @group.command(name="roster", description="Read the whitelist, operators or ban list")
    @app_commands.describe(
        which="Which list to read",
        server="Crafty server (defaults to the configured one)",
    )
    @app_commands.choices(which=ROSTER_CHOICES)
    @app_commands.autocomplete(server=server_autocomplete)
    async def roster(
        self,
        interaction: discord.Interaction,
        which: app_commands.Choice[str],
        server: str | None = None,
    ) -> None:
        if not await self.guard(interaction, Tier.SERVER):
            return
        await interaction.response.defer(ephemeral=True)
        title, filename, empty_hint = ROSTER_FILES[which.value]
        try:
            server_id = await self.resolve(server)
            name = await self.server_label(server_id)
        except BotError as exc:
            await self._fail(interaction, exc)
            return

        try:
            players = await self.crafty.read_player_list(server_id, filename)
        except CraftyNotFound:
            # Minecraft only writes these files once the list is first used.
            await interaction.edit_original_response(
                embed=embeds.warning_embed(
                    title, f"`{filename}` does not exist yet — {empty_hint}"
                )
            )
            return
        except BotError as exc:
            await self._fail(interaction, exc)
            return
        await interaction.edit_original_response(
            embed=embeds.player_list_embed(title, players, name, empty_hint=empty_hint)
        )

    @group.command(name="resources", description="CPU, RAM and disk of the Crafty host")
    async def resources(self, interaction: discord.Interaction) -> None:
        if not await self.guard(interaction, Tier.EVERYONE):
            return
        await interaction.response.defer()
        try:
            host = await self.crafty.get_host_stats()
        except BotError as exc:
            await self._fail(interaction, exc)
            return

        embed = embeds.panel_embed("🖥️ Crafty host")
        embed.add_field(
            name="CPU",
            value=f"{host.cpu_percent:.0f}%" if host.cpu_percent is not None else embeds.NA,
            inline=True,
        )
        embed.add_field(
            name="RAM",
            value=(
                f"{host.memory_used} / {host.memory_total}"
                if host.memory_used and host.memory_total
                else embeds.NA
            ),
            inline=True,
        )
        if host.memory_percent is not None:
            embed.add_field(name="RAM used", value=f"{host.memory_percent:.0f}%", inline=True)
        for disk in host.disks[:3]:
            embed.add_field(
                name=f"💾 {disk.get('device', 'disk')}",
                value=(
                    f"{disk.get('used', embeds.NA)} / {disk.get('total', embeds.NA)}"
                    f" ({disk.get('percent_used', '?')}%)"
                ),
                inline=False,
            )
        if host.boot_time is not None:
            embed.add_field(
                name="Booted",
                value=host.boot_time.strftime("%Y-%m-%d %H:%M"),
                inline=True,
            )
        await interaction.edit_original_response(embed=embed)

    @group.command(name="logs", description="Show the most recent server log lines")
    @app_commands.describe(
        lines="How many lines to show (1-50, default 20)",
        source="Console buffer or the log file on disk",
        server="Crafty server (defaults to the configured one)",
    )
    @app_commands.choices(source=LOG_SOURCES)
    @app_commands.autocomplete(server=server_autocomplete)
    async def logs(
        self,
        interaction: discord.Interaction,
        lines: app_commands.Range[int, 1, 50] = 20,
        source: app_commands.Choice[str] | None = None,
        server: str | None = None,
    ) -> None:
        if not await self.guard(interaction, Tier.SERVER):
            return
        await interaction.response.defer(ephemeral=True)
        from_file = bool(source and source.value == "file")
        try:
            server_id = await self.resolve(server)
            log_lines = await self.crafty.get_logs(server_id, lines=lines, from_file=from_file)
            name = await self.server_label(server_id)
        except BotError as exc:
            await self._fail(interaction, exc)
            return

        await interaction.edit_original_response(
            embed=embeds.logs_embed(
                log_lines,
                source="log file" if from_file else "console buffer",
                server_name=name,
            )
        )

    # ------------------------------------------------------------------ #
    # Control
    # ------------------------------------------------------------------ #
    @group.command(name="start", description="Start the Minecraft server")
    @app_commands.describe(server="Crafty server (defaults to the configured one)")
    @app_commands.autocomplete(server=server_autocomplete)
    async def start(
        self, interaction: discord.Interaction, server: str | None = None
    ) -> None:
        if not await self.guard(interaction, Tier.SERVER):
            return
        await self.run_workflow(
            interaction,
            "Starting Minecraft",
            lambda progress: self.orchestrator.start_infrastructure(
                server, on_progress=progress
            ),
        )

    @group.command(name="stop", description="Stop the Minecraft server gracefully")
    @app_commands.describe(
        server="Crafty server (defaults to the configured one)",
        shutdown_vm="Also deallocate the Azure VM once Minecraft has stopped",
    )
    @app_commands.autocomplete(server=server_autocomplete)
    async def stop(
        self,
        interaction: discord.Interaction,
        server: str | None = None,
        shutdown_vm: bool | None = None,
    ) -> None:
        if not await self.guard(interaction, Tier.SERVER):
            return

        # Deallocating the VM is an Azure-tier action, whether it was requested
        # explicitly or comes from AUTO_SHUTDOWN_VM.
        stop_vm = self.config.auto_shutdown_vm if shutdown_vm is None else shutdown_vm
        if stop_vm and not self.bot.permissions.allows(interaction.user, Tier.AZURE):
            if shutdown_vm:
                await self.guard(interaction, Tier.AZURE)
                return
            stop_vm = False

        await self.run_workflow(
            interaction,
            "Stopping Minecraft",
            lambda progress: self.orchestrator.stop_minecraft(
                server, shutdown_vm=stop_vm, on_progress=progress
            ),
        )

    @group.command(name="restart", description="Restart the Minecraft server")
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

    @group.command(name="kill", description="Force-kill a frozen server (no world save)")
    @app_commands.describe(server="Crafty server (defaults to the configured one)")
    @app_commands.autocomplete(server=server_autocomplete)
    async def kill(
        self, interaction: discord.Interaction, server: str | None = None
    ) -> None:
        if not await self.guard(interaction, Tier.ADMIN):
            return

        async def confirmed(button_interaction: discord.Interaction) -> None:
            await button_interaction.response.defer()
            try:
                server_id = await self.resolve(server)
                await self.crafty.kill_server(server_id)
            except BotError as exc:
                await self._fail(button_interaction, exc)
                return
            await button_interaction.edit_original_response(
                embed=embeds.success_embed(
                    "Server killed", "The process was terminated without saving."
                ),
                view=None,
            )

        view = ConfirmView(
            checker=self.bot.permissions,
            owner_id=interaction.user.id,
            tier=Tier.ADMIN,
            on_confirm=confirmed,
            confirm_label="Kill anyway",
        )
        await interaction.response.send_message(
            embed=embeds.confirm_embed(
                "Force-kill the server?",
                "The Minecraft process will be terminated immediately. **Unsaved world "
                "changes will be lost.** Prefer `/server stop` whenever possible.",
            ),
            view=view,
        )
        view.message = await interaction.original_response()

    @group.command(name="command", description="Send a command to the Minecraft console")
    @app_commands.describe(
        command="Console command without the leading slash, e.g. say Hello!",
        server="Crafty server (defaults to the configured one)",
    )
    @app_commands.autocomplete(server=server_autocomplete)
    async def console_command(
        self,
        interaction: discord.Interaction,
        command: app_commands.Range[str, 1, 400],
        server: str | None = None,
    ) -> None:
        if not await self.guard(interaction, Tier.SERVER):
            return

        payload = command.strip().lstrip("/")
        keyword = payload.split(" ", 1)[0].lower()

        if keyword in DANGEROUS_COMMANDS:
            await self._confirm_command(interaction, payload, server)
            return

        await interaction.response.defer(ephemeral=True)
        await self._send_command(interaction, payload, server)

    async def _confirm_command(
        self, interaction: discord.Interaction, payload: str, server: str | None
    ) -> None:
        async def confirmed(button_interaction: discord.Interaction) -> None:
            await button_interaction.response.defer()
            await self._send_command(button_interaction, payload, server, edit=True)

        view = ConfirmView(
            checker=self.bot.permissions,
            owner_id=interaction.user.id,
            tier=Tier.SERVER,
            on_confirm=confirmed,
            confirm_label="Send anyway",
        )
        await interaction.response.send_message(
            embed=embeds.confirm_embed(
                "Send this command?",
                f"`{payload}` can change or stop the server.\nSend it to the console?",
            ),
            view=view,
            ephemeral=True,
        )
        view.message = await interaction.original_response()

    async def _send_command(
        self,
        interaction: discord.Interaction,
        payload: str,
        server: str | None,
        *,
        edit: bool = False,
    ) -> None:
        try:
            server_id = await self.resolve(server)
            stats = await self.crafty.get_stats(server_id)
            if not stats.running:
                await interaction.edit_original_response(
                    embed=embeds.warning_embed(
                        "Server not running",
                        "Start the server before sending console commands.",
                    ),
                    view=None,
                )
                return
            await self.crafty.send_console_command(server_id, payload)
        except BotError as exc:
            await self._fail(interaction, exc)
            return

        logger.info(
            "User %s ran console command on server %s", interaction.user.id, server_id
        )
        embed = embeds.success_embed("Command sent", f"```\n{payload[:1800]}\n```")
        if edit:
            await interaction.edit_original_response(embed=embed, view=None)
        else:
            await interaction.edit_original_response(embed=embed)

    @group.command(
        name="update", description="Install the server jar update Crafty found"
    )
    @app_commands.describe(server="Crafty server (defaults to the configured one)")
    @app_commands.autocomplete(server=server_autocomplete)
    async def update(
        self, interaction: discord.Interaction, server: str | None = None
    ) -> None:
        if not await self.guard(interaction, Tier.ADMIN):
            return
        await interaction.response.defer()

        try:
            server_id = await self.resolve(server)
            data = await self.crafty.get_server(server_id)
            name = await self.server_label(server_id)
        except BotError as exc:
            await self._fail(interaction, exc)
            return

        status = data.get("status") or {}
        if isinstance(status, dict) and not status.get("update_available"):
            await interaction.edit_original_response(
                embed=embeds.success_embed(
                    "Already up to date", f"Crafty reports no pending update for **{name}**."
                )
            )
            return

        async def confirmed(button_interaction: discord.Interaction) -> None:
            await button_interaction.response.defer()
            try:
                await self.crafty.send_action(server_id, "update_executable")
            except BotError as exc:
                await self._fail(button_interaction, exc)
                return
            logger.info(
                "User %s started a jar update on server %s", interaction.user.id, server_id
            )
            await button_interaction.edit_original_response(
                embed=embeds.success_embed(
                    "Update started",
                    "Crafty is downloading and swapping the server executable. "
                    "It backs the old one up first and restarts the server when done — "
                    "watch `/server status`.",
                ),
                view=None,
            )

        view = ConfirmView(
            checker=self.bot.permissions,
            owner_id=interaction.user.id,
            tier=Tier.ADMIN,
            on_confirm=confirmed,
            confirm_label="Update now",
        )
        await interaction.edit_original_response(
            embed=embeds.confirm_embed(
                f"Update {name}?",
                "Crafty will stop the server, replace its executable and start it "
                "again. **Take a backup first** if you have not run one recently — "
                "plugins and mods can break on a version change.",
            ),
            view=view,
        )
        view.message = await interaction.original_response()

    # ------------------------------------------------------------------ #
    # Backups
    # ------------------------------------------------------------------ #
    @group.command(name="backups", description="List the backup configurations of a server")
    @app_commands.describe(server="Crafty server (defaults to the configured one)")
    @app_commands.autocomplete(server=server_autocomplete)
    async def backups(
        self, interaction: discord.Interaction, server: str | None = None
    ) -> None:
        if not await self.guard(interaction, Tier.SERVER):
            return
        await interaction.response.defer(ephemeral=True)
        try:
            server_id = await self.resolve(server)
            configs = await self.crafty.list_backups(server_id)
            name = await self.server_label(server_id)
        except BotError as exc:
            await self._fail(interaction, exc)
            return
        await interaction.edit_original_response(embed=embeds.backups_embed(configs, name))

    @group.command(name="backup", description="Run a Crafty backup for this server")
    @app_commands.describe(server="Crafty server (defaults to the configured one)")
    @app_commands.autocomplete(server=server_autocomplete)
    async def backup(
        self, interaction: discord.Interaction, server: str | None = None
    ) -> None:
        if not await self.guard(interaction, Tier.SERVER):
            return
        await interaction.response.defer()
        try:
            server_id = await self.resolve(server)
            configs = await self.crafty.list_backups(server_id)
        except BotError as exc:
            await self._fail(interaction, exc)
            return

        if len(configs) > 1:
            view = BackupSelectView(
                checker=self.bot.permissions,
                owner_id=interaction.user.id,
                options=[
                    (f"{cfg.name}{' (default)' if cfg.default else ''}", cfg.backup_id)
                    for cfg in configs
                ],
                on_pick=lambda i, backup_id: self._run_backup(i, server_id, backup_id),
            )
            message = await interaction.edit_original_response(
                embed=embeds.warning_embed(
                    "Several backup configurations",
                    "Pick the configuration Crafty should run.",
                ),
                view=view,
            )
            view.message = message
            return

        await self._run_backup(interaction, server_id, None, already_deferred=True)

    async def _run_backup(
        self,
        interaction: discord.Interaction,
        server_id: str,
        backup_id: str | None,
        *,
        already_deferred: bool = False,
    ) -> None:
        if not already_deferred and not interaction.response.is_done():
            await interaction.response.defer()
        try:
            chosen = await self.crafty.start_backup(server_id, backup_id)
        except BotError as exc:
            await self._fail(interaction, exc)
            return

        logger.info("User %s started backup on server %s", interaction.user.id, server_id)
        detail = (
            f"Crafty is running **{chosen.name}**.\n"
            "Backups run on the Crafty host — nothing is downloaded to the bot.\n"
            "Use `/server status` to see when the server is idle again."
        )
        if chosen.shutdown:
            detail += "\n⚠️ This configuration stops the server while it backs up."
        await interaction.edit_original_response(
            embed=embeds.success_embed("Backup started", detail), view=None
        )

    # ------------------------------------------------------------------ #
    async def _fail(self, interaction: discord.Interaction, error: BotError) -> None:
        embed = embeds.error_embed("Operation failed", error.user_message)
        hint = await self.bot.context_hint(error)
        if hint:
            embed.add_field(name="What to check", value=hint, inline=False)
        try:
            await interaction.edit_original_response(embed=embed, view=None)
        except discord.HTTPException:
            await interaction.followup.send(embed=embed, ephemeral=True)


async def setup(bot: CraftyBot) -> None:
    await bot.add_cog(ServerCog(bot))
