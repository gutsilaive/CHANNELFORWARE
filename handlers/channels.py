"""
handlers/channels.py — Browse joined channels handler
"""
import math
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes, CallbackQueryHandler
from telegram.constants import ParseMode

from database import get_session
from userbot import get_joined_channels
from handlers.ui import E, back_kb
from handlers.start import _require_admin

PAGE_SIZE = 8


def _channels_kb(channels: list[dict], page: int) -> InlineKeyboardMarkup:
    total_pages = max(1, math.ceil(len(channels) / PAGE_SIZE))
    start = page * PAGE_SIZE
    page_items = channels[start: start + PAGE_SIZE]
    rows = []
    for ch in page_items:
        label = f"{E['channel']} {ch['title'][:28]}  {ch['username']}"
        rows.append([InlineKeyboardButton(label, callback_data=f"ch_info:{ch['id']}")])

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀️ Prev", callback_data=f"ch_page:{page - 1}"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton("Next ▶️", callback_data=f"ch_page:{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton(f"{E['back']} Back", callback_data="home")])
    return InlineKeyboardMarkup(rows)


async def ch_list(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await _require_admin(update, ctx):
        return
    uid = update.effective_user.id
    await update.callback_query.answer()

    msg = await update.callback_query.edit_message_text(
        f"{E['refresh']} Fetching your channels… please wait."
    )

    session = get_session(uid)
    if not session:
        await msg.edit_text(
            f"{E['error']} You are not logged in. Go back and login first.",
            reply_markup=back_kb("home"),
        )
        return

    try:
        channels = await get_joined_channels(session)
        ctx.user_data["channels"] = channels
    except Exception as e:
        await msg.edit_text(
            f"{E['error']} Failed to fetch channels:\n`{e}`",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=back_kb("home"),
        )
        return

    if not channels:
        await msg.edit_text(
            f"{E['warn']} No channels found. Your account has not joined any channels yet.",
            reply_markup=back_kb("home"),
        )
        return

    text = (
        f"{E['channel']} *Your Channels*\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"Found *{len(channels)}* channels. Tap one for details."
    )
    await msg.edit_text(
        text, parse_mode=ParseMode.MARKDOWN,
        reply_markup=_channels_kb(channels, 0),
    )


async def ch_page(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    page = int(update.callback_query.data.split(":")[1])
    channels = ctx.user_data.get("channels", [])
    await update.callback_query.edit_message_reply_markup(
        reply_markup=_channels_kb(channels, page)
    )


def register(app):
    app.add_handler(CallbackQueryHandler(ch_list, pattern="^ch_list$"))
    app.add_handler(CallbackQueryHandler(ch_page, pattern=r"^ch_page:\d+$"))
