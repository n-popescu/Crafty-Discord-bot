"""Permission tiers, caching, formatting helpers and embed rendering."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from bot.cache import TTLCache
from bot.config import PermissionConfig
from bot.errors import CraftyUnavailable
from bot.permissions import PermissionChecker, Tier
from bot.services.azure import VmStatus
from bot.services.crafty import (
    BackupConfig,
    HistorySample,
    HostStats,
    Player,
    ServerStats,
    ServerStatusLine,
    Webhook,
)
from bot.services.orchestrator import InfraSnapshot, Step, StepState
from bot.services.timeout import TimeoutState
from bot.ui import embeds
from bot.utils import human_bytes, human_duration, parse_timestamp, poll_until


# --------------------------------------------------------------------------- #
# Permissions
# --------------------------------------------------------------------------- #
def make_member(user_id: int, role_ids=(), *, administrator=False, owner=False):
    member = MagicMock(spec=discord.Member)
    member.id = user_id
    member.roles = [MagicMock(id=role_id) for role_id in role_ids]
    member.guild_permissions = MagicMock(administrator=administrator)
    member.guild = MagicMock(owner_id=user_id if owner else 0)
    return member


def make_user(user_id: int):
    user = MagicMock(spec=discord.User)
    user.id = user_id
    return user


def test_everyone_tier_is_open_to_all():
    checker = PermissionChecker(PermissionConfig())
    assert checker.allows(make_user(1), Tier.EVERYONE) is True


def test_control_tiers_are_admin_only_by_default():
    """A fresh install must not hand the server to every member."""
    checker = PermissionChecker(PermissionConfig())
    member = make_member(1, [10])
    assert checker.allows(member, Tier.SERVER) is False
    assert checker.allows(member, Tier.AZURE) is False
    assert checker.allows(member, Tier.ADMIN) is False


def test_admin_user_ids_pass_every_tier():
    checker = PermissionChecker(PermissionConfig(admin_user_ids=frozenset({7})))
    admin = make_member(7)
    for tier in Tier:
        assert checker.allows(admin, tier) is True


def test_admin_role_and_guild_administrator_pass():
    checker = PermissionChecker(PermissionConfig(admin_role_ids=frozenset({99})))
    assert checker.allows(make_member(1, [99]), Tier.ADMIN) is True
    assert checker.allows(make_member(2, [], administrator=True), Tier.ADMIN) is True
    assert checker.allows(make_member(3, [], owner=True), Tier.ADMIN) is True


def test_role_based_tiers_are_independent():
    checker = PermissionChecker(
        PermissionConfig(
            server_control_role_ids=frozenset({11}),
            azure_control_role_ids=frozenset({22}),
        )
    )
    server_manager = make_member(1, [11])
    azure_operator = make_member(2, [22])

    assert checker.allows(server_manager, Tier.SERVER) is True
    assert checker.allows(server_manager, Tier.AZURE) is False
    assert checker.allows(azure_operator, Tier.AZURE) is True
    assert checker.allows(azure_operator, Tier.SERVER) is False
    assert checker.allows(azure_operator, Tier.ADMIN) is False


def test_plain_users_without_roles_cannot_control():
    checker = PermissionChecker(PermissionConfig(server_control_role_ids=frozenset({11})))
    assert checker.allows(make_user(5), Tier.SERVER) is False


def test_guild_restriction():
    checker = PermissionChecker(PermissionConfig(), guild_id=4242)
    assert checker.guild_allowed(4242) is True
    assert checker.guild_allowed(1) is False
    assert checker.guild_allowed(None) is False
    assert PermissionChecker(PermissionConfig()).guild_allowed(None) is True


def test_denial_reasons_are_specific():
    checker = PermissionChecker(PermissionConfig(), guild_id=4242)
    member = make_member(1)
    assert "configured Discord server" in checker.denial_reason(member, Tier.EVERYONE, 7)
    assert checker.denial_reason(member, Tier.EVERYONE, 4242) is None
    assert "manager role" in checker.denial_reason(member, Tier.SERVER, 4242)
    assert "Azure operator" in checker.denial_reason(member, Tier.AZURE, 4242)
    assert "administrators" in checker.denial_reason(member, Tier.ADMIN, 4242)


# --------------------------------------------------------------------------- #
# Cache
# --------------------------------------------------------------------------- #
async def test_cache_returns_cached_values_until_they_expire():
    cache = TTLCache()
    calls = 0

    async def factory():
        nonlocal calls
        calls += 1
        return calls

    assert await cache.get_or_fetch("k", 60, factory) == 1
    assert await cache.get_or_fetch("k", 60, factory) == 1
    assert calls == 1

    cache.invalidate("k")
    assert await cache.get_or_fetch("k", 60, factory) == 2


async def test_zero_ttl_disables_caching():
    cache = TTLCache()
    calls = 0

    async def factory():
        nonlocal calls
        calls += 1
        return calls

    await cache.get_or_fetch("k", 0, factory)
    await cache.get_or_fetch("k", 0, factory)
    assert calls == 2


async def test_concurrent_callers_share_one_request():
    cache = TTLCache()
    calls = 0

    async def factory():
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.01)
        return "value"

    results = await asyncio.gather(
        *(cache.get_or_fetch("k", 60, factory) for _ in range(5))
    )
    assert results == ["value"] * 5
    assert calls == 1


def test_prefix_invalidation():
    cache = TTLCache()
    cache.set("stats:a", 1, 60)
    cache.set("stats:b", 2, 60)
    cache.set("servers", 3, 60)
    cache.invalidate_prefix("stats:")
    assert cache.get("stats:a") is None
    assert cache.get("servers") == 3


# --------------------------------------------------------------------------- #
# Formatting helpers
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, "N/A"),
        (-1, "N/A"),
        (512, "512 B"),
        (1024, "1.0 KB"),
        (4831838208, "4.5 GB"),
        ("bad", "N/A"),
    ],
)
def test_human_bytes(value, expected):
    assert human_bytes(value) == expected


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [(None, "N/A"), (45, "45s"), (600, "10m"), (13320, "3h 42m"), (180000, "2d 2h")],
)
def test_human_duration(seconds, expected):
    assert human_duration(seconds) == expected


def test_parse_timestamp_handles_crafty_formats():
    assert parse_timestamp("2026-08-20 18:00:00") == datetime(2026, 8, 20, 18, 0, 0)
    assert parse_timestamp("2026/08/20, 18:00:00") == datetime(2026, 8, 20, 18, 0, 0)
    assert parse_timestamp("") is None
    assert parse_timestamp("False") is None
    assert parse_timestamp(False) is None


async def test_poll_until_returns_early_and_times_out():
    attempts = 0

    async def eventually_true():
        nonlocal attempts
        attempts += 1
        return attempts >= 2

    assert await poll_until(eventually_true, timeout=2, initial_interval=0.01) is True

    async def never():
        return False

    assert await poll_until(never, timeout=0.05, initial_interval=0.01) is None


# --------------------------------------------------------------------------- #
# Embeds
# --------------------------------------------------------------------------- #
def running_stats() -> ServerStats:
    return ServerStats(
        server_id="s1",
        name="Survival",
        running=True,
        online=3,
        max_players=20,
        players=(Player("PlayerOne", "uuid-1"), Player("PlayerTwo")),
        version="Paper 1.21.4",
        world_name="Survival",
        world_size="10.3GB",
        cpu_percent=23.0,
        memory_bytes=4831838208,
        memory_percent=60.0,
        port=25565,
        started_at=datetime.now() - timedelta(hours=3, minutes=42),
    )


def test_status_embed_shows_both_layers():
    snapshot = InfraSnapshot(
        vm=VmStatus(name="mc-vm", power_state="running", location="westeurope"),
        stats=running_stats(),
        server_id="s1",
    )
    embed = embeds.status_embed(snapshot, HostStats(memory_total="8.0GB"))
    rendered = embed.to_dict()
    text = str(rendered)

    assert "Survival" in text
    assert "Azure VM" in text
    assert "3" in text and "20" in text
    assert "PlayerOne" in text
    # Embeds must stay inside Discord's limits.
    assert len(embed.fields) <= 25


def test_status_embed_degrades_when_crafty_is_down():
    snapshot = InfraSnapshot(
        vm=VmStatus(name="mc-vm", power_state="running"),
        crafty_error=CraftyUnavailable(),
    )
    embed = embeds.status_embed(snapshot)
    assert "unreachable" in str(embed.to_dict()).lower()


def test_server_embed_uses_na_for_unknown_values():
    embed = embeds.server_embed(ServerStats(server_id="s1", name="Survival"))
    values = [field.value for field in embed.fields]
    assert embeds.NA in values


def test_players_embed_lists_names_and_uuids():
    embed = embeds.players_embed(running_stats())
    text = str(embed.to_dict())
    assert "PlayerOne" in text
    assert "uuid-1" in text


def test_players_embed_warns_when_offline():
    embed = embeds.players_embed(ServerStats(server_id="s1", running=False))
    assert "offline" in embed.title.lower()


def test_logs_embed_truncates_long_output():
    lines = [f"line {i} " + "x" * 100 for i in range(100)]
    embed = embeds.logs_embed(lines, source="console buffer", server_name="Survival")
    assert len(embed.description) < 4096
    assert embed.description.startswith("```")


def test_backups_embed_handles_empty_and_populated():
    empty = embeds.backups_embed([], "Survival")
    assert "No backup configuration" in empty.description

    populated = embeds.backups_embed(
        [BackupConfig(backup_id="b-1", name="Nightly", default=True, compress=True)],
        "Survival",
    )
    assert "Nightly" in str(populated.to_dict())


def test_azure_embed_hides_absent_addresses():
    embed = embeds.azure_embed(VmStatus(name="mc-vm", power_state="deallocated"))
    assert "Public IP" not in str(embed.to_dict())


def test_workflow_embed_reflects_step_states():
    steps = [
        Step("vm", "Azure VM", StepState.DONE, "Running"),
        Step("crafty", "Crafty Controller", StepState.ACTIVE, "Waiting…"),
        Step("minecraft", "Minecraft server", StepState.PENDING),
    ]
    embed = embeds.workflow_embed("Starting", steps)
    assert "Azure VM" in embed.description
    assert embeds.STEP_EMOJI[StepState.DONE] in embed.description

    failed = embeds.workflow_embed("Starting", steps, failed=True)
    assert failed.colour == embeds.COLOR_ERROR


def test_health_embed_marks_unconfigured_azure():
    embed = embeds.health_embed(
        discord_ok=True,
        crafty_reachable=True,
        crafty_authenticated=True,
        azure_state="⚪ Not configured",
        minecraft_state="🟢 running",
        latency_ms=42.0,
    )
    text = str(embed.to_dict())
    assert "Not configured" in text
    assert "42" in text


# --------------------------------------------------------------------------- #
# New embeds
# --------------------------------------------------------------------------- #
def test_servers_overview_counts_running_servers_and_players():
    lines = [
        ServerStatusLine(
            server_id="a", world_name="Survival", running=True, online=3, max_players=20
        ),
        ServerStatusLine(server_id="b", world_name="Creative", running=False),
    ]
    embed = embeds.servers_overview_embed(lines)
    assert "1/2 up" in embed.title
    assert "3 players online in total" in embed.footer.text


def test_servers_overview_explains_an_empty_list():
    embed = embeds.servers_overview_embed([])
    assert "Show status" in embed.description


def test_history_embed_draws_one_chart_per_metric():
    samples = [
        HistorySample(
            at=datetime(2026, 8, 20, 21, 0), cpu_percent=10.0, memory_percent=50.0, online=1
        ),
        HistorySample(
            at=datetime(2026, 8, 20, 21, 10), cpu_percent=30.0, memory_percent=60.0, online=4
        ),
    ]
    embed = embeds.history_embed(samples, "Survival")
    names = [field.name for field in embed.fields]
    assert any("CPU" in name for name in names)
    assert any("RAM" in name for name in names)
    assert any("Players" in name for name in names)
    # Latest value first, peak second.
    assert "now 30%" in names[0] and "peak 30%" in names[0]
    assert "21:00–21:10" in embed.footer.text


def test_history_embed_handles_no_samples():
    embed = embeds.history_embed([], "Survival")
    assert "No history" in embed.title


def test_history_embed_skips_a_metric_crafty_never_recorded():
    samples = [HistorySample(at=datetime(2026, 8, 20, 21, 0), cpu_percent=10.0)]
    names = [field.name for field in embeds.history_embed(samples, "Survival").fields]
    assert not any("RAM" in name for name in names)


def test_properties_embed_highlights_the_interesting_keys():
    embed = embeds.properties_embed(
        {"motd": "Hello", "difficulty": "hard", "enable-rcon": "false"}, "Survival"
    )
    names = [field.name for field in embed.fields]
    assert "motd" in names and "difficulty" in names
    # Everything else is folded into a single block.
    assert any(name.startswith("Other") for name in names)


def test_player_list_embed_explains_an_empty_list():
    embed = embeds.player_list_embed(
        "✅ Whitelist", [], "Survival", empty_hint="nobody has been whitelisted yet."
    )
    assert "whitelisted" in embed.description


def test_player_list_embed_truncates_a_long_list():
    players = [Player(name=f"Player{i}") for i in range(60)]
    embed = embeds.player_list_embed(
        "🚫 Banned players", players, "Survival", empty_hint="nobody is banned."
    )
    assert "and 20 more" in embed.description
    assert "60 entries" in embed.footer.text


def test_webhooks_embed_never_prints_the_url():
    """A webhook URL lets anyone post into the channel: it is a credential."""
    webhook = Webhook(
        webhook_id="3",
        name="Lifecycle",
        url="https://discord.com/api/webhooks/1/super-secret",
        triggers=("start_server", "stop_server"),
    )
    embed = embeds.webhooks_embed([webhook], "Survival")
    rendered = repr(embed.to_dict())
    assert "super-secret" not in rendered
    assert "start_server" in rendered


def test_webhooks_embed_pitches_the_feature_when_none_exist():
    embed = embeds.webhooks_embed([], "Survival")
    assert "no polling" in embed.description


def test_uptime_offset_reaches_the_status_embed():
    started = datetime.now() + timedelta(hours=2) - timedelta(hours=1)
    stats = ServerStats(server_id="a", name="Survival", running=True, started_at=started)
    snapshot = InfraSnapshot(server_id="a", stats=stats)
    embed = embeds.status_embed(snapshot, clock_offset=2)
    assert "1h 0m" in embed.fields[0].value


# --------------------------------------------------------------------------- #
# Inactivity timeout
# --------------------------------------------------------------------------- #
def armed(
    minutes: int = 90,
    *,
    empty: bool = False,
    shutdown_vm: bool = True,
    online=None,
    running=None,
    stalled: bool = False,
    idle_seconds: float = 0.0,
):
    return TimeoutState(
        server_id="a",
        minutes=minutes,
        shutdown_vm=shutdown_vm,
        armed_by=7,
        counting=empty,
        idle_seconds=idle_seconds,
        stalled=stalled,
        last_online=online,
        # `None` means the watcher has not checked yet; a player count implies
        # it has, and that the server was up.
        last_running=running if running is not None else (None if online is None else True),
    )


def test_timeout_line_says_off_when_nothing_is_armed():
    assert "off" in embeds.timeout_line(None).lower()


def test_timeout_line_shows_a_relative_deadline_while_counting():
    line = embeds.timeout_line(armed(90, empty=True))
    assert "armed" in line.lower()
    # Discord renders <t:unix:R> as a live "in 90 minutes".
    assert ":R>" in line


def test_timeout_line_reports_a_paused_countdown():
    line = embeds.timeout_line(armed(90, online=3))
    assert "paused" in line
    assert "3 players online" in line


def test_timeout_line_says_when_the_server_is_stopped():
    """A stopped server does not count down, so say so rather than "0 players"."""
    line = embeds.timeout_line(armed(90, online=0, running=False))
    assert "the server is stopped" in line


def test_timeout_line_says_when_nothing_has_been_checked_yet():
    line = embeds.timeout_line(armed(90))
    assert "waiting for the first check" in line


def test_timeout_line_says_whether_the_vm_is_included():
    assert "server + VM" in embeds.timeout_line(armed(shutdown_vm=True))
    assert "server + VM" not in embeds.timeout_line(armed(shutdown_vm=False))


def test_timeout_embed_explains_the_feature_when_disarmed():
    embed = embeds.timeout_embed(None, "Survival")
    assert "/timeout minutes:90" in embed.description


def test_timeout_embed_describes_a_graceful_stop():
    embed = embeds.timeout_embed(armed(90, empty=True), "Survival")
    assert "gracefully through Crafty" in embed.description
    assert "90 minutes" in embed.description
    assert any("Armed by" == field.name for field in embed.fields)


def test_timeout_embed_surfaces_the_last_shutdown():
    embed = embeds.timeout_embed(None, "Survival", last_result="Stopped after 90 min.")
    assert any("Last shutdown" == field.name for field in embed.fields)


def test_status_embed_always_reports_the_timeout():
    stats = ServerStats(server_id="a", name="Survival", running=True)
    snapshot = InfraSnapshot(server_id="a", stats=stats)

    off = embeds.status_embed(snapshot)
    assert any("Auto-shutdown" in field.name for field in off.fields)
    assert "off" in next(f.value for f in off.fields if "Auto-shutdown" in f.name).lower()

    on = embeds.status_embed(snapshot, timeout=armed(90, empty=True))
    assert "armed" in next(f.value for f in on.fields if "Auto-shutdown" in f.name).lower()


def test_server_embed_reports_the_timeout():
    stats = ServerStats(server_id="a", name="Survival", running=True)
    embed = embeds.server_embed(stats, timeout=armed(45, empty=True))
    value = next(f.value for f in embed.fields if "Auto-shutdown" in f.name)
    assert "45 min idle" in value


def test_health_embed_reports_the_timeout():
    embed = embeds.health_embed(
        discord_ok=True,
        crafty_reachable=True,
        crafty_authenticated=True,
        azure_state="🟢 running",
        minecraft_state="🟢 running",
        timeout=armed(30, empty=True),
    )
    assert any("Auto-shutdown" in field.name for field in embed.fields)


def test_players_embed_shows_the_countdown_when_the_server_just_emptied():
    stats = ServerStats(server_id="a", name="Survival", running=True, online=0)
    embed = embeds.players_embed(stats, armed(20, empty=True))
    assert any("Auto-shutdown" in field.name for field in embed.fields)


def test_players_embed_omits_the_field_when_nothing_is_armed():
    stats = ServerStats(server_id="a", name="Survival", running=True, online=0)
    embed = embeds.players_embed(stats, None)
    assert not any("Auto-shutdown" in field.name for field in embed.fields)


def test_servers_overview_marks_servers_with_a_timer():
    lines = [
        ServerStatusLine(server_id="a", world_name="Survival", running=True, online=0),
        ServerStatusLine(server_id="b", world_name="Creative", running=True, online=0),
    ]
    embed = embeds.servers_overview_embed(lines, {"a": armed(90, empty=True)})
    values = {field.name: field.value for field in embed.fields}
    assert "⏱️" in values["🎮 Survival"]
    assert "⏱️" not in values["🎮 Creative"]


# --------------------------------------------------------------------------- #
# The switch
# --------------------------------------------------------------------------- #
def _status_view(*, armed_state: bool, may_control: bool = True, toggle=True):
    from bot.ui.views import StatusView

    async def noop(_interaction):
        return None

    return StatusView(
        checker=PermissionChecker(PermissionConfig()),
        owner_id=1,
        refresh=noop,
        start=noop,
        stop=noop,
        restart=noop,
        running=True,
        may_control=may_control,
        timeout_armed=armed_state,
        toggle_timeout=noop if toggle else None,
    )


def test_the_switch_reads_on_when_a_timeout_is_armed():
    view = _status_view(armed_state=True)
    button = next(c for c in view.children if "Auto-shutdown" in (c.label or ""))
    assert button.label == "Auto-shutdown: ON"
    assert button.style is discord.ButtonStyle.success


def test_the_switch_reads_off_when_nothing_is_armed():
    view = _status_view(armed_state=False)
    button = next(c for c in view.children if "Auto-shutdown" in (c.label or ""))
    assert button.label == "Auto-shutdown: OFF"
    assert button.style is discord.ButtonStyle.secondary


def test_the_switch_is_hidden_from_users_who_may_not_control_the_server():
    view = _status_view(armed_state=True, may_control=False)
    assert not any("Auto-shutdown" in (c.label or "") for c in view.children)


def test_the_switch_is_hidden_when_no_server_could_be_resolved():
    view = _status_view(armed_state=False, toggle=False)
    assert not any("Auto-shutdown" in (c.label or "") for c in view.children)


async def test_the_modal_rejects_a_delay_that_is_not_a_number():
    from bot.ui.views import TimeoutModal

    submitted = []

    async def on_submit(_interaction, minutes):
        submitted.append(minutes)

    modal = TimeoutModal(on_submit=on_submit, max_minutes=1440)
    interaction = MagicMock()
    interaction.response.send_message = AsyncMock()

    for bad in ("abc", "0", "99999", ""):
        modal.minutes._value = bad
        await modal.on_submit(interaction)
    assert submitted == []
    assert interaction.response.send_message.await_count == 4


async def test_the_modal_passes_a_valid_delay_through():
    from bot.ui.views import TimeoutModal

    submitted = []

    async def on_submit(_interaction, minutes):
        submitted.append(minutes)

    modal = TimeoutModal(on_submit=on_submit, max_minutes=1440, default=30)
    assert modal.minutes.default == "30"
    modal.minutes._value = " 90 "
    await modal.on_submit(MagicMock())
    assert submitted == [90]
