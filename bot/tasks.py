"""Optional background task: stop an empty server (and its VM) to save money.

This is the modern replacement for the original bot's ``ENABLE_AUTO_STOP_SERVER``
loop. It is **disabled by default** and, when enabled, wakes up only once every
``IDLE_CHECK_INTERVAL`` seconds (15 minutes by default) and makes a single API
call per tick, which is affordable even on a Raspberry Pi Zero W.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING

from bot.errors import BotError

if TYPE_CHECKING:  # pragma: no cover - import cycle only needed for typing
    from bot.client import CraftyBot

logger = logging.getLogger(__name__)


async def idle_watcher(bot: CraftyBot) -> None:
    """Stop the Minecraft server after a configurable period without players."""
    config = bot.config
    interval = config.idle_check_interval
    idle_limit = config.idle_shutdown_minutes * 60
    empty_since: float | None = None

    logger.info(
        "Idle shutdown enabled: stopping after %d min without players (checked every %d s)",
        config.idle_shutdown_minutes,
        interval,
    )
    await bot.wait_until_ready()

    while not bot.is_closed():
        await asyncio.sleep(interval)
        try:
            server_id = await bot.crafty.resolve_server_id(None)
            stats = await bot.crafty.get_stats(server_id)
        except BotError as exc:
            logger.debug("Idle check skipped: %s", exc.user_message)
            empty_since = None
            continue

        if not stats.running or (stats.online or 0) > 0:
            empty_since = None
            continue

        now = time.monotonic()
        if empty_since is None:
            empty_since = now
            continue
        if now - empty_since < idle_limit:
            continue

        logger.info("Server %s idle for %d min: stopping", server_id, config.idle_shutdown_minutes)
        empty_since = None
        try:
            await bot.orchestrator.stop_minecraft(
                server_id, shutdown_vm=config.auto_shutdown_vm
            )
        except BotError as exc:
            logger.warning("Idle shutdown failed: %s", exc.user_message)
