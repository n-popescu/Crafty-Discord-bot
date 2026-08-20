"""Tests for :mod:`bot.services.azure` against a local stand-in for Azure ARM.

``ARM_ENDPOINT`` is redirected to a local aiohttp app, so no test can ever reach
a real subscription, and the credential layer is replaced by a stub.
"""

from __future__ import annotations

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from bot.errors import (
    AzureAPIError,
    AzureAuthError,
    AzureNotConfigured,
    AzureNotFound,
    AzureUnavailable,
)
from bot.services import azure as azure_module
from bot.services.azure import AzureCredentialProvider, AzureService

VM_PATH = "/subscriptions/sub-1/resourceGroups/rg-1/providers/Microsoft.Compute/virtualMachines/mc-vm"
NIC_PATH = "/subscriptions/sub-1/resourceGroups/rg-1/providers/Microsoft.Network/networkInterfaces/nic-1"
IP_PATH = "/subscriptions/sub-1/resourceGroups/rg-1/providers/Microsoft.Network/publicIPAddresses/ip-1"


class StubCredentials:
    """Credential provider that hands out a fixed token."""

    def __init__(self, token: str = "arm-token") -> None:
        self.token = token
        self.calls = 0

    async def get_token(self) -> str:
        self.calls += 1
        return self.token

    async def close(self) -> None:
        return None


class FakeArm:
    """Implements the handful of ARM routes the bot calls."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.tokens: list[str | None] = []
        self.api_versions: list[tuple[str, str | None]] = []
        self.power_state = "PowerState/deallocated"
        self.status_override: tuple[int, dict] | None = None
        self.has_public_ip = True

        app = web.Application()
        app.router.add_get(VM_PATH, self._vm)
        app.router.add_get(f"{VM_PATH}/instanceView", self._instance_view)
        app.router.add_post(f"{VM_PATH}/start", self._accepted)
        app.router.add_post(f"{VM_PATH}/deallocate", self._accepted)
        app.router.add_post(f"{VM_PATH}/powerOff", self._accepted)
        app.router.add_post(f"{VM_PATH}/restart", self._accepted)
        app.router.add_get(NIC_PATH, self._nic)
        app.router.add_get(IP_PATH, self._public_ip)
        self.app = app

    def _record(self, request: web.Request) -> None:
        self.calls.append((request.method, request.path))
        self.tokens.append(request.headers.get("Authorization"))
        self.api_versions.append((request.path, request.query.get("api-version")))

    async def _vm(self, request: web.Request) -> web.Response:
        self._record(request)
        if self.status_override:
            status, body = self.status_override
            return web.json_response(body, status=status)
        return web.json_response(
            {
                "name": "mc-vm",
                "location": "westeurope",
                "properties": {
                    "hardwareProfile": {"vmSize": "Standard_B2s"},
                    "networkProfile": {"networkInterfaces": [{"id": NIC_PATH}]},
                },
            }
        )

    async def _instance_view(self, request: web.Request) -> web.Response:
        self._record(request)
        if self.status_override:
            status, body = self.status_override
            return web.json_response(body, status=status)
        return web.json_response(
            {
                "provisioningState": "Succeeded",
                "statuses": [
                    {"code": "ProvisioningState/succeeded"},
                    {"code": self.power_state},
                ],
            }
        )

    async def _accepted(self, request: web.Request) -> web.Response:
        self._record(request)
        if self.status_override:
            status, body = self.status_override
            return web.json_response(body, status=status)
        action = request.path.rsplit("/", 1)[-1]
        self.power_state = {
            "start": "PowerState/running",
            "deallocate": "PowerState/deallocated",
            "powerOff": "PowerState/stopped",
            "restart": "PowerState/running",
        }[action]
        return web.Response(status=202)

    async def _nic(self, request: web.Request) -> web.Response:
        self._record(request)
        ip_config = {"properties": {"privateIPAddress": "10.0.0.4"}}
        if self.has_public_ip:
            ip_config["properties"]["publicIPAddress"] = {"id": IP_PATH}
        return web.json_response({"properties": {"ipConfigurations": [ip_config]}})

    async def _public_ip(self, request: web.Request) -> web.Response:
        self._record(request)
        return web.json_response({"properties": {"ipAddress": "20.30.40.50"}})


@pytest.fixture
async def fake_arm(monkeypatch):
    arm = FakeArm()
    server = TestServer(arm.app)
    await server.start_server()
    monkeypatch.setattr(azure_module, "ARM_ENDPOINT", str(server.make_url("")).rstrip("/"))
    yield arm
    await server.close()


@pytest.fixture
def credentials() -> StubCredentials:
    return StubCredentials()


@pytest.fixture
async def service(fake_arm, azure_config, credentials):
    svc = AzureService(azure_config, credential_provider=credentials)
    yield svc
    await svc.close()


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #
async def test_service_is_disabled_without_configuration(azure_config):
    from dataclasses import replace

    svc = AzureService(replace(azure_config, vm_name=""))
    try:
        assert svc.enabled is False
        with pytest.raises(AzureNotConfigured):
            await svc.get_vm_status()
    finally:
        await svc.close()


# --------------------------------------------------------------------------- #
# Reads
# --------------------------------------------------------------------------- #
async def test_status_reports_power_state_and_metadata(service, fake_arm):
    status = await service.get_vm_status()
    assert status.power_state == "deallocated"
    assert status.is_stopped is True
    assert status.is_running is False
    assert status.location == "westeurope"
    assert status.vm_size == "Standard_B2s"
    assert status.name == "mc-vm"


async def test_requests_carry_the_bearer_token(service, fake_arm):
    await service.get_vm_status()
    assert fake_arm.tokens[-1] == "Bearer arm-token"


async def test_compute_and_network_api_versions_are_sent(service, fake_arm, azure_config):
    await service.get_vm_status()
    await service.get_public_ip()
    versions = dict(fake_arm.api_versions)
    assert versions[VM_PATH] == azure_config.compute_api_version
    assert versions[NIC_PATH] == azure_config.network_api_version
    assert versions[IP_PATH] == azure_config.network_api_version


async def test_metadata_is_cached_but_power_state_can_be_forced(service, fake_arm):
    await service.get_vm_status()
    await service.get_vm_status(use_cache=False)
    assert fake_arm.calls.count(("GET", VM_PATH)) == 1
    assert fake_arm.calls.count(("GET", f"{VM_PATH}/instanceView")) == 2


async def test_public_and_private_ip_lookup(service, fake_arm):
    assert await service.get_public_ip() == "20.30.40.50"
    assert await service.get_private_ip() == "10.0.0.4"


async def test_missing_public_ip_returns_none(service, fake_arm):
    fake_arm.has_public_ip = False
    assert await service.get_public_ip() is None


# --------------------------------------------------------------------------- #
# Writes and waiting
# --------------------------------------------------------------------------- #
async def test_start_then_wait_for_running(service, fake_arm):
    await service.start_vm()
    assert ("POST", f"{VM_PATH}/start") in fake_arm.calls
    status = await service.wait_for_running(timeout=5)
    assert status is not None and status.is_running


async def test_deallocate_then_wait_for_stopped(service, fake_arm):
    fake_arm.power_state = "PowerState/running"
    await service.stop_vm(deallocate=True)
    assert ("POST", f"{VM_PATH}/deallocate") in fake_arm.calls
    status = await service.wait_for_stopped(timeout=5)
    assert status is not None and status.power_state == "deallocated"


async def test_power_off_keeps_the_vm_allocated(service, fake_arm):
    fake_arm.power_state = "PowerState/running"
    await service.stop_vm(deallocate=False)
    assert ("POST", f"{VM_PATH}/powerOff") in fake_arm.calls
    assert (await service.get_vm_status(use_cache=False)).power_state == "stopped"


async def test_restart_requests_the_restart_action(service, fake_arm):
    await service.restart_vm()
    assert ("POST", f"{VM_PATH}/restart") in fake_arm.calls


async def test_wait_times_out_without_hanging(service, fake_arm):
    fake_arm.power_state = "PowerState/starting"
    assert await service.wait_for_running(timeout=0.2) is None


async def test_write_invalidates_cached_state(service, fake_arm):
    await service.get_vm_status()
    fake_arm.power_state = "PowerState/running"
    await service.start_vm()
    assert (await service.get_vm_status()).is_running is True


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (401, AzureAuthError),
        (403, AzureAuthError),
        (404, AzureNotFound),
        (409, AzureAPIError),
        (429, AzureAPIError),
        (500, AzureAPIError),
    ],
)
async def test_http_errors_map_to_typed_exceptions(service, fake_arm, status, expected):
    fake_arm.status_override = (status, {"error": {"code": "Whatever"}})
    with pytest.raises(expected):
        await service.get_vm_status(use_cache=False)


async def test_unreachable_arm_raises_azure_unavailable(azure_config, credentials, monkeypatch):
    monkeypatch.setattr(azure_module, "ARM_ENDPOINT", "http://127.0.0.1:9")
    from dataclasses import replace

    svc = AzureService(replace(azure_config, timeout=1.0), credential_provider=credentials)
    try:
        with pytest.raises(AzureUnavailable):
            await svc.get_vm_status()
    finally:
        await svc.close()


async def test_check_authentication_swallows_failures(service, fake_arm):
    fake_arm.status_override = (403, {"error": {"code": "AuthorizationFailed"}})
    assert await service.check_authentication() is False


# --------------------------------------------------------------------------- #
# Credential provider
# --------------------------------------------------------------------------- #
class FakeLogin:
    """Stand-in for the Entra ID token endpoint."""

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.payloads: list[dict[str, str]] = []
        app = web.Application()
        app.router.add_post("/{tenant}/oauth2/v2.0/token", self._token)
        self.app = app

    async def _token(self, request: web.Request) -> web.Response:
        self.payloads.append(dict(await request.post()))
        if self.fail:
            return web.json_response({"error": "invalid_client"}, status=401)
        return web.json_response({"access_token": "sp-token", "expires_in": 3600})


@pytest.fixture
async def fake_login(monkeypatch):
    login = FakeLogin()
    server = TestServer(login.app)
    await server.start_server()
    monkeypatch.setattr(azure_module, "LOGIN_ENDPOINT", str(server.make_url("")).rstrip("/"))
    yield login
    await server.close()


async def test_service_principal_flow_is_used_without_the_sdk(azure_config, fake_login):
    import aiohttp

    session = aiohttp.ClientSession()
    provider = AzureCredentialProvider(azure_config, lambda: _ready(session))
    # Pretend azure-identity is not installed.
    provider._sdk_unavailable = True  # noqa: SLF001
    try:
        token = await provider.get_token()
        assert token == "sp-token"
        # The token is cached, so a second call does not hit the endpoint again.
        assert await provider.get_token() == "sp-token"
        assert len(fake_login.payloads) == 1
        assert fake_login.payloads[0]["grant_type"] == "client_credentials"
        assert fake_login.payloads[0]["scope"] == azure_module.ARM_SCOPE
    finally:
        await provider.close()
        await session.close()


async def test_rejected_credentials_raise_auth_error(azure_config, fake_login):
    import aiohttp

    fake_login.fail = True
    session = aiohttp.ClientSession()
    provider = AzureCredentialProvider(azure_config, lambda: _ready(session))
    provider._sdk_unavailable = True  # noqa: SLF001
    try:
        with pytest.raises(AzureAuthError):
            await provider.get_token()
    finally:
        await provider.close()
        await session.close()


async def test_missing_service_principal_raises_auth_error(azure_config):
    from dataclasses import replace

    import aiohttp

    session = aiohttp.ClientSession()
    provider = AzureCredentialProvider(
        replace(azure_config, client_secret=""), lambda: _ready(session)
    )
    provider._sdk_unavailable = True  # noqa: SLF001
    try:
        with pytest.raises(AzureAuthError):
            await provider.get_token()
    finally:
        await session.close()


async def _ready(value):
    return value
