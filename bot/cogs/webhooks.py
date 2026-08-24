"""``/webhook …`` — let Crafty announce server events by itself.

This is the cheapest feature in the bot on a Raspberry Pi Zero W: Crafty already
knows the moment a server starts, stops, crashes, finishes a backup or updates
its jar, and it can POST that straight to a Discord channel webhook. Wiring it up
here means those announcements cost the Pi **nothing at all** — no polling loop,
no open WebSocket, not even a running bot process.

The bot only manages the configuration; Crafty does the sending.
"""

from __future__ import annotations

import logging
import re

import discord
from discord import app_commands

from bot.client import CraftyBot
from bot.cogs.base import ServiceCog, server_autocomplete
from bot.errors import BotError
from bot.permissions import Tier
from bot.services.crafty import WEBHOOK_EVENTS
from bot.ui import embeds
from bot.ui.views import ConfirmView

logger = logging.getLogger(__name__)

#: Discord's own webhook endpoints. Anything else is almost certainly a mistake,
#: and posting a Crafty webhook at an arbitrary host would leak server activity.
DISCORD_WEBHOOK_RE = re.compile(
    r"^https://(?:\w+\.)?discord(?:app)?\.com/api/webhooks/\d+/[\w-]+$"
)

#: The presets people actually want, so nobody has to memorise event names.
EVENT_PRESETS = {
    "lifecycle": ("start_server", "stop_server", "crash_detected"),
    "all": WEBHOOK_EVENTS,
    "crashes": ("crash_detected",),
    "backups": ("backup_server",),
}

PRESET_CHOICES = [
    app_commands.Choice(name="Starts, stops and crashes (recommended)", value="lifecycle"),
    app_commands.Choice(name="Crashes only", value="crashes"),
    app_commands.Choice(name="Backups only", value="backups"),
    app_commands.Choice(name="Everything Crafty can report", value="all"),
]

#: Crafty substitutes these placeholders before sending.
DEFAULT_BODY = "**{server_name}** — {event_type} at {time_formatted}"


class WebhookCog(ServiceCog):
    """Manage the webhooks Crafty fires. Requires the CONFIG API permission."""

    group = app_commands.Group(
        name="webhook", description="Crafty's own event notifications"
    )

    @group.command(name="list", description="Show the webhooks Crafty fires for a server")
    @app_commands.describe(server="Crafty server (defaults to the configured one)")
    @app_commands.autocomplete(server=server_autocomplete)
    async def list_webhooks(
        self, interaction: discord.Interaction, server: str | None = None
    ) -> None:
        if not await self.guard(interaction, Tier.SERVER):
            return
        await interaction.response.defer(ephemeral=True)
        try:
            server_id = await self.resolve(server)
            webhooks = await self.crafty.list_webhooks(server_id)
            name = await self.server_label(server_id)
        except BotError as exc:
            await self._fail(interaction, exc)
            return
        await interaction.edit_original_response(embed=embeds.webhooks_embed(webhooks, name))

    @group.command(name="create", description="Have Crafty post server events to a channel")
    @app_commands.describe(
        url="A Discord channel webhook URL (Channel → Edit → Integrations)",
        events="Which events Crafty should announce",
        name="A label for this webhook in Crafty",
        server="Crafty server (defaults to the configured one)",
    )
    @app_commands.choices(events=PRESET_CHOICES)
    @app_commands.autocomplete(server=server_autocomplete)
    async def create(
        self,
        interaction: discord.Interaction,
        url: str,
        events: app_commands.Choice[str] | None = None,
        name: app_commands.Range[str, 1, 60] = "Discord bot",
        server: str | None = None,
    ) -> None:
        if not await self.guard(interaction, Tier.ADMIN):
            return
        # Ephemeral throughout: the URL is a credential for that channel.
        await interaction.response.defer(ephemeral=True)

        if not DISCORD_WEBHOOK_RE.match(url.strip()):
            await interaction.edit_original_response(
                embed=embeds.error_embed(
                    "That is not a Discord webhook URL",
                    "Copy it from **Channel → Edit Channel → Integrations → Webhooks**. "
                    "It looks like `https://discord.com/api/webhooks/…`.",
                )
            )
            return

        triggers = EVENT_PRESETS[events.value if events else "lifecycle"]
        try:
            server_id = await self.resolve(server)
            webhook_id = await self.crafty.create_webhook(
                server_id,
                name=name,
                url=url.strip(),
                triggers=triggers,
                body=DEFAULT_BODY,
                bot_name="Crafty",
            )
            label = await self.server_label(server_id)
        except BotError as exc:
            await self._fail(interaction, exc)
            return

        logger.info("User %s created a webhook on server %s", interaction.user.id, server_id)
        await interaction.edit_original_response(
            embed=embeds.success_embed(
                "Webhook created",
                f"Crafty will now announce **{', '.join(triggers)}** for **{label}**"
                + (f" (id `{webhook_id}`)." if webhook_id else ".")
                + "\n\nCrafty sends these itself, so this keeps working even while "
                "the bot is offline. Try it with `/webhook test`.",
            )
        )

    @group.command(name="test", description="Send a sample event through a webhook")
    @app_commands.describe(
        webhook_id="ID from `/webhook list`",
        server="Crafty server (defaults to the configured one)",
    )
    @app_commands.autocomplete(server=server_autocomplete)
    async def test(
        self,
        interaction: discord.Interaction,
        webhook_id: str,
        server: str | None = None,
    ) -> None:
        if not await self.guard(interaction, Tier.SERVER):
            return
        await interaction.response.defer(ephemeral=True)
        try:
            server_id = await self.resolve(server)
            await self.crafty.test_webhook(server_id, webhook_id)
        except BotError as exc:
            await self._fail(interaction, exc)
            return
        await interaction.edit_original_response(
            embed=embeds.success_embed(
                "Test sent", "Crafty fired a sample event. Check the target channel."
            )
        )

    @group.command(name="toggle", description="Enable or disable a webhook")
    @app_commands.describe(
        webhook_id="ID from `/webhook list`",
        enabled="Whether Crafty should fire this webhook",
        server="Crafty server (defaults to the configured one)",
    )
    @app_commands.autocomplete(server=server_autocomplete)
    async def toggle(
        self,
        interaction: discord.Interaction,
        webhook_id: str,
        enabled: bool,
        server: str | None = None,
    ) -> None:
        if not await self.guard(interaction, Tier.ADMIN):
            return
        await interaction.response.defer(ephemeral=True)
        try:
            server_id = await self.resolve(server)
            await self.crafty.set_webhook_enabled(server_id, webhook_id, enabled)
        except BotError as exc:
            await self._fail(interaction, exc)
            return
        await interaction.edit_original_response(
            embed=embeds.success_embed(
                "Webhook updated",
                f"Webhook `{webhook_id}` is now **{'enabled' if enabled else 'disabled'}**.",
            )
        )

    @group.command(name="delete", description="Remove a webhook from Crafty")
    @app_commands.describe(
        webhook_id="ID from `/webhook list`",
        server="Crafty server (defaults to the configured one)",
    )
    @app_commands.autocomplete(server=server_autocomplete)
    async def delete(
        self,
        interaction: discord.Interaction,
        webhook_id: str,
        server: str | None = None,
    ) -> None:
        if not await self.guard(interaction, Tier.ADMIN):
            return

        async def confirmed(button_interaction: discord.Interaction) -> None:
            await button_interaction.response.defer()
            try:
                server_id = await self.resolve(server)
                await self.crafty.delete_webhook(server_id, webhook_id)
            except BotError as exc:
                await self._fail(button_interaction, exc)
                return
            await button_interaction.edit_original_response(
                embed=embeds.success_embed(
                    "Webhook deleted", f"Crafty will no longer fire `{webhook_id}`."
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
                "Delete this webhook?",
                f"Crafty will stop announcing events through `{webhook_id}`. "
                "The Discord webhook itself is not touched.",
            ),
            view=view,
            ephemeral=True,
        )
        view.message = await interaction.original_response()

    async def _fail(self, interaction: discord.Interaction, error: BotError) -> None:
        embed = embeds.error_embed("Webhook request failed", error.user_message)
        try:
            await interaction.edit_original_response(embed=embed, view=None)
        except discord.HTTPException:
            await interaction.followup.send(embed=embed, ephemeral=True)


async def setup(bot: CraftyBot) -> None:
    await bot.add_cog(WebhookCog(bot))
