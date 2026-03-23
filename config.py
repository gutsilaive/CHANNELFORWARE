"""
config.py — centralised configuration loader
"""
import os
from dotenv import load_dotenv

load_dotenv()

# ── Telegram Bot ──────────────────────────────────────────────────────────────
BOT_TOKEN: str = os.environ["BOT_TOKEN"]
ADMIN_ID: int = int(os.environ["ADMIN_ID"])

# ── Pyrogram (user client) ────────────────────────────────────────────────────
API_ID: int = int(os.environ["API_ID"])
API_HASH: str = os.environ["API_HASH"]

# ── Supabase ──────────────────────────────────────────────────────────────────
SUPABASE_URL: str = os.environ["SUPABASE_URL"]
SUPABASE_KEY: str = os.environ["SUPABASE_KEY"]

# ── Bot meta ──────────────────────────────────────────────────────────────────
BOT_VERSION = "1.0.0"
MAX_FORWARD = 1000          # maximum messages per forward job
PROGRESS_INTERVAL = 10     # update progress message every N messages
