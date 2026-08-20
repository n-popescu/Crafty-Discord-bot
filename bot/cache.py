"""A tiny in-memory TTL cache.

The bot runs on a Raspberry Pi Zero W, so there is no database and no external
cache: a dict with monotonic timestamps is enough. Concurrent callers asking for
the same key share a single in-flight request instead of hammering the API.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Awaitable, Callable, TypeVar

T = TypeVar("T")


class TTLCache:
    """Async-safe cache with a per-entry time-to-live."""

    def __init__(self) -> None:
        self._values: dict[str, tuple[float, Any]] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    def get(self, key: str) -> Any | None:
        entry = self._values.get(key)
        if entry is None:
            return None
        expires_at, value = entry
        if expires_at < time.monotonic():
            self._values.pop(key, None)
            return None
        return value

    def set(self, key: str, value: Any, ttl: float) -> None:
        if ttl <= 0:
            return
        self._values[key] = (time.monotonic() + ttl, value)

    def invalidate(self, *keys: str) -> None:
        for key in keys:
            self._values.pop(key, None)

    def invalidate_prefix(self, prefix: str) -> None:
        for key in [k for k in self._values if k.startswith(prefix)]:
            self._values.pop(key, None)

    async def get_or_fetch(
        self, key: str, ttl: float, factory: Callable[[], Awaitable[T]]
    ) -> T:
        """Return the cached value for ``key`` or await ``factory`` to produce it."""
        cached = self.get(key)
        if cached is not None:
            return cached

        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            # Another coroutine may have populated the entry while we waited.
            cached = self.get(key)
            if cached is not None:
                return cached
            value = await factory()
            self.set(key, value, ttl)
            return value
