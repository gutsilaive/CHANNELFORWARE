"""
handlers/ui.py — shared UI helpers, emoji constants, keyboard builders
"""
from telegram import InlineKeyboardButton, InlineKeyboardMarkup

# ── Emoji palette ─────────────────────────────────────────────────────────────
E = {
    "bot":      "🤖",
    "key":      "🔑",
    "channel":  "📢",
    "forward":  "⏩",
    "tasks":    "📋",
    "done":     "✅",
    "error":    "❌",
    "warn":     "⚠️",
    "info":     "ℹ️",
    "logout":   "🚪",
    "help":     "❓",
    "back":     "◀️",
    "plus":     "➕",
    "trash":    "🗑️",
    "run":      "▶️",
    "stop":     "⏹",
    "refresh":  "🔄",
    "fire":     "🔥",
    "lock":     "🔒",
    "star":     "⭐",
    "version":  "🛸",
    "clock":    "⏱",
}

# ── Progress bar builder ───────────────────────────────────────────────────────
def progress_bar(current: int, total: int, width: int = 12) -> str:
    if total == 0:
        return "░" * width
    filled = round(width * current / total)
    return "█" * filled + "░" * (width - filled)


def pct(current: int, total: int) -> str:
    if total == 0:
        return "0%"
    return f"{round(current / total * 100)}%"


# ── Keyboard helpers ──────────────────────────────────────────────────────────
def btn(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text, callback_data=data)


def url_btn(text: str, url: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text, url=url)


def main_menu_kb(logged_in: bool) -> InlineKeyboardMarkup:
    rows = []
    if logged_in:
        rows.append([btn(f"{E['forward']} Forward Messages", "fw_start")])
        rows.append([btn(f"{E['channel']} My Channels", "ch_list"),
                     btn(f"{E['tasks']} Task History", "task_list")])
        rows.append([btn(f"{E['logout']} Logout", "logout")])
    else:
        rows.append([btn(f"{E['key']} Login with Phone", "login_start")])
    rows.append([btn(f"{E['help']} Help", "help"),
                 btn(f"{E['version']} About", "about")])
    return InlineKeyboardMarkup(rows)


def back_kb(callback: str = "home") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[btn(f"{E['back']} Back", callback)]])


def cancel_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[btn(f"{E['stop']} Cancel", "cancel")]])


def confirm_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [btn(f"{E['done']} Confirm & Start", "fw_confirm"),
         btn(f"{E['stop']} Cancel", "cancel")]
    ])
