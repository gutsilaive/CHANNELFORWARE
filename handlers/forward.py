"""
handlers/forward.py — Full forward conversation: source → destinations → range → caption → confirm → live progress
States: SRC_INPUT → DST_INPUT → RANGE_INPUT → CAPTION_INPUT → (confirming via callback)
"""
from __future__ import annotations
import asyncio
import math

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ContextTypes, ConversationHandler, CommandHandler,
    CallbackQueryHandler, MessageHandler, filters,
)
from telegram.constants import ParseMode

from database import get_session, get_api_credentials, create_task, update_task_progress, finish_task
from userbot import get_joined_channels, resolve_and_join_channel, forward_messages
from handlers.ui import E, back_kb, cancel_kb, progress_bar, pct
from handlers.start import _require_admin
import config

# ── Conversation states ───────────────────────────────────────────────────────
SRC_INPUT, DST_INPUT, RANGE_INPUT, CAPTION_INPUT = range(4)

# ── Per-user cancel events ────────────────────────────────────────────────────
_cancel_events: dict[int, asyncio.Event] = {}

PAGE_SIZE = 8

# ─────────────────────────────  Helper keyboards  ────────────────────────────

def _src_channels_kb(channels: list[dict], page: int) -> InlineKeyboardMarkup:
    total_pages = max(1, math.ceil(len(channels) / PAGE_SIZE))
    start = page * PAGE_SIZE
    items = channels[start: start + PAGE_SIZE]
    rows = []
    for ch in items:
        label = f"{E['channel']} {ch['title'][:28]}"
        rows.append([InlineKeyboardButton(label, callback_data=f"fw_src:{ch['id']}:{ch['title'][:20]}")])
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀️ Prev", callback_data=f"fw_srcpage:{page - 1}"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton("Next ▶️", callback_data=f"fw_srcpage:{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton(f"✏️ Type manually", callback_data="fw_src_manual")])
    rows.append([InlineKeyboardButton(f"{E['stop']} Cancel", callback_data="fw_cancel")])
    return InlineKeyboardMarkup(rows)


def _dst_keyboard(selected: list[str]) -> InlineKeyboardMarkup:
    rows = []
    for d in selected:
        rows.append([InlineKeyboardButton(f"💚 {d[:35]}", callback_data=f"fw_dst_remove:{d}")])
    rows.append([InlineKeyboardButton(f"{E['plus']} Add Destination", callback_data="fw_dst_add")])
    if selected:
        rows.append([InlineKeyboardButton(f"{E['done']} Done ({len(selected)} selected)", callback_data="fw_dst_done")])
    rows.append([InlineKeyboardButton(f"{E['stop']} Cancel", callback_data="fw_cancel")])
    return InlineKeyboardMarkup(rows)


# ─────────────────────────────  Step 1: Source  ──────────────────────────────

async def fw_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await _require_admin(update, ctx):
        return ConversationHandler.END
    uid = update.effective_user.id
    creds = get_api_credentials(uid)
    if not session or not creds:
        await update.callback_query.answer()
        await update.callback_query.edit_message_text(
            f"{E['error']} You are not logged in. Please login first.",
            reply_markup=back_kb("home"),
        )
        return ConversationHandler.END

    await update.callback_query.answer()
    msg = await update.callback_query.edit_message_text(
        f"{E['refresh']} Loading your channels…",
    )

    try:
        channels = await get_joined_channels(session, creds["api_id"], creds["api_hash"])
        ctx.user_data["_all_channels"] = channels
    except Exception as e:
        await msg.edit_text(
            f"{E['error']} Could not fetch channels:\n`{e}`",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=back_kb("home"),
        )
        return ConversationHandler.END

    # Reset forward state
    ctx.user_data["fw"] = {
        "source_id": None, "source_title": None,
        "destinations": [],   # list of {id, title, username}
        "start_id": None, "end_id": None,
        "caption": None,
    }

    text = (
        f"{E['forward']} *Forward Messages — Step 1/4*\n"
        "━━━━━━━━━━━━━━━━━━━━━\n"
        f"{E['channel']} *Select Source Channel*\n\n"
        "Pick from your joined channels below, or type a channel link/username manually.\n"
        "_Supports public, private invite links, and @usernames._"
    )
    await msg.edit_text(
        text, parse_mode=ParseMode.MARKDOWN,
        reply_markup=_src_channels_kb(channels, 0),
    )
    return SRC_INPUT


async def fw_srcpage(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    page = int(update.callback_query.data.split(":")[1])
    channels = ctx.user_data.get("_all_channels", [])
    await update.callback_query.edit_message_reply_markup(
        reply_markup=_src_channels_kb(channels, page)
    )
    return SRC_INPUT


async def fw_src_pick(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """User tapped a channel button."""
    await update.callback_query.answer()
    parts = update.callback_query.data.split(":", 2)
    src_id = int(parts[1])
    src_title = parts[2] if len(parts) > 2 else str(src_id)
    ctx.user_data["fw"]["source_id"] = src_id
    ctx.user_data["fw"]["source_title"] = src_title
    await _show_dst_step(update.callback_query.message, ctx)
    return DST_INPUT


async def fw_src_manual(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """User wants to type source manually."""
    await update.callback_query.answer()
    await update.callback_query.edit_message_text(
        f"{E['channel']} *Source Channel — Manual Entry*\n"
        "━━━━━━━━━━━━━━━━━━━━━\n"
        "Send the channel link or username:\n\n"
        "• `@username`\n"
        "• `https://t.me/username`\n"
        "• `https://t.me/+invitehash` _(private)_",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=cancel_kb(),
    )
    ctx.user_data["_awaiting"] = "source"
    return SRC_INPUT


async def fw_src_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """User sent text during SRC_INPUT — could be source or destination entry."""
    uid = update.effective_user.id
    text = update.message.text.strip()
    session = get_session(uid)
    creds = get_api_credentials(uid)
    awaiting = ctx.user_data.get("_awaiting")

    if not session or not creds:
        await update.message.reply_text(f"{E['error']} Session expired. Use /start.")
        return ConversationHandler.END

    msg = await update.message.reply_text(f"{E['refresh']} Resolving channel…")

    try:
        info = await resolve_and_join_channel(session, creds["api_id"], creds["api_hash"], text)
    except ValueError as e:
        await msg.edit_text(str(e) + "\n\nTry again or /start to cancel.")
        return SRC_INPUT if awaiting == "source" else DST_INPUT
    except Exception as e:
        await msg.edit_text(f"{E['error']} Unexpected error:\n`{e}`", parse_mode=ParseMode.MARKDOWN)
        return SRC_INPUT if awaiting == "source" else DST_INPUT

    if awaiting == "source":
        ctx.user_data["fw"]["source_id"] = info["id"]
        ctx.user_data["fw"]["source_title"] = info["title"]
        ctx.user_data.pop("_awaiting", None)
        await msg.delete()
        await _show_dst_step(update.message, ctx, new_message=True)
        return DST_INPUT

    elif awaiting == "destination":
        dests = ctx.user_data["fw"]["destinations"]
        # Prevent duplicate
        if not any(d["id"] == info["id"] for d in dests):
            dests.append(info)
        ctx.user_data.pop("_awaiting", None)
        await msg.delete()
        await _refresh_dst_message(update.message, ctx, new_message=True)
        return DST_INPUT

    return SRC_INPUT


# ─────────────────────────────  Step 2: Destinations  ────────────────────────

async def _show_dst_step(message, ctx: ContextTypes.DEFAULT_TYPE, new_message: bool = False):
    fw = ctx.user_data["fw"]
    src_title = fw["source_title"]
    dests = fw["destinations"]
    text = (
        f"{E['forward']} *Forward Messages — Step 2/4*\n"
        "━━━━━━━━━━━━━━━━━━━━━\n"
        f"{E['channel']} Source: `{src_title}`\n\n"
        f"{E['plus']} *Add Destination Channels*\n"
        "_You can add multiple destinations. Tap a green entry to remove it._\n\n"
        + (f"Selected: *{len(dests)}* destination(s)" if dests else "_No destinations added yet._")
    )
    kb = _dst_keyboard([d["title"][:35] for d in dests])
    if new_message:
        await message.reply_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)
    else:
        await message.edit_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)


async def _refresh_dst_message(message, ctx: ContextTypes.DEFAULT_TYPE, new_message: bool = False):
    await _show_dst_step(message, ctx, new_message=new_message)


async def fw_dst_add(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    await update.callback_query.edit_message_text(
        f"{E['plus']} *Add Destination Channel*\n"
        "━━━━━━━━━━━━━━━━━━━━━\n"
        "Send the destination channel link or username:\n\n"
        "• `@username`\n"
        "• `https://t.me/username`\n"
        "• `https://t.me/+invitehash` _(private)_\n\n"
        "_If not already joined, the bot will join it automatically._",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=cancel_kb(),
    )
    ctx.user_data["_awaiting"] = "destination"
    return DST_INPUT


async def fw_dst_remove(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer("Removed ✅")
    title = update.callback_query.data.split(":", 1)[1]
    dests = ctx.user_data["fw"]["destinations"]
    ctx.user_data["fw"]["destinations"] = [d for d in dests if d["title"][:35] != title]
    await _show_dst_step(update.callback_query.message, ctx)
    return DST_INPUT


async def fw_dst_done(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    fw = ctx.user_data["fw"]
    if not fw["destinations"]:
        await update.callback_query.answer(f"{E['warn']} Add at least one destination!", show_alert=True)
        return DST_INPUT
    await update.callback_query.edit_message_text(
        f"{E['forward']} *Forward Messages — Step 3/4*\n"
        "━━━━━━━━━━━━━━━━━━━━━\n"
        f"{E['channel']} Source: `{fw['source_title']}`\n"
        f"{E['plus']} Destinations: *{len(fw['destinations'])}* channel(s)\n\n"
        f"*Set Message Range*\n"
        "Send the start and end message IDs separated by a space:\n\n"
        "`<start_id> <end_id>`\n\n"
        "_Example: `1 500`  (forwards messages 1 through 500)_\n"
        f"_Max: {config.MAX_FORWARD} messages per run._",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=cancel_kb(),
    )
    return RANGE_INPUT


# ─────────────────────────────  Step 3: Range  ───────────────────────────────

async def fw_range(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    parts = text.split()
    if len(parts) != 2 or not all(p.isdigit() for p in parts):
        await update.message.reply_text(
            f"{E['warn']} Invalid format. Send two numbers:\n`<start_id> <end_id>`\n\nExample: `1 500`",
            parse_mode=ParseMode.MARKDOWN,
        )
        return RANGE_INPUT

    start_id, end_id = int(parts[0]), int(parts[1])
    if start_id > end_id:
        await update.message.reply_text(f"{E['warn']} Start ID must be less than or equal to End ID.")
        return RANGE_INPUT
    if end_id - start_id + 1 > config.MAX_FORWARD:
        end_id = start_id + config.MAX_FORWARD - 1
        await update.message.reply_text(
            f"{E['warn']} Range capped to {config.MAX_FORWARD} messages. End ID adjusted to `{end_id}`.",
            parse_mode=ParseMode.MARKDOWN,
        )

    ctx.user_data["fw"]["start_id"] = start_id
    ctx.user_data["fw"]["end_id"] = end_id
    fw = ctx.user_data["fw"]

    await update.message.reply_text(
        f"{E['forward']} *Forward Messages — Step 4/4*\n"
        "━━━━━━━━━━━━━━━━━━━━━\n"
        f"{E['channel']} Source: `{fw['source_title']}`\n"
        f"{E['plus']} Destinations: *{len(fw['destinations'])}* channel(s)\n"
        f"{E['clock']} Range: `{start_id}` → `{end_id}` (*{end_id - start_id + 1}* messages)\n\n"
        f"*Custom Caption (optional)*\n"
        "Send a caption to replace original captions, or press Skip:",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("⏭ Skip (keep original)", callback_data="fw_skip_caption")],
            [InlineKeyboardButton(f"{E['stop']} Cancel", callback_data="fw_cancel")],
        ]),
    )
    return CAPTION_INPUT


# ─────────────────────────────  Step 4: Caption  ─────────────────────────────

async def fw_caption(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    caption = update.message.text.strip()
    ctx.user_data["fw"]["caption"] = caption
    await _show_confirm(update.message, ctx, new_message=True)
    return ConversationHandler.END


async def fw_skip_caption(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    ctx.user_data["fw"]["caption"] = None
    await _show_confirm(update.callback_query.message, ctx)
    return ConversationHandler.END


async def _show_confirm(message, ctx: ContextTypes.DEFAULT_TYPE, new_message: bool = False):
    fw = ctx.user_data["fw"]
    dst_list = "\n".join(f"  • `{d['title'][:40]}`" for d in fw["destinations"])
    caption_preview = f"`{fw['caption'][:80]}`" if fw["caption"] else "_keep original_"
    text = (
        f"{E['forward']} *Confirm Forward Job*\n"
        "━━━━━━━━━━━━━━━━━━━━━\n"
        f"{E['channel']} *Source:* `{fw['source_title']}`\n"
        f"{E['plus']} *Destinations:*\n{dst_list}\n"
        f"{E['clock']} *Range:* `{fw['start_id']}` → `{fw['end_id']}` (*{fw['end_id'] - fw['start_id'] + 1}* msgs)\n"
        f"✏️ *Caption:* {caption_preview}\n"
        "━━━━━━━━━━━━━━━━━━━━━\n"
        "Tap *Confirm* to start forwarding."
    )
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton(f"{E['done']} Confirm & Start", callback_data="fw_confirm")],
        [InlineKeyboardButton(f"{E['stop']} Cancel", callback_data="fw_cancel")],
    ])
    if new_message:
        await message.reply_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)
    else:
        await message.edit_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)


# ─────────────────────────────  Confirm → Run  ───────────────────────────────

async def fw_confirm(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await _require_admin(update, ctx):
        return
    uid = update.effective_user.id
    session = get_session(uid)
    creds = get_api_credentials(uid)
    fw = ctx.user_data.get("fw")
    if not fw or not session or not creds:
        await update.callback_query.answer(f"{E['error']} Session lost. Use /start.", show_alert=True)
        return

    await update.callback_query.answer("🚀 Starting…")

    total = fw["end_id"] - fw["start_id"] + 1
    task_id = create_task(
        admin_id=uid,
        source=str(fw["source_id"]),
        destinations=[str(d["id"]) for d in fw["destinations"]],
        caption=fw["caption"],
        start_id=fw["start_id"],
        end_id=fw["end_id"],
    )

    progress_msg = await update.callback_query.edit_message_text(
        _progress_text(fw["source_title"], fw["destinations"], 0, total, 0, "Starting…"),
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton(f"{E['stop']} Stop Forwarding", callback_data=f"fw_stop:{task_id}")]
        ]),
    )

    stop_event = asyncio.Event()
    _cancel_events[task_id] = stop_event

    async def on_progress(forwarded: int, total: int, errors: int):
        update_task_progress(task_id, forwarded)
        bar = progress_bar(forwarded, total)
        eta_str = f"{E['clock']} {pct(forwarded, total)}"
        try:
            await progress_msg.edit_text(
                _progress_text(fw["source_title"], fw["destinations"], forwarded, total, errors, eta_str),
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton(f"{E['stop']} Stop Forwarding", callback_data=f"fw_stop:{task_id}")]
                ]),
            )
        except Exception:
            pass  # ignore edit conflicts (Telegram 5-second throttle)

    try:
        result = await forward_messages(
            session_string=session,
            api_id=creds["api_id"],
            api_hash=creds["api_hash"],
            source=fw["source_id"],
            destinations=[d["id"] for d in fw["destinations"]],
            start_id=fw["start_id"],
            end_id=fw["end_id"],
            caption=fw["caption"],
            progress_cb=on_progress,
            stop_event=stop_event,
        )
        stopped = stop_event.is_set()
        status = "stopped" if stopped else "done"
        finish_task(task_id, status=status)
        _cancel_events.pop(task_id, None)

        dst_names = ", ".join(f"`{d['title'][:20]}`" for d in fw["destinations"])
        final_text = (
            f"{'⏹' if stopped else E['done']} *Forward {'Stopped' if stopped else 'Complete'}!*\n"
            "━━━━━━━━━━━━━━━━━━━━━\n"
            f"{E['channel']} Source: `{fw['source_title']}`\n"
            f"{E['forward']} Destinations: {dst_names}\n"
            f"\n"
            f"✅ Forwarded: *{result['forwarded']}* / {total}\n"
            f"⏭ Skipped: *{result['skipped']}*\n"
            f"❌ Errors: *{result['errors']}*\n"
            f"\n_Task ID: `{task_id[:8]}…`_"
        )
        await progress_msg.edit_text(final_text, parse_mode=ParseMode.MARKDOWN,
                                     reply_markup=back_kb("home"))
    except ValueError as e:
        finish_task(task_id, status="error", error=str(e))
        await progress_msg.edit_text(
            f"{E['error']} *Error during forwarding:*\n{e}",
            parse_mode=ParseMode.MARKDOWN, reply_markup=back_kb("home")
        )
    except Exception as e:
        finish_task(task_id, status="error", error=str(e))
        await progress_msg.edit_text(
            f"{E['error']} Unexpected error:\n`{e}`",
            parse_mode=ParseMode.MARKDOWN, reply_markup=back_kb("home")
        )


def _progress_text(src_title, dests, done, total, errors, status_str) -> str:
    bar = progress_bar(done, total)
    dst_names = ", ".join(f"`{d['title'][:18]}`" for d in dests)
    return (
        f"{E['forward']} *Forwarding in Progress…*\n"
        "━━━━━━━━━━━━━━━━━━━━━\n"
        f"{E['channel']} Source: `{src_title}`\n"
        f"{E['plus']} To: {dst_names}\n\n"
        f"`{bar}` {pct(done, total)}\n"
        f"✅ Done: *{done}* / {total}\n"
        f"❌ Errors: *{errors}*\n"
        f"\n_{status_str}_"
    )


async def fw_stop(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await _require_admin(update, ctx):
        return
    task_id = update.callback_query.data.split(":", 1)[1]
    ev = _cancel_events.get(task_id)
    if ev:
        ev.set()
        await update.callback_query.answer("⏹ Stopping…")
    else:
        await update.callback_query.answer("Already finished.")


async def fw_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data.pop("fw", None)
    ctx.user_data.pop("_awaiting", None)
    if update.callback_query:
        await update.callback_query.answer("Cancelled")
        await update.callback_query.edit_message_text(
            f"{E['stop']} Forward job cancelled. Use /start to go back."
        )
    elif update.message:
        await update.message.reply_text(f"{E['stop']} Cancelled. Use /start.")
    return ConversationHandler.END


# ─────────────────────────────  DST text during conv  ────────────────────────

async def fw_dst_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Text received while expecting a destination entry."""
    return await fw_src_text(update, ctx)


# ─────────────────────────────  Register  ────────────────────────────────────

def register(app):
    # Conversation: forward flow
    conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(fw_start, pattern="^fw_start$")],
        states={
            SRC_INPUT: [
                CallbackQueryHandler(fw_srcpage, pattern=r"^fw_srcpage:\d+$"),
                CallbackQueryHandler(fw_src_pick, pattern=r"^fw_src:-?\d+:"),
                CallbackQueryHandler(fw_src_manual, pattern="^fw_src_manual$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, fw_src_text),
            ],
            DST_INPUT: [
                CallbackQueryHandler(fw_dst_add, pattern="^fw_dst_add$"),
                CallbackQueryHandler(fw_dst_remove, pattern=r"^fw_dst_remove:"),
                CallbackQueryHandler(fw_dst_done, pattern="^fw_dst_done$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, fw_dst_text),
            ],
            RANGE_INPUT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, fw_range),
            ],
            CAPTION_INPUT: [
                CallbackQueryHandler(fw_skip_caption, pattern="^fw_skip_caption$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, fw_caption),
            ],
        },
        fallbacks=[
            CallbackQueryHandler(fw_cancel, pattern="^fw_cancel$"),
            CallbackQueryHandler(cancel_kb, pattern="^cancel$"),
            CommandHandler("start", fw_cancel),
        ],
        allow_reentry=True,
        per_message=False,
    )
    app.add_handler(conv)
    # Confirm and stop run outside conversation (after ConversationHandler.END)
    app.add_handler(CallbackQueryHandler(fw_confirm, pattern="^fw_confirm$"))
    app.add_handler(CallbackQueryHandler(fw_stop, pattern=r"^fw_stop:"))
