"""
keepalive.py — HTTP health server + self-pinger to keep Render free-tier alive 24/7.

Setup:
  1. This file is already imported in bot.py.
  2. Set env var RENDER_EXTERNAL_URL = https://your-app-name.onrender.com in Render dashboard.
     Render sets this automatically for web services!
  3. That's it — the bot pings itself every 4 minutes forever.

Optional: also add an UptimeRobot monitor for extra insurance.
"""
import asyncio
import logging
import os
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

logger = logging.getLogger(__name__)

PORT = int(os.environ.get("PORT", 8080))

# Render sets this automatically for web services. You can also set it manually.
# e.g.  https://channelforware.onrender.com
RENDER_URL = os.environ.get("RENDER_EXTERNAL_URL", "").rstrip("/")

PING_INTERVAL = 4 * 60  # 4 minutes — safely under Render's 5-min sleep threshold


# ──────────────────────────  HTTP health server  ─────────────────────────────

class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/health":
            body = b'{"status":"ok","bot":"AutoForward Bot","alive":true}'
        else:
            body = b"AutoForward Bot is alive! Visit /health for status."

        self.send_response(200)
        self.send_header("Content-Type",
                         "application/json" if self.path == "/health" else "text/plain")
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
    if not RENDER_URL:
        logger.warning("⚠️  RENDER_EXTERNAL_URL not set — self-ping disabled. "
                       "Set it in Render dashboard to keep bot alive 24/7.")
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
        except Exception as e:
            logger.warning(f"🏓 Self-ping failed: {e}")
        await asyncio.sleep(PING_INTERVAL)


def start_ping_task():
    """Schedule the async ping loop on the running event loop."""
    try:
        loop = asyncio.get_event_loop()
        loop.create_task(_ping_loop())
        logger.info("🏓 Keep-alive self-pinger scheduled.")
    except RuntimeError:
        # No running loop yet — will be called again from post_init
        pass


# ──────────────────────────  Public API  ─────────────────────────────────────

def start_keepalive():
    """
    Start the HTTP health server in a background daemon thread.
    Call this at startup (before the bot loop starts).
    """
    thread = threading.Thread(target=_run_server, daemon=True)
    thread.start()
    logger.info(f"🌐 Health server running on port {PORT}")
