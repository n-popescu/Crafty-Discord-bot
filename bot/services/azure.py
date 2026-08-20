"""Async client for the Azure VM that hosts Crafty Controller.

Design notes
------------
Credentials are obtained through the official Azure SDK
(:class:`azure.identity.aio.DefaultAzureCredential`) whenever ``azure-identity``
is installed, which covers environment variables, managed identity, the Azure
CLI and Workload Identity. If the SDK is unavailable -- a realistic situation on
a Raspberry Pi Zero W, where ``cryptography`` has no ARMv6 wheel -- the service
falls back to the standard OAuth2 client-credentials flow using the configured
service principal. Either way the VM itself is driven through the Azure Resource
Manager REST API over ``aiohttp``, which keeps memory use far below the
generated ``azure-mgmt-compute`` client and avoids blocking the event loop.

Azure never sees a subprocess: no Azure CLI is invoked.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from typing import Any, Mapping

import aiohttp

from bot.cache import TTLCache
from bot.config import AzureConfig
from bot.errors import (
    AzureAPIError,
    AzureAuthError,
    AzureNotConfigured,
    AzureNotFound,
    AzureUnavailable,
)
from bot.utils import poll_until

logger = logging.getLogger(__name__)

ARM_ENDPOINT = "https://management.azure.com"
ARM_SCOPE = "https://management.azure.com/.default"
LOGIN_ENDPOINT = "https://login.microsoftonline.com"

#: Power states reported by the VM instance view, without the ``PowerState/`` prefix.
POWER_RUNNING = "running"
POWER_DEALLOCATED = "deallocated"
POWER_STOPPED = "stopped"
POWER_UNKNOWN = "unknown"

STOPPED_STATES = frozenset({POWER_DEALLOCATED, POWER_STOPPED})

_METADATA_TTL = 300.0
_POWER_TTL = 5.0
_IP_TTL = 60.0


def _decode_json(raw: str) -> Any:
    """ARM answers write operations with an empty 202 body."""
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except ValueError:
        return {}


@dataclass(frozen=True)
class VmStatus:
    """Normalised power/provisioning state of the VM."""

    name: str
    power_state: str = POWER_UNKNOWN
    provisioning_state: str | None = None
    location: str | None = None
    vm_size: str | None = None

    @property
    def is_running(self) -> bool:
        return self.power_state == POWER_RUNNING

    @property
    def is_stopped(self) -> bool:
        return self.power_state in STOPPED_STATES

    @property
    def is_transitioning(self) -> bool:
        return self.power_state in {"starting", "stopping", "deallocating"}


class AzureCredentialProvider:
    """Supplies ARM access tokens, caching them until shortly before expiry."""

    def __init__(self, config: AzureConfig, session_factory) -> None:
        self._config = config
        self._session_factory = session_factory
        self._token: str | None = None
        self._expires_at: float = 0.0
        self._lock = asyncio.Lock()
        self._sdk_credential: Any | None = None
        self._sdk_unavailable = False

    async def close(self) -> None:
        if self._sdk_credential is not None:
            await self._sdk_credential.close()
            self._sdk_credential = None
        self._token = None

    async def get_token(self) -> str:
        async with self._lock:
            if self._token and time.monotonic() < self._expires_at:
                return self._token
            token, expires_in = await self._acquire()
            self._token = token
            # Renew a little early so a long operation never runs out of token.
            self._expires_at = time.monotonic() + max(expires_in - 300, 60)
            return token

    async def _acquire(self) -> tuple[str, float]:
        credential = await self._get_sdk_credential()
        if credential is not None:
            token = await credential.get_token(ARM_SCOPE)
            return token.token, max(token.expires_on - time.time(), 60)
        return await self._acquire_with_client_secret()

    async def _get_sdk_credential(self) -> Any | None:
        """Instantiate ``DefaultAzureCredential`` if the SDK is installed."""
        if self._sdk_credential is not None or self._sdk_unavailable:
            return self._sdk_credential
        try:
            from azure.identity.aio import DefaultAzureCredential
        except ImportError:
            self._sdk_unavailable = True
            if not self._config.has_service_principal:
                raise AzureAuthError(
                    "azure-identity is not installed and no service principal is configured."
                )
            logger.info(
                "azure-identity not installed; using service principal credentials directly."
            )
            return None
        self._sdk_credential = DefaultAzureCredential()
        return self._sdk_credential

    async def _acquire_with_client_secret(self) -> tuple[str, float]:
        """Standard OAuth2 client-credentials grant against Entra ID."""
        if not self._config.has_service_principal:
            raise AzureAuthError("No Azure service principal credentials are configured.")

        session = await self._session_factory()
        url = f"{LOGIN_ENDPOINT}/{self._config.tenant_id}/oauth2/v2.0/token"
        payload = {
            "grant_type": "client_credentials",
            "client_id": self._config.client_id,
            "client_secret": self._config.client_secret,
            "scope": ARM_SCOPE,
        }
        try:
            async with session.post(url, data=payload) as response:
                body = _decode_json(await response.text())
                if response.status >= 400:
                    # `body` may echo the request; never surface or log it.
                    logger.warning(
                        "Azure token request failed with HTTP %s (%s)",
                        response.status,
                        (body or {}).get("error", "unknown_error")
                        if isinstance(body, Mapping)
                        else "unknown_error",
                    )
                    raise AzureAuthError()
                token = (body or {}).get("access_token")
                if not token:
                    raise AzureAuthError()
                return str(token), float((body or {}).get("expires_in", 3600))
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            raise AzureUnavailable("Could not reach the Azure login endpoint.") from exc


class AzureService:
    """Start, stop and inspect the Azure VM that runs Crafty."""

    def __init__(
        self,
        config: AzureConfig,
        *,
        session: aiohttp.ClientSession | None = None,
        cache: TTLCache | None = None,
        credential_provider: AzureCredentialProvider | None = None,
    ) -> None:
        self._config = config
        self._session = session
        self._owns_session = session is None
        self._cache = cache or TTLCache()
        self._lock = asyncio.Lock()
        self._credentials = credential_provider or AzureCredentialProvider(
            config, self._get_session
        )

    @property
    def enabled(self) -> bool:
        """``True`` when subscription, resource group and VM name are configured."""
        return self._config.configured

    @property
    def vm_name(self) -> str:
        return self._config.vm_name

    def _require_enabled(self) -> None:
        if not self.enabled:
            raise AzureNotConfigured()

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            async with self._lock:
                if self._session is None or self._session.closed:
                    self._session = aiohttp.ClientSession(
                        timeout=aiohttp.ClientTimeout(total=self._config.timeout),
                        connector=aiohttp.TCPConnector(limit=4, ttl_dns_cache=300),
                        headers={"Accept": "application/json"},
                    )
                    self._owns_session = True
        return self._session

    async def close(self) -> None:
        await self._credentials.close()
        if self._session is not None and self._owns_session and not self._session.closed:
            await self._session.close()
        self._session = None

    # ------------------------------------------------------------------ #
    # REST plumbing
    # ------------------------------------------------------------------ #
    async def _request(
        self,
        method: str,
        resource_path: str,
        *,
        api_version: str | None = None,
        params: Mapping[str, str] | None = None,
    ) -> Any:
        self._require_enabled()
        session = await self._get_session()
        token = await self._credentials.get_token()
        query = {"api-version": api_version or self._config.compute_api_version}
        if params:
            query.update(params)

        url = f"{ARM_ENDPOINT}{resource_path}"
        try:
            async with session.request(
                method,
                url,
                params=query,
                headers={"Authorization": f"Bearer {token}"},
            ) as response:
                body = _decode_json(await response.text())
                if response.status >= 400:
                    self._raise_for_status(response.status, body)
                return body
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            logger.warning("Azure %s %s failed: %s", method, resource_path, type(exc).__name__)
            raise AzureUnavailable() from exc

    @staticmethod
    def _raise_for_status(status: int, body: Any) -> None:
        code = ""
        if isinstance(body, Mapping):
            error = body.get("error")
            if isinstance(error, Mapping):
                code = str(error.get("code") or "")
        logger.debug("Azure responded HTTP %s (%s)", status, code or "no code")

        if status in (401, 403):
            raise AzureAuthError()
        if status == 404:
            raise AzureNotFound()
        if status == 409:
            raise AzureAPIError(
                "Azure refused the request: another VM operation is still running."
            )
        if status == 429:
            raise AzureAPIError("Azure is rate-limiting requests. Try again shortly.")
        if status >= 500:
            raise AzureAPIError("Azure reported an internal error (HTTP 5xx).")
        raise AzureAPIError(f"Azure returned HTTP {status}{f' ({code})' if code else ''}.")

    # ------------------------------------------------------------------ #
    # Reads
    # ------------------------------------------------------------------ #
    async def get_vm_metadata(self) -> Mapping[str, Any]:
        """Read the VM resource (location, size, NIC references). Cached 5 min."""

        async def fetch() -> Mapping[str, Any]:
            body = await self._request("GET", self._config.vm_resource_id)
            return body if isinstance(body, Mapping) else {}

        return await self._cache.get_or_fetch("vm:metadata", _METADATA_TTL, fetch)

    async def get_vm_status(self, *, use_cache: bool = True) -> VmStatus:
        """Read the VM's power state from its instance view."""
        if not use_cache:
            self._cache.invalidate("vm:power")

        async def fetch() -> VmStatus:
            # Metadata is cached for minutes, so this rarely costs a request.
            metadata = await self.get_vm_metadata()
            body = await self._request(
                "GET", f"{self._config.vm_resource_id}/instanceView"
            )
            return self._parse_instance_view(
                body if isinstance(body, Mapping) else {}, metadata
            )

        return await self._cache.get_or_fetch("vm:power", _POWER_TTL, fetch)

    def _parse_instance_view(
        self, body: Mapping[str, Any], metadata: Mapping[str, Any]
    ) -> VmStatus:
        power_state = POWER_UNKNOWN
        statuses = body.get("statuses")
        if isinstance(statuses, list):
            for status in statuses:
                code = str(status.get("code", "")) if isinstance(status, Mapping) else ""
                if code.lower().startswith("powerstate/"):
                    power_state = code.split("/", 1)[1].lower()
                    break
        properties = metadata.get("properties") or {}
        hardware = properties.get("hardwareProfile") or {}
        return VmStatus(
            name=self._config.vm_name,
            power_state=power_state,
            provisioning_state=str(body.get("provisioningState") or "") or None,
            location=str(metadata.get("location") or "") or None,
            vm_size=str(hardware.get("vmSize") or "") or None,
        )

    async def get_public_ip(self) -> str | None:
        """Resolve the VM's public IP address, or ``None`` if it has none."""

        async def fetch() -> str:
            metadata = await self.get_vm_metadata()
            properties = metadata.get("properties") or {}
            interfaces = (properties.get("networkProfile") or {}).get(
                "networkInterfaces"
            ) or []
            for interface in interfaces:
                nic_id = interface.get("id") if isinstance(interface, Mapping) else None
                if not nic_id:
                    continue
                nic = await self._request(
                    "GET", nic_id, api_version=self._config.network_api_version
                )
                for ip_config in (nic.get("properties") or {}).get("ipConfigurations") or []:
                    public = (ip_config.get("properties") or {}).get("publicIPAddress")
                    if not public or not public.get("id"):
                        continue
                    public_ip = await self._request(
                        "GET",
                        public["id"],
                        api_version=self._config.network_api_version,
                    )
                    address = (public_ip.get("properties") or {}).get("ipAddress")
                    if address:
                        return str(address)
            # Cache the "no address" answer as an empty string, not as None.
            return ""

        address = await self._cache.get_or_fetch("vm:ip", _IP_TTL, fetch)
        return address or None

    async def get_private_ip(self) -> str | None:
        """Resolve the VM's primary private IP address (useful with a VPN)."""
        metadata = await self.get_vm_metadata()
        properties = metadata.get("properties") or {}
        interfaces = (properties.get("networkProfile") or {}).get("networkInterfaces") or []
        for interface in interfaces:
            nic_id = interface.get("id") if isinstance(interface, Mapping) else None
            if not nic_id:
                continue
            nic = await self._request(
                "GET", nic_id, api_version=self._config.network_api_version
            )
            for ip_config in (nic.get("properties") or {}).get("ipConfigurations") or []:
                address = (ip_config.get("properties") or {}).get("privateIPAddress")
                if address:
                    return str(address)
        return None

    # ------------------------------------------------------------------ #
    # Writes
    # ------------------------------------------------------------------ #
    async def start_vm(self) -> None:
        await self._request("POST", f"{self._config.vm_resource_id}/start")
        self._cache.invalidate("vm:power", "vm:ip")
        logger.info("Azure VM %s: start requested", self._config.vm_name)

    async def stop_vm(self, *, deallocate: bool = True) -> None:
        """Stop the VM. Deallocating also stops compute billing."""
        action = "deallocate" if deallocate else "powerOff"
        await self._request("POST", f"{self._config.vm_resource_id}/{action}")
        self._cache.invalidate("vm:power", "vm:ip")
        logger.info("Azure VM %s: %s requested", self._config.vm_name, action)

    async def restart_vm(self) -> None:
        await self._request("POST", f"{self._config.vm_resource_id}/restart")
        self._cache.invalidate("vm:power", "vm:ip")
        logger.info("Azure VM %s: restart requested", self._config.vm_name)

    # ------------------------------------------------------------------ #
    # Waiting
    # ------------------------------------------------------------------ #
    async def wait_for_power_state(
        self, target: frozenset[str] | set[str], *, timeout: float = 300.0
    ) -> VmStatus | None:
        """Poll the instance view with exponential backoff until it matches."""

        async def check() -> VmStatus | None:
            status = await self.get_vm_status(use_cache=False)
            return status if status.power_state in target else None

        return await poll_until(check, timeout=timeout, initial_interval=5.0, max_interval=20.0)

    async def wait_for_running(self, *, timeout: float = 300.0) -> VmStatus | None:
        return await self.wait_for_power_state({POWER_RUNNING}, timeout=timeout)

    async def wait_for_stopped(self, *, timeout: float = 300.0) -> VmStatus | None:
        return await self.wait_for_power_state(STOPPED_STATES, timeout=timeout)

    async def check_authentication(self) -> bool:
        """Verify that credentials work and the VM is visible."""
        if not self.enabled:
            return False
        try:
            await self.get_vm_metadata()
            return True
        except (AzureAuthError, AzureNotFound, AzureUnavailable, AzureAPIError):
            return False
