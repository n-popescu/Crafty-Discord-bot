"""Shared helpers: logging setup, human formatting and backoff polling."""

from __future__ import annotations

import asyncio
import logging
import re
import sys
import time
from datetime import datetime, timedelta
from typing import Awaitable, Callable, Sequence, TypeVar

T = TypeVar("T")

_LOG_FORMAT = "%(asctime)s %(levelname)-8s %(name)-22s %(message)s"
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

# Values that must never reach the logs, even by accident.
_SECRET_PATTERNS = (
    re.compile(r"(Bearer\s+)[A-Za-z0-9._\-]+", re.IGNORECASE),
    re.compile(r"([?&](?:token|code|client_secret|access_token)=)[^&\s]+", re.IGNORECASE),
)


class SecretScrubbingFilter(logging.Filter):
    """Redact bearer tokens and secret query parameters from log records."""

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        scrubbed = message
        for pattern in _SECRET_PATTERNS:
            scrubbed = pattern.sub(r"\1[REDACTED]", scrubbed)
        if scrubbed != message:
            record.msg = scrubbed
            record.args = ()
        return True


def setup_logging(level: str = "INFO") -> None:
    """Configure root logging once, with secret scrubbing enabled."""
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter(_LOG_FORMAT, datefmt=_DATE_FORMAT))
    handler.addFilter(SecretScrubbingFilter())

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(getattr(logging, level, logging.INFO))

    # discord.py is chatty at INFO; keep the bot's own logs readable.
    logging.getLogger("discord").setLevel(logging.WARNING)
    logging.getLogger("discord.http").setLevel(logging.WARNING)


# --------------------------------------------------------------------------- #
# Formatting
# --------------------------------------------------------------------------- #
def human_bytes(value: float | int | None) -> str:
    """Format a byte count such as ``4831838208`` as ``4.5 GB``."""
    if value is None:
        return "N/A"
    try:
        size = float(value)
    except (TypeError, ValueError):
        return "N/A"
    if size < 0:
        return "N/A"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            precision = 0 if unit == "B" else 1
            return f"{size:.{precision}f} {unit}"
        size /= 1024
    return "N/A"


def human_duration(seconds: float | int | None) -> str:
    """Format a duration in seconds as ``3h 42m``."""
    if seconds is None:
        return "N/A"
    total = int(seconds)
    if total < 0:
        return "N/A"
    if total < 60:
        return f"{total}s"
    minutes, _ = divmod(total, 60)
    hours, minutes = divmod(minutes, 60)
    days, hours = divmod(hours, 24)
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def uptime_since(started: datetime | None, *, offset_hours: float = 0.0) -> str:
    """Format the elapsed time since ``started``.

    Crafty reports naive local timestamps from *its* host. When the bot's Pi and
    the Crafty host sit in different time zones, comparing them directly inflates
    or negates the uptime, so ``offset_hours`` (``CRAFTY_UTC_OFFSET`` minus the
    Pi's own offset) shifts the Crafty timestamp onto the bot's clock. A result
    that is still in the future means the clocks disagree, which is reported as
    unknown rather than as a wrong number.
    """
    if started is None:
        return "N/A"
    if offset_hours:
        started = started - timedelta(hours=offset_hours)
    now = datetime.now(started.tzinfo) if started.tzinfo else datetime.now()
    elapsed = (now - started).total_seconds()
    if elapsed < 0:
        return "N/A"
    return human_duration(elapsed)


def clock_offset_hours(crafty_utc_offset: float | None) -> float:
    """Hours to subtract from a Crafty timestamp to express it on the bot's clock.

    Returns ``0.0`` when ``CRAFTY_UTC_OFFSET`` is unset, which keeps the old
    behaviour of assuming both machines share a time zone.
    """
    if crafty_utc_offset is None:
        return 0.0
    local = datetime.now().astimezone().utcoffset()
    local_hours = local.total_seconds() / 3600 if local else 0.0
    return crafty_utc_offset - local_hours


#: Eight block heights, used to draw a graph as plain text. Rendering an image
#: would mean pulling matplotlib onto a Pi Zero W; this costs nothing.
_SPARK_BLOCKS = "▁▂▃▄▅▆▇█"


def sparkline(values: Sequence[float | int | None], *, width: int = 24) -> str:
    """Render a series as a one-line Unicode bar chart.

    Values are down-sampled to ``width`` buckets by averaging, and ``None``
    entries (a metric Crafty could not read) are drawn as a gap.
    """
    numbers = [None if value is None else float(value) for value in values]
    if not numbers:
        return ""

    if len(numbers) > width:
        bucket = len(numbers) / width
        buckets: list[float | None] = []
        for index in range(width):
            start = int(index * bucket)
            chunk = numbers[start : max(int((index + 1) * bucket), start + 1)]
            present = [value for value in chunk if value is not None]
            buckets.append(sum(present) / len(present) if present else None)
        numbers = buckets

    present = [value for value in numbers if value is not None]
    if not present:
        return " " * len(numbers)
    low, high = min(present), max(present)
    span = high - low

    out = []
    for value in numbers:
        if value is None:
            out.append(" ")
            continue
        # A flat series should read as a flat line, not as noise.
        level = 0 if span <= 0 else round((value - low) / span * (len(_SPARK_BLOCKS) - 1))
        out.append(_SPARK_BLOCKS[level])
    return "".join(out)


def parse_timestamp(raw: object) -> datetime | None:
    """Parse the loosely-typed timestamps Crafty reports (``"2026-05-25 15:44:05"``)."""
    if not raw or not isinstance(raw, str) or raw.lower() in {"false", "none", "n/a"}:
        return None
    text = raw.strip().replace("T", " ").replace("/", "-").replace(",", "")
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


# --------------------------------------------------------------------------- #
# Polling
# --------------------------------------------------------------------------- #
async def poll_until(
    check: Callable[[], Awaitable[T]],
    *,
    timeout: float,
    initial_interval: float = 2.0,
    max_interval: float = 15.0,
    factor: float = 1.5,
) -> T | None:
    """Await ``check`` with exponential backoff until it returns a truthy value.

    Returns the truthy result, or ``None`` if ``timeout`` seconds elapsed first.
    This is used instead of fixed ``sleep`` calls so that fast transitions are
    noticed quickly while slow ones stay cheap on a Pi Zero W.
    """
    deadline = time.monotonic() + timeout
    interval = initial_interval
    while True:
        result = await check()
        if result:
            return result
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        await asyncio.sleep(min(interval, max_interval, remaining))
        interval *= factor
