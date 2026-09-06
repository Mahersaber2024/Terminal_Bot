import asyncio
import html
import logging
import os
import queue as queue_mod
import re
import tempfile
import time
import urllib.parse
import uuid

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, ReplyKeyboardRemove
from telegram.error import BadRequest
from telegram.ext import ContextTypes, ConversationHandler

from . import settings
from . import engine
from . import health
from . import maintenance
from . import advanced
from . import automation
import subscription

logger = logging.getLogger(__name__)

SERVERMGR_ADD_HOST = 610
SERVERMGR_ADD_PORT = 611
SERVERMGR_ADD_USER = 612
SERVERMGR_ADD_AUTHTYPE = 613
SERVERMGR_ADD_SECRET = 614
SERVERMGR_ADD_PASSPHRASE = 615
SERVERMGR_ADD_LABEL = 616
SERVERMGR_CMD_INPUT = 605
SERVERMGR_QUICKPING_INPUT = 606
SERVERMGR_QUICKADD_LABEL = 607
SERVERMGR_QUICKADD_CMD = 608
SERVERMGR_FILES_BROWSE = 620
SERVERMGR_ADV_BROWSE = 630

CANCEL_BUTTON_TEXT = "✕ Cancel"
CMD_DONE_TEXT = "■ End Session"
MENU_BUTTON_TEXT = "▣ Server Manager"
CONNECT_TIMEOUT = 15
COMMAND_TIMEOUT = 6 * 60 * 60
MAX_SESSIONS = 3

FILES_START_CB_PREFIX = "servermgr_filesopen_"
FILES_NAV_CB_PREFIX = "servermgr_filesnav_"
FILES_DL_CB_PREFIX = "servermgr_filesdl_"
FILES_UP_CB = "servermgr_filesup"
FILES_UPLOAD_HERE_CB = "servermgr_filesuploadhere"
FILES_GOTO_CB = "servermgr_filesgoto"
FILES_URLDL_CB = "servermgr_filesurldl"
FILES_MKDIR_CB = "servermgr_filesmkdir"
FILES_CLOSE_CB = "servermgr_filesclose"
FILES_REFRESH_CB = "servermgr_filesrefresh"
FILES_ACTIONS_CB_PREFIX = "servermgr_filesact_"
FILES_EDIT_CB_PREFIX = "servermgr_filesedit_"
FILES_RENAME_CB_PREFIX = "servermgr_filesren_"
FILES_DELCONFIRM_CB_PREFIX = "servermgr_filesdelconf_"
FILES_DELOK_CB_PREFIX = "servermgr_filesdelok_"
FILES_BACK_CB = "servermgr_filesback"
FILES_OVERWRITE_CB = "servermgr_filesowconfirm"
FILES_OVERWRITE_CANCEL_CB = "servermgr_filesowcancel"

# ---- Advanced Tools (process manager / services / crontab / log tail) ----
ADV_START_CB_PREFIX = "servermgr_advopen_"
ADV_MENU_CB = "servermgr_advmenu"
ADV_CLOSE_CB = "servermgr_advclose"
ADV_PROC_CB_PREFIX = "servermgr_advproc_"          # + "cpu" | "ram"
ADV_PROC_KILL_CB_PREFIX = "servermgr_advkill_"     # + pid
ADV_PROC_KILL9_CB_PREFIX = "servermgr_advkill9_"   # + pid
ADV_SVC_CB = "servermgr_advsvc"
ADV_SVC_FILTERMODE_CB_PREFIX = "servermgr_advsvcfmode_"  # + "all" | "custom" | "system"
ADV_SVC_DETAIL_CB_PREFIX = "servermgr_advsvcd_"    # + index into adv["units"]
ADV_SVC_ACTION_CB_PREFIX = "servermgr_advsvcact_"  # + "index|action"
ADV_SVC_RM_CONFIRM_CB_PREFIX = "servermgr_advsvcrmc_"
ADV_SVC_RM_OK_CB_PREFIX = "servermgr_advsvcrmok_"
ADV_SVC_LOG_CB_PREFIX = "servermgr_advsvclog_"       # + index into adv["units"]
ADV_SVC_LOGSTOP_CB = "servermgr_advsvclogstop"
ADV_SVC_LOG_EDIT_INTERVAL = 1.5
ADV_SVC_LOG_BODY_CHARS = 3200
ADV_CRON_VIEW_CB = "servermgr_advcronview"
ADV_CRON_EDIT_CB = "servermgr_advcronedit"
ADV_CMD_TIMEOUT = 25

UPLOAD_BATCH_DEBOUNCE = 1.2
SFTP_MAX_LIST_ENTRIES = 40
FILE_TRANSFER_TIMEOUT = 120
URL_DOWNLOAD_TIMEOUT = 600

HEALTH_CHECK_CB_PREFIX = "servermgr_health_"
HEALTH_TOGGLE_CB_PREFIX = "servermgr_healthtoggle_"

HEALTH_RESTART_CB_PREFIX = "servermgr_restart_"
HEALTH_RESTART_CONFIRM_CB_PREFIX = "servermgr_restartok_"
HEALTH_CLEANUP_CB_PREFIX = "servermgr_cleanup_"

ADD_DEFAULT_PORT = 22
ADD_DEFAULT_USER = "root"
ADD_PORT_DEFAULT_CB = "servermgr_addport_default"
ADD_USER_DEFAULT_CB = "servermgr_adduser_default"
ADD_AUTH_PASS_CB = "servermgr_addauth_pass"
ADD_AUTH_KEY_CB = "servermgr_addauth_key"
ADD_PASSPHRASE_NONE_CB = "servermgr_addpass_none"
ADD_LABEL_DEFAULT_CB = "servermgr_addlabel_default"

CMD_CANCEL_CALLBACK = "servermgr_cmdcancel"
CMD_ENTER_CALLBACK = "servermgr_cmdenter"
CMD_YES_CALLBACK = "servermgr_cmdyes"
CMD_NO_CALLBACK = "servermgr_cmdno"

# ---- Quick commands (⚡) inside a live SSH terminal tab - saved per (user, server) ----
QUICK_MENU_CB_PREFIX = "servermgr_cmdquickmenu_"
QUICK_CLOSE_CB_PREFIX = "servermgr_cmdquickclose_"
QUICK_MANAGE_CB_PREFIX = "servermgr_cmdquickmanage_"
QUICK_DEL_CB_PREFIX = "servermgr_cmdquickdel_"
QUICK_ADD_CB_PREFIX = "servermgr_cmdquickadd_"
QUICK_RUN_CB_PREFIX = "servermgr_cmdquickrun_"

TERMINAL_EDIT_INTERVAL = 1.2
TERMINAL_MAX_IDLE_EDIT = 4.0
TERMINAL_BODY_CHARS = 3200

_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]|\x1b\][^\x07]*\x07|\x1b[=>]")

def _strip_ansi(text: str) -> str:
    text = _ANSI_ESCAPE_RE.sub("", text)
    text = text.replace("\r\n", "\n")
    if "\r" in text:
        text = "\n".join(line.split("\r")[-1] for line in text.split("\n"))
    return text

_get_main_menu_func = None


def set_get_main_menu(func):
    global _get_main_menu_func
    _get_main_menu_func = func


def get_main_menu(user_id=None):
    if _get_main_menu_func:
        return _get_main_menu_func(user_id)
    return None


def _uid(update: Update) -> int:
    return update.effective_user.id


def _short_host(host: str) -> str:
    host = (host or "").strip()
    parts = host.split(".")
    if len(parts) == 4 and all(p.isdigit() for p in parts):
        return parts[-1]
    return parts[0] if parts else host


_NO_SUBSCRIPTION_TEXT = (
    "⚿ Server Manager requires an active subscription.\n\n"
    "Tap ◆ Subscription on the main menu to buy or renew a plan."
)


def _effective_tab_limit(user_id) -> int:
    _, plan_max_tabs = subscription.get_limits(user_id)
    return min(MAX_SESSIONS, plan_max_tabs)


def _cancel_keyboard():
    return ReplyKeyboardMarkup([[CANCEL_BUTTON_TEXT]], resize_keyboard=True, one_time_keyboard=True)


def _ssh_session_keyboard():
    return ReplyKeyboardMarkup([[CMD_DONE_TEXT]], resize_keyboard=True)


TAB_SWITCH_CB_PREFIX = "servermgr_tabswitch_"
TAB_CLOSE_CB_PREFIX = "servermgr_tabclose_"
TAB_NEWTAB_CB = "servermgr_newtab"


def _tab_label(sessions: dict, sid: str) -> str:
    ids = list(sessions.keys())
    try:
        idx = ids.index(sid) + 1
    except ValueError:
        idx = len(ids) + 1
    server_label = sessions.get(sid, {}).get("label", "")
    return f"{idx} · {server_label}" if server_label else str(idx)


def _tabs_keyboard_rows(sessions: dict, active_id: str) -> list:
    rows = []
    for sid in sessions:
        is_active = sid == active_id
        rows.append([
            InlineKeyboardButton(
                f"{'●' if is_active else '○'} {_tab_label(sessions, sid)}",
                callback_data="servermgr_noop" if is_active else f"{TAB_SWITCH_CB_PREFIX}{sid}",
            ),
            InlineKeyboardButton("Close", callback_data=f"{TAB_CLOSE_CB_PREFIX}{sid}"),
        ])
    return rows


def _servermgr_quick_keyboard_rows(user_id: int, server_id: str, session_id: str) -> list:
    rows = []
    row = []
    quick_commands = automation.get_quick_commands(user_id, server_id)
    for qc in quick_commands:
        row.append(InlineKeyboardButton(qc["label"], callback_data=f"{QUICK_RUN_CB_PREFIX}{qc['id']}_{session_id}"))
        if len(row) == 3:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    add_row = [InlineKeyboardButton("+ Add command", callback_data=f"{QUICK_ADD_CB_PREFIX}{session_id}")]
    if quick_commands:
        add_row.append(InlineKeyboardButton("✕ Remove", callback_data=f"{QUICK_MANAGE_CB_PREFIX}{session_id}"))
    rows.append(add_row)
    rows.append([InlineKeyboardButton("◀ Close quick menu", callback_data=f"{QUICK_CLOSE_CB_PREFIX}{session_id}")])
    return rows


def _servermgr_quick_manage_keyboard_rows(user_id: int, server_id: str, session_id: str) -> list:
    rows = []
    for qc in automation.get_quick_commands(user_id, server_id):
        rows.append([InlineKeyboardButton(
            f"✕ {qc['label']}", callback_data=f"{QUICK_DEL_CB_PREFIX}{qc['id']}_{session_id}",
        )])
    rows.append([InlineKeyboardButton("◀ Back", callback_data=f"{QUICK_MENU_CB_PREFIX}{session_id}")])
    return rows


def _terminal_keyboard(sessions: dict, active_id: str, session_id: str = None) -> InlineKeyboardMarkup:
    rows = _tabs_keyboard_rows(sessions, active_id)

    new_window_btn = (
        InlineKeyboardButton("▣ New Window", callback_data=TAB_NEWTAB_CB)
        if len(sessions) < MAX_SESSIONS else None
    )
    session = sessions.get(session_id or active_id)
    if session and session.get("sftp_enabled"):
        combo_row = ([new_window_btn] if new_window_btn else []) + [
            InlineKeyboardButton(
                "▥ SFTP", callback_data=f"{FILES_START_CB_PREFIX}{session['server_id']}",
            )
        ]
        rows.append(combo_row)
    elif new_window_btn:
        rows.append([new_window_btn])

    if session_id:
        if session and session.get("quick_manage"):
            rows.extend(_servermgr_quick_manage_keyboard_rows(session["user_id"], session["server_id"], session_id))
        elif session and session.get("quick_open"):
            rows.extend(_servermgr_quick_keyboard_rows(session["user_id"], session["server_id"], session_id))
        else:
            rows.append([
                InlineKeyboardButton("✓ y", callback_data=f"{CMD_YES_CALLBACK}_{session_id}"),
                InlineKeyboardButton("⏎ Enter", callback_data=f"{CMD_ENTER_CALLBACK}_{session_id}"),
                InlineKeyboardButton("⊘ n", callback_data=f"{CMD_NO_CALLBACK}_{session_id}"),
            ])
            rows.append([
                InlineKeyboardButton("✕ Cancel", callback_data=f"{CMD_CANCEL_CALLBACK}_{session_id}"),
                InlineKeyboardButton("⚡ Quick", callback_data=f"{QUICK_MENU_CB_PREFIX}{session_id}"),
            ])
    return InlineKeyboardMarkup(rows)


def _cancel_session_id_from_markup(reply_markup) -> str:
    if not reply_markup:
        return None
    for row in reply_markup.inline_keyboard:
        for btn in row:
            if btn.callback_data and btn.callback_data.startswith(f"{CMD_CANCEL_CALLBACK}_"):
                return btn.callback_data.replace(f"{CMD_CANCEL_CALLBACK}_", "", 1)
    return None


async def _clear_tab_state_msg(bot, session: dict):
    if session is None:
        return
    old = session.pop("tab_state_msg", None)
    if old is None:
        return
    try:
        await bot.delete_message(chat_id=old[0], message_id=old[1])
    except Exception:
        pass


async def _send_tab_state(bot, chat_id: int, sessions: dict, active_id: str):
    if not sessions or active_id not in sessions:
        return
    session = sessions[active_id]
    await _clear_tab_state_msg(bot, session)
    term_state = session.get("term_state")
    if term_state:
        text = _format_terminal(
            term_state["label"], term_state["command"], term_state["output"],
            term_state.get("status", "✓ Ready — send the next command."),
        )
        keyboard = _terminal_keyboard(sessions, active_id, active_id)
    else:
        label = session.get("label", "")
        text = _format_terminal(label, "", "", "● Ready — send a command.")
        keyboard = _terminal_keyboard(sessions, active_id)
    msg = await bot.send_message(chat_id=chat_id, text=text, parse_mode="Markdown", reply_markup=keyboard)
    session["tab_state_msg"] = (msg.chat_id, msg.message_id)


async def servermgr_noop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()


async def servermgr_tabsbar_newtab(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    servers = settings.get_servers(query.from_user.id)
    sessions = context.user_data.get("servermgr_sessions", {})

    if not servers:
        await context.bot.send_message(
            chat_id=query.message.chat_id,
            text="You don't have any registered servers yet. Add one from the Server Manager menu first.",
        )
        return SERVERMGR_CMD_INPUT

    tab_limit = _effective_tab_limit(query.from_user.id)
    if len(sessions) >= tab_limit:
        await context.bot.send_message(
            chat_id=query.message.chat_id,
            text=f"⚠ Your plan allows {tab_limit} concurrent terminal tab(s). Close one first, then try again.",
        )
        return SERVERMGR_CMD_INPUT

    open_counts = {}
    for s in sessions.values():
        open_counts[s["server_id"]] = open_counts.get(s["server_id"], 0) + 1

    def _btn_text(s):
        count = open_counts.get(s["id"], 0)
        suffix = f" — {count} open" if count else ""
        return f"{'● ' if count else ''}▣ {s['label']} -> {_short_host(s['host'])}{suffix}"

    keyboard = [[InlineKeyboardButton(_btn_text(s), callback_data=f"servermgr_ssh_start_{s['id']}")] for s in servers]
    await context.bot.send_message(
        chat_id=query.message.chat_id, text="Pick a server to open in a new tab:", reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return SERVERMGR_CMD_INPUT


async def _run_with_timeout(func, *args, timeout: int, **kwargs):
    try:
        result = await asyncio.wait_for(asyncio.to_thread(func, *args, **kwargs), timeout=timeout)
        return result, False
    except asyncio.TimeoutError:
        return None, True
    except Exception as e:
        logger.warning(f"servermgr background call failed: {e}")
        return e, False


async def _sftp_call(func, *args, timeout: int = FILE_TRANSFER_TIMEOUT, **kwargs):
    result, timed_out = await _run_with_timeout(func, *args, timeout=timeout, **kwargs)
    if timed_out:
        return None, f"[time] Timed out after {timeout}s."
    if isinstance(result, Exception):
        return None, str(result)[:300]
    return result, None


_PROGRESS_EDIT_INTERVAL = 1.5


def _progress_bar(pct: float, width: int = 12) -> str:
    filled = min(width, max(0, round(pct / 100 * width)))
    return "[" + "#" * filled + "-" * (width - filled) + "]"


async def _run_transfer_with_progress(func, *args, label: str, status_msg, timeout: int = FILE_TRANSFER_TIMEOUT):
    progress = {"done": 0, "total": None}

    def _on_chunk(done, total):
        progress["done"] = done
        progress["total"] = total

    task = asyncio.create_task(_sftp_call(func, *args, progress_callback=_on_chunk, timeout=timeout))
    last_text = None
    while not task.done():
        await asyncio.sleep(_PROGRESS_EDIT_INTERVAL)
        total = progress["total"]
        if total:
            pct = min(100.0, progress["done"] / total * 100)
            text = f"{label}\n{_progress_bar(pct)}  {pct:.0f}%  ({_human_size(progress['done'])}/{_human_size(total)})"
        else:
            text = f"{label}\n{_progress_bar(0)}  0%"
        if text != last_text:
            try:
                await status_msg.edit_text(text)
                last_text = text
            except Exception:
                pass
    return await task


_SPINNER_FRAMES = ["◐", "◓", "◑", "◒"]
_SPINNER_EDIT_INTERVAL = 1.5


async def _run_maintenance_with_spinner(func, *args, label: str, query, timeout: int):
    start = time.monotonic()
    task = asyncio.create_task(_run_with_timeout(func, *args, timeout=timeout))
    frame = 0
    last_text = None
    while not task.done():
        await asyncio.sleep(_SPINNER_EDIT_INTERVAL)
        elapsed = int(time.monotonic() - start)
        text = f"{_SPINNER_FRAMES[frame % len(_SPINNER_FRAMES)]} {label}  ({elapsed}s)"
        frame += 1
        if text != last_text:
            try:
                await query.edit_message_text(text)
                last_text = text
            except Exception:
                pass
    return await task


def _format_terminal(label: str, command: str, output: str, status_line: str) -> str:
    header = f"▣ *Terminal* — `{label}`"
    if not command:
        return f"{header}\n{status_line}"
    body = _strip_ansi(output)
    body = body[-TERMINAL_BODY_CHARS:]
    if len(output) > TERMINAL_BODY_CHARS:
        body = "…(older output trimmed)…\n" + body
    body = body.rstrip("\n")
    header += f"\n`$ {command}`"
    return f"{header}\n```\n{body}\n\n```\n{status_line}"


def _cancel_session_timeout(session: dict):
    job = session.get("timeout_job")
    if job is not None:
        try:
            job.schedule_removal()
        except Exception:
            pass


def _close_one_session(context: ContextTypes.DEFAULT_TYPE, session_id: str):
    sessions = context.user_data.get("servermgr_sessions", {})
    session = sessions.pop(session_id, None)
    if session is None:
        return
    _cancel_session_timeout(session)
    engine.close_shell(session.get("channel"))
    client = session.get("client")
    if client is not None:
        try:
            client.close()
        except Exception:
            pass


def _close_ssh_session(context: ContextTypes.DEFAULT_TYPE):
    sessions = context.user_data.pop("servermgr_sessions", {}) or {}
    for session_id in list(sessions.keys()):
        session = sessions[session_id]
        _cancel_session_timeout(session)
        engine.close_shell(session.get("channel"))
        client = session.get("client")
        if client is not None:
            try:
                client.close()
            except Exception:
                pass
    context.user_data.pop("servermgr_active_session", None)


def _close_file_browser(context: ContextTypes.DEFAULT_TYPE):
    fb = context.user_data.pop("servermgr_filebrowser", None)
    if not fb:
        return
    engine.close_sftp(fb.get("sftp"))
    client = fb.get("client")
    if client is not None:
        try:
            client.close()
        except Exception:
            pass


def _close_adv_tools(context: ContextTypes.DEFAULT_TYPE):
    adv = context.user_data.pop("servermgr_adv", None)
    if not adv:
        return
    log_channel = adv.get("svc_log_channel")
    if log_channel is not None:
        try:
            engine.remote_tail_stop(log_channel)
        except Exception:
            pass
    client = adv.get("client")
    if client is not None:
        try:
            client.close()
        except Exception:
            pass


def _do_close_tab(context: ContextTypes.DEFAULT_TYPE, session_id: str):
    sessions = context.user_data.get("servermgr_sessions", {})
    session = sessions.get(session_id)
    if session is None:
        return None
    label, server_id = session["label"], session["server_id"]
    _close_one_session(context, session_id)
    sessions = context.user_data.get("servermgr_sessions", {})
    if context.user_data.get("servermgr_active_session") == session_id:
        context.user_data.pop("servermgr_active_session", None)
        if sessions:
            context.user_data["servermgr_active_session"] = next(iter(sessions))
    return {
        "label": label, "server_id": server_id,
        "sessions": sessions, "active_id": context.user_data.get("servermgr_active_session"),
    }


async def _session_timeout_tick(context: ContextTypes.DEFAULT_TYPE):
    job = context.job
    session_id = job.data["session_id"]
    sessions = context.user_data.get("servermgr_sessions", {})
    if session_id not in sessions:
        return
    label = sessions[session_id].get("label", "")
    _do_close_tab(context, session_id)
    try:
        await context.bot.send_message(
            chat_id=job.chat_id,
            text=f"⏱ Your session on \"{label}\" was auto-closed - your plan's session time limit was reached.",
        )
    except Exception:
        pass


def _schedule_session_timeout(context: ContextTypes.DEFAULT_TYPE, chat_id: int, user_id: int,
                               session_id: str, minutes):
    if not minutes or context.job_queue is None:
        return None
    return context.job_queue.run_once(
        _session_timeout_tick, when=int(minutes) * 60, chat_id=chat_id, user_id=user_id,
        data={"session_id": session_id}, name=f"servermgr_timeout_{user_id}_{session_id}",
    )


def _cmd_state_or_end(context: ContextTypes.DEFAULT_TYPE):
    return SERVERMGR_CMD_INPUT if context.user_data.get("servermgr_sessions") else ConversationHandler.END


async def servermgr_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    adv = context.user_data.get("servermgr_adv")
    if adv and adv.get("awaiting_cron_edit"):
        adv["awaiting_cron_edit"] = False
        await update.message.reply_text("✕ Cancelled.", reply_markup=ReplyKeyboardRemove())
        text, reply_markup = _adv_menu_text_and_keyboard(adv)
        await context.bot.send_message(chat_id=adv["chat_id"], text=text, reply_markup=reply_markup, parse_mode="Markdown")
        return SERVERMGR_ADV_BROWSE

    fb = context.user_data.get("servermgr_filebrowser")
    if fb and (fb.get("awaiting_upload") or fb.get("awaiting_path") or fb.get("awaiting_url")
               or fb.get("awaiting_rename_idx") is not None or fb.get("awaiting_edit_idx") is not None
               or fb.get("awaiting_mkdir")):
        was_upload = fb.get("awaiting_upload")
        fb["awaiting_upload"] = False
        fb["awaiting_path"] = False
        fb["awaiting_url"] = False
        fb["awaiting_rename_idx"] = None
        fb["awaiting_edit_idx"] = None
        fb["awaiting_mkdir"] = False
        await update.message.reply_text(
            "✕ Upload cancelled." if was_upload else "✕ Cancelled.", reply_markup=ReplyKeyboardRemove(),
        )
        await context.bot.send_message(
            chat_id=fb["chat_id"], text=_files_text(fb), reply_markup=_files_keyboard(fb), parse_mode="Markdown",
        )
        return SERVERMGR_FILES_BROWSE

    sessions = context.user_data.get("servermgr_sessions")
    if sessions:
        active_id = context.user_data.get("servermgr_active_session")
        label = sessions.get(active_id, {}).get("label", "session")
        if active_id:
            _close_one_session(context, active_id)
        sessions = context.user_data.get("servermgr_sessions", {})
        if sessions:
            new_active_id, new_session = next(iter(sessions.items()))
            context.user_data["servermgr_active_session"] = new_active_id
            await update.message.reply_text(
                f"■ Closed \"{label}\". Active tab: \"{new_session['label']}\".",
                reply_markup=_ssh_session_keyboard(),
            )
            await _send_tab_state(context.bot, update.effective_chat.id, sessions, new_active_id)
            return SERVERMGR_CMD_INPUT
        context.user_data.pop("servermgr_active_session", None)
        await update.message.reply_text(f"■ Closed \"{label}\". All SSH tabs are now closed.", reply_markup=ReplyKeyboardRemove())
        await _reply_with_servermgr_menu(update, "", context)
        return ConversationHandler.END

    _close_ssh_session(context)
    _close_file_browser(context)
    _close_adv_tools(context)
    context.user_data.pop("servermgr_new_server", None)
    await update.message.reply_text("✕ Operation cancelled.", reply_markup=ReplyKeyboardRemove())
    await _reply_with_servermgr_menu(update, "", context)
    return ConversationHandler.END


def _servers_text_and_keyboard(user_id, sessions: dict = None):
    servers = settings.get_servers(user_id)
    sessions = sessions or {}
    open_server_ids = {s["server_id"] for s in sessions.values()}
    max_servers, max_tabs = subscription.get_limits(user_id)
    text = (
        "▣ Server Manager\n\n"
        "Register your servers here, then run any command on them over SSH.\n"
        "To quickly ping an IP/domain (no server registration needed), use the button below.\n\n"
        f"▦ Plan limits: {len(servers)}/{max_servers} servers, {max_tabs} tab(s)\n\n"
    )
    if sessions:
        text += f"● {len(sessions)}/{_effective_tab_limit(user_id)} terminal tabs open.\n\n"
    keyboard = []
    if servers:
        for i in range(0, len(servers), 2):
            row = servers[i:i + 2]
            keyboard.append([
                InlineKeyboardButton(
                    f"{'⚠ ' if health.get_last_known_status(s['id']) is False else ''}"
                    f"{'● ' if s['id'] in open_server_ids else ''}▣ {s['label']} -> {_short_host(s['host'])}",
                    callback_data=f"servermgr_srv_{s['id']}",
                )
                for s in row
            ])
    else:
        text += "✕ No servers registered yet."
    keyboard.append([
        InlineKeyboardButton("⇝ Ping", callback_data="servermgr_quickping_start"),
        InlineKeyboardButton("+ Add Server", callback_data="servermgr_srv_add"),
    ])
    keyboard.append([InlineKeyboardButton("← Back", callback_data="servermgr_back_to_main")])
    return text, InlineKeyboardMarkup(keyboard)


def _server_detail_text_and_keyboard(server: dict, sessions: dict = None, active_id: str = None, user_id=None):
    auth_line = "▤ SSH Key" if server.get("private_key") else "⚷ Password"
    sessions = sessions or {}
    own_sessions = [(sid, s) for sid, s in sessions.items() if s["server_id"] == server["id"]]
    text = (
        f"▣ {server['label']}\n\n"
        f"Address: `{server['host']}:{server.get('port', 22)}`\n"
        f"Username: `{server['username']}`\n"
        f"Auth: {auth_line}"
    )
    rows = []
    if own_sessions:
        text += f"\n\n● {len(own_sessions)} terminal tab(s) open on this server."
        for sid, _s in own_sessions:
            is_active = sid == active_id
            rows.append([
                InlineKeyboardButton(
                    f"⇄ {_tab_label(sessions, sid)}{' (active)' if is_active else ''}",
                    callback_data=f"servermgr_switch_{sid}",
                ),
                InlineKeyboardButton("■ Close", callback_data=f"servermgr_closetab_{sid}"),
            ])
    if len(sessions) < MAX_SESSIONS:
        run_label = "▣ Open Another Tab" if own_sessions else "▣ Run Command (SSH)"
        rows.append([InlineKeyboardButton(run_label, callback_data=f"servermgr_ssh_start_{server['id']}")])
    sftp_enabled = subscription.get_capabilities(user_id).get("sftp_enabled", True) if user_id is not None else True
    sftp_label = "▥ SFTP" if sftp_enabled else "⚿ SFTP (upgrade to unlock)"
    rows.append([InlineKeyboardButton(sftp_label, callback_data=f"{FILES_START_CB_PREFIX}{server['id']}")])
    adv_enabled = subscription.get_capabilities(user_id).get("advanced_tools_enabled", False) if user_id is not None else False
    adv_label = "⚡ Advanced Tools" if adv_enabled else "⚡ Advanced Tools (upgrade to unlock)"
    rows.append([InlineKeyboardButton(adv_label, callback_data=f"{ADV_START_CB_PREFIX}{server['id']}")])
    monitor_on = server.get("monitor_enabled", True)
    rows.append([
        InlineKeyboardButton("✚ Health Check", callback_data=f"{HEALTH_CHECK_CB_PREFIX}{server['id']}"),
        InlineKeyboardButton(
            f"◎ Alerts: {'ON' if monitor_on else 'OFF'}",
            callback_data=f"{HEALTH_TOGGLE_CB_PREFIX}{server['id']}",
        ),
    ])
    bottom_row = []
    rows.append([InlineKeyboardButton("⚙ Automation", callback_data=f"svauto_menu_{server['id']}")])
    if not own_sessions:
        bottom_row.append(InlineKeyboardButton("⌫ Delete Server", callback_data=f"servermgr_del_{server['id']}"))
    bottom_row.append(InlineKeyboardButton("← Back", callback_data="servermgr_menu"))
    rows.append(bottom_row)
    return text, InlineKeyboardMarkup(rows)


async def _reply_with_servermgr_menu(update: Update, message: str, context: ContextTypes.DEFAULT_TYPE = None):
    user_id = _uid(update)
    sessions = context.user_data.get("servermgr_sessions") if context else None
    text, reply_markup = _servers_text_and_keyboard(user_id, sessions)
    await update.message.reply_text(message or "Server Manager:", reply_markup=get_main_menu(update.effective_user.id))
    try:
        await update.message.reply_text(text, reply_markup=reply_markup, parse_mode="Markdown")
    except Exception as e:
        logger.debug(f"Markdown reply failed, falling back to plain text: {e}")
        await update.message.reply_text(text, reply_markup=reply_markup, parse_mode=None)


async def servermgr_open(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = _uid(update)
    if not subscription.is_active(user_id):
        await update.message.reply_text(_NO_SUBSCRIPTION_TEXT)
        return
    text, reply_markup = _servers_text_and_keyboard(user_id, context.user_data.get("servermgr_sessions"))
    try:
        await update.message.reply_text(text, reply_markup=reply_markup, parse_mode="Markdown")
    except Exception as e:
        logger.debug(f"Markdown reply failed, falling back to plain text: {e}")
        await update.message.reply_text(text, reply_markup=reply_markup, parse_mode=None)


async def servermgr_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    text, reply_markup = _servers_text_and_keyboard(query.from_user.id, context.user_data.get("servermgr_sessions"))
    try:
        await query.edit_message_text(text, reply_markup=reply_markup, parse_mode="Markdown")
    except Exception as e:
        logger.debug(f"Markdown edit failed, falling back to plain text: {e}")
        await query.edit_message_text(text, reply_markup=reply_markup, parse_mode=None)


async def servermgr_back_to_main(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    try:
        await query.edit_message_text("← Returned to the main menu.")
    except Exception:
        pass
    await context.bot.send_message(
        chat_id=query.message.chat_id,
        text="Main menu:",
        reply_markup=get_main_menu(update.effective_user.id),
    )


async def servermgr_srv_detail(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    server_id = query.data.replace("servermgr_srv_", "", 1)
    server = settings.get_server(query.from_user.id, server_id)
    sessions = context.user_data.get("servermgr_sessions", {})
    if not server:
        text, reply_markup = _servers_text_and_keyboard(query.from_user.id, sessions)
        await query.edit_message_text("✕ Server not found.", reply_markup=reply_markup)
        return
    active_id = context.user_data.get("servermgr_active_session")
    text, reply_markup = _server_detail_text_and_keyboard(server, sessions, active_id, user_id=query.from_user.id)
    await query.edit_message_text(text, reply_markup=reply_markup, parse_mode="Markdown")


async def servermgr_health_check(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    server_id = query.data.replace(HEALTH_CHECK_CB_PREFIX, "", 1)
    server = settings.get_server(query.from_user.id, server_id)
    if not server:
        await query.answer("Server not found.", show_alert=True)
        return
    await query.answer("⏳ Checking...")

    check_timeout = health.get_check_timeout()
    snapshot, timed_out = await _run_with_timeout(
        engine.check_health, server, check_timeout, timeout=check_timeout + 5,
    )
    if timed_out:
        text = f"⏱ Health check for \"{server['label']}\" took too long."
    elif isinstance(snapshot, Exception):
        text = f"✕ Health check failed: {snapshot}"
    else:
        text = health.format_health_text(server["label"], snapshot)

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("↻ Restart", callback_data=f"{HEALTH_RESTART_CB_PREFIX}{server['id']}"),
            InlineKeyboardButton("⟲ Cleanup", callback_data=f"{HEALTH_CLEANUP_CB_PREFIX}{server['id']}"),
        ],
        [InlineKeyboardButton("← Back", callback_data=f"servermgr_srv_{server['id']}")],
    ])
    await context.bot.send_message(
        chat_id=query.message.chat_id, text=text, parse_mode="Markdown", reply_markup=keyboard,
    )


async def servermgr_health_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    server_id = query.data.replace(HEALTH_TOGGLE_CB_PREFIX, "", 1)
    server = settings.get_server(query.from_user.id, server_id)
    if not server:
        await query.answer("Server not found.", show_alert=True)
        return

    new_state = not server.get("monitor_enabled", True)
    settings.set_monitor_enabled(query.from_user.id, server_id, new_state)
    await query.answer(f"◎ Alerts {'enabled' if new_state else 'disabled'} for this server.")

    server = settings.get_server(query.from_user.id, server_id)
    sessions = context.user_data.get("servermgr_sessions", {})
    active_id = context.user_data.get("servermgr_active_session")
    text, reply_markup = _server_detail_text_and_keyboard(server, sessions, active_id, user_id=query.from_user.id)
    await query.edit_message_text(text, reply_markup=reply_markup, parse_mode="Markdown")


async def servermgr_restart_confirm_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    server_id = query.data.replace(HEALTH_RESTART_CB_PREFIX, "", 1)
    server = settings.get_server(query.from_user.id, server_id)
    if not server:
        await query.answer("Server not found.", show_alert=True)
        return
    await query.answer()
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("✓ Yes, restart", callback_data=f"{HEALTH_RESTART_CONFIRM_CB_PREFIX}{server_id}"),
         InlineKeyboardButton("✕ Cancel", callback_data=f"servermgr_srv_{server_id}")],
    ])
    await query.edit_message_text(
        f"⚠ Restart \"{server['label']}\"? This reboots the server and will close any open terminal tabs on it.",
        reply_markup=keyboard,
    )


async def servermgr_restart_execute(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    server_id = query.data.replace(HEALTH_RESTART_CONFIRM_CB_PREFIX, "", 1)
    server = settings.get_server(query.from_user.id, server_id)
    if not server:
        await query.answer("Server not found.", show_alert=True)
        return
    await query.answer("⏳ Restarting...")

    result, timed_out = await _run_with_timeout(
        maintenance.restart_server, server, timeout=maintenance.REBOOT_TIMEOUT + 5,
    )
    if timed_out:
        text = f"⏱ Restart request for \"{server['label']}\" took too long."
    elif isinstance(result, Exception):
        text = f"✕ Restart failed: {result}"
    elif not result.get("ok"):
        text = f"✕ Restart failed: {result.get('error') or 'unknown error'}"
    else:
        text = f"↻ \"{server['label']}\" is rebooting now. Give it a minute, then run a Health Check to confirm it's back."

    back_kb = InlineKeyboardMarkup([[InlineKeyboardButton("← Back", callback_data=f"servermgr_srv_{server_id}")]])
    try:
        await query.edit_message_text(text, reply_markup=back_kb)
    except Exception:
        await context.bot.send_message(chat_id=query.message.chat_id, text=text, reply_markup=back_kb)


async def servermgr_cleanup_execute(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    server_id = query.data.replace(HEALTH_CLEANUP_CB_PREFIX, "", 1)
    server = settings.get_server(query.from_user.id, server_id)
    if not server:
        await query.answer("Server not found.", show_alert=True)
        return
    await query.answer("⏳ Cleaning up...")

    result, timed_out = await _run_with_timeout(
        maintenance.cleanup_server, server, timeout=maintenance.CLEANUP_TIMEOUT + 10,
    )
    if timed_out:
        text = f"⏱ Cleanup on \"{server['label']}\" took too long."
    elif isinstance(result, Exception):
        text = f"✕ Cleanup failed: {result}"
    elif not result.get("ok"):
        text = f"✕ Cleanup failed: {result.get('error') or 'unknown error'}"
    else:
        freed_summary = maintenance.format_freed_summary(result)
        text = f"⟲ Cleanup finished on \"{server['label']}\" — approximately {freed_summary}."

    back_kb = InlineKeyboardMarkup([[InlineKeyboardButton("← Back", callback_data=f"servermgr_srv_{server_id}")]])
    try:
        await query.edit_message_text(text, reply_markup=back_kb)
    except Exception:
        await context.bot.send_message(chat_id=query.message.chat_id, text=text, reply_markup=back_kb)


async def servermgr_quickping_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("⇝ Quick Ping\n\nSend the IP or domain you want to ping:")
    await context.bot.send_message(chat_id=query.message.chat_id, text="Address:", reply_markup=_cancel_keyboard())
    return SERVERMGR_QUICKPING_INPUT


async def servermgr_quickping_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    if text in ("/cancel", CANCEL_BUTTON_TEXT):
        await _reply_with_servermgr_menu(update, "✕ Operation cancelled.", context)
        return ConversationHandler.END
    if not text:
        await update.message.reply_text("✕ Send an IP or domain:")
        return SERVERMGR_QUICKPING_INPUT

    await update.message.reply_text("⏳ Pinging...", reply_markup=ReplyKeyboardRemove())
    result, timed_out = await _run_with_timeout(engine.ping_host, text, 4, 2, timeout=20)

    if timed_out:
        reply = f"⏱ Ping to \"{text}\" took too long."
    elif result.get("error") and result.get("loss_percent") is None:
        reply = f"✕ Ping to \"{text}\" failed:\n{result['error']}"
    elif result["ok"]:
        avg = f"{result['avg_ms']:.0f}ms" if result.get("avg_ms") is not None else "-"
        reply = f"✓ \"{text}\" responded.\nAvg latency: {avg} — Packet loss: {result['loss_percent']}%"
    else:
        reply = f"✕ \"{text}\" did not respond to ping (packet loss {result.get('loss_percent', 100)}%)."

    await update.message.reply_text(reply)
    await _reply_with_servermgr_menu(update, "", context)
    return ConversationHandler.END


_ADD_STATE_KEY = "servermgr_new_server"


def _new_server_data(context: ContextTypes.DEFAULT_TYPE) -> dict:
    return context.user_data.setdefault(_ADD_STATE_KEY, {})


def _port_default_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton(f"✓ Use default ({ADD_DEFAULT_PORT})", callback_data=ADD_PORT_DEFAULT_CB)]])


def _user_default_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton(f"✓ Use default ({ADD_DEFAULT_USER})", callback_data=ADD_USER_DEFAULT_CB)]])


def _authtype_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("⚷ Password", callback_data=ADD_AUTH_PASS_CB),
        InlineKeyboardButton("▤ SSH Key", callback_data=ADD_AUTH_KEY_CB),
    ]])


def _passphrase_none_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("⊘ No passphrase", callback_data=ADD_PASSPHRASE_NONE_CB)]])


def _label_default_kb(host: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton(f"✓ Use \"{host}\"", callback_data=ADD_LABEL_DEFAULT_CB)]])


async def _prompt_port(bot, chat_id: int):
    await bot.send_message(
        chat_id=chat_id,
        text="⌁ *Port*\n\nSend the SSH port, or tap below for the default:",
        parse_mode="Markdown",
        reply_markup=_port_default_kb(),
    )


async def _prompt_username(bot, chat_id: int):
    await bot.send_message(
        chat_id=chat_id,
        text="@ *Username*\n\nSend the SSH username, or tap below for the default:",
        parse_mode="Markdown",
        reply_markup=_user_default_kb(),
    )


async def _prompt_authtype(bot, chat_id: int):
    await bot.send_message(
        chat_id=chat_id,
        text="⚿ *Login method*\n\nHow should the bot authenticate?",
        parse_mode="Markdown",
        reply_markup=_authtype_kb(),
    )


async def _prompt_secret(bot, chat_id: int, auth_type: str, retry: bool = False):
    prefix = "✕ That key didn't load. Please paste it again" if retry else "Paste it below"
    if auth_type == "key":
        text = (
            "▤ *SSH Private Key*\n\n"
            f"{prefix} - the full block, including the "
            "`-----BEGIN ... -----` and `-----END ... -----` lines:"
        )
    else:
        text = "⚷ *Password*\n\nSend the SSH password:"
    await bot.send_message(chat_id=chat_id, text=text, parse_mode="Markdown")


async def _prompt_passphrase(bot, chat_id: int):
    await bot.send_message(
        chat_id=chat_id,
        text="⚿ *Key passphrase*\n\nSend the passphrase protecting this key, or tap below if it has none:",
        parse_mode="Markdown",
        reply_markup=_passphrase_none_kb(),
    )


async def _prompt_label(bot, chat_id: int, host: str):
    await bot.send_message(
        chat_id=chat_id,
        text="# *Label*\n\nLast step - send a short name for this server (e.g. \"DE-1\"), "
        "or tap below to just use its address:",
        parse_mode="Markdown",
        reply_markup=_label_default_kb(host),
    )


async def _finish_add_server(update_or_query, context: ContextTypes.DEFAULT_TYPE, chat_id: int, label: str):
    data = context.user_data.pop(_ADD_STATE_KEY, {})
    if data.get("auth_type") == "key":
        server = settings.add_server(
            data["_uid"], label=label, host=data["host"], port=data["port"], username=data["user"],
            private_key=data["secret"], key_passphrase=data.get("passphrase", ""),
        )
        auth_note = "SSH key auth"
    else:
        server = settings.add_server(
            data["_uid"], label=label, host=data["host"], port=data["port"], username=data["user"],
            password=data["secret"],
        )
        auth_note = "password auth"

    await context.bot.send_message(
        chat_id=chat_id, text=f"✓ Server \"{server['label']}\" registered ({auth_note}).",
        reply_markup=ReplyKeyboardRemove(),
    )
    user_id = data["_uid"]
    sessions = context.user_data.get("servermgr_sessions")
    text, reply_markup = _servers_text_and_keyboard(user_id, sessions)
    try:
        await context.bot.send_message(chat_id=chat_id, text=text, reply_markup=reply_markup, parse_mode="Markdown")
    except Exception as e:
        logger.debug(f"Markdown reply failed, falling back to plain text: {e}")
        await context.bot.send_message(chat_id=chat_id, text=text, reply_markup=reply_markup, parse_mode=None)


async def servermgr_srv_add_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id

    if not subscription.is_active(user_id):
        await query.answer("⚿ Your subscription has expired. Please renew it first.", show_alert=True)
        return ConversationHandler.END

    max_servers, _ = subscription.get_limits(user_id)
    if len(settings.get_servers(user_id)) >= max_servers:
        await query.answer(
            f"⚠ Your plan allows up to {max_servers} server(s). Remove one or upgrade your plan to add more.",
            show_alert=True,
        )
        return ConversationHandler.END

    await query.answer()
    context.user_data[_ADD_STATE_KEY] = {"_uid": query.from_user.id}
    await query.edit_message_text("+ *Add Server*\n\nSend the server's IP address or domain:", parse_mode="Markdown")
    await context.bot.send_message(
        chat_id=query.message.chat_id, text="↓ Type the address, or tap Cancel below:", reply_markup=_cancel_keyboard()
    )
    return SERVERMGR_ADD_HOST


async def servermgr_add_host_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    if not text:
        await update.message.reply_text("✕ Please send an address:")
        return SERVERMGR_ADD_HOST
    _new_server_data(context)["host"] = text
    await _prompt_port(context.bot, update.effective_chat.id)
    return SERVERMGR_ADD_PORT


async def servermgr_add_port_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    try:
        port = int(text)
        if not (1 <= port <= 65535):
            raise ValueError
    except ValueError:
        await update.message.reply_text("✕ Invalid port. Send a number between 1-65535, or tap below:", reply_markup=_port_default_kb())
        return SERVERMGR_ADD_PORT
    _new_server_data(context)["port"] = port
    await _prompt_username(context.bot, update.effective_chat.id)
    return SERVERMGR_ADD_USER


async def servermgr_add_port_default_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    _new_server_data(context)["port"] = ADD_DEFAULT_PORT
    try:
        await query.edit_message_text(f"⌁ Port: {ADD_DEFAULT_PORT} (default)")
    except Exception:
        pass
    await _prompt_username(context.bot, query.message.chat_id)
    return SERVERMGR_ADD_USER


async def servermgr_add_user_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    if not text:
        await update.message.reply_text("✕ Please send a username:")
        return SERVERMGR_ADD_USER
    _new_server_data(context)["user"] = text
    await _prompt_authtype(context.bot, update.effective_chat.id)
    return SERVERMGR_ADD_AUTHTYPE


async def servermgr_add_user_default_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    _new_server_data(context)["user"] = ADD_DEFAULT_USER
    try:
        await query.edit_message_text(f"@ Username: {ADD_DEFAULT_USER} (default)")
    except Exception:
        pass
    await _prompt_authtype(context.bot, query.message.chat_id)
    return SERVERMGR_ADD_AUTHTYPE


async def servermgr_add_authtype_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    is_key = query.data == ADD_AUTH_KEY_CB
    auth_type = "key" if is_key else "password"
    _new_server_data(context)["auth_type"] = auth_type
    try:
        await query.edit_message_text(f"⚿ Login method: {'SSH Key' if is_key else 'Password'}")
    except Exception:
        pass
    await _prompt_secret(context.bot, query.message.chat_id, auth_type)
    return SERVERMGR_ADD_SECRET


async def servermgr_add_secret_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    if not text:
        await update.message.reply_text("✕ That was empty. Please send it again:")
        return SERVERMGR_ADD_SECRET

    data = _new_server_data(context)
    data["secret"] = text
    try:
        await update.message.delete()
    except Exception:
        pass

    if data.get("auth_type") == "key":
        await _prompt_passphrase(context.bot, update.effective_chat.id)
        return SERVERMGR_ADD_PASSPHRASE

    await _prompt_label(context.bot, update.effective_chat.id, data["host"])
    return SERVERMGR_ADD_LABEL


async def _validate_key_or_reprompt(bot, chat_id: int, context: ContextTypes.DEFAULT_TYPE) -> bool:
    data = _new_server_data(context)
    _, load_error = engine.try_load_private_key(data["secret"], (data.get("passphrase") or None))
    if load_error:
        await bot.send_message(chat_id=chat_id, text=f"✕ {load_error}")
        await _prompt_secret(bot, chat_id, "key", retry=True)
        return False
    return True


async def servermgr_add_passphrase_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text or ""
    data = _new_server_data(context)
    data["passphrase"] = text.strip()
    try:
        await update.message.delete()
    except Exception:
        pass

    chat_id = update.effective_chat.id
    if not await _validate_key_or_reprompt(context.bot, chat_id, context):
        return SERVERMGR_ADD_SECRET

    await _prompt_label(context.bot, chat_id, data["host"])
    return SERVERMGR_ADD_LABEL


async def servermgr_add_passphrase_none_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = _new_server_data(context)
    data["passphrase"] = ""
    try:
        await query.edit_message_text("⚿ Key passphrase: (none)")
    except Exception:
        pass

    chat_id = query.message.chat_id
    if not await _validate_key_or_reprompt(context.bot, chat_id, context):
        return SERVERMGR_ADD_SECRET

    await _prompt_label(context.bot, chat_id, data["host"])
    return SERVERMGR_ADD_LABEL


async def servermgr_add_label_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    data = _new_server_data(context)
    label = text or data["host"]
    await _finish_add_server(update, context, update.effective_chat.id, label)
    return ConversationHandler.END


async def servermgr_add_label_default_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = _new_server_data(context)
    try:
        await query.edit_message_text(f"# Label: {data['host']}")
    except Exception:
        pass
    await _finish_add_server(update, context, query.message.chat_id, data["host"])
    return ConversationHandler.END


async def servermgr_del_confirm_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    server_id = query.data.replace("servermgr_del_", "", 1)
    server = settings.get_server(query.from_user.id, server_id)
    if not server:
        await query.answer("Server not found.", show_alert=True)
        return
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("✓ Yes, delete", callback_data=f"servermgr_delok_{server_id}"),
         InlineKeyboardButton("✕ Cancel", callback_data=f"servermgr_srv_{server_id}")],
    ])
    await query.edit_message_text(f"Are you sure you want to delete \"{server['label']}\"?", reply_markup=keyboard)


async def servermgr_del_execute(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    server_id = query.data.replace("servermgr_delok_", "", 1)
    settings.remove_server(query.from_user.id, server_id)
    await query.answer("⌫ Deleted.")
    text, reply_markup = _servers_text_and_keyboard(query.from_user.id, context.user_data.get("servermgr_sessions"))
    await query.edit_message_text(text, reply_markup=reply_markup, parse_mode="Markdown")


async def servermgr_trustkey_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    server_id = query.data.replace("servermgr_trustkey_", "", 1)
    server = settings.get_server(query.from_user.id, server_id)
    if not server:
        await query.answer("Server not found.", show_alert=True)
        return
    engine.forget_host_key(server["host"], server.get("port", 22))
    await query.answer("✓ Old key forgotten.")
    sessions = context.user_data.get("servermgr_sessions", {})
    text, reply_markup = _server_detail_text_and_keyboard(server, sessions, context.user_data.get("servermgr_active_session"), user_id=query.from_user.id)
    await query.edit_message_text(
        f"✓ The old host key for \"{server['label']}\" was removed. Tap \"Run Command (SSH)\" "
        f"again to reconnect - the new key will be pinned at that point.",
        reply_markup=reply_markup,
    )


def _human_size(n: int) -> str:
    size = float(n or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f}{unit}" if unit == "B" else f"{size:.1f}{unit}"
        size /= 1024
    return f"{size:.1f}TB"


async def _list_dir_safely(sftp, path: str):
    return await _sftp_call(engine.sftp_listdir, sftp, path)


SERVERMGR_FILES_LASTPATH_KEY = "servermgr_files_lastpath"


def _files_remember_cwd(context: ContextTypes.DEFAULT_TYPE, server_id: str, cwd: str):
    context.user_data.setdefault(SERVERMGR_FILES_LASTPATH_KEY, {})[server_id] = cwd


def _files_status_line(fb: dict) -> str:
    count = len(fb["entries"])
    shown = min(count, SFTP_MAX_LIST_ENTRIES)
    counter = f"{shown} of {count} shown" if count > shown else f"{count} item{'s' if count != 1 else ''}"

    if fb.get("awaiting_upload"):
        return "↑ _Waiting for a file…_"
    if fb.get("awaiting_path"):
        return "✎ _Waiting for a path…_"
    if fb.get("awaiting_url"):
        return "↗ _Waiting for a URL…_"
    if fb.get("awaiting_rename_idx") is not None:
        return "✎ _Waiting for a new name…_"
    if fb.get("awaiting_edit_idx") is not None:
        return "✎ _Waiting for new file content…_"
    if fb.get("awaiting_mkdir"):
        return "＋ _Waiting for a folder name…_"
    return f"_{counter}_"


def _files_hint(fb: dict) -> str:
    if fb.get("awaiting_upload") or fb.get("awaiting_path") or fb.get("awaiting_url") \
            or fb.get("awaiting_rename_idx") is not None or fb.get("awaiting_edit_idx") is not None \
            or fb.get("awaiting_mkdir"):
        return ""
    return "▢ open · ▤ download · ⚙ rename/edit/delete"


def _files_text(fb: dict) -> str:
    hint = _files_hint(fb)
    lines = [
        f"▣ *{fb['label']}*  ·  SFTP",
        f"▢ `{fb['cwd']}`",
        _files_status_line(fb),
    ]
    if hint:
        lines.append(f"_{hint}_")
    return "\n".join(lines)


def _files_keyboard(fb: dict) -> InlineKeyboardMarkup:
    entries = fb["entries"]
    rows = []
    for i, e in enumerate(entries[:SFTP_MAX_LIST_ENTRIES]):
        actions_btn = InlineKeyboardButton("⚙", callback_data=f"{FILES_ACTIONS_CB_PREFIX}{i}")
        if e["is_dir"]:
            rows.append([
                InlineKeyboardButton(f"▢ {e['name']}", callback_data=f"{FILES_NAV_CB_PREFIX}{i}"),
                actions_btn,
            ])
        else:
            rows.append([
                InlineKeyboardButton(
                    f"▤ {e['name']}  ·  {_human_size(e['size'])}",
                    callback_data=f"{FILES_DL_CB_PREFIX}{i}",
                ),
                actions_btn,
            ])

    if rows:
        cwd_label = fb["cwd"] if fb["cwd"] not in ("/", "") else "/"
        rows.append([InlineKeyboardButton(f"── {cwd_label} ──", callback_data="servermgr_noop")])

    nav_row = []
    if fb["cwd"] not in ("/", ""):
        nav_row.append(InlineKeyboardButton("← Up", callback_data=FILES_UP_CB))
    nav_row.append(InlineKeyboardButton("↻ Refresh", callback_data=FILES_REFRESH_CB))
    rows.append(nav_row)

    rows.append([
        InlineKeyboardButton("✎ Go to path", callback_data=FILES_GOTO_CB),
        InlineKeyboardButton("↗ From URL", callback_data=FILES_URLDL_CB),
    ])
    rows.append([
        InlineKeyboardButton("↑ Upload", callback_data=FILES_UPLOAD_HERE_CB),
        InlineKeyboardButton("＋ New folder", callback_data=FILES_MKDIR_CB),
    ])
    rows.append([InlineKeyboardButton("✕ Close", callback_data=FILES_CLOSE_CB)])
    return InlineKeyboardMarkup(rows)


async def _render_file_browser(query, fb: dict):
    try:
        await query.edit_message_text(_files_text(fb), reply_markup=_files_keyboard(fb), parse_mode="Markdown")
    except BadRequest as e:
        if "not modified" not in str(e).lower():
            raise


async def servermgr_files_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id
    server_id = query.data.replace(FILES_START_CB_PREFIX, "", 1)
    server = settings.get_server(user_id, server_id)

    if not subscription.is_active(user_id):
        await query.answer("⚿ Your subscription has expired. Please renew it first.", show_alert=True)
        return ConversationHandler.END

    if not subscription.get_capabilities(user_id).get("sftp_enabled"):
        await query.answer(
            "⚿ The SFTP file browser isn't included in your current plan. "
            "Upgrade from ◆ Subscription to unlock it.",
            show_alert=True,
        )
        return ConversationHandler.END

    if not server:
        await query.answer("Server not found.", show_alert=True)
        return ConversationHandler.END

    _close_file_browser(context)

    await query.answer("⏳ Connecting...")
    client, timed_out = await _run_with_timeout(engine.connect, server, timeout=CONNECT_TIMEOUT)
    if timed_out:
        await query.edit_message_text(f"⏱ Connecting to \"{server['label']}\" took more than {CONNECT_TIMEOUT}s and was cancelled.")
        return ConversationHandler.END
    if isinstance(client, engine.HostKeyChangedError):
        await query.edit_message_text(
            f"⚠ SECURITY WARNING for \"{server['label']}\"\n\n{client}",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("⚠ Trust new key & retry", callback_data=f"servermgr_trustkey_{server_id}")],
                [InlineKeyboardButton("← Back", callback_data=f"servermgr_srv_{server_id}")],
            ]),
        )
        return ConversationHandler.END
    if client is None or isinstance(client, Exception):
        err = f"\n{client}" if isinstance(client, Exception) else ""
        await query.edit_message_text(f"✕ Failed to connect to \"{server['label']}\".{err}")
        return ConversationHandler.END

    sftp, err = await _sftp_call(engine.open_sftp, client, timeout=CONNECT_TIMEOUT)
    if err:
        try:
            client.close()
        except Exception:
            pass
        await query.edit_message_text(f"✕ Failed to open an SFTP session on \"{server['label']}\".\n{err}")
        return ConversationHandler.END

    home_cwd, _err = await _sftp_call(engine.sftp_home_dir, sftp, timeout=CONNECT_TIMEOUT)
    if not isinstance(home_cwd, str) or not home_cwd:
        home_cwd = "/"

    saved_cwd = context.user_data.get(SERVERMGR_FILES_LASTPATH_KEY, {}).get(server_id)
    cwd = saved_cwd or home_cwd
    entries, err = await _list_dir_safely(sftp, cwd)
    if err and saved_cwd:
        cwd = home_cwd
        entries, err = await _list_dir_safely(sftp, cwd)
    if err:
        engine.close_sftp(sftp)
        try:
            client.close()
        except Exception:
            pass
        await query.edit_message_text(f"✕ Could not list \"{cwd}\" on \"{server['label']}\".\n{err}")
        return ConversationHandler.END
    _files_remember_cwd(context, server_id, cwd)

    fb = {
        "server_id": server_id,
        "label": server["label"],
        "client": client,
        "sftp": sftp,
        "cwd": cwd,
        "entries": entries,
        "chat_id": query.message.chat_id,
        "awaiting_upload": False,
        "awaiting_path": False,
        "awaiting_url": False,
        "awaiting_rename_idx": None,
        "awaiting_edit_idx": None,
        "awaiting_mkdir": False,
    }
    context.user_data["servermgr_filebrowser"] = fb
    await _render_file_browser(query, fb)
    return SERVERMGR_FILES_BROWSE


async def servermgr_files_nav(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    fb = context.user_data.get("servermgr_filebrowser")
    if not fb:
        await query.answer("File browser session expired.", show_alert=True)
        return ConversationHandler.END

    idx = int(query.data.replace(FILES_NAV_CB_PREFIX, "", 1))
    entries = fb["entries"]
    if idx < 0 or idx >= len(entries) or not entries[idx]["is_dir"]:
        await query.answer("That folder isn't listed anymore - try refreshing.", show_alert=True)
        return SERVERMGR_FILES_BROWSE

    new_cwd = engine.sftp_join(fb["cwd"], entries[idx]["name"])
    new_entries, err = await _list_dir_safely(fb["sftp"], new_cwd)
    if err:
        await query.answer(f"✕ {err}", show_alert=True)
        return SERVERMGR_FILES_BROWSE

    fb["cwd"], fb["entries"] = new_cwd, new_entries
    _files_remember_cwd(context, fb["server_id"], new_cwd)
    await query.answer()
    await _render_file_browser(query, fb)
    return SERVERMGR_FILES_BROWSE


async def servermgr_files_up(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    fb = context.user_data.get("servermgr_filebrowser")
    if not fb:
        await query.answer("File browser session expired.", show_alert=True)
        return ConversationHandler.END

    new_cwd = engine.sftp_parent(fb["cwd"])
    new_entries, err = await _list_dir_safely(fb["sftp"], new_cwd)
    if err:
        await query.answer(f"✕ {err}", show_alert=True)
        return SERVERMGR_FILES_BROWSE

    fb["cwd"], fb["entries"] = new_cwd, new_entries
    _files_remember_cwd(context, fb["server_id"], new_cwd)
    await query.answer()
    await _render_file_browser(query, fb)
    return SERVERMGR_FILES_BROWSE


async def servermgr_files_refresh(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    fb = context.user_data.get("servermgr_filebrowser")
    if not fb:
        await query.answer("File browser session expired.", show_alert=True)
        return ConversationHandler.END

    new_entries, err = await _list_dir_safely(fb["sftp"], fb["cwd"])
    if err:
        await query.answer(f"✕ {err}", show_alert=True)
        return SERVERMGR_FILES_BROWSE

    fb["entries"] = new_entries
    await query.answer("↻ Refreshed.")
    await _render_file_browser(query, fb)
    return SERVERMGR_FILES_BROWSE


async def servermgr_files_download(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    fb = context.user_data.get("servermgr_filebrowser")
    if not fb:
        await query.answer("File browser session expired.", show_alert=True)
        return ConversationHandler.END

    idx = int(query.data.replace(FILES_DL_CB_PREFIX, "", 1))
    entries = fb["entries"]
    if idx < 0 or idx >= len(entries) or entries[idx]["is_dir"]:
        await query.answer("That file isn't listed anymore - try refreshing.", show_alert=True)
        return SERVERMGR_FILES_BROWSE

    entry = entries[idx]
    if entry["size"] > engine.SFTP_MAX_DOWNLOAD_BYTES:
        await query.answer(
            f"✕ \"{entry['name']}\" is {_human_size(entry['size'])} - too large to send here "
            f"(limit {_human_size(engine.SFTP_MAX_DOWNLOAD_BYTES)}).",
            show_alert=True,
        )
        return SERVERMGR_FILES_BROWSE

    await query.answer()
    remote_path = engine.sftp_join(fb["cwd"], entry["name"])
    local_path = os.path.join(tempfile.gettempdir(), f"svm_{uuid.uuid4().hex}_{entry['name']}")
    status_msg = await context.bot.send_message(chat_id=fb["chat_id"], text=f"Downloading \"{entry['name']}\"...\n{_progress_bar(0)}  0%")
    _, err = await _run_transfer_with_progress(
        engine.sftp_download, fb["sftp"], remote_path, local_path,
        label=f"Downloading \"{entry['name']}\"...", status_msg=status_msg, timeout=FILE_TRANSFER_TIMEOUT,
    )
    try:
        if err:
            await _osf_safe_edit_status(status_msg, f"✕ Download of \"{entry['name']}\" failed: {err}")
        else:
            await _osf_safe_edit_status(status_msg, f"✓ Downloaded \"{entry['name']}\" - sending it now...")
            with open(local_path, "rb") as f:
                await context.bot.send_document(chat_id=fb["chat_id"], document=f, filename=entry["name"])
    finally:
        try:
            os.remove(local_path)
        except Exception:
            pass
    return SERVERMGR_FILES_BROWSE


async def servermgr_files_goto_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    fb = context.user_data.get("servermgr_filebrowser")
    if not fb:
        await query.answer("File browser session expired.", show_alert=True)
        return ConversationHandler.END

    fb["awaiting_path"] = True
    await query.answer()
    await context.bot.send_message(
        chat_id=fb["chat_id"],
        text=f"✎ Send the path to open, e.g. `/opt/terminal-bot` (relative paths are relative to `{fb['cwd']}`).",
        parse_mode="Markdown",
        reply_markup=_cancel_keyboard(),
    )
    return SERVERMGR_FILES_BROWSE


async def _handle_goto_path_text(update: Update, context: ContextTypes.DEFAULT_TYPE, fb: dict):
    raw = (update.message.text or "").strip()
    candidate = raw if raw.startswith("/") else engine.sftp_join(fb["cwd"], raw)

    norm, _err = await _sftp_call(engine.sftp_normalize, fb["sftp"], candidate)
    path = norm if isinstance(norm, str) and norm else candidate

    entries, err = await _list_dir_safely(fb["sftp"], path)
    if err:
        await update.message.reply_text(f"✕ Could not open \"{path}\":\n{err}\n\nSend another path, or tap Cancel.")
        return SERVERMGR_FILES_BROWSE

    fb["awaiting_path"] = False
    fb["cwd"], fb["entries"] = path, entries
    _files_remember_cwd(context, fb["server_id"], path)
    await update.message.reply_text("✓ Moved.", reply_markup=ReplyKeyboardRemove())
    await context.bot.send_message(
        chat_id=fb["chat_id"], text=_files_text(fb), reply_markup=_files_keyboard(fb), parse_mode="Markdown",
    )
    return SERVERMGR_FILES_BROWSE


async def servermgr_files_urldl_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    fb = context.user_data.get("servermgr_filebrowser")
    if not fb:
        await query.answer("File browser session expired.", show_alert=True)
        return ConversationHandler.END

    fb["awaiting_url"] = True
    await query.answer()
    await context.bot.send_message(
        chat_id=fb["chat_id"],
        text=(
            f"↗ Send the URL to upload directly into:\n`{fb['cwd']}`\n\n"
            f"The server fetches it itself with wget/curl, so there's no Telegram size limit - "
            f"but `wget` or `curl` must be installed on the server."
        ),
        parse_mode="Markdown",
        reply_markup=_cancel_keyboard(),
    )
    return SERVERMGR_FILES_BROWSE


_URL_RE = re.compile(r"^https?://", re.IGNORECASE)


async def _handle_url_download_text(update: Update, context: ContextTypes.DEFAULT_TYPE, fb: dict):
    url = (update.message.text or "").strip()
    if not _URL_RE.match(url):
        await update.message.reply_text("✕ That doesn't look like an http(s) URL. Send a valid URL, or tap Cancel.")
        return SERVERMGR_FILES_BROWSE

    filename = urllib.parse.unquote(os.path.basename(urllib.parse.urlsplit(url).path))
    if not filename:
        filename = f"download_{uuid.uuid4().hex[:8]}"

    fb["awaiting_url"] = False
    status_msg = await update.message.reply_text(
        f"Downloading on the server into:\n`{fb['cwd']}/{filename}`\n{_progress_bar(0)}  0%",
        parse_mode="Markdown", reply_markup=ReplyKeyboardRemove(),
    )
    result, err = await _run_transfer_with_progress(
        engine.remote_download_url, fb["client"], fb["cwd"], filename, url,
        label=f"Downloading on the server into:\n`{fb['cwd']}/{filename}`",
        status_msg=status_msg, timeout=URL_DOWNLOAD_TIMEOUT + 15,
    )

    if err:
        await status_msg.edit_text(f"✕ Upload failed: {err}")
        return SERVERMGR_FILES_BROWSE
    if result.get("error"):
        await status_msg.edit_text(f"✕ Upload failed: {result['error']}")
        return SERVERMGR_FILES_BROWSE
    if not result.get("ok"):
        stderr_tail = (result.get("stderr") or "").strip()[-500:]
        text = f"✕ Upload failed (exit code {result.get('exit_status')})."
        if stderr_tail:
            text += f"\n```\n{stderr_tail}\n```"
        await status_msg.edit_text(text, parse_mode="Markdown")
        return SERVERMGR_FILES_BROWSE

    new_entries, list_err = await _list_dir_safely(fb["sftp"], fb["cwd"])
    if not list_err:
        fb["entries"] = new_entries
    await status_msg.edit_text(f"✓ Uploaded \"{filename}\" directly on the server.")
    await context.bot.send_message(
        chat_id=fb["chat_id"],
        text=_files_text(fb),
        reply_markup=_files_keyboard(fb),
        parse_mode="Markdown",
    )
    return SERVERMGR_FILES_BROWSE


async def servermgr_files_mkdir_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    fb = context.user_data.get("servermgr_filebrowser")
    if not fb:
        await query.answer("File browser session expired.", show_alert=True)
        return ConversationHandler.END

    fb["awaiting_mkdir"] = True
    await query.answer()
    await context.bot.send_message(
        chat_id=fb["chat_id"],
        text=f"＋ Send the name for the new folder inside:\n`{fb['cwd']}`",
        parse_mode="Markdown",
        reply_markup=_cancel_keyboard(),
    )
    return SERVERMGR_FILES_BROWSE


async def _handle_mkdir_text(update: Update, context: ContextTypes.DEFAULT_TYPE, fb: dict):
    name = (update.message.text or "").strip()
    if not name or "/" in name:
        await update.message.reply_text("✕ That's not a valid folder name - it can't be empty or contain \"/\". Send another name, or tap Cancel.")
        return SERVERMGR_FILES_BROWSE

    new_path = engine.sftp_join(fb["cwd"], name)
    fb["awaiting_mkdir"] = False
    status_msg = await update.message.reply_text("⏳ Creating folder...", reply_markup=ReplyKeyboardRemove())
    _, err = await _sftp_call(engine.sftp_mkdir, fb["sftp"], new_path)

    if err:
        try:
            await status_msg.edit_text(f"✕ Could not create folder: {err}")
        except BadRequest as e:
            logger.debug(f"could not edit mkdir-failure status message: {e}")
            await context.bot.send_message(chat_id=fb["chat_id"], text=f"✕ Could not create folder: {err}")
    else:
        new_entries, list_err = await _list_dir_safely(fb["sftp"], fb["cwd"])
        if not list_err:
            fb["entries"] = new_entries
        try:
            await status_msg.edit_text(f"✓ Created folder \"{name}\".")
        except BadRequest as e:
            logger.debug(f"could not edit mkdir-success status message: {e}")
            await context.bot.send_message(chat_id=fb["chat_id"], text=f"✓ Created folder \"{name}\".")

    await context.bot.send_message(chat_id=fb["chat_id"], text=_files_text(fb), reply_markup=_files_keyboard(fb), parse_mode="Markdown")
    return SERVERMGR_FILES_BROWSE


async def servermgr_files_text_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    fb = context.user_data.get("servermgr_filebrowser")
    if not fb:
        return ConversationHandler.END
    if fb.get("awaiting_edit_idx") is not None:
        return await _handle_edit_save_text(update, context, fb)
    if fb.get("awaiting_rename_idx") is not None:
        return await _handle_rename_text(update, context, fb)
    if fb.get("awaiting_path"):
        return await _handle_goto_path_text(update, context, fb)
    if fb.get("awaiting_url"):
        return await _handle_url_download_text(update, context, fb)
    if fb.get("awaiting_mkdir"):
        return await _handle_mkdir_text(update, context, fb)
    await update.message.reply_text("Tap a button below, or use \"✎ Go to path\" / \"↗ Upload from URL\".")
    return SERVERMGR_FILES_BROWSE


async def servermgr_files_upload_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    fb = context.user_data.get("servermgr_filebrowser")
    if not fb:
        await query.answer("File browser session expired.", show_alert=True)
        return ConversationHandler.END

    fb["awaiting_upload"] = True
    await query.answer()
    await context.bot.send_message(
        chat_id=fb["chat_id"],
        text=(
            f"↑ Send the file to upload into:\n`{fb['cwd']}`\n\n"
            f"Send it as a Telegram *document* (not a photo), up to {_human_size(engine.SFTP_MAX_UPLOAD_BYTES)}."
        ),
        parse_mode="Markdown",
        reply_markup=_cancel_keyboard(),
    )
    return SERVERMGR_FILES_BROWSE


async def servermgr_files_upload_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    fb = context.user_data.get("servermgr_filebrowser")
    if not fb:
        return ConversationHandler.END
    if not fb.get("awaiting_upload"):
        await update.message.reply_text("Tap \"↑ Upload file here\" first, then send the file.")
        return SERVERMGR_FILES_BROWSE

    doc = update.message.document
    if doc.file_size and doc.file_size > engine.SFTP_MAX_UPLOAD_BYTES:
        await update.message.reply_text(
            f"✕ \"{doc.file_name}\" is {_human_size(doc.file_size)} - too large to upload here "
            f"(limit {_human_size(engine.SFTP_MAX_UPLOAD_BYTES)})."
        )
        return SERVERMGR_FILES_BROWSE

    local_path = os.path.join(tempfile.gettempdir(), f"svm_{uuid.uuid4().hex}_{doc.file_name}")
    try:
        tg_file = await doc.get_file()
        await tg_file.download_to_drive(local_path)
    except Exception as e:
        try:
            os.remove(local_path)
        except Exception:
            pass
        await update.message.reply_text(f"✕ Upload of \"{doc.file_name}\" failed: {str(e)[:300]}")
        return SERVERMGR_FILES_BROWSE

    buffer = fb.setdefault("upload_buffer", [])
    buffer.append({"local_path": local_path, "filename": doc.file_name})

    if context.job_queue is not None:
        job_name = f"svm_uploadbatch_{fb['chat_id']}"
        for job in context.job_queue.get_jobs_by_name(job_name):
            job.schedule_removal()
        context.job_queue.run_once(
            _finalize_servermgr_upload_batch,
            when=UPLOAD_BATCH_DEBOUNCE,
            chat_id=fb["chat_id"],
            user_id=update.effective_user.id,
            name=job_name,
        )
    else:
        await _finalize_servermgr_upload_batch_for(fb, context)

    return SERVERMGR_FILES_BROWSE


async def _finalize_servermgr_upload_batch(context: ContextTypes.DEFAULT_TYPE):
    fb = context.user_data.get("servermgr_filebrowser")
    if not fb:
        return
    await _finalize_servermgr_upload_batch_for(fb, context)


async def _finalize_servermgr_upload_batch_for(fb: dict, context: ContextTypes.DEFAULT_TYPE):
    buffer = fb.pop("upload_buffer", [])
    if not buffer:
        return
    fb["awaiting_upload"] = False

    status_msg = await context.bot.send_message(
        chat_id=fb["chat_id"],
        text=f"Uploading \"{buffer[0]['filename']}\"...\n{_progress_bar(0)}  0%" if len(buffer) == 1
        else f"Uploading 1/{len(buffer)}: \"{buffer[0]['filename']}\"...\n{_progress_bar(0)}  0%",
        reply_markup=ReplyKeyboardRemove(),
    )

    conflicts, clean = [], []
    for item in buffer:
        item["remote_path"] = engine.sftp_join(fb["cwd"], item["filename"])
        exists, err = await _sftp_call(engine.sftp_exists, fb["sftp"], item["remote_path"])
        if err:
            item["check_error"] = err
            clean.append(item)
        elif exists:
            conflicts.append(item)
        else:
            clean.append(item)

    uploaded, failed = [], []
    for i, item in enumerate(clean, start=1):
        if item.get("check_error"):
            failed.append(f"{item['filename']}: {item['check_error']}")
            try:
                os.remove(item["local_path"])
            except Exception:
                pass
            continue
        label = f"Uploading \"{item['filename']}\"..." if len(clean) == 1 else f"Uploading {i}/{len(clean)}: \"{item['filename']}\"..."
        _, err = await _run_transfer_with_progress(
            engine.sftp_upload, fb["sftp"], item["local_path"], item["remote_path"],
            label=label, status_msg=status_msg, timeout=FILE_TRANSFER_TIMEOUT,
        )
        try:
            os.remove(item["local_path"])
        except Exception:
            pass
        if err:
            failed.append(f"{item['filename']}: {err}")
        else:
            uploaded.append(item["filename"])

    lines = []
    if uploaded:
        if len(uploaded) == 1:
            lines.append(f"✓ Uploaded \"{uploaded[0]}\".")
        else:
            lines.append(f"✓ Uploaded {len(uploaded)} files: " + ", ".join(f'"{n}"' for n in uploaded))
    if failed:
        lines.append("✕ Failed: " + "; ".join(failed))

    await _osf_safe_edit_status(status_msg, "\n".join(lines) if lines else "…")

    if conflicts:
        fb["pending_uploads"] = conflicts
        names = "\n".join(f"• `{c['filename']}`" for c in conflicts)
        if len(conflicts) == 1:
            text = f"⚠ `{conflicts[0]['filename']}` already exists in `{fb['cwd']}`. Overwrite it?"
        else:
            text = f"⚠ {len(conflicts)} files already exist in `{fb['cwd']}`:\n{names}\n\nOverwrite all of them?"
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("✓ Overwrite" + (" all" if len(conflicts) > 1 else ""), callback_data=FILES_OVERWRITE_CB),
             InlineKeyboardButton("✕ Cancel", callback_data=FILES_OVERWRITE_CANCEL_CB)],
        ])
        await context.bot.send_message(
            chat_id=fb["chat_id"], text=text, reply_markup=keyboard, parse_mode="Markdown",
        )
        return

    new_entries, list_err = await _list_dir_safely(fb["sftp"], fb["cwd"])
    if not list_err:
        fb["entries"] = new_entries
    await context.bot.send_message(
        chat_id=fb["chat_id"], text=_files_text(fb), reply_markup=_files_keyboard(fb), parse_mode="Markdown",
    )


async def _osf_safe_edit_status(status_msg, text: str):
    try:
        await status_msg.edit_text(text)
    except BadRequest:
        try:
            await status_msg.reply_text(text)
        except Exception:
            pass


async def servermgr_files_overwrite_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    fb = context.user_data.get("servermgr_filebrowser")
    if not fb:
        await query.answer("File browser session expired.", show_alert=True)
        return ConversationHandler.END

    pending = fb.pop("pending_uploads", None)
    if not pending:
        await query.answer("Nothing pending - try uploading again.", show_alert=True)
        return SERVERMGR_FILES_BROWSE

    await query.answer()
    uploaded, failed = [], []
    for i, item in enumerate(pending, start=1):
        label = f"Uploading \"{item['filename']}\"..." if len(pending) == 1 else f"Uploading {i}/{len(pending)}: \"{item['filename']}\"..."
        _, err = await _run_transfer_with_progress(
            engine.sftp_upload, fb["sftp"], item["local_path"], item["remote_path"],
            label=label, status_msg=query.message, timeout=FILE_TRANSFER_TIMEOUT,
        )
        try:
            os.remove(item["local_path"])
        except Exception:
            pass
        if err:
            failed.append(f"{item['filename']}: {err}")
        else:
            uploaded.append(item["filename"])

    lines = []
    if uploaded:
        if len(uploaded) == 1:
            lines.append(f"✓ Uploaded \"{uploaded[0]}\" (overwritten).")
        else:
            lines.append(f"✓ Uploaded {len(uploaded)} files (overwritten): " + ", ".join(f'"{n}"' for n in uploaded))
    if failed:
        lines.append("✕ Failed: " + "; ".join(failed))
    await _osf_safe_edit_status(query.message, "\n".join(lines))

    new_entries, list_err = await _list_dir_safely(fb["sftp"], fb["cwd"])
    if not list_err:
        fb["entries"] = new_entries
    await context.bot.send_message(
        chat_id=fb["chat_id"], text=_files_text(fb), reply_markup=_files_keyboard(fb), parse_mode="Markdown",
    )
    return SERVERMGR_FILES_BROWSE


async def servermgr_files_overwrite_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    fb = context.user_data.get("servermgr_filebrowser")
    if not fb:
        await query.answer("File browser session expired.", show_alert=True)
        return ConversationHandler.END

    pending = fb.pop("pending_uploads", None)
    await query.answer()
    if pending:
        for item in pending:
            try:
                os.remove(item["local_path"])
            except Exception:
                pass
        if len(pending) == 1:
            text = f"✕ Upload of \"{pending[0]['filename']}\" cancelled - existing file kept."
        else:
            text = f"✕ Upload of {len(pending)} files cancelled - existing files kept."
    else:
        text = "✕ Cancelled."
    await _osf_safe_edit_status(query.message, text)
    return SERVERMGR_FILES_BROWSE


def _entry_at(fb: dict, idx: int):
    entries = fb["entries"]
    if idx < 0 or idx >= len(entries):
        return None
    return entries[idx]


async def servermgr_files_actions_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    fb = context.user_data.get("servermgr_filebrowser")
    if not fb:
        await query.answer("File browser session expired.", show_alert=True)
        return ConversationHandler.END

    idx = int(query.data.replace(FILES_ACTIONS_CB_PREFIX, "", 1))
    entry = _entry_at(fb, idx)
    if entry is None:
        await query.answer("That item isn't listed anymore - try refreshing.", show_alert=True)
        return SERVERMGR_FILES_BROWSE

    kind = "▢ folder" if entry["is_dir"] else "▤ file"
    await query.answer()
    rows = []
    if not entry["is_dir"]:
        rows.append([InlineKeyboardButton("✎ Edit", callback_data=f"{FILES_EDIT_CB_PREFIX}{idx}")])
    rows.append([InlineKeyboardButton("✎ Rename", callback_data=f"{FILES_RENAME_CB_PREFIX}{idx}")])
    rows.append([InlineKeyboardButton("⌫ Delete", callback_data=f"{FILES_DELCONFIRM_CB_PREFIX}{idx}")])
    rows.append([InlineKeyboardButton("← Back", callback_data=FILES_BACK_CB)])
    keyboard = InlineKeyboardMarkup(rows)
    await query.edit_message_text(
        f"{kind} `{entry['name']}`\n\nWhat would you like to do?",
        reply_markup=keyboard, parse_mode="Markdown",
    )
    return SERVERMGR_FILES_BROWSE


async def servermgr_files_rename_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    fb = context.user_data.get("servermgr_filebrowser")
    if not fb:
        await query.answer("File browser session expired.", show_alert=True)
        return ConversationHandler.END

    idx = int(query.data.replace(FILES_RENAME_CB_PREFIX, "", 1))
    entry = _entry_at(fb, idx)
    if entry is None:
        await query.answer("That item isn't listed anymore - try refreshing.", show_alert=True)
        return SERVERMGR_FILES_BROWSE

    fb["awaiting_rename_idx"] = idx
    await query.answer()
    try:
        await query.edit_message_text(f"✎ Renaming `{entry['name']}`…", parse_mode="Markdown")
    except BadRequest as e:
        if "not modified" not in str(e).lower():
            logger.debug(f"could not clear actions menu before rename: {e}")
    await context.bot.send_message(
        chat_id=fb["chat_id"],
        text=f"✎ Send the new name for `{entry['name']}` (name only, it stays in the same folder).",
        parse_mode="Markdown",
        reply_markup=_cancel_keyboard(),
    )
    return SERVERMGR_FILES_BROWSE


async def _handle_rename_text(update: Update, context: ContextTypes.DEFAULT_TYPE, fb: dict):
    idx = fb.get("awaiting_rename_idx")
    entry = _entry_at(fb, idx) if idx is not None else None
    if entry is None:
        fb["awaiting_rename_idx"] = None
        await update.message.reply_text(
            "✕ That item isn't listed anymore - try refreshing.", reply_markup=ReplyKeyboardRemove(),
        )
        await context.bot.send_message(
            chat_id=fb["chat_id"], text=_files_text(fb), reply_markup=_files_keyboard(fb), parse_mode="Markdown",
        )
        return SERVERMGR_FILES_BROWSE

    new_name = (update.message.text or "").strip()
    if not new_name or "/" in new_name:
        await update.message.reply_text("✕ That's not a valid name - it can't be empty or contain \"/\". Send another name, or tap Cancel.")
        return SERVERMGR_FILES_BROWSE

    old_path = engine.sftp_join(fb["cwd"], entry["name"])
    new_path = engine.sftp_join(fb["cwd"], new_name)
    fb["awaiting_rename_idx"] = None
    status_msg = await update.message.reply_text("⏳ Renaming...", reply_markup=ReplyKeyboardRemove())
    _, err = await _sftp_call(engine.sftp_rename, fb["sftp"], old_path, new_path)

    if err:
        try:
            await status_msg.edit_text(f"✕ Rename failed: {err}")
        except BadRequest as e:
            logger.debug(f"could not edit rename-failure status message: {e}")
            await context.bot.send_message(chat_id=fb["chat_id"], text=f"✕ Rename failed: {err}")
    else:
        new_entries, list_err = await _list_dir_safely(fb["sftp"], fb["cwd"])
        if not list_err:
            fb["entries"] = new_entries
        try:
            await status_msg.edit_text(f"✓ Renamed \"{entry['name']}\" to \"{new_name}\".")
        except BadRequest as e:
            logger.debug(f"could not edit rename-success status message: {e}")
            await context.bot.send_message(
                chat_id=fb["chat_id"], text=f"✓ Renamed \"{entry['name']}\" to \"{new_name}\"."
            )
        await context.bot.send_message(
            chat_id=fb["chat_id"],
            text=_files_text(fb),
            reply_markup=_files_keyboard(fb),
            parse_mode="Markdown",
        )
        return SERVERMGR_FILES_BROWSE

    await context.bot.send_message(chat_id=fb["chat_id"], text=_files_text(fb), reply_markup=_files_keyboard(fb), parse_mode="Markdown")
    return SERVERMGR_FILES_BROWSE


async def servermgr_files_edit_open(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    fb = context.user_data.get("servermgr_filebrowser")
    if not fb:
        await query.answer("File browser session expired.", show_alert=True)
        return ConversationHandler.END

    idx = int(query.data.replace(FILES_EDIT_CB_PREFIX, "", 1))
    entry = _entry_at(fb, idx)
    if entry is None or entry["is_dir"]:
        await query.answer("That file isn't listed anymore - try refreshing.", show_alert=True)
        return SERVERMGR_FILES_BROWSE

    path = engine.sftp_join(fb["cwd"], entry["name"])
    await query.answer("⏳ Opening...")
    content, err = await _sftp_call(engine.sftp_read_text, fb["sftp"], path, engine.SFTP_EDITOR_MAX_BYTES)
    if err:
        try:
            await query.edit_message_text(f"✕ Could not open \"{entry['name']}\" for editing.\n{err}")
        except BadRequest as e:
            logger.debug(f"could not show edit-open failure: {e}")
        await context.bot.send_message(
            chat_id=fb["chat_id"], text=_files_text(fb), reply_markup=_files_keyboard(fb), parse_mode="Markdown",
        )
        return SERVERMGR_FILES_BROWSE

    fb["awaiting_edit_idx"] = idx
    try:
        await query.edit_message_text(f"✎ Opening `{entry['name']}`…", parse_mode="Markdown")
    except BadRequest as e:
        if "not modified" not in str(e).lower():
            logger.debug(f"could not clear actions menu before edit: {e}")

    body = html.escape(content) if content else " "
    text = (
        f"✎ <b>{html.escape(entry['name'])}</b>\n"
        f"▢ <code>{html.escape(fb['cwd'])}</code>  ·  {_human_size(entry['size'])}\n\n"
        f"<pre>{body}</pre>\n\n"
        f"✎ Reply with the new content to save, or tap Cancel below."
    )
    await context.bot.send_message(
        chat_id=fb["chat_id"], text=text, parse_mode="HTML", reply_markup=_cancel_keyboard(),
    )
    return SERVERMGR_FILES_BROWSE


async def _handle_edit_save_text(update: Update, context: ContextTypes.DEFAULT_TYPE, fb: dict):
    idx = fb.get("awaiting_edit_idx")
    entry = _entry_at(fb, idx) if idx is not None else None
    if entry is None:
        fb["awaiting_edit_idx"] = None
        await update.message.reply_text(
            "✕ That file isn't listed anymore - try refreshing.", reply_markup=ReplyKeyboardRemove(),
        )
        await context.bot.send_message(
            chat_id=fb["chat_id"], text=_files_text(fb), reply_markup=_files_keyboard(fb), parse_mode="Markdown",
        )
        return SERVERMGR_FILES_BROWSE

    new_content = update.message.text or ""
    if len(new_content.encode("utf-8")) > engine.SFTP_EDITOR_MAX_BYTES:
        await update.message.reply_text(
            f"✕ That's over {engine.SFTP_EDITOR_MAX_BYTES} bytes - too big to save here. "
            f"Send something shorter, or tap Cancel."
        )
        return SERVERMGR_FILES_BROWSE

    path = engine.sftp_join(fb["cwd"], entry["name"])
    fb["awaiting_edit_idx"] = None
    status_msg = await update.message.reply_text("⏳ Saving...", reply_markup=ReplyKeyboardRemove())
    _, err = await _sftp_call(engine.sftp_write_text, fb["sftp"], path, new_content)

    if err:
        try:
            await status_msg.edit_text(f"✕ Save failed: {err}")
        except BadRequest as e:
            logger.debug(f"could not edit save-failure status message: {e}")
            await context.bot.send_message(chat_id=fb["chat_id"], text=f"✕ Save failed: {err}")
    else:
        new_entries, list_err = await _list_dir_safely(fb["sftp"], fb["cwd"])
        if not list_err:
            fb["entries"] = new_entries
        try:
            await status_msg.edit_text(f"✓ Saved \"{entry['name']}\".")
        except BadRequest as e:
            logger.debug(f"could not edit save-success status message: {e}")
            await context.bot.send_message(chat_id=fb["chat_id"], text=f"✓ Saved \"{entry['name']}\".")

    await context.bot.send_message(
        chat_id=fb["chat_id"], text=_files_text(fb), reply_markup=_files_keyboard(fb), parse_mode="Markdown",
    )
    return SERVERMGR_FILES_BROWSE


async def servermgr_files_delete_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    fb = context.user_data.get("servermgr_filebrowser")
    if not fb:
        await query.answer("File browser session expired.", show_alert=True)
        return ConversationHandler.END

    idx = int(query.data.replace(FILES_DELCONFIRM_CB_PREFIX, "", 1))
    entry = _entry_at(fb, idx)
    if entry is None:
        await query.answer("That item isn't listed anymore - try refreshing.", show_alert=True)
        return SERVERMGR_FILES_BROWSE

    await query.answer()
    warn = "\n\n⚠ This will delete the folder and *everything inside it*." if entry["is_dir"] else ""
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("✓ Yes, delete", callback_data=f"{FILES_DELOK_CB_PREFIX}{idx}"),
         InlineKeyboardButton("✕ Cancel", callback_data=FILES_BACK_CB)],
    ])
    await query.edit_message_text(
        f"Are you sure you want to delete `{entry['name']}`?{warn}",
        reply_markup=keyboard, parse_mode="Markdown",
    )
    return SERVERMGR_FILES_BROWSE


async def servermgr_files_delete_execute(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    fb = context.user_data.get("servermgr_filebrowser")
    if not fb:
        await query.answer("File browser session expired.", show_alert=True)
        return ConversationHandler.END

    idx = int(query.data.replace(FILES_DELOK_CB_PREFIX, "", 1))
    entry = _entry_at(fb, idx)
    if entry is None:
        await query.answer("That item isn't listed anymore - try refreshing.", show_alert=True)
        return SERVERMGR_FILES_BROWSE

    path = engine.sftp_join(fb["cwd"], entry["name"])
    _, err = await _sftp_call(
        engine.sftp_delete_recursive, fb["sftp"], path, entry["is_dir"], timeout=FILE_TRANSFER_TIMEOUT,
    )

    async def _popup(text: str):
        try:
            await query.answer(text[:200], show_alert=True)
        except BadRequest:
            pass

    if err:
        await _popup(f"✕ Delete failed: {err}")
        await _render_file_browser(query, fb)
        return SERVERMGR_FILES_BROWSE

    new_entries, list_err = await _list_dir_safely(fb["sftp"], fb["cwd"])
    if not list_err:
        fb["entries"] = new_entries
    await _popup(f"✓ Deleted \"{entry['name']}\".")
    await _render_file_browser(query, fb)
    return SERVERMGR_FILES_BROWSE


async def servermgr_files_back(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    fb = context.user_data.get("servermgr_filebrowser")
    if not fb:
        await query.answer("File browser session expired.", show_alert=True)
        return ConversationHandler.END

    await query.answer()
    await _render_file_browser(query, fb)
    return SERVERMGR_FILES_BROWSE


async def servermgr_files_close(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    fb = context.user_data.get("servermgr_filebrowser")
    label = fb["label"] if fb else "session"
    server_id = fb.get("server_id") if fb else None
    _close_file_browser(context)
    await query.answer()

    sessions = context.user_data.get("servermgr_sessions", {})
    active_id = context.user_data.get("servermgr_active_session")
    has_active_tab = bool(sessions and active_id in sessions)

    keyboard = None
    if not has_active_tab and server_id:
        keyboard = InlineKeyboardMarkup(
            [[InlineKeyboardButton("← Back to server", callback_data=f"servermgr_srv_{server_id}")]]
        )
    try:
        await query.edit_message_text(f"■ Closed the file browser for \"{label}\".", reply_markup=keyboard)
    except Exception:
        pass

    if has_active_tab:
        await _send_tab_state(context.bot, query.message.chat_id, sessions, active_id)

    return ConversationHandler.END


# ======================================================================
# ====================== Advanced Tools (SSH) =========================
# Remote equivalents of the Owner Server's process manager / systemd
# control / crontab editor / log tail (see ServerManager/owner_server.py
# and ServerManager/advanced.py) - gated per-plan via
# subscription.get_capabilities()["advanced_tools_enabled"].
# ======================================================================

def _adv_menu_text_and_keyboard(adv: dict):
    text = f"⚡ *Advanced Tools* — `{adv['label']}`\n\nProcess manager, services, crontab."
    keyboard = [
        [
            InlineKeyboardButton("▤ Processes (CPU)", callback_data=f"{ADV_PROC_CB_PREFIX}cpu"),
            InlineKeyboardButton("▤ Processes (RAM)", callback_data=f"{ADV_PROC_CB_PREFIX}ram"),
        ],
        [
            InlineKeyboardButton("⚙ Services", callback_data=ADV_SVC_CB),
            InlineKeyboardButton("⏰ Crontab", callback_data=ADV_CRON_VIEW_CB),
        ],
        [InlineKeyboardButton("■ Close", callback_data=ADV_CLOSE_CB)],
    ]
    return text, InlineKeyboardMarkup(keyboard)


def _adv_back_kb(extra_row=None):
    rows = []
    if extra_row:
        rows.append(extra_row)
    rows.append([InlineKeyboardButton("← Back", callback_data=ADV_MENU_CB)])
    return InlineKeyboardMarkup(rows)


async def servermgr_adv_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id
    server_id = query.data.replace(ADV_START_CB_PREFIX, "", 1)
    server = settings.get_server(user_id, server_id)
    if not server:
        await query.answer("Server not found.", show_alert=True)
        return ConversationHandler.END

    if not subscription.get_capabilities(user_id).get("advanced_tools_enabled", False):
        await query.answer()
        await query.edit_message_text(
            "⚡ Advanced Tools (process manager, services, crontab) isn't included in your "
            "current plan.\n\nTap ◆ Subscription on the main menu to upgrade.",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("← Back", callback_data=f"servermgr_srv_{server_id}")]]
            ),
        )
        return ConversationHandler.END

    await query.answer("⏳ Connecting...")
    client, timed_out = await _run_with_timeout(engine.connect, server, timeout=CONNECT_TIMEOUT)
    if timed_out:
        await query.edit_message_text(
            "⏱ Connection timed out.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("← Back", callback_data=f"servermgr_srv_{server_id}")]]),
        )
        return ConversationHandler.END
    if isinstance(client, Exception):
        await query.edit_message_text(
            f"✕ Could not connect: {client}",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("← Back", callback_data=f"servermgr_srv_{server_id}")]]),
        )
        return ConversationHandler.END

    _close_adv_tools(context)
    context.user_data["servermgr_adv"] = {
        "server_id": server_id, "label": server["label"], "client": client,
        "chat_id": query.message.chat_id, "units": [], "svc_filter_mode": "all",
        "awaiting_cron_edit": False,
        "svc_log_channel": None, "svc_log_idx": None,
    }
    text, reply_markup = _adv_menu_text_and_keyboard(context.user_data["servermgr_adv"])
    await query.edit_message_text(text, reply_markup=reply_markup, parse_mode="Markdown")
    return SERVERMGR_ADV_BROWSE


async def servermgr_adv_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    adv = context.user_data.get("servermgr_adv")
    if not adv:
        await query.answer("Session expired.", show_alert=True)
        return ConversationHandler.END
    await query.answer()
    adv["awaiting_cron_edit"] = False
    _adv_svc_log_stop_active(adv)
    text, reply_markup = _adv_menu_text_and_keyboard(adv)
    await query.edit_message_text(text, reply_markup=reply_markup, parse_mode="Markdown")
    return SERVERMGR_ADV_BROWSE


async def servermgr_adv_close(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    adv = context.user_data.get("servermgr_adv")
    label = adv["label"] if adv else "session"
    server_id = adv.get("server_id") if adv else None
    _close_adv_tools(context)
    await query.answer()
    keyboard = None
    if server_id:
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("← Back to server", callback_data=f"servermgr_srv_{server_id}")]])
    try:
        await query.edit_message_text(f"■ Closed Advanced Tools for \"{label}\".", reply_markup=keyboard)
    except Exception:
        pass
    return ConversationHandler.END


# ---------------------- Processes ----------------------------------------

async def servermgr_adv_processes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    adv = context.user_data.get("servermgr_adv")
    if not adv:
        await query.answer("Session expired.", show_alert=True)
        return ConversationHandler.END
    by = query.data.replace(ADV_PROC_CB_PREFIX, "", 1)
    await query.answer("⏳ Loading...")

    result, timed_out = await _run_with_timeout(
        engine.remote_top_processes, adv["client"], by, 10, timeout=ADV_CMD_TIMEOUT,
    )
    if timed_out:
        text = "⏱ Timed out."
    elif isinstance(result, Exception) or not result.get("ok"):
        text = f"✕ Failed: {result if isinstance(result, Exception) else result.get('error')}"
        result = {"rows": []}
    else:
        text = advanced.format_top_processes_text(result["rows"], by, adv["label"])

    rows = []
    for r in result.get("rows", [])[:10]:
        rows.append([
            InlineKeyboardButton(f"✕ Kill {r['pid']}", callback_data=f"{ADV_PROC_KILL_CB_PREFIX}{r['pid']}|{by}"),
            InlineKeyboardButton("☠ -9", callback_data=f"{ADV_PROC_KILL9_CB_PREFIX}{r['pid']}|{by}"),
        ])
    rows.append([InlineKeyboardButton("↻ Refresh", callback_data=f"{ADV_PROC_CB_PREFIX}{by}")])
    rows.append([InlineKeyboardButton("← Back", callback_data=ADV_MENU_CB)])
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(rows), parse_mode="Markdown")
    return SERVERMGR_ADV_BROWSE


async def _servermgr_adv_kill(update: Update, context: ContextTypes.DEFAULT_TYPE, force: bool):
    query = update.callback_query
    adv = context.user_data.get("servermgr_adv")
    if not adv:
        await query.answer("Session expired.", show_alert=True)
        return ConversationHandler.END
    prefix = ADV_PROC_KILL9_CB_PREFIX if force else ADV_PROC_KILL_CB_PREFIX
    pid_s, _, by = query.data.replace(prefix, "", 1).partition("|")
    by = by or "cpu"
    await query.answer("⏳ Sending signal...")

    result, timed_out = await _run_with_timeout(
        engine.remote_kill_process, adv["client"], int(pid_s), force, timeout=ADV_CMD_TIMEOUT,
    )
    if timed_out:
        await query.answer("⏱ Timed out.", show_alert=True)
    elif isinstance(result, Exception):
        await query.answer(f"✕ {result}", show_alert=True)
    elif not result.get("ok"):
        await query.answer(f"✕ {result.get('error')}", show_alert=True)
    else:
        await query.answer(f"✓ Signal sent to PID {pid_s}.")

    # Refresh the list in place.
    query.data = f"{ADV_PROC_CB_PREFIX}{by}"
    return await servermgr_adv_processes(update, context)


async def servermgr_adv_kill(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await _servermgr_adv_kill(update, context, force=False)


async def servermgr_adv_kill9(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await _servermgr_adv_kill(update, context, force=True)


# ---------------------- Services (systemd) --------------------------------

def _adv_svc_keyboard(units: list, filter_mode: str) -> InlineKeyboardMarkup:
    indexed = list(enumerate(units))
    custom = [(i, u) for i, u in indexed if u.get("origin") == "custom"]
    system = [(i, u) for i, u in indexed if u.get("origin") != "custom"]

    def _unit_button(i, u):
        mark = "●" if u["active"] == "active" else "○"
        return InlineKeyboardButton(f"{mark} {u['unit']}", callback_data=f"{ADV_SVC_DETAIL_CB_PREFIX}{i}")

    rows = []
    if filter_mode == "custom":
        groups = [("◦ Custom", custom)]
    elif filter_mode == "system":
        groups = [("▣ System", system)]
    else:
        groups = [("◦ Custom", custom), ("▣ System", system)]

    any_shown = False
    for label, group in groups:
        if not group:
            continue
        any_shown = True
        if filter_mode == "all":
            rows.append([InlineKeyboardButton(f"┈┈ {label} ┈┈", callback_data="servermgr_noop")])
        for i, u in group[:25]:
            rows.append([_unit_button(i, u)])
    if not any_shown:
        rows.append([InlineKeyboardButton("(none in this category)", callback_data="servermgr_noop")])

    rows.append([
        InlineKeyboardButton(
            ("• " if filter_mode == mode else "") + f"{label} ({len(units) if mode == 'all' else (len(custom) if mode == 'custom' else len(system))})",
            callback_data=f"{ADV_SVC_FILTERMODE_CB_PREFIX}{mode}",
        )
        for mode, label in advanced.SVC_FILTERS
    ])
    rows.append([InlineKeyboardButton("↻ Refresh", callback_data=ADV_SVC_CB)])
    rows.append([InlineKeyboardButton("← Back", callback_data=ADV_MENU_CB)])
    return InlineKeyboardMarkup(rows)


async def _adv_render_services(query, adv: dict):
    result, timed_out = await _run_with_timeout(
        engine.remote_systemd_list_units, adv["client"], 60, timeout=ADV_CMD_TIMEOUT,
    )
    if timed_out:
        text, units = "⏱ Timed out.", []
    elif isinstance(result, Exception) or not result.get("ok"):
        text = f"✕ Failed: {result if isinstance(result, Exception) else result.get('error')}"
        units = []
    else:
        units = result["units"]
    adv["units"] = units
    filter_mode = adv.get("svc_filter_mode", "all")
    if units:
        text = advanced.format_units_text(units, adv["label"], filter_mode)

    keyboard = _adv_svc_keyboard(units, filter_mode) if units else InlineKeyboardMarkup(
        [[InlineKeyboardButton("↻ Refresh", callback_data=ADV_SVC_CB)],
         [InlineKeyboardButton("← Back", callback_data=ADV_MENU_CB)]]
    )
    await query.edit_message_text(text, reply_markup=keyboard, parse_mode="Markdown")


async def servermgr_adv_services(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    adv = context.user_data.get("servermgr_adv")
    if not adv:
        await query.answer("Session expired.", show_alert=True)
        return ConversationHandler.END
    await query.answer("⏳ Loading...")
    _adv_svc_log_stop_active(adv)
    await _adv_render_services(query, adv)
    return SERVERMGR_ADV_BROWSE


async def servermgr_adv_svc_filter_mode(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    adv = context.user_data.get("servermgr_adv")
    if not adv:
        await query.answer("Session expired.", show_alert=True)
        return ConversationHandler.END
    mode = query.data.replace(ADV_SVC_FILTERMODE_CB_PREFIX, "", 1)
    if mode not in ("all", "custom", "system"):
        mode = "all"
    adv["svc_filter_mode"] = mode
    units = adv.get("units", [])
    await query.answer()
    text = advanced.format_units_text(units, adv["label"], mode)
    await query.edit_message_text(text, reply_markup=_adv_svc_keyboard(units, mode), parse_mode="Markdown")
    return SERVERMGR_ADV_BROWSE


def _adv_svc_log_stop_active(adv: dict):
    """Stops any in-flight live-log channel for this session (navigating away, refreshing, etc.)."""
    channel = adv.get("svc_log_channel")
    if channel is not None:
        try:
            engine.remote_tail_stop(channel)
        except Exception:
            pass
    adv["svc_log_channel"] = None
    adv["svc_log_idx"] = None


def _format_adv_svc_log(unit: str, output: str, status_line: str) -> str:
    body = _strip_ansi(output)[-ADV_SVC_LOG_BODY_CHARS:].rstrip("\n")
    header = f"▤ *Live log* `{unit}`"
    if not body:
        return f"{header}\n{status_line}"
    return f"{header}\n```\n{body}\n\n```\n{status_line}"


def _adv_svc_log_keyboard(alive: bool = True) -> InlineKeyboardMarkup:
    label = "⊘ Stop" if alive else "← Back"
    return InlineKeyboardMarkup([[InlineKeyboardButton(label, callback_data=ADV_SVC_LOGSTOP_CB)]])


async def servermgr_adv_svc_log_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    adv = context.user_data.get("servermgr_adv")
    if not adv:
        await query.answer("Session expired.", show_alert=True)
        return ConversationHandler.END
    idx = int(query.data.replace(ADV_SVC_LOG_CB_PREFIX, "", 1))
    units = adv.get("units", [])
    if idx >= len(units):
        await query.answer("That list is stale - refresh Services first.", show_alert=True)
        return SERVERMGR_ADV_BROWSE
    unit = units[idx]["unit"]

    _adv_svc_log_stop_active(adv)
    await query.answer("⏳ Starting...")
    try:
        channel = await asyncio.to_thread(engine.remote_journal_tail_start, adv["client"], unit)
    except Exception as e:
        await query.answer(f"✕ Could not start log: {str(e)[:180]}", show_alert=True)
        return SERVERMGR_ADV_BROWSE

    adv["svc_log_channel"] = channel
    adv["svc_log_idx"] = idx
    chat_id = adv["chat_id"]
    msg = await context.bot.send_message(
        chat_id=chat_id,
        text=_format_adv_svc_log(unit, "", "◉ Following — new log lines appear here as they're written."),
        parse_mode="Markdown", reply_markup=_adv_svc_log_keyboard(),
    )
    asyncio.create_task(_stream_adv_svc_log(context, chat_id, unit, msg.message_id))
    return SERVERMGR_ADV_BROWSE


async def _stream_adv_svc_log(context: ContextTypes.DEFAULT_TYPE, chat_id: int, unit: str, message_id: int):
    output_so_far = ""
    last_sent_text = None
    while True:
        await asyncio.sleep(ADV_SVC_LOG_EDIT_INTERVAL)
        adv = context.user_data.get("servermgr_adv")
        if not adv:
            return
        channel = adv.get("svc_log_channel")
        if channel is None:
            return

        chunk = await asyncio.to_thread(engine.remote_tail_read_available, channel)
        if adv.get("svc_log_channel") is not channel:
            return
        if chunk:
            output_so_far += chunk
        alive = engine.remote_tail_is_alive(channel)
        status = "◉ Following…" if alive else "▪ journalctl ended (unit may have been removed)."
        new_text = _format_adv_svc_log(unit, output_so_far, status)
        if new_text != last_sent_text:
            try:
                await context.bot.edit_message_text(
                    new_text, chat_id=chat_id, message_id=message_id,
                    parse_mode="Markdown", reply_markup=_adv_svc_log_keyboard(alive),
                )
                last_sent_text = new_text
            except Exception:
                pass
        if not alive:
            adv["svc_log_channel"] = None
            return


async def servermgr_adv_svc_log_stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    adv = context.user_data.get("servermgr_adv")
    if not adv:
        await query.answer("Session expired.", show_alert=True)
        return ConversationHandler.END
    _adv_svc_log_stop_active(adv)
    await query.answer("⊘ Stopped.")
    idx = adv.get("svc_log_idx")
    if idx is not None:
        query.data = f"{ADV_SVC_DETAIL_CB_PREFIX}{idx}"
        return await servermgr_adv_svc_detail(update, context)
    try:
        await query.edit_message_text(_format_adv_svc_log("?", "", "⊘ Stopped."), parse_mode="Markdown")
    except Exception:
        pass
    return SERVERMGR_ADV_BROWSE


async def servermgr_adv_svc_detail(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    adv = context.user_data.get("servermgr_adv")
    if not adv:
        await query.answer("Session expired.", show_alert=True)
        return ConversationHandler.END
    idx = int(query.data.replace(ADV_SVC_DETAIL_CB_PREFIX, "", 1))
    units = adv.get("units", [])
    if idx >= len(units):
        await query.answer("That list is stale - refresh Services first.", show_alert=True)
        return SERVERMGR_ADV_BROWSE
    unit = units[idx]["unit"]
    await query.answer("⏳ Loading...")
    _adv_svc_log_stop_active(adv)

    result, timed_out = await _run_with_timeout(
        engine.remote_systemd_unit_status, adv["client"], unit, timeout=ADV_CMD_TIMEOUT,
    )
    if timed_out:
        text = f"⏱ Timed out getting status for `{unit}`."
    elif isinstance(result, Exception) or not result.get("ok"):
        text = f"✕ Failed: {result if isinstance(result, Exception) else result.get('error')}"
    else:
        text = advanced.format_unit_status_text(unit, result["text"])

    rows = [
        [
            InlineKeyboardButton("▶ Start", callback_data=f"{ADV_SVC_ACTION_CB_PREFIX}{idx}|start"),
            InlineKeyboardButton("■ Stop", callback_data=f"{ADV_SVC_ACTION_CB_PREFIX}{idx}|stop"),
            InlineKeyboardButton("↻ Restart", callback_data=f"{ADV_SVC_ACTION_CB_PREFIX}{idx}|restart"),
        ],
        [
            InlineKeyboardButton("✓ Enable", callback_data=f"{ADV_SVC_ACTION_CB_PREFIX}{idx}|enable"),
            InlineKeyboardButton("⊘ Disable", callback_data=f"{ADV_SVC_ACTION_CB_PREFIX}{idx}|disable"),
            InlineKeyboardButton("▤ Live Log", callback_data=f"{ADV_SVC_LOG_CB_PREFIX}{idx}"),
        ],
        [InlineKeyboardButton("⌫ Remove unit", callback_data=f"{ADV_SVC_RM_CONFIRM_CB_PREFIX}{idx}")],
        [
            InlineKeyboardButton("↻ Refresh", callback_data=f"{ADV_SVC_DETAIL_CB_PREFIX}{idx}"),
            InlineKeyboardButton("← Back", callback_data=ADV_SVC_CB),
        ],
    ]
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(rows), parse_mode="Markdown")
    return SERVERMGR_ADV_BROWSE


async def servermgr_adv_svc_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    adv = context.user_data.get("servermgr_adv")
    if not adv:
        await query.answer("Session expired.", show_alert=True)
        return ConversationHandler.END
    idx_s, _, action = query.data.replace(ADV_SVC_ACTION_CB_PREFIX, "", 1).partition("|")
    idx = int(idx_s)
    units = adv.get("units", [])
    if idx >= len(units):
        await query.answer("That list is stale - refresh Services first.", show_alert=True)
        return SERVERMGR_ADV_BROWSE
    unit = units[idx]["unit"]
    await query.answer(f"⏳ {action}...")

    result, timed_out = await _run_with_timeout(
        engine.remote_systemd_unit_action, adv["client"], unit, action, timeout=ADV_CMD_TIMEOUT,
    )
    if timed_out:
        await query.answer(f"⏱ {action} timed out.", show_alert=True)
    elif isinstance(result, Exception):
        await query.answer(f"✕ {result}", show_alert=True)
    elif not result.get("ok"):
        await query.answer(f"✕ {result.get('error') or 'failed'}", show_alert=True)
    else:
        await query.answer(f"✓ {action} succeeded.")

    query.data = f"{ADV_SVC_DETAIL_CB_PREFIX}{idx}"
    return await servermgr_adv_svc_detail(update, context)


async def servermgr_adv_svc_remove_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    adv = context.user_data.get("servermgr_adv")
    if not adv:
        await query.answer("Session expired.", show_alert=True)
        return ConversationHandler.END
    idx = int(query.data.replace(ADV_SVC_RM_CONFIRM_CB_PREFIX, "", 1))
    units = adv.get("units", [])
    if idx >= len(units):
        await query.answer("That list is stale - refresh Services first.", show_alert=True)
        return SERVERMGR_ADV_BROWSE
    unit = units[idx]["unit"]
    await query.answer()
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("✓ Yes, remove", callback_data=f"{ADV_SVC_RM_OK_CB_PREFIX}{idx}"),
         InlineKeyboardButton("✕ Cancel", callback_data=f"{ADV_SVC_DETAIL_CB_PREFIX}{idx}")],
    ])
    await query.edit_message_text(
        f"⚠ Remove `{unit}`? Only unit files under /etc/systemd/system can be removed (custom units) - "
        f"this stops it, disables it, deletes its unit file, and reloads systemd. This cannot be undone.",
        reply_markup=keyboard, parse_mode="Markdown",
    )
    return SERVERMGR_ADV_BROWSE


async def servermgr_adv_svc_remove_execute(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    adv = context.user_data.get("servermgr_adv")
    if not adv:
        await query.answer("Session expired.", show_alert=True)
        return ConversationHandler.END
    idx = int(query.data.replace(ADV_SVC_RM_OK_CB_PREFIX, "", 1))
    units = adv.get("units", [])
    if idx >= len(units):
        await query.answer("That list is stale - refresh Services first.", show_alert=True)
        return SERVERMGR_ADV_BROWSE
    unit = units[idx]["unit"]
    await query.answer("⏳ Removing...")

    result, timed_out = await _run_with_timeout(
        engine.remote_systemd_unit_remove, adv["client"], unit, timeout=ADV_CMD_TIMEOUT,
    )
    if timed_out:
        text = f"⏱ Timed out removing `{unit}`."
    elif isinstance(result, Exception):
        text = f"✕ Failed: {result}"
    elif not result.get("ok"):
        text = f"✕ Failed: {result.get('error')}"
    else:
        text = f"✓ `{unit}` removed."
        logger.info(f"User {query.from_user.id} removed systemd unit {unit} on server {adv.get('server_id')}")

    await query.edit_message_text(
        text, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("← Back to Services", callback_data=ADV_SVC_CB)]]),
        parse_mode="Markdown",
    )
    return SERVERMGR_ADV_BROWSE


# ---------------------- Crontab -------------------------------------------

async def servermgr_adv_cron_view(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    adv = context.user_data.get("servermgr_adv")
    if not adv:
        await query.answer("Session expired.", show_alert=True)
        return ConversationHandler.END
    await query.answer("⏳ Reading crontab...")

    result, timed_out = await _run_with_timeout(
        engine.remote_get_crontab, adv["client"], timeout=ADV_CMD_TIMEOUT,
    )
    if timed_out:
        text = "⏱ Timed out."
    elif isinstance(result, Exception) or not result.get("ok"):
        text = f"✕ Failed: {result if isinstance(result, Exception) else result.get('error')}"
    else:
        text = advanced.format_crontab_text(result["content"], adv["label"])

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("✎ Edit", callback_data=ADV_CRON_EDIT_CB)],
        [InlineKeyboardButton("← Back", callback_data=ADV_MENU_CB)],
    ])
    await query.edit_message_text(text, reply_markup=keyboard, parse_mode="Markdown")
    return SERVERMGR_ADV_BROWSE


async def servermgr_adv_cron_edit_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    adv = context.user_data.get("servermgr_adv")
    if not adv:
        await query.answer("Session expired.", show_alert=True)
        return ConversationHandler.END
    await query.answer("⏳ Reading current crontab...")

    result, timed_out = await _run_with_timeout(
        engine.remote_get_crontab, adv["client"], timeout=ADV_CMD_TIMEOUT,
    )
    current = "" if timed_out or isinstance(result, Exception) or not result.get("ok") else result.get("content", "")

    adv["awaiting_cron_edit"] = True
    body = current if current.strip() else "(empty)"
    await query.edit_message_text(f"⏰ Current crontab:\n```\n{body}\n```", parse_mode="Markdown")
    await context.bot.send_message(
        chat_id=adv["chat_id"],
        text=(
            f"✎ Send the *complete* new crontab content (replaces everything - copy from above, edit, "
            f"and send it back). Max {engine.CRONTAB_EDITOR_MAX_BYTES} bytes."
        ),
        parse_mode="Markdown", reply_markup=_cancel_keyboard(),
    )
    return SERVERMGR_ADV_BROWSE


async def _adv_handle_cron_edit_text(update: Update, context: ContextTypes.DEFAULT_TYPE, adv: dict):
    new_content = update.message.text or ""
    if len(new_content.encode("utf-8")) > engine.CRONTAB_EDITOR_MAX_BYTES:
        await update.message.reply_text(
            f"✕ That's over {engine.CRONTAB_EDITOR_MAX_BYTES} bytes - too big to save here. Trim it and resend, "
            f"or tap Cancel.", reply_markup=_cancel_keyboard(),
        )
        return SERVERMGR_ADV_BROWSE

    adv["awaiting_cron_edit"] = False
    logger.info(f"User {update.effective_user.id} replaced crontab on server {adv.get('server_id')}")
    result, timed_out = await _run_with_timeout(
        engine.remote_set_crontab, adv["client"], new_content, timeout=ADV_CMD_TIMEOUT,
    )
    if timed_out:
        text = "⏱ Timed out saving crontab."
    elif isinstance(result, Exception):
        text = f"✕ Failed: {result}"
    elif not result.get("ok"):
        text = f"✕ Failed: {result.get('error')}"
    else:
        text = "✓ Crontab saved."

    await update.message.reply_text(text, reply_markup=ReplyKeyboardRemove())
    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("← Back", callback_data=ADV_MENU_CB)]])
    await context.bot.send_message(chat_id=adv["chat_id"], text="⏰ Crontab", reply_markup=keyboard)
    return SERVERMGR_ADV_BROWSE


# ---------------------- Shared text-input dispatcher -----------------------

async def servermgr_adv_text_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    adv = context.user_data.get("servermgr_adv")
    if not adv:
        await update.message.reply_text("Session expired.", reply_markup=ReplyKeyboardRemove())
        return ConversationHandler.END

    if adv.get("awaiting_cron_edit"):
        return await _adv_handle_cron_edit_text(update, context, adv)

    await update.message.reply_text("Use the buttons above, or Cancel to close Advanced Tools.")
    return SERVERMGR_ADV_BROWSE


async def servermgr_ssh_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id
    server_id = query.data.replace("servermgr_ssh_start_", "", 1)
    server = settings.get_server(user_id, server_id)
    sessions = context.user_data.setdefault("servermgr_sessions", {})

    if not subscription.is_active(user_id):
        await query.answer("⚿ Your subscription has expired. Please renew it first.", show_alert=True)
        return _cmd_state_or_end(context)

    if not server:
        await query.answer("Server not found.", show_alert=True)
        return _cmd_state_or_end(context)

    tab_limit = _effective_tab_limit(user_id)
    if len(sessions) >= tab_limit:
        await query.answer(
            f"⚠ Your plan allows {tab_limit} concurrent terminal tab(s). Close one first (open its server page "
            f"and tap \"Close tab\", or tap \"{CMD_DONE_TEXT}\" to close the active one).",
            show_alert=True,
        )
        return _cmd_state_or_end(context)

    await query.answer("⏳ Connecting...")
    client, timed_out = await _run_with_timeout(engine.connect, server, timeout=CONNECT_TIMEOUT)
    if timed_out:
        await query.edit_message_text(f"⏱ Connecting to \"{server['label']}\" took more than {CONNECT_TIMEOUT}s and was cancelled.")
        return _cmd_state_or_end(context)
    if isinstance(client, engine.HostKeyChangedError):
        await query.edit_message_text(
            f"⚠ SECURITY WARNING for \"{server['label']}\"\n\n{client}",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("⚠ Trust new key & retry", callback_data=f"servermgr_trustkey_{server_id}")],
                [InlineKeyboardButton("← Back", callback_data=f"servermgr_srv_{server_id}")],
            ]),
        )
        return _cmd_state_or_end(context)
    if client is None or isinstance(client, Exception):
        err = f"\n{client}" if isinstance(client, Exception) else ""
        await query.edit_message_text(f"✕ Failed to connect to \"{server['label']}\".{err}")
        return _cmd_state_or_end(context)

    channel, shell_timed_out = await _run_with_timeout(engine.open_shell, client, timeout=CONNECT_TIMEOUT)
    if shell_timed_out or channel is None or isinstance(channel, Exception):
        try:
            client.close()
        except Exception:
            pass
        err = f"\n{channel}" if isinstance(channel, Exception) else ""
        await query.edit_message_text(f"✕ Failed to open a shell on \"{server['label']}\".{err}")
        return _cmd_state_or_end(context)

    capabilities = subscription.get_capabilities(user_id)

    session_id = uuid.uuid4().hex[:6]
    sessions[session_id] = {
        "client": client,
        "channel": channel,
        "server_id": server_id,
        "label": server["label"],
        "chat_id": query.message.chat_id,
        "user_id": user_id,
        "cmd_handle_box": {"handle": None},
        "cancel_requested": False,
        "busy": False,
        "term_state": None,
        "tab_state_msg": None,
        "timeout_job": None,
        "sftp_enabled": bool(capabilities.get("sftp_enabled")),
        "quick_open": False,
        "quick_manage": False,
    }
    context.user_data["servermgr_active_session"] = session_id

    timeout_minutes = capabilities.get("session_timeout_minutes")
    sessions[session_id]["timeout_job"] = _schedule_session_timeout(
        context, query.message.chat_id, user_id, session_id, timeout_minutes
    )
    timeout_note = f" Auto-closes in {timeout_minutes} min (your plan's limit)." if timeout_minutes else ""

    await query.edit_message_text(
        f"▣ Connected to \"{server['label']}\" ({server['host']}) — tab {len(sessions)}/{tab_limit}.{timeout_note}",
    )
    await context.bot.send_message(
        chat_id=query.message.chat_id,
        text=f"Active tab: \"{server['label']}\". Send a command:",
        reply_markup=_ssh_session_keyboard(),
    )
    await _send_tab_state(context.bot, query.message.chat_id, sessions, session_id)
    return SERVERMGR_CMD_INPUT


async def servermgr_switch_tab(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    session_id = query.data.replace("servermgr_switch_", "", 1)
    sessions = context.user_data.get("servermgr_sessions", {})
    session = sessions.get(session_id)
    if session is None:
        await query.answer("That tab is no longer open.", show_alert=True)
        return _cmd_state_or_end(context)

    context.user_data["servermgr_active_session"] = session_id
    await query.answer(f"Switched to \"{session['label']}\".")

    server = settings.get_server(query.from_user.id, session["server_id"])
    if server:
        text, reply_markup = _server_detail_text_and_keyboard(server, sessions, session_id, user_id=query.from_user.id)
        try:
            await query.edit_message_text(text, reply_markup=reply_markup, parse_mode="Markdown")
        except Exception:
            pass

    await context.bot.send_message(
        chat_id=query.message.chat_id,
        text=f"Active tab: \"{session['label']}\". Send a command:",
        reply_markup=_ssh_session_keyboard(),
    )
    await _send_tab_state(context.bot, query.message.chat_id, sessions, session_id)
    return SERVERMGR_CMD_INPUT


async def servermgr_closetab(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    session_id = query.data.replace("servermgr_closetab_", "", 1)
    if session_id not in context.user_data.get("servermgr_sessions", {}):
        await query.answer("That tab is already closed.", show_alert=True)
        return _cmd_state_or_end(context)

    result = _do_close_tab(context, session_id)
    await query.answer(f"■ Closed \"{result['label']}\".")

    server = settings.get_server(query.from_user.id, result["server_id"])
    if server:
        text, reply_markup = _server_detail_text_and_keyboard(server, result["sessions"], result["active_id"], user_id=query.from_user.id)
        try:
            await query.edit_message_text(text, reply_markup=reply_markup, parse_mode="Markdown")
        except Exception:
            pass

    if result["sessions"]:
        active_label = result["sessions"][result["active_id"]]["label"]
        await context.bot.send_message(
            chat_id=query.message.chat_id,
            text=f"Active tab: \"{active_label}\". Send a command:",
            reply_markup=_ssh_session_keyboard(),
        )
        await _send_tab_state(context.bot, query.message.chat_id, result["sessions"], result["active_id"])
        return SERVERMGR_CMD_INPUT

    await context.bot.send_message(chat_id=query.message.chat_id, text="■ All SSH tabs closed.", reply_markup=ReplyKeyboardRemove())
    text, reply_markup = _servers_text_and_keyboard(query.from_user.id, {})
    await context.bot.send_message(chat_id=query.message.chat_id, text=text, reply_markup=reply_markup, parse_mode="Markdown")
    return ConversationHandler.END


async def servermgr_tabsbar_switch(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    session_id = query.data.replace(TAB_SWITCH_CB_PREFIX, "", 1)
    sessions = context.user_data.get("servermgr_sessions", {})
    session = sessions.get(session_id)
    if session is None:
        await query.answer("That tab is no longer open.", show_alert=True)
        return _cmd_state_or_end(context)

    context.user_data["servermgr_active_session"] = session_id
    await query.answer(f"Switched to \"{session['label']}\".")
    try:
        cancel_sid = _cancel_session_id_from_markup(query.message.reply_markup)
        await query.edit_message_reply_markup(reply_markup=_terminal_keyboard(sessions, session_id, cancel_sid))
    except Exception:
        pass
    await context.bot.send_message(
        chat_id=query.message.chat_id,
        text=f"Active tab: \"{session['label']}\". Send a command:",
        reply_markup=_ssh_session_keyboard(),
    )
    await _send_tab_state(context.bot, query.message.chat_id, sessions, session_id)
    return SERVERMGR_CMD_INPUT


async def servermgr_tabsbar_close(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    session_id = query.data.replace(TAB_CLOSE_CB_PREFIX, "", 1)
    if session_id not in context.user_data.get("servermgr_sessions", {}):
        await query.answer("That tab is already closed.", show_alert=True)
        return _cmd_state_or_end(context)

    result = _do_close_tab(context, session_id)
    await query.answer(f"■ Closed \"{result['label']}\".")

    if result["sessions"]:
        try:
            cancel_sid = _cancel_session_id_from_markup(query.message.reply_markup)
            if cancel_sid == session_id:
                cancel_sid = None
            await query.edit_message_reply_markup(
                reply_markup=_terminal_keyboard(result["sessions"], result["active_id"], cancel_sid)
            )
        except Exception:
            pass
        await context.bot.send_message(
            chat_id=query.message.chat_id,
            text=f"Active tab: \"{result['sessions'][result['active_id']]['label']}\". Send a command:",
            reply_markup=_ssh_session_keyboard(),
        )
        await _send_tab_state(context.bot, query.message.chat_id, result["sessions"], result["active_id"])
        return SERVERMGR_CMD_INPUT

    try:
        await query.edit_message_text("■ All tabs closed.")
    except Exception:
        pass
    await context.bot.send_message(chat_id=query.message.chat_id, text="Server Manager:", reply_markup=ReplyKeyboardRemove())
    text, reply_markup = _servers_text_and_keyboard(query.from_user.id, {})
    await context.bot.send_message(chat_id=query.message.chat_id, text=text, reply_markup=reply_markup, parse_mode="Markdown")
    return ConversationHandler.END


async def servermgr_cmd_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    fb = context.user_data.get("servermgr_filebrowser")
    if fb and (fb.get("awaiting_upload") or fb.get("awaiting_path") or fb.get("awaiting_url")
               or fb.get("awaiting_rename_idx") is not None or fb.get("awaiting_edit_idx") is not None
               or fb.get("awaiting_mkdir")):
        await servermgr_files_text_input(update, context)
        return SERVERMGR_CMD_INPUT

    text = (update.message.text or "").strip()
    sessions = context.user_data.get("servermgr_sessions", {})
    active_id = context.user_data.get("servermgr_active_session")
    session = sessions.get(active_id) if active_id else None

    if not sessions or session is None:
        _close_ssh_session(context)
        await update.message.reply_text("⚠ No active SSH tab. Please open one again.", reply_markup=ReplyKeyboardRemove())
        await _reply_with_servermgr_menu(update, "Server Manager menu:", context)
        return ConversationHandler.END

    if not text:
        await update.message.reply_text("Send a command:")
        return SERVERMGR_CMD_INPUT

    if session.get("busy"):
        handle_box = session.get("cmd_handle_box")
        handle = handle_box.get("handle") if handle_box else None
        if handle is None:
            await update.message.reply_text(
                f"⏳ \"{session['label']}\" is still starting up - try again in a second."
            )
            return SERVERMGR_CMD_INPUT
        if not handle.send_raw(text + "\n"):
            await update.message.reply_text(
                f"⚠ Could not send that to \"{session['label']}\" - the session may have dropped."
            )
        return SERVERMGR_CMD_INPUT

    server = settings.get_server(_uid(update), session["server_id"])
    if not server:
        _close_one_session(context, active_id)
        await update.message.reply_text(f"⚠ \"{session['label']}\" no longer exists. Tab closed.", reply_markup=ReplyKeyboardRemove())
        return _cmd_state_or_end(context)

    client, channel = session["client"], session["channel"]

    if not engine.is_alive(client) or channel is None or channel.closed:
        new_client, timed_out = await _run_with_timeout(engine.connect, server, timeout=CONNECT_TIMEOUT)
        if isinstance(new_client, engine.HostKeyChangedError):
            _close_one_session(context, active_id)
            await update.message.reply_text(
                f"⚠ SECURITY WARNING for \"{server['label']}\"\n\n{new_client}",
                reply_markup=ReplyKeyboardRemove(),
            )
            await update.message.reply_text(
                "Tab closed. Go to this server's page if you want to trust the new key and retry.",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("⚠ Trust new key & retry", callback_data=f"servermgr_trustkey_{session['server_id']}"),
                ]]),
            )
            return _cmd_state_or_end(context)
        if timed_out or new_client is None or isinstance(new_client, Exception):
            _close_one_session(context, active_id)
            await update.message.reply_text(
                f"✕ The connection to \"{server['label']}\" dropped and reconnecting also failed. Tab closed.",
                reply_markup=ReplyKeyboardRemove(),
            )
            return _cmd_state_or_end(context)
        new_channel, shell_timed_out = await _run_with_timeout(engine.open_shell, new_client, timeout=CONNECT_TIMEOUT)
        if shell_timed_out or new_channel is None or isinstance(new_channel, Exception):
            try:
                new_client.close()
            except Exception:
                pass
            _close_one_session(context, active_id)
            await update.message.reply_text(
                f"✕ The connection to \"{server['label']}\" dropped and reopening a shell also failed. Tab closed.",
                reply_markup=ReplyKeyboardRemove(),
            )
            return _cmd_state_or_end(context)
        client, channel = new_client, new_channel
        session["client"], session["channel"] = client, channel

    session["busy"] = True
    session["cancel_requested"] = False
    asyncio.create_task(_stream_command(context, active_id, text, session["chat_id"]))
    return SERVERMGR_CMD_INPUT


async def _stream_command(context: ContextTypes.DEFAULT_TYPE, session_id: str, command_text: str, chat_id: int):
    sessions = context.user_data.get("servermgr_sessions", {})
    session = sessions.get(session_id)
    if session is None:
        return

    channel = session["channel"]
    label = session["label"]
    handle_box = session["cmd_handle_box"]
    handle_box["handle"] = None
    chunk_queue = queue_mod.Queue()

    await _clear_tab_state_msg(context.bot, session)

    def on_chunk(handle, chunk_text):
        handle_box["handle"] = handle
        if chunk_text:
            chunk_queue.put(chunk_text)

    spin_frame = 0
    running_status = f"{_SPINNER_FRAMES[0]} Running…"

    try:
        active_id = context.user_data.get("servermgr_active_session")
        initial_markup = _terminal_keyboard(sessions, active_id, session_id)
        msg = await context.bot.send_message(
            chat_id=chat_id,
            text=_format_terminal(label, command_text, "", running_status),
            parse_mode="Markdown",
            reply_markup=initial_markup,
        )
    except Exception as e:
        logger.warning(f"servermgr: failed to send terminal message for tab {session_id}: {e}")
        session["busy"] = False
        return

    term_state = {"msg": msg, "label": label, "command": command_text, "output": "", "status": running_status}
    session["term_state"] = term_state

    task = asyncio.create_task(
        asyncio.to_thread(engine.run_shell_input, channel, command_text, COMMAND_TIMEOUT, on_chunk)
    )

    output_so_far = ""
    last_edit_at = 0.0
    last_sent_text = None
    last_sent_markup = initial_markup
    while not task.done():
        await asyncio.sleep(0.3)

        if context.user_data.get("servermgr_sessions", {}).get(session_id) is None:
            handle = handle_box.get("handle")
            if handle is not None:
                try:
                    handle.cancel()
                except Exception:
                    pass

        got_new = False
        while True:
            try:
                output_so_far += chunk_queue.get_nowait()
                got_new = True
            except queue_mod.Empty:
                break

        now = time.monotonic()
        # Only edit when there's actual new output to show - a pure spinner
        # tick with nothing new isn't worth an edit, and (more importantly)
        # would otherwise keep stomping on a Quick menu the user just opened
        # on this same message before any real output arrived.
        if got_new and now - last_edit_at >= TERMINAL_EDIT_INTERVAL:
            spin_frame += 1
            frame = _SPINNER_FRAMES[spin_frame % len(_SPINNER_FRAMES)]
            status = f"{frame} Cancelling…" if session.get("cancel_requested") else f"{frame} Running…"
            display_output = output_so_far.replace(engine._SHELL_PROMPT_MARKER, "")
            new_text = _format_terminal(label, command_text, display_output, status)
            if new_text != last_sent_text:
                try:
                    cur_sessions = context.user_data.get("servermgr_sessions", {})
                    cur_active = context.user_data.get("servermgr_active_session")
                    new_markup = _terminal_keyboard(cur_sessions, cur_active, session_id)
                    # editMessageText clears the existing keyboard if reply_markup
                    # is omitted (unlike editMessageReplyMarkup) - it does NOT
                    # keep the previous one, so it must be sent on every edit.
                    await msg.edit_text(
                        new_text, parse_mode="Markdown",
                        reply_markup=new_markup,
                    )
                    last_sent_markup = new_markup
                    last_sent_text = new_text
                    term_state["output"] = display_output
                    term_state["status"] = status
                except BadRequest as e:
                    if "not modified" not in str(e).lower():
                        logger.debug(f"terminal live edit failed: {e}")
                except Exception as e:
                    logger.debug(f"terminal live edit failed: {e}")
            last_edit_at = now

    while True:
        try:
            output_so_far += chunk_queue.get_nowait()
        except queue_mod.Empty:
            break

    try:
        result = task.result()
    except Exception as e:
        result = {"error": str(e)}

    session["busy"] = False
    session["cancel_requested"] = False
    handle_box["handle"] = None

    if result.get("error"):
        status = f"✕ Execution error: {result['error']}"
    elif result.get("cancelled"):
        status = "⊘ Cancelled. Session is still open — send the next command."
    elif result.get("timed_out"):
        status = f"⏱ No prompt back after {COMMAND_TIMEOUT // 3600}h — still running in the background."
    else:
        status = "✓ Ready — send the next command."

    final_output = result.get("output")
    if final_output is None:
        final_output = output_so_far.replace(engine._SHELL_PROMPT_MARKER, "")

    final_text = _format_terminal(label, command_text, final_output, status)
    try:
        cur_sessions = context.user_data.get("servermgr_sessions", {})
        cur_active = context.user_data.get("servermgr_active_session")
        await msg.edit_text(
            final_text, parse_mode="Markdown",
            reply_markup=_terminal_keyboard(cur_sessions, cur_active, session_id),
        )
    except BadRequest as e:
        if "not modified" not in str(e).lower():
            logger.debug(f"terminal final edit failed: {e}")
    except Exception as e:
        logger.debug(f"terminal final edit failed: {e}")

    term_state["output"] = final_output
    term_state["status"] = status


async def _inject_idle_keystroke(context: ContextTypes.DEFAULT_TYPE, session: dict, session_id: str, data: str, status_text: str) -> bool:
    channel = session.get("channel")
    if channel is None or channel.closed:
        return False
    try:
        channel.send(data)
    except Exception:
        return False

    term_state = session.get("term_state")
    if term_state is None or term_state.get("msg") is None:
        return True

    extra_output = ""
    start = time.monotonic()
    while True:
        got_data = False
        try:
            if channel.recv_ready():
                chunk = channel.recv(4096).decode(errors="ignore")
                if chunk:
                    extra_output += chunk
                    got_data = True
            if channel.recv_stderr_ready():
                chunk = channel.recv_stderr(4096).decode(errors="ignore")
                if chunk:
                    extra_output += chunk
                    got_data = True
        except Exception:
            break
        if engine._SHELL_PROMPT_MARKER in extra_output or time.monotonic() - start > 5:
            break
        if not got_data:
            await asyncio.sleep(0.15)

    extra_output = extra_output.replace(engine._SHELL_PROMPT_MARKER, "")
    if not extra_output:
        return True

    term_state["output"] += extra_output
    term_state["status"] = status_text
    new_text = _format_terminal(
        term_state["label"], term_state["command"], term_state["output"],
        term_state["status"],
    )
    try:
        cur_sessions = context.user_data.get("servermgr_sessions", {})
        cur_active = context.user_data.get("servermgr_active_session")
        await term_state["msg"].edit_text(
            new_text, parse_mode="Markdown",
            reply_markup=_terminal_keyboard(cur_sessions, cur_active, session_id),
        )
    except BadRequest as e:
        if "not modified" not in str(e).lower():
            logger.debug(f"terminal idle-keystroke edit failed: {e}")
    except Exception as e:
        logger.debug(f"terminal idle-keystroke edit failed: {e}")
    return True


async def servermgr_cmd_cancel_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    session_id = query.data.replace(f"{CMD_CANCEL_CALLBACK}_", "", 1)
    sessions = context.user_data.get("servermgr_sessions", {})
    session = sessions.get(session_id)
    if session is None:
        await query.answer("That tab is no longer open.")
        return

    handle_box = session.get("cmd_handle_box")
    handle = handle_box.get("handle") if handle_box else None
    if handle is not None:
        session["cancel_requested"] = True
        handle.cancel()
        await query.answer("⊘ Cancelling…")
        return

    await query.answer("⊘ Ctrl-C sent.")
    ok = await _inject_idle_keystroke(
        context, session, session_id, engine.CTRL_C, "⊘ Ctrl-C sent — send the next command."
    )
    if not ok:
        await query.answer("⚠ Could not cancel - the session may have dropped.")


async def servermgr_cmd_enter_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    session_id = query.data.replace(f"{CMD_ENTER_CALLBACK}_", "", 1)
    sessions = context.user_data.get("servermgr_sessions", {})
    session = sessions.get(session_id)
    if session is None:
        await query.answer("That tab is no longer open.")
        return

    handle_box = session.get("cmd_handle_box")
    handle = handle_box.get("handle") if handle_box else None
    if handle is not None:
        if handle.send_raw("\n"):
            await query.answer("⏎ Enter sent.")
        else:
            await query.answer("⚠ Could not send Enter - the session may have dropped.")
        return

    await query.answer("⏎ Enter sent.")
    ok = await _inject_idle_keystroke(
        context, session, session_id, "\n", "⏎ Enter sent — send the next command."
    )
    if not ok:
        await query.answer("⚠ Could not send Enter - the session may have dropped.")


async def _servermgr_cmd_quick_answer(update: Update, context: ContextTypes.DEFAULT_TYPE, callback_prefix: str, key: str, label: str):
    query = update.callback_query
    session_id = query.data.replace(f"{callback_prefix}_", "", 1)
    sessions = context.user_data.get("servermgr_sessions", {})
    session = sessions.get(session_id)
    if session is None:
        await query.answer("That tab is no longer open.")
        return

    handle_box = session.get("cmd_handle_box")
    handle = handle_box.get("handle") if handle_box else None
    if handle is not None:
        if handle.send_raw(f"{key}\n"):
            await query.answer(f"{label} sent.")
        else:
            await query.answer(f"⚠ Could not send {label} - the session may have dropped.")
        return

    await query.answer(f"{label} sent.")
    ok = await _inject_idle_keystroke(
        context, session, session_id, f"{key}\n", f"{label} sent — send the next command."
    )
    if not ok:
        await query.answer(f"⚠ Could not send {label} - the session may have dropped.")


async def servermgr_cmd_yes_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _servermgr_cmd_quick_answer(update, context, CMD_YES_CALLBACK, "y", "✓ y")


async def servermgr_cmd_no_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _servermgr_cmd_quick_answer(update, context, CMD_NO_CALLBACK, "n", "⊘ n")


# ---------------------- Quick commands (⚡) inside a live terminal tab -------
# Saved per (user, server) via automation.py's Quick Commands storage - the
# same list shown on the Automation screen is offered here so a command can
# be fired straight into the active SSH session instead of typing it.

async def _servermgr_refresh_quick_markup(query, context: ContextTypes.DEFAULT_TYPE, sessions: dict, session_id: str):
    try:
        cur_active = context.user_data.get("servermgr_active_session")
        await query.edit_message_reply_markup(reply_markup=_terminal_keyboard(sessions, cur_active, session_id))
    except BadRequest as e:
        if "not modified" not in str(e).lower():
            logger.debug(f"servermgr: quick menu keyboard refresh failed: {e}")
    except Exception as e:
        logger.debug(f"servermgr: quick menu keyboard refresh failed: {e}")


async def servermgr_quick_menu_open(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    session_id = query.data.replace(QUICK_MENU_CB_PREFIX, "", 1)
    sessions = context.user_data.get("servermgr_sessions", {})
    session = sessions.get(session_id)
    if session is None:
        await query.answer("That tab is no longer open.", show_alert=True)
        return _cmd_state_or_end(context)
    await query.answer()
    session["quick_open"] = True
    session["quick_manage"] = False
    await _servermgr_refresh_quick_markup(query, context, sessions, session_id)
    return SERVERMGR_CMD_INPUT


async def servermgr_quick_menu_close(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    session_id = query.data.replace(QUICK_CLOSE_CB_PREFIX, "", 1)
    sessions = context.user_data.get("servermgr_sessions", {})
    session = sessions.get(session_id)
    if session is None:
        await query.answer("That tab is no longer open.", show_alert=True)
        return _cmd_state_or_end(context)
    await query.answer()
    session["quick_open"] = False
    session["quick_manage"] = False
    await _servermgr_refresh_quick_markup(query, context, sessions, session_id)
    return SERVERMGR_CMD_INPUT


async def servermgr_quick_manage_open(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    session_id = query.data.replace(QUICK_MANAGE_CB_PREFIX, "", 1)
    sessions = context.user_data.get("servermgr_sessions", {})
    session = sessions.get(session_id)
    if session is None:
        await query.answer("That tab is no longer open.", show_alert=True)
        return _cmd_state_or_end(context)
    await query.answer()
    session["quick_open"] = True
    session["quick_manage"] = True
    await _servermgr_refresh_quick_markup(query, context, sessions, session_id)
    return SERVERMGR_CMD_INPUT


async def servermgr_quick_del(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    rest = query.data.replace(QUICK_DEL_CB_PREFIX, "", 1)
    qc_id, _, session_id = rest.rpartition("_")
    sessions = context.user_data.get("servermgr_sessions", {})
    session = sessions.get(session_id)
    if session is None:
        await query.answer("That tab is no longer open.", show_alert=True)
        return _cmd_state_or_end(context)
    removed = automation.remove_quick_command(query.from_user.id, qc_id)
    await query.answer("✕ Removed." if removed else "Already removed.")
    if not automation.get_quick_commands(query.from_user.id, session["server_id"]):
        session["quick_manage"] = False
    await _servermgr_refresh_quick_markup(query, context, sessions, session_id)
    return SERVERMGR_CMD_INPUT


async def _cleanup_servermgr_quickadd_messages(context: ContextTypes.DEFAULT_TYPE, chat_id: int, *message_ids):
    """Best-effort delete of the throwaway prompt/reply messages from the quick-add
    conversation, so it doesn't leave a trail behind once it's done."""
    for mid in message_ids:
        if not mid:
            continue
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=mid)
        except BadRequest as e:
            if "message to delete not found" not in str(e).lower():
                logger.debug(f"servermgr: could not delete quickadd message {mid}: {e}")
        except Exception as e:
            logger.debug(f"servermgr: could not delete quickadd message {mid}: {e}")


async def servermgr_quick_add_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    session_id = query.data.replace(QUICK_ADD_CB_PREFIX, "", 1)
    sessions = context.user_data.get("servermgr_sessions", {})
    session = sessions.get(session_id)
    if session is None:
        await query.answer("That tab is no longer open.", show_alert=True)
        return _cmd_state_or_end(context)
    await query.answer()
    context.user_data["servermgr_quickadd_session"] = session_id
    msg = await query.message.reply_text(
        "+ *Add quick command*\n\nSend the button label (e.g. `Restart nginx`):",
        parse_mode="Markdown", reply_markup=ReplyKeyboardMarkup([[CANCEL_BUTTON_TEXT]], resize_keyboard=True),
    )
    context.user_data["servermgr_quickadd_prompt_msg_id"] = msg.message_id
    return SERVERMGR_QUICKADD_LABEL


async def servermgr_quick_add_label_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    label = (update.message.text or "").strip()
    if not label:
        await update.message.reply_text("Label can't be empty. Send the button label:")
        return SERVERMGR_QUICKADD_LABEL
    context.user_data["servermgr_quickadd_label"] = label

    prev_prompt_id = context.user_data.pop("servermgr_quickadd_prompt_msg_id", None)
    await _cleanup_servermgr_quickadd_messages(
        context, update.effective_chat.id, prev_prompt_id, update.message.message_id,
    )

    msg = await update.message.reply_text(
        f"Label: `{label}`\n\nNow send the shell command to run when it's tapped (e.g. `systemctl restart nginx`):",
        parse_mode="Markdown",
    )
    context.user_data["servermgr_quickadd_prompt_msg_id"] = msg.message_id
    return SERVERMGR_QUICKADD_CMD


async def servermgr_quick_add_cmd_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cmd = (update.message.text or "").strip()
    if not cmd:
        await update.message.reply_text("Command can't be empty. Send the shell command:")
        return SERVERMGR_QUICKADD_CMD
    label = context.user_data.pop("servermgr_quickadd_label", cmd)
    session_id = context.user_data.pop("servermgr_quickadd_session", None)
    user_id = update.effective_user.id

    prev_prompt_id = context.user_data.pop("servermgr_quickadd_prompt_msg_id", None)
    await _cleanup_servermgr_quickadd_messages(
        context, update.effective_chat.id, prev_prompt_id, update.message.message_id,
    )

    sessions = context.user_data.get("servermgr_sessions", {})
    session = sessions.get(session_id) if session_id else None

    if not session:
        await update.message.reply_text("Session expired.", reply_markup=ReplyKeyboardRemove())
        return _cmd_state_or_end(context)

    automation.add_quick_command(user_id, session["server_id"], label, cmd)
    confirm_msg = await update.message.reply_text(
        f"✓ Added to the Quick menu: *{label}* → `{cmd}`",
        parse_mode="Markdown", reply_markup=ReplyKeyboardRemove(),
    )
    session["quick_open"] = True
    session["quick_manage"] = False
    term_state = session.get("term_state")
    if term_state and term_state.get("msg"):
        try:
            cur_active = context.user_data.get("servermgr_active_session")
            await term_state["msg"].edit_reply_markup(
                reply_markup=_terminal_keyboard(sessions, cur_active, session_id)
            )
        except BadRequest as e:
            if "not modified" not in str(e).lower():
                logger.warning(f"servermgr: could not refresh quick menu after add: {e}")
        except Exception as e:
            logger.warning(f"servermgr: could not refresh quick menu after add: {e}")

    # The terminal keyboard now shows the new button - the confirmation text has
    # done its job, so clear it too instead of leaving it sitting in the chat.
    await _cleanup_servermgr_quickadd_messages(context, update.effective_chat.id, confirm_msg.message_id)
    return SERVERMGR_CMD_INPUT


async def servermgr_quick_run(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    rest = query.data.replace(QUICK_RUN_CB_PREFIX, "", 1)
    qc_id, _, session_id = rest.rpartition("_")
    sessions = context.user_data.get("servermgr_sessions", {})
    session = sessions.get(session_id)
    if session is None:
        await query.answer("That tab is no longer open.", show_alert=True)
        return _cmd_state_or_end(context)
    qc = automation.get_quick_command(query.from_user.id, qc_id)
    if not qc:
        await query.answer("Unknown quick command.", show_alert=True)
        return SERVERMGR_CMD_INPUT
    if session.get("busy"):
        await query.answer("⏳ This tab is already running something.", show_alert=True)
        return SERVERMGR_CMD_INPUT

    server = settings.get_server(query.from_user.id, session["server_id"])
    if not server:
        await query.answer("Server not found.", show_alert=True)
        return SERVERMGR_CMD_INPUT

    await query.answer(f"▶ {qc['command']}")
    session["quick_open"] = False
    context.user_data["servermgr_active_session"] = session_id
    await _servermgr_refresh_quick_markup(query, context, sessions, session_id)

    if not engine.is_alive(session["client"]) or session["channel"] is None or session["channel"].closed:
        new_client, timed_out = await _run_with_timeout(engine.connect, server, timeout=CONNECT_TIMEOUT)
        if timed_out or new_client is None or isinstance(new_client, Exception):
            await context.bot.send_message(
                chat_id=session["chat_id"],
                text=f"✕ The connection to \"{server['label']}\" dropped and reconnecting also failed.",
            )
            return SERVERMGR_CMD_INPUT
        new_channel, shell_timed_out = await _run_with_timeout(engine.open_shell, new_client, timeout=CONNECT_TIMEOUT)
        if shell_timed_out or new_channel is None or isinstance(new_channel, Exception):
            try:
                new_client.close()
            except Exception:
                pass
            await context.bot.send_message(
                chat_id=session["chat_id"],
                text=f"✕ The connection to \"{server['label']}\" dropped and reopening a shell also failed.",
            )
            return SERVERMGR_CMD_INPUT
        session["client"], session["channel"] = new_client, new_channel

    session["busy"] = True
    session["cancel_requested"] = False
    asyncio.create_task(_stream_command(context, session_id, qc["command"], session["chat_id"]))
    return SERVERMGR_CMD_INPUT


def register_handlers(application):
    from telegram.ext import CallbackQueryHandler, MessageHandler, filters
    application.add_handler(MessageHandler(filters.Regex(f"^{MENU_BUTTON_TEXT}$"), servermgr_open))
    application.add_handler(CallbackQueryHandler(servermgr_menu, pattern="^servermgr_menu$"))
    application.add_handler(CallbackQueryHandler(servermgr_back_to_main, pattern="^servermgr_back_to_main$"))
    application.add_handler(CallbackQueryHandler(servermgr_srv_detail, pattern="^servermgr_srv_(?!add$)"))
    application.add_handler(CallbackQueryHandler(servermgr_del_confirm_prompt, pattern="^servermgr_del_"))
    application.add_handler(CallbackQueryHandler(servermgr_del_execute, pattern="^servermgr_delok_"))
    application.add_handler(CallbackQueryHandler(servermgr_trustkey_confirm, pattern="^servermgr_trustkey_"))
    application.add_handler(CallbackQueryHandler(servermgr_noop, pattern="^servermgr_noop$"))
    application.add_handler(CallbackQueryHandler(servermgr_health_check, pattern=f"^{HEALTH_CHECK_CB_PREFIX}"))
    application.add_handler(CallbackQueryHandler(servermgr_health_toggle, pattern=f"^{HEALTH_TOGGLE_CB_PREFIX}"))
    application.add_handler(CallbackQueryHandler(servermgr_restart_execute, pattern=f"^{HEALTH_RESTART_CONFIRM_CB_PREFIX}"))
    application.add_handler(CallbackQueryHandler(servermgr_restart_confirm_prompt, pattern=f"^{HEALTH_RESTART_CB_PREFIX}"))
    application.add_handler(CallbackQueryHandler(servermgr_cleanup_execute, pattern=f"^{HEALTH_CLEANUP_CB_PREFIX}"))