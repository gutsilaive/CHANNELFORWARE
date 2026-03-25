"""
bot.py — Main entry point for AutoForward Bot
"""
import sys
import time
import logging

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# Start keepalive absolutely first, before ANY other imports that might crash (like config)
from keepalive import start_keepalive, start_ping_task
start_keepalive()

try:
    import asyncio
    # ── Critical: create event loop before Pyrogram import ──────────────────────
    if sys.version_info >= (3, 10):
        _loop = asyncio.new_event_loop()
        asyncio.set_event_loop(_loop)

    from telegram import BotCommand
    from telegram.ext import ApplicationBuilder, ContextTypes

    import config
    import handlers.start as start_h
    import handlers.auth as auth_h
    import handlers.channels as channels_h
    import handlers.forward as forward_h
    import handlers.tasks as tasks_h

    async def post_init(application):
        """Set bot commands menu and start keep-alive pinger."""
        await application.bot.set_my_commands([
            BotCommand("start", "Open main menu"),
        ])
        logger.info("✅ Bot commands set.")
        start_ping_task()  # schedules self-ping on the live event loop

    async def global_error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Suppress specific noisy errors from filling the logs."""
        if context.error:
            err_str = str(context.error)
            if "Conflict: terminated by other getUpdates request" in err_str:
                # Silence deployment overlap noise
                return
            if "Message is not modified" in err_str:
                return
            logger.error(f"Telegram API Exception: {context.error}")

    def main():
        app = (
            ApplicationBuilder()
            .token(config.BOT_TOKEN)
            .post_init(post_init)
            .concurrent_updates(True)   # allow parallel message handling
            .build()
        )

        app.add_error_handler(global_error_handler)


        # Register handlers in priority order
        start_h.register(app)
        auth_h.register(app)
        channels_h.register(app)
        forward_h.register(app)
        tasks_h.register(app)

        logger.info(f"🤖 AutoForward Bot v{config.BOT_VERSION} starting…")
        logger.info(f"🔒 Admin ID: {config.ADMIN_ID}")
        app.run_polling(drop_pending_updates=True)

    if __name__ == "__main__":
        main()

except Exception as e:
    logger.critical(f"FATAL STARTUP ERROR: {e}")
    logger.critical("Bot initialization failed. The keepalive server is still running to satisfy Render/UptimeRobot.")
    print("\n" + "="*50)
    print("CRASH DETAILS: Please check if you provided all environment variables (BOT_TOKEN, ADMIN_ID, SUPABASE_URL, SUPABASE_KEY).")
    print("="*50 + "\n")
    # Keep the main thread alive indefinitely so Render proxy doesn't die and UptimeRobot stays green
    while True:
        time.sleep(3600)
