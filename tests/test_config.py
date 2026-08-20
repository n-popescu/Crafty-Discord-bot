"""Configuration loading and validation."""

from __future__ import annotations

import pytest

from bot.config import load_config
from bot.errors import ConfigError

REQUIRED = {
    "DISCORD_TOKEN": "discord-token",
    "CRAFTY_URL": "https://crafty.example.com:8443/",
    "CRAFTY_API_TOKEN": "crafty-token",
}


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for name in list(REQUIRED) + [
        "DISCORD_GUILD_ID",
        "CRAFTY_VERIFY_SSL",
        "CRAFTY_TIMEOUT",
        "CRAFTY_SERVER_ID",
        "AZURE_SUBSCRIPTION_ID",
        "AZURE_RESOURCE_GROUP",
        "AZURE_VM_NAME",
        "AZURE_TENANT_ID",
        "AZURE_CLIENT_ID",
        "AZURE_CLIENT_SECRET",
        "ADMIN_USER_IDS",
        "ADMIN_ROLE_IDS",
        "SERVER_CONTROL_ROLE_IDS",
        "AZURE_CONTROL_ROLE_IDS",
        "AUTO_SHUTDOWN_VM",
        "AUTO_SHUTDOWN_DELAY",
        "IDLE_SHUTDOWN_ENABLED",
        "LOG_LEVEL",
        "STATUS_CACHE_TTL",
    ]:
        monkeypatch.delenv(name, raising=False)


def set_required(monkeypatch, **overrides):
    values = {**REQUIRED, **overrides}
    for name, value in values.items():
        monkeypatch.setenv(name, value)


def test_minimal_configuration_is_accepted(monkeypatch):
    set_required(monkeypatch)
    config = load_config()

    assert config.discord_token == "discord-token"
    # Trailing slashes are trimmed so paths never double up.
    assert config.crafty.url == "https://crafty.example.com:8443"
    assert config.crafty.verify_ssl is True
    assert config.azure_enabled is False


def test_missing_variables_are_listed_without_values(monkeypatch):
    monkeypatch.setenv("CRAFTY_API_TOKEN", "super-secret")

    with pytest.raises(ConfigError) as excinfo:
        load_config()

    message = excinfo.value.user_message
    assert "DISCORD_TOKEN" in message
    assert "CRAFTY_URL" in message
    assert "super-secret" not in message


def test_crafty_url_must_have_a_scheme(monkeypatch):
    set_required(monkeypatch, CRAFTY_URL="crafty.example.com")
    with pytest.raises(ConfigError):
        load_config()


def test_private_vpn_address_is_accepted(monkeypatch):
    set_required(monkeypatch, CRAFTY_URL="https://100.101.102.103:8443")
    assert load_config().crafty.url == "https://100.101.102.103:8443"


def test_tls_verification_can_be_disabled(monkeypatch):
    set_required(monkeypatch, CRAFTY_VERIFY_SSL="false")
    assert load_config().crafty.verify_ssl is False


def test_azure_is_enabled_only_when_complete(monkeypatch):
    set_required(
        monkeypatch,
        AZURE_SUBSCRIPTION_ID="sub",
        AZURE_RESOURCE_GROUP="rg",
    )
    assert load_config().azure_enabled is False

    monkeypatch.setenv("AZURE_VM_NAME", "mc-vm")
    config = load_config()
    assert config.azure_enabled is True
    assert config.azure.vm_resource_id == (
        "/subscriptions/sub/resourceGroups/rg/providers/Microsoft.Compute/virtualMachines/mc-vm"
    )


def test_require_azure_reports_the_missing_azure_names(monkeypatch):
    set_required(monkeypatch)
    with pytest.raises(ConfigError) as excinfo:
        load_config(require_azure=True)
    assert "AZURE_SUBSCRIPTION_ID" in excinfo.value.user_message


def test_service_principal_detection(monkeypatch):
    set_required(
        monkeypatch,
        AZURE_SUBSCRIPTION_ID="sub",
        AZURE_RESOURCE_GROUP="rg",
        AZURE_VM_NAME="vm",
        AZURE_TENANT_ID="tenant",
        AZURE_CLIENT_ID="client",
    )
    assert load_config().azure.has_service_principal is False

    monkeypatch.setenv("AZURE_CLIENT_SECRET", "secret")
    assert load_config().azure.has_service_principal is True


def test_permission_id_lists_accept_commas_and_spaces(monkeypatch):
    set_required(
        monkeypatch,
        ADMIN_USER_IDS="123, 456",
        SERVER_CONTROL_ROLE_IDS="789 1011",
    )
    permissions = load_config().permissions
    assert permissions.admin_user_ids == frozenset({123, 456})
    assert permissions.server_control_role_ids == frozenset({789, 1011})
    assert permissions.azure_control_role_ids == frozenset()


def test_non_numeric_permission_ids_are_rejected(monkeypatch):
    set_required(monkeypatch, ADMIN_ROLE_IDS="@everyone")
    with pytest.raises(ConfigError):
        load_config()


def test_numeric_settings_are_validated(monkeypatch):
    set_required(monkeypatch, AUTO_SHUTDOWN_DELAY="not-a-number")
    with pytest.raises(ConfigError):
        load_config()

    set_required(monkeypatch, AUTO_SHUTDOWN_DELAY="600", AUTO_SHUTDOWN_VM="true")
    config = load_config()
    assert config.auto_shutdown_delay == 600
    assert config.auto_shutdown_vm is True


def test_log_level_is_normalised(monkeypatch):
    set_required(monkeypatch, LOG_LEVEL="debug")
    assert load_config().log_level == "DEBUG"
