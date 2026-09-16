"""``/timeout …`` — arm the inactivity auto-shutdown.

``/timeout minutes:90`` means "once nobody has been online for 90 minutes, stop
the server gracefully through Crafty and then free the Azure VM".
``/timeout minutes:0`` cancels it, and ``/timeout`` on its own shows the state.

Arming is a *scheduled* stop, so it is gated exactly like the manual one:
stopping Minecraft needs the server tier, and deallocating the VM additionally
needs the Azure tier.
"""

from __future__ import annotations

import logging

import discord
from discord import app_commands

from bot.client import CraftyBot
from bot.cogs.base import ServiceCog, server_autocomplete
from bot.errors import BotError, CraftyUnavailable
from bot.permissions import Tier
from bot.services.timeout import MAX_MINUTES
from bot.ui import embeds

logger = logging.getLogger(__name__)


class TimeoutCog(ServiceCog):
    """One command that arms, disarms and reports the inactivity timeout."""

    @app_commands.command(
        name="timeout",
        description="Stop the server (and the VM) after N minutes with nobody online",
    )
    @app_commands.describe(
        minutes=f"Idle minutes before shutting down (1-{MAX_MINUTES}); 0 cancels it",
        shutdown_vm="Also deallocate the Azure VM afterwards (default: yes)",
        server="Crafty server (defaults to the configured one)",
    )
    @app_commands.autocomplete(server=server_autocomplete)
    async def timeout(
        self,
        interaction: discord.Interaction,
        minutes: app_commands.Range[int, 0, MAX_MINUTES] | None = None,
        shutdown_vm: bool | None = None,
        server: str | None = None,
    ) -> None:
        # Reading the state is open to everyone; changing it is not.
        tier = Tier.EVERYONE if minutes is None else Tier.SERVER
        if not await self.guard(interaction, tier):
            return
        await interaction.response.defer()

        try:
            server_id, live = await self._resolve_tolerant(server)
        except BotError as exc:
            await self._fail(interaction, exc)
            return

        if server_id is None:
            await self._unidentified(interaction, minutes)
            return

        name = await self.server_label(server_id)

        if minutes is None:
            await self._show(interaction, server_id, name, live=live)
            return

        if minutes == 0:
            disarmed = self.bot.timeouts.disarm(server_id)
            logger.info(
                "User %s disarmed the timeout for server %s", interaction.user.id, server_id
            )
            embed = (
                embeds.success_embed(
                    "Auto-shutdown cancelled",
                    f"**{name}** will now stay up until somebody stops it.",
                )
                if disarmed
                else embeds.warning_embed(
                    "Nothing to cancel", f"No timeout was armed for **{name}**."
                )
            )
            await interaction.edit_original_response(embed=embed, view=None)
            return

        stop_vm = self.azure.enabled if shutdown_vm is None else shutdown_vm
        stop_vm = stop_vm and self.azure.enabled
        downgraded = False
        if stop_vm and not self.bot.permissions.allows(interaction.user, Tier.AZURE):
            if shutdown_vm:
                # Explicitly asked for a VM shutdown without the rights for it.
                await self.guard(interaction, Tier.AZURE)
                return
            # Implicit default: arm the Minecraft-only stop rather than refusing
            # outright, but say so instead of quietly doing less than expected.
            stop_vm = False
            downgraded = True

        state = self.bot.timeouts.arm(
            server_id, minutes, shutdown_vm=stop_vm, armed_by=interaction.user.id
        )
        logger.info(
            "User %s armed a %d min timeout for server %s (VM: %s)",
            interaction.user.id,
            minutes,
            server_id,
            stop_vm,
        )

        detail = (
            f"**{name}** will be stopped gracefully through Crafty once it has been "
            f"empty for **{minutes} minutes**"
        )
        detail += (
            f", and the Azure VM will be deallocated "
            f"{self.config.auto_shutdown_delay}s later."
            if stop_vm
            else ". The Azure VM will be left running."
        )
        detail += (
            "\n\nThe countdown restarts whenever somebody is online, and only runs "
            "while the player count is zero."
        )
        if downgraded:
            detail += (
                "\n⚠️ The VM will be left running: deallocating it needs the Azure "
                "operator role."
            )

        embed = embeds.success_embed(f"Auto-shutdown armed — {minutes} min", detail)
        embed.add_field(name="Current state", value=embeds.timeout_line(state), inline=False)
        await interaction.edit_original_response(embed=embed, view=None)

    async def _resolve_tolerant(self, server: str | None) -> tuple[str | None, bool]:
        """Resolve a server id without requiring a reachable Crafty.

        Arming, cancelling and reading a timeout are bot-local operations, so
        they have to keep working while the Azure VM -- and therefore Crafty --
        is powered off. That is precisely when somebody wants to check whether
        the auto-shutdown is still set.

        Returns the id (or ``None`` when it cannot be determined) and whether it
        came from a live Crafty lookup.
        """
        try:
            return await self.resolve(server), True
        except CraftyUnavailable:
            # Covers CraftyHostOffline too. Every other Crafty error (unknown
            # server, bad token) still propagates: those are real answers.
            pass

        explicit = (server or self.config.crafty.default_server_id or "").strip()
        if explicit:
            return explicit, False
        armed = self.bot.timeouts.active()
        if len(armed) == 1:
            return armed[0].server_id, False
        return None, False

    async def _unidentified(
        self, interaction: discord.Interaction, minutes: int | None
    ) -> None:
        """Crafty is unreachable and nothing says which server was meant."""
        if minutes is not None:
            await interaction.edit_original_response(
                embed=embeds.error_embed(
                    "Which server?",
                    "Crafty is unreachable, so the server could not be looked up. "
                    "Pass `server:` with a server ID, or set `CRAFTY_SERVER_ID` so "
                    "the bot knows which one you mean while the VM is off.",
                ),
                view=None,
            )
            return

        armed = self.bot.timeouts.active()
        if not armed:
            embed = embeds.timeout_embed(None, "this server")
        else:
            embed = embeds.armed_timeouts_embed(armed)
        embed.add_field(
            name="Note",
            value=(
                "Crafty is unreachable, so this is the bot's own record rather "
                "than a live reading."
            ),
            inline=False,
        )
        await interaction.edit_original_response(embed=embed, view=None)

    async def _show(
        self,
        interaction: discord.Interaction,
        server_id: str,
        name: str,
        *,
        live: bool = True,
    ) -> None:
        embed = embeds.timeout_embed(
            self.bot.timeouts.get(server_id),
            name,
            last_result=self.bot.timeouts.last_result,
        )
        if not live:
            embed.add_field(
                name="Note",
                value=(
                    "The Azure VM is off, so Crafty was not contacted. The "
                    "countdown resumes when the server is running again."
                ),
                inline=False,
            )
        await interaction.edit_original_response(embed=embed, view=None)

    async def _fail(self, interaction: discord.Interaction, error: BotError) -> None:
        embed = embeds.error_embed("Timeout request failed", error.user_message)
        try:
            await interaction.edit_original_response(embed=embed, view=None)
        except discord.HTTPException:
            await interaction.followup.send(embed=embed, ephemeral=True)


async def setup(bot: CraftyBot) -> None:
    await bot.add_cog(TimeoutCog(bot))
