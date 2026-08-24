"""Configuration loading and validation.

All configuration comes from environment variables (optionally provided through
a ``.env`` file). Nothing is ever hard-coded, and secrets are only ever stored
on the config object -- never logged, never rendered into Discord messages.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from bot.errors import ConfigError

DEFAULT_COMPUTE_API_VERSION = "2024-07-01"
DEFAULT_NETWORK_API_VERSION = "2024-05-01"


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _env_bool(name: str, default: bool) -> bool:
    raw = _env(name)
    if not raw:
        return default
    return raw.lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int, minimum: int | None = None) -> int:
    raw = _env(name)
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer.") from exc
    if minimum is not None and value < minimum:
        raise ConfigError(f"{name} must be >= {minimum}.")
    return value


def _env_float(name: str) -> float | None:
    """Parse an optional float, e.g. a UTC offset such as ``-3.5``."""
    raw = _env(name)
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be a number (hours offset from UTC).") from exc


def _env_id_set(name: str) -> frozenset[int]:
    """Parse a comma/space separated list of Discord snowflakes."""
    raw = _env(name).replace(",", " ")
    ids: set[int] = set()
    for chunk in raw.split():
        try:
            ids.add(int(chunk))
        except ValueError as exc:
            raise ConfigError(f"{name} must only contain numeric Discord IDs.") from exc
    return frozenset(ids)


@dataclass(frozen=True)
class CraftyConfig:
    """Connection settings for a remote Crafty Controller instance."""

    url: str
    api_token: str
    verify_ssl: bool = True
    timeout: float = 10.0
    default_server_id: str = ""

    @property
    def configured(self) -> bool:
        return bool(self.url and self.api_token)


@dataclass(frozen=True)
class AzureConfig:
    """Settings for the Azure VM that hosts Crafty."""

    subscription_id: str = ""
    resource_group: str = ""
    vm_name: str = ""
    tenant_id: str = ""
    client_id: str = ""
    client_secret: str = ""
    timeout: float = 30.0
    compute_api_version: str = DEFAULT_COMPUTE_API_VERSION
    network_api_version: str = DEFAULT_NETWORK_API_VERSION

    @property
    def configured(self) -> bool:
        return bool(self.subscription_id and self.resource_group and self.vm_name)

    @property
    def has_service_principal(self) -> bool:
        return bool(self.tenant_id and self.client_id and self.client_secret)

    @property
    def vm_resource_id(self) -> str:
        return (
            f"/subscriptions/{self.subscription_id}"
            f"/resourceGroups/{self.resource_group}"
            f"/providers/Microsoft.Compute/virtualMachines/{self.vm_name}"
        )


@dataclass(frozen=True)
class PermissionConfig:
    """Discord IDs allowed to use each tier of commands."""

    admin_user_ids: frozenset[int] = frozenset()
    admin_role_ids: frozenset[int] = frozenset()
    server_control_role_ids: frozenset[int] = frozenset()
    azure_control_role_ids: frozenset[int] = frozenset()


@dataclass(frozen=True)
class Config:
    """Fully validated runtime configuration."""

    discord_token: str
    guild_id: int
    crafty: CraftyConfig
    azure: AzureConfig
    permissions: PermissionConfig
    log_level: str = "INFO"
    status_cache_ttl: float = 10.0
    #: UTC offset in hours of the machine running Crafty. Crafty reports naive
    #: local timestamps, so this is what lets a Pi in one time zone show a
    #: correct uptime for a VM in another. ``None`` = assume the same clock.
    crafty_utc_offset: float | None = None
    auto_shutdown_vm: bool = False
    auto_shutdown_delay: int = 300
    idle_shutdown_enabled: bool = False
    idle_shutdown_minutes: int = 30
    idle_check_interval: int = 900
    start_timeout: int = 600
    stop_timeout: int = 300

    @property
    def azure_enabled(self) -> bool:
        return self.azure.configured


def load_config(*, require_azure: bool = False) -> Config:
    """Build a :class:`Config` from the environment.

    Raises:
        ConfigError: if required variables are missing. The error message lists
            the missing variable *names* only -- never any values.
    """
    missing: list[str] = []

    discord_token = _env("DISCORD_TOKEN")
    if not discord_token:
        missing.append("DISCORD_TOKEN")

    crafty_url = _env("CRAFTY_URL").rstrip("/")
    if not crafty_url:
        missing.append("CRAFTY_URL")
    elif not crafty_url.startswith(("http://", "https://")):
        raise ConfigError("CRAFTY_URL must start with http:// or https://")

    crafty_token = _env("CRAFTY_API_TOKEN")
    if not crafty_token:
        missing.append("CRAFTY_API_TOKEN")

    azure = AzureConfig(
        subscription_id=_env("AZURE_SUBSCRIPTION_ID"),
        resource_group=_env("AZURE_RESOURCE_GROUP"),
        vm_name=_env("AZURE_VM_NAME"),
        tenant_id=_env("AZURE_TENANT_ID"),
        client_id=_env("AZURE_CLIENT_ID"),
        client_secret=_env("AZURE_CLIENT_SECRET"),
        timeout=float(_env_int("AZURE_TIMEOUT", 30, minimum=5)),
        compute_api_version=_env("AZURE_COMPUTE_API_VERSION", DEFAULT_COMPUTE_API_VERSION),
        network_api_version=_env("AZURE_NETWORK_API_VERSION", DEFAULT_NETWORK_API_VERSION),
    )
    if require_azure and not azure.configured:
        missing.extend(
            name
            for name, value in (
                ("AZURE_SUBSCRIPTION_ID", azure.subscription_id),
                ("AZURE_RESOURCE_GROUP", azure.resource_group),
                ("AZURE_VM_NAME", azure.vm_name),
            )
            if not value
        )

    if missing:
        raise ConfigError(
            "Missing configuration:\n" + "\n".join(f"- {name}" for name in missing)
        )

    return Config(
        discord_token=discord_token,
        guild_id=_env_int("DISCORD_GUILD_ID", 0, minimum=0),
        crafty=CraftyConfig(
            url=crafty_url,
            api_token=crafty_token,
            verify_ssl=_env_bool("CRAFTY_VERIFY_SSL", True),
            timeout=float(_env_int("CRAFTY_TIMEOUT", 10, minimum=1)),
            default_server_id=_env("CRAFTY_SERVER_ID"),
        ),
        azure=azure,
        permissions=PermissionConfig(
            admin_user_ids=_env_id_set("ADMIN_USER_IDS"),
            admin_role_ids=_env_id_set("ADMIN_ROLE_IDS"),
            server_control_role_ids=_env_id_set("SERVER_CONTROL_ROLE_IDS"),
            azure_control_role_ids=_env_id_set("AZURE_CONTROL_ROLE_IDS"),
        ),
        log_level=_env("LOG_LEVEL", "INFO").upper(),
        status_cache_ttl=float(_env_int("STATUS_CACHE_TTL", 10, minimum=0)),
        crafty_utc_offset=_env_float("CRAFTY_UTC_OFFSET"),
        auto_shutdown_vm=_env_bool("AUTO_SHUTDOWN_VM", False),
        auto_shutdown_delay=_env_int("AUTO_SHUTDOWN_DELAY", 300, minimum=0),
        idle_shutdown_enabled=_env_bool("IDLE_SHUTDOWN_ENABLED", False),
        idle_shutdown_minutes=_env_int("IDLE_SHUTDOWN_MINUTES", 30, minimum=1),
        idle_check_interval=_env_int("IDLE_CHECK_INTERVAL", 900, minimum=60),
        start_timeout=_env_int("START_TIMEOUT", 600, minimum=30),
        stop_timeout=_env_int("STOP_TIMEOUT", 300, minimum=30),
    )
