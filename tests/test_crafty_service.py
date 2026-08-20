"""Tests for :mod:`bot.services.crafty` against a local stand-in Crafty API."""

from __future__ import annotations

from dataclasses import replace

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from bot.errors import (
    CraftyAPIError,
    CraftyAuthError,
    CraftyNotFound,
    CraftyUnavailable,
)
from bot.services.crafty import CraftyService

SERVER_ID = "b9c1f0a2-1111-4222-8333-444455556666"

SERVER_ROW = {
    "server_id": SERVER_ID,
    "server_name": "Survival",
    "type": "minecraft-java",
    "server_ip": "127.0.0.1",
    "server_port": 25565,
}

RUNNING_STATS = {
    "status": "ok",
    "data": {
        "stats_id": 42,
        "created": "2026-08-20 21:00:00",
        "server_id": SERVER_ROW,
        "started": "2026-08-20 18:00:00",
        "running": True,
        "cpu": 23.5,
        "mem": 4831838208,
        "mem_percent": 60.0,
        "world_name": "Survival",
        "world_size": "10.3GB",
        "server_port": 25565,
        "int_ping_results": "True",
        "online": 3,
        "max": 20,
        "players": "[{'name': 'PlayerOne', 'id': 'uuid-1'}, {'name': 'PlayerTwo', 'id': 'uuid-2'}]",
        "desc": "A Minecraft Server",
        "version": "Paper 1.21.4",
        "updating": False,
        "waiting_start": False,
        "crashed": False,
    },
}

STOPPED_STATS = {
    "status": "ok",
    "data": {
        "server_id": SERVER_ROW,
        "started": "",
        "running": False,
        "cpu": 0,
        "mem": 0,
        "mem_percent": 0,
        "world_name": "Survival",
        "world_size": "10.3GB",
        "server_port": 25565,
        "int_ping_results": "False",
        "online": False,
        "max": False,
        "players": False,
        "desc": False,
        "version": False,
        "crashed": False,
    },
}


class FakeCrafty:
    """A minimal HTTP app that mimics the endpoints the bot uses."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.bodies: list[str] = []
        self.auth_headers: list[str | None] = []
        self.stats = RUNNING_STATS
        self.fail_times = 0
        self.status_override: tuple[int, dict] | None = None

        app = web.Application()
        app.router.add_get("/api/v2/crafty/check", self._check)
        app.router.add_get("/api/v2/crafty/stats", self._host_stats)
        app.router.add_get("/api/v2/servers", self._servers)
        app.router.add_get("/api/v2/servers/{sid}", self._server)
        app.router.add_get("/api/v2/servers/{sid}/stats", self._server_stats)
        app.router.add_post("/api/v2/servers/{sid}/action/{action}", self._action)
        app.router.add_post("/api/v2/servers/{sid}/action/{action}/{aid}", self._action)
        app.router.add_post("/api/v2/servers/{sid}/stdin", self._stdin)
        app.router.add_get("/api/v2/servers/{sid}/logs", self._logs)
        app.router.add_get("/api/v2/servers/{sid}/backups", self._backups)
        app.router.add_post("/api/v2/servers/{sid}/tasks/{tid}/run", self._run_task)
        self.app = app

    # -- helpers -------------------------------------------------------- #
    def _record(self, request: web.Request) -> None:
        self.calls.append((request.method, request.path))
        self.auth_headers.append(request.headers.get("Authorization"))

    # -- handlers ------------------------------------------------------- #
    async def _check(self, request: web.Request) -> web.Response:
        self._record(request)
        if self.fail_times > 0:
            self.fail_times -= 1
            return web.Response(status=502, text='{"status": "error"}')
        return web.json_response({"status": "ok"})

    async def _host_stats(self, request: web.Request) -> web.Response:
        self._record(request)
        return web.json_response(
            {
                "status": "ok",
                "data": {
                    "cpu_usage": 12.5,
                    "mem_percent": 55.0,
                    "mem_usage": "4.4GB",
                    "mem_total": "8.0GB",
                    "boot_time": "2026-08-18 09:00:00",
                    "disk_json": "[{'device': '/dev/sda1', 'used': '42GB', "
                    "'total': '128GB', 'percent_used': 33}]",
                },
            }
        )

    async def _servers(self, request: web.Request) -> web.Response:
        self._record(request)
        if self.status_override:
            status, body = self.status_override
            return web.json_response(body, status=status)
        return web.json_response({"status": "ok", "data": [SERVER_ROW]})

    async def _server(self, request: web.Request) -> web.Response:
        self._record(request)
        return web.json_response({"status": "ok", "data": dict(SERVER_ROW, auto_start=True)})

    async def _server_stats(self, request: web.Request) -> web.Response:
        self._record(request)
        if request.match_info["sid"] != SERVER_ID:
            return web.json_response({"status": "error", "error": "SERVER_NOT_FOUND"}, status=404)
        return web.json_response(self.stats)

    async def _action(self, request: web.Request) -> web.Response:
        self._record(request)
        return web.json_response({"status": "ok"})

    async def _stdin(self, request: web.Request) -> web.Response:
        self._record(request)
        self.bodies.append(await request.text())
        return web.json_response({"status": "ok"})

    async def _logs(self, request: web.Request) -> web.Response:
        self._record(request)
        self.calls[-1] = (request.method, f"{request.path}?file={request.query.get('file')}")
        return web.json_response(
            {"status": "ok", "data": [f"[21:14:{i:02d}] line {i}" for i in range(30)]}
        )

    async def _backups(self, request: web.Request) -> web.Response:
        self._record(request)
        return web.json_response(
            {
                "b-1": {
                    "backup_id": "b-1",
                    "backup_name": "Nightly",
                    "default": False,
                    "enabled": True,
                    "compress": True,
                    "shutdown": False,
                    "max_backups": 5,
                    "backup_type": "zip_vault",
                },
                "b-2": {
                    "backup_id": "b-2",
                    "backup_name": "Default Backup",
                    "default": True,
                    "enabled": True,
                    "compress": False,
                    "shutdown": True,
                    "max_backups": 0,
                    "backup_type": "snapshot",
                },
            }
        )

    async def _run_task(self, request: web.Request) -> web.Response:
        self._record(request)
        self.bodies.append(await request.text())
        return web.json_response({"status": "ok"})


@pytest.fixture
async def fake_crafty():
    api = FakeCrafty()
    server = TestServer(api.app)
    await server.start_server()
    api.url = str(server.make_url("")).rstrip("/")
    yield api
    await server.close()


@pytest.fixture
async def service(fake_crafty, crafty_config):
    svc = CraftyService(replace(crafty_config, url=fake_crafty.url))
    yield svc
    await svc.close()


class _FlakyRequest:
    """Wraps ``session.request`` so the first N calls fail at transport level."""

    def __init__(self, session: aiohttp.ClientSession, fails: int) -> None:
        self._session = session
        self._fails = fails
        self.attempts = 0

    def __call__(self, *args, **kwargs):
        self.attempts += 1
        if self._fails > 0:
            self._fails -= 1
            raise aiohttp.ClientConnectionError("simulated connection reset")
        return self._session.request(*args, **kwargs)


class _SessionProxy:
    """Stands in for a ClientSession, forwarding to a flaky request wrapper."""

    closed = False

    def __init__(self, request) -> None:
        self.request = request


@pytest.fixture
async def flaky(service):
    """Install a transport that fails the first N requests of ``service``."""
    sessions: list[aiohttp.ClientSession] = []

    async def install(fails: int) -> _FlakyRequest:
        session = aiohttp.ClientSession()
        sessions.append(session)
        wrapper = _FlakyRequest(session, fails)
        proxy = _SessionProxy(wrapper)

        async def get_session():
            return proxy

        service._get_session = get_session  # noqa: SLF001 - deliberate test seam
        return wrapper

    yield install
    for session in sessions:
        await session.close()


# --------------------------------------------------------------------------- #
# Health and metadata
# --------------------------------------------------------------------------- #
async def test_check_connection_ok(service, fake_crafty):
    assert await service.check_connection() is True
    # The health probe must not send the API token.
    assert fake_crafty.auth_headers == [None]


async def test_check_connection_handles_unreachable_host(crafty_config):
    svc = CraftyService(replace(crafty_config, url="http://127.0.0.1:9", timeout=1.0))
    try:
        assert await svc.check_connection() is False
    finally:
        await svc.close()


async def test_list_servers_is_cached(service, fake_crafty):
    first = await service.list_servers()
    second = await service.list_servers()
    assert first == second
    assert first[0].name == "Survival"
    assert first[0].is_minecraft is True
    assert fake_crafty.calls.count(("GET", "/api/v2/servers")) == 1


async def test_requests_send_bearer_token(service, fake_crafty):
    await service.list_servers()
    assert fake_crafty.auth_headers[-1] == "Bearer test-token"


async def test_resolve_server_id_by_name_and_default(service, crafty_config, fake_crafty):
    assert await service.resolve_server_id(SERVER_ID) == SERVER_ID
    assert await service.resolve_server_id("survival") == SERVER_ID
    # Only one server exists, so no argument is needed either.
    assert await service.resolve_server_id(None) == SERVER_ID

    with pytest.raises(CraftyNotFound):
        await service.resolve_server_id("does-not-exist")


async def test_host_stats_parsing(service):
    host = await service.get_host_stats()
    assert host.cpu_percent == 12.5
    assert host.memory_total == "8.0GB"
    assert host.primary_disk["used"] == "42GB"
    assert host.boot_time is not None


# --------------------------------------------------------------------------- #
# Statistics
# --------------------------------------------------------------------------- #
async def test_running_stats_are_normalised(service):
    stats = await service.get_stats(SERVER_ID)
    assert stats.running is True
    assert stats.state == "running"
    assert stats.online == 3
    assert stats.max_players == 20
    assert [p.name for p in stats.players] == ["PlayerOne", "PlayerTwo"]
    assert stats.players[0].uuid == "uuid-1"
    assert stats.version == "Paper 1.21.4"
    assert stats.cpu_percent == 23.5
    assert stats.memory_bytes == 4831838208
    assert stats.port == 25565
    assert stats.started_at is not None
    assert stats.name == "Survival"
    assert stats.reachable is True


async def test_stopped_stats_use_none_instead_of_false(service, fake_crafty):
    fake_crafty.stats = STOPPED_STATS
    stats = await service.get_stats(SERVER_ID)
    assert stats.running is False
    assert stats.state == "stopped"
    assert stats.version is None
    assert stats.online is None
    assert stats.players == ()
    assert stats.started_at is None


async def test_zero_players_is_not_treated_as_missing(service, fake_crafty):
    fake_crafty.stats = {
        "status": "ok",
        "data": dict(RUNNING_STATS["data"], online=0, players="[]", cpu=0),
    }
    stats = await service.get_stats(SERVER_ID)
    assert stats.online == 0
    assert stats.cpu_percent == 0.0
    assert stats.players == ()


async def test_stats_accept_the_documented_flat_shape(service, fake_crafty):
    """The published OpenAPI spec puts statistics at the top level."""
    fake_crafty.stats = {
        "status": "ok",
        "running": True,
        "online": 1,
        "max": 10,
        "version": "1.21.4",
        "cpu": 5,
        "mem": 1024,
        "data": SERVER_ROW,
    }
    stats = await service.get_stats(SERVER_ID)
    assert stats.running is True
    assert stats.online == 1
    assert stats.version == "1.21.4"


async def test_stats_can_be_cached(service, fake_crafty):
    await service.get_stats(SERVER_ID, ttl=60)
    await service.get_stats(SERVER_ID, ttl=60)
    assert fake_crafty.calls.count(("GET", f"/api/v2/servers/{SERVER_ID}/stats")) == 1


async def test_unknown_server_raises_not_found(service):
    with pytest.raises(CraftyNotFound):
        await service.get_stats("11111111-2222-3333-4444-555555555555")


# --------------------------------------------------------------------------- #
# Actions
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "action", ["start_server", "stop_server", "restart_server", "kill_server"]
)
async def test_lifecycle_actions_hit_the_right_endpoint(service, fake_crafty, action):
    await getattr(service, action)(SERVER_ID)
    assert ("POST", f"/api/v2/servers/{SERVER_ID}/action/{action}") in fake_crafty.calls


async def test_actions_invalidate_the_stats_cache(service, fake_crafty):
    await service.get_stats(SERVER_ID, ttl=60)
    await service.stop_server(SERVER_ID)
    await service.get_stats(SERVER_ID, ttl=60)
    assert fake_crafty.calls.count(("GET", f"/api/v2/servers/{SERVER_ID}/stats")) == 2


async def test_unsupported_action_is_rejected_locally(service, fake_crafty):
    with pytest.raises(CraftyAPIError):
        await service.send_action(SERVER_ID, "format_disk")
    assert fake_crafty.calls == []


async def test_console_command_is_sent_as_plain_text_without_slash(service, fake_crafty):
    await service.send_console_command(SERVER_ID, "/say Hello from Discord!")
    assert ("POST", f"/api/v2/servers/{SERVER_ID}/stdin") in fake_crafty.calls
    assert fake_crafty.bodies == ["say Hello from Discord!"]


async def test_empty_console_command_is_rejected(service):
    with pytest.raises(CraftyAPIError):
        await service.send_console_command(SERVER_ID, "   /  ")


async def test_logs_are_limited_and_source_selectable(service, fake_crafty):
    lines = await service.get_logs(SERVER_ID, lines=5)
    assert len(lines) == 5
    assert lines[-1].endswith("line 29")
    assert ("GET", f"/api/v2/servers/{SERVER_ID}/logs?file=false") in fake_crafty.calls

    await service.get_logs(SERVER_ID, lines=5, from_file=True)
    assert ("GET", f"/api/v2/servers/{SERVER_ID}/logs?file=true") in fake_crafty.calls


# --------------------------------------------------------------------------- #
# Backups and scheduler
# --------------------------------------------------------------------------- #
async def test_list_backups_handles_the_mapping_response(service):
    backups = await service.list_backups(SERVER_ID)
    assert {b.backup_id for b in backups} == {"b-1", "b-2"}
    default = next(b for b in backups if b.default)
    assert default.name == "Default Backup"
    assert default.shutdown is True


async def test_start_backup_uses_the_default_configuration(service, fake_crafty):
    chosen = await service.start_backup(SERVER_ID)
    assert chosen.backup_id == "b-2"
    assert (
        "POST",
        f"/api/v2/servers/{SERVER_ID}/action/backup_server/b-2",
    ) in fake_crafty.calls


async def test_start_backup_rejects_an_unknown_configuration(service):
    with pytest.raises(CraftyNotFound):
        await service.start_backup(SERVER_ID, "nope")


async def test_run_task_sends_the_cascade_flag(service, fake_crafty):
    await service.run_task(SERVER_ID, "7", cascade=True)
    assert ("POST", f"/api/v2/servers/{SERVER_ID}/tasks/7/run") in fake_crafty.calls
    assert '"cascade": true' in fake_crafty.bodies[-1]


# --------------------------------------------------------------------------- #
# Error handling
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("status", "body", "expected"),
    [
        (401, {"status": "error", "error": "ACCESS_DENIED"}, CraftyAuthError),
        (403, {"status": "error", "error": "ACCESS_DENIED"}, CraftyAuthError),
        # Crafty answers permission problems with HTTP 400 + NOT_AUTHORIZED.
        (400, {"status": "error", "error": "NOT_AUTHORIZED"}, CraftyAuthError),
        (400, {"status": "error", "error": "INVALID_JSON"}, CraftyAPIError),
        (404, {"status": "error"}, CraftyNotFound),
        (500, {"status": "error"}, CraftyAPIError),
    ],
)
async def test_http_errors_map_to_typed_exceptions(service, fake_crafty, status, body, expected):
    fake_crafty.status_override = (status, body)
    with pytest.raises(expected):
        await service.list_servers(force_refresh=True)


async def test_error_status_with_http_200_is_detected(service, fake_crafty):
    fake_crafty.status_override = (200, {"status": "error", "error": "SOMETHING"})
    with pytest.raises(CraftyAPIError):
        await service.list_servers(force_refresh=True)


async def test_authentication_failure_is_reported_by_check_authentication(service, fake_crafty):
    fake_crafty.status_override = (403, {"status": "error", "error": "ACCESS_DENIED"})
    assert await service.check_authentication() is False


async def test_connection_failure_raises_crafty_unavailable(crafty_config):
    svc = CraftyService(replace(crafty_config, url="http://127.0.0.1:9", timeout=1.0))
    try:
        with pytest.raises(CraftyUnavailable):
            await svc.list_servers()
    finally:
        await svc.close()


async def test_transport_failures_are_retried_for_reads(service, flaky):
    wrapper = await flaky(1)
    servers = await service.list_servers()
    assert servers
    assert wrapper.attempts == 2


async def test_writes_are_not_retried(service, flaky):
    wrapper = await flaky(1)
    with pytest.raises(CraftyUnavailable):
        await service.stop_server(SERVER_ID)
    assert wrapper.attempts == 1


async def test_error_messages_never_contain_the_token(service, fake_crafty):
    fake_crafty.status_override = (403, {"status": "error", "error": "ACCESS_DENIED"})
    with pytest.raises(CraftyAuthError) as excinfo:
        await service.list_servers(force_refresh=True)
    assert "test-token" not in str(excinfo.value)
