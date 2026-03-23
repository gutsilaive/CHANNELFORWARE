"""
handlers/auth.py — Login and Logout conversation handler
States: PHONE → OTP → (PASSWORD)
"""
import asyncio
from telegram import Update
from telegram.ext import (
    ContextTypes, ConversationHandler, CommandHandler,
    CallbackQueryHandler, MessageHandler, filters
)
from telegram.constants import ParseMode

from database import get_session, save_session, delete_session
from userbot import LoginSession
from handlers.ui import E, back_kb, cancel_kb
from handlers.start import _require_admin

# Conversation states
PHONE, OTP, PASSWORD = range(3)

_sessions: dict[int, LoginSession] = {}  # uid → active LoginSession


async def login_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await _require_admin(update, ctx):
        return ConversationHandler.END
    uid = update.effective_user.id
    if get_session(uid):
        await update.callback_query.answer()
        await update.callback_query.edit_message_text(
            f"{E['done']} You are already logged in.\n\nUse *Logout* first to switch accounts.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=back_kb("home"),
        )
        return ConversationHandler.END

    await update.callback_query.answer()
    await update.callback_query.edit_message_text(
        f"{E['key']} *Login — Step 1 of 2*\n"
        "━━━━━━━━━━━━━━━━━━━━━\n"
        "Please send your phone number in international format:\n\n"
        "`+1 234 567 8900`\n\n"
        f"{E['warn']} _Your number is never stored, only a secure session string._",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=cancel_kb(),
    )
    return PHONE


async def got_phone(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    phone = update.message.text.strip()

    msg = await update.message.reply_text(
        f"{E['clock']} Sending OTP to *{phone}*…",
        parse_mode=ParseMode.MARKDOWN,
    )

    ls = LoginSession()
    try:
        code_type = await ls.start_login(phone)
        _sessions[uid] = ls
        await msg.edit_text(
            f"{E['done']} OTP sent via *{code_type}*\n\n"
            f"{E['key']} *Login — Step 2 of 2*\n"
            "━━━━━━━━━━━━━━━━━━━━━\n"
            "Please enter the *5-digit OTP* you received:",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=cancel_kb(),
        )
        return OTP
    except Exception as e:
        await ls.cancel()
        await msg.edit_text(
            f"{E['error']} Failed to send OTP:\n`{e}`\n\nTry /start again.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return ConversationHandler.END


async def got_otp(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    code = update.message.text.strip()
    ls = _sessions.get(uid)
    if not ls:
        await update.message.reply_text(f"{E['error']} Session expired. Please /start again.")
        return ConversationHandler.END

    msg = await update.message.reply_text(f"{E['clock']} Verifying OTP…")

    try:
        needs_pw, session_string = await ls.submit_code(code)
        if needs_pw:
            await msg.edit_text(
                f"{E['lock']} *Two-Factor Authentication*\n"
                "━━━━━━━━━━━━━━━━━━━━━\n"
                "Your account has 2FA enabled.\n"
                "Please enter your *2FA password*:",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=cancel_kb(),
            )
            return PASSWORD
        else:
            save_session(uid, ls.phone, session_string)
            _sessions.pop(uid, None)
            await msg.edit_text(
                f"{E['done']} *Login Successful!*\n\n"
                f"Welcome, you are now logged in as `{ls.phone}`.\n\n"
                "Use /start to access the main menu.",
                parse_mode=ParseMode.MARKDOWN,
            )
            return ConversationHandler.END
    except ValueError as e:
        await msg.edit_text(str(e) + "\n\nPlease try again or /start to cancel.")
        return OTP
    except Exception as e:
        await msg.edit_text(f"{E['error']} Unexpected error: `{e}`\n\nUse /start to retry.")
        return ConversationHandler.END


async def got_password(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    password = update.message.text.strip()
    ls = _sessions.get(uid)
    if not ls:
        await update.message.reply_text(f"{E['error']} Session expired. Please /start again.")
        return ConversationHandler.END

    # Delete the message to protect the password
    try:
        await update.message.delete()
    except Exception:
        pass

    msg = await update.effective_chat.send_message(f"{E['clock']} Verifying 2FA password…")

    try:
        session_string = await ls.submit_password(password)
        save_session(uid, ls.phone, session_string)
        _sessions.pop(uid, None)
        await msg.edit_text(
            f"{E['done']} *Login Successful!*\n\n"
            f"Logged in as `{ls.phone}` with 2FA. ✅\n\n"
            "Use /start to access the main menu.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return ConversationHandler.END
    except Exception as e:
        await msg.edit_text(
            f"{E['error']} Wrong 2FA password or error:\n`{e}`\n\nEnter your password again or /start to cancel."
        )
        return PASSWORD


async def logout_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await _require_admin(update, ctx):
        return
    uid = update.effective_user.id
    delete_session(uid)
    await update.callback_query.answer("Logged out ✅")
    await update.callback_query.edit_message_text(
        f"{E['logout']} *Logged Out*\n\nYour session has been removed.\nUse /start to log in again.",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=back_kb("home"),
    )


async def cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    ls = _sessions.pop(uid, None)
    if ls:
        asyncio.create_task(ls.cancel())
    if update.callback_query:
        await update.callback_query.answer("Cancelled")
        await update.callback_query.edit_message_text(
            f"{E['stop']} Cancelled. Use /start to go back.",
        )
    elif update.message:
        await update.message.reply_text(f"{E['stop']} Cancelled. Use /start to go back.")
    return ConversationHandler.END


def register(app):
    conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(login_start, pattern="^login_start$")],
        states={
            PHONE: [MessageHandler(filters.TEXT & ~filters.COMMAND, got_phone)],
            OTP: [MessageHandler(filters.TEXT & ~filters.COMMAND, got_otp)],
            PASSWORD: [MessageHandler(filters.TEXT & ~filters.COMMAND, got_password)],
        },
        fallbacks=[
            CallbackQueryHandler(cancel, pattern="^cancel$"),
            CommandHandler("start", cancel),
        ],
        allow_reentry=True,
    )
    app.add_handler(conv)
    app.add_handler(CallbackQueryHandler(logout_cb, pattern="^logout$"))
