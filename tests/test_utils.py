"""Formatting helpers, clock-skew handling and the TTL cache."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

from bot.cache import TTLCache
from bot.utils import clock_offset_hours, sparkline, uptime_since


# --------------------------------------------------------------------------- #
# Uptime across time zones
# --------------------------------------------------------------------------- #
def test_uptime_uses_the_local_clock_by_default():
    started = datetime.now() - timedelta(hours=3, minutes=42)
    assert uptime_since(started) == "3h 42m"


def test_uptime_corrects_for_a_crafty_host_in_another_time_zone():
    """Crafty timestamps are naive local time *on the Crafty host*.

    A host two hours ahead of the bot would otherwise look two hours less
    up than it is.
    """
    started = datetime.now() + timedelta(hours=2) - timedelta(hours=1)
    assert uptime_since(started, offset_hours=2) == "1h 0m"


def test_uptime_reports_a_future_start_as_unknown():
    """A start time in the future means the clocks disagree; do not invent one."""
    assert uptime_since(datetime.now() + timedelta(hours=5)) == "N/A"


def test_uptime_of_nothing_is_not_available():
    assert uptime_since(None) == "N/A"


def test_clock_offset_is_zero_when_unconfigured():
    assert clock_offset_hours(None) == 0.0


def test_clock_offset_is_relative_to_the_bot():
    local = datetime.now().astimezone().utcoffset()
    local_hours = local.total_seconds() / 3600 if local else 0.0
    assert clock_offset_hours(local_hours + 2) == 2.0


# --------------------------------------------------------------------------- #
# Sparklines
# --------------------------------------------------------------------------- #
def test_sparkline_spans_the_full_block_range():
    assert sparkline([0, 1, 2, 3, 4, 5, 6, 7]) == "▁▂▃▄▅▆▇█"


def test_a_flat_series_is_drawn_flat():
    assert sparkline([5, 5, 5, 5]) == "▁▁▁▁"


def test_sparkline_leaves_gaps_for_missing_samples():
    assert sparkline([None, 0, None, 10]) == " ▁ █"


def test_sparkline_downsamples_to_the_requested_width():
    assert len(sparkline(list(range(500)), width=24)) == 24


def test_an_empty_series_draws_nothing():
    assert sparkline([]) == ""


# --------------------------------------------------------------------------- #
# Cache
# --------------------------------------------------------------------------- #
async def test_concurrent_callers_share_one_fetch():
    cache = TTLCache()
    calls = 0

    async def fetch():
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.01)
        return "value"

    results = await asyncio.gather(
        *(cache.get_or_fetch("key", 10.0, fetch) for _ in range(5))
    )
    assert results == ["value"] * 5
    assert calls == 1


async def test_locks_do_not_accumulate():
    """Keys embed server ids, so a leaked lock per key would grow forever."""
    cache = TTLCache()

    async def fetch():
        return 1

    for index in range(50):
        await cache.get_or_fetch(f"stats:{index}", 0.0, fetch)
    assert cache._locks == {}
    assert cache._waiters == {}


async def test_a_lock_survives_while_others_still_want_it():
    """Dropping the lock too early would let a second fetch start in parallel."""
    cache = TTLCache()
    calls = 0
    release = asyncio.Event()

    async def fetch():
        nonlocal calls
        calls += 1
        await release.wait()
        return "value"

    tasks = [
        asyncio.create_task(cache.get_or_fetch("key", 10.0, fetch)) for _ in range(3)
    ]
    await asyncio.sleep(0)
    release.set()
    assert await asyncio.gather(*tasks) == ["value"] * 3
    assert calls == 1
    assert cache._locks == {}
