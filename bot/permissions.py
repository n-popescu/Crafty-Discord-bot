"""Granular, configurable Discord permission checks.

Three tiers are supported, each independently configurable:

* ``EVERYONE`` -- read-only commands (``/status``, ``/server players``, ...).
* ``SERVER``   -- Minecraft control (start/stop/restart/console/logs/backups).
* ``AZURE``    -- VM lifecycle (start/stop/restart of the Azure VM).

Anyone in ``ADMIN_USER_IDS``/``ADMIN_ROLE_IDS`` -- and any member with Discord's
Administrator permission or the guild owner -- passes every tier. If a tier has
no roles configured it stays admin-only, so a fresh installation is locked down
by default rather than open to everyone.
"""

from __future__ import annotations

from enum import Enum

import discord

from bot.config import PermissionConfig


class Tier(Enum):
    EVERYONE = "everyone"
    SERVER = "server"
    AZURE = "azure"
    ADMIN = "admin"


class PermissionChecker:
    """Answers "may this user run this?" from the configured IDs."""

    def __init__(self, config: PermissionConfig, guild_id: int = 0) -> None:
        self._config = config
        self._guild_id = guild_id

    # ------------------------------------------------------------------ #
    def is_admin(self, user: discord.abc.User | discord.Member) -> bool:
        if user.id in self._config.admin_user_ids:
            return True
        if isinstance(user, discord.Member):
            if user.guild.owner_id == user.id:
                return True
            if user.guild_permissions.administrator:
                return True
            if self._config.admin_role_ids & {role.id for role in user.roles}:
                return True
        return False

    def allows(self, user: discord.abc.User | discord.Member, tier: Tier) -> bool:
        if tier is Tier.EVERYONE:
            return True
        if self.is_admin(user):
            return True
        if tier is Tier.ADMIN:
            return False

        allowed_roles = (
            self._config.server_control_role_ids
            if tier is Tier.SERVER
            else self._config.azure_control_role_ids
        )
        if not allowed_roles or not isinstance(user, discord.Member):
            return False
        return bool(allowed_roles & {role.id for role in user.roles})

    def guild_allowed(self, guild_id: int | None) -> bool:
        """Enforce ``DISCORD_GUILD_ID`` when it is configured."""
        if not self._guild_id:
            return True
        return guild_id == self._guild_id

    def denial_reason(
        self, user: discord.abc.User | discord.Member, tier: Tier, guild_id: int | None
    ) -> str | None:
        """Return ``None`` when allowed, else a short ephemeral explanation."""
        if not self.guild_allowed(guild_id):
            return "This bot only accepts commands from its configured Discord server."
        if self.allows(user, tier):
            return None
        if tier is Tier.SERVER:
            return "You need a Minecraft server manager role to use this command."
        if tier is Tier.AZURE:
            return "You need an Azure operator role to use this command."
        return "This command is restricted to administrators."
