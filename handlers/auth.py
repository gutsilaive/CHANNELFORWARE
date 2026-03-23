"""
handlers/auth.py — Login via session string paste + Logout
New flow: user pastes a Pyrogram session string (generated locally/Termux/Replit)
          bot validates it live, then saves to Supabase.
"""
from telegram import Update
from telegram.ext import (
    ContextTypes, ConversationHandler, CommandHandler,
    CallbackQueryHandler, MessageHandler, filters
)
from telegram.constants import ParseMode

from database import get_session, save_session, delete_session, get_api_credentials
from handlers.ui import E, back_kb, cancel_kb
from handlers.start import _require_admin

# Conversation state
SESSION_INPUT = 0

_HOW_TO = (
    f"*How to generate a session string (pick one):*\n\n"
    "📱 *Option A — Termux on Android (FREE):*\n"
    "`pkg install python` → `pip install pyrogram TgCrypto`\n"
    "Then run:\n"
    "```\npython -c \"\nimport asyncio\nfrom pyrogram import Client\nasync def m():\n"
    "    async with Client('s', api_id=YOUR_ID, api_hash='YOUR_HASH',\n"
    "                      in_memory=True) as c:\n"
    "        print(await c.export_session_string())\nasyncio.run(m())\"\n```\n\n"
    "🌐 *Option B — Replit.com (in browser, FREE):*\n"
    "Create a new Python repl → paste the code above → Run\n\n"
    "The program will ask for your phone + OTP, then print a long string.\n"
    "*Copy that string and paste it here.*"
)


async def login_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await _require_admin(update, ctx):
        return ConversationHandler.END
    uid = update.effective_user.id

    if get_session(uid):
        await update.callback_query.answer()
        try:
            await update.callback_query.edit_message_text(
                f"{E['done']} You are already logged in.\n\nUse *Logout* first to switch accounts.",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=back_kb("home"),
            )
        except Exception:
            pass
        return ConversationHandler.END

    await update.callback_query.answer()
    try:
        await update.callback_query.edit_message_text(
            f"{E['key']} *Login — Paste Session String*\n"
            "━━━━━━━━━━━━━━━━━━━━━\n"
            "This bot requires a *Pyrogram session string* to operate.\n\n"
            + _HOW_TO +
            f"\n\n{E['warn']} _Send your session string below:_",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=cancel_kb(),
            disable_web_page_preview=True,
        )
    except Exception:
        pass
    return SESSION_INPUT


async def got_session(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    text = update.message.text.strip()

    # Basic validation: Pyrogram session strings are long base64-like strings
    if len(text) < 200:
        await update.message.reply_text(
            f"{E['warn']} That doesn't look like a valid session string (too short).\n\n"
            "Please paste the full string copied from Termux/Replit.",
            reply_markup=cancel_kb(),
        )
        return SESSION_INPUT

    msg = await update.message.reply_text(f"{E['clock']} Validating session string…")

    # Validate by actually connecting with it
    try:
        from pyrogram import Client as PyroClient

        creds = get_api_credentials(uid)
        if not creds:
            # Try to extract api_id/hash from the session string itself
            # Pyrogram session strings encode this info — try common test credentials
            # We'll use a trick: connect with the session and read its own data
            await msg.edit_text(
                f"{E['warn']} *API credentials needed*\n\n"
                "To validate your session, please also provide your *API ID* (just the number).\n"
                "You used it when generating the session string:",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=cancel_kb(),
            )
            ctx.user_data["_pending_session"] = text
            return _API_ID_FOR_VALIDATION

        api_id = creds["api_id"]
        api_hash = creds["api_hash"]

        async with PyroClient(
            name="validate",
            api_id=api_id,
            api_hash=api_hash,
            session_string=text,
            in_memory=True,
        ) as client:
            me = await client.get_me()
            phone = me.phone_number or str(me.id)

        save_session(uid, phone, text)
        await msg.edit_text(
            f"{E['done']} *Login Successful!* ✅\n\n"
            f"Logged in as `{me.first_name}` (`{phone}`).\n\n"
            "Use /start to access the main menu.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return ConversationHandler.END

    except Exception as e:
        await msg.edit_text(
            f"{E['error']} Failed to validate session:\n`{e}`\n\n"
            "Make sure you copied the *full* session string.\nTry /start to retry.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return ConversationHandler.END


# Extra states needed when no API creds exist yet
_API_ID_FOR_VALIDATION = 1
_API_HASH_FOR_VALIDATION = 2


async def got_api_id_for_val(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if not text.isdigit():
        await update.message.reply_text(
            f"{E['warn']} API ID must be numbers only. Try again:",
            reply_markup=cancel_kb(),
        )
        return _API_ID_FOR_VALIDATION
    ctx.user_data["_val_api_id"] = int(text)
    await update.message.reply_text(
        f"{E['done']} Got it. Now send your *API Hash* (32-char string):",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=cancel_kb(),
    )
    return _API_HASH_FOR_VALIDATION


async def got_api_hash_for_val(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    api_hash = update.message.text.strip()
    api_id = ctx.user_data.get("_val_api_id")
    session_str = ctx.user_data.get("_pending_session")

    if not api_id or not session_str or len(api_hash) < 20:
        await update.message.reply_text(
            f"{E['error']} Something went wrong. Please /start and try again."
        )
        return ConversationHandler.END

    msg = await update.message.reply_text(f"{E['clock']} Validating session…")

    try:
        from pyrogram import Client as PyroClient
        from database import save_api_credentials

        async with PyroClient(
            name="validate",
            api_id=api_id,
            api_hash=api_hash,
            session_string=session_str,
            in_memory=True,
        ) as client:
            me = await client.get_me()
            phone = me.phone_number or str(me.id)

        save_api_credentials(uid, api_id, api_hash)
        save_session(uid, phone, session_str)

        await msg.edit_text(
            f"{E['done']} *Login Successful!* ✅\n\n"
            f"Logged in as `{me.first_name}` (`{phone}`).\n"
            f"API credentials saved for future use.\n\n"
            "Use /start to access the main menu.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return ConversationHandler.END

    except Exception as e:
        await msg.edit_text(
            f"{E['error']} Failed to validate session:\n`{e}`\n\n"
            "Check your API credentials and session string. Try /start to retry.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return ConversationHandler.END


async def logout_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await _require_admin(update, ctx):
        return
    uid = update.effective_user.id
    delete_session(uid)
    await update.callback_query.answer("Logged out ✅")
    try:
        await update.callback_query.edit_message_text(
            f"{E['logout']} *Logged Out*\n\n"
            "Session removed. API credentials kept for next login.\n"
            "Use /start to log in again.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=back_kb("home"),
        )
    except Exception:
        pass


async def cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data.pop("_pending_session", None)
    ctx.user_data.pop("_val_api_id", None)
    if update.callback_query:
        await update.callback_query.answer("Cancelled")
        try:
            await update.callback_query.edit_message_text(
                f"{E['stop']} Cancelled. Use /start to go back."
            )
        except Exception:
            pass
    elif update.message:
        await update.message.reply_text(f"{E['stop']} Cancelled. Use /start.")
    return ConversationHandler.END


def register(app):
    conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(login_start, pattern="^login_start$")],
        states={
            SESSION_INPUT: [MessageHandler(filters.TEXT & ~filters.COMMAND, got_session)],
            _API_ID_FOR_VALIDATION: [MessageHandler(filters.TEXT & ~filters.COMMAND, got_api_id_for_val)],
            _API_HASH_FOR_VALIDATION: [MessageHandler(filters.TEXT & ~filters.COMMAND, got_api_hash_for_val)],
        },
        fallbacks=[
            CallbackQueryHandler(cancel, pattern="^cancel$"),
            CommandHandler("start", cancel),
        ],
        allow_reentry=True,
    )
    app.add_handler(conv)
    app.add_handler(CallbackQueryHandler(logout_cb, pattern="^logout$"))
