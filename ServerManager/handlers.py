# ====================== Server Manager (public feature - every user manages their own server(s)) ======================
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
import subscription

logger = logging.getLogger(__name__)

# ====================== Conversation States ======================
# Add-server wizard: one short question per step instead of one dense
# multi-line message - see the "Add server (step-by-step wizard)" section below.
SERVERMGR_ADD_HOST = 610
SERVERMGR_ADD_PORT = 611
SERVERMGR_ADD_USER = 612
SERVERMGR_ADD_AUTHTYPE = 613
SERVERMGR_ADD_SECRET = 614
SERVERMGR_ADD_PASSPHRASE = 615
SERVERMGR_ADD_LABEL = 616
SERVERMGR_CMD_INPUT = 605
SERVERMGR_QUICKPING_INPUT = 606
SERVERMGR_FILES_BROWSE = 620

CANCEL_BUTTON_TEXT = "❌ Cancel"
CMD_DONE_TEXT = "🔚 End Session"
MENU_BUTTON_TEXT = "🖥 Server Manager"   # matches the KeyboardButton added to handlers.get_main_menu()
CONNECT_TIMEOUT = 15   # seconds, for opening the SSH connection
COMMAND_TIMEOUT = 6 * 60 * 60   # seconds (6 hours), for running one command (incl. paramiko's own exec timeout) - long because there's a manual Cancel button, so no need to auto-kill quickly
MAX_SESSIONS = 3   # max concurrent SSH "tabs" per user - see the sessions dict in _close_one_session() etc.

# File browser (SFTP): its own dedicated connection, entirely separate from
# terminal tabs above, so it never counts against MAX_SESSIONS and closing
# one never affects the other.
FILES_START_CB_PREFIX = "servermgr_filesopen_"
FILES_NAV_CB_PREFIX = "servermgr_filesnav_"
FILES_DL_CB_PREFIX = "servermgr_filesdl_"
FILES_UP_CB = "servermgr_filesup"
FILES_UPLOAD_HERE_CB = "servermgr_filesuploadhere"
FILES_GOTO_CB = "servermgr_filesgoto"
FILES_URLDL_CB = "servermgr_filesurldl"
FILES_CLOSE_CB = "servermgr_filesclose"
FILES_REFRESH_CB = "servermgr_filesrefresh"
FILES_ACTIONS_CB_PREFIX = "servermgr_filesact_"          # "⚙️" on a row -> rename/edit/delete menu for that entry
FILES_EDIT_CB_PREFIX = "servermgr_filesedit_"            # "📝 Edit" tapped -> loads & shows the file's text content
FILES_RENAME_CB_PREFIX = "servermgr_filesren_"           # "✏️ Rename" tapped -> prompts for the new name
FILES_DELCONFIRM_CB_PREFIX = "servermgr_filesdelconf_"   # "🗑 Delete" tapped -> are-you-sure prompt
FILES_DELOK_CB_PREFIX = "servermgr_filesdelok_"          # confirmed -> actually deletes
FILES_BACK_CB = "servermgr_filesback"                    # "🔙 Back" out of the actions/delete-confirm submenu
SFTP_MAX_LIST_ENTRIES = 40   # keep the keyboard well under Telegram's per-message button limit
FILE_TRANSFER_TIMEOUT = 120  # seconds - more generous than CONNECT_TIMEOUT since transfers can take a while
URL_DOWNLOAD_TIMEOUT = 600   # seconds - server-side wget/curl, so this can run much longer than a Telegram upload/download ever could

# Health check (on-demand CPU/RAM/Disk snapshot) + per-server alerts toggle -
# background monitoring itself lives in server_manager_health.py.
HEALTH_CHECK_CB_PREFIX = "servermgr_health_"
HEALTH_TOGGLE_CB_PREFIX = "servermgr_healthtoggle_"

# "🔄 Restart" / "🧹 Cleanup" buttons shown on the health card. Restart reboots
# the server (destructive to any open session), so it goes through an
# are-you-sure step first, same as server deletion; cleanup only clears junk
# files (logs/caches/tmp) so it runs immediately.
HEALTH_RESTART_CB_PREFIX = "servermgr_restart_"          # "🔄 Restart" tapped -> are-you-sure prompt
HEALTH_RESTART_CONFIRM_CB_PREFIX = "servermgr_restartok_"  # confirmed -> actually reboots
HEALTH_CLEANUP_CB_PREFIX = "servermgr_cleanup_"          # "🧹 Cleanup" tapped -> runs disk cleanup directly

# Add-server wizard: default values offered as one-tap buttons, and the
# callback_data strings those buttons use.
ADD_DEFAULT_PORT = 22
ADD_DEFAULT_USER = "root"
ADD_PORT_DEFAULT_CB = "servermgr_addport_default"
ADD_USER_DEFAULT_CB = "servermgr_adduser_default"
ADD_AUTH_PASS_CB = "servermgr_addauth_pass"
ADD_AUTH_KEY_CB = "servermgr_addauth_key"
ADD_PASSPHRASE_NONE_CB = "servermgr_addpass_none"
ADD_LABEL_DEFAULT_CB = "servermgr_addlabel_default"

# Live "terminal" view while a command is streaming
CMD_CANCEL_CALLBACK = "servermgr_cmdcancel"
TERMINAL_EDIT_INTERVAL = 1.2   # min seconds between Telegram message edits (stay well under rate limits)
TERMINAL_MAX_IDLE_EDIT = 4.0   # force a heartbeat edit at least this often even with no new output
TERMINAL_BODY_CHARS = 3200     # tail of output kept inside the code block (Telegram caps messages at 4096 chars)

_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]|\x1b\][^\x07]*\x07|\x1b[=>]")

def _strip_ansi(text: str) -> str:
    text = _ANSI_ESCAPE_RE.sub("", text)
    # A pty ends every line with "\r\n" - that trailing \r is just part of the
    # normal line ending, not a mid-line overwrite, so normalize it away first.
    text = text.replace("\r\n", "\n")
    # Any \r still left is a genuine mid-line overwrite (e.g. a progress bar
    # redrawing the same line) - keep only what was printed last on that line.
    if "\r" in text:
        text = "\n".join(line.split("\r")[-1] for line in text.split("\n"))
    return text

# ====================== External deps (set from main.py) ======================
_get_main_menu_func = None


def set_get_main_menu(func):
    global _get_main_menu_func
    _get_main_menu_func = func


def get_main_menu():
    if _get_main_menu_func:
        return _get_main_menu_func()
    return None


def _uid(update: Update) -> int:
    return update.effective_user.id


def _short_host(host: str) -> str:
    """Compact form of a host for button labels - last octet for an IPv4
    address, first label for a domain - so buttons like 'NL1 -> 209' stay
    short instead of showing the full address."""
    host = (host or "").strip()
    parts = host.split(".")
    if len(parts) == 4 and all(p.isdigit() for p in parts):
        return parts[-1]
    return parts[0] if parts else host


_NO_SUBSCRIPTION_TEXT = (
    "🔒 Server Manager requires an active subscription.\n\n"
    "Tap 💳 Subscription on the main menu to buy or renew a plan."
)


def _effective_tab_limit(user_id) -> int:
    """The lower of the account-wide hard ceiling (MAX_SESSIONS) and the
    user's plan's max_tabs - a plan can restrict tabs further but never
    exceed the hard technical ceiling."""
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
    """Short label per tab: its open-order number (so two tabs on the same
    server are still distinguishable) plus the server's own label, kept
    minimal so it fits comfortably on a button."""
    ids = list(sessions.keys())
    try:
        idx = ids.index(sid) + 1
    except ValueError:
        idx = len(ids) + 1
    server_label = sessions.get(sid, {}).get("label", "")
    return f"{idx} · {server_label}" if server_label else str(idx)


def _tabs_keyboard_rows(sessions: dict, active_id: str) -> list:
    """Tab-switcher rows, meant to be folded directly into a terminal message's
    own inline keyboard (see _terminal_keyboard below) rather than posted as a
    separate '🗂 Tabs:' message - so the controls stay attached to the terminal
    box itself and switching/opening/closing tabs is always one tap away."""
    rows = []
    for sid in sessions:
        is_active = sid == active_id
        rows.append([
            InlineKeyboardButton(
                f"{'🟢' if is_active else '⚪'} {_tab_label(sessions, sid)}",
                callback_data="servermgr_noop" if is_active else f"{TAB_SWITCH_CB_PREFIX}{sid}",
            ),
            InlineKeyboardButton("Close", callback_data=f"{TAB_CLOSE_CB_PREFIX}{sid}"),
        ])
    if len(sessions) < MAX_SESSIONS:
        rows.append([InlineKeyboardButton("➕ New Tab", callback_data=TAB_NEWTAB_CB)])
    return rows


def _terminal_keyboard(sessions: dict, active_id: str, session_id: str = None) -> InlineKeyboardMarkup:
    """The single keyboard attached to every terminal message: tab switcher/closer
    rows plus, when session_id is given (i.e. this message belongs to an actual
    command run), a Cancel row for that specific tab's command."""
    rows = _tabs_keyboard_rows(sessions, active_id)
    if session_id:
        rows.append([InlineKeyboardButton("❌ Cancel", callback_data=f"{CMD_CANCEL_CALLBACK}_{session_id}")])
    return InlineKeyboardMarkup(rows)


def _cancel_session_id_from_markup(reply_markup) -> str:
    """Reads back which tab a terminal message's Cancel button (if any) belongs
    to, so refreshing its tab row (switch/close/new-tab taps) can rebuild the
    keyboard without dropping or misplacing that Cancel button."""
    if not reply_markup:
        return None
    for row in reply_markup.inline_keyboard:
        for btn in row:
            if btn.callback_data and btn.callback_data.startswith(f"{CMD_CANCEL_CALLBACK}_"):
                return btn.callback_data.replace(f"{CMD_CANCEL_CALLBACK}_", "", 1)
    return None


async def _clear_tab_state_msg(bot, session: dict):
    """Deletes a tab's leftover 'catch-up' box (blank Ready placeholder, or a
    resent copy of its last output), if any - called right before that tab gets
    a fresh one or an actual command starts, so the old one never lingers in
    the chat as a duplicate."""
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
    """Sent right after opening/switching to a tab. Telegram has no way to pull
    an old message back down to the bottom of the chat, so this posts a fresh
    copy of wherever that tab actually left off: its last command's output and
    status if it has run one, or a blank 'ready' box if it hasn't - switching
    tabs never looks like it reset progress. Always carries the tab switcher
    (no separate tabs message needed), and replaces (deletes) any older copy
    of this same catch-up box for this tab first."""
    if not sessions or active_id not in sessions:
        return
    session = sessions[active_id]
    await _clear_tab_state_msg(bot, session)
    term_state = session.get("term_state")
    if term_state:
        text = _format_terminal(
            term_state["label"], term_state["command"], term_state["output"],
            term_state.get("status", "✅ Ready — send the next command."),
        )
        keyboard = _terminal_keyboard(sessions, active_id, active_id)
    else:
        label = session.get("label", "")
        text = _format_terminal(label, "", "", "🟢 Ready — send a command.")
        keyboard = _terminal_keyboard(sessions, active_id)
    msg = await bot.send_message(chat_id=chat_id, text=text, parse_mode="Markdown", reply_markup=keyboard)
    session["tab_state_msg"] = (msg.chat_id, msg.message_id)


async def servermgr_noop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()


async def servermgr_tabsbar_newtab(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """"➕ New Tab" on the tabs bar - lists ALL of the user's registered servers,
    including ones that already have a tab open, so opening a second (or third)
    tab to the same server is a single extra tap instead of a dead end."""
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
            text=f"⚠️ Your plan allows {tab_limit} concurrent terminal tab(s). Close one first, then try again.",
        )
        return SERVERMGR_CMD_INPUT

    open_counts = {}
    for s in sessions.values():
        open_counts[s["server_id"]] = open_counts.get(s["server_id"], 0) + 1

    def _btn_text(s):
        count = open_counts.get(s["id"], 0)
        suffix = f" — {count} open" if count else ""
        return f"{'🟢 ' if count else ''}🖥 {s['label']} -> {_short_host(s['host'])}{suffix}"

    keyboard = [[InlineKeyboardButton(_btn_text(s), callback_data=f"servermgr_ssh_start_{s['id']}")] for s in servers]
    await context.bot.send_message(
        chat_id=query.message.chat_id, text="Pick a server to open in a new tab:", reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return SERVERMGR_CMD_INPUT


async def _run_with_timeout(func, *args, timeout: int):
    try:
        result = await asyncio.wait_for(asyncio.to_thread(func, *args), timeout=timeout)
        return result, False
    except asyncio.TimeoutError:
        return None, True
    except Exception as e:
        logger.warning(f"servermgr background call failed: {e}")
        return e, False


async def _sftp_call(func, *args, timeout: int = FILE_TRANSFER_TIMEOUT):
    """Thin wrapper over _run_with_timeout for SFTP calls, normalized to a
    (result, error_message) pair so file-browser handlers don't each have to
    juggle the timed_out/Exception cases separately."""
    result, timed_out = await _run_with_timeout(func, *args, timeout=timeout)
    if timed_out:
        return None, f"⏱ Timed out after {timeout}s."
    if isinstance(result, Exception):
        return None, str(result)[:300]
    return result, None


def _format_terminal(label: str, command: str, output: str, status_line: str) -> str:
    header = f"🖥️ *Terminal* — `{label}`"
    if not command:
        # Idle state (tab just opened/switched to, nothing run yet) - no command
        # line and no empty code block, just the header and status.
        return f"{header}\n{status_line}"
    body = _strip_ansi(output)
    body = body[-TERMINAL_BODY_CHARS:]
    if len(output) > TERMINAL_BODY_CHARS:
        body = "…(older output trimmed)…\n" + body
    header += f"\n`$ {command}`"
    return f"{header}\n```\n{body}\n```\n{status_line}"


def _close_one_session(context: ContextTypes.DEFAULT_TYPE, session_id: str):
    """Closes and forgets a single SSH tab, leaving any other open tabs untouched."""
    sessions = context.user_data.get("servermgr_sessions", {})
    session = sessions.pop(session_id, None)
    if session is None:
        return
    engine.close_shell(session.get("channel"))
    client = session.get("client")
    if client is not None:
        try:
            client.close()
        except Exception:
            pass


def _close_ssh_session(context: ContextTypes.DEFAULT_TYPE):
    """Closes every open SSH tab for this user - used when leaving Server Manager entirely."""
    sessions = context.user_data.pop("servermgr_sessions", {}) or {}
    for session_id in list(sessions.keys()):
        session = sessions[session_id]
        engine.close_shell(session.get("channel"))
        client = session.get("client")
        if client is not None:
            try:
                client.close()
            except Exception:
                pass
    context.user_data.pop("servermgr_active_session", None)


def _close_file_browser(context: ContextTypes.DEFAULT_TYPE):
    """Closes the SFTP file browser's dedicated connection (if any). Separate
    from _close_ssh_session() above since a file browser is never one of the
    entries in servermgr_sessions - it has its own client/sftp pair."""
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


def _do_close_tab(context: ContextTypes.DEFAULT_TYPE, session_id: str):
    """Closes one tab and, if it was the active one, promotes another open tab
    to active (if any are left). Returns None if the tab was already gone, or
    a dict with what changed - used by both the server-detail-page close
    button and the tabs-bar close button below."""
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


def _cmd_state_or_end(context: ContextTypes.DEFAULT_TYPE):
    """Whether other tabs are still open determines if the conversation should stay
    alive (more commands may come in for them) or end (nothing left to talk to)."""
    return SERVERMGR_CMD_INPUT if context.user_data.get("servermgr_sessions") else ConversationHandler.END


async def servermgr_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    fb = context.user_data.get("servermgr_filebrowser")
    if fb and (fb.get("awaiting_upload") or fb.get("awaiting_path") or fb.get("awaiting_url")
               or fb.get("awaiting_rename_idx") is not None or fb.get("awaiting_edit_idx") is not None):
        # The file browser only shows this reply-keyboard Cancel button while
        # waiting for an upload, a typed path, a URL, a new name for a rename,
        # or new content for an edit (see the various servermgr_files_*_prompt
        # functions) - so this just aborts that wait and drops back into
        # browsing, rather than closing the whole SFTP session.
        was_upload = fb.get("awaiting_upload")
        fb["awaiting_upload"] = False
        fb["awaiting_path"] = False
        fb["awaiting_url"] = False
        fb["awaiting_rename_idx"] = None
        fb["awaiting_edit_idx"] = None
        await update.message.reply_text(
            "❌ Upload cancelled." if was_upload else "❌ Cancelled.", reply_markup=ReplyKeyboardRemove(),
        )
        await context.bot.send_message(
            chat_id=fb["chat_id"], text=_files_text(fb), reply_markup=_files_keyboard(fb), parse_mode="Markdown",
        )
        return SERVERMGR_FILES_BROWSE

    sessions = context.user_data.get("servermgr_sessions")
    if sessions:
        # Reached via the "🔚 End Session" button while at least one SSH tab is open -
        # close only the active tab, not every open tab.
        active_id = context.user_data.get("servermgr_active_session")
        label = sessions.get(active_id, {}).get("label", "session")
        if active_id:
            _close_one_session(context, active_id)
        sessions = context.user_data.get("servermgr_sessions", {})
        if sessions:
            new_active_id, new_session = next(iter(sessions.items()))
            context.user_data["servermgr_active_session"] = new_active_id
            await update.message.reply_text(
                f"🔚 Closed \"{label}\". Active tab: \"{new_session['label']}\".",
                reply_markup=_ssh_session_keyboard(),
            )
            await _send_tab_state(context.bot, update.effective_chat.id, sessions, new_active_id)
            return SERVERMGR_CMD_INPUT
        context.user_data.pop("servermgr_active_session", None)
        await update.message.reply_text(f"🔚 Closed \"{label}\". All SSH tabs are now closed.", reply_markup=ReplyKeyboardRemove())
        await _reply_with_servermgr_menu(update, "", context)
        return ConversationHandler.END

    _close_ssh_session(context)
    _close_file_browser(context)
    context.user_data.pop("servermgr_new_server", None)
    # Clear whatever custom reply-keyboard was showing (Cancel / End Session).
    # The main menu keyboard gets restored centrally in _reply_with_servermgr_menu()
    # below - see the comment there for why that step can't be skipped.
    await update.message.reply_text("❌ Operation cancelled.", reply_markup=ReplyKeyboardRemove())
    await _reply_with_servermgr_menu(update, "", context)
    return ConversationHandler.END


# ====================== Helpers ======================
def _servers_text_and_keyboard(user_id, sessions: dict = None):
    servers = settings.get_servers(user_id)
    sessions = sessions or {}
    open_server_ids = {s["server_id"] for s in sessions.values()}
    max_servers, max_tabs = subscription.get_limits(user_id)
    text = (
        "🖥 Server Manager\n\n"
        "Register your servers here, then run any command on them over SSH.\n"
        "To quickly ping an IP/domain (no server registration needed), use the button below.\n\n"
        f"📦 Plan limits: {len(servers)}/{max_servers} servers, {max_tabs} tab(s)\n\n"
    )
    if sessions:
        text += f"🟢 {len(sessions)}/{_effective_tab_limit(user_id)} terminal tabs open.\n\n"
    keyboard = []
    if servers:
        # Two servers per row
        for i in range(0, len(servers), 2):
            row = servers[i:i + 2]
            keyboard.append([
                InlineKeyboardButton(
                    f"{'⚠️ ' if health.get_last_known_status(s['id']) is False else ''}"
                    f"{'🟢 ' if s['id'] in open_server_ids else ''}🖥 {s['label']} -> {_short_host(s['host'])}",
                    callback_data=f"servermgr_srv_{s['id']}",
                )
                for s in row
            ])
    else:
        text += "❌ No servers registered yet."
    keyboard.append([
        InlineKeyboardButton("📶 Quick Ping", callback_data="servermgr_quickping_start"),
        InlineKeyboardButton("➕ Add Server", callback_data="servermgr_srv_add"),
    ])
    keyboard.append([InlineKeyboardButton("🔙 Back", callback_data="servermgr_back_to_main")])
    return text, InlineKeyboardMarkup(keyboard)


def _server_detail_text_and_keyboard(server: dict, sessions: dict = None, active_id: str = None):
    auth_line = "📄 SSH Key" if server.get("private_key") else "🔑 Password"
    sessions = sessions or {}
    own_sessions = [(sid, s) for sid, s in sessions.items() if s["server_id"] == server["id"]]
    text = (
        f"🖥 {server['label']}\n\n"
        f"Address: `{server['host']}:{server.get('port', 22)}`\n"
        f"Username: `{server['username']}`\n"
        f"Auth: {auth_line}"
    )
    rows = []
    if own_sessions:
        text += f"\n\n🟢 {len(own_sessions)} terminal tab(s) open on this server."
        for sid, _s in own_sessions:
            is_active = sid == active_id
            rows.append([
                InlineKeyboardButton(
                    f"🔀 {_tab_label(sessions, sid)}{' (active)' if is_active else ''}",
                    callback_data=f"servermgr_switch_{sid}",
                ),
                InlineKeyboardButton("🔚 Close", callback_data=f"servermgr_closetab_{sid}"),
            ])
    if len(sessions) < MAX_SESSIONS:
        run_label = "💻 Open Another Tab" if own_sessions else "💻 Run Command (SSH)"
        rows.append([InlineKeyboardButton(run_label, callback_data=f"servermgr_ssh_start_{server['id']}")])
    rows.append([InlineKeyboardButton("🗄 Open SFTP", callback_data=f"{FILES_START_CB_PREFIX}{server['id']}")])
    monitor_on = server.get("monitor_enabled", True)
    rows.append([
        InlineKeyboardButton("🩺 Health Check", callback_data=f"{HEALTH_CHECK_CB_PREFIX}{server['id']}"),
        InlineKeyboardButton(
            f"🔔 Alerts: {'ON' if monitor_on else 'OFF'}",
            callback_data=f"{HEALTH_TOGGLE_CB_PREFIX}{server['id']}",
        ),
    ])
    bottom_row = []
    rows.append([InlineKeyboardButton("⚙️ Automation", callback_data=f"svauto_menu_{server['id']}")])
    if not own_sessions:
        # Can't delete a server while it has live tabs - close them first.
        bottom_row.append(InlineKeyboardButton("🗑 Delete Server", callback_data=f"servermgr_del_{server['id']}"))
    bottom_row.append(InlineKeyboardButton("🔙 Back", callback_data="servermgr_menu"))
    rows.append(bottom_row)
    return text, InlineKeyboardMarkup(rows)


async def _reply_with_servermgr_menu(update: Update, message: str, context: ContextTypes.DEFAULT_TYPE = None):
    user_id = _uid(update)
    sessions = context.user_data.get("servermgr_sessions") if context else None
    text, reply_markup = _servers_text_and_keyboard(user_id, sessions)
    await update.message.reply_text(message or "Server Manager:", reply_markup=get_main_menu())
    try:
        await update.message.reply_text(text, reply_markup=reply_markup, parse_mode="Markdown")
    except Exception as e:
        logger.debug(f"Markdown reply failed, falling back to plain text: {e}")
        await update.message.reply_text(text, reply_markup=reply_markup, parse_mode=None)


# ====================== Entry point from the main reply-keyboard menu ======================
async def servermgr_open(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Triggered by the '🖥 Server Manager' button on the bot's main menu (any user)."""
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


# ====================== List / detail navigation ======================
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
        await query.edit_message_text("🔙 Returned to the main menu.")
    except Exception:
        pass
    await context.bot.send_message(
        chat_id=query.message.chat_id,
        text="Main menu:",
        reply_markup=get_main_menu(),
    )


async def servermgr_srv_detail(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    server_id = query.data.replace("servermgr_srv_", "", 1)
    server = settings.get_server(query.from_user.id, server_id)
    sessions = context.user_data.get("servermgr_sessions", {})
    if not server:
        text, reply_markup = _servers_text_and_keyboard(query.from_user.id, sessions)
        await query.edit_message_text("❌ Server not found.", reply_markup=reply_markup)
        return
    active_id = context.user_data.get("servermgr_active_session")
    text, reply_markup = _server_detail_text_and_keyboard(server, sessions, active_id)
    await query.edit_message_text(text, reply_markup=reply_markup, parse_mode="Markdown")


async def servermgr_health_check(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """On-demand CPU/RAM/Disk snapshot - runs the same check as the
    background monitor (server_manager_engine.check_health) but doesn't
    touch _last_state, since a manual check shouldn't affect when the
    next automatic down/disk alert is allowed to fire."""
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
        text = f"❌ Health check failed: {snapshot}"
    else:
        text = health.format_health_text(server["label"], snapshot)

    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("🔄 Restart", callback_data=f"{HEALTH_RESTART_CB_PREFIX}{server['id']}"),
        InlineKeyboardButton("🧹 Cleanup", callback_data=f"{HEALTH_CLEANUP_CB_PREFIX}{server['id']}"),
    ]])
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
    await query.answer(f"🔔 Alerts {'enabled' if new_state else 'disabled'} for this server.")

    server = settings.get_server(query.from_user.id, server_id)
    sessions = context.user_data.get("servermgr_sessions", {})
    active_id = context.user_data.get("servermgr_active_session")
    text, reply_markup = _server_detail_text_and_keyboard(server, sessions, active_id)
    await query.edit_message_text(text, reply_markup=reply_markup, parse_mode="Markdown")


# ====================== Restart (reboot) - confirm first, since it drops any open sessions ======================
async def servermgr_restart_confirm_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    server_id = query.data.replace(HEALTH_RESTART_CB_PREFIX, "", 1)
    server = settings.get_server(query.from_user.id, server_id)
    if not server:
        await query.answer("Server not found.", show_alert=True)
        return
    await query.answer()
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Yes, restart", callback_data=f"{HEALTH_RESTART_CONFIRM_CB_PREFIX}{server_id}"),
         InlineKeyboardButton("❌ Cancel", callback_data=f"servermgr_srv_{server_id}")],
    ])
    await query.edit_message_text(
        f"⚠️ Restart \"{server['label']}\"? This reboots the server and will close any open terminal tabs on it.",
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
        text = f"❌ Restart failed: {result}"
    elif not result.get("ok"):
        text = f"❌ Restart failed: {result.get('error') or 'unknown error'}"
    else:
        text = f"🔄 \"{server['label']}\" is rebooting now. Give it a minute, then run a Health Check to confirm it's back."

    try:
        await query.edit_message_text(text)
    except Exception:
        await context.bot.send_message(chat_id=query.message.chat_id, text=text)


# ====================== Cleanup - safe (logs/caches/tmp only), so it runs immediately with no confirmation ======================
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
        text = f"❌ Cleanup failed: {result}"
    elif not result.get("ok"):
        text = f"❌ Cleanup failed: {result.get('error') or 'unknown error'}"
    else:
        freed_mb = round((result.get("freed_bytes") or 0) / (1024 * 1024), 1)
        text = f"🧹 Cleanup finished on \"{server['label']}\" — approximately {freed_mb}MB freed."

    try:
        await query.edit_message_text(text)
    except Exception:
        await context.bot.send_message(chat_id=query.message.chat_id, text=text)


# ====================== Quick ping (no server registration needed) ======================
async def servermgr_quickping_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("📶 Quick Ping\n\nSend the IP or domain you want to ping:")
    await context.bot.send_message(chat_id=query.message.chat_id, text="Address:", reply_markup=_cancel_keyboard())
    return SERVERMGR_QUICKPING_INPUT


async def servermgr_quickping_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    if text in ("/cancel", CANCEL_BUTTON_TEXT):
        await _reply_with_servermgr_menu(update, "❌ Operation cancelled.", context)
        return ConversationHandler.END
    if not text:
        await update.message.reply_text("❌ Send an IP or domain:")
        return SERVERMGR_QUICKPING_INPUT

    await update.message.reply_text("⏳ Pinging...", reply_markup=ReplyKeyboardRemove())
    result, timed_out = await _run_with_timeout(engine.ping_host, text, 4, 2, timeout=20)

    if timed_out:
        reply = f"⏱ Ping to \"{text}\" took too long."
    elif result.get("error") and result.get("loss_percent") is None:
        reply = f"❌ Ping to \"{text}\" failed:\n{result['error']}"
    elif result["ok"]:
        avg = f"{result['avg_ms']:.0f}ms" if result.get("avg_ms") is not None else "-"
        reply = f"✅ \"{text}\" responded.\nAvg latency: {avg} — Packet loss: {result['loss_percent']}%"
    else:
        reply = f"❌ \"{text}\" did not respond to ping (packet loss {result.get('loss_percent', 100)}%)."

    await update.message.reply_text(reply)
    await _reply_with_servermgr_menu(update, "", context)
    return ConversationHandler.END


# ====================== Add server (step-by-step wizard) ======================
# One short question per step, with tap-to-use-default / tap-to-choose buttons
# wherever there's an obvious default or a small fixed set of choices - much
# harder to get wrong than the old single "type 5 lines correctly" message.
_ADD_STATE_KEY = "servermgr_new_server"


def _new_server_data(context: ContextTypes.DEFAULT_TYPE) -> dict:
    return context.user_data.setdefault(_ADD_STATE_KEY, {})


def _port_default_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton(f"✅ Use default ({ADD_DEFAULT_PORT})", callback_data=ADD_PORT_DEFAULT_CB)]])


def _user_default_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton(f"✅ Use default ({ADD_DEFAULT_USER})", callback_data=ADD_USER_DEFAULT_CB)]])


def _authtype_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("🔑 Password", callback_data=ADD_AUTH_PASS_CB),
        InlineKeyboardButton("📄 SSH Key", callback_data=ADD_AUTH_KEY_CB),
    ]])


def _passphrase_none_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("🚫 No passphrase", callback_data=ADD_PASSPHRASE_NONE_CB)]])


def _label_default_kb(host: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton(f"✅ Use \"{host}\"", callback_data=ADD_LABEL_DEFAULT_CB)]])


async def _prompt_port(bot, chat_id: int):
    await bot.send_message(
        chat_id=chat_id,
        text="🔌 *Port*\n\nSend the SSH port, or tap below for the default:",
        parse_mode="Markdown",
        reply_markup=_port_default_kb(),
    )


async def _prompt_username(bot, chat_id: int):
    await bot.send_message(
        chat_id=chat_id,
        text="👤 *Username*\n\nSend the SSH username, or tap below for the default:",
        parse_mode="Markdown",
        reply_markup=_user_default_kb(),
    )


async def _prompt_authtype(bot, chat_id: int):
    await bot.send_message(
        chat_id=chat_id,
        text="🔐 *Login method*\n\nHow should the bot authenticate?",
        parse_mode="Markdown",
        reply_markup=_authtype_kb(),
    )


async def _prompt_secret(bot, chat_id: int, auth_type: str, retry: bool = False):
    prefix = "❌ That key didn't load. Please paste it again" if retry else "Paste it below"
    if auth_type == "key":
        text = (
            "📄 *SSH Private Key*\n\n"
            f"{prefix} - the full block, including the "
            "`-----BEGIN ... -----` and `-----END ... -----` lines:"
        )
    else:
        text = "🔑 *Password*\n\nSend the SSH password:"
    await bot.send_message(chat_id=chat_id, text=text, parse_mode="Markdown")


async def _prompt_passphrase(bot, chat_id: int):
    await bot.send_message(
        chat_id=chat_id,
        text="🔏 *Key passphrase*\n\nSend the passphrase protecting this key, or tap below if it has none:",
        parse_mode="Markdown",
        reply_markup=_passphrase_none_kb(),
    )


async def _prompt_label(bot, chat_id: int, host: str):
    await bot.send_message(
        chat_id=chat_id,
        text="🏷 *Label*\n\nLast step - send a short name for this server (e.g. \"DE-1\"), "
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
        chat_id=chat_id, text=f"✅ Server \"{server['label']}\" registered ({auth_note}).",
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
        await query.answer("🔒 Your subscription has expired. Please renew it first.", show_alert=True)
        return ConversationHandler.END

    max_servers, _ = subscription.get_limits(user_id)
    if len(settings.get_servers(user_id)) >= max_servers:
        await query.answer(
            f"⚠️ Your plan allows up to {max_servers} server(s). Remove one or upgrade your plan to add more.",
            show_alert=True,
        )
        return ConversationHandler.END

    await query.answer()
    context.user_data[_ADD_STATE_KEY] = {"_uid": query.from_user.id}
    await query.edit_message_text("➕ *Add Server*\n\nSend the server's IP address or domain:", parse_mode="Markdown")
    await context.bot.send_message(
        chat_id=query.message.chat_id, text="👇 Type the address, or tap Cancel below:", reply_markup=_cancel_keyboard()
    )
    return SERVERMGR_ADD_HOST


async def servermgr_add_host_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    if not text:
        await update.message.reply_text("❌ Please send an address:")
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
        await update.message.reply_text("❌ Invalid port. Send a number between 1-65535, or tap below:", reply_markup=_port_default_kb())
        return SERVERMGR_ADD_PORT
    _new_server_data(context)["port"] = port
    await _prompt_username(context.bot, update.effective_chat.id)
    return SERVERMGR_ADD_USER


async def servermgr_add_port_default_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    _new_server_data(context)["port"] = ADD_DEFAULT_PORT
    try:
        await query.edit_message_text(f"🔌 Port: {ADD_DEFAULT_PORT} (default)")
    except Exception:
        pass
    await _prompt_username(context.bot, query.message.chat_id)
    return SERVERMGR_ADD_USER


async def servermgr_add_user_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    if not text:
        await update.message.reply_text("❌ Please send a username:")
        return SERVERMGR_ADD_USER
    _new_server_data(context)["user"] = text
    await _prompt_authtype(context.bot, update.effective_chat.id)
    return SERVERMGR_ADD_AUTHTYPE


async def servermgr_add_user_default_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    _new_server_data(context)["user"] = ADD_DEFAULT_USER
    try:
        await query.edit_message_text(f"👤 Username: {ADD_DEFAULT_USER} (default)")
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
        await query.edit_message_text(f"🔐 Login method: {'SSH Key' if is_key else 'Password'}")
    except Exception:
        pass
    await _prompt_secret(context.bot, query.message.chat_id, auth_type)
    return SERVERMGR_ADD_SECRET


async def servermgr_add_secret_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    if not text:
        await update.message.reply_text("❌ That was empty. Please send it again:")
        return SERVERMGR_ADD_SECRET

    data = _new_server_data(context)
    data["secret"] = text
    # Delete the message right away - it contains the password/private key in plaintext.
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
        await bot.send_message(chat_id=chat_id, text=f"❌ {load_error}")
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
        await query.edit_message_text("🔏 Key passphrase: (none)")
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
        await query.edit_message_text(f"🏷 Label: {data['host']}")
    except Exception:
        pass
    await _finish_add_server(update, context, query.message.chat_id, data["host"])
    return ConversationHandler.END


# ====================== Delete server ======================
async def servermgr_del_confirm_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    server_id = query.data.replace("servermgr_del_", "", 1)
    server = settings.get_server(query.from_user.id, server_id)
    if not server:
        await query.answer("Server not found.", show_alert=True)
        return
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Yes, delete", callback_data=f"servermgr_delok_{server_id}"),
         InlineKeyboardButton("❌ Cancel", callback_data=f"servermgr_srv_{server_id}")],
    ])
    await query.edit_message_text(f"Are you sure you want to delete \"{server['label']}\"?", reply_markup=keyboard)


async def servermgr_del_execute(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    server_id = query.data.replace("servermgr_delok_", "", 1)
    settings.remove_server(query.from_user.id, server_id)
    await query.answer("🗑 Deleted.")
    text, reply_markup = _servers_text_and_keyboard(query.from_user.id, context.user_data.get("servermgr_sessions"))
    await query.edit_message_text(text, reply_markup=reply_markup, parse_mode="Markdown")


async def servermgr_trustkey_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Deliberately forgets a pinned host key after a HostKeyChangedError, so the
    next connection attempt re-pins whatever key the server presents. Only reached
    after the user has seen the security warning and chosen to proceed anyway."""
    query = update.callback_query
    server_id = query.data.replace("servermgr_trustkey_", "", 1)
    server = settings.get_server(query.from_user.id, server_id)
    if not server:
        await query.answer("Server not found.", show_alert=True)
        return
    engine.forget_host_key(server["host"], server.get("port", 22))
    await query.answer("✅ Old key forgotten.")
    sessions = context.user_data.get("servermgr_sessions", {})
    text, reply_markup = _server_detail_text_and_keyboard(server, sessions, context.user_data.get("servermgr_active_session"))
    await query.edit_message_text(
        f"✅ The old host key for \"{server['label']}\" was removed. Tap \"Run Command (SSH)\" "
        f"again to reconnect - the new key will be pinned at that point.",
        reply_markup=reply_markup,
    )


# ====================== File browser (SFTP upload/download) ======================
def _human_size(n: int) -> str:
    size = float(n or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f}{unit}" if unit == "B" else f"{size:.1f}{unit}"
        size /= 1024
    return f"{size:.1f}TB"


async def _list_dir_safely(sftp, path: str):
    return await _sftp_call(engine.sftp_listdir, sftp, path)


def _files_status_line(fb: dict) -> str:
    """The live item-count / pending-action line, now rendered inside the
    message text (see _files_text) rather than as a fake button, so the
    keyboard stays short and this line can wrap/format freely."""
    count = len(fb["entries"])
    shown = min(count, SFTP_MAX_LIST_ENTRIES)
    counter = f"{shown} of {count} shown" if count > shown else f"{count} item{'s' if count != 1 else ''}"

    if fb.get("awaiting_upload"):
        return "📤 _Waiting for a file…_"
    if fb.get("awaiting_path"):
        return "✏️ _Waiting for a path…_"
    if fb.get("awaiting_url"):
        return "🔗 _Waiting for a URL…_"
    if fb.get("awaiting_rename_idx") is not None:
        return "✏️ _Waiting for a new name…_"
    if fb.get("awaiting_edit_idx") is not None:
        return "📝 _Waiting for new file content…_"
    return f"_{counter}_"


def _files_hint(fb: dict) -> str:
    """One-line legend so first-time users know what tapping each icon
    does, without needing a separate help screen. Hidden while an input
    is pending, since the status line already explains what's needed."""
    if fb.get("awaiting_upload") or fb.get("awaiting_path") or fb.get("awaiting_url") \
            or fb.get("awaiting_rename_idx") is not None or fb.get("awaiting_edit_idx") is not None:
        return ""
    return "📁 open · 📄 download · ⚙️ rename/edit/delete"


def _files_text(fb: dict) -> str:
    """Compact header: server name, a breadcrumb-style path, a status
    line, and a one-line usage hint - everything the user needs to
    orient themselves in one glance, without eating a row in the
    keyboard below."""
    hint = _files_hint(fb)
    lines = [
        f"🖥 *{fb['label']}*  ·  SFTP",
        f"📂 `{fb['cwd']}`",
        _files_status_line(fb),
    ]
    if hint:
        lines.append(f"_{hint}_")
    return "\n".join(lines)


def _files_keyboard(fb: dict) -> InlineKeyboardMarkup:
    entries = fb["entries"]
    rows = []
    for i, e in enumerate(entries[:SFTP_MAX_LIST_ENTRIES]):
        actions_btn = InlineKeyboardButton("⚙️", callback_data=f"{FILES_ACTIONS_CB_PREFIX}{i}")
        if e["is_dir"]:
            rows.append([
                InlineKeyboardButton(f"📁 {e['name']}", callback_data=f"{FILES_NAV_CB_PREFIX}{i}"),
                actions_btn,
            ])
        else:
            rows.append([
                InlineKeyboardButton(
                    f"📄 {e['name']}  ·  {_human_size(e['size'])}",
                    callback_data=f"{FILES_DL_CB_PREFIX}{i}",
                ),
                actions_btn,
            ])

    # Thin divider so the file list doesn't visually run straight into the
    # nav/action buttons below it - short and symmetric (dashes on both
    # sides of the path) rather than a long, one-sided line. Doubles as a
    # quiet reminder of the current path. Not a real button (servermgr_noop
    # just answers the tap and does nothing else).
    if rows:
        cwd_label = fb["cwd"] if fb["cwd"] not in ("/", "") else "/"
        rows.append([InlineKeyboardButton(f"── {cwd_label} ──", callback_data="servermgr_noop")])

    # Nav row: Up (only when not already at root) + Refresh, side by side.
    nav_row = []
    if fb["cwd"] not in ("/", ""):
        nav_row.append(InlineKeyboardButton("⬅️ Up", callback_data=FILES_UP_CB))
    nav_row.append(InlineKeyboardButton("🔄 Refresh", callback_data=FILES_REFRESH_CB))
    rows.append(nav_row)

    # Actions paired 2-per-row instead of 4 stacked full-width rows - same
    # controls, roughly half the vertical space.
    rows.append([
        InlineKeyboardButton("✏️ Go to path", callback_data=FILES_GOTO_CB),
        InlineKeyboardButton("🔗 From URL", callback_data=FILES_URLDL_CB),
    ])
    rows.append([
        InlineKeyboardButton("📤 Upload", callback_data=FILES_UPLOAD_HERE_CB),
        InlineKeyboardButton("✖️ Close", callback_data=FILES_CLOSE_CB),
    ])
    return InlineKeyboardMarkup(rows)


async def _render_file_browser(query, fb: dict):
    try:
        await query.edit_message_text(_files_text(fb), reply_markup=_files_keyboard(fb), parse_mode="Markdown")
    except BadRequest as e:
        if "not modified" not in str(e).lower():
            raise


async def servermgr_files_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Opens the file browser for a server over its own dedicated SFTP
    connection (never one of the terminal tabs in servermgr_sessions, and
    never limited by MAX_SESSIONS)."""
    query = update.callback_query
    user_id = query.from_user.id
    server_id = query.data.replace(FILES_START_CB_PREFIX, "", 1)
    server = settings.get_server(user_id, server_id)

    if not subscription.is_active(user_id):
        await query.answer("🔒 Your subscription has expired. Please renew it first.", show_alert=True)
        return ConversationHandler.END

    if not server:
        await query.answer("Server not found.", show_alert=True)
        return ConversationHandler.END

    # Only one file browser per user at a time - close any previous one first.
    _close_file_browser(context)

    await query.answer("⏳ Connecting...")
    client, timed_out = await _run_with_timeout(engine.connect, server, timeout=CONNECT_TIMEOUT)
    if timed_out:
        await query.edit_message_text(f"⏱ Connecting to \"{server['label']}\" took more than {CONNECT_TIMEOUT}s and was cancelled.")
        return ConversationHandler.END
    if isinstance(client, engine.HostKeyChangedError):
        await query.edit_message_text(
            f"🚨 SECURITY WARNING for \"{server['label']}\"\n\n{client}",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("⚠️ Trust new key & retry", callback_data=f"servermgr_trustkey_{server_id}")],
                [InlineKeyboardButton("🔙 Back", callback_data=f"servermgr_srv_{server_id}")],
            ]),
        )
        return ConversationHandler.END
    if client is None or isinstance(client, Exception):
        err = f"\n{client}" if isinstance(client, Exception) else ""
        await query.edit_message_text(f"❌ Failed to connect to \"{server['label']}\".{err}")
        return ConversationHandler.END

    sftp, err = await _sftp_call(engine.open_sftp, client, timeout=CONNECT_TIMEOUT)
    if err:
        try:
            client.close()
        except Exception:
            pass
        await query.edit_message_text(f"❌ Failed to open an SFTP session on \"{server['label']}\".\n{err}")
        return ConversationHandler.END

    cwd, _err = await _sftp_call(engine.sftp_home_dir, sftp, timeout=CONNECT_TIMEOUT)
    if not isinstance(cwd, str) or not cwd:
        cwd = "/"

    entries, err = await _list_dir_safely(sftp, cwd)
    if err:
        engine.close_sftp(sftp)
        try:
            client.close()
        except Exception:
            pass
        await query.edit_message_text(f"❌ Could not list \"{cwd}\" on \"{server['label']}\".\n{err}")
        return ConversationHandler.END

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
        await query.answer(f"❌ {err}", show_alert=True)
        return SERVERMGR_FILES_BROWSE

    fb["cwd"], fb["entries"] = new_cwd, new_entries
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
        await query.answer(f"❌ {err}", show_alert=True)
        return SERVERMGR_FILES_BROWSE

    fb["cwd"], fb["entries"] = new_cwd, new_entries
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
        await query.answer(f"❌ {err}", show_alert=True)
        return SERVERMGR_FILES_BROWSE

    fb["entries"] = new_entries
    await query.answer("🔄 Refreshed.")
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
            f"❌ \"{entry['name']}\" is {_human_size(entry['size'])} - too large to send here "
            f"(limit {_human_size(engine.SFTP_MAX_DOWNLOAD_BYTES)}).",
            show_alert=True,
        )
        return SERVERMGR_FILES_BROWSE

    await query.answer("⏳ Downloading...")
    remote_path = engine.sftp_join(fb["cwd"], entry["name"])
    local_path = os.path.join(tempfile.gettempdir(), f"svm_{uuid.uuid4().hex}_{entry['name']}")
    _, err = await _sftp_call(engine.sftp_download, fb["sftp"], remote_path, local_path, timeout=FILE_TRANSFER_TIMEOUT)
    try:
        if err:
            await context.bot.send_message(chat_id=fb["chat_id"], text=f"❌ Download of \"{entry['name']}\" failed: {err}")
        else:
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
        text=f"✏️ Send the path to open, e.g. `/opt/terminal-bot` (relative paths are relative to `{fb['cwd']}`).",
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
        await update.message.reply_text(f"❌ Could not open \"{path}\":\n{err}\n\nSend another path, or tap Cancel.")
        return SERVERMGR_FILES_BROWSE

    fb["awaiting_path"] = False
    fb["cwd"], fb["entries"] = path, entries
    await update.message.reply_text("✅ Moved.", reply_markup=ReplyKeyboardRemove())
    await context.bot.send_message(
        chat_id=fb["chat_id"], text=_files_text(fb), reply_markup=_files_keyboard(fb), parse_mode="Markdown",
    )
    return SERVERMGR_FILES_BROWSE


async def servermgr_files_urldl_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Lets the server pull a file itself via wget/curl and place it on the
    server (run over the plain SSH client, not SFTP) - since the file never passes through Telegram at all,
    Telegram's ~20MB bot-upload limit simply doesn't apply here."""
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
            f"🔗 Send the URL to upload directly into:\n`{fb['cwd']}`\n\n"
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
        await update.message.reply_text("❌ That doesn't look like an http(s) URL. Send a valid URL, or tap Cancel.")
        return SERVERMGR_FILES_BROWSE

    filename = urllib.parse.unquote(os.path.basename(urllib.parse.urlsplit(url).path))
    if not filename:
        filename = f"download_{uuid.uuid4().hex[:8]}"

    fb["awaiting_url"] = False
    status_msg = await update.message.reply_text(
        f"⏳ Uploading on the server into:\n`{fb['cwd']}/{filename}`\n\nThis can take a while for large files…",
        parse_mode="Markdown", reply_markup=ReplyKeyboardRemove(),
    )
    result, err = await _sftp_call(
        engine.remote_download_url, fb["client"], fb["cwd"], filename, url, URL_DOWNLOAD_TIMEOUT,
        timeout=URL_DOWNLOAD_TIMEOUT + 15,
    )

    if err:
        await status_msg.edit_text(f"❌ Upload failed: {err}")
        return SERVERMGR_FILES_BROWSE
    if result.get("error"):
        await status_msg.edit_text(f"❌ Upload failed: {result['error']}")
        return SERVERMGR_FILES_BROWSE
    if not result.get("ok"):
        stderr_tail = (result.get("stderr") or "").strip()[-500:]
        text = f"❌ Upload failed (exit code {result.get('exit_status')})."
        if stderr_tail:
            text += f"\n```\n{stderr_tail}\n```"
        await status_msg.edit_text(text, parse_mode="Markdown")
        return SERVERMGR_FILES_BROWSE

    new_entries, list_err = await _list_dir_safely(fb["sftp"], fb["cwd"])
    if not list_err:
        fb["entries"] = new_entries
    await status_msg.edit_text(f"✅ Uploaded \"{filename}\" directly on the server.")
    await context.bot.send_message(
        chat_id=fb["chat_id"],
        text=_files_text(fb),
        reply_markup=_files_keyboard(fb),
        parse_mode="Markdown",
    )
    return SERVERMGR_FILES_BROWSE


async def servermgr_files_text_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Routes a plain text message to whichever prompt the file browser is
    currently waiting on (go-to-path or download-by-URL); ignored otherwise."""
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
    await update.message.reply_text("Tap a button below, or use \"✏️ Go to path\" / \"🔗 Upload from URL\".")
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
            f"📤 Send the file to upload into:\n`{fb['cwd']}`\n\n"
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
        # A document arrived while browsing but before "Upload file here" was tapped.
        await update.message.reply_text("Tap \"📤 Upload file here\" first, then send the file.")
        return SERVERMGR_FILES_BROWSE

    doc = update.message.document
    if doc.file_size and doc.file_size > engine.SFTP_MAX_UPLOAD_BYTES:
        await update.message.reply_text(
            f"❌ \"{doc.file_name}\" is {_human_size(doc.file_size)} - too large to upload here "
            f"(limit {_human_size(engine.SFTP_MAX_UPLOAD_BYTES)})."
        )
        return SERVERMGR_FILES_BROWSE

    fb["awaiting_upload"] = False
    status_msg = await update.message.reply_text("⏳ Uploading...", reply_markup=ReplyKeyboardRemove())

    local_path = os.path.join(tempfile.gettempdir(), f"svm_{uuid.uuid4().hex}_{doc.file_name}")
    try:
        tg_file = await doc.get_file()
        await tg_file.download_to_drive(local_path)
        remote_path = engine.sftp_join(fb["cwd"], doc.file_name)
        _, err = await _sftp_call(engine.sftp_upload, fb["sftp"], local_path, remote_path, timeout=FILE_TRANSFER_TIMEOUT)
    except Exception as e:
        err = str(e)[:300]
    finally:
        try:
            os.remove(local_path)
        except Exception:
            pass

    if err:
        await status_msg.edit_text(f"❌ Upload of \"{doc.file_name}\" failed: {err}")
        return SERVERMGR_FILES_BROWSE

    new_entries, list_err = await _list_dir_safely(fb["sftp"], fb["cwd"])
    if not list_err:
        fb["entries"] = new_entries
    await status_msg.edit_text(f"✅ Uploaded \"{doc.file_name}\".")
    await context.bot.send_message(
        chat_id=fb["chat_id"],
        text=_files_text(fb),
        reply_markup=_files_keyboard(fb),
        parse_mode="Markdown",
    )
    return SERVERMGR_FILES_BROWSE


def _entry_at(fb: dict, idx: int):
    entries = fb["entries"]
    if idx < 0 or idx >= len(entries):
        return None
    return entries[idx]


async def servermgr_files_actions_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """"⚙️" tapped next to an item - shows a small Rename/Delete/Back submenu
    for that one entry, without disturbing the rest of the browser state."""
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

    kind = "📁 folder" if entry["is_dir"] else "📄 file"
    await query.answer()
    rows = []
    if not entry["is_dir"]:
        rows.append([InlineKeyboardButton("📝 Edit", callback_data=f"{FILES_EDIT_CB_PREFIX}{idx}")])
    rows.append([InlineKeyboardButton("✏️ Rename", callback_data=f"{FILES_RENAME_CB_PREFIX}{idx}")])
    rows.append([InlineKeyboardButton("🗑 Delete", callback_data=f"{FILES_DELCONFIRM_CB_PREFIX}{idx}")])
    rows.append([InlineKeyboardButton("🔙 Back", callback_data=FILES_BACK_CB)])
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
    # Strip the buttons off the "Rename / Delete / Back" menu now that one of
    # them was used - otherwise it stays fully tappable in the chat history,
    # and a stale/duplicate tap on it later (double-tap, slow network retry)
    # would reopen this same prompt against whatever is now at this index -
    # which, after a rename, is the entry's own NEW name, looking like a loop.
    try:
        await query.edit_message_text(f"✏️ Renaming `{entry['name']}`…", parse_mode="Markdown")
    except BadRequest as e:
        if "not modified" not in str(e).lower():
            logger.debug(f"could not clear actions menu before rename: {e}")
    await context.bot.send_message(
        chat_id=fb["chat_id"],
        text=f"✏️ Send the new name for `{entry['name']}` (name only, it stays in the same folder).",
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
            "❌ That item isn't listed anymore - try refreshing.", reply_markup=ReplyKeyboardRemove(),
        )
        await context.bot.send_message(
            chat_id=fb["chat_id"], text=_files_text(fb), reply_markup=_files_keyboard(fb), parse_mode="Markdown",
        )
        return SERVERMGR_FILES_BROWSE

    new_name = (update.message.text or "").strip()
    if not new_name or "/" in new_name:
        await update.message.reply_text("❌ That's not a valid name - it can't be empty or contain \"/\". Send another name, or tap Cancel.")
        return SERVERMGR_FILES_BROWSE

    old_path = engine.sftp_join(fb["cwd"], entry["name"])
    new_path = engine.sftp_join(fb["cwd"], new_name)
    fb["awaiting_rename_idx"] = None
    status_msg = await update.message.reply_text("⏳ Renaming...", reply_markup=ReplyKeyboardRemove())
    _, err = await _sftp_call(engine.sftp_rename, fb["sftp"], old_path, new_path)

    if err:
        try:
            await status_msg.edit_text(f"❌ Rename failed: {err}")
        except BadRequest as e:
            logger.debug(f"could not edit rename-failure status message: {e}")
            await context.bot.send_message(chat_id=fb["chat_id"], text=f"❌ Rename failed: {err}")
    else:
        new_entries, list_err = await _list_dir_safely(fb["sftp"], fb["cwd"])
        if not list_err:
            fb["entries"] = new_entries
        try:
            await status_msg.edit_text(f"✅ Renamed \"{entry['name']}\" to \"{new_name}\".")
        except BadRequest as e:
            logger.debug(f"could not edit rename-success status message: {e}")
            await context.bot.send_message(
                chat_id=fb["chat_id"], text=f"✅ Renamed \"{entry['name']}\" to \"{new_name}\"."
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
    """"📝 Edit" tapped in the actions submenu - loads a small text file in
    full and shows it in-chat. Replying to that message with new content
    overwrites the file, nano-style: view the whole buffer, retype it, save."""
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
            await query.edit_message_text(f"❌ Could not open \"{entry['name']}\" for editing.\n{err}")
        except BadRequest as e:
            logger.debug(f"could not show edit-open failure: {e}")
        await context.bot.send_message(
            chat_id=fb["chat_id"], text=_files_text(fb), reply_markup=_files_keyboard(fb), parse_mode="Markdown",
        )
        return SERVERMGR_FILES_BROWSE

    fb["awaiting_edit_idx"] = idx
    try:
        await query.edit_message_text(f"📝 Opening `{entry['name']}`…", parse_mode="Markdown")
    except BadRequest as e:
        if "not modified" not in str(e).lower():
            logger.debug(f"could not clear actions menu before edit: {e}")

    # HTML (not Markdown) here, and everything escaped - arbitrary file
    # content can freely contain *, _, ` etc. that would otherwise break
    # Markdown parsing or get silently swallowed.
    body = html.escape(content) if content else " "
    text = (
        f"📝 <b>{html.escape(entry['name'])}</b>\n"
        f"📂 <code>{html.escape(fb['cwd'])}</code>  ·  {_human_size(entry['size'])}\n\n"
        f"<pre>{body}</pre>\n\n"
        f"✏️ Reply with the new content to save, or tap Cancel below."
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
            "❌ That file isn't listed anymore - try refreshing.", reply_markup=ReplyKeyboardRemove(),
        )
        await context.bot.send_message(
            chat_id=fb["chat_id"], text=_files_text(fb), reply_markup=_files_keyboard(fb), parse_mode="Markdown",
        )
        return SERVERMGR_FILES_BROWSE

    new_content = update.message.text or ""
    if len(new_content.encode("utf-8")) > engine.SFTP_EDITOR_MAX_BYTES:
        await update.message.reply_text(
            f"❌ That's over {engine.SFTP_EDITOR_MAX_BYTES} bytes - too big to save here. "
            f"Send something shorter, or tap Cancel."
        )
        return SERVERMGR_FILES_BROWSE

    path = engine.sftp_join(fb["cwd"], entry["name"])
    fb["awaiting_edit_idx"] = None
    status_msg = await update.message.reply_text("⏳ Saving...", reply_markup=ReplyKeyboardRemove())
    _, err = await _sftp_call(engine.sftp_write_text, fb["sftp"], path, new_content)

    if err:
        try:
            await status_msg.edit_text(f"❌ Save failed: {err}")
        except BadRequest as e:
            logger.debug(f"could not edit save-failure status message: {e}")
            await context.bot.send_message(chat_id=fb["chat_id"], text=f"❌ Save failed: {err}")
    else:
        new_entries, list_err = await _list_dir_safely(fb["sftp"], fb["cwd"])
        if not list_err:
            fb["entries"] = new_entries
        try:
            await status_msg.edit_text(f"✅ Saved \"{entry['name']}\".")
        except BadRequest as e:
            logger.debug(f"could not edit save-success status message: {e}")
            await context.bot.send_message(chat_id=fb["chat_id"], text=f"✅ Saved \"{entry['name']}\".")

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
    warn = "\n\n⚠️ This will delete the folder and *everything inside it*." if entry["is_dir"] else ""
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Yes, delete", callback_data=f"{FILES_DELOK_CB_PREFIX}{idx}"),
         InlineKeyboardButton("❌ Cancel", callback_data=FILES_BACK_CB)],
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
        # A modal popup (not text baked into the listing) so the result is
        # seen right away even when the file list below is long enough that
        # a header note would scroll out of view.
        try:
            await query.answer(text[:200], show_alert=True)
        except BadRequest:
            pass

    if err:
        await _popup(f"❌ Delete failed: {err}")
        await _render_file_browser(query, fb)
        return SERVERMGR_FILES_BROWSE

    new_entries, list_err = await _list_dir_safely(fb["sftp"], fb["cwd"])
    if not list_err:
        fb["entries"] = new_entries
    await _popup(f"✅ Deleted \"{entry['name']}\".")
    await _render_file_browser(query, fb)
    return SERVERMGR_FILES_BROWSE


async def servermgr_files_back(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """"🔙 Back" / "❌ Cancel" out of the per-item actions or delete-confirm
    submenu, back to the normal file listing."""
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
    _close_file_browser(context)
    await query.answer()
    try:
        await query.edit_message_text(f"🔚 Closed the file browser for \"{label}\".")
    except Exception:
        pass
    return ConversationHandler.END


# ====================== SSH command execution (up to MAX_SESSIONS tabs per user) ======================
async def servermgr_ssh_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Opens a new SSH tab for a server, up to MAX_SESSIONS at once - including a
    second/third tab to a server that already has one open (each tap always opens
    a fresh connection; it never silently switches to an existing tab instead)."""
    query = update.callback_query
    user_id = query.from_user.id
    server_id = query.data.replace("servermgr_ssh_start_", "", 1)
    server = settings.get_server(user_id, server_id)
    sessions = context.user_data.setdefault("servermgr_sessions", {})

    if not subscription.is_active(user_id):
        await query.answer("🔒 Your subscription has expired. Please renew it first.", show_alert=True)
        return _cmd_state_or_end(context)

    if not server:
        await query.answer("Server not found.", show_alert=True)
        return _cmd_state_or_end(context)

    tab_limit = _effective_tab_limit(user_id)
    if len(sessions) >= tab_limit:
        await query.answer(
            f"⚠️ Your plan allows {tab_limit} concurrent terminal tab(s). Close one first (open its server page "
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
            f"🚨 SECURITY WARNING for \"{server['label']}\"\n\n{client}",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("⚠️ Trust new key & retry", callback_data=f"servermgr_trustkey_{server_id}")],
                [InlineKeyboardButton("🔙 Back", callback_data=f"servermgr_srv_{server_id}")],
            ]),
        )
        return _cmd_state_or_end(context)
    if client is None or isinstance(client, Exception):
        err = f"\n{client}" if isinstance(client, Exception) else ""
        await query.edit_message_text(f"❌ Failed to connect to \"{server['label']}\".{err}")
        return _cmd_state_or_end(context)

    channel, shell_timed_out = await _run_with_timeout(engine.open_shell, client, timeout=CONNECT_TIMEOUT)
    if shell_timed_out or channel is None or isinstance(channel, Exception):
        try:
            client.close()
        except Exception:
            pass
        err = f"\n{channel}" if isinstance(channel, Exception) else ""
        await query.edit_message_text(f"❌ Failed to open a shell on \"{server['label']}\".{err}")
        return _cmd_state_or_end(context)

    session_id = uuid.uuid4().hex[:6]
    sessions[session_id] = {
        "client": client,
        "channel": channel,
        "server_id": server_id,
        "label": server["label"],
        "chat_id": query.message.chat_id,
        "cmd_handle_box": {"handle": None},
        "cancel_requested": False,
        "busy": False,
        "term_state": None,
        "tab_state_msg": None,
    }
    context.user_data["servermgr_active_session"] = session_id

    await query.edit_message_text(
        f"💻 Connected to \"{server['label']}\" ({server['host']}) — tab {len(sessions)}/{tab_limit}.",
    )
    await context.bot.send_message(
        chat_id=query.message.chat_id,
        text=f"Active tab: \"{server['label']}\". Send a command:",
        reply_markup=_ssh_session_keyboard(),
    )
    await _send_tab_state(context.bot, query.message.chat_id, sessions, session_id)
    return SERVERMGR_CMD_INPUT


async def servermgr_switch_tab(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Inline "🔀 Switch to this tab" button on a server's detail page."""
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
        text, reply_markup = _server_detail_text_and_keyboard(server, sessions, session_id)
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
    """Inline "🔚 Close tab" button on a server's detail page - closes that specific
    tab even if it isn't the active one."""
    query = update.callback_query
    session_id = query.data.replace("servermgr_closetab_", "", 1)
    if session_id not in context.user_data.get("servermgr_sessions", {}):
        await query.answer("That tab is already closed.", show_alert=True)
        return _cmd_state_or_end(context)

    result = _do_close_tab(context, session_id)
    await query.answer(f"🔚 Closed \"{result['label']}\".")

    server = settings.get_server(query.from_user.id, result["server_id"])
    if server:
        text, reply_markup = _server_detail_text_and_keyboard(server, result["sessions"], result["active_id"])
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

    await context.bot.send_message(chat_id=query.message.chat_id, text="🔚 All SSH tabs closed.", reply_markup=ReplyKeyboardRemove())
    text, reply_markup = _servers_text_and_keyboard(query.from_user.id, {})
    await context.bot.send_message(chat_id=query.message.chat_id, text=text, reply_markup=reply_markup, parse_mode="Markdown")
    return ConversationHandler.END


# ---- Tabs-bar buttons (the "🗂 Tabs:" message under every command prompt) ----
# Same switch/close actions as above, but these refresh the tabs bar message
# itself rather than a server's detail page - kept as separate handlers/
# callback prefixes so the two contexts never edit the wrong message.
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
    await query.answer(f"🔚 Closed \"{result['label']}\".")

    if result["sessions"]:
        try:
            cancel_sid = _cancel_session_id_from_markup(query.message.reply_markup)
            if cancel_sid == session_id:
                cancel_sid = None   # that tab (and its running command, if any) is gone
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
        await query.edit_message_text("🔚 All tabs closed.")
    except Exception:
        pass
    await context.bot.send_message(chat_id=query.message.chat_id, text="Server Manager:", reply_markup=ReplyKeyboardRemove())
    text, reply_markup = _servers_text_and_keyboard(query.from_user.id, {})
    await context.bot.send_message(chat_id=query.message.chat_id, text=text, reply_markup=reply_markup, parse_mode="Markdown")
    return ConversationHandler.END


async def servermgr_cmd_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Routes a typed command to the *active* tab. Kicks off the streaming/execution
    as a detached background task and returns right away, so other tabs stay
    responsive (commands can run in more than one tab at the same time) instead of
    the whole conversation being stuck waiting for this one command to finish."""
    text = (update.message.text or "").strip()
    sessions = context.user_data.get("servermgr_sessions", {})
    active_id = context.user_data.get("servermgr_active_session")
    session = sessions.get(active_id) if active_id else None

    if not sessions or session is None:
        _close_ssh_session(context)
        await update.message.reply_text("⚠️ No active SSH tab. Please open one again.", reply_markup=ReplyKeyboardRemove())
        await _reply_with_servermgr_menu(update, "Server Manager menu:", context)
        return ConversationHandler.END

    if not text:
        await update.message.reply_text("Send a command:")
        return SERVERMGR_CMD_INPUT

    if session.get("busy"):
        await update.message.reply_text(
            f"⏳ A command is still running in \"{session['label']}\". Wait for it to finish, or open another "
            f"server's page to switch to (or open) a different tab in the meantime."
        )
        return SERVERMGR_CMD_INPUT

    server = settings.get_server(_uid(update), session["server_id"])
    if not server:
        _close_one_session(context, active_id)
        await update.message.reply_text(f"⚠️ \"{session['label']}\" no longer exists. Tab closed.", reply_markup=ReplyKeyboardRemove())
        return _cmd_state_or_end(context)

    client, channel = session["client"], session["channel"]

    if not engine.is_alive(client) or channel is None or channel.closed:
        # Try reconnecting (and reopening a shell) once before giving up on this tab.
        # This one reconnect step stays synchronous/blocking - it's short (<= CONNECT_TIMEOUT)
        # and rare, unlike the command streaming below which can run far longer.
        new_client, timed_out = await _run_with_timeout(engine.connect, server, timeout=CONNECT_TIMEOUT)
        if isinstance(new_client, engine.HostKeyChangedError):
            _close_one_session(context, active_id)
            await update.message.reply_text(
                f"🚨 SECURITY WARNING for \"{server['label']}\"\n\n{new_client}",
                reply_markup=ReplyKeyboardRemove(),
            )
            await update.message.reply_text(
                "Tab closed. Go to this server's page if you want to trust the new key and retry.",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("⚠️ Trust new key & retry", callback_data=f"servermgr_trustkey_{session['server_id']}"),
                ]]),
            )
            return _cmd_state_or_end(context)
        if timed_out or new_client is None or isinstance(new_client, Exception):
            _close_one_session(context, active_id)
            await update.message.reply_text(
                f"❌ The connection to \"{server['label']}\" dropped and reconnecting also failed. Tab closed.",
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
                f"❌ The connection to \"{server['label']}\" dropped and reopening a shell also failed. Tab closed.",
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
    """Runs one command against a tab's shell and live-updates a terminal message,
    exactly like the old single-session version - the only difference is this now
    runs as a detached task (see servermgr_cmd_input above) so it doesn't block the
    conversation from handling commands sent to other open tabs in the meantime."""
    sessions = context.user_data.get("servermgr_sessions", {})
    session = sessions.get(session_id)
    if session is None:
        return

    channel = session["channel"]
    label = session["label"]
    handle_box = session["cmd_handle_box"]
    handle_box["handle"] = None
    chunk_queue = queue_mod.Queue()

    # The idle "Ready — send a command" placeholder for this tab is about to be
    # superseded by the real running/output message below - clear it out first
    # so it doesn't linger in the chat.
    await _clear_tab_state_msg(context.bot, session)

    def on_chunk(handle, chunk_text):
        # Runs on the worker thread (see engine.run_command_stream). Just
        # hands the CommandHandle to this coroutine and queues output -
        # all actual Telegram calls happen below, on the event loop.
        handle_box["handle"] = handle
        if chunk_text:
            chunk_queue.put(chunk_text)

    try:
        active_id = context.user_data.get("servermgr_active_session")
        msg = await context.bot.send_message(
            chat_id=chat_id,
            text=_format_terminal(label, command_text, "", "⏳ Running…"),
            parse_mode="Markdown",
            reply_markup=_terminal_keyboard(sessions, active_id, session_id),
        )
    except Exception as e:
        logger.warning(f"servermgr: failed to send terminal message for tab {session_id}: {e}")
        session["busy"] = False
        return

    term_state = {"msg": msg, "label": label, "command": command_text, "output": "", "status": "⏳ Running…"}
    session["term_state"] = term_state

    task = asyncio.create_task(
        asyncio.to_thread(engine.run_shell_input, channel, command_text, COMMAND_TIMEOUT, engine.SHELL_QUIET_SECONDS, on_chunk)
    )

    output_so_far = ""
    last_edit_at = 0.0
    last_sent_text = None
    while not task.done():
        await asyncio.sleep(0.3)

        # The tab may have been closed (🔚 End Session / Close tab) while this
        # command was still running - ask the worker thread to stop and bail out
        # quietly rather than keep editing a message for a session that's gone.
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
        due_for_heartbeat = now - last_edit_at >= TERMINAL_MAX_IDLE_EDIT
        if (got_new and now - last_edit_at >= TERMINAL_EDIT_INTERVAL) or due_for_heartbeat:
            status = "⏳ Cancelling…" if session.get("cancel_requested") else "⏳ Running…"
            new_text = _format_terminal(label, command_text, output_so_far, status)
            if new_text != last_sent_text:
                try:
                    cur_sessions = context.user_data.get("servermgr_sessions", {})
                    cur_active = context.user_data.get("servermgr_active_session")
                    await msg.edit_text(
                        new_text, parse_mode="Markdown",
                        reply_markup=_terminal_keyboard(cur_sessions, cur_active, session_id),
                    )
                    last_sent_text = new_text
                    term_state["output"] = output_so_far
                    term_state["status"] = status
                except BadRequest as e:
                    if "not modified" not in str(e).lower():
                        logger.debug(f"terminal live edit failed: {e}")
                except Exception as e:
                    logger.debug(f"terminal live edit failed: {e}")
            last_edit_at = now

    # Drain whatever arrived between the last poll and the task actually finishing
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
        status = f"❌ Execution error: {result['error']}"
    elif result.get("cancelled"):
        status = "🛑 Cancelled. Session is still open — send the next command."
    elif result.get("timed_out"):
        status = f"⏱ No prompt back after {COMMAND_TIMEOUT // 3600}h — still running in the background."
    else:
        status = "✅ Ready — send the next command."

    final_text = _format_terminal(label, command_text, output_so_far, status)
    try:
        # Keep the Cancel button attached even after the command finishes -
        # the shell session itself stays open (see run_shell_input()'s design
        # note above), so the user may want to send Ctrl-C at any moment even
        # while sitting idle at a prompt, not only while a command is running.
        # servermgr_cmd_cancel_button() below handles both cases.
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

    term_state["output"] = output_so_far
    term_state["status"] = status


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
        # A command is actively streaming in this tab - cancel it the normal way.
        session["cancel_requested"] = True
        handle.cancel()
        await query.answer("🛑 Cancelling…")
        return

    # No command is currently running in this tab (the button is shown at all times -
    # see _stream_command()'s final edit above), but the shell session is still open,
    # so send Ctrl-C straight to it - e.g. to break out of a stuck foreground program
    # or an interactive menu the user no longer wants.
    channel = session.get("channel")
    if channel is None or channel.closed:
        await query.answer("Nothing to cancel right now.")
        return
    try:
        channel.send(engine.CTRL_C)
    except Exception:
        await query.answer("⚠️ Could not cancel - the session may have dropped.")
        return
    await query.answer("🛑 Ctrl-C sent.")

    # query.answer() only pops a small toast - it never touches the terminal
    # message, so without the block below the ^C and the fresh shell prompt
    # only became visible the next time the user sent a command (this was the
    # actual bug: the terminal looked unchanged right after tapping Cancel).
    # Give the pty a brief moment to echo the ^C and print its next prompt,
    # then fold that into the same terminal message right away.
    term_state = session.get("term_state")
    if term_state is None or term_state.get("msg") is None:
        return

    extra_output = ""
    start = time.monotonic()
    last_data_at = start
    while True:
        got_data = False
        try:
            if channel.recv_ready():
                chunk = channel.recv(4096).decode(errors="ignore")
                if chunk:
                    extra_output += chunk
                    got_data = True
                    last_data_at = time.monotonic()
            if channel.recv_stderr_ready():
                chunk = channel.recv_stderr(4096).decode(errors="ignore")
                if chunk:
                    extra_output += chunk
                    got_data = True
                    last_data_at = time.monotonic()
        except Exception:
            break
        now = time.monotonic()
        if now - last_data_at > engine.SHELL_QUIET_SECONDS or now - start > 5:
            break
        if not got_data:
            await asyncio.sleep(0.15)

    if not extra_output:
        return

    term_state["output"] += extra_output
    term_state["status"] = "🛑 Ctrl-C sent — send the next command."
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
            logger.debug(f"terminal cancel edit failed: {e}")
    except Exception as e:
        logger.debug(f"terminal cancel edit failed: {e}")


# ========== Handler registration (call this function from main.py) ==========
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
