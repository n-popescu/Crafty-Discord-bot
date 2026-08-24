"""Async client for the Crafty Controller v2 API.

Every Crafty-specific detail (URL layout, response shapes, quirks) lives in this
module so that a Crafty API change only ever touches this file.

Verified against the Crafty Controller **4.10.8** source and the published v2
OpenAPI specification. Where the two disagree the source wins -- for example
``GET /api/v2/servers/{id}/stats`` nests every statistic inside ``data`` even
though the spec shows them at the top level, so both shapes are accepted here.

Endpoints used
--------------
======================================================= ==============================
``GET  /api/v2/crafty/check``                           connectivity probe (no auth)
``GET  /api/v2/crafty/stats``                           Crafty host CPU/RAM/disk
``GET  /api/v2/servers``                                servers the token may see
``GET  /api/v2/servers/{id}``                           server configuration
``GET  /api/v2/servers/{id}/stats``                      live statistics
``POST /api/v2/servers/{id}/action/{action}``           start/stop/restart/kill/backup
``POST /api/v2/servers/{id}/stdin``                     console command
``GET  /api/v2/servers/{id}/logs``                      terminal buffer or log file
``GET  /api/v2/servers/{id}/backups``                   backup configurations
``GET  /api/v2/servers/status``                         every server in one call
``GET  /api/v2/servers/{id}/history``                   last hour of samples
``POST /api/v2/servers/{id}/files``                     read a server file
``GET  /api/v2/servers/{id}/webhook``                   Crafty's own webhooks
``POST /api/v2/servers/{id}/webhook``                   create a webhook
``PATCH/DELETE .../webhook/{id}``                       edit / delete a webhook
``POST /api/v2/servers/{id}/webhook/{id}``              send a test webhook
``GET  /api/v2/servers/{id}/tasks/{taskId}``            one scheduled task
``POST /api/v2/servers/{id}/tasks``                     create a scheduled task
``PATCH/DELETE .../tasks/{taskId}``                     edit / delete a task
``POST /api/v2/servers/{id}/tasks/{taskId}/run``        run a scheduled task now
======================================================= ==============================

Deliberately not used
---------------------
* ``GET /api/v2/servers/{id}/tasks`` and ``/tasks/{id}/children`` are stub
  handlers in 4.10.8 (``def get(...): pass``), so *listing* schedules is not
  supported by the API and is not faked here -- creating, editing, deleting and
  running individual tasks all work and are exposed.
* The console WebSocket authenticates with a browser cookie and requires a
  permanently open connection, which suits neither an API token nor a Pi Zero W.
  Crafty's own webhooks cover the same ground by *pushing* events to Discord,
  which is why they are wired up here instead.
"""

from __future__ import annotations

import ast
import asyncio
import html
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Awaitable, Callable, Mapping, Sequence

import aiohttp

from bot.cache import TTLCache
from bot.config import CraftyConfig
from bot.errors import (
    CraftyAPIError,
    CraftyAuthError,
    CraftyHostOffline,
    CraftyNotFound,
    CraftyUnavailable,
)
from bot.utils import parse_timestamp

logger = logging.getLogger(__name__)

API = "/api/v2"

#: Actions accepted by ``POST /servers/{id}/action/{action}`` in Crafty 4.10.x.
SERVER_ACTIONS = frozenset(
    {
        "start_server",
        "stop_server",
        "restart_server",
        "kill_server",
        "backup_server",
        "update_executable",
    }
)

#: Events Crafty can fire a webhook for (``WebhookFactory.get_monitored_events``).
WEBHOOK_EVENTS = (
    "start_server",
    "stop_server",
    "crash_detected",
    "backup_server",
    "jar_update",
    "send_command",
    "kill",
)

#: Actions a scheduled task can perform.
#:
#: Crafty stores an ``action`` (what the panel shows) *and* a ``command`` (what
#: the scheduler actually runs). Its own front-end derives the second from the
#: first as ``f"{action}_server"`` unless the action is ``command``, in which
#: case the command is the console text; :meth:`CraftyService.create_task`
#: reproduces exactly that, so bot-made tasks are indistinguishable from
#: panel-made ones.
TASK_ACTIONS = ("start", "stop", "restart", "backup", "command")

_SERVERS_TTL = 60.0
_SERVER_TTL = 300.0
_AUTH_ERROR_CODES = {"NOT_AUTHORIZED", "ACCESS_DENIED", "UNAUTHORIZED"}


def _falsey_string(value: Any) -> bool:
    """Crafty reports "unknown" as ``False`` or as the string ``"False"``.

    A real numeric ``0`` (zero players online, 0% CPU) must survive, so booleans
    are compared by identity rather than by equality.
    """
    if value is None or value is False:
        return True
    if isinstance(value, str):
        return value.strip().lower() in {"", "false", "none", "0", "n/a"}
    return False


def _as_float(value: Any) -> float | None:
    if _falsey_string(value):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_int(value: Any) -> int | None:
    number = _as_float(value)
    return None if number is None else int(number)


def _as_text(value: Any) -> str | None:
    if _falsey_string(value):
        return None
    return str(value).strip() or None


@dataclass(frozen=True)
class Player:
    """A player reported by Crafty's server ping."""

    name: str
    uuid: str | None = None


@dataclass(frozen=True)
class ServerSummary:
    """Identity of a server, as returned by ``GET /servers``."""

    server_id: str
    name: str
    server_type: str = ""
    ip: str = ""
    port: int | None = None

    @property
    def is_minecraft(self) -> bool:
        return "minecraft" in self.server_type.lower()


@dataclass(frozen=True)
class ServerStats:
    """Normalised view of ``GET /servers/{id}/stats``.

    Anything Crafty could not determine is ``None`` so the Discord layer can
    render ``N/A`` instead of failing.
    """

    server_id: str
    name: str | None = None
    running: bool = False
    crashed: bool = False
    updating: bool = False
    waiting_start: bool = False
    online: int | None = None
    max_players: int | None = None
    players: tuple[Player, ...] = ()
    version: str | None = None
    description: str | None = None
    world_name: str | None = None
    world_size: str | None = None
    cpu_percent: float | None = None
    memory_bytes: float | None = None
    memory_percent: float | None = None
    port: int | None = None
    started_at: datetime | None = None
    reachable: bool = False

    @property
    def state(self) -> str:
        """A single word describing what the server is doing."""
        if self.crashed:
            return "crashed"
        if self.updating:
            return "updating"
        if self.waiting_start:
            return "starting"
        return "running" if self.running else "stopped"


@dataclass(frozen=True)
class HostStats:
    """Normalised view of ``GET /crafty/stats`` (the machine running Crafty)."""

    cpu_percent: float | None = None
    memory_percent: float | None = None
    memory_used: str | None = None
    memory_total: str | None = None
    boot_time: datetime | None = None
    disks: tuple[Mapping[str, Any], ...] = ()

    @property
    def primary_disk(self) -> Mapping[str, Any] | None:
        return self.disks[0] if self.disks else None


@dataclass(frozen=True)
class BackupConfig:
    """A backup configuration attached to a server."""

    backup_id: str
    name: str
    enabled: bool = True
    default: bool = False
    compress: bool = False
    shutdown: bool = False
    max_backups: int | None = None
    backup_type: str = ""


@dataclass(frozen=True)
class ScheduledTask:
    """A Crafty scheduled task (``GET /servers/{id}/tasks/{taskId}``)."""

    task_id: str
    name: str = ""
    action: str = ""
    enabled: bool = True
    interval: str = ""
    cron: str = ""
    next_run: str = ""
    raw: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ServerStatusLine:
    """One entry of ``GET /servers/status``.

    That endpoint answers for *every* server in a single unauthenticated call,
    which makes a multi-server overview cost one request instead of one per
    server -- the difference between usable and painful on a Pi Zero W.
    """

    server_id: str
    world_name: str | None = None
    running: bool = False
    online: int | None = None
    max_players: int | None = None
    version: str | None = None
    description: str | None = None


@dataclass(frozen=True)
class HistorySample:
    """One row of ``GET /servers/{id}/history`` (Crafty keeps ~1 hour)."""

    at: datetime | None = None
    running: bool = False
    cpu_percent: float | None = None
    memory_percent: float | None = None
    online: int | None = None


@dataclass(frozen=True)
class Webhook:
    """A Crafty webhook (``GET /servers/{id}/webhook``).

    Crafty fires these itself when a server starts, stops or crashes, so the bot
    does not have to poll for those events at all.
    """

    webhook_id: str
    name: str = ""
    provider: str = "Discord"
    url: str = ""
    bot_name: str = ""
    triggers: tuple[str, ...] = ()
    body: str = ""
    color: str = "#005cd1"
    enabled: bool = True


def _parse_players(raw: Any) -> tuple[Player, ...]:
    """Parse the ``players`` field.

    Crafty stores the ping's player sample in a text column, so this can be a
    real list, a JSON string, a Python ``repr`` of a list, or ``False``.
    """
    if _falsey_string(raw):
        return ()

    value: Any = raw
    if isinstance(value, str):
        text = value.strip()
        try:
            value = json.loads(text)
        except (TypeError, ValueError):
            try:
                value = ast.literal_eval(text)
            except (ValueError, SyntaxError):
                return ()

    if isinstance(value, Mapping):
        value = value.get("sample") or value.get("players") or []
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return ()

    players: list[Player] = []
    for entry in value:
        if isinstance(entry, Mapping):
            name = _as_text(entry.get("name")) or _as_text(entry.get("username"))
            if name:
                players.append(Player(name=name, uuid=_as_text(entry.get("id") or entry.get("uuid"))))
        else:
            name = _as_text(entry)
            if name:
                players.append(Player(name=name))
    return tuple(players)


class CraftyService:
    """Thin, async, retrying client for one remote Crafty Controller."""

    def __init__(
        self,
        config: CraftyConfig,
        *,
        session: aiohttp.ClientSession | None = None,
        cache: TTLCache | None = None,
        host_available: Callable[[], Awaitable[bool]] | None = None,
    ) -> None:
        self._config = config
        self._session = session
        self._owns_session = session is None
        self._cache = cache or TTLCache()
        self._lock = asyncio.Lock()
        #: Optional predicate telling whether the machine hosting Crafty is up.
        #: Crafty runs on the Azure VM, so there is no point in waiting for a
        #: TCP timeout while that VM is deallocated.
        self._host_available = host_available

    # ------------------------------------------------------------------ #
    # Session plumbing
    # ------------------------------------------------------------------ #
    @property
    def base_url(self) -> str:
        return self._config.url

    @property
    def default_server_id(self) -> str:
        return self._config.default_server_id

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            async with self._lock:
                if self._session is None or self._session.closed:
                    self._session = aiohttp.ClientSession(
                        timeout=aiohttp.ClientTimeout(total=self._config.timeout),
                        connector=aiohttp.TCPConnector(
                            limit=4, ttl_dns_cache=300, ssl=self._config.verify_ssl
                        ),
                        headers={"Accept": "application/json"},
                    )
                    self._owns_session = True
        return self._session

    async def close(self) -> None:
        if self._session is not None and self._owns_session and not self._session.closed:
            await self._session.close()
        self._session = None

    # ------------------------------------------------------------------ #
    # Request handling
    # ------------------------------------------------------------------ #
    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        json_body: Any = None,
        text_body: str | None = None,
        retries: int = 2,
        authenticated: bool = True,
    ) -> Any:
        """Perform one API call and return the decoded body.

        Only idempotent requests are retried, and the token is passed as a
        header so it can never leak into a URL, a log line or an error message.
        """
        await self._require_host()
        session = await self._get_session()
        url = f"{self._config.url}{path}"
        headers: dict[str, str] = {}
        if authenticated:
            headers["Authorization"] = f"Bearer {self._config.api_token}"
        if text_body is not None:
            headers["Content-Type"] = "text/plain"

        attempt = 0
        while True:
            attempt += 1
            try:
                async with session.request(
                    method,
                    url,
                    params=params,
                    json=json_body,
                    data=text_body,
                    headers=headers,
                ) as response:
                    body = await self._decode(response)
                    if response.status >= 400:
                        self._raise_for_status(response.status, body, path)
                    self._raise_for_error_body(body, path)
                    return body
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                # Network-level failure: retry idempotent calls, then give up.
                if attempt <= retries and method == "GET":
                    delay = 0.5 * attempt
                    logger.debug(
                        "Crafty %s %s failed (%s); retrying in %.1fs",
                        method,
                        path,
                        type(exc).__name__,
                        delay,
                    )
                    await asyncio.sleep(delay)
                    continue
                logger.warning(
                    "Crafty %s %s unreachable: %s", method, path, type(exc).__name__
                )
                raise CraftyUnavailable() from exc

    async def _require_host(self) -> None:
        """Refuse to send a request when Crafty's host is known to be down."""
        if self._host_available is None:
            return
        if not await self._host_available():
            raise CraftyHostOffline()

    @staticmethod
    async def _decode(response: aiohttp.ClientResponse) -> Any:
        raw = await response.text()
        if not raw:
            return {}
        try:
            return json.loads(raw)
        except ValueError:
            if response.status >= 400:
                return {}
            raise CraftyAPIError("Crafty returned a response that could not be parsed.")

    @staticmethod
    def _error_code(body: Any) -> str:
        if isinstance(body, Mapping):
            return str(body.get("error") or "").upper()
        return ""

    def _raise_for_status(self, status: int, body: Any, path: str) -> None:
        code = self._error_code(body)
        logger.debug("Crafty %s -> HTTP %s (%s)", path, status, code or "no code")

        if status in (401, 403) or code in _AUTH_ERROR_CODES:
            raise CraftyAuthError()
        if status == 404:
            raise CraftyNotFound()
        if status == 405:
            raise CraftyAPIError("Crafty does not support this operation.")
        if status == 409:
            raise CraftyAPIError("Crafty refused the request: the server is busy.")
        if status == 400:
            raise CraftyAPIError(
                f"Crafty rejected the request{f' ({code})' if code else ''}.", code=code
            )
        if status >= 500:
            raise CraftyAPIError("Crafty reported an internal error (HTTP 5xx).")
        raise CraftyAPIError(f"Crafty returned HTTP {status}.")

    def _raise_for_error_body(self, body: Any, path: str) -> None:
        """Crafty sometimes reports failures with HTTP 200 and ``status: error``."""
        if isinstance(body, Mapping) and str(body.get("status", "")).lower() == "error":
            code = self._error_code(body)
            if code in _AUTH_ERROR_CODES:
                raise CraftyAuthError()
            logger.debug("Crafty %s -> status=error (%s)", path, code or "no code")
            raise CraftyAPIError(
                f"Crafty could not complete the request{f' ({code})' if code else ''}.",
                code=code,
            )

    @staticmethod
    def _payload(body: Any) -> Any:
        """Return ``body["data"]`` when the standard envelope is used."""
        if isinstance(body, Mapping) and "data" in body:
            return body["data"]
        return body

    # ------------------------------------------------------------------ #
    # Health & metadata
    # ------------------------------------------------------------------ #
    async def check_connection(self) -> bool:
        """Return ``True`` if Crafty answers its unauthenticated health check."""
        try:
            await self._request("GET", f"{API}/crafty/check", authenticated=False, retries=1)
            return True
        except CraftyUnavailable:
            return False

    async def check_authentication(self) -> bool:
        """Return ``True`` if the configured API token is accepted."""
        try:
            await self.list_servers(force_refresh=True)
            return True
        except (CraftyAuthError, CraftyUnavailable, CraftyAPIError):
            return False

    async def get_host_stats(self) -> HostStats:
        """Read CPU/RAM/disk of the machine that runs Crafty (i.e. the Azure VM)."""
        body = await self._request("GET", f"{API}/crafty/stats")
        data = self._payload(body) or {}
        if not isinstance(data, Mapping):
            return HostStats()

        disks: tuple[Mapping[str, Any], ...] = ()
        raw_disks = data.get("disk_json") or data.get("disk_data")
        if isinstance(raw_disks, str):
            try:
                raw_disks = ast.literal_eval(raw_disks)
            except (ValueError, SyntaxError):
                raw_disks = []
        if isinstance(raw_disks, Sequence) and not isinstance(raw_disks, (str, bytes)):
            disks = tuple(item for item in raw_disks if isinstance(item, Mapping))

        return HostStats(
            cpu_percent=_as_float(data.get("cpu_usage")),
            memory_percent=_as_float(data.get("mem_percent")),
            memory_used=_as_text(data.get("mem_usage")),
            memory_total=_as_text(data.get("mem_total")),
            boot_time=parse_timestamp(data.get("boot_time")),
            disks=disks,
        )

    async def list_servers(self, *, force_refresh: bool = False) -> tuple[ServerSummary, ...]:
        """List every server the API token can see (cached for 60s)."""
        if force_refresh:
            self._cache.invalidate("servers")

        async def fetch() -> tuple[ServerSummary, ...]:
            body = await self._request("GET", f"{API}/servers")
            data = self._payload(body)
            if not isinstance(data, Sequence) or isinstance(data, (str, bytes)):
                return ()
            return tuple(
                ServerSummary(
                    server_id=str(item.get("server_id")),
                    name=_as_text(item.get("server_name")) or str(item.get("server_id")),
                    server_type=str(item.get("type") or ""),
                    ip=str(item.get("server_ip") or ""),
                    port=_as_int(item.get("server_port")),
                )
                for item in data
                if isinstance(item, Mapping) and item.get("server_id")
            )

        return await self._cache.get_or_fetch("servers", _SERVERS_TTL, fetch)

    async def list_server_status(self) -> tuple[ServerStatusLine, ...]:
        """Status of every server Crafty shows publicly, in a single request.

        ``GET /servers/status`` is the endpoint Crafty's own public dashboard
        uses: it is unauthenticated and only lists servers whose "show status"
        flag is on, so a server hidden in Crafty stays hidden here too.
        """
        body = await self._request("GET", f"{API}/servers/status", authenticated=False)
        data = self._payload(body)
        if not isinstance(data, Sequence) or isinstance(data, (str, bytes)):
            return ()
        return tuple(
            ServerStatusLine(
                server_id=str(item.get("id")),
                world_name=_as_text(item.get("world_name")),
                running=bool(item.get("running")),
                online=_as_int(item.get("online")),
                max_players=_as_int(item.get("max")),
                version=_as_text(item.get("version")),
                description=_as_text(item.get("desc")),
            )
            for item in data
            if isinstance(item, Mapping) and item.get("id")
        )

    async def get_history(self, server_id: str) -> tuple[HistorySample, ...]:
        """Return Crafty's stored samples for a server (roughly the last hour).

        Crafty already records these for its own graphs, so reading them costs
        one request and no extra work on the host.
        """
        body = await self._request("GET", f"{API}/servers/{server_id}/history")
        data = self._payload(body)
        if not isinstance(data, Sequence) or isinstance(data, (str, bytes)):
            return ()
        samples = [
            HistorySample(
                at=parse_timestamp(item.get("created")),
                running=bool(item.get("running")),
                cpu_percent=_as_float(item.get("cpu")),
                memory_percent=_as_float(item.get("mem_percent")),
                online=_as_int(item.get("online")),
            )
            for item in data
            if isinstance(item, Mapping)
        ]
        # The handler does not order its query, so sort by timestamp before use.
        samples.sort(key=lambda sample: sample.at or datetime.min)
        return tuple(samples)

    async def get_server(self, server_id: str) -> Mapping[str, Any]:
        """Read a server's configuration (cached for 5 minutes)."""

        async def fetch() -> Mapping[str, Any]:
            body = await self._request("GET", f"{API}/servers/{server_id}")
            data = self._payload(body)
            return data if isinstance(data, Mapping) else {}

        return await self._cache.get_or_fetch(f"server:{server_id}", _SERVER_TTL, fetch)

    async def resolve_server_id(self, requested: str | None = None) -> str:
        """Resolve a server id, a server name, or the configured default.

        Keeping this in one place is what allows several Crafty servers to be
        supported without hard-coding an id anywhere else in the code base.
        """
        candidate = (requested or self._config.default_server_id or "").strip()
        servers = await self.list_servers()

        if candidate:
            for server in servers:
                if server.server_id == candidate:
                    return server.server_id
            for server in servers:
                if server.name.casefold() == candidate.casefold():
                    return server.server_id
            raise CraftyNotFound(f"No Crafty server matches `{candidate}`.")

        if len(servers) == 1:
            return servers[0].server_id
        if not servers:
            raise CraftyNotFound("The API token has access to no servers.")
        raise CraftyNotFound(
            "Several servers are available: pass the `server` option or set CRAFTY_SERVER_ID."
        )

    # ------------------------------------------------------------------ #
    # Statistics
    # ------------------------------------------------------------------ #
    async def get_stats(self, server_id: str, *, ttl: float = 0.0) -> ServerStats:
        """Read live statistics for a server, optionally cached for ``ttl`` seconds."""
        if ttl <= 0:
            return await self._fetch_stats(server_id)
        return await self._cache.get_or_fetch(
            f"stats:{server_id}", ttl, lambda: self._fetch_stats(server_id)
        )

    async def _fetch_stats(self, server_id: str) -> ServerStats:
        body = await self._request("GET", f"{API}/servers/{server_id}/stats")
        data = self._payload(body)
        if not isinstance(data, Mapping):
            raise CraftyAPIError("Crafty returned unexpected statistics.")

        # The spec puts the statistics at the top level, the implementation
        # nests them in `data`; accept whichever shape we were given.
        if "running" not in data and isinstance(body, Mapping) and "running" in body:
            data = body

        # `server_id` is expanded into the full server object by the API.
        server_obj = data.get("server_id")
        server_meta: Mapping[str, Any] = server_obj if isinstance(server_obj, Mapping) else {}

        return ServerStats(
            server_id=str(server_meta.get("server_id") or server_id),
            name=_as_text(server_meta.get("server_name")) or _as_text(data.get("world_name")),
            running=bool(data.get("running")),
            crashed=bool(data.get("crashed")),
            updating=bool(data.get("updating")),
            waiting_start=bool(data.get("waiting_start")),
            online=_as_int(data.get("online")),
            max_players=_as_int(data.get("max")),
            players=_parse_players(data.get("players")),
            version=_as_text(data.get("version")),
            description=_as_text(data.get("desc")),
            world_name=_as_text(data.get("world_name")),
            world_size=_as_text(data.get("world_size")),
            cpu_percent=_as_float(data.get("cpu")),
            memory_bytes=_as_float(data.get("mem")),
            memory_percent=_as_float(data.get("mem_percent")),
            port=_as_int(data.get("server_port")),
            started_at=parse_timestamp(data.get("started")),
            reachable=str(data.get("int_ping_results", "")).lower() in {"true", "1"},
        )

    # ------------------------------------------------------------------ #
    # Actions
    # ------------------------------------------------------------------ #
    async def send_action(self, server_id: str, action: str, action_id: str | None = None) -> None:
        """Send one of :data:`SERVER_ACTIONS` to a server."""
        if action not in SERVER_ACTIONS:
            raise CraftyAPIError(f"`{action}` is not a supported Crafty action.")
        path = f"{API}/servers/{server_id}/action/{action}"
        if action_id:
            path = f"{path}/{action_id}"
        await self._request("POST", path, retries=0)
        self._cache.invalidate(f"stats:{server_id}")
        logger.info("Crafty action %s sent for server %s", action, server_id)

    async def start_server(self, server_id: str) -> None:
        await self.send_action(server_id, "start_server")

    async def stop_server(self, server_id: str) -> None:
        await self.send_action(server_id, "stop_server")

    async def restart_server(self, server_id: str) -> None:
        await self.send_action(server_id, "restart_server")

    async def kill_server(self, server_id: str) -> None:
        await self.send_action(server_id, "kill_server")

    async def send_console_command(self, server_id: str, command: str) -> None:
        """Send a console command through ``POST /stdin`` (no leading slash)."""
        payload = command.strip().lstrip("/")
        if not payload:
            raise CraftyAPIError("The console command is empty.")
        await self._request(
            "POST", f"{API}/servers/{server_id}/stdin", text_body=payload, retries=0
        )
        logger.info("Console command sent to server %s", server_id)

    async def get_logs(
        self, server_id: str, *, lines: int = 20, from_file: bool = False
    ) -> tuple[str, ...]:
        """Return the last ``lines`` log lines.

        ``from_file=False`` reads Crafty's terminal buffer (needs the TERMINAL
        API permission); ``from_file=True`` reads the server log file (needs
        LOGS).
        """
        body = await self._request(
            "GET",
            f"{API}/servers/{server_id}/logs",
            params={"file": str(from_file).lower()},
        )
        data = self._payload(body)
        if isinstance(data, str):
            data = data.splitlines()
        if not isinstance(data, Sequence):
            return ()
        # Crafty HTML-escapes every line (`&quot;`, `&#x27;`, `&lt;`) because the
        # same payload feeds its web terminal. Discord shows those entities
        # literally inside a code block, so undo the escaping here.
        cleaned = [
            html.unescape(str(line)).rstrip() for line in data if str(line).strip()
        ]
        return tuple(cleaned[-lines:])

    # ------------------------------------------------------------------ #
    # Backups
    # ------------------------------------------------------------------ #
    async def list_backups(self, server_id: str) -> tuple[BackupConfig, ...]:
        """List backup configurations.

        ``GET /backups`` answers with a mapping of ``backup_id -> config``
        (no ``status``/``data`` envelope), so both shapes are handled.
        """
        body = await self._request("GET", f"{API}/servers/{server_id}/backups")
        data = self._payload(body)
        entries: list[Mapping[str, Any]] = []
        if isinstance(data, Mapping):
            entries = [value for value in data.values() if isinstance(value, Mapping)]
        elif isinstance(data, Sequence) and not isinstance(data, (str, bytes)):
            entries = [item for item in data if isinstance(item, Mapping)]

        return tuple(
            BackupConfig(
                backup_id=str(entry.get("backup_id")),
                name=_as_text(entry.get("backup_name")) or "Backup",
                enabled=bool(entry.get("enabled", True)),
                default=bool(entry.get("default", False)),
                compress=bool(entry.get("compress", False)),
                shutdown=bool(entry.get("shutdown", False)),
                max_backups=_as_int(entry.get("max_backups")),
                backup_type=str(entry.get("backup_type") or ""),
            )
            for entry in entries
            if entry.get("backup_id")
        )

    async def start_backup(self, server_id: str, backup_id: str | None = None) -> BackupConfig:
        """Trigger a backup, defaulting to the server's default configuration."""
        backups = await self.list_backups(server_id)
        if not backups:
            raise CraftyNotFound("This server has no backup configuration in Crafty.")

        chosen: BackupConfig | None = None
        if backup_id:
            chosen = next((b for b in backups if b.backup_id == backup_id), None)
            if chosen is None:
                raise CraftyNotFound("That backup configuration does not exist.")
        else:
            chosen = next((b for b in backups if b.default), backups[0])

        await self.send_action(server_id, "backup_server", chosen.backup_id)
        return chosen

    # ------------------------------------------------------------------ #
    # Scheduler
    # ------------------------------------------------------------------ #
    async def get_task(self, server_id: str, task_id: str) -> ScheduledTask:
        """Read one scheduled task. Requires the SCHEDULE API permission."""
        body = await self._request("GET", f"{API}/servers/{server_id}/tasks/{task_id}")
        data = self._payload(body)
        if not isinstance(data, Mapping):
            raise CraftyNotFound("That scheduled task does not exist.")
        return ScheduledTask(
            task_id=str(data.get("schedule_id") or task_id),
            name=_as_text(data.get("name")) or "",
            action=_as_text(data.get("action")) or _as_text(data.get("command")) or "",
            enabled=bool(data.get("enabled", True)),
            interval=" ".join(
                str(part)
                for part in (data.get("interval"), data.get("interval_type"))
                if not _falsey_string(part)
            ),
            cron=_as_text(data.get("cron_string")) or "",
            next_run=_as_text(data.get("next_run")) or "",
            raw=data,
        )

    async def run_task(self, server_id: str, task_id: str, *, cascade: bool = False) -> None:
        """Run a scheduled task immediately, optionally cascading its task chain."""
        await self._request(
            "POST",
            f"{API}/servers/{server_id}/tasks/{task_id}/run",
            json_body={"cascade": cascade},
            retries=0,
        )
        logger.info("Scheduled task %s triggered for server %s", task_id, server_id)

    # ------------------------------------------------------------------ #
    # Server files
    # ------------------------------------------------------------------ #
    async def read_file(self, server_id: str, path: str) -> str:
        """Read one text file from a server directory.

        Crafty exposes this as a ``POST`` because its GET handlers take no body;
        ``path`` is relative to the server root and Crafty rejects any attempt
        to escape it. Requires the FILES API permission.
        """
        try:
            body = await self._request(
                "POST",
                f"{API}/servers/{server_id}/files",
                json_body={"path": path},
                retries=0,
            )
        except CraftyAPIError as exc:
            # Crafty answers 400 DECODE_ERROR both for a file that is not there
            # and for one it cannot read as UTF-8; "missing" is by far the more
            # common case and the only one worth a tailored message.
            if exc.code == "DECODE_ERROR":
                raise CraftyNotFound(
                    f"Crafty could not read `{path}` — it may not exist yet."
                ) from exc
            raise
        data = self._payload(body)
        if not isinstance(data, Mapping):
            raise CraftyNotFound(f"Crafty returned no contents for `{path}`.")
        if "content" not in data:
            # Directory listings come back keyed by filename, with `root_path`.
            raise CraftyAPIError(f"`{path}` is a directory, not a file.")
        return str(data.get("content") or "")

    async def read_properties(self, server_id: str) -> dict[str, str]:
        """Parse ``server.properties`` into an ordered mapping."""
        text = await self.read_file(server_id, "server.properties")
        properties: dict[str, str] = {}
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            properties[key.strip()] = value.strip()
        return properties

    async def read_player_list(self, server_id: str, filename: str) -> tuple[Player, ...]:
        """Read one of Minecraft's JSON player lists (whitelist/ops/bans)."""
        text = await self.read_file(server_id, filename)
        try:
            entries = json.loads(text or "[]")
        except ValueError as exc:
            raise CraftyAPIError(f"`{filename}` is not valid JSON.") from exc
        if not isinstance(entries, Sequence) or isinstance(entries, (str, bytes)):
            return ()
        players: list[Player] = []
        for entry in entries:
            if not isinstance(entry, Mapping):
                continue
            name = _as_text(entry.get("name")) or _as_text(entry.get("uuid"))
            if name:
                players.append(Player(name=name, uuid=_as_text(entry.get("uuid"))))
        return tuple(players)

    # ------------------------------------------------------------------ #
    # Webhooks
    # ------------------------------------------------------------------ #
    async def list_webhooks(self, server_id: str) -> tuple[Webhook, ...]:
        """List the webhooks Crafty fires for this server (CONFIG permission)."""
        body = await self._request("GET", f"{API}/servers/{server_id}/webhook")
        data = self._payload(body)
        if not isinstance(data, Mapping):
            return ()
        return tuple(
            Webhook(
                webhook_id=str(webhook_id),
                name=_as_text(entry.get("name")) or "",
                provider=str(entry.get("webhook_type") or "Discord"),
                url=str(entry.get("url") or ""),
                bot_name=_as_text(entry.get("bot_name")) or "",
                triggers=tuple(
                    part.strip()
                    for part in str(entry.get("trigger") or "").split(",")
                    if part.strip()
                ),
                body=str(entry.get("body") or ""),
                color=str(entry.get("color") or "#005cd1"),
                enabled=bool(entry.get("enabled", True)),
            )
            for webhook_id, entry in data.items()
            if isinstance(entry, Mapping)
        )

    async def create_webhook(
        self,
        server_id: str,
        *,
        name: str,
        url: str,
        triggers: Sequence[str],
        body: str = "",
        bot_name: str = "Crafty Controller",
        color: str = "#005cd1",
        enabled: bool = True,
        provider: str = "Discord",
    ) -> str:
        """Register a webhook and return its id.

        Every field is sent because Crafty's schema demands at least seven of
        the eight properties and rejects anything it does not know about.
        """
        unknown = [event for event in triggers if event not in WEBHOOK_EVENTS]
        if unknown:
            raise CraftyAPIError(f"Unknown webhook event: `{unknown[0]}`.")
        if not triggers:
            raise CraftyAPIError("A webhook needs at least one event to react to.")

        payload = {
            "webhook_type": provider,
            "name": name,
            "url": url,
            "bot_name": bot_name,
            "trigger": list(triggers),
            "body": body,
            "color": color,
            "enabled": enabled,
        }
        response = await self._request(
            "POST", f"{API}/servers/{server_id}/webhook", json_body=payload, retries=0
        )
        data = self._payload(response)
        webhook_id = data.get("webhook_id") if isinstance(data, Mapping) else None
        logger.info("Webhook created for server %s", server_id)
        return str(webhook_id) if webhook_id is not None else ""

    async def set_webhook_enabled(
        self, server_id: str, webhook_id: str, enabled: bool
    ) -> None:
        """Enable or disable a webhook without deleting it."""
        await self._request(
            "PATCH",
            f"{API}/servers/{server_id}/webhook/{webhook_id}",
            json_body={"enabled": enabled},
            retries=0,
        )

    async def delete_webhook(self, server_id: str, webhook_id: str) -> None:
        await self._request(
            "DELETE", f"{API}/servers/{server_id}/webhook/{webhook_id}", retries=0
        )
        logger.info("Webhook %s deleted from server %s", webhook_id, server_id)

    async def test_webhook(self, server_id: str, webhook_id: str) -> None:
        """Ask Crafty to fire a sample event through this webhook."""
        await self._request(
            "POST", f"{API}/servers/{server_id}/webhook/{webhook_id}", retries=0
        )

    # ------------------------------------------------------------------ #
    # Scheduler (writes)
    # ------------------------------------------------------------------ #
    async def create_task(
        self,
        server_id: str,
        *,
        name: str,
        action: str,
        cron: str = "",
        interval: int = 0,
        interval_type: str = "",
        start_time: str = "00:00",
        command: str | None = None,
        action_id: str | None = None,
        enabled: bool = True,
        one_time: bool = False,
    ) -> str:
        """Create a scheduled task and return its id.

        ``action`` is one of :data:`TASK_ACTIONS`. Either ``cron`` or
        ``interval``/``interval_type`` describes when the task runs; Crafty
        validates the cron string itself and answers HTTP 405 when it is
        malformed. A ``backup`` task needs the ``action_id`` of a backup
        configuration.
        """
        if action not in TASK_ACTIONS:
            raise CraftyAPIError(f"`{action}` is not a supported task action.")
        if action == "command":
            payload_command = (command or "").strip().lstrip("/")
            if not payload_command:
                raise CraftyAPIError("A `command` task needs a console command.")
        else:
            # Mirror Crafty's own front-end: the scheduler dispatches `command`.
            payload_command = f"{action}_server"
        if action == "backup" and not action_id:
            raise CraftyAPIError("A `backup` task needs a backup configuration id.")
        if not cron and not interval:
            raise CraftyAPIError("A task needs either a cron string or an interval.")

        payload: dict[str, Any] = {
            "name": name,
            "action": action,
            "enabled": enabled,
            "one_time": one_time,
            "start_time": start_time,
            "cron_string": cron,
            "interval": interval,
            "interval_type": interval_type,
            "command": payload_command,
        }
        if action_id:
            payload["action_id"] = action_id

        response = await self._request(
            "POST", f"{API}/servers/{server_id}/tasks", json_body=payload, retries=0
        )
        data = self._payload(response)
        task_id = data.get("schedule_id") if isinstance(data, Mapping) else None
        logger.info("Scheduled task created for server %s", server_id)
        return str(task_id) if task_id is not None else ""

    async def set_task_enabled(self, server_id: str, task_id: str, enabled: bool) -> None:
        """Pause or resume a scheduled task."""
        await self._request(
            "PATCH",
            f"{API}/servers/{server_id}/tasks/{task_id}",
            json_body={"enabled": enabled},
            retries=0,
        )

    async def delete_task(self, server_id: str, task_id: str) -> None:
        await self._request(
            "DELETE", f"{API}/servers/{server_id}/tasks/{task_id}", retries=0
        )
        logger.info("Scheduled task %s deleted from server %s", task_id, server_id)
