"""
handlers/channels.py — Browse joined channels + resolve by link
Fast: channels are cached in ctx.bot_data for 30 min per user.
User can also instantly paste any link/username without waiting.
"""
import math
import time
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ContextTypes, ConversationHandler, CommandHandler,
    CallbackQueryHandler, MessageHandler, filters,
)
from telegram.constants import ParseMode

from database import get_session
from userbot import get_joined_channels, resolve_and_join_channel
from handlers.ui import E, back_kb, cancel_kb
from handlers.start import _require_admin

PAGE_SIZE = 8
CACHE_TTL = 1800  # 30 minutes

# ── Cache helpers ─────────────────────────────────────────────────────────────

def _cache_key(uid: int) -> str:
    return f"ch_cache_{uid}"

def _get_cached(ctx: ContextTypes.DEFAULT_TYPE, uid: int) -> list[dict] | None:
    key = _cache_key(uid)
    entry = ctx.bot_data.get(key)
    if entry and (time.time() - entry["ts"]) < CACHE_TTL:
        return entry["data"]
    return None

def _set_cache(ctx: ContextTypes.DEFAULT_TYPE, uid: int, channels: list[dict]):
    ctx.bot_data[_cache_key(uid)] = {"ts": time.time(), "data": channels}

def _clear_cache(ctx: ContextTypes.DEFAULT_TYPE, uid: int):
    ctx.bot_data.pop(_cache_key(uid), None)


# ── Keyboard builders ─────────────────────────────────────────────────────────

def _channels_kb(channels: list[dict], page: int, uid: int = 0) -> InlineKeyboardMarkup:
    total_pages = max(1, math.ceil(len(channels) / PAGE_SIZE))
    start = page * PAGE_SIZE
    page_items = channels[start: start + PAGE_SIZE]
    rows = []
    for ch in page_items:
        label = f"📢 {ch['title'][:30]}"
        rows.append([InlineKeyboardButton(label, callback_data=f"ch_info:{ch['id']}")])

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀️ Prev", callback_data=f"ch_page:{page - 1}"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton("Next ▶️", callback_data=f"ch_page:{page + 1}"))
    if nav:
        rows.append(nav)

    rows.append([
        InlineKeyboardButton("🔗 Paste link instead", callback_data="ch_by_link"),
        InlineKeyboardButton("🔄 Refresh", callback_data="ch_refresh"),
    ])
    rows.append([InlineKeyboardButton(f"{E['back']} Back", callback_data="home")])
    return InlineKeyboardMarkup(rows)


def _main_kb() -> InlineKeyboardMarkup:
    """Shown when user first opens My Channels — before channels load."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔗 Paste channel link / @username", callback_data="ch_by_link")],
        [InlineKeyboardButton("📋 Browse my joined channels", callback_data="ch_load")],
        [InlineKeyboardButton(f"{E['back']} Back", callback_data="home")],
    ])


# ── Handlers ──────────────────────────────────────────────────────────────────

async def ch_menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Entry point — show options without loading channels yet."""
    if not await _require_admin(update, ctx):
        return
    uid = update.effective_user.id
    await update.callback_query.answer()

    cached = _get_cached(ctx, uid)
    if cached:
        # Already cached — show list immediately
        text = (
            f"{E['channel']} *Your Channels* _(cached)_\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"Found *{len(cached)}* channels."
        )
        try:
            await update.callback_query.edit_message_text(
                text, parse_mode=ParseMode.MARKDOWN,
                reply_markup=_channels_kb(cached, 0, uid),
            )
        except Exception:
            pass
        return

    try:
        await update.callback_query.edit_message_text(
            f"{E['channel']} *My Channels*\n"
            "━━━━━━━━━━━━━━━━━━━━━\n"
            "Paste a link instantly, or browse your joined channels:",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=_main_kb(),
        )
    except Exception:
        pass


async def ch_load(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Load channels (use cache if available)."""
    if not await _require_admin(update, ctx):
        return
    uid = update.effective_user.id
    await update.callback_query.answer()

    cached = _get_cached(ctx, uid)
    if cached:
        channels = cached
        cached_note = " _(cached)_"
    else:
        msg = await update.callback_query.edit_message_text(
            f"{E['refresh']} Fetching your channels… this may take ~15 seconds."
        )
        session = get_session(uid)
        if not session:
            await msg.edit_text(
                f"{E['error']} Not logged in.", reply_markup=back_kb("home")
            )
            return
        try:
            channels = await get_joined_channels(session)
            _set_cache(ctx, uid, channels)
            cached_note = ""
        except Exception as e:
            await msg.edit_text(
                f"{E['error']} Failed to fetch channels:\n`{e}`",
                parse_mode=ParseMode.MARKDOWN, reply_markup=back_kb("home")
            )
            return

    if not channels:
        try:
            await update.callback_query.edit_message_text(
                f"{E['warn']} No channels found.", reply_markup=back_kb("home")
            )
        except Exception:
            pass
        return

    text = (
        f"{E['channel']} *Your Channels*{cached_note}\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"Found *{len(channels)}* channels."
    )
    try:
        await update.callback_query.edit_message_text(
            text, parse_mode=ParseMode.MARKDOWN,
            reply_markup=_channels_kb(channels, 0, uid),
        )
    except Exception:
        pass


async def ch_refresh(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Force-clear cache and reload."""
    uid = update.effective_user.id
    _clear_cache(ctx, uid)
    await ch_load(update, ctx)


async def ch_page(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    page = int(update.callback_query.data.split(":")[1])
    uid = update.effective_user.id
    channels = _get_cached(ctx, uid) or []
    try:
        await update.callback_query.edit_message_reply_markup(
            reply_markup=_channels_kb(channels, page, uid)
        )
    except Exception:
        pass


# ── Paste-link flow (ConversationHandler) ─────────────────────────────────────

LINK_INPUT = 10


async def ch_by_link_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    try:
        await update.callback_query.edit_message_text(
            f"🔗 *Resolve Channel by Link*\n"
            "━━━━━━━━━━━━━━━━━━━━━\n"
            "Send the channel link or username:\n\n"
            "• `@username`\n"
            "• `https://t.me/username`\n"
            "• `https://t.me/+invitehash` _(private)_\n"
            "• `-100xxxxxxxxxx` _(numeric ID)_",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=cancel_kb(),
        )
    except Exception:
        pass
    return LINK_INPUT


async def ch_by_link_input(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    text = update.message.text.strip()
    session = get_session(uid)
    if not session:
        await update.message.reply_text(f"{E['error']} Not logged in. Use /start.")
        return ConversationHandler.END

    msg = await update.message.reply_text(f"{E['refresh']} Resolving…")
    try:
        info = await resolve_and_join_channel(session, text)
        await msg.edit_text(
            f"✅ *Channel Found!*\n\n"
            f"📢 *{info['title']}*\n"
            f"🔗 `{info['username']}`\n"
            f"🆔 `{info['id']}`",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=back_kb("home"),
        )
    except ValueError as e:
        await msg.edit_text(str(e) + "\n\nTry again or /start.", reply_markup=back_kb("home"))
    except Exception as e:
        await msg.edit_text(
            f"{E['error']} Error:\n`{e}`",
            parse_mode=ParseMode.MARKDOWN, reply_markup=back_kb("home")
        )
    return ConversationHandler.END


async def ch_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.callback_query:
        await update.callback_query.answer("Cancelled")
        try:
            await update.callback_query.edit_message_text(
                f"{E['stop']} Cancelled. Use /start."
            )
        except Exception:
            pass
    return ConversationHandler.END


def register(app):
    # Paste-link conversation
    conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(ch_by_link_start, pattern="^ch_by_link$")],
        states={
            LINK_INPUT: [MessageHandler(filters.TEXT & ~filters.COMMAND, ch_by_link_input)],
        },
        fallbacks=[
            CallbackQueryHandler(ch_cancel, pattern="^cancel$"),
            CommandHandler("start", ch_cancel),
        ],
        allow_reentry=True,
    )
    app.add_handler(conv)

    # Plain callbacks
    app.add_handler(CallbackQueryHandler(ch_menu, pattern="^ch_list$"))
    app.add_handler(CallbackQueryHandler(ch_load, pattern="^ch_load$"))
    app.add_handler(CallbackQueryHandler(ch_refresh, pattern="^ch_refresh$"))
    app.add_handler(CallbackQueryHandler(ch_page, pattern=r"^ch_page:\d+$"))
