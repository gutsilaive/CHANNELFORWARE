"""
keepalive.py — HTTP health server + self-pinger to keep Render free-tier alive 24/7.

HOW IT WORKS:
  1. An HTTP server runs in a background thread, listening on PORT (auto-set by Render).
  2. An async ping loop hits /health every 4 minutes to prevent Render sleep.
  3. The ping task registers itself so it can be cancelled cleanly on shutdown.

SETUP:
  - No action needed: Render auto-sets RENDER_EXTERNAL_URL for web services.
  - The bot pings itself every 4 minutes forever.
  - For extra insurance, also add UptimeRobot → https://uptimerobot.com (free).
"""
import asyncio
import logging
import os
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

logger = logging.getLogger(__name__)

# Render injects PORT automatically for web services (usually 10000).
# Do NOT hardcode this — always read from env.
PORT = int(os.environ.get("PORT", 10000))

# Render sets this automatically for web services.
RENDER_URL = os.environ.get("RENDER_EXTERNAL_URL", "").rstrip("/")

PING_INTERVAL = 4 * 60  # 4 minutes — safely under Render's 5-min sleep threshold

# Global reference so bot.py shutdown hook can cancel it
_ping_task: asyncio.Task | None = None


# ──────────────────────────  HTTP health server  ─────────────────────────────

class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/health":
            body = b'{"status":"ok","bot":"AutoForward Bot","alive":true}'
            content_type = "application/json"
        else:
            body = b"AutoForward Bot is alive! Visit /health for status."
            content_type = "text/plain"

        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_HEAD(self):
        self.do_GET()

    def log_message(self, format, *args):
        pass  # silence access logs


def _run_server():
    server = HTTPServer(("0.0.0.0", PORT), _Handler)
    server.serve_forever()


# ──────────────────────────  Self-pinger  ────────────────────────────────────

async def _ping_loop():
    """Ping our own /health endpoint every PING_INTERVAL seconds."""
    global _ping_task
    _ping_task = asyncio.current_task()

    if not RENDER_URL:
        logger.warning(
            "⚠️  RENDER_EXTERNAL_URL not set — self-ping disabled. "
            "Render will set this automatically for web services."
        )
        return

    url = f"{RENDER_URL}/health"
    logger.info(f"🏓 Self-pinger active → {url} every {PING_INTERVAL // 60} min")

    # Small initial delay so the server is fully up before first ping
    await asyncio.sleep(30)

    while True:
        try:
            import httpx
            async with httpx.AsyncClient(timeout=10) as client:
                r = await client.get(url)
            logger.debug(f"🏓 Self-ping {r.status_code}")
        except asyncio.CancelledError:
            logger.info("🏓 Self-pinger cancelled cleanly.")
            return
        except Exception as e:
            logger.warning(f"🏓 Self-ping failed: {e}")
        await asyncio.sleep(PING_INTERVAL)


def start_ping_task():
    """Schedule the async ping loop on the running event loop."""
    try:
        loop = asyncio.get_event_loop()
        loop.create_task(_ping_loop(), name="keepalive_ping")
        logger.info("🏓 Keep-alive self-pinger scheduled.")
    except RuntimeError as e:
        logger.warning(f"🏓 Could not schedule ping task: {e}")


def cancel_ping_task():
    """Cancel the ping task cleanly (call from shutdown hook)."""
    global _ping_task
    if _ping_task and not _ping_task.done():
        _ping_task.cancel()


# ──────────────────────────  Public API  ─────────────────────────────────────

def start_keepalive():
    """
    Start the HTTP health server in a background daemon thread.
    Call this at startup (before the bot loop starts).
    """
    thread = threading.Thread(target=_run_server, daemon=True)
    thread.start()
    logger.info(f"🌐 Health server running on port {PORT}")
