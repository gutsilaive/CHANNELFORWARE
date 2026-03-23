"""
handlers/auth.py — Login and Logout conversation handler
States: API_ID_INPUT → API_HASH_INPUT → PHONE → OTP → (PASSWORD)
API credentials are collected once and saved to Supabase for reuse.
"""
import asyncio
from telegram import Update
from telegram.ext import (
    ContextTypes, ConversationHandler, CommandHandler,
    CallbackQueryHandler, MessageHandler, filters
)
from telegram.constants import ParseMode

from database import (
    get_session, save_session, delete_session,
    get_api_credentials, save_api_credentials
)
from userbot import LoginSession
from handlers.ui import E, back_kb, cancel_kb
from handlers.start import _require_admin

# Conversation states
API_ID_INPUT, API_HASH_INPUT, PHONE, OTP, PASSWORD = range(5)

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

    # Check if API credentials are already stored
    creds = get_api_credentials(uid)
    if creds:
        # Skip API creds step — go straight to phone
        ctx.user_data["_api_id"] = creds["api_id"]
        ctx.user_data["_api_hash"] = creds["api_hash"]
        await update.callback_query.edit_message_text(
            f"{E['key']} *Login — Enter Phone Number*\n"
            "━━━━━━━━━━━━━━━━━━━━━\n"
            f"{E['done']} API credentials loaded from storage.\n\n"
            "Send your phone number in international format:\n"
            "`+1 234 567 8900`\n\n"
            f"{E['warn']} _Your number is never stored, only a secure session._",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=cancel_kb(),
        )
        return PHONE

    # First time — need API credentials
    await update.callback_query.edit_message_text(
        f"{E['key']} *Login — Step 1 of 4: API ID*\n"
        "━━━━━━━━━━━━━━━━━━━━━\n"
        "You need a Telegram API ID and Hash.\n\n"
        f"*How to get them:*\n"
        f"1. Go to [my.telegram.org](https://my.telegram.org/auth)\n"
        f"2. Login → *API development tools*\n"
        f"3. Create an app → copy **App api_id** and **App api_hash**\n\n"
        f"Send your *API ID* (numbers only):",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=cancel_kb(),
    )
    return API_ID_INPUT


async def got_api_id(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if not text.isdigit():
        await update.message.reply_text(
            f"{E['warn']} API ID must be numbers only. Try again:",
            reply_markup=cancel_kb(),
        )
        return API_ID_INPUT
    ctx.user_data["_api_id"] = int(text)
    await update.message.reply_text(
        f"{E['done']} API ID saved.\n\n"
        f"{E['key']} *Login — Step 2 of 4: API Hash*\n"
        "━━━━━━━━━━━━━━━━━━━━━\n"
        "Now send your *API Hash* (32-character string):",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=cancel_kb(),
    )
    return API_HASH_INPUT


async def got_api_hash(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    api_hash = update.message.text.strip()
    if len(api_hash) < 20:
        await update.message.reply_text(
            f"{E['warn']} That doesn't look like a valid API Hash. Try again:",
            reply_markup=cancel_kb(),
        )
        return API_HASH_INPUT
    ctx.user_data["_api_hash"] = api_hash
    await update.message.reply_text(
        f"{E['done']} API Hash saved.\n\n"
        f"{E['key']} *Login — Step 3 of 4: Phone Number*\n"
        "━━━━━━━━━━━━━━━━━━━━━\n"
        "Send your phone number in international format:\n"
        "`+1 234 567 8900`",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=cancel_kb(),
    )
    return PHONE


async def got_phone(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    phone = update.message.text.strip()
    api_id = ctx.user_data.get("_api_id")
    api_hash = ctx.user_data.get("_api_hash")

    if not api_id or not api_hash:
        await update.message.reply_text(
            f"{E['error']} API credentials lost. Please /start again."
        )
        return ConversationHandler.END

    msg = await update.message.reply_text(
        f"{E['clock']} Sending OTP to *{phone}*…",
        parse_mode=ParseMode.MARKDOWN,
    )
    ls = LoginSession()
    try:
        code_type = await ls.start_login(phone, api_id, api_hash)
        _sessions[uid] = ls
        await msg.edit_text(
            f"{E['done']} OTP sent via *{code_type}*\n\n"
            f"{E['key']} *Login — Step 4 of 4: Enter OTP*\n"
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
            # Save both the session and API credentials
            save_api_credentials(uid, ls.api_id, ls.api_hash)
            save_session(uid, ls.phone, session_string)
            _sessions.pop(uid, None)
            await msg.edit_text(
                f"{E['done']} *Login Successful!* ✅\n\n"
                f"Logged in as `{ls.phone}`.\n"
                f"API credentials saved securely.\n\n"
                "Use /start to access the main menu.",
                parse_mode=ParseMode.MARKDOWN,
            )
            return ConversationHandler.END
    except ValueError as e:
        await msg.edit_text(str(e) + "\n\nPlease try again.")
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

    try:
        await update.message.delete()
    except Exception:
        pass

    msg = await update.effective_chat.send_message(f"{E['clock']} Verifying 2FA password…")
    try:
        session_string = await ls.submit_password(password)
        save_api_credentials(uid, ls.api_id, ls.api_hash)
        save_session(uid, ls.phone, session_string)
        _sessions.pop(uid, None)
        await msg.edit_text(
            f"{E['done']} *Login Successful!* ✅\n\n"
            f"Logged in as `{ls.phone}` with 2FA.\n"
            "API credentials saved securely.\n\n"
            "Use /start to access the main menu.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return ConversationHandler.END
    except Exception as e:
        await msg.edit_text(
            f"{E['error']} Wrong 2FA password:\n`{e}`\n\nTry again:",
            parse_mode=ParseMode.MARKDOWN,
        )
        return PASSWORD


async def logout_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await _require_admin(update, ctx):
        return
    uid = update.effective_user.id
    delete_session(uid)
    await update.callback_query.answer("Logged out ✅")
    await update.callback_query.edit_message_text(
        f"{E['logout']} *Logged Out*\n\n"
        "Session removed. API credentials are kept for next login.\n"
        "Use /start to log in again.",
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
            f"{E['stop']} Cancelled. Use /start to go back."
        )
    elif update.message:
        await update.message.reply_text(f"{E['stop']} Cancelled. Use /start.")
    return ConversationHandler.END


def register(app):
    conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(login_start, pattern="^login_start$")],
        states={
            API_ID_INPUT: [MessageHandler(filters.TEXT & ~filters.COMMAND, got_api_id)],
            API_HASH_INPUT: [MessageHandler(filters.TEXT & ~filters.COMMAND, got_api_hash)],
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
