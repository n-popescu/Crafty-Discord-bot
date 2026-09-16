"""Interactive Discord components: refresh/control buttons and confirmations.

Views always edit the message they belong to, so one command produces exactly
one message no matter how many times its buttons are pressed.
"""

from __future__ import annotations

import logging
from typing import Awaitable, Callable, Sequence

import discord

from bot.permissions import PermissionChecker, Tier
from bot.ui import embeds

logger = logging.getLogger(__name__)

TIMEOUT = 180.0


class GuardedView(discord.ui.View):
    """A view whose buttons re-check permissions and disable themselves on timeout."""

    def __init__(
        self,
        *,
        checker: PermissionChecker,
        owner_id: int,
        timeout: float = TIMEOUT,
    ) -> None:
        super().__init__(timeout=timeout)
        self._checker = checker
        self._owner_id = owner_id
        self.message: discord.Message | None = None

    async def _deny(self, interaction: discord.Interaction, reason: str) -> None:
        await interaction.response.send_message(
            embed=embeds.error_embed("Not allowed", reason), ephemeral=True
        )

    async def ensure(self, interaction: discord.Interaction, tier: Tier) -> bool:
        guild_id = interaction.guild_id
        reason = self._checker.denial_reason(interaction.user, tier, guild_id)
        if reason:
            await self._deny(interaction, reason)
            return False
        return True

    async def on_timeout(self) -> None:
        for child in self.children:
            if isinstance(child, (discord.ui.Button, discord.ui.Select)):
                child.disabled = True
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass

    async def on_error(
        self,
        interaction: discord.Interaction,
        error: Exception,
        item: discord.ui.Item,
    ) -> None:
        logger.exception("Component %s failed", type(item).__name__, exc_info=error)
        message = embeds.error_embed(
            "Something went wrong", "The action could not be completed."
        )
        if interaction.response.is_done():
            await interaction.followup.send(embed=message, ephemeral=True)
        else:
            await interaction.response.send_message(embed=message, ephemeral=True)


class StatusView(GuardedView):
    """Buttons under ``/status``: refresh plus the controls the user may use."""

    def __init__(
        self,
        *,
        checker: PermissionChecker,
        owner_id: int,
        refresh: Callable[[discord.Interaction], Awaitable[None]],
        start: Callable[[discord.Interaction], Awaitable[None]],
        stop: Callable[[discord.Interaction], Awaitable[None]],
        restart: Callable[[discord.Interaction], Awaitable[None]],
        running: bool,
        may_control: bool,
        timeout_armed: bool = False,
        toggle_timeout: Callable[[discord.Interaction], Awaitable[None]] | None = None,
    ) -> None:
        super().__init__(checker=checker, owner_id=owner_id)
        self._refresh = refresh
        self._start = start
        self._stop = stop
        self._restart = restart
        self._toggle_timeout = toggle_timeout

        if not may_control or toggle_timeout is None:
            # Don't advertise controls the caller is not allowed to use.
            self.remove_item(self.timeout_button)
        else:
            # The switch reads as on/off at a glance: green when armed.
            self.timeout_button.label = (
                "Auto-shutdown: ON" if timeout_armed else "Auto-shutdown: OFF"
            )
            self.timeout_button.style = (
                discord.ButtonStyle.success if timeout_armed else discord.ButtonStyle.secondary
            )

        if not may_control:
            for button in (self.start_button, self.stop_button, self.restart_button):
                self.remove_item(button)
            return

        self.start_button.disabled = running
        self.stop_button.disabled = not running
        self.restart_button.disabled = not running

    @discord.ui.button(label="Refresh", emoji="🔄", style=discord.ButtonStyle.secondary)
    async def refresh_button(
        self, interaction: discord.Interaction, _: discord.ui.Button
    ) -> None:
        if await self.ensure(interaction, Tier.EVERYONE):
            await self._refresh(interaction)

    @discord.ui.button(label="Start", emoji="▶️", style=discord.ButtonStyle.success)
    async def start_button(
        self, interaction: discord.Interaction, _: discord.ui.Button
    ) -> None:
        if await self.ensure(interaction, Tier.SERVER):
            await self._start(interaction)

    @discord.ui.button(label="Stop", emoji="⏹️", style=discord.ButtonStyle.danger)
    async def stop_button(
        self, interaction: discord.Interaction, _: discord.ui.Button
    ) -> None:
        if await self.ensure(interaction, Tier.SERVER):
            await self._stop(interaction)

    @discord.ui.button(label="Restart", emoji="🔃", style=discord.ButtonStyle.primary)
    async def restart_button(
        self, interaction: discord.Interaction, _: discord.ui.Button
    ) -> None:
        if await self.ensure(interaction, Tier.SERVER):
            await self._restart(interaction)

    @discord.ui.button(
        label="Auto-shutdown: OFF", emoji="⏱️", style=discord.ButtonStyle.secondary, row=1
    )
    async def timeout_button(
        self, interaction: discord.Interaction, _: discord.ui.Button
    ) -> None:
        if self._toggle_timeout is None:
            return
        if await self.ensure(interaction, Tier.SERVER):
            await self._toggle_timeout(interaction)


class TimeoutModal(discord.ui.Modal):
    """Asks for the idle delay, because a button cannot carry a parameter."""

    minutes: discord.ui.TextInput = discord.ui.TextInput(
        label="Idle minutes before shutting down",
        placeholder="90",
        default="90",
        min_length=1,
        max_length=4,
        required=True,
    )

    def __init__(
        self,
        *,
        on_submit: Callable[[discord.Interaction, int], Awaitable[None]],
        max_minutes: int,
        default: int = 90,
    ) -> None:
        super().__init__(title="Arm auto-shutdown")
        self._on_submit = on_submit
        self._max_minutes = max_minutes
        self.minutes.default = str(default)
        # The declared max_length is a fallback for MAX_MINUTES' current digit
        # count; deriving it here means a wider bound stays enterable even if
        # that constant ever grows past four digits, instead of silently
        # capping the text field below its own maximum.
        self.minutes.max_length = max(len(str(max_minutes)), self.minutes.max_length)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        raw = str(self.minutes.value).strip()
        try:
            value = int(raw)
        except ValueError:
            value = -1
        if not 1 <= value <= self._max_minutes:
            await interaction.response.send_message(
                embed=embeds.error_embed(
                    "Not a valid delay",
                    f"Enter a whole number of minutes between 1 and {self._max_minutes}.",
                ),
                ephemeral=True,
            )
            return
        await self._on_submit(interaction, value)

    async def on_error(
        self, interaction: discord.Interaction, error: Exception
    ) -> None:  # pragma: no cover - defensive
        logger.exception("Timeout modal failed", exc_info=error)
        if not interaction.response.is_done():
            await interaction.response.send_message(
                embed=embeds.error_embed(
                    "Something went wrong", "The timeout could not be armed."
                ),
                ephemeral=True,
            )


class ConfirmView(GuardedView):
    """A two-button confirmation gate for destructive operations."""

    def __init__(
        self,
        *,
        checker: PermissionChecker,
        owner_id: int,
        tier: Tier,
        on_confirm: Callable[[discord.Interaction], Awaitable[None]],
        confirm_label: str = "Confirm",
    ) -> None:
        super().__init__(checker=checker, owner_id=owner_id, timeout=60.0)
        self._tier = tier
        self._on_confirm = on_confirm
        self.confirm_button.label = confirm_label

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        """Only the person who triggered the command may answer the prompt."""
        if interaction.user.id != self._owner_id:
            await self._deny(
                interaction, "Only the person who ran the command can confirm it."
            )
            return False
        return True

    @discord.ui.button(label="Confirm", style=discord.ButtonStyle.danger)
    async def confirm_button(
        self, interaction: discord.Interaction, _: discord.ui.Button
    ) -> None:
        if not await self.ensure(interaction, self._tier):
            return
        for child in self.children:
            if isinstance(child, discord.ui.Button):
                child.disabled = True
        self.stop()
        await self._on_confirm(interaction)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel_button(
        self, interaction: discord.Interaction, _: discord.ui.Button
    ) -> None:
        self.stop()
        await interaction.response.edit_message(
            embed=embeds.warning_embed("Cancelled", "Nothing was changed."), view=None
        )


class BackupSelectView(GuardedView):
    """Select menu shown when a server has several backup configurations."""

    def __init__(
        self,
        *,
        checker: PermissionChecker,
        owner_id: int,
        options: Sequence[tuple[str, str]],
        on_pick: Callable[[discord.Interaction, str], Awaitable[None]],
    ) -> None:
        super().__init__(checker=checker, owner_id=owner_id, timeout=90.0)
        self._on_pick = on_pick
        self.picker.options = [
            discord.SelectOption(label=label[:100], value=value) for label, value in options[:25]
        ]

    @discord.ui.select(placeholder="Choose a backup configuration…")
    async def picker(
        self, interaction: discord.Interaction, select: discord.ui.Select
    ) -> None:
        if not await self.ensure(interaction, Tier.SERVER):
            return
        self.stop()
        await self._on_pick(interaction, select.values[0])
