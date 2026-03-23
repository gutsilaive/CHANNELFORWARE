"""
config.py — centralised configuration loader
"""
import os
from dotenv import load_dotenv

load_dotenv()

# ── Telegram Bot ──────────────────────────────────────────────────────────────
BOT_TOKEN: str = os.environ["BOT_TOKEN"]
ADMIN_ID: int = int(os.environ["ADMIN_ID"])

# ── Pyrogram API credentials ─────────────────────────────────────────────────
# No longer required as env vars — stored per-admin in Supabase.
# Optionally set here as a dev fallback.
API_ID: int | None = int(os.environ["API_ID"]) if os.environ.get("API_ID") else None
API_HASH: str | None = os.environ.get("API_HASH")

# ── Supabase ──────────────────────────────────────────────────────────────────
SUPABASE_URL: str = os.environ["SUPABASE_URL"]
SUPABASE_KEY: str = os.environ["SUPABASE_KEY"]

# ── Bot meta ──────────────────────────────────────────────────────────────────
BOT_VERSION = "1.0.0"
MAX_FORWARD = 1000          # maximum messages per forward job
PROGRESS_INTERVAL = 10     # update progress message every N messages
