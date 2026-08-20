"""Reusable Discord embed builders.

Everything the bot sends goes through one of these builders so that colours,
emoji, titles, footers and timestamps stay consistent. No builder ever receives
or renders a credential.
"""

from __future__ import annotations

from typing import Iterable, Sequence

import discord

from bot.services.azure import POWER_RUNNING, VmStatus
from bot.services.crafty import BackupConfig, HostStats, ServerStats
from bot.services.orchestrator import InfraSnapshot, Step, StepState
from bot.utils import human_bytes, uptime_since

NA = "N/A"
FOOTER = "Crafty Control Panel"

COLOR_ONLINE = discord.Colour.from_rgb(59, 165, 93)
COLOR_OFFLINE = discord.Colour.from_rgb(120, 126, 134)
COLOR_WARNING = discord.Colour.from_rgb(240, 178, 50)
COLOR_ERROR = discord.Colour.from_rgb(217, 74, 74)
COLOR_INFO = discord.Colour.from_rgb(88, 101, 242)

STATE_EMOJI = {
    "running": "🟢",
    "stopped": "⚫",
    "starting": "🟡",
    "stopping": "🟡",
    "deallocating": "🟡",
    "deallocated": "⚫",
    "crashed": "🔴",
    "updating": "🔵",
    "unknown": "⚪",
}

STEP_EMOJI = {
    StepState.PENDING: "⚪",
    StepState.ACTIVE: "⏳",
    StepState.DONE: "✅",
    StepState.SKIPPED: "⏭️",
    StepState.FAILED: "❌",
}


def _state_line(state: str) -> str:
    return f"{STATE_EMOJI.get(state, '⚪')} **{state.upper()}**"


def _base(title: str, colour: discord.Colour, description: str | None = None) -> discord.Embed:
    embed = discord.Embed(
        title=title,
        description=description,
        colour=colour,
        timestamp=discord.utils.utcnow(),
    )
    embed.set_footer(text=FOOTER)
    return embed


# --------------------------------------------------------------------------- #
# Generic feedback
# --------------------------------------------------------------------------- #
def panel_embed(title: str, description: str = "", colour: discord.Colour = COLOR_INFO) -> discord.Embed:
    """A neutral embed for ad-hoc panels (server info, host resources, ...)."""
    return _base(title, colour, description or None)


def success_embed(title: str, detail: str = "") -> discord.Embed:
    return _base(f"✅ {title}", COLOR_ONLINE, detail or None)


def error_embed(title: str, detail: str = "", *, hint: str = "") -> discord.Embed:
    embed = _base(f"❌ {title}", COLOR_ERROR, detail or None)
    if hint:
        embed.add_field(name="What to check", value=hint, inline=False)
    return embed


def warning_embed(title: str, detail: str = "") -> discord.Embed:
    return _base(f"⚠️ {title}", COLOR_WARNING, detail or None)


def confirm_embed(title: str, detail: str) -> discord.Embed:
    return _base(f"⚠️ {title}", COLOR_WARNING, detail)


# --------------------------------------------------------------------------- #
# Status
# --------------------------------------------------------------------------- #
def _players_value(stats: ServerStats) -> str:
    if not stats.running:
        return NA
    online = stats.online if stats.online is not None else "?"
    maximum = stats.max_players if stats.max_players else "?"
    return f"**{online}** / {maximum}"


def _resource_lines(stats: ServerStats, host: HostStats | None) -> str:
    cpu = f"{stats.cpu_percent:.0f}%" if stats.cpu_percent is not None else NA
    if stats.memory_bytes:
        memory = human_bytes(stats.memory_bytes)
        if host and host.memory_total:
            memory = f"{memory} / {host.memory_total}"
        elif stats.memory_percent is not None:
            memory = f"{memory} ({stats.memory_percent:.0f}%)"
    else:
        memory = NA

    lines = [f"⚡ CPU **{cpu}**", f"🧠 RAM **{memory}**"]
    disk = host.primary_disk if host else None
    if disk:
        used = disk.get("used") or disk.get("usage")
        total = disk.get("total")
        percent = disk.get("percent_used") or disk.get("percent")
        if used and total:
            lines.append(f"💾 Disk **{used} / {total}**")
        elif percent is not None:
            lines.append(f"💾 Disk **{percent}%**")
    if stats.world_size:
        lines.append(f"🌍 World **{stats.world_size}**")
    return "\n".join(lines)


def status_embed(snapshot: InfraSnapshot, host: HostStats | None = None) -> discord.Embed:
    """The centrepiece: one embed describing the whole infrastructure."""
    stats = snapshot.stats
    title = "🎮 Minecraft Infrastructure"

    if stats is not None:
        colour = COLOR_ONLINE if stats.running else COLOR_OFFLINE
        if stats.crashed:
            colour = COLOR_ERROR
    elif snapshot.vm is not None and snapshot.vm.is_running:
        colour = COLOR_WARNING
    else:
        colour = COLOR_OFFLINE

    embed = _base(title, colour)

    # -- Minecraft ------------------------------------------------------- #
    if stats is not None:
        name = stats.name or "Minecraft server"
        version_bits = " • ".join(
            bit for bit in (stats.version, f"port {stats.port}" if stats.port else "") if bit
        )
        embed.add_field(
            name=f"🎮 {name}",
            value="\n".join(
                bit
                for bit in (
                    _state_line(stats.state),
                    version_bits or None,
                    f"👥 Players {_players_value(stats)}",
                    f"⏱️ Uptime **{uptime_since(stats.started_at)}**"
                    if stats.running
                    else None,
                )
                if bit
            ),
            inline=True,
        )
    else:
        reason = snapshot.crafty_error.user_message if snapshot.crafty_error else "Unknown error"
        embed.add_field(name="🎮 Minecraft", value=f"⚠️ {reason}", inline=True)

    # -- Azure ----------------------------------------------------------- #
    if snapshot.vm is not None:
        vm = snapshot.vm
        details = [_state_line(vm.power_state), f"`{vm.name}`"]
        if vm.location:
            details.append(f"📍 {vm.location}")
        if vm.vm_size:
            details.append(f"🖥️ {vm.vm_size}")
        embed.add_field(name="☁️ Azure VM", value="\n".join(details), inline=True)
    elif snapshot.vm_error is not None:
        embed.add_field(
            name="☁️ Azure VM", value=f"⚠️ {snapshot.vm_error.user_message}", inline=True
        )

    # -- Resources ------------------------------------------------------- #
    if stats is not None and stats.running:
        embed.add_field(name="📊 Resources", value=_resource_lines(stats, host), inline=False)
        if stats.players:
            names = ", ".join(f"`{player.name}`" for player in stats.players[:10])
            embed.add_field(name="👥 Online now", value=names, inline=False)

    if snapshot.server_id:
        embed.set_footer(text=f"{FOOTER} • server {snapshot.server_id[:8]}")
    return embed


def health_embed(
    *,
    discord_ok: bool,
    crafty_reachable: bool,
    crafty_authenticated: bool,
    azure_state: str,
    minecraft_state: str,
    latency_ms: float | None = None,
) -> discord.Embed:
    """Diagnostic view answering "which layer is broken?"."""

    def mark(ok: bool | None) -> str:
        if ok is None:
            return "⚪ Not configured"
        return "🟢 OK" if ok else "🔴 Failing"

    embed = _base("🩺 Health", COLOR_INFO if crafty_reachable else COLOR_WARNING)
    bot_value = mark(discord_ok)
    if latency_ms is not None:
        bot_value += f" ({latency_ms:.0f} ms)"
    embed.add_field(name="🤖 Bot", value=bot_value, inline=False)
    embed.add_field(
        name="🔧 Crafty",
        value=(
            f"Reachable: {mark(crafty_reachable)}\nToken: {mark(crafty_authenticated)}"
        ),
        inline=False,
    )
    embed.add_field(name="☁️ Azure", value=azure_state, inline=False)
    embed.add_field(name="🎮 Minecraft", value=minecraft_state, inline=False)
    return embed


# --------------------------------------------------------------------------- #
# Server-specific
# --------------------------------------------------------------------------- #
def server_embed(stats: ServerStats, host: HostStats | None = None) -> discord.Embed:
    colour = COLOR_ERROR if stats.crashed else (COLOR_ONLINE if stats.running else COLOR_OFFLINE)
    embed = _base(f"🎮 {stats.name or 'Minecraft server'}", colour, _state_line(stats.state))
    embed.add_field(name="Version", value=stats.version or NA, inline=True)
    embed.add_field(name="Players", value=_players_value(stats), inline=True)
    embed.add_field(name="Port", value=str(stats.port) if stats.port else NA, inline=True)
    if stats.running:
        embed.add_field(name="Uptime", value=uptime_since(stats.started_at), inline=True)
        embed.add_field(
            name="CPU",
            value=f"{stats.cpu_percent:.0f}%" if stats.cpu_percent is not None else NA,
            inline=True,
        )
        embed.add_field(name="RAM", value=human_bytes(stats.memory_bytes), inline=True)
    if stats.world_name:
        embed.add_field(name="World", value=stats.world_name, inline=True)
    if stats.world_size:
        embed.add_field(name="World size", value=stats.world_size, inline=True)
    if stats.description:
        embed.add_field(name="MOTD", value=stats.description[:1024], inline=False)
    if host is not None:
        embed.add_field(name="Host resources", value=_resource_lines(stats, host), inline=False)
    return embed


def players_embed(stats: ServerStats) -> discord.Embed:
    if not stats.running:
        return warning_embed(
            "Server offline", "The Minecraft server is not running, so nobody is online."
        )

    online = stats.online or 0
    embed = _base(f"🎮 Online Players — {online}", COLOR_ONLINE if online else COLOR_OFFLINE)
    if stats.players:
        embed.description = "\n".join(f"👤 **{player.name}**" for player in stats.players)
        uuid_lines = [
            f"`{player.name}` · `{player.uuid}`" for player in stats.players if player.uuid
        ]
        if uuid_lines:
            embed.add_field(name="UUIDs", value="\n".join(uuid_lines)[:1024], inline=False)
    elif online:
        embed.description = (
            "Crafty did not include a player list. Minecraft only samples a few "
            "names in its server ping."
        )
    else:
        embed.description = "Nobody is online right now."

    embed.add_field(name="Server", value=stats.name or NA, inline=True)
    embed.add_field(name="Players", value=_players_value(stats), inline=True)
    return embed


def logs_embed(lines: Sequence[str], *, source: str, server_name: str) -> discord.Embed:
    body = "\n".join(lines) if lines else "No log output available."
    # Discord hard-caps descriptions at 4096 characters; stay well below it.
    if len(body) > 1800:
        body = "…\n" + body[-1800:]
    embed = _base(f"📜 Logs — {server_name}", COLOR_INFO, f"```\n{body}\n```")
    embed.set_footer(text=f"{FOOTER} • {source} • {len(lines)} lines")
    return embed


def backups_embed(backups: Iterable[BackupConfig], server_name: str) -> discord.Embed:
    entries = list(backups)
    embed = _base(f"💾 Backup configurations — {server_name}", COLOR_INFO)
    if not entries:
        embed.description = "No backup configuration exists for this server in Crafty."
        return embed
    for backup in entries[:10]:
        flags = [
            "default" if backup.default else "",
            "compressed" if backup.compress else "",
            "stops server" if backup.shutdown else "",
            "disabled" if not backup.enabled else "",
        ]
        detail = ", ".join(flag for flag in flags if flag) or "—"
        embed.add_field(
            name=f"{backup.name} ({backup.backup_type or 'zip_vault'})",
            value=f"`{backup.backup_id}`\n{detail}",
            inline=False,
        )
    return embed


# --------------------------------------------------------------------------- #
# Azure
# --------------------------------------------------------------------------- #
def azure_embed(status: VmStatus, public_ip: str | None = None, private_ip: str | None = None) -> discord.Embed:
    colour = COLOR_ONLINE if status.power_state == POWER_RUNNING else COLOR_OFFLINE
    if status.is_transitioning:
        colour = COLOR_WARNING
    embed = _base("☁️ Azure VM", colour, _state_line(status.power_state))
    embed.add_field(name="Name", value=f"`{status.name}`", inline=True)
    embed.add_field(name="Region", value=status.location or NA, inline=True)
    embed.add_field(name="Size", value=status.vm_size or NA, inline=True)
    if public_ip:
        embed.add_field(name="Public IP", value=f"`{public_ip}`", inline=True)
    if private_ip:
        embed.add_field(name="Private IP", value=f"`{private_ip}`", inline=True)
    if status.provisioning_state:
        embed.add_field(name="Provisioning", value=status.provisioning_state, inline=True)
    return embed


# --------------------------------------------------------------------------- #
# Workflows
# --------------------------------------------------------------------------- #
def workflow_embed(
    title: str, steps: Sequence[Step], *, finished: bool = False, failed: bool = False
) -> discord.Embed:
    if failed:
        colour = COLOR_ERROR
    elif finished:
        colour = COLOR_ONLINE
    else:
        colour = COLOR_INFO

    lines = []
    for step in steps:
        emoji = STEP_EMOJI[step.state]
        detail = f" — {step.detail}" if step.detail else ""
        lines.append(f"{emoji} **{step.label}**{detail}")

    prefix = "✅" if finished and not failed else ("❌" if failed else "⏳")
    return _base(f"{prefix} {title}", colour, "\n".join(lines))

