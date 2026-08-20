"""Entrypoint: ``python -m bot``.

Configuration problems are reported clearly (names only, never values) and the
bot exits with a non-zero status so systemd can restart it once fixed.
"""

from __future__ import annotations

import asyncio
import logging
import sys

import discord
from dotenv import load_dotenv

from bot.client import CraftyBot
from bot.config import load_config
from bot.errors import ConfigError
from bot.utils import setup_logging

logger = logging.getLogger("bot")


async def _run() -> int:
    load_dotenv()

    try:
        config = load_config()
    except ConfigError as exc:
        setup_logging("INFO")
        logger.error("Configuration error\n%s", exc.user_message)
        logger.error("Copy .env.example to .env and fill in the missing values.")
        return 2

    setup_logging(config.log_level)
    logger.info("Starting Crafty Control Panel bot")
    logger.info("Crafty URL: %s (TLS verification: %s)", config.crafty.url, config.crafty.verify_ssl)
    if config.azure_enabled:
        logger.info("Azure VM: %s (resource group %s)", config.azure.vm_name, config.azure.resource_group)
    else:
        logger.info("Azure integration disabled (no subscription/resource group/VM configured)")

    bot = CraftyBot(config)
    try:
        await bot.start(config.discord_token)
    except discord.LoginFailure:
        logger.error("Discord rejected DISCORD_TOKEN. Check the bot token.")
        return 3
    except discord.PrivilegedIntentsRequired:
        logger.error("Discord requires intents this bot does not use; check the application settings.")
        return 3
    finally:
        if not bot.is_closed():
            await bot.close()
    return 0


def main() -> int:
    try:
        return asyncio.run(_run())
    except KeyboardInterrupt:
        logger.info("Shutting down")
        return 0


if __name__ == "__main__":
    sys.exit(main())
