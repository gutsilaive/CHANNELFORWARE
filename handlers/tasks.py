"""
handlers/tasks.py — View task history with live status & errors
"""
import math
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes, CallbackQueryHandler
from telegram.constants import ParseMode

from database import get_tasks
from handlers.ui import E, back_kb, progress_bar, pct
from handlers.start import _require_admin

PAGE_SIZE = 5

STATUS_ICONS = {
    "pending":  "⏳",
    "running":  "🔄",
    "done":     "✅",
    "stopped":  "⏹",
    "error":    "❌",
}


def _task_list_kb(tasks: list[dict], page: int) -> InlineKeyboardMarkup:
    total_pages = max(1, math.ceil(len(tasks) / PAGE_SIZE))
    start = page * PAGE_SIZE
    items = tasks[start: start + PAGE_SIZE]
    rows = []
    for t in items:
        icon = STATUS_ICONS.get(t["status"], "❓")
        src = (t.get("source") or "?")[:15]
        label = f"{icon} [{t['status'].upper()}] {src} → {t['forwarded']}/{t['total']}"
        rows.append([InlineKeyboardButton(label, callback_data=f"task_detail:{t['id']}")])

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀️ Prev", callback_data=f"task_page:{page - 1}"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton("Next ▶️", callback_data=f"task_page:{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append([
        InlineKeyboardButton(f"{E['refresh']} Refresh", callback_data="task_list"),
        InlineKeyboardButton(f"{E['back']} Back", callback_data="home"),
    ])
    return InlineKeyboardMarkup(rows)


async def task_list(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await _require_admin(update, ctx):
        return
    uid = update.effective_user.id
    await update.callback_query.answer()

    tasks = get_tasks(uid, limit=50)
    ctx.user_data["_tasks"] = tasks

    if not tasks:
        await update.callback_query.edit_message_text(
            f"{E['tasks']} *Task History*\n\n_No tasks yet. Start a forward job from the main menu._",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=back_kb("home"),
        )
        return

    running = sum(1 for t in tasks if t["status"] == "running")
    done = sum(1 for t in tasks if t["status"] == "done")
    errors = sum(1 for t in tasks if t["status"] == "error")

    text = (
        f"{E['tasks']} *Task History*\n"
        "━━━━━━━━━━━━━━━━━━━━━\n"
        f"🔄 Running: *{running}*   ✅ Done: *{done}*   ❌ Errors: *{errors}*\n\n"
        "_Tap a task to view details._"
    )
    await update.callback_query.edit_message_text(
        text, parse_mode=ParseMode.MARKDOWN,
        reply_markup=_task_list_kb(tasks, 0),
    )


async def task_page(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    page = int(update.callback_query.data.split(":")[1])
    tasks = ctx.user_data.get("_tasks", [])
    await update.callback_query.edit_message_reply_markup(
        reply_markup=_task_list_kb(tasks, page)
    )


async def task_detail(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    task_id = update.callback_query.data.split(":", 1)[1]
    tasks = ctx.user_data.get("_tasks", [])
    task = next((t for t in tasks if t["id"] == task_id), None)
    if not task:
        await update.callback_query.answer("Task not found", show_alert=True)
        return

    icon = STATUS_ICONS.get(task["status"], "❓")
    bar = progress_bar(task["forwarded"], task["total"])
    dests = task.get("destinations") or []
    dst_str = "\n".join(f"  • `{d}`" for d in (dests if isinstance(dests, list) else [dests]))
    cap = task.get("caption") or "_original_"
    err = task.get("error") or "_none_"

    text = (
        f"{icon} *Task Details*\n"
        "━━━━━━━━━━━━━━━━━━━━━\n"
        f"🆔 ID: `{task['id'][:12]}…`\n"
        f"📌 Status: *{task['status'].upper()}*\n"
        f"{E['channel']} Source: `{task.get('source', '?')}`\n"
        f"{E['plus']} Destinations:\n{dst_str}\n"
        f"{E['clock']} Range: `{task.get('start_msg_id')}` → `{task.get('end_msg_id')}`\n"
        f"✏️ Caption: {cap[:80]}\n\n"
        f"📊 Progress:\n`{bar}` {pct(task['forwarded'], task['total'])}\n"
        f"✅ Forwarded: *{task['forwarded']}* / {task['total']}\n"
        f"❌ Error: {err[:120]}\n"
        f"\n_Created: {str(task.get('created_at', ''))[:16]}_"
    )
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton(f"{E['back']} Back to List", callback_data="task_list")]
    ])
    await update.callback_query.edit_message_text(
        text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb
    )


def register(app):
    app.add_handler(CallbackQueryHandler(task_list, pattern="^task_list$"))
    app.add_handler(CallbackQueryHandler(task_page, pattern=r"^task_page:\d+$"))
    app.add_handler(CallbackQueryHandler(task_detail, pattern=r"^task_detail:"))
