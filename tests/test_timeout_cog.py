"""``/timeout`` permission tiers, driven through the real command callback.

Arming a timeout is a *scheduled* stop, so it is gated exactly like the manual
one: stopping Minecraft needs the server tier, and deallocating the VM
additionally needs the Azure tier.
"""

from __future__ import annotations

from dataclasses import replace
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from bot.client import CraftyBot
from bot.config import PermissionConfig
from bot.errors import CraftyHostOffline

SERVER_ROLE = 10
AZURE_ROLE = 20


def member(user_id: int, roles: tuple[int, ...] = ()) -> MagicMock:
    who = MagicMock(spec=discord.Member)
    who.id = user_id
    who.roles = [MagicMock(id=role_id) for role_id in roles]
    who.guild_permissions = MagicMock(administrator=False)
    who.guild = MagicMock(owner_id=0)
    return who


@pytest.fixture
def tiered_config(config):
    return replace(
        config,
        permissions=PermissionConfig(
            server_control_role_ids=frozenset({SERVER_ROLE}),
            azure_control_role_ids=frozenset({AZURE_ROLE}),
        ),
    )


class Run:
    """The outcome of one ``/timeout`` invocation."""

    def __init__(self, state, refused: bool, embed) -> None:
        self.state = state
        self.refused = refused
        self.embed = embed


async def invoke(
    config, user, minutes=None, shutdown_vm=None, *, vm_off=False, prearm=None
) -> Run:
    bot = CraftyBot(config)
    await bot.load_extension("bot.cogs.timeout")
    cog = bot.get_cog("TimeoutCog")
    if vm_off:
        # Every Crafty call refuses before touching the network, exactly as it
        # does while the Azure VM is deallocated.
        bot.crafty.resolve_server_id = AsyncMock(side_effect=CraftyHostOffline())
        bot.crafty.list_servers = AsyncMock(side_effect=CraftyHostOffline())
    else:
        bot.crafty.resolve_server_id = AsyncMock(return_value="srv-1")
        bot.crafty.list_servers = AsyncMock(return_value=())
    if prearm:
        bot.timeouts.arm(prearm, 90, shutdown_vm=True, armed_by=user.id)

    interaction = MagicMock(spec=discord.Interaction)
    interaction.user = user
    interaction.guild_id = 0
    interaction.response = MagicMock()
    interaction.response.is_done.return_value = False
    interaction.response.defer = AsyncMock()
    interaction.response.send_message = AsyncMock()
    interaction.edit_original_response = AsyncMock()
    interaction.followup.send = AsyncMock()

    try:
        await cog.timeout.callback(cog, interaction, minutes, shutdown_vm, None)
        edited = interaction.edit_original_response.await_args
        return Run(
            state=bot.timeouts.get("srv-1"),
            # A refusal is the only thing sent with send_message before a defer.
            refused=interaction.response.send_message.await_count > 0,
            embed=edited.kwargs["embed"] if edited else None,
        )
    finally:
        await bot.crafty.close()
        await bot.azure.close()


async def test_a_user_without_roles_cannot_arm_a_timeout(tiered_config):
    run = await invoke(tiered_config, member(1), minutes=90)
    assert run.refused is True
    assert run.state is None


async def test_the_server_role_arms_a_minecraft_only_stop(tiered_config):
    """Without the Azure tier the timeout still works -- it just spares the VM."""
    run = await invoke(tiered_config, member(2, (SERVER_ROLE,)), minutes=90)
    assert run.refused is False
    assert run.state.minutes == 90
    assert run.state.shutdown_vm is False
    # And it says so, rather than quietly doing less than the user expected.
    assert "left running" in run.embed.description


async def test_asking_for_a_vm_shutdown_without_the_azure_role_is_refused(tiered_config):
    run = await invoke(
        tiered_config, member(2, (SERVER_ROLE,)), minutes=90, shutdown_vm=True
    )
    assert run.refused is True
    assert run.state is None


async def test_the_azure_role_arms_the_full_shutdown(tiered_config):
    run = await invoke(tiered_config, member(3, (SERVER_ROLE, AZURE_ROLE)), minutes=90)
    assert run.state.minutes == 90
    assert run.state.shutdown_vm is True


async def test_zero_minutes_cancels(tiered_config):
    user = member(3, (SERVER_ROLE, AZURE_ROLE))
    armed = await invoke(tiered_config, user, minutes=90)
    assert armed.state is not None

    cancelled = await invoke(tiered_config, user, minutes=0)
    assert cancelled.state is None


async def test_reading_the_state_needs_no_role(tiered_config):
    """`/timeout` with no argument is a read, open to everyone."""
    run = await invoke(tiered_config, member(1))
    assert run.refused is False
    assert run.state is None
    assert "Auto-shutdown" in run.embed.title


# --------------------------------------------------------------------------- #
# A powered-off VM must not block a bot-local operation
# --------------------------------------------------------------------------- #
@pytest.fixture
def offline_config(tiered_config):
    """Crafty unreachable, but the default server is known from the config."""
    return replace(
        tiered_config,
        crafty=replace(tiered_config.crafty, default_server_id="srv-1"),
    )


async def test_reading_the_timeout_works_while_the_vm_is_off(offline_config):
    """Checking whether auto-shutdown is set is exactly what you do when it is off."""
    run = await invoke(offline_config, member(1), vm_off=True)
    assert run.refused is False
    assert "Auto-shutdown" in run.embed.title
    assert any("not contacted" in field.value for field in run.embed.fields)


async def test_arming_works_while_the_vm_is_off(offline_config):
    run = await invoke(
        offline_config, member(3, (SERVER_ROLE, AZURE_ROLE)), minutes=90, vm_off=True
    )
    assert run.state is not None
    assert run.state.minutes == 90


async def test_cancelling_works_while_the_vm_is_off(offline_config):
    user = member(3, (SERVER_ROLE, AZURE_ROLE))
    run = await invoke(
        offline_config, user, minutes=0, vm_off=True, prearm="srv-1"
    )
    assert run.state is None
    assert "cancelled" in run.embed.title.lower()


async def test_an_armed_timeout_identifies_the_server_without_crafty(tiered_config):
    """No configured default, but one armed timeout says which server is meant."""
    run = await invoke(tiered_config, member(1), vm_off=True, prearm="srv-9")
    assert run.refused is False
    assert "srv-9" in run.embed.title


async def test_reading_with_nothing_to_go_on_still_answers(tiered_config):
    """Unreachable, no default, nothing armed: report, never error."""
    run = await invoke(tiered_config, member(1), vm_off=True)
    assert run.refused is False
    assert "No timeout is armed" in run.embed.description


async def test_arming_with_nothing_to_go_on_asks_which_server(tiered_config):
    """Arming genuinely needs a target, so this one is an honest error."""
    run = await invoke(
        tiered_config, member(3, (SERVER_ROLE, AZURE_ROLE)), minutes=90, vm_off=True
    )
    assert run.state is None
    assert "Which server?" in run.embed.title
