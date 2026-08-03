# ====================== Automation: schedules, quick commands, server tags ======================

import asyncio
import datetime
import json
import logging
import os
import uuid

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup, Update
from telegram.error import BadRequest
from telegram.ext import ContextTypes, ConversationHandler

from . import engine
from . import settings
import subscription

logger = logging.getLogger(__name__)

# ====================== Conversation states ======================
(
    AUTO_TAG_INPUT,
    AUTO_QC_ADD_LABEL,
    AUTO_QC_ADD_COMMAND,
    AUTO_SCHED_ADD_COMMAND,
    AUTO_SCHED_ADD_TIME,
) = range(5)

CANCEL_BUTTON_TEXT = "❌ Cancel"
RUN_TIMEOUT = 60           # seconds - for scheduled runs, quick-command runs, and "Run Now"
MAX_HISTORY = 15           # per user, most-recent-first
JOB_NAME_PREFIX = "servermgr_sched_"

# Wired up from main.py the same way admin.py and ServerManager/handlers.py get
# their main-menu keyboard (see main.py: set_get_main_menu), so this module
# doesn't need to import main.py.
_get_main_menu_func = None


def set_get_main_menu(func):
    global _get_main_menu_func
    _get_main_menu_func = func


def get_main_menu():
    if _get_main_menu_func:
        return _get_main_menu_func()
    return ReplyKeyboardMarkup([[]], resize_keyboard=True)


def _cancel_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup([[KeyboardButton(CANCEL_BUTTON_TEXT)]], resize_keyboard=True)


async def _edit_then_prompt_cancel(query, text: str):
    try:
        await query.edit_message_text(text)
    except BadRequest as e:
        logger.warning(f"_edit_then_prompt_cancel: edit failed ({e}); sending a new message instead")
        await query.message.reply_text(text)
    await query.message.reply_text("👇 Tap below to cancel:", reply_markup=_cancel_kb())


async def automation_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Fallback for /cancel or the "❌ Cancel" reply-keyboard button while an
    automation conversation (tag / quick command / schedule) is waiting on input."""
    context.user_data.pop("svauto_tmp", None)
    await update.message.reply_text("❌ Operation cancelled.", reply_markup=get_main_menu())
    return ConversationHandler.END


def _uid(update: Update):
    return update.effective_user.id


# ====================== Storage ======================
# Own JSON file, same directory/pattern as settings.py's server_manager_settings.json.
# Nothing stored here is sensitive (no passwords/keys), so - unlike settings.py - it's
# plain JSON, no encryption needed.
STORE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "server_manager_automation.json")
DEFAULT_STORE = {"users": {}}
_cache = None


def _load():
    global _cache
    if _cache is not None:
        return _cache
    data = {}
    if os.path.exists(STORE_FILE):
        try:
            with open(STORE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            logger.error(f"Error loading server_manager_automation.json: {e}")
            data = {}
    merged = {**DEFAULT_STORE, **data}
    _cache = merged
    return merged


def _save(data):
    global _cache
    with open(STORE_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    _cache = data


def _uid_key(user_id) -> str:
    return str(user_id)


def _user_bucket(data, user_id) -> dict:
    users = data.setdefault("users", {})
    bucket = users.setdefault(_uid_key(user_id), {})
    bucket.setdefault("tags", {})
    bucket.setdefault("quick_commands", [])
    bucket.setdefault("history", [])
    bucket.setdefault("scheduled", [])
    return bucket


# ---------------------- Tags ----------------------

def set_tag(user_id, server_id: str, tag: str) -> None:
    data = _load()
    bucket = _user_bucket(data, user_id)
    tag = tag.strip()
    if tag:
        bucket["tags"][server_id] = tag
    else:
        bucket["tags"].pop(server_id, None)
    _save(data)


def get_tag(user_id, server_id: str):
    return _load().get("users", {}).get(_uid_key(user_id), {}).get("tags", {}).get(server_id)


# ---------------------- Quick commands (favorites) ----------------------

def add_quick_command(user_id, server_id: str, label: str, command: str) -> dict:
    data = _load()
    bucket = _user_bucket(data, user_id)
    qc = {"id": uuid.uuid4().hex[:8], "server_id": server_id, "label": label.strip()[:60], "command": command.strip()}
    bucket["quick_commands"].append(qc)
    _save(data)
    return qc


def get_quick_commands(user_id, server_id: str = None) -> list:
    qcs = _load().get("users", {}).get(_uid_key(user_id), {}).get("quick_commands", [])
    return [q for q in qcs if server_id is None or q["server_id"] == server_id]


def get_quick_command(user_id, qc_id: str):
    for q in get_quick_commands(user_id):
        if q["id"] == qc_id:
            return q
    return None


def remove_quick_command(user_id, qc_id: str) -> bool:
    data = _load()
    bucket = _user_bucket(data, user_id)
    before = len(bucket["quick_commands"])
    bucket["quick_commands"] = [q for q in bucket["quick_commands"] if q["id"] != qc_id]
    changed = len(bucket["quick_commands"]) != before
    if changed:
        _save(data)
    return changed


# ---------------------- History ----------------------

def record_history(user_id, server_id: str, command: str) -> None:
    data = _load()
    bucket = _user_bucket(data, user_id)
    bucket["history"].insert(0, {
        "server_id": server_id, "command": command,
        "ts": datetime.datetime.utcnow().isoformat(timespec="seconds"),
    })
    bucket["history"] = bucket["history"][:MAX_HISTORY]
    _save(data)


def get_history(user_id, server_id: str = None, limit: int = 10) -> list:
    hist = _load().get("users", {}).get(_uid_key(user_id), {}).get("history", [])
    if server_id is not None:
        hist = [h for h in hist if h["server_id"] == server_id]
    return hist[:limit]


# ---------------------- Scheduled jobs ----------------------

def add_scheduled_job(user_id, server_id: str, command: str, hour: int, minute: int) -> dict:
    data = _load()
    bucket = _user_bucket(data, user_id)
    job = {
        "id": uuid.uuid4().hex[:8], "server_id": server_id, "command": command.strip(),
        "hour": hour, "minute": minute, "enabled": True,
        "last_run": None, "last_ok": None,
    }
    bucket["scheduled"].append(job)
    _save(data)
    return job


def get_scheduled_jobs(user_id, server_id: str = None) -> list:
    jobs = _load().get("users", {}).get(_uid_key(user_id), {}).get("scheduled", [])
    return [j for j in jobs if server_id is None or j["server_id"] == server_id]


def get_scheduled_job(user_id, job_id: str):
    for j in get_scheduled_jobs(user_id):
        if j["id"] == job_id:
            return j
    return None


def get_all_scheduled_jobs():
    """Every scheduled job across every user, as (user_id_str, job) pairs - used at
    startup to (re)register every job with the JobQueue, the same way
    settings.get_all_servers() feeds the health monitor."""
    data = _load()
    out = []
    for uid, bucket in data.get("users", {}).items():
        for job in bucket.get("scheduled", []):
            out.append((uid, job))
    return out


def remove_scheduled_job(user_id, job_id: str) -> bool:
    data = _load()
    bucket = _user_bucket(data, user_id)
    before = len(bucket["scheduled"])
    bucket["scheduled"] = [j for j in bucket["scheduled"] if j["id"] != job_id]
    changed = len(bucket["scheduled"]) != before
    if changed:
        _save(data)
    return changed


def set_scheduled_enabled(user_id, job_id: str, enabled: bool) -> bool:
    data = _load()
    bucket = _user_bucket(data, user_id)
    for j in bucket["scheduled"]:
        if j["id"] == job_id:
            j["enabled"] = bool(enabled)
            _save(data)
            return True
    return False


def _record_job_run(user_id, job_id: str, ok: bool) -> None:
    data = _load()
    bucket = _user_bucket(data, user_id)
    for j in bucket["scheduled"]:
        if j["id"] == job_id:
            j["last_run"] = datetime.datetime.utcnow().isoformat(timespec="seconds")
            j["last_ok"] = ok
            _save(data)
            return


def _job_name(user_id, job_id: str) -> str:
    return f"{JOB_NAME_PREFIX}{user_id}_{job_id}"


def schedule_job(job_queue, user_id, job: dict) -> None:
    """(Re)registers one job with the JobQueue - removes any existing entry for it
    first since JobQueue can't mutate a job's time in place (same approach as
    health.reschedule_job). No-op (removal only) if the job is disabled."""
    if job_queue is None:
        return
    name = _job_name(user_id, job["id"])
    for existing in job_queue.get_jobs_by_name(name):
        existing.schedule_removal()
    if not job.get("enabled"):
        return
    job_queue.run_daily(
        _scheduled_job_tick,
        time=datetime.time(hour=job["hour"], minute=job["minute"]),
        name=name,
        data={"user_id": user_id, "job_id": job["id"]},
    )


def unschedule_job(job_queue, user_id, job_id: str) -> None:
    if job_queue is None:
        return
    for existing in job_queue.get_jobs_by_name(_job_name(user_id, job_id)):
        existing.schedule_removal()


def register_all_jobs(job_queue) -> None:
    """Called once at startup (main.py) to (re)register every scheduled job -
    JobQueue's schedule doesn't survive a bot restart on its own, only what's
    saved in server_manager_automation.json does."""
    for user_id, job in get_all_scheduled_jobs():
        schedule_job(job_queue, user_id, job)


# ====================== Execution (shared by scheduled jobs, quick commands, "Run Now") ======================

def _run_ssh_command_sync(server: dict, command: str, timeout: int = RUN_TIMEOUT) -> dict:
    """Blocking connect + run + close - always called via asyncio.to_thread from
    async handlers/job callbacks, same pattern as maintenance.py."""
    try:
        client = engine.connect(server)
    except Exception as e:
        return {"ok": False, "error": str(e)[:300]}
    try:
        return engine.run_command(client, command, timeout=timeout)
    finally:
        try:
            client.close()
        except Exception:
            pass


def _format_run_result(label: str, command: str, result: dict) -> str:
    if result.get("error"):
        return f"⚠️ *{label}*\n`{command}`\n\n❌ {result['error']}"
    out = (result.get("stdout") or "").strip()
    err = (result.get("stderr") or "").strip()
    status = "✅" if result.get("ok") else f"❌ exit {result.get('exit_status')}"
    body = out or err or "(no output)"
    if len(body) > 3200:
        body = "…(truncated)…\n" + body[-3200:]
    return f"⚙️ *{label}*\n`{command}`\n\n{status}\n```\n{body}\n```"


async def _execute_and_report(bot, user_id, server: dict, label: str, command: str) -> bool:
    result = await asyncio.to_thread(_run_ssh_command_sync, server, command)
    record_history(user_id, server["id"], command)
    try:
        await bot.send_message(
            chat_id=int(user_id), text=_format_run_result(label, command, result), parse_mode="Markdown",
        )
    except Exception as e:
        logger.warning(f"automation result delivery failed for chat {user_id}: {e}")
    return bool(result.get("ok"))


async def _scheduled_job_tick(context: ContextTypes.DEFAULT_TYPE):
    """job_queue.run_daily() callback for one scheduled job."""
    job_data = context.job.data or {}
    user_id, job_id = job_data.get("user_id"), job_data.get("job_id")
    job = get_scheduled_job(user_id, job_id)
    if not job or not job.get("enabled"):
        return
    server = settings.get_server(user_id, job["server_id"])
    if not server:
        return
    ok = await _execute_and_report(context.bot, user_id, server, f"⏰ Scheduled: {server['label']}", job["command"])
    _record_job_run(user_id, job_id, ok)


# ====================== UI: Automation menu (entry point from server detail) ======================

async def automation_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    server_id = query.data.replace("svauto_menu_", "", 1)
    user_id = _uid(update)
    server = settings.get_server(user_id, server_id)
    if not server:
        await query.edit_message_text("❌ Server not found.")
        return
    tag = get_tag(user_id, server_id)
    qcs = get_quick_commands(user_id, server_id)
    jobs = get_scheduled_jobs(user_id, server_id)
    enabled_jobs = [j for j in jobs if j["enabled"]]
    text = (
        f"⚙️ *Automation — {server['label']}*\n\n"
        f"🏷 Tag: {tag or '_none_'}\n"
        f"⭐ Quick Commands: {len(qcs)} saved\n"
        f"⏰ Scheduled Jobs: {len(jobs)} ({len(enabled_jobs)} active)"
    )
    keyboard = [
        [InlineKeyboardButton("🏷 Set Tag", callback_data=f"svauto_tag_start_{server_id}")],
        [InlineKeyboardButton("⭐ Quick Commands", callback_data=f"svauto_qc_{server_id}")],
        [InlineKeyboardButton("⏰ Scheduled Jobs", callback_data=f"svauto_sched_{server_id}")],
        [InlineKeyboardButton("📜 History", callback_data=f"svauto_hist_{server_id}")],
        [InlineKeyboardButton("🔙 Back", callback_data=f"servermgr_srv_{server_id}")],
    ]
    try:
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
    except BadRequest:
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=None)


# ---------------------- Tag flow ----------------------

async def tag_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    server_id = query.data.replace("svauto_tag_start_", "", 1)
    server = settings.get_server(_uid(update), server_id)
    if not server:
        await query.edit_message_text("❌ Server not found.")
        return ConversationHandler.END
    context.user_data["svauto_tmp"] = {"server_id": server_id}
    current = get_tag(_uid(update), server_id)
    await _edit_then_prompt_cancel(
        query,
        f"🏷 Send a tag for *{server['label']}* (e.g. prod, staging).\n"
        f"Current: {current or 'none'}. Send `-` to clear it.",
    )
    return AUTO_TAG_INPUT


async def tag_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tmp = context.user_data.get("svauto_tmp") or {}
    server_id = tmp.get("server_id")
    text = update.message.text.strip()
    if not server_id:
        await update.message.reply_text("❌ Something went wrong, please try again.", reply_markup=get_main_menu())
        return ConversationHandler.END
    set_tag(_uid(update), server_id, "" if text == "-" else text)
    context.user_data.pop("svauto_tmp", None)
    await update.message.reply_text(
        "✅ Tag cleared." if text == "-" else f"✅ Tag set to `{text}`.",
        reply_markup=get_main_menu(), parse_mode="Markdown",
    )
    return ConversationHandler.END


# ---------------------- Quick Commands flow ----------------------

def _qc_list_keyboard(user_id, server_id):
    keyboard = []
    for q in get_quick_commands(user_id, server_id):
        keyboard.append([
            InlineKeyboardButton(f"▶️ {q['label']}", callback_data=f"svauto_qc_run_{q['id']}"),
            InlineKeyboardButton("🗑", callback_data=f"svauto_qc_del_{q['id']}"),
        ])
    keyboard.append([InlineKeyboardButton("➕ Add", callback_data=f"svauto_qc_add_{server_id}")])
    keyboard.append([InlineKeyboardButton("🔙 Back", callback_data=f"svauto_menu_{server_id}")])
    return InlineKeyboardMarkup(keyboard)


async def qc_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    server_id = query.data.replace("svauto_qc_", "", 1)
    user_id = _uid(update)
    server = settings.get_server(user_id, server_id)
    if not server:
        await query.edit_message_text("❌ Server not found.")
        return
    qcs = get_quick_commands(user_id, server_id)
    text = f"⭐ *Quick Commands — {server['label']}*\n\n" + (
        "\n".join(f"• *{q['label']}* — `{q['command']}`" for q in qcs) if qcs else "No saved commands yet."
    )
    try:
        await query.edit_message_text(text, reply_markup=_qc_list_keyboard(user_id, server_id), parse_mode="Markdown")
    except BadRequest:
        await query.edit_message_text(text, reply_markup=_qc_list_keyboard(user_id, server_id), parse_mode=None)


async def qc_add_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    server_id = query.data.replace("svauto_qc_add_", "", 1)
    server = settings.get_server(_uid(update), server_id)
    if not server:
        await query.edit_message_text("❌ Server not found.")
        return ConversationHandler.END
    context.user_data["svauto_tmp"] = {"server_id": server_id}
    await _edit_then_prompt_cancel(query, "⭐ Send a short label for this quick command (e.g. Restart nginx).")
    return AUTO_QC_ADD_LABEL


async def qc_add_label(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if not text:
        await update.message.reply_text("❌ Please send a label, or tap Cancel.", reply_markup=_cancel_kb())
        return AUTO_QC_ADD_LABEL
    context.user_data.setdefault("svauto_tmp", {})["label"] = text
    await update.message.reply_text(
        "💻 Now send the actual command to run (e.g. systemctl restart nginx).", reply_markup=_cancel_kb(),
    )
    return AUTO_QC_ADD_COMMAND


async def qc_add_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    tmp = context.user_data.get("svauto_tmp") or {}
    server_id, label = tmp.get("server_id"), tmp.get("label")
    if not text or not server_id or not label:
        await update.message.reply_text("❌ Please send a command, or tap Cancel.", reply_markup=_cancel_kb())
        return AUTO_QC_ADD_COMMAND
    add_quick_command(_uid(update), server_id, label, text)
    context.user_data.pop("svauto_tmp", None)
    await update.message.reply_text(
        f"✅ Saved *{label}* as a quick command.", reply_markup=get_main_menu(), parse_mode="Markdown",
    )
    return ConversationHandler.END


async def qc_run(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer("Running…")
    qc_id = query.data.replace("svauto_qc_run_", "", 1)
    user_id = _uid(update)
    qc = get_quick_command(user_id, qc_id)
    if not qc:
        await query.edit_message_text("❌ Quick command not found.")
        return
    server = settings.get_server(user_id, qc["server_id"])
    if not server:
        await query.edit_message_text("❌ Server not found.")
        return
    await _execute_and_report(context.bot, user_id, server, f"⭐ {qc['label']}", qc["command"])


async def qc_delete(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    qc_id = query.data.replace("svauto_qc_del_", "", 1)
    user_id = _uid(update)
    qc = get_quick_command(user_id, qc_id)
    server_id = qc["server_id"] if qc else None
    remove_quick_command(user_id, qc_id)
    if not server_id:
        await query.edit_message_text("🗑 Removed.")
        return
    server = settings.get_server(user_id, server_id)
    text = f"⭐ *Quick Commands — {server['label']}*\n\n🗑 Removed." if server else "🗑 Removed."
    try:
        await query.edit_message_text(text, reply_markup=_qc_list_keyboard(user_id, server_id), parse_mode="Markdown")
    except BadRequest:
        await query.edit_message_text(text, reply_markup=_qc_list_keyboard(user_id, server_id), parse_mode=None)


# ---------------------- Scheduled Jobs flow ----------------------

def _sched_list_keyboard(user_id, server_id):
    keyboard = []
    for j in get_scheduled_jobs(user_id, server_id):
        onoff = "🟢 ON" if j["enabled"] else "⚪️ OFF"
        keyboard.append([
            InlineKeyboardButton(f"⏰ {j['hour']:02d}:{j['minute']:02d} — {onoff}", callback_data=f"svauto_sched_toggle_{j['id']}"),
            InlineKeyboardButton("🗑", callback_data=f"svauto_sched_del_{j['id']}"),
        ])
    max_automations = subscription.get_capabilities(user_id).get("max_automations")
    at_cap = max_automations is not None and len(get_scheduled_jobs(user_id)) >= max_automations
    add_label = "🔒 Add Schedule (plan limit reached)" if at_cap else "➕ Add Schedule"
    keyboard.append([InlineKeyboardButton(add_label, callback_data=f"svauto_sched_add_{server_id}")])
    keyboard.append([InlineKeyboardButton("🔙 Back", callback_data=f"svauto_menu_{server_id}")])
    return InlineKeyboardMarkup(keyboard)


def _sched_text(server, jobs, user_id) -> str:
    lines = []
    for j in jobs:
        last = f" (last: {'✅' if j['last_ok'] else '❌'} {j['last_run'][:16].replace('T', ' ')})" if j.get("last_run") else ""
        lines.append(f"• `{j['hour']:02d}:{j['minute']:02d}` — `{j['command']}`{last}")
    max_automations = subscription.get_capabilities(user_id).get("max_automations")
    usage = (
        f"{len(get_scheduled_jobs(user_id))}/{max_automations} automation jobs used (plan limit)"
        if max_automations is not None else "Automation jobs: unlimited on your plan"
    )
    return (
        f"⏰ *Scheduled Jobs — {server['label']}*\n\n"
        + ("\n".join(lines) if lines else "No scheduled jobs yet.")
        + f"\n\n_{usage}. Times are the bot server's clock (usually UTC). Tap a job to toggle it on/off._"
    )


async def sched_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    server_id = query.data.replace("svauto_sched_", "", 1)
    user_id = _uid(update)
    server = settings.get_server(user_id, server_id)
    if not server:
        await query.edit_message_text("❌ Server not found.")
        return
    jobs = get_scheduled_jobs(user_id, server_id)
    text = _sched_text(server, jobs, user_id)
    try:
        await query.edit_message_text(text, reply_markup=_sched_list_keyboard(user_id, server_id), parse_mode="Markdown")
    except BadRequest:
        await query.edit_message_text(text, reply_markup=_sched_list_keyboard(user_id, server_id), parse_mode=None)


async def sched_add_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = _uid(update)
    server_id = query.data.replace("svauto_sched_add_", "", 1)
    server = settings.get_server(user_id, server_id)
    if not server:
        await query.edit_message_text("❌ Server not found.")
        return ConversationHandler.END

    max_automations = subscription.get_capabilities(user_id).get("max_automations")
    if max_automations is not None and len(get_scheduled_jobs(user_id)) >= max_automations:
        await query.answer(
            f"🔒 Your plan allows {max_automations} automation job(s) max. "
            "Remove one first, or upgrade from 💳 Subscription.",
            show_alert=True,
        )
        return ConversationHandler.END

    context.user_data["svauto_tmp"] = {"server_id": server_id}
    await _edit_then_prompt_cancel(query, f"💻 Send the command to run daily on *{server['label']}*.")
    return AUTO_SCHED_ADD_COMMAND


async def sched_add_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if not text:
        await update.message.reply_text("❌ Please send a command, or tap Cancel.", reply_markup=_cancel_kb())
        return AUTO_SCHED_ADD_COMMAND
    context.user_data.setdefault("svauto_tmp", {})["command"] = text
    await update.message.reply_text(
        "⏰ Now send the time to run it daily, 24h HH:MM (bot server's clock, usually UTC), e.g. 03:00.",
        reply_markup=_cancel_kb(),
    )
    return AUTO_SCHED_ADD_TIME


async def sched_add_time(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    tmp = context.user_data.get("svauto_tmp") or {}
    server_id, command = tmp.get("server_id"), tmp.get("command")
    parts = text.split(":")
    valid = (
        len(parts) == 2 and all(p.isdigit() for p in parts)
        and 0 <= int(parts[0]) <= 23 and 0 <= int(parts[1]) <= 59
    )
    if not valid or not server_id or not command:
        await update.message.reply_text(
            "❌ Please send a valid time as HH:MM (e.g. 03:00), or tap Cancel.", reply_markup=_cancel_kb(),
        )
        return AUTO_SCHED_ADD_TIME
    hour, minute = int(parts[0]), int(parts[1])
    user_id = _uid(update)
    job = add_scheduled_job(user_id, server_id, command, hour, minute)
    schedule_job(context.job_queue, user_id, job)
    context.user_data.pop("svauto_tmp", None)
    await update.message.reply_text(
        f"✅ Scheduled daily at `{hour:02d}:{minute:02d}`.", reply_markup=get_main_menu(), parse_mode="Markdown",
    )
    return ConversationHandler.END


async def sched_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    job_id = query.data.replace("svauto_sched_toggle_", "", 1)
    user_id = _uid(update)
    job = get_scheduled_job(user_id, job_id)
    if not job:
        await query.answer("❌ Job not found.")
        return
    new_enabled = not job["enabled"]
    set_scheduled_enabled(user_id, job_id, new_enabled)
    job["enabled"] = new_enabled
    schedule_job(context.job_queue, user_id, job)
    await query.answer("🟢 Enabled" if new_enabled else "⚪️ Disabled")
    server = settings.get_server(user_id, job["server_id"])
    if not server:
        return
    jobs = get_scheduled_jobs(user_id, job["server_id"])
    try:
        await query.edit_message_text(
            _sched_text(server, jobs, user_id), reply_markup=_sched_list_keyboard(user_id, job["server_id"]), parse_mode="Markdown",
        )
    except BadRequest:
        pass


async def sched_delete(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    job_id = query.data.replace("svauto_sched_del_", "", 1)
    user_id = _uid(update)
    job = get_scheduled_job(user_id, job_id)
    server_id = job["server_id"] if job else None
    if job:
        unschedule_job(context.job_queue, user_id, job_id)
        remove_scheduled_job(user_id, job_id)
    if not server_id:
        await query.edit_message_text("🗑 Removed.")
        return
    server = settings.get_server(user_id, server_id)
    if not server:
        await query.edit_message_text("🗑 Removed.")
        return
    jobs = get_scheduled_jobs(user_id, server_id)
    try:
        await query.edit_message_text(_sched_text(server, jobs, user_id), reply_markup=_sched_list_keyboard(user_id, server_id), parse_mode="Markdown")
    except BadRequest:
        await query.edit_message_text(_sched_text(server, jobs, user_id), reply_markup=_sched_list_keyboard(user_id, server_id), parse_mode=None)


# ---------------------- History (read-only) ----------------------

async def history_view(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    server_id = query.data.replace("svauto_hist_", "", 1)
    user_id = _uid(update)
    server = settings.get_server(user_id, server_id)
    if not server:
        await query.edit_message_text("❌ Server not found.")
        return
    hist = get_history(user_id, server_id, limit=10)
    lines = [f"• `{h['ts'][:16].replace('T', ' ')}` — `{h['command']}`" for h in hist]
    text = f"📜 *Recent Commands — {server['label']}*\n\n" + ("\n".join(lines) if lines else "No history yet.")
    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data=f"svauto_menu_{server_id}")]])
    try:
        await query.edit_message_text(text, reply_markup=keyboard, parse_mode="Markdown")
    except BadRequest:
        await query.edit_message_text(text, reply_markup=keyboard, parse_mode=None)
