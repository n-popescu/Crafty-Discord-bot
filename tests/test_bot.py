"""Bot wiring: the command surface, intents and error reporting."""

from __future__ import annotations

import pytest

from bot.client import COGS, CraftyBot
from bot.errors import AzureAuthError, CraftyHostOffline, CraftyUnavailable
from bot.services.azure import POWER_DEALLOCATED, POWER_RUNNING, VmStatus

EXPECTED_COMMANDS = {
    "status": set(),
    "servers": set(),
    "health": set(),
    "server": {
        "status",
        "players",
        "info",
        "resources",
        "logs",
        "history",
        "properties",
        "roster",
        "start",
        "stop",
        "restart",
        "kill",
        "update",
        "command",
        "backups",
        "backup",
    },
    "azure": {"status", "ip", "start", "stop", "restart"},
    "minecraft": {"start", "stop", "restart"},
    "schedule": {"info", "run", "create", "toggle", "delete"},
    "webhook": {"list", "create", "test", "toggle", "delete"},
}


@pytest.fixture
async def bot(config):
    instance = CraftyBot(config)
    for extension in COGS:
        await instance.load_extension(extension)
    yield instance
    await instance.crafty.close()
    await instance.azure.close()


async def test_every_documented_command_is_registered(bot):
    registered = {
        command.name: {sub.name for sub in getattr(command, "commands", [])}
        for command in bot.tree.get_commands()
    }
    assert registered == EXPECTED_COMMANDS


async def test_no_privileged_intents_are_requested(bot):
    """Slash commands only: no message content, members or presences."""
    assert bot.intents.message_content is False
    assert bot.intents.members is False
    assert bot.intents.presences is False
    assert bot.intents.guilds is True


async def test_azure_commands_are_disabled_when_unconfigured(config):
    from dataclasses import replace

    instance = CraftyBot(replace(config, azure=replace(config.azure, vm_name="")))
    try:
        assert instance.azure.enabled is False
        assert instance.orchestrator.azure_enabled is False
    finally:
        await instance.crafty.close()
        await instance.azure.close()


async def test_crafty_errors_are_explained_with_the_vm_state(bot, monkeypatch):
    async def fake_state():
        return "deallocated"

    monkeypatch.setattr(bot.orchestrator, "vm_power_state", fake_state)
    hint = await bot.context_hint(CraftyUnavailable())
    assert "deallocated" in hint
    assert "/azure start" in hint


async def test_no_hint_when_azure_is_not_configured(config):
    from dataclasses import replace

    instance = CraftyBot(replace(config, azure=replace(config.azure, subscription_id="")))
    try:
        assert await instance.context_hint(CraftyUnavailable()) == ""
    finally:
        await instance.crafty.close()
        await instance.azure.close()


# --------------------------------------------------------------------------- #
# Crafty runs on the Azure VM: no VM, no Crafty request
# --------------------------------------------------------------------------- #
def _vm(power_state: str) -> VmStatus:
    return VmStatus(name="mc-vm", power_state=power_state)


async def test_crafty_is_not_contacted_while_the_vm_is_stopped(bot, monkeypatch):
    async def stopped(*, use_cache: bool = True) -> VmStatus:
        return _vm(POWER_DEALLOCATED)

    monkeypatch.setattr(bot.azure, "get_vm_status", stopped)
    assert await bot.crafty_host_available() is False
    with pytest.raises(CraftyHostOffline):
        await bot.crafty.list_servers()


async def test_crafty_is_contacted_once_the_vm_runs_or_is_starting(bot, monkeypatch):
    for state in (POWER_RUNNING, "starting", "unknown"):

        async def status(*, use_cache: bool = True, state=state) -> VmStatus:
            return _vm(state)

        monkeypatch.setattr(bot.azure, "get_vm_status", status)
        assert await bot.crafty_host_available() is True


async def test_azure_failures_do_not_block_crafty(bot, monkeypatch):
    async def broken(*, use_cache: bool = True) -> VmStatus:
        raise AzureAuthError()

    monkeypatch.setattr(bot.azure, "get_vm_status", broken)
    assert await bot.crafty_host_available() is True


async def test_azure_start_only_starts_the_vm_by_default(bot):
    group = next(c for c in bot.tree.get_commands() if c.name == "azure")
    start = next(c for c in group.commands if c.name == "start")
    option = next(p for p in start.parameters if p.name == "start_minecraft")
    assert option.default is False


# --------------------------------------------------------------------------- #
# Webhook cog guard rails
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "url",
    [
        "https://discord.com/api/webhooks/123456789/abcDEF-_123",
        "https://discordapp.com/api/webhooks/1/tok",
        "https://ptb.discord.com/api/webhooks/1/tok",
    ],
)
def test_real_discord_webhook_urls_are_accepted(url):
    from bot.cogs.webhooks import DISCORD_WEBHOOK_RE

    assert DISCORD_WEBHOOK_RE.match(url)


@pytest.mark.parametrize(
    "url",
    [
        "http://discord.com/api/webhooks/1/tok",  # not TLS
        "https://evil.example.com/api/webhooks/1/tok",  # would leak activity
        "https://discord.com/api/webhooks/1/tok?x=1",
        "https://discord.com/channels/1/2",
        "not a url",
    ],
)
def test_other_urls_are_rejected(url):
    from bot.cogs.webhooks import DISCORD_WEBHOOK_RE

    assert DISCORD_WEBHOOK_RE.match(url) is None


def test_every_webhook_preset_uses_events_crafty_knows():
    from bot.cogs.webhooks import EVENT_PRESETS, PRESET_CHOICES
    from bot.services.crafty import WEBHOOK_EVENTS

    for events in EVENT_PRESETS.values():
        assert set(events) <= set(WEBHOOK_EVENTS)
    # Every preset must be reachable from the slash command.
    assert {choice.value for choice in PRESET_CHOICES} == set(EVENT_PRESETS)


def test_every_schedule_action_choice_is_supported():
    from bot.cogs.schedule import ACTION_CHOICES
    from bot.services.crafty import TASK_ACTIONS

    assert [choice.value for choice in ACTION_CHOICES] == list(TASK_ACTIONS)


def test_every_roster_choice_maps_to_a_file():
    from bot.cogs.server import ROSTER_CHOICES, ROSTER_FILES

    assert {choice.value for choice in ROSTER_CHOICES} == set(ROSTER_FILES)
