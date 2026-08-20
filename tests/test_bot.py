"""Bot wiring: the command surface, intents and error reporting."""

from __future__ import annotations

import pytest

from bot.client import COGS, CraftyBot
from bot.errors import CraftyUnavailable

EXPECTED_COMMANDS = {
    "status": set(),
    "health": set(),
    "server": {
        "status",
        "players",
        "info",
        "resources",
        "logs",
        "start",
        "stop",
        "restart",
        "kill",
        "command",
        "backups",
        "backup",
    },
    "azure": {"status", "ip", "start", "stop", "restart"},
    "minecraft": {"start", "stop", "restart"},
    "schedule": {"info", "run"},
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
