"""
handlers/start.py — /start, main menu, help, about, admin guard
"""
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import (
    ContextTypes, CommandHandler, CallbackQueryHandler
)
from telegram.constants import ParseMode

import config
from database import get_session
from handlers.ui import E, main_menu_kb, back_kb

BOT_VERSION = config.BOT_VERSION


def _is_admin(user_id: int) -> bool:
    return user_id == config.ADMIN_ID


async def _require_admin(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> bool:
    uid = update.effective_user.id
    if _is_admin(uid):
        return True
    msg = (
        f"{E['lock']} *Access Denied*\n\n"
        "This bot is private and only accessible to its owner."
    )
    if update.message:
        await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)
    elif update.callback_query:
        await update.callback_query.answer("⛔ Unauthorized", show_alert=True)
    return False


WELCOME = (
    "{bot} *AutoForward Bot* `v{ver}`\n"
    "━━━━━━━━━━━━━━━━━━━━━\n"
    "{fire} Forward up to *3,000 messages* to multiple channels at once\n"
    "{lock} No *\"Forwarded from\"* tag — messages appear as original\n"
    "{channel} Works with *private & public channels*\n"
    "━━━━━━━━━━━━━━━━━━━━━\n"
    "_Status: {status}_"
)

HELP_TEXT = (
    f"{E['help']} *How to use AutoForward Bot*\n"
    "━━━━━━━━━━━━━━━━━━━━━\n"
    f"*1.* {E['key']} **Login** — Connect your Telegram account via phone + OTP\n"
    f"*2.* {E['channel']} **My Channels** — Browse channels your account has joined\n"
    f"*3.* {E['forward']} **Forward Messages** — Pick source, destination(s), set range & caption\n"
    f"*4.* {E['tasks']} **Task History** — See live progress and past jobs\n\n"
    f"*Supported link formats:*\n"
    f"• `@username` or `username`\n"
    f"• `https://t.me/username`\n"
    f"• `https://t.me/+invitehash` (private channels)\n\n"
    f"{E['warn']} Your account must have permission to send messages in destination channels."
)

ABOUT_TEXT = (
    f"{E['version']} *AutoForward Bot* `v{BOT_VERSION}`\n"
    "━━━━━━━━━━━━━━━━━━━━━\n"
    "• Engine: Pyrogram (MTProto)\n"
    "• Storage: Supabase\n"
    "• No \"Forwarded from\" tag ✅\n"
    "• Private channel support ✅\n"
    "• Multi-destination forwarding ✅\n"
    "• Live progress updates ✅\n"
    "• Batch up to 3,000 messages \u2705\n"
)


async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await _require_admin(update, ctx):
        return
    uid = update.effective_user.id
    session = get_session(uid)
    logged_in = session is not None
    status = f"{E['done']} Logged in" if logged_in else f"{E['warn']} Not logged in"
    text = WELCOME.format(
        bot=E["bot"], ver=BOT_VERSION, fire=E["fire"],
        lock=E["lock"], channel=E["channel"], status=status
    )
    kb = main_menu_kb(logged_in)
    if update.message:
        await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)
    else:
        await update.callback_query.edit_message_text(
            text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb
        )


async def home_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    await start(update, ctx)


async def help_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await _require_admin(update, ctx):
        return
    await update.callback_query.answer()
    await update.callback_query.edit_message_text(
        HELP_TEXT, parse_mode=ParseMode.MARKDOWN,
        reply_markup=back_kb("home")
    )


async def about_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await _require_admin(update, ctx):
        return
    await update.callback_query.answer()
    await update.callback_query.edit_message_text(
        ABOUT_TEXT, parse_mode=ParseMode.MARKDOWN,
        reply_markup=back_kb("home")
    )


def register(app):
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(home_cb, pattern="^home$"))
    app.add_handler(CallbackQueryHandler(help_cb, pattern="^help$"))
    app.add_handler(CallbackQueryHandler(about_cb, pattern="^about$"))
