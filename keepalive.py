"""
keepalive.py — Tiny HTTP server that keeps Render free-tier web service alive.
Runs in a background thread alongside the bot.
Pin this URL with UptimeRobot (every 5 min) to prevent sleep.
"""
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
import logging

logger = logging.getLogger(__name__)

PORT = 8080


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/health":
            body = b'{"status":"ok","bot":"AutoForward Bot","alive":true}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            body = b"AutoForward Bot is alive! Visit /health for status."
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(body)

    def log_message(self, format, *args):
        pass  # silence access logs


def start_keepalive():
    """Start the keepalive HTTP server in a daemon thread."""
    server = HTTPServer(("0.0.0.0", PORT), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    logger.info(f"🌐 Keepalive server running on port {PORT}")
