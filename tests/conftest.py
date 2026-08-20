"""Shared test fixtures.

No test ever touches a real Crafty Controller, a real Azure subscription or a
real Discord gateway: the Crafty/Azure APIs are served by a local aiohttp app
and the orchestrator is exercised against in-memory fakes.
"""

from __future__ import annotations

import pytest

from bot.config import AzureConfig, Config, CraftyConfig, PermissionConfig


@pytest.fixture
def crafty_config() -> CraftyConfig:
    return CraftyConfig(
        url="http://127.0.0.1:1",
        api_token="test-token",
        verify_ssl=False,
        timeout=2.0,
        default_server_id="",
    )


@pytest.fixture
def azure_config() -> AzureConfig:
    return AzureConfig(
        subscription_id="sub-1",
        resource_group="rg-1",
        vm_name="mc-vm",
        tenant_id="tenant",
        client_id="client",
        client_secret="secret",
        timeout=2.0,
    )


@pytest.fixture
def config(crafty_config: CraftyConfig, azure_config: AzureConfig) -> Config:
    return Config(
        discord_token="discord-token",
        guild_id=0,
        crafty=crafty_config,
        azure=azure_config,
        permissions=PermissionConfig(),
        status_cache_ttl=0.0,
        start_timeout=30,
        stop_timeout=30,
    )
