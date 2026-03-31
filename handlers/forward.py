"""
handlers/forward.py
New forward flow (6 steps):
  Step 1: Source channel  (any link / @username / invite / ID)
  Step 2: Message link(s) (single link  or  start link + end link)
  Step 3: Custom Caption  (optional, only applied to media)
  Step 4: Custom Thumbnail (optional, only applied to video/doc)
  Step 5: Destination channel(s) (add one or more)
  Step 6: Confirm → live progress
"""
from __future__ import annotations
import asyncio
import math
import os
import re
import tempfile
import time

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ContextTypes, ConversationHandler, CommandHandler,
    CallbackQueryHandler, MessageHandler, filters,
)
from telegram.constants import ParseMode

from database import get_session, create_task, update_task_progress, finish_task
from userbot import resolve_and_join_channel, forward_messages
from handlers.ui import E, back_kb, progress_bar, pct
from handlers.start import _require_admin
import config

# ── Conversation states ───────────────────────────────────────────────────────
SRC, MSG, COUNT, CAPTION, THUMB, DST = range(6)

# ── Per-user stop events for running jobs ─────────────────────────────────────
_stop_events: dict[str, asyncio.Event] = {}


# ─────────────────────────  Helpers  ─────────────────────────────────────────

def _cancel_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"{E['stop']} Cancel", callback_data="fw_cancel")]
    ])


def _skip_cancel_kb(skip_cb: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⏭️ Skip", callback_data=skip_cb)],
        [InlineKeyboardButton(f"{E['stop']} Cancel", callback_data="fw_cancel")],
    ])


def _parse_msg_link(text: str) -> tuple[int | None, int | None]:
    """
    Parse a Telegram message link → (channel_id_or_None, msg_id_or_None).
    Handles:
      • https://t.me/c/CHANNEL_ID/MSG_ID   (private)
      • https://t.me/username/MSG_ID       (public)
      • plain number                        (treat as msg_id)
    """
    text = text.strip()
    # Private: t.me/c/CHANNEL_ID/MSG_ID
    m = re.search(r't\.me/c/(\d+)/(\d+)', text)
    if m:
        return int(f"-100{m.group(1)}"), int(m.group(2))
    # Public: t.me/username/MSG_ID
    m = re.search(r't\.me/[\w_-]+/(\d+)', text)
    if m:
        return None, int(m.group(1))
    # Bare number
    if text.isdigit():
        return None, int(text)
    return None, None


def _dst_kb(dests: list[dict]) -> InlineKeyboardMarkup:
    rows = []
    for d in dests:
        rows.append([InlineKeyboardButton(
            f"✅ {d['title'][:28]} (tap to remove)",
            callback_data=f"fw_dst_rm:{d['id']}"
        )])
    rows.append([InlineKeyboardButton("➕ Add Another Channel/Group", callback_data="fw_dst_add")])
    if dests:
        rows.append([InlineKeyboardButton(
            f"✔️ Done ({len(dests)} selected)", callback_data="fw_dst_done"
        )])
    rows.append([InlineKeyboardButton(f"{E['stop']} Cancel", callback_data="fw_cancel")])
    return InlineKeyboardMarkup(rows)


# ─────────────────────────  Step 1: Source  ──────────────────────────────────

async def fw_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Entry point — ask for source channel link."""
    if not await _require_admin(update, ctx):
        return ConversationHandler.END

    uid = update.effective_user.id
    session = get_session(uid)
    if not session:
        await update.callback_query.answer()
        await update.callback_query.edit_message_text(
            f"{E['error']} Not logged in. Use /start → Login.",
            reply_markup=back_kb("home"),
        )
        return ConversationHandler.END

    await update.callback_query.answer()

    # Init state
    ctx.user_data["fw"] = {
        "source_id": None, "source_title": None, "source_ref": None,
        "start_id": None, "end_id": None,
        "msg_link_raw": None,
        "destinations": [],
        "caption": None, "thumbnail_path": None,
    }

    await update.callback_query.edit_message_text(
        f"{E['forward']} *Forward — Step 1/7  ·  Source Channel*\n"
        "━━━━━━━━━━━━━━━━━━━━━\n\n"
        "Send the **source channel** link or username:\n\n"
        "• `@username`\n"
        "• `https://t.me/username`\n"
        "• `https://t.me/+invitehash`  ← private\n"
        "• `-100xxxxxxxxxx`  ← numeric ID\n\n"
        "_The bot will join automatically if needed._",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=_cancel_kb(),
    )
    return SRC


async def fw_src_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Received source link text."""
    uid = update.effective_user.id
    text = update.message.text.strip()
    session = get_session(uid)

    if not session:
        await update.message.reply_text(f"{E['error']} Session expired. Use /start.")
        return ConversationHandler.END

    wait = await update.message.reply_text(f"{E['refresh']} Resolving source channel…")
    try:
        info = await resolve_and_join_channel(session, text)
    except ValueError as e:
        await wait.edit_text(
            f"{str(e)}\n\n_Try again or tap Cancel._",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=_cancel_kb(),
        )
        return SRC
    except Exception as e:
        await wait.edit_text(
            f"{E['error']} `{e}`\n\n_Try again._",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=_cancel_kb(),
        )
        return SRC

    ctx.user_data["fw"]["source_id"] = info["id"]
    ctx.user_data["fw"]["source_title"] = info["title"]
    ctx.user_data["fw"]["source_ref"] = text  # keep original for peer fallback
    await wait.delete()

    await update.message.reply_text(
        f"{E['forward']} *Forward — Step 2/7  ·  Start Message Link*\n"
        "━━━━━━━━━━━━━━━━━━━━━\n"
        f"✅ Source: `{info['title']}`\n\n"
        "📎 *Paste the link of the FIRST message to forward:*\n\n"
        "• `https://t.me/c/1234567890/100` ← private channel\n"
        "• `https://t.me/username/100` ← public channel\n\n"
        "_Right-click any message in Telegram → Copy Message Link_",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=_cancel_kb(),
    )
    return MSG


# ─────────────────────────  Step 2: Message Link(s)  ─────────────────────────

async def fw_msg(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Step 2 — Parse the single start message link."""
    text = update.message.text.strip()
    fw = ctx.user_data["fw"]

    parts = text.split()
    _, id1 = _parse_msg_link(parts[0]) if parts else (None, None)

    if not id1:
        await update.message.reply_text(
            f"{E['warn']} Couldn't read that message link.\n\n"
            "Please send a valid Telegram message link like:\n"
            "`https://t.me/c/1234567890/123`\n\n"
            "_Right-click any message in Telegram → Copy Message Link_",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=_cancel_kb(),
        )
        return MSG

    fw["start_id"] = id1
    fw["msg_link_raw"] = text

    await update.message.reply_text(
        f"{E['forward']} *Forward — Step 3/7  ·  Number of Messages*\n"
        "━━━━━━━━━━━━━━━━━━━━━\n"
        f"✅ Source: `{fw['source_title']}`\n"
        f"✅ Start message ID: `{id1}`\n\n"
        "🔢 *How many messages to forward?*\n"
        "Send a number (1 – 1000).\n\n"
        "_Example: `10` → forwards the linked message + 9 more after it (10 total)._",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=_cancel_kb(),
    )
    return COUNT


# ─────────────────────────  Step 3: Count  ───────────────────────────────────

async def fw_count(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Step 3 — Receive the total count of messages to forward."""
    text = update.message.text.strip()
    fw = ctx.user_data["fw"]

    if not text.isdigit() or int(text) < 1:
        await update.message.reply_text(
            f"{E['warn']} Please enter a valid number between 1 and {config.MAX_FORWARD}.",
            reply_markup=_cancel_kb(),
        )
        return COUNT

    count = int(text)
    if count > config.MAX_FORWARD:
        count = config.MAX_FORWARD
        await update.message.reply_text(
            f"{E['warn']} Capped to maximum `{config.MAX_FORWARD}` messages.",
            parse_mode=ParseMode.MARKDOWN,
        )

    start_id = fw["start_id"]
    end_id = start_id + count - 1
    fw["end_id"] = end_id
    total = end_id - start_id + 1

    await update.message.reply_text(
        f"{E['forward']} *Forward — Step 4/7  ·  Caption*\n"
        "━━━━━━━━━━━━━━━━━━━━━\n"
        f"Source: `{fw['source_title']}`\n"
        f"Messages: `{start_id}` → `{end_id}` (*{total}* msg{'s' if total > 1 else ''})\n\n"
        "✏️ *Custom caption for videos / photos / documents?*\n"
        "Send new caption text, or **Skip** to keep originals.\n\n"
        "_Text-only messages are always forwarded unchanged._",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=_skip_cancel_kb("fw_skip_cap"),
    )
    return CAPTION


# ─────────────────────────  Step 3: Caption  ─────────────────────────────────

async def fw_caption(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data["fw"]["caption"] = update.message.text.strip()
    await _ask_thumb(update.message, ctx, new=True)
    return THUMB


async def fw_skip_cap(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    ctx.user_data["fw"]["caption"] = None
    await _ask_thumb(update.callback_query.message, ctx)
    return THUMB


# ─────────────────────────  Step 4: Thumbnail  ───────────────────────────────

async def _ask_thumb(message, ctx, new=False):
    fw = ctx.user_data["fw"]
    cap_str = f"`{fw['caption'][:60]}`" if fw.get("caption") else "_keep original_"
    text = (
        f"{E['forward']} *Forward — Step 5/7  ·  Thumbnail*\n"
        "━━━━━━━━━━━━━━━━━━━━━\n"
        f"Caption: {cap_str}\n\n"
        "🖼️ Send a **photo** to replace thumbnails on videos & documents,\n"
        "or **Skip** to keep originals."
    )
    kb = _skip_cancel_kb("fw_skip_thumb")
    try:
        if new:
            await message.reply_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)
        else:
            await message.edit_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)
    except Exception:
        await message.reply_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)


async def fw_thumb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """User sent a photo for thumbnail."""
    # Use smallest resolution available [0] to strictly meet Pyrogram's 320x320/200kb limit
    photo = update.message.photo[0]
    wait = await update.message.reply_text("🔄 Saving thumbnail…")
    try:
        f = await photo.get_file()
        tmp = tempfile.NamedTemporaryFile(suffix=".jpg", delete=False)
        tmp.close()
        await f.download_to_drive(tmp.name)
        ctx.user_data["fw"]["thumbnail_path"] = tmp.name
    except Exception:
        ctx.user_data["fw"]["thumbnail_path"] = None
    await wait.delete()
    await _ask_dst(update.message, ctx, new=True)
    return DST


async def fw_skip_thumb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    ctx.user_data["fw"]["thumbnail_path"] = None
    await _ask_dst(update.callback_query.message, ctx)
    return DST


# ─────────────────────────  Step 5: Destination(s)  ─────────────────────────

async def _ask_dst(message, ctx, new=False):
    fw = ctx.user_data["fw"]
    dests = fw["destinations"]
    dests_str = ("*Added:*  " + ", ".join(f"`{d['title'][:20]}`" for d in dests)) if dests else "_None yet_"
    text = (
        f"{E['forward']} *Forward — Step 6/7  ·  Destination*\n"
        "━━━━━━━━━━━━━━━━━━━━━\n"
        f"Source: `{fw['source_title']}`\n\n"
        "📥 Where to forward *to*?\n"
        "Tap **➕ Add Channel/Group** and send the link.\n\n"
        f"{dests_str}"
    )
    kb = _dst_kb(dests)
    try:
        if new:
            await message.reply_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)
        else:
            await message.edit_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)
    except Exception:
        await message.reply_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)


async def fw_dst_add(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    await update.callback_query.edit_message_text(
        f"{E['forward']} *Forward — Step 5/6  ·  Add Destination*\n"
        "━━━━━━━━━━━━━━━━━━━━━\n"
        "Send the destination channel/group link:\n\n"
        "• `@username`\n"
        "• `https://t.me/username`\n"
        "• `https://t.me/+invitehash`\n"
        "• `-100xxxxxxxxxx`",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=_cancel_kb(),
    )
    return DST


async def fw_dst_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Received destination link text."""
    uid = update.effective_user.id
    text = update.message.text.strip()
    session = get_session(uid)

    if not session:
        await update.message.reply_text(f"{E['error']} Session expired. Use /start.")
        return ConversationHandler.END

    wait = await update.message.reply_text(f"{E['refresh']} Resolving destination…")
    try:
        info = await resolve_and_join_channel(session, text)
    except ValueError as e:
        await wait.edit_text(
            f"{str(e)}\n\n_Try again or tap Cancel._",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=_cancel_kb(),
        )
        return DST
    except Exception as e:
        await wait.edit_text(
            f"{E['error']} `{e}`\n\n_Try again._",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=_cancel_kb(),
        )
        return DST

    dests = ctx.user_data["fw"]["destinations"]
    if not any(d["id"] == info["id"] for d in dests):
        # Store username for peer fallback resolution
        info.setdefault("ref", text)
        dests.append(info)

    await wait.delete()
    await _ask_dst(update.message, ctx, new=True)
    return DST


async def fw_dst_rm(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer("Removed")
    ch_id = int(update.callback_query.data.split(":")[1])
    dests = ctx.user_data["fw"]["destinations"]
    ctx.user_data["fw"]["destinations"] = [d for d in dests if d["id"] != ch_id]
    await _ask_dst(update.callback_query.message, ctx)
    return DST


async def fw_dst_done(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    fw = ctx.user_data["fw"]
    if not fw["destinations"]:
        await update.callback_query.answer("⚠️ Add at least one destination first!", show_alert=True)
        return DST
    await _show_confirm(update.callback_query.message, ctx)
    return ConversationHandler.END


# ─────────────────────────  Step 6: Confirm  ─────────────────────────────────

async def _show_confirm(message, ctx, new=False):
    fw = ctx.user_data["fw"]
    dst_text = "\n".join(f"  • `{d['title'][:40]}`" for d in fw["destinations"])
    cap_str  = f"`{fw['caption'][:60]}`" if fw.get("caption") else "_keep original_"
    thm_str  = "✅ Custom photo" if fw.get("thumbnail_path") else "_keep original_"
    total    = fw["end_id"] - fw["start_id"] + 1
    text = (
        f"{E['forward']} *Confirm Forward Job*\n"
        "━━━━━━━━━━━━━━━━━━━━━\n"
        f"📤 *Source:* `{fw['source_title']}`\n"
        f"📋 *Messages:* `{fw['start_id']}` → `{fw['end_id']}` (*{total}* msgs)\n"
        f"✏️ *Caption:* {cap_str}\n"
        f"🖼️ *Thumbnail:* {thm_str}\n"
        f"📥 *Destinations:*\n{dst_text}\n"
        "━━━━━━━━━━━━━━━━━━━━━\n"
        "Tap **✅ Confirm** to start forwarding!"
    )
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Confirm & Start", callback_data="fw_confirm")],
        [InlineKeyboardButton(f"{E['stop']} Cancel", callback_data="fw_cancel")],
    ])
    try:
        if new:
            await message.reply_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)
        else:
            await message.edit_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)
    except Exception:
        await message.reply_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)


# ─────────────────────────  Confirm → Run  ───────────────────────────────────

async def fw_confirm(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await _require_admin(update, ctx):
        return
    uid = update.effective_user.id
    session = get_session(uid)
    fw = ctx.user_data.get("fw")

    if not fw or not session:
        await update.callback_query.answer("Session lost. Use /start.", show_alert=True)
        return
    if not fw.get("source_id") or not fw.get("destinations"):
        await update.callback_query.answer("Missing source or destinations!", show_alert=True)
        return

    await update.callback_query.answer("🚀 Starting…")

    total = fw["end_id"] - fw["start_id"] + 1
    task_id = create_task(
        admin_id=uid,
        source=str(fw["source_id"]),
        destinations=[str(d["id"]) for d in fw["destinations"]],
        caption=fw.get("caption"),
        start_id=fw["start_id"],
        end_id=fw["end_id"],
    )

    def _prog_text(done, err, status=""):
        bar = progress_bar(done, total)
        dst = ", ".join(f"`{d['title'][:15]}`" for d in fw["destinations"])
        return (
            f"{E['forward']} *Forwarding…*\n"
            "━━━━━━━━━━━━━━━━━━━━━\n"
            f"📤 Source: `{fw['source_title']}`\n"
            f"📥 To: {dst}\n\n"
            f"`{bar}` {pct(done, total)}\n"
            f"✅ Done: *{done}* / {total}\n"
            f"❌ Errors: *{err}*\n"
            + (f"\n⚡ _{status}_\n" if status else "")
        )

    progress_msg = await update.callback_query.edit_message_text(
        _prog_text(0, 0, "Warming up…"),
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton(f"{E['stop']} Stop", callback_data=f"fw_stop:{task_id}")]
        ]),
    )

    stop_ev = asyncio.Event()
    _stop_events[task_id] = stop_ev

    last_update_time = time.time() - 3.0

    async def on_progress(done, total_, errors, status_str=""):
        nonlocal last_update_time
        update_task_progress(task_id, done)
        now = time.time()
        # Time-throttle UI edits to 2 seconds to avoid Telegram FloodWait
        if now - last_update_time >= 2.0 or done == total_:
            try:
                await progress_msg.edit_text(
                    _prog_text(done, errors, status_str if status_str else pct(done, total_)),
                    parse_mode=ParseMode.MARKDOWN,
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton(f"{E['stop']} Stop", callback_data=f"fw_stop:{task_id}")]
                    ]),
                )
                last_update_time = now
            except Exception:
                pass

    try:
        result = await forward_messages(
            session_string=session,
            source=fw["source_id"],
            destinations=[d["id"] for d in fw["destinations"]],
            start_id=fw["start_id"],
            end_id=fw["end_id"],
            caption=fw.get("caption"),
            thumbnail_path=fw.get("thumbnail_path"),
            progress_cb=on_progress,
            stop_event=stop_ev,
            source_ref=fw.get("source_ref"),
            dest_refs=[d.get("ref", d.get("username", "")) for d in fw["destinations"]],
        )
        stopped = stop_ev.is_set()
        finish_task(task_id, status="stopped" if stopped else "done")
        _stop_events.pop(task_id, None)

        # Clean up thumbnail temp file
        tpath = fw.get("thumbnail_path")
        if tpath:
            try:
                os.unlink(tpath)
            except Exception:
                pass

        fwd = result.get("forwarded", 0)
        err = result.get("errors", 0)
        skp = result.get("skipped", 0)
        dst = ", ".join(f"`{d['title'][:20]}`" for d in fw["destinations"])
        status_icon = "⏹" if stopped else ("✅" if err == 0 else "⚠️")
        final = (
            f"{status_icon} *Forward {'Stopped' if stopped else 'Complete'}*\n"
            "━━━━━━━━━━━━━━━━━━━━━\n"
            f"📤 Source: `{fw['source_title']}`\n"
            f"📥 To: {dst}\n\n"
            f"✅ Forwarded: *{fwd}*\n"
            f"❌ Errors: *{err}*\n"
            f"⏭️ Skipped (empty/deleted): *{skp}*\n"
        )
        if result.get("last_error"):
            final += f"\n⚠️ Last error: `{result['last_error'][:80]}`\n"

        await progress_msg.edit_text(
            final,
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔁 Forward Again", callback_data="fw_start")],
                [InlineKeyboardButton(f"{E['back']} Home", callback_data="home")],
            ]),
        )

    except Exception as e:
        finish_task(task_id, status="error", error=str(e))
        _stop_events.pop(task_id, None)
        await progress_msg.edit_text(
            f"❌ *Forward Failed*\n\n`{str(e)[:200]}`",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=back_kb("home"),
        )


# ─────────────────────────  Stop handler  ────────────────────────────────────

async def fw_stop(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer("⏹ Stopping…")
    task_id = update.callback_query.data.split(":", 1)[1]
    ev = _stop_events.get(task_id)
    if ev:
        ev.set()


# ─────────────────────────  Cancel  ──────────────────────────────────────────

async def fw_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer("Cancelled")
    ctx.user_data.pop("fw", None)
    ctx.user_data.pop("_fw_mode", None)
    await update.callback_query.edit_message_text(
        f"{E['stop']} Forward cancelled.",
        reply_markup=back_kb("home"),
    )
    return ConversationHandler.END


# ─────────────────────────  Register  ────────────────────────────────────────

def register(app):
    text_filter = filters.TEXT & ~filters.COMMAND

    conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(fw_start, pattern="^fw_start$")],
        states={
            SRC: [
                MessageHandler(text_filter, fw_src_text),
                CallbackQueryHandler(fw_cancel, pattern="^fw_cancel$"),
            ],
            MSG: [
                MessageHandler(text_filter, fw_msg),
                CallbackQueryHandler(fw_cancel, pattern="^fw_cancel$"),
            ],
            COUNT: [
                MessageHandler(text_filter, fw_count),
                CallbackQueryHandler(fw_cancel, pattern="^fw_cancel$"),
            ],
            CAPTION: [
                CallbackQueryHandler(fw_skip_cap, pattern="^fw_skip_cap$"),
                CallbackQueryHandler(fw_cancel, pattern="^fw_cancel$"),
                MessageHandler(text_filter, fw_caption),
            ],
            THUMB: [
                CallbackQueryHandler(fw_skip_thumb, pattern="^fw_skip_thumb$"),
                CallbackQueryHandler(fw_cancel, pattern="^fw_cancel$"),
                MessageHandler(filters.PHOTO, fw_thumb),
                MessageHandler(text_filter, lambda u, c: u.message.reply_text(
                    "📸 Please send a *photo* as thumbnail, or tap **Skip**.",
                    parse_mode=ParseMode.MARKDOWN,
                    reply_markup=_skip_cancel_kb("fw_skip_thumb"),
                )),
            ],
            DST: [
                CallbackQueryHandler(fw_dst_add, pattern="^fw_dst_add$"),
                CallbackQueryHandler(fw_dst_rm, pattern=r"^fw_dst_rm:-?\d+$"),
                CallbackQueryHandler(fw_dst_done, pattern="^fw_dst_done$"),
                CallbackQueryHandler(fw_cancel, pattern="^fw_cancel$"),
                MessageHandler(text_filter, fw_dst_text),
            ],
        },
        fallbacks=[CallbackQueryHandler(fw_cancel, pattern="^fw_cancel$")],
        allow_reentry=True,
    )
    app.add_handler(conv)

    # Handlers outside conversation (confirm + stop)
    app.add_handler(CallbackQueryHandler(fw_confirm, pattern="^fw_confirm$"))
    app.add_handler(CallbackQueryHandler(fw_stop, pattern=r"^fw_stop:"))
