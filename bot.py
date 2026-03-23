"""
bot.py — Main entry point for AutoForward Bot
"""
import asyncio
import logging

from telegram import BotCommand
from telegram.ext import ApplicationBuilder

import config
from keepalive import start_keepalive
import handlers.start as start_h
import handlers.auth as auth_h
import handlers.channels as channels_h
import handlers.forward as forward_h
import handlers.tasks as tasks_h

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


async def post_init(application):
    """Set bot commands menu."""
    await application.bot.set_my_commands([
        BotCommand("start", "Open main menu"),
    ])
    logger.info("✅ Bot commands set.")


def main():
    app = (
        ApplicationBuilder()
        .token(config.BOT_TOKEN)
        .post_init(post_init)
        .concurrent_updates(True)   # allow parallel message handling
        .build()
    )

    # Register handlers in priority order
    start_h.register(app)   # /start, home, help, about
    auth_h.register(app)    # login conversation + logout
    channels_h.register(app)  # channel browser
    forward_h.register(app)   # forward conversation + confirm + stop
    tasks_h.register(app)     # task list + detail

    # Start keepalive HTTP server (prevents Render free tier from sleeping)
    start_keepalive()

    logger.info(f"🤖 AutoForward Bot v{config.BOT_VERSION} starting…")
    logger.info(f"🔒 Admin ID: {config.ADMIN_ID}")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
