"""
handlers/forward.py
Full forward conversation flow:
  Step 1: Pick source channel (from list or type link)
  Step 2: Add destination channels
  Step 3: Set message range (count or start/end)
  Step 4: Optional custom caption
  Step 5: Optional thumbnail photo
  Step 6: Confirm → live progress
"""
from __future__ import annotations
import asyncio
import math
import os
import tempfile

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ContextTypes, ConversationHandler, CommandHandler,
    CallbackQueryHandler, MessageHandler, filters,
)
from telegram.constants import ParseMode

from database import get_session, create_task, update_task_progress, finish_task
from userbot import get_joined_channels, resolve_and_join_channel, forward_messages, get_latest_message_id
from handlers.ui import E, back_kb, progress_bar, pct
from handlers.start import _require_admin
import config

# ── States ────────────────────────────────────────────────────────────────────
SRC, DST, RANGE, CAPTION, THUMB = range(5)

# ── Per-user stop events ──────────────────────────────────────────────────────
_stop_events: dict[str, asyncio.Event] = {}

PAGE = 8


# ─────────────────────────  Keyboards  ───────────────────────────────────────

def _src_kb(channels: list[dict], page: int) -> InlineKeyboardMarkup:
    total = max(1, math.ceil(len(channels) / PAGE))
    rows = []
    for ch in channels[page * PAGE: page * PAGE + PAGE]:
        # callback_data must be < 64 bytes — only store the ID
        rows.append([InlineKeyboardButton(
            f"📢 {ch['title'][:30]}",
            callback_data=f"fw_src:{ch['id']}"
        )])
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀️ Prev", callback_data=f"fw_srcpg:{page-1}"))
    if page < total - 1:
        nav.append(InlineKeyboardButton("Next ▶️", callback_data=f"fw_srcpg:{page+1}"))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton("✏️ Type link/username", callback_data="fw_src_manual")])
    rows.append([InlineKeyboardButton(f"{E['stop']} Cancel", callback_data="fw_cancel")])
    return InlineKeyboardMarkup(rows)


def _dst_kb(dests: list[dict]) -> InlineKeyboardMarkup:
    rows = []
    for d in dests:
        rows.append([InlineKeyboardButton(
            f"✅ {d['title'][:30]} (tap to remove)",
            callback_data=f"fw_dst_rm:{d['id']}"
        )])
    rows.append([InlineKeyboardButton("➕ Add Channel/Group", callback_data="fw_dst_add")])
    if dests:
        rows.append([InlineKeyboardButton(
            f"✔️ Done ({len(dests)} selected)",
            callback_data="fw_dst_done"
        )])
    rows.append([InlineKeyboardButton(f"{E['stop']} Cancel", callback_data="fw_cancel")])
    return InlineKeyboardMarkup(rows)


def _cancel_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"{E['stop']} Cancel", callback_data="fw_cancel")]
    ])


def _skip_cancel_kb(skip_cb: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⏭️ Skip", callback_data=skip_cb)],
        [InlineKeyboardButton(f"{E['stop']} Cancel", callback_data="fw_cancel")],
    ])


# ─────────────────────────  Step 1: Source  ──────────────────────────────────

async def fw_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
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

    # Use shared channel cache
    from handlers.channels import _get_cached, _set_cache
    channels = _get_cached(ctx, uid)
    if not channels:
        msg = await update.callback_query.edit_message_text(
            f"{E['refresh']} Loading channels… (first time ~15s)"
        )
        try:
            channels = await get_joined_channels(session)
            _set_cache(ctx, uid, channels)
        except Exception as e:
            await msg.edit_text(
                f"{E['error']} Could not load channels:\n`{e}`",
                parse_mode=ParseMode.MARKDOWN, reply_markup=back_kb("home"),
            )
            return ConversationHandler.END

    # Init state
    ctx.user_data["fw"] = {
        "source_id": None, "source_title": None,
        "destinations": [],
        "start_id": None, "end_id": None,
        "caption": None, "thumbnail_path": None,
    }
    ctx.user_data["fw_channels"] = channels

    text = (
        f"{E['forward']} *Forward — Step 1/5  · Pick Source*\n"
        "━━━━━━━━━━━━━━━━━━━━━\n"
        "Select which channel to forward *from*:"
    )
    try:
        await (msg if channels and not _get_cached(ctx, uid) else update.callback_query).edit_message_text(
            text, parse_mode=ParseMode.MARKDOWN, reply_markup=_src_kb(channels, 0)
        )
    except Exception:
        await update.callback_query.edit_message_text(
            text, parse_mode=ParseMode.MARKDOWN, reply_markup=_src_kb(channels, 0)
        )
    return SRC


async def fw_srcpg(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    page = int(update.callback_query.data.split(":")[1])
    channels = ctx.user_data.get("fw_channels", [])
    try:
        await update.callback_query.edit_message_reply_markup(
            reply_markup=_src_kb(channels, page)
        )
    except Exception:
        pass
    return SRC


async def fw_src_pick(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Tapped a channel button as source — look up title from cache."""
    await update.callback_query.answer()
    parts = update.callback_query.data.split(":")
    ch_id = int(parts[1])
    # Look up title from cached channels
    channels = ctx.user_data.get("fw_channels", [])
    ch = next((c for c in channels if c["id"] == ch_id), None)
    ctx.user_data["fw"]["source_id"] = ch_id
    ctx.user_data["fw"]["source_title"] = ch["title"] if ch else str(ch_id)
    await _show_dst(update.callback_query.message, ctx)
    return DST


async def fw_src_manual(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Type source channel manually."""
    await update.callback_query.answer()
    await update.callback_query.edit_message_text(
        f"{E['forward']} *Forward — Step 1/5  · Source*\n"
        "━━━━━━━━━━━━━━━━━━━━━\n"
        "Send the source channel link or username:\n\n"
        "• `@username`\n"
        "• `https://t.me/username`\n"
        "• `https://t.me/+invitehash`\n"
        "• `-100xxxxxxxxxx`",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=_cancel_kb(),
    )
    ctx.user_data["_fw_mode"] = "src"
    return SRC


async def fw_src_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Text received in SRC or DST state."""
    uid = update.effective_user.id
    text = update.message.text.strip()
    session = get_session(uid)
    mode = ctx.user_data.get("_fw_mode", "dst")

    if not session:
        await update.message.reply_text(f"{E['error']} Session expired. Use /start.")
        return ConversationHandler.END

    wait = await update.message.reply_text(f"{E['refresh']} Resolving…")
    try:
        info = await resolve_and_join_channel(session, text)
    except ValueError as e:
        await wait.edit_text(f"{str(e)}\n\n_Try again or tap Cancel._", parse_mode=ParseMode.MARKDOWN)
        return SRC if mode == "src" else DST
    except Exception as e:
        await wait.edit_text(f"{E['error']} `{e}`", parse_mode=ParseMode.MARKDOWN)
        return SRC if mode == "src" else DST

    if mode == "src":
        ctx.user_data["fw"]["source_id"] = info["id"]
        ctx.user_data["fw"]["source_title"] = info["title"]
        ctx.user_data.pop("_fw_mode", None)
        await wait.delete()
        await _show_dst(update.message, ctx, new=True)
        return DST
    else:  # dst
        dests = ctx.user_data["fw"]["destinations"]
        if not any(d["id"] == info["id"] for d in dests):
            dests.append(info)
        ctx.user_data.pop("_fw_mode", None)
        await wait.delete()
        await _show_dst(update.message, ctx, new=True)
        return DST


# ─────────────────────────  Step 2: Destinations  ────────────────────────────

async def _show_dst(message, ctx, new=False):
    fw = ctx.user_data["fw"]
    dests = fw["destinations"]
    text = (
        f"{E['forward']} *Forward — Step 2/5  · Destinations*\n"
        "━━━━━━━━━━━━━━━━━━━━━\n"
        f"Source: `{fw['source_title']}`\n\n"
        f"Add the channel(s)/group(s) to forward *to*.\n"
        f"{'*Added:*  ' + ', '.join(f'`{d[\"title\"][:20]}`' for d in dests) if dests else '_None added yet_'}"
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
        f"{E['forward']} *Forward — Step 2/5  · Add Destination*\n"
        "━━━━━━━━━━━━━━━━━━━━━\n"
        "Send the destination channel/group link:\n\n"
        "• `@username`\n"
        "• `https://t.me/username`\n"
        "• `https://t.me/+invitehash`\n"
        "• `-100xxxxxxxxxx`\n\n"
        "_The user account will auto-join if not already a member._",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=_cancel_kb(),
    )
    ctx.user_data["_fw_mode"] = "dst"
    return DST


async def fw_dst_rm(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer("Removed")
    ch_id = int(update.callback_query.data.split(":")[1])
    dests = ctx.user_data["fw"]["destinations"]
    ctx.user_data["fw"]["destinations"] = [d for d in dests if d["id"] != ch_id]
    await _show_dst(update.callback_query.message, ctx)
    return DST


async def fw_dst_done(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    fw = ctx.user_data["fw"]
    if not fw["destinations"]:
        await update.callback_query.answer("⚠️ Add at least one destination first!", show_alert=True)
        return DST
    await update.callback_query.edit_message_text(
        f"{E['forward']} *Forward — Step 3/5  · Message Range*\n"
        "━━━━━━━━━━━━━━━━━━━━━\n"
        f"Source: `{fw['source_title']}`\n"
        f"Destinations: *{len(fw['destinations'])}* channel(s)\n\n"
        "📋 *How many messages to forward?*\n\n"
        "Send one of:\n"
        "• `100` — messages 1 to 100\n"
        "• `50 200` — messages 50 to 200\n"
        "• `50-200` — same as above\n\n"
        f"_Max: `{config.MAX_FORWARD}` per job._",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=_cancel_kb(),
    )
    return RANGE


async def fw_dst_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Text typed during DST state — treat as destination channel input."""
    ctx.user_data["_fw_mode"] = "dst"
    return await fw_src_text(update, ctx)


# ─────────────────────────  Step 3: Range  ───────────────────────────────────

async def fw_range(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    raw = update.message.text.strip().replace(",", " ").replace("-", " ")
    parts = [p for p in raw.split() if p.isdigit()]

    uid = update.effective_user.id
    session = get_session(uid)
    fw = ctx.user_data["fw"]

    if len(parts) == 1:
        # User gave a count — resolve to the LAST N actual messages
        count = int(parts[0])
        wait = await update.message.reply_text(f"{E['refresh']} Checking latest message ID…")
        latest = await get_latest_message_id(session, fw["source_id"])
        await wait.delete()
        if not latest:
            await update.message.reply_text(
                f"{E['error']} Could not fetch latest message. Try using `start end` format (e.g. `1000 1100`).",
                parse_mode=ParseMode.MARKDOWN,
            )
            return RANGE
        end_id = latest
        start_id = max(1, latest - count + 1)
    elif len(parts) >= 2:
        start_id, end_id = int(parts[0]), int(parts[1])
    else:
        await update.message.reply_text(
            f"{E['warn']} Send a number like `100` (last 100 msgs) or range like `1000 1100`.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return RANGE

    if start_id > end_id:
        start_id, end_id = end_id, start_id

    cap = config.MAX_FORWARD
    if end_id - start_id + 1 > cap:
        start_id = end_id - cap + 1
        await update.message.reply_text(
            f"{E['warn']} Capped to `{cap}` messages. Start adjusted to `{start_id}`.",
            parse_mode=ParseMode.MARKDOWN,
        )

    ctx.user_data["fw"]["start_id"] = start_id
    ctx.user_data["fw"]["end_id"] = end_id

    await update.message.reply_text(
        f"{E['forward']} *Forward — Step 4/5  · Caption*\n"
        "━━━━━━━━━━━━━━━━━━━━━\n"
        f"Source: `{fw['source_title']}`\n"
        f"Range: `{start_id}` → `{end_id}` (*{end_id-start_id+1}* messages)\n\n"
        "✏️ *Custom Caption (for media)*\n"
        "Send new caption text, or *Skip* to keep originals.\n"
        "_Text messages are always forwarded unchanged._",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=_skip_cancel_kb("fw_skip_cap"),
    )
    return CAPTION


# ─────────────────────────  Step 4: Caption  ─────────────────────────────────

async def fw_caption(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data["fw"]["caption"] = update.message.text.strip()
    await _ask_thumb(update.message, ctx, new=True)
    return THUMB


async def fw_skip_cap(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    ctx.user_data["fw"]["caption"] = None
    await _ask_thumb(update.callback_query.message, ctx)
    return THUMB


# ─────────────────────────  Step 5: Thumbnail  ───────────────────────────────

async def _ask_thumb(message, ctx, new=False):
    fw = ctx.user_data["fw"]
    cap_str = f"`{fw['caption'][:50]}`" if fw.get("caption") else "_keep original_"
    text = (
        f"{E['forward']} *Forward — Step 5/5  · Thumbnail*\n"
        "━━━━━━━━━━━━━━━━━━━━━\n"
        f"Caption: {cap_str}\n\n"
        "🖼️ Send a **photo** to replace video thumbnails,\n"
        "or *Skip* to keep original thumbnails."
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
    """User sent a photo as thumbnail."""
    photo = update.message.photo[-1]
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
    await _show_confirm(update.message, ctx, new=True)
    return ConversationHandler.END


async def fw_skip_thumb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    ctx.user_data["fw"]["thumbnail_path"] = None
    await _show_confirm(update.callback_query.message, ctx)
    return ConversationHandler.END


# ─────────────────────────  Confirm screen  ──────────────────────────────────

async def _show_confirm(message, ctx, new=False):
    fw = ctx.user_data["fw"]
    dst_text = "\n".join(f"  • `{d['title'][:40]}`" for d in fw["destinations"])
    cap_str  = f"`{fw['caption'][:60]}`" if fw.get("caption") else "_keep original_"
    thm_str  = "✅ Custom photo set" if fw.get("thumbnail_path") else "_keep original_"
    total    = fw["end_id"] - fw["start_id"] + 1
    text = (
        f"{E['forward']} *Confirm Forward Job*\n"
        "━━━━━━━━━━━━━━━━━━━━━\n"
        f"📤 *Source:* `{fw['source_title']}`\n"
        f"📥 *Destinations:*\n{dst_text}\n"
        f"📋 *Range:* `{fw['start_id']}` → `{fw['end_id']}` (*{total}* msgs)\n"
        f"✏️ *Caption:* {cap_str}\n"
        f"🖼️ *Thumbnail:* {thm_str}\n"
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
            + (f"\n_{status}_" if status else "")
        )

    progress_msg = await update.callback_query.edit_message_text(
        _prog_text(0, 0, "Starting…"),
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton(f"{E['stop']} Stop", callback_data=f"fw_stop:{task_id}")]
        ]),
    )

    stop_ev = asyncio.Event()
    _stop_events[task_id] = stop_ev

    async def on_progress(done, total_, errors):
        update_task_progress(task_id, done)
        try:
            await progress_msg.edit_text(
                _prog_text(done, errors, pct(done, total_)),
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton(f"{E['stop']} Stop", callback_data=f"fw_stop:{task_id}")]
                ]),
            )
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
        )
        stopped = stop_ev.is_set()
        finish_task(task_id, status="stopped" if stopped else "done")
        _stop_events.pop(task_id, None)

        # Clean up thumbnail temp file
        tpath = fw.get("thumbnail_path")
        if tpath:
            try:
                os.remove(tpath)
            except Exception:
                pass

        last_err = result.get("last_error", "")
        final = (
            f"{'⏹' if stopped else '✅'} *Forward {'Stopped' if stopped else 'Complete'}!*\n"
            "━━━━━━━━━━━━━━━━━━━━━\n"
            f"📤 Source: `{fw['source_title']}`\n\n"
            f"✅ Forwarded: *{result['forwarded']}* / {total}\n"
            f"⏭️ Skipped: *{result['skipped']}*\n"
            f"❌ Errors: *{result['errors']}*\n"
            + (f"\n⚠️ Last error: `{last_err[:120]}`" if last_err else "")
        )
        await progress_msg.edit_text(final, parse_mode=ParseMode.MARKDOWN, reply_markup=back_kb("home"))

    except ValueError as e:
        finish_task(task_id, status="error", error=str(e))
        await progress_msg.edit_text(
            f"{E['error']} *Error:*\n{e}", parse_mode=ParseMode.MARKDOWN, reply_markup=back_kb("home")
        )
    except Exception as e:
        finish_task(task_id, status="error", error=str(e))
        await progress_msg.edit_text(
            f"{E['error']} Unexpected error:\n`{e}`", parse_mode=ParseMode.MARKDOWN, reply_markup=back_kb("home")
        )


# ─────────────────────────  Stop  ────────────────────────────────────────────

async def fw_stop(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    task_id = update.callback_query.data.split(":", 1)[1]
    ev = _stop_events.get(task_id)
    if ev:
        ev.set()
        await update.callback_query.answer("⏹ Stopping after current message…")
    else:
        await update.callback_query.answer("Already finished.")


# ─────────────────────────  Cancel  ──────────────────────────────────────────

async def fw_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data.pop("fw", None)
    ctx.user_data.pop("fw_channels", None)
    ctx.user_data.pop("_fw_mode", None)
    if update.callback_query:
        await update.callback_query.answer("Cancelled")
        try:
            await update.callback_query.edit_message_text(
                f"{E['stop']} Cancelled. Use /start to go back."
            )
        except Exception:
            pass
    elif update.message:
        await update.message.reply_text(f"{E['stop']} Cancelled.")
    return ConversationHandler.END


# ─────────────────────────  Register  ────────────────────────────────────────

def register(app):
    conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(fw_start, pattern="^fw_start$")],
        states={
                SRC: [
                CallbackQueryHandler(fw_srcpg,      pattern=r"^fw_srcpg:\d+$"),
                CallbackQueryHandler(fw_src_pick,   pattern=r"^fw_src:-?\d+$"),
                CallbackQueryHandler(fw_src_manual, pattern="^fw_src_manual$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, fw_src_text),
            ],
            DST: [
                CallbackQueryHandler(fw_dst_add,  pattern="^fw_dst_add$"),
                CallbackQueryHandler(fw_dst_rm,   pattern=r"^fw_dst_rm:-?\d+$"),
                CallbackQueryHandler(fw_dst_done, pattern="^fw_dst_done$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, fw_dst_text),
            ],
            RANGE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, fw_range),
            ],
            CAPTION: [
                CallbackQueryHandler(fw_skip_cap, pattern="^fw_skip_cap$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, fw_caption),
            ],
            THUMB: [
                CallbackQueryHandler(fw_skip_thumb, pattern="^fw_skip_thumb$"),
                MessageHandler(filters.PHOTO, fw_thumb),
            ],
        },
        fallbacks=[
            CallbackQueryHandler(fw_cancel, pattern="^fw_cancel$"),
            CallbackQueryHandler(fw_cancel, pattern="^cancel$"),
            CommandHandler("start", fw_cancel),
        ],
        allow_reentry=True,
        per_message=False,
    )
    app.add_handler(conv)
    # These fire AFTER the conversation ends
    app.add_handler(CallbackQueryHandler(fw_confirm, pattern="^fw_confirm$"))
    app.add_handler(CallbackQueryHandler(fw_stop,    pattern=r"^fw_stop:"))
