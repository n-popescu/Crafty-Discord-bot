"""Exception hierarchy shared by the service layer.

Every exception carries a short, user-facing message that is safe to show in
Discord. Credentials are never included in these messages.
"""

from __future__ import annotations


class BotError(Exception):
    """Base class for all errors raised by the bot's service layer."""

    #: Short message shown to Discord users.
    user_message = "An unexpected error occurred."

    def __init__(self, user_message: str | None = None) -> None:
        if user_message:
            self.user_message = user_message
        super().__init__(self.user_message)


class ConfigError(BotError):
    """Raised when the runtime configuration is invalid or incomplete."""


# --------------------------------------------------------------------------- #
# Crafty
# --------------------------------------------------------------------------- #
class CraftyError(BotError):
    """Base class for Crafty Controller failures."""

    user_message = "Crafty returned an unexpected error."


class CraftyUnavailable(CraftyError):
    """Crafty could not be reached (DNS, TCP, TLS or timeout)."""

    user_message = "Crafty is unreachable. The host may be offline."


class CraftyHostOffline(CraftyUnavailable):
    """The machine hosting Crafty is powered off, so no request was attempted."""

    user_message = (
        "The Azure VM that hosts Crafty is powered off, so Crafty was not contacted."
    )


class CraftyAuthError(CraftyError):
    """The API token is invalid, expired or lacks the required permission."""

    user_message = "Crafty rejected the API token (missing or insufficient permissions)."


class CraftyNotFound(CraftyError):
    """The requested Crafty resource does not exist."""

    user_message = "That resource does not exist in Crafty."


class CraftyAPIError(CraftyError):
    """Crafty replied, but with an error status or an unusable body."""


# --------------------------------------------------------------------------- #
# Azure
# --------------------------------------------------------------------------- #
class AzureError(BotError):
    """Base class for Azure failures."""

    user_message = "Azure returned an unexpected error."


class AzureNotConfigured(AzureError):
    """Azure support was requested but the configuration is incomplete."""

    user_message = "Azure integration is not configured."


class AzureAuthError(AzureError):
    """Authentication or authorisation against Azure failed."""

    user_message = "Azure authentication failed. Check the credentials and role assignment."


class AzureNotFound(AzureError):
    """The subscription, resource group or VM could not be found."""

    user_message = "The Azure VM, resource group or subscription could not be found."


class AzureUnavailable(AzureError):
    """Azure Resource Manager could not be reached in time."""

    user_message = "Azure Resource Manager is unreachable or timed out."


class AzureAPIError(AzureError):
    """Azure replied with an error status."""


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #
class OrchestrationError(BotError):
    """A multi-step Azure/Crafty workflow could not be completed."""


class OperationTimeout(OrchestrationError):
    """A workflow step did not reach its target state in time."""

    user_message = "The operation timed out before finishing."
