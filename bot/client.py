"""The Discord client: wiring, startup validation and shared helpers for cogs."""

from __future__ import annotations

import asyncio
import logging

import discord
from discord.ext import commands

from bot.cache import TTLCache
from bot.config import Config
from bot.errors import BotError
from bot.permissions import PermissionChecker
from bot.services.azure import AzureService
from bot.services.crafty import CraftyService
from bot.services.orchestrator import InfraOrchestrator
from bot.services.timeout import IdleTimeoutService
from bot.ui import embeds

logger = logging.getLogger(__name__)

COGS = (
    "bot.cogs.status",
    "bot.cogs.server",
    "bot.cogs.azure",
    "bot.cogs.minecraft",
    "bot.cogs.schedule",
    "bot.cogs.webhooks",
    "bot.cogs.timeout",
)


class CraftyBot(commands.Bot):
    """Slash-command only bot: no message content intent, no prefix commands."""

    def __init__(self, config: Config) -> None:
        # The default intents are enough for slash commands, which keeps both
        # the privileged-intent requirements and the memory footprint minimal.
        intents = discord.Intents.none()
        intents.guilds = True
        super().__init__(command_prefix=commands.when_mentioned, intents=intents, help_command=None)

        self.config = config
        self.cache = TTLCache()
        self.azure = AzureService(config.azure, cache=self.cache)
        self.crafty = CraftyService(
            config.crafty, cache=self.cache, host_available=self.crafty_host_available
        )
        self.orchestrator = InfraOrchestrator(config, self.crafty, self.azure)
        self.timeouts = IdleTimeoutService(config, self.crafty, self.orchestrator)
        # Wired after both exist (IdleTimeoutService itself depends on the
        # orchestrator, so the orchestrator cannot take this dependency at
        # construction time without a cycle). This is what lets the watcher
        # notice a server starting immediately, rather than only on its own
        # next poll -- which can be minutes away while every armed server is
        # stopped.
        self.orchestrator.on_server_running = self.timeouts.wake
        self.permissions = PermissionChecker(config.permissions, config.guild_id)

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #
    async def crafty_host_available(self) -> bool:
        """``False`` while the Azure VM that hosts Crafty is powered off.

        Crafty only exists as long as its VM runs, so every Crafty request is
        skipped while the VM is stopped or deallocated instead of waiting for a
        TCP timeout. Transitional states (``starting``, ``unknown``) still get a
        request: that is what the start workflow polls for. If Azure itself
        cannot be queried the gate opens, so an Azure outage never hides Crafty.
        """
        if not self.azure.enabled:
            return True
        try:
            status = await self.azure.get_vm_status()
        except BotError:
            return True
        return not status.is_stopped

    async def setup_hook(self) -> None:
        for extension in COGS:
            await self.load_extension(extension)

        if self.config.guild_id:
            guild = discord.Object(id=self.config.guild_id)
            self.tree.copy_global_to(guild=guild)
            synced = await self.tree.sync(guild=guild)
            logger.info(
                "Registered %d slash commands in guild %s", len(synced), self.config.guild_id
            )
        else:
            synced = await self.tree.sync()
            logger.info("Registered %d global slash commands", len(synced))

        await self._log_startup_report()

        self.timeouts.start()
        await self._prearm_idle_timeout()

    async def _prearm_idle_timeout(self) -> None:
        """Honour ``IDLE_SHUTDOWN_ENABLED`` by arming ``/timeout`` at startup.

        The environment variables that used to drive a separate watcher loop are
        now just a default for the same mechanism ``/timeout`` uses, so there is
        only ever one countdown per server and one place that stops anything.
        """
        if not self.config.idle_shutdown_enabled:
            return
        if self.timeouts.any_armed:
            # A timeout restored from disk is a deliberate, more recent choice
            # than the configured default.
            return

        # CraftyService.resolve_server_id() always validates against a live
        # list_servers() call, even when a candidate id is already given --
        # so it fails whenever Crafty is unreachable, which is routinely true
        # at startup: the whole point of IDLE_SHUTDOWN_ENABLED is a VM that
        # rests powered off between sessions. When CRAFTY_SERVER_ID is set,
        # there is nothing to resolve, so it is armed directly; the watcher
        # itself will report a problem the first time it can reach Crafty if
        # the id turns out to be wrong. Auto-detecting the single server (no
        # configured id) still needs a live server list.
        configured = self.config.crafty.default_server_id
        if configured:
            server_id = configured
        else:
            try:
                server_id = await self.crafty.resolve_server_id(None)
            except BotError as exc:
                logger.warning(
                    "IDLE_SHUTDOWN_ENABLED is set but no server could be resolved "
                    "(%s); set CRAFTY_SERVER_ID so it can be armed even while the "
                    "VM is off, or arm it by hand with /timeout once it is up",
                    exc.user_message,
                )
                return

        self.timeouts.arm(
            server_id,
            self.config.idle_shutdown_minutes,
            shutdown_vm=self.config.auto_shutdown_vm,
        )

    async def _log_startup_report(self) -> None:
        """Probe both APIs concurrently; unavailability must not block startup."""
        crafty_task = asyncio.create_task(self.crafty.check_connection())
        azure_task = asyncio.create_task(
            self.azure.check_authentication() if self.azure.enabled else _false()
        )
        crafty_ok, azure_ok = await asyncio.gather(crafty_task, azure_task)

        crafty_authenticated = await self.crafty.check_authentication() if crafty_ok else False
        host_up = await self.crafty_host_available()

        logger.info("Startup status:")
        logger.info("  Discord: 🟢 connected as %s", self.user)
        if not host_up:
            logger.info(
                "  Crafty:  ⚪ not checked — the Azure VM %s is powered off",
                self.azure.vm_name,
            )
        elif not crafty_ok:
            logger.warning("  Crafty:  ⚠️ unreachable (%s) — will retry per command", self.crafty.base_url)
        elif not crafty_authenticated:
            logger.warning("  Crafty:  ⚠️ reachable but the API token was rejected")
        else:
            logger.info("  Crafty:  🟢 connected (%s)", self.crafty.base_url)
        if not self.azure.enabled:
            logger.info("  Azure:   ⚪ not configured — VM commands are disabled")
        elif azure_ok:
            logger.info("  Azure:   🟢 authenticated (VM %s)", self.azure.vm_name)
        else:
            logger.warning("  Azure:   ⚠️ authentication or VM lookup failed")

    async def on_ready(self) -> None:
        logger.info("Logged in as %s (%s)", self.user, getattr(self.user, "id", "?"))
        await self.change_presence(
            activity=discord.Activity(
                type=discord.ActivityType.watching, name="/status"
            ),
            status=discord.Status.online,
        )

    async def close(self) -> None:
        await self.timeouts.close()
        await self.crafty.close()
        await self.azure.close()
        await super().close()

    # ------------------------------------------------------------------ #
    # Shared error surface
    # ------------------------------------------------------------------ #
    async def report_error(self, interaction: discord.Interaction, error: Exception) -> None:
        """Turn any exception into one clean, ephemeral-safe embed."""
        if isinstance(error, BotError):
            hint = await self.context_hint(error)
            embed = embeds.error_embed(
                "Operation failed", error.user_message, hint=hint
            )
        else:
            logger.exception("Unhandled command error", exc_info=error)
            embed = embeds.error_embed(
                "Unexpected error", "The bot hit an internal error. Check the logs."
            )

        try:
            if interaction.response.is_done():
                await interaction.followup.send(embed=embed, ephemeral=True)
            else:
                await interaction.response.send_message(embed=embed, ephemeral=True)
        except discord.HTTPException:
            # Interaction expired (Discord allows 15 minutes); nothing to do.
            logger.debug("Could not deliver the error embed: interaction expired")

    async def context_hint(self, error: BotError) -> str:
        """Add the Azure power state to Crafty errors: it usually explains them."""
        from bot.errors import CraftyUnavailable

        if not isinstance(error, CraftyUnavailable) or not self.azure.enabled:
            return ""
        state = await self.orchestrator.vm_power_state()
        emoji = "🟢" if state == "running" else "⚫"
        hint = f"The Azure VM is currently: {emoji} `{state}`"
        if state != "running":
            hint += "\nUse `/azure start` or `/minecraft start` to bring it up."
        return hint


async def _false() -> bool:
    return False
