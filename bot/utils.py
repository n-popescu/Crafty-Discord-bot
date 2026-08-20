"""Shared helpers: logging setup, human formatting and backoff polling."""

from __future__ import annotations

import asyncio
import logging
import re
import sys
import time
from datetime import datetime
from typing import Awaitable, Callable, TypeVar

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


def uptime_since(started: datetime | None) -> str:
    """Format the elapsed time since ``started``."""
    if started is None:
        return "N/A"
    now = datetime.now(started.tzinfo) if started.tzinfo else datetime.now()
    return human_duration((now - started).total_seconds())


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
