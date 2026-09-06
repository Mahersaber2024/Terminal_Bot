import asyncio
import html
import io
import logging
import os
import queue as queue_mod
import re
import tempfile
import time
import uuid
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    Update,
)
from telegram.error import BadRequest
from telegram.ext import (
    ApplicationHandlerStop,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

import bot_settings
import backup_manager
import config
import subscription
import logger_bot
from db.database import get_db
from ServerManager import health as svm_health
from ServerManager import maintenance as svm_maintenance
from ServerManager import owner_server
from ServerManager import settings as svm_settings
from ServerManager import automation as svm_automation

logger = logging.getLogger(__name__)

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")


def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text or "")

ADMIN_CHANNEL_ADD_ID, ADMIN_CHANNEL_ADD_TITLE, ADMIN_CHANNEL_ADD_LINK = range(3)
(
    ADMIN_PLAN_ADD_NAME,
    ADMIN_PLAN_ADD_PRICE,
    ADMIN_PLAN_ADD_DAYS,
    ADMIN_PLAN_ADD_MAXSERVERS,
    ADMIN_PLAN_ADD_MAXTABS,
    ADMIN_PLAN_ADD_DESC,
) = range(3, 9)
ADMIN_PLAN_ADD_SFTP, ADMIN_PLAN_ADD_TIMEOUT, ADMIN_PLAN_ADD_MAXAUTO = 19, 20, 21
ADMIN_PLAN_EDIT_VALUE = 22
ADMIN_PLAN_ADD_ADVTOOLS = 23
ADMIN_CARD_NUMBER, ADMIN_CARD_HOLDER, ADMIN_CARD_BANK = range(9, 12)
(
    ADMIN_MON_INTERVAL,
    ADMIN_MON_TIMEOUT,
    ADMIN_MON_DISKPCT,
    ADMIN_MON_HYSTERESIS,
) = range(12, 16)
ADMIN_USER_SEARCH, ADMIN_USER_BALANCE_ADJUST = range(16, 18)
ADMIN_MON_CPUPCT, ADMIN_MON_RAMPCT = 23, 24
ADMIN_OWNERSRV_CMD, ADMIN_OWNERSRV_FILES = 25, 26
ADMIN_OWNERSRV_CRON = 27
ADMIN_OWNERSRV_TZ = 28
ADMIN_OWNERSRV_QUICKADD_LABEL, ADMIN_OWNERSRV_QUICKADD_CMD = 29, 30
ADMIN_LOGGROUP_SET = 31
ADMIN_BOTNAME_SET = 32
ADMIN_BACKUP_INTERVAL = 33
USERS_PAGE_SIZE = 10

OWNERSRV_FILES_START_CB = "admin_ownersrv_files"
OWNERSRV_FILES_NAV_CB_PREFIX = "admin_osf_nav_"
OWNERSRV_FILES_UP_CB = "admin_osf_up"
OWNERSRV_FILES_REFRESH_CB = "admin_osf_refresh"
OWNERSRV_FILES_DL_CB_PREFIX = "admin_osf_dl_"
OWNERSRV_FILES_GOTO_CB = "admin_osf_goto"
OWNERSRV_FILES_UPLOAD_HERE_CB = "admin_osf_uploadhere"
OWNERSRV_FILES_URLDL_CB = "admin_osf_urldl"
OWNERSRV_FILES_MKDIR_CB = "admin_osf_mkdir"
OSF_URLDL_TIMEOUT = 300
OWNERSRV_FILES_ACTIONS_CB_PREFIX = "admin_osf_act_"
OWNERSRV_FILES_RENAME_CB_PREFIX = "admin_osf_ren_"
OWNERSRV_FILES_EDIT_CB_PREFIX = "admin_osf_edit_"
OWNERSRV_FILES_DELCONFIRM_CB_PREFIX = "admin_osf_delc_"
OWNERSRV_FILES_DELOK_CB_PREFIX = "admin_osf_delok_"
OWNERSRV_UPLOAD_BATCH_DEBOUNCE = 1.2
OWNERSRV_FILES_BACK_CB = "admin_osf_back"
OWNERSRV_FILES_CLOSE_CB = "admin_osf_close"
OWNERSRV_FILES_MAX_LIST_ENTRIES = 40

OWNERSRV_FILES_SELMODE_CB = "admin_osf_selmode"
OWNERSRV_FILES_SELDONE_CB = "admin_osf_seldone"
OWNERSRV_FILES_SELTOGGLE_CB_PREFIX = "admin_osf_seltog_"
OWNERSRV_FILES_SELDELCONFIRM_CB = "admin_osf_seldelc"
OWNERSRV_FILES_SELDELOK_CB = "admin_osf_seldelok"
OWNERSRV_FILES_SELCOMPRESS_CB = "admin_osf_selzip"
OWNERSRV_TERMINAL_EXIT_CB = "admin_ownersrv_terminal_exit"
OWNERSRV_TERMINAL_CANCEL_CB = "admin_ownersrv_terminal_cancel"
OWNERSRV_TERMINAL_ENTER_CB = "admin_ownersrv_terminal_enter"
OWNERSRV_TERMINAL_YES_CB = "admin_ownersrv_terminal_yes"
OWNERSRV_TERMINAL_NO_CB = "admin_ownersrv_terminal_no"
OWNERSRV_TERMINAL_CANCEL_CB_PREFIX = f"{OWNERSRV_TERMINAL_CANCEL_CB}_"
OWNERSRV_TERMINAL_ENTER_CB_PREFIX = f"{OWNERSRV_TERMINAL_ENTER_CB}_"
OWNERSRV_TERMINAL_YES_CB_PREFIX = f"{OWNERSRV_TERMINAL_YES_CB}_"
OWNERSRV_TERMINAL_NO_CB_PREFIX = f"{OWNERSRV_TERMINAL_NO_CB}_"

OWNERSRV_MAX_TABS = 3
OWNERSRV_TAB_SWITCH_CB_PREFIX = "admin_ownersrv_tabswitch_"
OWNERSRV_TAB_CLOSE_CB_PREFIX = "admin_ownersrv_tabclose_"
OWNERSRV_TAB_NEW_CB = "admin_ownersrv_tabnew"
OWNERSRV_NOOP_CB = "admin_ownersrv_noop"

OWNERSRV_QUICK_COMMANDS = [
    ("df -h", "df -h"),
    ("free -h", "free -h"),
    ("docker ps", "docker ps"),
    ("nginx status", "systemctl status nginx --no-pager"),
    ("top (snapshot)", "top -bn1 | head -20"),
    ("journalctl", "journalctl -n 50 --no-pager"),
]
OWNERSRV_QUICK_MENU_CB_PREFIX = "admin_ownersrv_quickmenu_"
OWNERSRV_QUICK_CLOSE_CB_PREFIX = "admin_ownersrv_quickclose_"
OWNERSRV_QUICK_RUN_CB_PREFIX = "admin_ownersrv_quickrun_"
OWNERSRV_QUICK_ADD_CB_PREFIX = "admin_ownersrv_quickadd_"
OWNERSRV_QUICK_MANAGE_CB_PREFIX = "admin_ownersrv_quickmanage_"
OWNERSRV_QUICK_DEL_CB_PREFIX = "admin_ownersrv_quickdel_"

OWNERSRV_RESIZE_CB_PREFIX = "admin_ownersrv_resize_"
OWNERSRV_TERM_NORMAL_SIZE = (120, 32)
OWNERSRV_TERM_WIDE_SIZE = (220, 50)

OWNERSRV_TERMINAL_DOWNLOAD_CB_PREFIX = "admin_ownersrv_termdl_"

OWNERSRV_TERM_IDLE_TIMEOUT_MINUTES = 15

OWNERSRV_FILES_CHMOD_CB_PREFIX = "admin_osf_chmod_"
OWNERSRV_FILES_CHOWN_CB_PREFIX = "admin_osf_chown_"
OWNERSRV_FILES_EXTRACT_CB_PREFIX = "admin_osf_extract_"
OWNERSRV_FILES_COMPRESS_CB_PREFIX = "admin_osf_compress_"
OWNERSRV_FILES_TAIL_CB_PREFIX = "admin_osf_tail_"
OWNERSRV_FILES_TAILSTOP_CB = "admin_osf_tailstop"
OWNERSRV_TAIL_EDIT_INTERVAL = 1.5
OWNERSRV_TAIL_BODY_CHARS = 3200

OWNERSRV_KILL_PROMPT_CB_PREFIX = "admin_osk_c_"
OWNERSRV_KILL_TERM_CB_PREFIX = "admin_osk_term_"
OWNERSRV_KILL_FORCE_CB_PREFIX = "admin_osk_kill_"

OWNERSRV_SVC_MENU_CB = "admin_ownersrv_svc"
OWNERSRV_SVC_VIEW_CB_PREFIX = "admin_osvc_view_"
OWNERSRV_SVC_ACTION_CB_PREFIX = "admin_osvc_act_"
OWNERSRV_SVC_FILTER_CB_PREFIX = "admin_osvc_filter_"
OWNERSRV_SVC_LOG_CB_PREFIX = "admin_osvc_log_"
OWNERSRV_SVC_LOGSTOP_CB = "admin_osvc_logstop"
OWNERSRV_SVC_REMOVE_PROMPT_CB_PREFIX = "admin_osvc_rm_"
OWNERSRV_SVC_REMOVE_CONFIRM_CB_PREFIX = "admin_osvc_rmyes_"

OWNERSRV_CRON_MENU_CB = "admin_ownersrv_cron"
OWNERSRV_CRON_EDIT_CB = "admin_ownersrv_cron_edit"
OWNERSRV_CRON_BACK_CB = "admin_ownersrv_cron_back"

OWNERSRV_TERM_EDIT_INTERVAL = 1.2
OWNERSRV_TERM_MAX_IDLE_EDIT = 4.0
OWNERSRV_TERM_BODY_CHARS = 3200

CANCEL_BUTTON_TEXT = "✕ Cancel"
ADMIN_MENU_BUTTON_TEXT = "⚙ Admin"
OWNERSRV_MENU_BUTTON_TEXT = "▣ Owner Server"


def is_admin(user_id: int) -> bool:
    return user_id in config.ADMIN_IDS


def _loggroup_text_and_keyboard(status_line: str = None):
    group_id = bot_settings.get_log_group_id()
    bot_name = bot_settings.get_bot_name()
    if group_id:
        text = (
            f"▤ *Log Group*\n\n"
            f"Current log group: `{group_id}`\n\n"
            f"New users, invoices, subscriptions, wallet changes, admin actions, "
            f"subscription expiry and system errors are all sent there, split into topics."
        )
    else:
        text = (
            "▤ *Log Group*\n\n"
            "No log group is set yet - activity logs won't be sent anywhere until "
            "you set one."
        )
    text += f"\n\nBot name: `{bot_name}`" if bot_name else "\n\nBot name: _not set_"
    if status_line:
        text += f"\n\n{status_line}"

    keyboard = [
        [InlineKeyboardButton(
            "↻ Change Log Group" if group_id else "➕ Set Log Group",
            callback_data="admin_loggroup_set",
        )],
        [InlineKeyboardButton(
            "✎ Change Bot Name" if bot_name else "✎ Set Bot Name",
            callback_data="admin_botname_set",
        )],
    ]
    if group_id:
        keyboard.append([InlineKeyboardButton("↻ Refresh", callback_data="admin_loggroup_reload")])
    keyboard.append([InlineKeyboardButton("← Back", callback_data="admin_back_to_main")])
    return text, InlineKeyboardMarkup(keyboard)


async def admin_loggroup_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        await query.edit_message_text("⊘ You do not have admin access.")
        return
    text, reply_markup = _loggroup_text_and_keyboard()
    await query.edit_message_text(text, reply_markup=reply_markup, parse_mode="Markdown")


async def admin_loggroup_reload(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Re-checks that the bot can still reach the configured log group and
    that its topics are in place (without going through the whole
    forward-a-message / retype-the-id flow), then refreshes this screen."""
    query = update.callback_query
    if not is_admin(query.from_user.id):
        await query.answer("⊘ No access.", show_alert=True)
        return
    await query.answer("⏳ Checking...")

    group_id = bot_settings.get_log_group_id()
    if not group_id:
        text, reply_markup = _loggroup_text_and_keyboard()
        await query.edit_message_text(text, reply_markup=reply_markup, parse_mode="Markdown")
        return

    try:
        chat = await context.bot.get_chat(group_id)
        logger_bot.init_logger(group_id)
        ok = await logger_bot.create_all_topics(context.bot)
        title = chat.title or str(group_id)
        if ok:
            status_line = f"✓ Connected to \"{title}\" - topics are ready."
        else:
            status_line = (
                f"⚠ Connected to \"{title}\", but couldn't verify/create topics - "
                f"check the bot's admin rights there (needs 'Manage Topics')."
            )
    except Exception as e:
        status_line = f"✕ Can't reach this chat: {e}"

    text, reply_markup = _loggroup_text_and_keyboard(status_line)
    try:
        await query.edit_message_text(text, reply_markup=reply_markup, parse_mode="Markdown")
    except BadRequest:
        await query.edit_message_text(text, reply_markup=reply_markup, parse_mode=None)


async def admin_loggroup_set_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Entry point for the button-driven log-group setup (replaces the old
    /setloggroup command, which had to be run inside the target group)."""
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        await query.edit_message_text("⊘ You do not have admin access.")
        return ConversationHandler.END

    await _edit_then_prompt_cancel(
        query,
        "▤ Set Log Group\n\n"
        "1) Add the bot to the target group as an admin, with the "
        "'Manage Topics' permission, and make sure Topics (forum mode) is "
        "enabled for the group.\n"
        "2) Then either forward any message from that group to me here, or "
        "send its numeric chat id (e.g. -1001234567890).",
    )
    return ADMIN_LOGGROUP_SET


def _extract_forwarded_chat_id(message):
    """Pulls the source chat id/title out of a forwarded message, supporting
    both the modern forward_origin API and the legacy forward_from_chat."""
    origin = getattr(message, "forward_origin", None)
    if origin is not None:
        chat = getattr(origin, "chat", None)
        if chat is not None:
            return chat.id, getattr(chat, "title", None)
    legacy_chat = getattr(message, "forward_from_chat", None)
    if legacy_chat is not None:
        return legacy_chat.id, getattr(legacy_chat, "title", None)
    return None, None


async def _run_with_spinner(status_msg, label: str, coro):
    """Same minimal spinner used for owner-server long-running actions
    (_osf_run_with_spinner) - a rotating frame + elapsed seconds, edited in
    place every 1.5s - adapted here for a plain Message instead of a
    CallbackQuery."""
    start = time.monotonic()
    task = asyncio.create_task(coro)
    frame = 0
    last_text = None
    while not task.done():
        await asyncio.sleep(_OSF_SPINNER_EDIT_INTERVAL)
        elapsed = int(time.monotonic() - start)
        text = f"{_OSF_SPINNER_FRAMES[frame % len(_OSF_SPINNER_FRAMES)]} {label}  ({elapsed}s)"
        frame += 1
        if text != last_text:
            try:
                await status_msg.edit_text(text)
                last_text = text
            except Exception:
                pass
    return await task


async def _safe_edit_status(status_msg, text: str, parse_mode: str = "Markdown"):
    """Same safe-edit-with-fallback approach as _edit_then_prompt_cancel: try
    the edit, retry without parse_mode, and if the message truly can't be
    edited (e.g. Telegram's transient "message can't be edited"), send a new
    message instead of leaving the admin stuck on the loading text."""
    try:
        await status_msg.edit_text(text, parse_mode=parse_mode)
        return
    except BadRequest as e:
        logger.warning(f"_safe_edit_status: edit with parse_mode failed ({e}); retrying plain")
    try:
        await status_msg.edit_text(text, parse_mode=None)
        return
    except BadRequest as e:
        logger.warning(f"_safe_edit_status: plain edit also failed ({e}); sending a new message instead")
    try:
        await status_msg.chat.send_message(text)
    except Exception as e:
        logger.warning(f"_safe_edit_status: fallback send also failed: {e}")


async def admin_loggroup_set_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    group_id, group_title = _extract_forwarded_chat_id(message)

    if group_id is None:
        text = (message.text or "").strip()
        try:
            group_id = int(text)
        except ValueError:
            group_id = None

    if group_id is None:
        await message.reply_text(
            "✕ Couldn't detect a group. Forward any message from the target "
            "group, or send its numeric chat id (e.g. -1001234567890), or tap Cancel.",
            reply_markup=_cancel_kb(),
        )
        return ADMIN_LOGGROUP_SET

    status_msg = await message.reply_text("⏳ Checking access to that chat...", reply_markup=ReplyKeyboardRemove())

    # Verify the bot can actually reach this chat *before* saving it -
    # otherwise a bad/mistyped id gets persisted and every future log (incl.
    # system-error reports) silently fails with "Chat not found".
    try:
        chat = await context.bot.get_chat(group_id)
    except Exception as e:
        await _safe_edit_status(
            status_msg,
            f"✕ Can't reach chat `{group_id}`: {e}\n\n"
            f"Make sure the bot has been added to that group first, then try again.",
        )
        return ADMIN_LOGGROUP_SET

    if chat.type not in ("group", "supergroup"):
        await _safe_edit_status(
            status_msg,
            f"✕ That chat is a {chat.type}, not a group. Please forward a message from "
            f"a group/supergroup, or send its numeric id.",
        )
        return ADMIN_LOGGROUP_SET

    group_title = group_title or chat.title

    bot_settings.set_log_group_id(group_id)
    logger_bot.init_logger(group_id)
    logger.info(f"Admin {update.effective_user.id} set log group to {group_id}")

    await _safe_edit_status(status_msg, f"{_OSF_SPINNER_FRAMES[0]} Setting up log topics...  (0s)")
    try:
        ok = await _run_with_spinner(
            status_msg, "Setting up log topics...", logger_bot.create_all_topics(context.bot)
        )
    except Exception as e:
        ok = False
        logger.warning(f"admin_loggroup_set_input: create_all_topics failed: {e}")

    title_line = f"\n{group_title}" if group_title else ""
    if ok:
        result_text = f"✓ Log group set to `{group_id}`{title_line}. Topics are ready."
    else:
        result_text = (
            f"⚠ Log group set to `{group_id}`{title_line}, but topic creation failed - "
            f"make sure the bot is an admin there with 'Manage Topics' permission "
            f"and that Topics (forum mode) is enabled for the group."
        )
    await _safe_edit_status(status_msg, result_text)

    menu_text, menu_keyboard = _loggroup_text_and_keyboard()
    await message.reply_text(menu_text, reply_markup=menu_keyboard, parse_mode="Markdown")
    return ConversationHandler.END


async def admin_botname_set_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        await query.edit_message_text("⊘ You do not have admin access.")
        return ConversationHandler.END
    current = bot_settings.get_bot_name()
    current_line = f" (current: {current})" if current else ""
    await _edit_then_prompt_cancel(
        query,
        f"✎ Send the bot name to show in log-group topic titles{current_line}, "
        f"e.g. \"Terminal Bot\" -> \"📢 New Users(Terminal Bot)\".",
    )
    return ADMIN_BOTNAME_SET


async def admin_botname_set_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if not text:
        await update.message.reply_text(
            "✕ Bot name can't be empty. Send a name, or tap Cancel.",
            reply_markup=_cancel_kb(),
        )
        return ADMIN_BOTNAME_SET
    bot_settings.set_bot_name(text)
    logger.info(f"Admin {update.effective_user.id} set bot_name={text}")

    menu_text, menu_keyboard = _loggroup_text_and_keyboard(f"✓ Bot name set to \"{text}\".")
    await update.message.reply_text(menu_text, reply_markup=menu_keyboard, parse_mode="Markdown")
    return ConversationHandler.END


def _backup_text_and_keyboard(status_line: str = None):
    interval = bot_settings.get_backup_interval_days()
    last_at_raw = bot_settings.get_last_backup_at()
    if last_at_raw:
        try:
            from datetime import datetime as _dt
            last_at = _dt.fromisoformat(last_at_raw).strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            last_at = last_at_raw
    else:
        last_at = "never"

    text = (
        f"🗄 *Backup Settings*\n\n"
        f"Periodic backup: every {interval} day(s)\n"
        f"Last backup: {last_at}\n\n"
        f"A backup includes the full database, bot settings, and "
        f"ServerManager data (encrypted server credentials, automation "
        f"rules). It's sent as a file to the 🗄 Backups topic in the log "
        f"group and then removed from the server - Telegram is the only "
        f"place it's kept."
    )
    if not bot_settings.get_log_group_id():
        text += "\n\n⚠ No log group is set yet - set one first (▤ Log Group) or backups have nowhere to go."
    if status_line:
        text += f"\n\n{status_line}"

    keyboard = [
        [InlineKeyboardButton("▶ Backup Now", callback_data="admin_backup_now")],
        [InlineKeyboardButton(f"⏱ Interval: Every {interval}d (tap to change)", callback_data="admin_backup_interval")],
        [InlineKeyboardButton("← Back", callback_data="admin_back_to_main")],
    ]
    return text, InlineKeyboardMarkup(keyboard)


async def admin_backup_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        await query.edit_message_text("⊘ You do not have admin access.")
        return
    text, reply_markup = _backup_text_and_keyboard()
    await query.edit_message_text(text, reply_markup=reply_markup, parse_mode="Markdown")


async def admin_backup_now(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not is_admin(query.from_user.id):
        await query.answer("⊘ You do not have admin access.", show_alert=True)
        return
    await query.answer("⏳ Building backup...")
    try:
        await query.edit_message_text("⏳ Building backup - this can take a moment for larger databases...")
    except BadRequest:
        pass

    result = await backup_manager.run_backup_and_send(context.bot)
    logger.info(f"Admin {query.from_user.id} triggered a manual backup: {result}")

    text, reply_markup = _backup_text_and_keyboard(result)
    await query.edit_message_text(text, reply_markup=reply_markup, parse_mode="Markdown")


async def admin_backup_interval_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        await query.edit_message_text("⊘ You do not have admin access.")
        return ConversationHandler.END
    current = bot_settings.get_backup_interval_days()
    await _edit_then_prompt_cancel(
        query, f"✎ Send the new backup interval in days (current: {current}), e.g. \"3\"."
    )
    return ADMIN_BACKUP_INTERVAL


async def admin_backup_interval_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    try:
        value = int(text)
        if value < 1:
            raise ValueError
    except ValueError:
        await update.message.reply_text(
            "✕ Please send a whole number of days, 1 or more, or tap Cancel.",
            reply_markup=_cancel_kb(),
        )
        return ADMIN_BACKUP_INTERVAL

    backup_manager.reschedule_job(context.job_queue, value)
    logger.info(f"Admin {update.effective_user.id} set backup_interval_days={value}")

    menu_text, menu_keyboard = _backup_text_and_keyboard(f"✓ Backup interval set to every {value} day(s).")
    await update.message.reply_text(menu_text, reply_markup=menu_keyboard, parse_mode="Markdown")
    return ConversationHandler.END


BANNED_MESSAGE = "⊘ You have been banned from using this bot."


async def ban_gate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Registered at group=-1 (before every other handler, same slot as
    sponsor_gate.gate) so a banned user's messages/button taps never reach
    any real handler. Admins are never blocked, even if somehow flagged."""
    user = update.effective_user
    if user is None or is_admin(user.id):
        return

    try:
        banned = get_db().is_banned(user.id)
    except Exception as e:
        logger.warning(f"ban_gate: could not check ban status for {user.id}: {e}")
        return

    if not banned:
        return

    if update.callback_query:
        try:
            await update.callback_query.answer(BANNED_MESSAGE, show_alert=True)
        except BadRequest:
            pass
    elif update.effective_message:
        try:
            await update.effective_message.reply_text(BANNED_MESSAGE, reply_markup=ReplyKeyboardRemove())
        except Exception:
            pass
    raise ApplicationHandlerStop


_get_main_menu_func = None


def set_get_main_menu(func):
    global _get_main_menu_func
    _get_main_menu_func = func


def get_main_menu(user_id=None):
    if _get_main_menu_func:
        return _get_main_menu_func(user_id)
    return ReplyKeyboardMarkup([[]], resize_keyboard=True)


def _cancel_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup([[KeyboardButton(CANCEL_BUTTON_TEXT)]], resize_keyboard=True)


async def _edit_then_prompt_cancel(query, text: str):
    try:
        await query.edit_message_text(text)
    except BadRequest as e:
        logger.warning(f"_edit_then_prompt_cancel: edit failed ({e}); sending a new message instead")
        await query.message.reply_text(text)
    await query.message.reply_text("↓ Tap below to cancel:", reply_markup=_cancel_kb())


async def _finish_with_menu(update: Update, confirm_text: str, menu_text: str, menu_keyboard, parse_mode="Markdown"):
    await update.message.reply_text(confirm_text, reply_markup=ReplyKeyboardRemove())
    try:
        await update.message.reply_text(menu_text, reply_markup=menu_keyboard, parse_mode=parse_mode)
    except BadRequest:
        await update.message.reply_text(menu_text, reply_markup=menu_keyboard, parse_mode=None)


async def admin_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    _close_all_ownersrv_terminals(context)
    fb = context.user_data.get("admin_ownersrv_fb")
    if fb and fb.get("tail_proc") is not None:
        owner_server.tail_stop(fb["tail_proc"])
        fb["tail_proc"] = None
    await update.message.reply_text("✕ Operation cancelled.", reply_markup=get_main_menu(update.effective_user.id))
    return ConversationHandler.END


async def admin_ownersrv_files_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    fb = context.user_data.get("admin_ownersrv_fb")
    if not fb:
        await update.message.reply_text("✕ Operation cancelled.", reply_markup=get_main_menu(update.effective_user.id))
        return ConversationHandler.END

    was_pending = any([
        fb.get("awaiting_upload"),
        fb.get("awaiting_url"),
        fb.get("awaiting_path"),
        fb.get("awaiting_rename_idx") is not None,
        fb.get("awaiting_edit_idx") is not None,
        fb.get("awaiting_chmod_idx") is not None,
        fb.get("awaiting_chown_idx") is not None,
        fb.get("awaiting_mkdir"),
    ])
    fb["awaiting_upload"] = False
    fb["awaiting_url"] = False
    fb["awaiting_path"] = False
    fb["awaiting_rename_idx"] = None
    fb["awaiting_edit_idx"] = None
    fb["awaiting_chmod_idx"] = None
    fb["awaiting_chown_idx"] = None
    fb["awaiting_mkdir"] = False
    if fb.get("tail_proc") is not None:
        owner_server.tail_stop(fb["tail_proc"])
        fb["tail_proc"] = None

    if not was_pending:
        await update.message.reply_text("✕ Operation cancelled.", reply_markup=get_main_menu(update.effective_user.id))
        return ConversationHandler.END

    await update.message.reply_text("✕ Cancelled.", reply_markup=ReplyKeyboardRemove())
    await context.bot.send_message(
        chat_id=fb["chat_id"], text=_osf_text(fb), reply_markup=_osf_keyboard(fb), parse_mode="Markdown",
    )
    return ADMIN_OWNERSRV_FILES


async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.message.reply_text("⊘ You do not have admin access.")
        return

    keyboard = [
        [InlineKeyboardButton("◦ Manage Users", callback_data="admin_users_menu")],
        [InlineKeyboardButton("» Sponsor Channel Settings", callback_data="admin_channel_settings")],
        [InlineKeyboardButton("▨ Manage Plans", callback_data="admin_plans_menu")],
        [InlineKeyboardButton("▭ Payment Settings", callback_data="admin_payment_settings")],
        [InlineKeyboardButton("✚ Monitoring Settings", callback_data="admin_monitoring_settings")],
        [InlineKeyboardButton("▣ Owner Server (Host)", callback_data="admin_ownersrv_menu")],
        [InlineKeyboardButton("▤ Log Group", callback_data="admin_loggroup_menu")],
        [InlineKeyboardButton("🗄 Backup", callback_data="admin_backup_menu")],
    ]
    await update.message.reply_text(
        "⚙ Admin Panel\n\nPlease select an option:",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def admin_ownersrv_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.message.reply_text("⊘ You do not have admin access.")
        return
    try:
        snap = owner_server.snapshot()
        text = owner_server.format_snapshot_text(snap)
    except Exception as e:
        logger.error(f"owner server snapshot failed: {e}")
        text = f"✕ Could not read host stats: {e}"
    await update.message.reply_text(text, reply_markup=_owner_server_keyboard(), parse_mode="Markdown")


def _channel_settings_text_and_keyboard():
    channels = bot_settings.get_sponsor_channels()
    required = bot_settings.is_membership_required()
    status_text = "✓ Enabled (mandatory)" if required else "✕ Disabled (not required)"

    if channels:
        channel_lines = "\n".join(f"• {ch['title']} ({ch['id']})" for ch in channels)
    else:
        channel_lines = "(none configured)"

    text = (
        f"» Sponsor Channel Settings\n\n"
        f"» Current channel(s):\n{channel_lines}\n\n"
        f"✦ Membership check status: {status_text}\n\n"
        f"Use the buttons below to add or remove a channel, or toggle the requirement."
    )

    keyboard = [
        [InlineKeyboardButton("+ Add Channel", callback_data="admin_channel_add")],
    ]
    if channels:
        keyboard.append([InlineKeyboardButton("- Remove Channel", callback_data="admin_channel_remove")])
    keyboard.append([InlineKeyboardButton(
        "◯ Disable Membership Check" if required else "◉ Enable Membership Check",
        callback_data="admin_channel_toggle",
    )])
    keyboard.append([InlineKeyboardButton("← Back", callback_data="admin_back_to_main")])
    return text, InlineKeyboardMarkup(keyboard)


async def admin_channel_settings_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if not is_admin(query.from_user.id):
        await query.edit_message_text("⊘ You do not have admin access.")
        return

    text, reply_markup = _channel_settings_text_and_keyboard()
    await query.edit_message_text(text, reply_markup=reply_markup)


async def admin_back_to_main(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if not is_admin(query.from_user.id):
        await query.edit_message_text("⊘ You do not have admin access.")
        return ConversationHandler.END

    keyboard = [
        [InlineKeyboardButton("◦ Manage Users", callback_data="admin_users_menu")],
        [InlineKeyboardButton("» Sponsor Channel Settings", callback_data="admin_channel_settings")],
        [InlineKeyboardButton("▨ Manage Plans", callback_data="admin_plans_menu")],
        [InlineKeyboardButton("▭ Payment Settings", callback_data="admin_payment_settings")],
        [InlineKeyboardButton("✚ Monitoring Settings", callback_data="admin_monitoring_settings")],
        [InlineKeyboardButton("▣ Owner Server (Host)", callback_data="admin_ownersrv_menu")],
        [InlineKeyboardButton("▤ Log Group", callback_data="admin_loggroup_menu")],
        [InlineKeyboardButton("🗄 Backup", callback_data="admin_backup_menu")],
    ]
    await query.edit_message_text(
        "⚙ Admin Panel\n\nPlease select an option:",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )
    return ConversationHandler.END


async def admin_channel_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query

    if not is_admin(query.from_user.id):
        await query.answer("⊘ No access.", show_alert=True)
        return

    new_value = not bot_settings.is_membership_required()
    bot_settings.set_membership_required(new_value)
    await query.answer("✓ Status updated.")

    logger.info(
        f"Admin {query.from_user.id} set membership_required={new_value}"
    )

    text, reply_markup = _channel_settings_text_and_keyboard()
    await query.edit_message_text(text, reply_markup=reply_markup)


async def admin_channel_add_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if not is_admin(query.from_user.id):
        await query.edit_message_text("⊘ You do not have admin access.")
        return ConversationHandler.END

    await _edit_then_prompt_cancel(
        query,
        "» Add Sponsor Channel\n\n"
        "Please send the channel's numeric id (e.g. -1001234567890) or its "
        "@username.\n\n"
        "⚠ Note: the bot must be an admin in the target channel to check "
        "membership.",
    )
    return ADMIN_CHANNEL_ADD_ID


async def admin_channel_add_id_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if not text:
        await update.message.reply_text(
            "✕ Invalid value. Please send the channel id/username again, or tap Cancel.",
            reply_markup=_cancel_kb(),
        )
        return ADMIN_CHANNEL_ADD_ID

    context.user_data["admin_new_channel_id"] = text
    await update.message.reply_text(
        "◆ Now send a display title for this channel (shown to users in the "
        "join prompt), or send \"-\" to just use the id/username.",
        reply_markup=_cancel_kb(),
    )
    return ADMIN_CHANNEL_ADD_TITLE


async def admin_channel_add_title_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    title = "" if text == "-" else text
    context.user_data["admin_new_channel_title"] = title

    await update.message.reply_text(
        "» Optionally send an invite link (e.g. https://t.me/mychannel) to "
        "show on the \"Join\" button, or send \"-\" to skip (falls back to "
        "the @username / title).",
        reply_markup=_cancel_kb(),
    )
    return ADMIN_CHANNEL_ADD_LINK


async def admin_channel_add_link_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    link = "" if text == "-" else text

    channel_id = context.user_data.pop("admin_new_channel_id", "")
    title = context.user_data.pop("admin_new_channel_title", "")

    channel = bot_settings.add_sponsor_channel(channel_id, title, link)
    logger.info(f"Admin {update.effective_user.id} added sponsor channel: {channel}")

    menu_text, menu_keyboard = _channel_settings_text_and_keyboard()
    await _finish_with_menu(
        update,
        f"✓ Channel added!\n\n"
        f"» {channel['title']} ({channel['id']})\n\n"
        f"⚠ Make sure the bot is an admin in this channel, otherwise "
        f"membership checks will fail.",
        menu_text, menu_keyboard, parse_mode=None,
    )
    return ConversationHandler.END


async def admin_channel_remove_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if not is_admin(query.from_user.id):
        await query.edit_message_text("⊘ You do not have admin access.")
        return

    channels = bot_settings.get_sponsor_channels()
    if not channels:
        await query.edit_message_text(
            "No channels configured.",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("← Back", callback_data="admin_channel_settings")]]
            ),
        )
        return

    keyboard = [
        [InlineKeyboardButton(f"✕ {ch['title']} ({ch['id']})", callback_data=f"admin_channel_remove_{i}")]
        for i, ch in enumerate(channels)
    ]
    keyboard.append([InlineKeyboardButton("← Back", callback_data="admin_channel_settings")])
    await query.edit_message_text(
        "- Remove Channel\n\nTap a channel to remove it:",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def admin_channel_remove_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query

    if not is_admin(query.from_user.id):
        await query.answer("⊘ No access.", show_alert=True)
        return

    index = int(query.data.rsplit("_", 1)[-1])
    channels = bot_settings.get_sponsor_channels()
    removed = channels[index] if 0 <= index < len(channels) else None

    if bot_settings.remove_sponsor_channel(index):
        await query.answer("✓ Channel removed.")
        logger.info(f"Admin {query.from_user.id} removed sponsor channel: {removed}")
    else:
        await query.answer("✕ Could not find that channel.", show_alert=True)

    text, reply_markup = _channel_settings_text_and_keyboard()
    await query.edit_message_text(text, reply_markup=reply_markup)


def _user_summary_line(u: dict) -> str:
    who = u.get("username")
    who = f"@{who}" if who else (u.get("first_name") or str(u["user_id"]))
    banned = " ⊘" if u.get("is_banned") else ""
    return f"• {who} (id: {u['user_id']}){banned}"


def _users_list_keyboard(users: list, offset: int, total: int, search: str = None):
    rows = [
        [InlineKeyboardButton(_user_summary_line(u), callback_data=f"admin_user_view_{u['user_id']}")]
        for u in users
    ]
    nav = []
    if offset > 0:
        nav.append(InlineKeyboardButton("← Prev", callback_data=f"admin_users_page_{max(0, offset - USERS_PAGE_SIZE)}"))
    if offset + USERS_PAGE_SIZE < total:
        nav.append(InlineKeyboardButton("→ Next", callback_data=f"admin_users_page_{offset + USERS_PAGE_SIZE}"))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton("◎ Search by id / username", callback_data="admin_user_search_start")])
    rows.append([InlineKeyboardButton("← Back", callback_data="admin_back_to_main")])
    return InlineKeyboardMarkup(rows)


async def admin_users_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        await query.edit_message_text("⊘ You do not have admin access.")
        return

    db = get_db()
    total = db.count_users()
    users = db.list_users(limit=USERS_PAGE_SIZE, offset=0)
    text = f"◦ Users\n\nTotal registered: {total}\n\nTap a user to view details, or search below."
    if not users:
        text = "◦ Users\n\nNobody has started the bot yet."
    await query.edit_message_text(text, reply_markup=_users_list_keyboard(users, 0, total))


async def admin_users_page(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        await query.edit_message_text("⊘ You do not have admin access.")
        return

    offset = int(query.data.replace("admin_users_page_", ""))
    search = context.user_data.get("admin_user_search")
    db = get_db()
    total = db.count_users()
    users = db.list_users(limit=USERS_PAGE_SIZE, offset=offset, search=search)
    text = f"◦ Users\n\nTotal registered: {total}\n\nTap a user to view details, or search below."
    await query.edit_message_text(text, reply_markup=_users_list_keyboard(users, offset, total, search))


async def admin_user_search_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        await query.edit_message_text("⊘ You do not have admin access.")
        return ConversationHandler.END

    context.user_data.pop("admin_user_search", None)
    await _edit_then_prompt_cancel(query, "◎ Send a numeric user id, or a @username to search for:")
    return ADMIN_USER_SEARCH


async def admin_user_search_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if not text:
        await update.message.reply_text("✕ Please send a user id or @username, or tap Cancel.", reply_markup=_cancel_kb())
        return ADMIN_USER_SEARCH

    context.user_data["admin_user_search"] = text
    db = get_db()
    total_all = db.count_users()
    users = db.list_users(limit=USERS_PAGE_SIZE, offset=0, search=text)
    if not users:
        context.user_data.pop("admin_user_search", None)
        all_users = db.list_users(limit=USERS_PAGE_SIZE, offset=0)
        await _finish_with_menu(
            update,
            f"✕ No users matched \"{text}\".",
            f"◦ Users\n\nTotal registered: {total_all}\n\nTap a user to view details, or search below.",
            _users_list_keyboard(all_users, 0, total_all),
            parse_mode=None,
        )
        return ConversationHandler.END

    await update.message.reply_text(
        f"◦ Results for \"{text}\":",
        reply_markup=_users_list_keyboard(users, 0, total_all, text),
    )
    return ConversationHandler.END


def _user_detail_text(u: dict) -> str:
    who = f"@{u['username']}" if u.get("username") else (u.get("first_name") or "(no name)")
    balance = subscription.get_balance(u["user_id"])
    lines = [
        f"◦ {who}",
        f"# ID: {u['user_id']}",
        f"▤ Joined: {u['created_at']}",
        f"◉ Status: {'⊘ Banned' if u.get('is_banned') else '✓ Active'}",
        f"◈ Wallet balance: {balance:,}",
    ]
    if subscription.is_active(u["user_id"]):
        sub = subscription.get_subscription(u["user_id"])
        lines.append(f"▨ Plan: {sub['plan_name']} ({subscription.days_remaining(u['user_id'])}d left)")
    else:
        lines.append("▨ Plan: none active")
    return "\n".join(lines)


ADMIN_USER_DELETE_CONFIRM_CB_PREFIX = "admin_user_delc_"
ADMIN_USER_DELETE_EXECUTE_CB_PREFIX = "admin_user_delok_"


def _user_detail_keyboard(u: dict):
    ban_btn = (
        InlineKeyboardButton("◉ Unban", callback_data=f"admin_user_unban_{u['user_id']}")
        if u.get("is_banned")
        else InlineKeyboardButton("⊘ Ban", callback_data=f"admin_user_ban_{u['user_id']}")
    )
    return InlineKeyboardMarkup([
        [ban_btn],
        [InlineKeyboardButton("◈ Adjust balance", callback_data=f"admin_user_balance_{u['user_id']}")],
        [InlineKeyboardButton("🗑 Delete user", callback_data=f"{ADMIN_USER_DELETE_CONFIRM_CB_PREFIX}{u['user_id']}")],
        [InlineKeyboardButton("← Back to list", callback_data="admin_users_menu")],
    ])


async def admin_user_view(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        await query.edit_message_text("⊘ You do not have admin access.")
        return

    user_id = int(query.data.replace("admin_user_view_", ""))
    u = get_db().get_user(user_id)
    if not u:
        await query.answer("✕ User not found.", show_alert=True)
        return
    await query.edit_message_text(_user_detail_text(u), reply_markup=_user_detail_keyboard(u))


async def admin_user_ban_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not is_admin(query.from_user.id):
        await query.answer("⊘ No access.", show_alert=True)
        return

    banned = query.data.startswith("admin_user_ban_")
    prefix = "admin_user_ban_" if banned else "admin_user_unban_"
    user_id = int(query.data.replace(prefix, ""))

    db = get_db()
    db.set_banned(user_id, banned)
    u = db.get_user(user_id)
    await query.answer("⊘ User banned." if banned else "◉ User unbanned.")
    await query.edit_message_text(_user_detail_text(u), reply_markup=_user_detail_keyboard(u))
    logger.info(f"Admin {query.from_user.id} {'banned' if banned else 'unbanned'} user {user_id}")


async def admin_user_delete_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not is_admin(query.from_user.id):
        await query.answer("⊘ No access.", show_alert=True)
        return

    user_id = int(query.data.replace(ADMIN_USER_DELETE_CONFIRM_CB_PREFIX, ""))
    u = get_db().get_user(user_id)
    if not u:
        await query.answer("✕ User not found.", show_alert=True)
        return

    await query.answer()
    who = f"@{u['username']}" if u.get("username") else (u.get("first_name") or str(user_id))
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("✓ Yes, delete permanently", callback_data=f"{ADMIN_USER_DELETE_EXECUTE_CB_PREFIX}{user_id}")],
        [InlineKeyboardButton("✕ Cancel", callback_data=f"admin_user_view_{user_id}")],
    ])
    await query.edit_message_text(
        f"⚠ Are you sure you want to *permanently delete* {who} (id: {user_id})?\n\n"
        "This removes their account, wallet balance, subscription, transaction history, "
        "registered servers, and automations (tags, quick commands, scheduled jobs). "
        "This cannot be undone.",
        reply_markup=keyboard,
        parse_mode="Markdown",
    )


async def admin_user_delete_execute(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not is_admin(query.from_user.id):
        await query.answer("⊘ No access.", show_alert=True)
        return

    user_id = int(query.data.replace(ADMIN_USER_DELETE_EXECUTE_CB_PREFIX, ""))
    db = get_db()
    u = db.get_user(user_id)
    if not u:
        await query.answer("✕ User not found - may already be deleted.", show_alert=True)
        await query.edit_message_text("✕ That user no longer exists.")
        return

    who = f"@{u['username']}" if u.get("username") else (u.get("first_name") or str(user_id))

    # Best-effort heads-up before we wipe their account; failure to deliver
    # (blocked bot, deactivated account, etc.) must not stop the deletion.
    try:
        await context.bot.send_message(
            chat_id=user_id,
            text="⊘ Your account on this bot has been permanently deleted by an admin.",
        )
    except Exception as e:
        logger.warning(f"Could not notify user {user_id} of account deletion: {e}")

    svm_automation.delete_user_data(context.job_queue, user_id)
    svm_settings.delete_user_data(user_id)
    deleted = db.delete_user(user_id)

    await query.answer("🗑 User deleted." if deleted else "✕ Delete failed.", show_alert=True)
    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("← Back to list", callback_data="admin_users_menu")]])
    if deleted:
        await query.edit_message_text(f"🗑 {who} (id: {user_id}) has been permanently deleted.", reply_markup=keyboard)
        logger.info(f"Admin {query.from_user.id} permanently deleted user {user_id}")
    else:
        await query.edit_message_text(f"✕ Could not delete {who} (id: {user_id}). Check the logs.", reply_markup=keyboard)


async def admin_user_balance_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        await query.edit_message_text("⊘ You do not have admin access.")
        return ConversationHandler.END

    user_id = int(query.data.replace("admin_user_balance_", ""))
    context.user_data["admin_balance_target"] = user_id
    await _edit_then_prompt_cancel(
        query,
        "◈ Send the amount to add to this user's wallet.\n"
        "Use a negative number to deduct (e.g. -5000).",
    )
    return ADMIN_USER_BALANCE_ADJUST


async def admin_user_balance_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip().replace(",", "")
    try:
        amount = int(text)
    except ValueError:
        await update.message.reply_text("✕ Please send a whole number (e.g. 5000 or -2000), or tap Cancel.", reply_markup=_cancel_kb())
        return ADMIN_USER_BALANCE_ADJUST

    user_id = context.user_data.pop("admin_balance_target", None)
    if not user_id:
        await update.message.reply_text("✕ Something went wrong, please try again.", reply_markup=get_main_menu(update.effective_user.id))
        return ConversationHandler.END

    new_balance = subscription.update_balance(user_id, amount)
    subscription.add_transaction(user_id, amount, "admin_adjustment", f"Adjusted by admin {update.effective_user.id}")

    await update.message.reply_text(
        f"✓ Done.\n◈ New balance for {user_id}: {new_balance:,}", reply_markup=get_main_menu(update.effective_user.id)
    )
    logger.info(f"Admin {update.effective_user.id} adjusted balance of {user_id} by {amount}")

    try:
        await context.bot.send_message(
            chat_id=user_id,
            text=f"◈ Your wallet balance was adjusted by an admin: {amount:+,}\nNew balance: {new_balance:,}",
        )
    except Exception as e:
        logger.warning(f"Could not notify user {user_id} of balance adjustment: {e}")

    return ConversationHandler.END


def _plans_menu_text_and_keyboard():
    all_plans = subscription.get_all_plans(active_only=False)
    if all_plans:
        default_id = None
        enabled = [(pid, p) for pid, p in all_plans.items() if p.get("enabled", True)]
        if enabled:
            default_id = min(enabled, key=lambda item: (item[1]["price"], item[1].get("plan_order", 0)))[0]

        blocks = []
        for pid, p in all_plans.items():
            status = "◉ Active" if p.get("enabled", True) else "◯ Disabled"
            free_flag = "  ◈ default for new users" if pid == default_id else ""
            price_txt = "Free" if p["price"] == 0 else f"{p['price']:,} / {p['days']}d"
            sftp_flag = "✓ SFTP" if p.get("sftp_enabled", True) else "⚿ No SFTP"
            adv_flag = "⚡ Advanced" if p.get("advanced_tools_enabled", False) else "⚡ No Advanced"
            timeout = p.get("session_timeout_minutes")
            timeout_txt = f"⏱ {timeout}m session" if timeout else "⏱ Unlimited session"
            max_auto = p.get("max_automations")
            max_auto_txt = f"⚙ {max_auto} automations" if max_auto is not None else "⚙ Unlimited automations"
            blocks.append(
                f"*{p['name']}*{free_flag}\n"
                f"{status}  ·  ◈ {price_txt}\n"
                f"▣ {p['max_servers']} servers  ·  ▤ {p['max_tabs']} tabs  ·  {sftp_flag}  ·  {adv_flag}\n"
                f"{timeout_txt}  ·  {max_auto_txt}"
            )
        plan_lines = "\n\n".join(blocks)
    else:
        plan_lines = "(no plans yet)"

    text = (
        f"▨ *Plans*\n\n{plan_lines}\n\n"
        f"◈ New users are automatically granted the cheapest active plan - no need to pick one.\n"
        f"Tap a plan below to toggle it on/off, edit it, or delete it."
    )

    keyboard = [[InlineKeyboardButton("+ Add Plan", callback_data="admin_plan_add")]]
    for pid, p in all_plans.items():
        status = "◉" if p.get("enabled", True) else "◯"
        keyboard.append([
            InlineKeyboardButton(f"{status} {p['name']}", callback_data=f"admin_plan_toggle_{pid}"),
            InlineKeyboardButton("✎", callback_data=f"admin_plan_edit_{pid}"),
            InlineKeyboardButton("✕", callback_data=f"admin_plan_delete_{pid}"),
        ])
    keyboard.append([InlineKeyboardButton("← Back", callback_data="admin_back_to_main")])
    return text, InlineKeyboardMarkup(keyboard)


async def admin_plans_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        await query.edit_message_text("⊘ You do not have admin access.")
        return
    text, reply_markup = _plans_menu_text_and_keyboard()
    await query.edit_message_text(text, reply_markup=reply_markup, parse_mode="Markdown")


async def admin_plan_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not is_admin(query.from_user.id):
        await query.answer("⊘ No access.", show_alert=True)
        return
    plan_id = query.data.replace("admin_plan_toggle_", "")
    if subscription.toggle_plan(plan_id):
        await query.answer("✓ Updated.")
    else:
        await query.answer("✕ Plan not found.", show_alert=True)
    text, reply_markup = _plans_menu_text_and_keyboard()
    await query.edit_message_text(text, reply_markup=reply_markup, parse_mode="Markdown")


async def admin_plan_delete(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not is_admin(query.from_user.id):
        await query.answer("⊘ No access.", show_alert=True)
        return
    plan_id = query.data.replace("admin_plan_delete_", "")
    if subscription.delete_plan(plan_id):
        await query.answer("✕ Deleted.")
        logger.info(f"Admin {query.from_user.id} deleted plan {plan_id}")
    else:
        await query.answer("✕ Plan not found.", show_alert=True)
    text, reply_markup = _plans_menu_text_and_keyboard()
    await query.edit_message_text(text, reply_markup=reply_markup, parse_mode="Markdown")


_PLAN_EDIT_FIELD_PROMPTS = {
    "name": "◆ Send the new name:",
    "description": "✎ Send the new description, or \"-\" for none:",
    "price": "◈ Send the new price (a plain number):",
    "days": "⏳ Send the new duration in days:",
    "max_servers": "▣ Send the new max number of servers:",
    "max_tabs": "▤ Send the new max concurrent terminal tabs:",
    "session_timeout_minutes": "⏱ Send the new max SSH session length in minutes (send 0 for unlimited):",
    "max_automations": "⚙ Send the new max number of automation jobs (send 0 for unlimited):",
}


def _plan_edit_text_and_keyboard(plan_id: str):
    plan = subscription.get_plan(plan_id)
    if not plan:
        return None, None

    sftp_flag = "✓ Included" if plan.get("sftp_enabled", True) else "⚿ Not included"
    adv_flag = "✓ Included" if plan.get("advanced_tools_enabled", False) else "⚿ Not included"
    timeout = plan.get("session_timeout_minutes")
    timeout_txt = f"{timeout}m" if timeout else "Unlimited"
    max_auto = plan.get("max_automations")
    max_auto_txt = str(max_auto) if max_auto is not None else "Unlimited"

    text = (
        f"✎ Edit Plan: {plan['name']}\n\n"
        f"◆ Name: {plan['name']}\n"
        f"✎ Description: {plan.get('description') or '(none)'}\n"
        f"◈ Price: {plan['price']:,}\n"
        f"⏳ Days: {plan['days']}\n"
        f"▣ Max servers: {plan['max_servers']}\n"
        f"▤ Max tabs: {plan['max_tabs']}\n"
        f"▥ SFTP (+ terminal): {sftp_flag}\n"
        f"⚡ Advanced tools (SSH): {adv_flag}\n"
        f"⏱ Session timeout: {timeout_txt}\n"
        f"⚙ Max automations: {max_auto_txt}\n\n"
        f"Tap a field below to change it."
    )

    keyboard = [
        [
            InlineKeyboardButton("◆ Name", callback_data=f"admin_plan_ef_{plan_id}|name"),
            InlineKeyboardButton("✎ Description", callback_data=f"admin_plan_ef_{plan_id}|description"),
        ],
        [
            InlineKeyboardButton("◈ Price", callback_data=f"admin_plan_ef_{plan_id}|price"),
            InlineKeyboardButton("⏳ Days", callback_data=f"admin_plan_ef_{plan_id}|days"),
        ],
        [
            InlineKeyboardButton("▣ Max servers", callback_data=f"admin_plan_ef_{plan_id}|max_servers"),
            InlineKeyboardButton("▤ Max tabs", callback_data=f"admin_plan_ef_{plan_id}|max_tabs"),
        ],
        [InlineKeyboardButton(
            f"▥ SFTP + terminal: "
            f"{'✓ on' if plan.get('sftp_enabled', True) else '⚿ off'} (tap to toggle)",
            callback_data=f"admin_plan_ef_{plan_id}|sftp_toggle",
        )],
        [InlineKeyboardButton(
            f"⚡ Advanced tools: "
            f"{'✓ on' if plan.get('advanced_tools_enabled', False) else '⚿ off'} (tap to toggle)",
            callback_data=f"admin_plan_ef_{plan_id}|advtools_toggle",
        )],
        [
            InlineKeyboardButton("⏱ Session timeout", callback_data=f"admin_plan_ef_{plan_id}|session_timeout_minutes"),
            InlineKeyboardButton("⚙ Max automations", callback_data=f"admin_plan_ef_{plan_id}|max_automations"),
        ],
        [InlineKeyboardButton("← Back to Plans", callback_data="admin_plans_menu")],
    ]
    return text, InlineKeyboardMarkup(keyboard)


async def admin_plan_edit_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        await query.edit_message_text("⊘ You do not have admin access.")
        return

    plan_id = query.data.replace("admin_plan_edit_", "")
    text, reply_markup = _plan_edit_text_and_keyboard(plan_id)
    if text is None:
        await query.answer("✕ Plan not found.", show_alert=True)
        return
    await query.edit_message_text(text, reply_markup=reply_markup)


async def admin_plan_edit_field_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        await query.edit_message_text("⊘ You do not have admin access.")
        return ConversationHandler.END

    plan_id, _, field = query.data.replace("admin_plan_ef_", "").partition("|")
    plan = subscription.get_plan(plan_id)
    if not plan:
        await query.answer("✕ Plan not found.", show_alert=True)
        return ConversationHandler.END

    if field == "sftp_toggle":
        subscription.update_plan(plan_id, sftp_enabled=not plan.get("sftp_enabled", True))
        logger.info(f"Admin {query.from_user.id} toggled sftp_enabled for plan {plan_id}")
        text, reply_markup = _plan_edit_text_and_keyboard(plan_id)
        await query.edit_message_text(text, reply_markup=reply_markup)
        return ConversationHandler.END

    if field == "advtools_toggle":
        subscription.update_plan(plan_id, advanced_tools_enabled=not plan.get("advanced_tools_enabled", False))
        logger.info(f"Admin {query.from_user.id} toggled advanced_tools_enabled for plan {plan_id}")
        text, reply_markup = _plan_edit_text_and_keyboard(plan_id)
        await query.edit_message_text(text, reply_markup=reply_markup)
        return ConversationHandler.END

    prompt = _PLAN_EDIT_FIELD_PROMPTS.get(field)
    if not prompt:
        await query.answer("✕ Unknown field.", show_alert=True)
        return ConversationHandler.END

    context.user_data["admin_edit_plan"] = {"plan_id": plan_id, "field": field}
    await _edit_then_prompt_cancel(query, prompt)
    return ADMIN_PLAN_EDIT_VALUE


async def admin_plan_edit_value_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    target = context.user_data.get("admin_edit_plan")
    if not target:
        await update.message.reply_text("✕ Something went wrong, please try again.", reply_markup=get_main_menu(update.effective_user.id))
        return ConversationHandler.END

    plan_id, field = target["plan_id"], target["field"]
    text = update.message.text.strip()
    kwargs = {}

    if field == "name":
        if not text:
            await update.message.reply_text("✕ Please send a name, or tap Cancel.", reply_markup=_cancel_kb())
            return ADMIN_PLAN_EDIT_VALUE
        kwargs["name"] = text
    elif field == "description":
        kwargs["description"] = "" if text == "-" else text
    elif field == "price":
        clean = text.replace(",", "")
        if not clean.isdigit():
            await update.message.reply_text(
                "✕ Please send a valid non-negative number, or tap Cancel.", reply_markup=_cancel_kb()
            )
            return ADMIN_PLAN_EDIT_VALUE
        kwargs["price"] = int(clean)
    elif field == "days":
        if not text.isdigit() or int(text) <= 0:
            await update.message.reply_text(
                "✕ Please send a valid positive number of days, or tap Cancel.", reply_markup=_cancel_kb()
            )
            return ADMIN_PLAN_EDIT_VALUE
        kwargs["days"] = int(text)
    elif field == "max_servers":
        if not text.isdigit() or int(text) <= 0:
            await update.message.reply_text(
                "✕ Please send a valid positive number, or tap Cancel.", reply_markup=_cancel_kb()
            )
            return ADMIN_PLAN_EDIT_VALUE
        kwargs["max_servers"] = int(text)
    elif field == "max_tabs":
        if not text.isdigit() or int(text) <= 0:
            await update.message.reply_text(
                "✕ Please send a valid positive number, or tap Cancel.", reply_markup=_cancel_kb()
            )
            return ADMIN_PLAN_EDIT_VALUE
        kwargs["max_tabs"] = int(text)
    elif field == "session_timeout_minutes":
        if not text.isdigit():
            await update.message.reply_text(
                "✕ Please send a whole number (0 for unlimited), or tap Cancel.", reply_markup=_cancel_kb()
            )
            return ADMIN_PLAN_EDIT_VALUE
        minutes = int(text)
        kwargs["session_timeout_minutes"] = minutes if minutes > 0 else None
    elif field == "max_automations":
        if not text.isdigit():
            await update.message.reply_text(
                "✕ Please send a whole number (0 for unlimited), or tap Cancel.", reply_markup=_cancel_kb()
            )
            return ADMIN_PLAN_EDIT_VALUE
        count = int(text)
        kwargs["max_automations"] = count if count > 0 else None
    else:
        context.user_data.pop("admin_edit_plan", None)
        await update.message.reply_text("✕ Unknown field.", reply_markup=get_main_menu(update.effective_user.id))
        return ConversationHandler.END

    context.user_data.pop("admin_edit_plan", None)
    ok = subscription.update_plan(plan_id, **kwargs)
    logger.info(f"Admin {update.effective_user.id} edited plan {plan_id}: {kwargs} (ok={ok})")

    if not ok:
        await update.message.reply_text("✕ Could not update plan (it may have been deleted).", reply_markup=get_main_menu(update.effective_user.id))
        return ConversationHandler.END

    text2, reply_markup2 = _plan_edit_text_and_keyboard(plan_id)
    if text2 is None:
        await update.message.reply_text("✓ Plan updated!", reply_markup=get_main_menu(update.effective_user.id))
        return ConversationHandler.END

    await update.message.reply_text(f"✓ Plan updated!\n\n{text2}", reply_markup=reply_markup2)
    return ConversationHandler.END


async def admin_plan_add_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        await query.edit_message_text("⊘ You do not have admin access.")
        return ConversationHandler.END
    context.user_data["admin_new_plan"] = {}
    await _edit_then_prompt_cancel(query, "▨ Add Plan\n\nSend the plan's name:")
    return ADMIN_PLAN_ADD_NAME


async def admin_plan_add_name_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if not text:
        await update.message.reply_text("✕ Please send a name, or tap Cancel.", reply_markup=_cancel_kb())
        return ADMIN_PLAN_ADD_NAME
    context.user_data["admin_new_plan"]["name"] = text
    await update.message.reply_text("◈ Send the price (a plain number):", reply_markup=_cancel_kb())
    return ADMIN_PLAN_ADD_PRICE


async def admin_plan_add_price_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip().replace(",", "")
    if not text.isdigit():
        await update.message.reply_text("✕ Please send a valid positive number, or tap Cancel.", reply_markup=_cancel_kb())
        return ADMIN_PLAN_ADD_PRICE
    context.user_data["admin_new_plan"]["price"] = int(text)
    await update.message.reply_text("⏳ Send the duration in days:", reply_markup=_cancel_kb())
    return ADMIN_PLAN_ADD_DAYS


async def admin_plan_add_days_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if not text.isdigit() or int(text) <= 0:
        await update.message.reply_text("✕ Please send a valid number of days, or tap Cancel.", reply_markup=_cancel_kb())
        return ADMIN_PLAN_ADD_DAYS
    context.user_data["admin_new_plan"]["days"] = int(text)
    await update.message.reply_text("▣ Max number of servers allowed on this plan:", reply_markup=_cancel_kb())
    return ADMIN_PLAN_ADD_MAXSERVERS


async def admin_plan_add_maxservers_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if not text.isdigit() or int(text) <= 0:
        await update.message.reply_text("✕ Please send a valid positive number, or tap Cancel.", reply_markup=_cancel_kb())
        return ADMIN_PLAN_ADD_MAXSERVERS
    context.user_data["admin_new_plan"]["max_servers"] = int(text)
    await update.message.reply_text("▤ Max concurrent terminal tabs on this plan:", reply_markup=_cancel_kb())
    return ADMIN_PLAN_ADD_MAXTABS


async def admin_plan_add_maxtabs_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if not text.isdigit() or int(text) <= 0:
        await update.message.reply_text("✕ Please send a valid positive number, or tap Cancel.", reply_markup=_cancel_kb())
        return ADMIN_PLAN_ADD_MAXTABS
    context.user_data["admin_new_plan"]["max_tabs"] = int(text)
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("✓ Yes", callback_data="admin_plan_add_sftp_yes")],
        [InlineKeyboardButton("⚿ No", callback_data="admin_plan_add_sftp_no")],
    ])
    await update.message.reply_text(
        "▥ Include SFTP (usable alongside terminal)?",
        reply_markup=keyboard,
    )
    return ADMIN_PLAN_ADD_SFTP


async def admin_plan_add_sftp_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data["admin_new_plan"]["sftp_enabled"] = query.data == "admin_plan_add_sftp_yes"
    await _edit_then_prompt_cancel(
        query, "⏱ Max SSH session length in minutes (send 0 for unlimited):"
    )
    return ADMIN_PLAN_ADD_TIMEOUT


async def admin_plan_add_timeout_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if not text.isdigit():
        await update.message.reply_text("✕ Please send a whole number (0 for unlimited), or tap Cancel.", reply_markup=_cancel_kb())
        return ADMIN_PLAN_ADD_TIMEOUT
    minutes = int(text)
    context.user_data["admin_new_plan"]["session_timeout_minutes"] = minutes if minutes > 0 else None
    await update.message.reply_text(
        "⚙ Max number of automation (scheduled) jobs on this plan (send 0 for unlimited):",
        reply_markup=_cancel_kb(),
    )
    return ADMIN_PLAN_ADD_MAXAUTO


async def admin_plan_add_maxauto_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if not text.isdigit():
        await update.message.reply_text("✕ Please send a whole number (0 for unlimited), or tap Cancel.", reply_markup=_cancel_kb())
        return ADMIN_PLAN_ADD_MAXAUTO
    count = int(text)
    context.user_data["admin_new_plan"]["max_automations"] = count if count > 0 else None
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("✓ Yes", callback_data="admin_plan_add_advtools_yes")],
        [InlineKeyboardButton("⚿ No", callback_data="admin_plan_add_advtools_no")],
    ])
    await update.message.reply_text(
        "⚡ Include Advanced Tools (remote process manager, systemd services, crontab editor, log tail)?",
        reply_markup=keyboard,
    )
    return ADMIN_PLAN_ADD_ADVTOOLS


async def admin_plan_add_advtools_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data["admin_new_plan"]["advanced_tools_enabled"] = query.data == "admin_plan_add_advtools_yes"
    await _edit_then_prompt_cancel(query, "✎ Send a short description, or \"-\" to skip:")
    return ADMIN_PLAN_ADD_DESC


async def admin_plan_add_desc_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    description = "" if text == "-" else text
    data = context.user_data.pop("admin_new_plan", {})
    data["description"] = description
    if not data.get("name"):
        await update.message.reply_text("✕ Something went wrong, please start again.", reply_markup=get_main_menu(update.effective_user.id))
        return ConversationHandler.END

    plan_id = subscription.add_plan(
        name=data["name"],
        price=data["price"],
        days=data["days"],
        max_servers=data["max_servers"],
        max_tabs=data["max_tabs"],
        description=data.get("description", ""),
        sftp_enabled=data.get("sftp_enabled", True),
        session_timeout_minutes=data.get("session_timeout_minutes"),
        max_automations=data.get("max_automations"),
        advanced_tools_enabled=data.get("advanced_tools_enabled", False),
    )
    logger.info(f"Admin {update.effective_user.id} added plan {plan_id}: {data}")

    sftp_flag = "✓" if data.get("sftp_enabled", True) else "⚿"
    adv_flag = "✓" if data.get("advanced_tools_enabled", False) else "⚿"
    timeout = data.get("session_timeout_minutes")
    timeout_txt = f"{timeout}m" if timeout else "∞"
    max_auto = data.get("max_automations")
    max_auto_txt = str(max_auto) if max_auto is not None else "∞"
    price_txt = "Free" if data["price"] == 0 else f"{data['price']:,} / {data['days']}d"
    await update.message.reply_text(
        f"✓ Plan added!\n\n"
        f"▨ {data['name']} - {price_txt} "
        f"(▣{data['max_servers']} ▤{data['max_tabs']} ▥{sftp_flag} ⚡{adv_flag} ⏱{timeout_txt} ⚙{max_auto_txt})\n\n"
        f"◈ New users are auto-granted whichever active plan is cheapest - "
        f"no extra step needed if this one's meant to be that.",
        reply_markup=get_main_menu(update.effective_user.id),
    )
    return ConversationHandler.END


def _payment_settings_text_and_keyboard():
    number = bot_settings.get_card_number() or "(not set)"
    holder = bot_settings.get_card_holder() or "(not set)"
    bank = bot_settings.get_card_bank() or "(not set)"
    text = (
        f"▭ Payment Settings\n\n"
        f"Card number: {number}\n"
        f"Holder: {holder}\n"
        f"Bank: {bank}\n\n"
        f"This is shown to users paying by card-to-card for a subscription or wallet top-up."
    )
    keyboard = [
        [InlineKeyboardButton("✎ Set Card Info", callback_data="admin_card_set")],
        [InlineKeyboardButton("← Back", callback_data="admin_back_to_main")],
    ]
    return text, InlineKeyboardMarkup(keyboard)


async def admin_payment_settings_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        await query.edit_message_text("⊘ You do not have admin access.")
        return
    text, reply_markup = _payment_settings_text_and_keyboard()
    await query.edit_message_text(text, reply_markup=reply_markup)


async def admin_card_set_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        await query.edit_message_text("⊘ You do not have admin access.")
        return ConversationHandler.END
    context.user_data["admin_new_card"] = {}
    await _edit_then_prompt_cancel(query, "▭ Send the card number:")
    return ADMIN_CARD_NUMBER


async def admin_card_number_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if not text:
        await update.message.reply_text("✕ Please send the card number, or tap Cancel.", reply_markup=_cancel_kb())
        return ADMIN_CARD_NUMBER
    context.user_data["admin_new_card"]["number"] = text
    await update.message.reply_text("◆ Send the card holder's name:", reply_markup=_cancel_kb())
    return ADMIN_CARD_HOLDER


async def admin_card_holder_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if not text:
        await update.message.reply_text("✕ Please send the holder's name, or tap Cancel.", reply_markup=_cancel_kb())
        return ADMIN_CARD_HOLDER
    context.user_data["admin_new_card"]["holder"] = text
    await update.message.reply_text("◈ Send the bank name:", reply_markup=_cancel_kb())
    return ADMIN_CARD_BANK


async def admin_card_bank_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if not text:
        await update.message.reply_text("✕ Please send the bank name, or tap Cancel.", reply_markup=_cancel_kb())
        return ADMIN_CARD_BANK
    data = context.user_data.pop("admin_new_card", {})
    bot_settings.set_card_info(card_number=data.get("number", ""), card_holder=data.get("holder", ""), card_bank=text)
    logger.info(f"Admin {update.effective_user.id} updated card payment info")

    await update.message.reply_text("✓ Card info updated!", reply_markup=get_main_menu(update.effective_user.id))
    return ConversationHandler.END


def _monitoring_settings_text_and_keyboard():
    text = (
        f"✚ Monitoring Settings\n\n"
        f"⏱ Check interval: {svm_health.get_interval_seconds() / 3600:.1f}h\n"
        f"⌛ Per-server SSH timeout: {svm_health.get_check_timeout()}s\n"
        f"▪ Disk alert threshold: {svm_health.get_disk_alert_percent()}%\n"
        f"↓ Disk alert hysteresis: {svm_health.get_disk_alert_hysteresis()} points\n"
        f"▣ CPU alert threshold: {svm_health.get_cpu_alert_percent()}%\n"
        f"✦ RAM alert threshold: {svm_health.get_ram_alert_percent()}%\n\n"
        f"CPU/RAM thresholds are used by the Owner Server monitor (see ▣ Owner "
        f"Server in the admin panel); the hysteresis above applies to all three.\n\n"
        f"Tap a setting below to change it."
    )
    keyboard = [
        [
            InlineKeyboardButton("⏱ Check Interval", callback_data="admin_mon_interval"),
            InlineKeyboardButton("⌛ SSH Timeout", callback_data="admin_mon_timeout"),
        ],
        [
            InlineKeyboardButton("▪ Disk Threshold", callback_data="admin_mon_diskpct"),
            InlineKeyboardButton("↓ Disk Hysteresis", callback_data="admin_mon_hysteresis"),
        ],
        [
            InlineKeyboardButton("▣ CPU Threshold", callback_data="admin_mon_cpupct"),
            InlineKeyboardButton("✦ RAM Threshold", callback_data="admin_mon_rampct"),
        ],
        [InlineKeyboardButton("← Back", callback_data="admin_back_to_main")],
    ]
    return text, InlineKeyboardMarkup(keyboard)


async def admin_monitoring_settings_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        await query.edit_message_text("⊘ You do not have admin access.")
        return
    text, reply_markup = _monitoring_settings_text_and_keyboard()
    await query.edit_message_text(text, reply_markup=reply_markup)


async def admin_mon_interval_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        await query.edit_message_text("⊘ You do not have admin access.")
        return ConversationHandler.END
    await _edit_then_prompt_cancel(
        query,
        f"⏱ Send the new check interval in hours (current: {svm_health.get_interval_seconds() / 3600:.1f}h).\n"
        f"Minimum 0.5h (30 min). Decimals are fine, e.g. 1.5.",
    )
    return ADMIN_MON_INTERVAL


async def admin_mon_interval_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip().replace(",", ".")
    try:
        hours = float(text)
    except ValueError:
        hours = None
    if hours is None or hours < 0.5:
        await update.message.reply_text(
            "✕ Please send a number of hours, 0.5 or higher (e.g. 1 or 1.5), or tap Cancel.",
            reply_markup=_cancel_kb(),
        )
        return ADMIN_MON_INTERVAL
    value = round(hours * 3600)
    bot_settings.set_health_interval_seconds(value)
    svm_health.reschedule_job(context.job_queue, value)
    logger.info(f"Admin {update.effective_user.id} set health_interval_seconds={value} ({hours}h)")
    await update.message.reply_text(f"✓ Check interval set to {hours:.1f}h.", reply_markup=get_main_menu(update.effective_user.id))
    return ConversationHandler.END


async def admin_mon_timeout_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        await query.edit_message_text("⊘ You do not have admin access.")
        return ConversationHandler.END
    await _edit_then_prompt_cancel(
        query,
        f"⌛ Send the new per-server SSH timeout in seconds (current: {svm_health.get_check_timeout()}s).\n"
        f"Range 3-120.",
    )
    return ADMIN_MON_TIMEOUT


async def admin_mon_timeout_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if not text.isdigit() or not (3 <= int(text) <= 120):
        await update.message.reply_text(
            "✕ Please send a whole number of seconds between 3 and 120, or tap Cancel.", reply_markup=_cancel_kb()
        )
        return ADMIN_MON_TIMEOUT
    value = int(text)
    bot_settings.set_health_check_timeout(value)
    logger.info(f"Admin {update.effective_user.id} set health_check_timeout={value}")
    await update.message.reply_text(f"✓ SSH timeout set to {value}s.", reply_markup=get_main_menu(update.effective_user.id))
    return ConversationHandler.END


async def admin_mon_diskpct_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        await query.edit_message_text("⊘ You do not have admin access.")
        return ConversationHandler.END
    await _edit_then_prompt_cancel(
        query,
        f"▪ Send the new disk-full alert threshold as a percent (current: {svm_health.get_disk_alert_percent()}%).\n"
        f"Range 1-100.",
    )
    return ADMIN_MON_DISKPCT


async def admin_mon_diskpct_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if not text.isdigit() or not (1 <= int(text) <= 100):
        await update.message.reply_text(
            "✕ Please send a whole number between 1 and 100, or tap Cancel.", reply_markup=_cancel_kb()
        )
        return ADMIN_MON_DISKPCT
    value = int(text)
    bot_settings.set_disk_alert_percent(value)
    logger.info(f"Admin {update.effective_user.id} set disk_alert_percent={value}")
    await update.message.reply_text(f"✓ Disk alert threshold set to {value}%.", reply_markup=get_main_menu(update.effective_user.id))
    return ConversationHandler.END


async def admin_mon_hysteresis_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        await query.edit_message_text("⊘ You do not have admin access.")
        return ConversationHandler.END
    await _edit_then_prompt_cancel(
        query,
        f"↓ Send the new disk alert hysteresis in points (current: {svm_health.get_disk_alert_hysteresis()}).\n"
        f"This is how far disk usage must drop below the threshold before a future "
        f"crossing can alert again - keeps a server hovering near the threshold from "
        f"spamming alerts. Range 0-50.",
    )
    return ADMIN_MON_HYSTERESIS


async def admin_mon_hysteresis_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if not text.isdigit() or not (0 <= int(text) <= 50):
        await update.message.reply_text(
            "✕ Please send a whole number between 0 and 50, or tap Cancel.", reply_markup=_cancel_kb()
        )
        return ADMIN_MON_HYSTERESIS
    value = int(text)
    bot_settings.set_disk_alert_hysteresis(value)
    logger.info(f"Admin {update.effective_user.id} set disk_alert_hysteresis={value}")
    await update.message.reply_text(f"✓ Disk alert hysteresis set to {value} points.", reply_markup=get_main_menu(update.effective_user.id))
    return ConversationHandler.END


async def admin_mon_cpupct_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        await query.edit_message_text("⊘ You do not have admin access.")
        return ConversationHandler.END
    await _edit_then_prompt_cancel(
        query,
        f"▣ Send the new CPU alert threshold as a percent (current: {svm_health.get_cpu_alert_percent()}%).\n"
        f"Used by the Owner Server monitor. Range 1-100.",
    )
    return ADMIN_MON_CPUPCT


async def admin_mon_cpupct_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if not text.isdigit() or not (1 <= int(text) <= 100):
        await update.message.reply_text(
            "✕ Please send a whole number between 1 and 100, or tap Cancel.", reply_markup=_cancel_kb()
        )
        return ADMIN_MON_CPUPCT
    value = int(text)
    bot_settings.set_cpu_alert_percent(value)
    logger.info(f"Admin {update.effective_user.id} set cpu_alert_percent={value}")
    await update.message.reply_text(f"✓ CPU alert threshold set to {value}%.", reply_markup=get_main_menu(update.effective_user.id))
    return ConversationHandler.END


async def admin_mon_rampct_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        await query.edit_message_text("⊘ You do not have admin access.")
        return ConversationHandler.END
    await _edit_then_prompt_cancel(
        query,
        f"✦ Send the new RAM alert threshold as a percent (current: {svm_health.get_ram_alert_percent()}%).\n"
        f"Used by the Owner Server monitor. Range 1-100.",
    )
    return ADMIN_MON_RAMPCT


async def admin_mon_rampct_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if not text.isdigit() or not (1 <= int(text) <= 100):
        await update.message.reply_text(
            "✕ Please send a whole number between 1 and 100, or tap Cancel.", reply_markup=_cancel_kb()
        )
        return ADMIN_MON_RAMPCT
    value = int(text)
    bot_settings.set_ram_alert_percent(value)
    logger.info(f"Admin {update.effective_user.id} set ram_alert_percent={value}")
    await update.message.reply_text(f"✓ RAM alert threshold set to {value}%.", reply_markup=get_main_menu(update.effective_user.id))
    return ConversationHandler.END


def _owner_server_keyboard() -> InlineKeyboardMarkup:
    monitor_on = bot_settings.is_owner_monitor_enabled()
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("↻ Refresh", callback_data="admin_ownersrv_menu"),
            InlineKeyboardButton(f"◉ Alerts: {'ON' if monitor_on else 'OFF'}", callback_data="admin_ownersrv_toggle"),
        ],
        [
            InlineKeyboardButton("↑ Top CPU", callback_data="admin_ownersrv_top_cpu"),
            InlineKeyboardButton("▤ Top RAM", callback_data="admin_ownersrv_top_ram"),
        ],
        [
            InlineKeyboardButton("↻ Restart Bot", callback_data="admin_ownersrv_restart"),
            InlineKeyboardButton("✕ Cleanup", callback_data="admin_ownersrv_cleanup"),
            InlineKeyboardButton("⟲ Reboot Server", callback_data="admin_ownersrv_reboot"),
        ],
        [InlineKeyboardButton("🧵 Threads", callback_data="admin_ownersrv_threads")],
        [
            InlineKeyboardButton("▣ Terminal", callback_data="admin_ownersrv_terminal"),
            InlineKeyboardButton("▥ Files", callback_data=OWNERSRV_FILES_START_CB),
        ],
        [
            InlineKeyboardButton("⚙ Services", callback_data=OWNERSRV_SVC_MENU_CB),
            InlineKeyboardButton("⧗ Crontab", callback_data=OWNERSRV_CRON_MENU_CB),
        ],
        [InlineKeyboardButton("▸ Set Timezone", callback_data="admin_ownersrv_tz")],
        [InlineKeyboardButton("← Back", callback_data="admin_back_to_main")],
    ])


async def _send_owner_server_card(query):
    try:
        snap = owner_server.snapshot()
        text = owner_server.format_snapshot_text(snap)
    except Exception as e:
        logger.error(f"owner server snapshot failed: {e}")
        text = f"✕ Could not read host stats: {e}"
    try:
        await query.edit_message_text(text, reply_markup=_owner_server_keyboard(), parse_mode="Markdown")
    except BadRequest:
        pass


async def admin_ownersrv_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer("⏳ Reading host stats...")
    if not is_admin(query.from_user.id):
        await query.edit_message_text("⊘ You do not have admin access.")
        return
    await _send_owner_server_card(query)


async def admin_ownersrv_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not is_admin(query.from_user.id):
        await query.answer("⊘ You do not have admin access.", show_alert=True)
        return
    new_state = not bot_settings.is_owner_monitor_enabled()
    bot_settings.set_owner_monitor_enabled(new_state)
    logger.info(f"Admin {query.from_user.id} set owner_monitor_enabled={new_state}")
    await query.answer(f"◉ Owner Server alerts {'enabled' if new_state else 'disabled'}.")
    await _send_owner_server_card(query)


async def admin_ownersrv_tz_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        await query.edit_message_text("⊘ You do not have admin access.")
        return ConversationHandler.END
    current = bot_settings.get_owner_timezone()
    await _edit_then_prompt_cancel(
        query,
        f"▸ Send the IANA timezone name for the Owner Server clock (current: {current}).\n"
        f"Examples: Europe/Berlin, Asia/Tehran, America/New_York, UTC.",
    )
    return ADMIN_OWNERSRV_TZ


async def admin_ownersrv_tz_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    try:
        ZoneInfo(text)
    except (ZoneInfoNotFoundError, ValueError, KeyError):
        await update.message.reply_text(
            "✕ Unknown timezone. Send a valid IANA name (e.g. Europe/Berlin), or tap Cancel.",
            reply_markup=_cancel_kb(),
        )
        return ADMIN_OWNERSRV_TZ
    bot_settings.set_owner_timezone(text)
    logger.info(f"Admin {update.effective_user.id} set owner_timezone={text}")
    await update.message.reply_text(
        f"✓ Owner Server timezone set to {text}.", reply_markup=get_main_menu(update.effective_user.id)
    )
    return ConversationHandler.END


async def _render_ownersrv_top(query, context: ContextTypes.DEFAULT_TYPE, by: str):
    context.user_data["ownersrv_top_by"] = by
    try:
        rows = owner_server.top_processes(by=by)
        text = owner_server.format_top_processes_text(rows, by)
    except Exception as e:
        logger.error(f"owner server top_processes failed: {e}")
        rows, text = [], f"✕ Could not list processes: {e}"

    kb_rows = []
    for r in rows:
        kb_rows.append([InlineKeyboardButton(
            f"⊘ Kill {r['pid']} · {r['name']}", callback_data=f"{OWNERSRV_KILL_PROMPT_CB_PREFIX}{r['pid']}"
        )])
    kb_rows.append([InlineKeyboardButton("← Back", callback_data="admin_ownersrv_menu")])
    keyboard = InlineKeyboardMarkup(kb_rows)
    try:
        await query.edit_message_text(text, reply_markup=keyboard, parse_mode="Markdown")
    except BadRequest:
        pass


async def admin_ownersrv_top(update: Update, context: ContextTypes.DEFAULT_TYPE, by: str):
    query = update.callback_query
    if not is_admin(query.from_user.id):
        await query.answer("⊘ You do not have admin access.", show_alert=True)
        return
    await query.answer("⏳ Sampling processes...")
    await _render_ownersrv_top(query, context, by)


async def admin_ownersrv_kill_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not is_admin(query.from_user.id):
        await query.answer("⊘ You do not have admin access.", show_alert=True)
        return
    pid = int(query.data.replace(OWNERSRV_KILL_PROMPT_CB_PREFIX, "", 1))
    await query.answer()
    name = owner_server.process_name(pid) or "?"
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("⊘ Terminate (SIGTERM)", callback_data=f"{OWNERSRV_KILL_TERM_CB_PREFIX}{pid}")],
        [InlineKeyboardButton("⊘ Force kill (SIGKILL)", callback_data=f"{OWNERSRV_KILL_FORCE_CB_PREFIX}{pid}")],
        [InlineKeyboardButton("✕ Cancel", callback_data="admin_ownersrv_menu")],
    ])
    await query.edit_message_text(
        f"Kill process `{pid}` ({name})?\n\nTry Terminate first - it gives the process a chance to shut down "
        f"cleanly. Only Force kill if it doesn't respond.",
        reply_markup=keyboard, parse_mode="Markdown",
    )


async def _admin_ownersrv_kill_execute(update: Update, context: ContextTypes.DEFAULT_TYPE, force: bool):
    query = update.callback_query
    if not is_admin(query.from_user.id):
        await query.answer("⊘ You do not have admin access.", show_alert=True)
        return
    prefix = OWNERSRV_KILL_FORCE_CB_PREFIX if force else OWNERSRV_KILL_TERM_CB_PREFIX
    pid = int(query.data.replace(prefix, "", 1))
    logger.warning(f"Admin {query.from_user.id} sent {'SIGKILL' if force else 'SIGTERM'} to host pid {pid}")
    result = owner_server.kill_process(pid, force=force)
    await query.answer("✓ Signal sent." if result["ok"] else f"✕ {result['error']}", show_alert=not result["ok"])
    by = context.user_data.get("ownersrv_top_by", "cpu")
    await _render_ownersrv_top(query, context, by)


async def admin_ownersrv_kill_term(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _admin_ownersrv_kill_execute(update, context, force=False)


async def admin_ownersrv_kill_force(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _admin_ownersrv_kill_execute(update, context, force=True)


_OSVC_FILTERS = (("all", "▤ All"), ("custom", "◦ Custom"), ("system", "▣ System"))


def _osvc_menu_text(units, filter_mode: str = "all") -> str:
    if not units:
        return "⚙ *Services*\n\n(none found, or `systemctl` isn't available on this host)"
    custom_n = sum(1 for u in units if u.get("origin") == "custom")
    system_n = len(units) - custom_n
    shown_n = {"custom": custom_n, "system": system_n}.get(filter_mode, len(units))
    return (
        f"⚙ *Services*  ·  {shown_n} shown\n"
        f"◦ {custom_n} custom   ▣ {system_n} system\n\n"
        f"Tap one to view status and start/stop/restart it."
    )


def _osvc_keyboard(units, filter_mode: str = "all") -> InlineKeyboardMarkup:
    def _unit_button(i, u):
        dot = "◉" if u["active"] == "active" else ("◯" if u["sub"] == "failed" else "◯")
        return InlineKeyboardButton(
            f"{dot} {u['unit']} ({u['active']})", callback_data=f"{OWNERSRV_SVC_VIEW_CB_PREFIX}{i}"
        )

    indexed = list(enumerate(units))
    custom = [(i, u) for i, u in indexed if u.get("origin") == "custom"]
    system = [(i, u) for i, u in indexed if u.get("origin") != "custom"]

    filter_row = [
        InlineKeyboardButton(
            ("• " if filter_mode == mode else "") + f"{label} ({len(units) if mode == 'all' else (len(custom) if mode == 'custom' else len(system))})",
            callback_data=f"{OWNERSRV_SVC_FILTER_CB_PREFIX}{mode}",
        )
        for mode, label in _OSVC_FILTERS
    ]

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
            rows.append([InlineKeyboardButton(f"┈┈ {label} ┈┈", callback_data="admin_noop")])
        for i, u in group:
            rows.append([_unit_button(i, u)])
    if not any_shown:
        rows.append([InlineKeyboardButton("(none in this category)", callback_data="admin_noop")])

    rows.append(filter_row)
    rows.append([InlineKeyboardButton("↻ Refresh", callback_data=OWNERSRV_SVC_MENU_CB)])
    rows.append([InlineKeyboardButton("← Back", callback_data="admin_ownersrv_menu")])
    return InlineKeyboardMarkup(rows)


async def _render_svc_menu(query, context: ContextTypes.DEFAULT_TYPE):
    units = owner_server.systemd_list_units()
    context.user_data["ownersrv_svc_list"] = units
    filter_mode = context.user_data.get("ownersrv_svc_filter", "all")
    text = _osvc_menu_text(units, filter_mode)
    try:
        await query.edit_message_text(text, reply_markup=_osvc_keyboard(units, filter_mode), parse_mode="Markdown")
    except BadRequest:
        pass


async def admin_ownersrv_svc_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not is_admin(query.from_user.id):
        await query.answer("⊘ You do not have admin access.", show_alert=True)
        return
    await query.answer("⏳ Listing services...")
    await _render_svc_menu(query, context)


async def admin_ownersrv_svc_filter(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not is_admin(query.from_user.id):
        await query.answer("⊘ You do not have admin access.", show_alert=True)
        return
    mode = query.data.replace(OWNERSRV_SVC_FILTER_CB_PREFIX, "", 1)
    if mode not in ("all", "custom", "system"):
        mode = "all"
    context.user_data["ownersrv_svc_filter"] = mode
    units = context.user_data.get("ownersrv_svc_list") or []
    await query.answer()
    try:
        await query.edit_message_text(
            _osvc_menu_text(units, mode), reply_markup=_osvc_keyboard(units, mode), parse_mode="Markdown"
        )
    except BadRequest:
        pass


async def _render_svc_view(query, context: ContextTypes.DEFAULT_TYPE, idx: int):
    units = context.user_data.get("ownersrv_svc_list") or []
    if idx < 0 or idx >= len(units):
        await query.answer("That list is stale - tap Refresh.", show_alert=True)
        return
    unit = units[idx]["unit"]
    is_custom = units[idx].get("origin") == "custom"
    status = owner_server.systemd_unit_status(unit)
    body = _strip_ansi(status.get("text") or status.get("error") or "(no output)")[:3200]
    rows = [
        [
            InlineKeyboardButton("▶ Start", callback_data=f"{OWNERSRV_SVC_ACTION_CB_PREFIX}{idx}_start"),
            InlineKeyboardButton("⏹ Stop", callback_data=f"{OWNERSRV_SVC_ACTION_CB_PREFIX}{idx}_stop"),
            InlineKeyboardButton("↻ Restart", callback_data=f"{OWNERSRV_SVC_ACTION_CB_PREFIX}{idx}_restart"),
        ],
        [
            InlineKeyboardButton("◉ Enable", callback_data=f"{OWNERSRV_SVC_ACTION_CB_PREFIX}{idx}_enable"),
            InlineKeyboardButton("◯ Disable", callback_data=f"{OWNERSRV_SVC_ACTION_CB_PREFIX}{idx}_disable"),
            InlineKeyboardButton("▤ Live Log", callback_data=f"{OWNERSRV_SVC_LOG_CB_PREFIX}{idx}"),
        ],
    ]
    if is_custom:
        rows.append([InlineKeyboardButton("✕ Remove", callback_data=f"{OWNERSRV_SVC_REMOVE_PROMPT_CB_PREFIX}{idx}")])
    rows.append([InlineKeyboardButton("← Back", callback_data=OWNERSRV_SVC_MENU_CB)])
    keyboard = InlineKeyboardMarkup(rows)
    try:
        await query.edit_message_text(f"⚙ *{unit}*\n```\n{body}\n```", reply_markup=keyboard, parse_mode="Markdown")
    except BadRequest:
        pass


async def admin_ownersrv_svc_view(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not is_admin(query.from_user.id):
        await query.answer("⊘ You do not have admin access.", show_alert=True)
        return
    idx = int(query.data.replace(OWNERSRV_SVC_VIEW_CB_PREFIX, "", 1))
    await query.answer("⏳ Reading status...")
    await _render_svc_view(query, context, idx)


async def admin_ownersrv_svc_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not is_admin(query.from_user.id):
        await query.answer("⊘ You do not have admin access.", show_alert=True)
        return
    units = context.user_data.get("ownersrv_svc_list") or []
    payload = query.data.replace(OWNERSRV_SVC_ACTION_CB_PREFIX, "", 1)
    idx_str, _, action = payload.partition("_")
    idx = int(idx_str)
    if idx < 0 or idx >= len(units):
        await query.answer("That list is stale - tap Refresh.", show_alert=True)
        return
    unit = units[idx]["unit"]
    logger.warning(f"Admin {query.from_user.id} ran systemctl {action} {unit}")
    result = owner_server.systemd_unit_action(unit, action)
    if not result["ok"]:
        await query.answer(f"✕ {action} failed: {(result.get('error') or result.get('output') or '')[:180]}", show_alert=True)
    else:
        await query.answer(f"✓ {unit}: {action} done.")
    await _render_svc_view(query, context, idx)


def _format_osvc_log(unit: str, output: str, status_line: str) -> str:
    body = _strip_ansi(output)[-OWNERSRV_TAIL_BODY_CHARS:].rstrip("\n")
    header = f"▤ *Live log* `{unit}`"
    if not body:
        return f"{header}\n{status_line}"
    return f"{header}\n```\n{body}\n\n```\n{status_line}"


def _osvc_log_keyboard(alive: bool = True) -> InlineKeyboardMarkup:
    label = "⊘ Stop" if alive else "← Back"
    return InlineKeyboardMarkup([[InlineKeyboardButton(label, callback_data=OWNERSRV_SVC_LOGSTOP_CB)]])


async def admin_ownersrv_svc_log_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not is_admin(query.from_user.id):
        await query.answer("⊘ You do not have admin access.", show_alert=True)
        return
    units = context.user_data.get("ownersrv_svc_list") or []
    idx = int(query.data.replace(OWNERSRV_SVC_LOG_CB_PREFIX, "", 1))
    if idx < 0 or idx >= len(units):
        await query.answer("That list is stale - tap Refresh.", show_alert=True)
        return
    unit = units[idx]["unit"]

    old_proc = context.user_data.get("ownersrv_svc_log_proc")
    if old_proc is not None:
        owner_server.tail_stop(old_proc)
        context.user_data["ownersrv_svc_log_proc"] = None

    await query.answer("⏳ Starting...")
    try:
        proc = owner_server.systemd_journal_tail_start(unit)
    except Exception as e:
        await query.answer(f"✕ Could not start log: {str(e)[:180]}", show_alert=True)
        return

    context.user_data["ownersrv_svc_log_proc"] = proc
    context.user_data["ownersrv_svc_log_idx"] = idx
    chat_id = query.message.chat_id
    msg = await context.bot.send_message(
        chat_id=chat_id,
        text=_format_osvc_log(unit, "", "◉ Following — new log lines appear here as they're written."),
        parse_mode="Markdown", reply_markup=_osvc_log_keyboard(),
    )
    logger.info(f"Admin {query.from_user.id} started a live log for {unit} on the owner server")
    asyncio.create_task(_stream_osvc_log(context, chat_id, unit, msg.message_id))


async def _stream_osvc_log(context: ContextTypes.DEFAULT_TYPE, chat_id: int, unit: str, message_id: int):
    output_so_far = ""
    last_sent_text = None
    while True:
        await asyncio.sleep(OWNERSRV_TAIL_EDIT_INTERVAL)
        proc = context.user_data.get("ownersrv_svc_log_proc")
        if proc is None:
            return

        chunk = await asyncio.to_thread(owner_server.tail_read_available, proc)
        if context.user_data.get("ownersrv_svc_log_proc") is not proc:
            return
        if chunk:
            output_so_far += chunk
        alive = owner_server.tail_is_alive(proc)
        status = "◉ Following…" if alive else "▪ journalctl ended (unit may have been removed)."
        new_text = _format_osvc_log(unit, output_so_far, status)
        if new_text != last_sent_text:
            try:
                await context.bot.edit_message_text(
                    new_text, chat_id=chat_id, message_id=message_id,
                    parse_mode="Markdown", reply_markup=_osvc_log_keyboard(alive),
                )
                last_sent_text = new_text
            except BadRequest as e:
                if "not modified" not in str(e).lower():
                    logger.debug(f"owner-server service log live edit failed: {e}")
            except Exception as e:
                logger.debug(f"owner-server service log live edit failed: {e}")
        if not alive:
            context.user_data["ownersrv_svc_log_proc"] = None
            return


async def admin_ownersrv_svc_log_stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    proc = context.user_data.get("ownersrv_svc_log_proc")
    context.user_data["ownersrv_svc_log_proc"] = None
    if proc is not None:
        await asyncio.to_thread(owner_server.tail_stop, proc)
    await query.answer("⊘ Stopped.")
    idx = context.user_data.get("ownersrv_svc_log_idx")
    if idx is not None:
        await _render_svc_view(query, context, idx)
        return
    try:
        await query.edit_message_text(
            _format_osvc_log("?", "", "⊘ Stopped."), parse_mode="Markdown", reply_markup=None,
        )
    except BadRequest:
        pass


async def admin_ownersrv_svc_remove_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not is_admin(query.from_user.id):
        await query.answer("⊘ You do not have admin access.", show_alert=True)
        return
    units = context.user_data.get("ownersrv_svc_list") or []
    idx = int(query.data.replace(OWNERSRV_SVC_REMOVE_PROMPT_CB_PREFIX, "", 1))
    if idx < 0 or idx >= len(units):
        await query.answer("That list is stale - tap Refresh.", show_alert=True)
        return
    unit = units[idx]
    if unit.get("origin") != "custom":
        await query.answer("⚿ Only custom units can be removed here.", show_alert=True)
        return
    await query.answer()
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("✕ Yes, remove it", callback_data=f"{OWNERSRV_SVC_REMOVE_CONFIRM_CB_PREFIX}{idx}")],
        [InlineKeyboardButton("✕ Cancel", callback_data=f"{OWNERSRV_SVC_VIEW_CB_PREFIX}{idx}")],
    ])
    await query.edit_message_text(
        f"⚠ Remove `{unit['unit']}`?\n\n"
        f"This stops it, disables it, deletes its unit file, and reloads systemd. This cannot be undone.",
        reply_markup=keyboard, parse_mode="Markdown",
    )


async def admin_ownersrv_svc_remove_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not is_admin(query.from_user.id):
        await query.answer("⊘ You do not have admin access.", show_alert=True)
        return
    units = context.user_data.get("ownersrv_svc_list") or []
    idx = int(query.data.replace(OWNERSRV_SVC_REMOVE_CONFIRM_CB_PREFIX, "", 1))
    if idx < 0 or idx >= len(units):
        await query.answer("That list is stale - tap Refresh.", show_alert=True)
        return
    unit = units[idx]["unit"]
    logger.warning(f"Admin {query.from_user.id} removed systemd unit {unit}")
    result = owner_server.systemd_unit_remove(unit)
    if not result["ok"]:
        await query.answer(f"✕ Remove failed: {(result.get('error') or '')[:180]}", show_alert=True)
        await _render_svc_view(query, context, idx)
        return
    await query.answer(f"✓ {unit} removed.")
    await _render_svc_menu(query, context)


def _oscron_text(body: str) -> str:
    body = body if body.strip() else "(empty - no crontab entries yet)"
    escaped = html.escape(body)
    return f"⧗ <b>Crontab</b>\n<i>Runs as the same user the bot process runs as.</i>\n\n<pre>{escaped}</pre>"


async def admin_ownersrv_cron_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not is_admin(query.from_user.id):
        await query.answer("⊘ You do not have admin access.", show_alert=True)
        return ConversationHandler.END
    await query.answer("⏳ Reading crontab...")
    try:
        body = owner_server.get_crontab()
    except Exception as e:
        body = f"✕ Could not read crontab: {e}"
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("✎ Edit", callback_data=OWNERSRV_CRON_EDIT_CB)],
        [InlineKeyboardButton("← Back", callback_data="admin_ownersrv_menu")],
    ])
    try:
        await query.edit_message_text(_oscron_text(body), reply_markup=keyboard, parse_mode="HTML")
    except BadRequest:
        pass
    return ConversationHandler.END


async def admin_ownersrv_cron_edit_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not is_admin(query.from_user.id):
        await query.answer("⊘ You do not have admin access.", show_alert=True)
        return ConversationHandler.END
    await query.answer()
    context.user_data["ownersrv_cron_chat_id"] = query.message.chat_id
    try:
        current = owner_server.get_crontab()
    except Exception as e:
        current = f"# could not read current crontab: {e}\n"
    await _edit_then_prompt_cancel(
        query,
        "✎ Send the *complete* new crontab content (replaces everything - copy from above, edit, and send it "
        "back). One `# ...` comment line if you just want it emptied.\n\n"
        f"Current:\n```\n{current.strip() or '(empty)'}\n```",
    )
    return ADMIN_OWNERSRV_CRON


async def admin_ownersrv_cron_save(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return ConversationHandler.END
    new_content = update.message.text or ""
    if len(new_content.encode("utf-8")) > owner_server.CRONTAB_EDITOR_MAX_BYTES:
        await update.message.reply_text(
            f"✕ That's over {owner_server.CRONTAB_EDITOR_MAX_BYTES} bytes - too big to save here. "
            f"Send something shorter, or tap Cancel."
        )
        return ADMIN_OWNERSRV_CRON
    if not new_content.endswith("\n"):
        new_content += "\n"
    logger.warning(f"Admin {update.effective_user.id} replaced the owner-server crontab")
    try:
        owner_server.set_crontab(new_content)
    except Exception as e:
        await update.message.reply_text(f"✕ Save failed: {str(e)[:300]}", reply_markup=ReplyKeyboardRemove())
        return ADMIN_OWNERSRV_CRON
    await update.message.reply_text("✓ Crontab saved.", reply_markup=ReplyKeyboardRemove())
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("✎ Edit", callback_data=OWNERSRV_CRON_EDIT_CB)],
        [InlineKeyboardButton("← Back", callback_data="admin_ownersrv_menu")],
    ])
    await update.message.reply_text(_oscron_text(new_content), reply_markup=keyboard, parse_mode="HTML")
    return ConversationHandler.END


async def admin_ownersrv_top_cpu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await admin_ownersrv_top(update, context, by="cpu")


async def admin_ownersrv_top_ram(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await admin_ownersrv_top(update, context, by="mem")


async def admin_ownersrv_threads(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not is_admin(query.from_user.id):
        await query.answer("⊘ You do not have admin access.", show_alert=True)
        return
    await query.answer("⏳ Sampling threads...")
    try:
        threads = owner_server.bot_thread_details()
        text = owner_server.format_bot_threads_text(threads)
    except Exception as e:
        logger.error(f"owner server bot_thread_details failed: {e}")
        text = f"✕ Could not read thread details: {e}"
    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("← Back", callback_data="admin_ownersrv_menu")]])
    try:
        await query.edit_message_text(text, reply_markup=keyboard, parse_mode="Markdown")
    except BadRequest:
        pass


async def admin_ownersrv_restart_confirm_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not is_admin(query.from_user.id):
        await query.answer("⊘ You do not have admin access.", show_alert=True)
        return
    await query.answer()
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("✓ Yes, restart", callback_data="admin_ownersrv_restartok"),
         InlineKeyboardButton("✕ Cancel", callback_data="admin_ownersrv_menu")],
    ])
    await query.edit_message_text(
        "⚠ Restart the bot process? This briefly drops the connection to Telegram and closes any "
        "open Server Manager terminal tabs for every user.",
        reply_markup=keyboard,
    )


async def admin_ownersrv_restart_execute(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not is_admin(query.from_user.id):
        await query.answer("⊘ You do not have admin access.", show_alert=True)
        return
    await query.answer("⏳ Restarting...")
    logger.warning(f"Admin {query.from_user.id} triggered an owner-server bot restart")
    result = owner_server.restart_bot()
    text = f"↻ Restarting now via `{result['method']}`. Give it a few seconds and send /start."
    try:
        await query.edit_message_text(text, parse_mode="Markdown")
    except BadRequest:
        pass


async def admin_ownersrv_reboot_confirm_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not is_admin(query.from_user.id):
        await query.answer("⊘ You do not have admin access.", show_alert=True)
        return
    await query.answer()
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("✓ Yes, reboot", callback_data="admin_ownersrv_rebootok"),
         InlineKeyboardButton("✕ Cancel", callback_data="admin_ownersrv_menu")],
    ])
    await query.edit_message_text(
        "⚠ Reboot the Owner Server host? This restarts the entire machine the bot is running "
        "on - the bot itself, every service on this host, and any open terminal tabs will all "
        "go down until it comes back up.",
        reply_markup=keyboard,
    )


async def admin_ownersrv_reboot_execute(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not is_admin(query.from_user.id):
        await query.answer("⊘ You do not have admin access.", show_alert=True)
        return
    await query.answer("⏳ Rebooting...")
    logger.warning(f"Admin {query.from_user.id} triggered an owner-server host reboot")
    result = owner_server.reboot_host()
    if result.get("ok"):
        text = "↻ Owner Server is rebooting now. Give it a minute, then check back."
    else:
        text = f"✕ Reboot failed: {result.get('error') or 'unknown error'}"
    try:
        await query.edit_message_text(text)
    except BadRequest:
        pass


async def admin_ownersrv_cleanup_execute(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not is_admin(query.from_user.id):
        await query.answer("⊘ You do not have admin access.", show_alert=True)
        return
    await query.answer("⏳ Cleaning up...")
    try:
        await query.edit_message_text("◐ Cleaning up the Owner Server…  (0s)")
    except BadRequest:
        pass

    result, timed_out = await _osf_run_with_spinner(
        owner_server.cleanup_local, label="Cleaning up the Owner Server…",
        query=query, timeout=svm_maintenance.CLEANUP_TIMEOUT + 10,
    )
    if timed_out:
        text = "⏱ Cleanup on the Owner Server took too long."
    elif isinstance(result, Exception):
        text = f"✕ Cleanup failed: {result}"
    elif not result.get("ok"):
        text = f"✕ Cleanup failed: {result.get('error') or 'unknown error'}"
    else:
        freed_summary = svm_maintenance.format_freed_summary(result)
        text = f"⟲ Cleanup finished on the Owner Server — approximately {freed_summary}."
    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("← Back", callback_data="admin_ownersrv_menu")]])
    try:
        await query.edit_message_text(text, reply_markup=keyboard)
    except BadRequest:
        pass


OWNERSRV_TERM_DOWNLOAD_TAIL_CHARS = 200_000


def _ownersrv_tab_label(terminals: dict, tab_id: str) -> str:
    ids = list(terminals.keys())
    try:
        idx = ids.index(tab_id) + 1
    except ValueError:
        idx = len(ids) + 1
    return f"Window {idx}"


def _ownersrv_tabs_keyboard_rows(terminals: dict, active_id: str) -> list:
    rows = []
    for tid in terminals:
        is_active = tid == active_id
        rows.append([
            InlineKeyboardButton(
                f"{'◉' if is_active else '◯'} {_ownersrv_tab_label(terminals, tid)}",
                callback_data=OWNERSRV_NOOP_CB if is_active else f"{OWNERSRV_TAB_SWITCH_CB_PREFIX}{tid}",
            ),
            InlineKeyboardButton("Close", callback_data=f"{OWNERSRV_TAB_CLOSE_CB_PREFIX}{tid}"),
        ])
    return rows


def _ownersrv_all_quick_commands() -> list:
    custom = [(c["label"], c["cmd"]) for c in bot_settings.get_owner_quick_commands()]
    return OWNERSRV_QUICK_COMMANDS + custom


def _ownersrv_quick_keyboard_rows(tab_id: str) -> list:
    rows = []
    row = []
    for i, (label, _cmd) in enumerate(_ownersrv_all_quick_commands()):
        row.append(InlineKeyboardButton(label, callback_data=f"{OWNERSRV_QUICK_RUN_CB_PREFIX}{i}_{tab_id}"))
        if len(row) == 3:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    add_row = [InlineKeyboardButton("+ Add command", callback_data=f"{OWNERSRV_QUICK_ADD_CB_PREFIX}{tab_id}")]
    if bot_settings.get_owner_quick_commands():
        add_row.append(InlineKeyboardButton("✕ Remove", callback_data=f"{OWNERSRV_QUICK_MANAGE_CB_PREFIX}{tab_id}"))
    rows.append(add_row)
    rows.append([InlineKeyboardButton("◀ Close quick menu", callback_data=f"{OWNERSRV_QUICK_CLOSE_CB_PREFIX}{tab_id}")])
    return rows


def _ownersrv_quick_manage_keyboard_rows(tab_id: str) -> list:
    rows = []
    for i, entry in enumerate(bot_settings.get_owner_quick_commands()):
        rows.append([InlineKeyboardButton(
            f"✕ {entry['label']}", callback_data=f"{OWNERSRV_QUICK_DEL_CB_PREFIX}{i}_{tab_id}",
        )])
    rows.append([InlineKeyboardButton("◀ Back", callback_data=f"{OWNERSRV_QUICK_MENU_CB_PREFIX}{tab_id}")])
    return rows


def _ownersrv_terminal_keyboard(terminals: dict, active_id: str, tab_id: str = None,
                                 busy: bool = False, show_download: bool = False,
                                 quick_open: bool = False, quick_manage: bool = False,
                                 wide: bool = False) -> InlineKeyboardMarkup:
    rows = _ownersrv_tabs_keyboard_rows(terminals, active_id)
    new_window_btn = (
        InlineKeyboardButton("▣ New Window", callback_data=OWNERSRV_TAB_NEW_CB)
        if len(terminals) < OWNERSRV_MAX_TABS else None
    )
    if tab_id:
        combo_row = ([new_window_btn] if new_window_btn else []) + [
            InlineKeyboardButton("▥ SFTP", callback_data=OWNERSRV_FILES_START_CB)
        ]
        rows.append(combo_row)
        if quick_manage:
            rows.extend(_ownersrv_quick_manage_keyboard_rows(tab_id))
        elif quick_open:
            rows.extend(_ownersrv_quick_keyboard_rows(tab_id))
        else:
            if busy:
                rows.append([
                    InlineKeyboardButton("✓ y", callback_data=f"{OWNERSRV_TERMINAL_YES_CB_PREFIX}{tab_id}"),
                    InlineKeyboardButton("⏎ Enter", callback_data=f"{OWNERSRV_TERMINAL_ENTER_CB_PREFIX}{tab_id}"),
                    InlineKeyboardButton("⊘ n", callback_data=f"{OWNERSRV_TERMINAL_NO_CB_PREFIX}{tab_id}"),
                ])
                rows.append([InlineKeyboardButton("⊘ Cancel", callback_data=f"{OWNERSRV_TERMINAL_CANCEL_CB_PREFIX}{tab_id}")])
            if show_download:
                rows.append([InlineKeyboardButton("▤ Download full output", callback_data=f"{OWNERSRV_TERMINAL_DOWNLOAD_CB_PREFIX}{tab_id}")])
            term = terminals.get(tab_id) if terminals else None
            shell_alive = busy or owner_server.is_local_shell_alive(term.get("pid") if term else None)
            if shell_alive:
                rows.append([
                    InlineKeyboardButton("⚡ Quick", callback_data=f"{OWNERSRV_QUICK_MENU_CB_PREFIX}{tab_id}"),
                    InlineKeyboardButton(
                        f"⇄ Resize: {'Wide' if wide else 'Normal'}",
                        callback_data=f"{OWNERSRV_RESIZE_CB_PREFIX}{tab_id}",
                    ),
                ])
            else:
                rows.append([InlineKeyboardButton("⚡ Quick", callback_data=f"{OWNERSRV_QUICK_MENU_CB_PREFIX}{tab_id}")])
    elif new_window_btn:
        rows.append([new_window_btn])
    return InlineKeyboardMarkup(rows)


def _format_ownersrv_terminal(label: str, command: str, output: str, status_line: str) -> str:
    header = f"▣ *Owner Server Terminal* — `{label}`"
    if not command:
        return f"{header}\n{status_line}"
    body = _strip_ansi(output)
    body = body[-OWNERSRV_TERM_BODY_CHARS:]
    if len(output) > OWNERSRV_TERM_BODY_CHARS:
        body = "…(older output trimmed - use ▤ Download full output below for everything)…\n" + body
    body = body.rstrip("\n")
    header += f"\n`$ {command}`"
    return f"{header}\n```\n{body}\n\n```\n{status_line}"


def _cancel_ownersrv_idle_timeout(term: dict):
    job = term.get("idle_job") if term else None
    if job is not None:
        try:
            job.schedule_removal()
        except Exception:
            pass


def _schedule_ownersrv_idle_timeout(context: ContextTypes.DEFAULT_TYPE, chat_id: int, user_id: int, tab_id: str):
    terminals = context.user_data.get("ownersrv_terminals")
    term = terminals.get(tab_id) if terminals else None
    if term is None or context.job_queue is None:
        return
    _cancel_ownersrv_idle_timeout(term)
    term["idle_job"] = context.job_queue.run_once(
        _ownersrv_idle_timeout_tick, when=OWNERSRV_TERM_IDLE_TIMEOUT_MINUTES * 60,
        chat_id=chat_id, user_id=user_id, data={"tab_id": tab_id},
        name=f"ownersrv_idle_{user_id}_{tab_id}",
    )


def _close_ownersrv_tab(context: ContextTypes.DEFAULT_TYPE, tab_id: str):
    terminals = context.user_data.get("ownersrv_terminals", {})
    term = terminals.pop(tab_id, None)
    if term is None:
        return
    _cancel_ownersrv_idle_timeout(term)
    owner_server.close_local_shell(term.get("pid"), term.get("master_fd"))


def _close_all_ownersrv_terminals(context: ContextTypes.DEFAULT_TYPE):
    terminals = context.user_data.pop("ownersrv_terminals", {})
    for tab_id, term in terminals.items():
        _cancel_ownersrv_idle_timeout(term)
        owner_server.close_local_shell(term.get("pid"), term.get("master_fd"))
    context.user_data.pop("ownersrv_active_terminal", None)


async def _ownersrv_idle_timeout_tick(context: ContextTypes.DEFAULT_TYPE):
    job = context.job
    tab_id = job.data["tab_id"]
    terminals = context.user_data.get("ownersrv_terminals", {})
    if tab_id not in terminals:
        return
    label = _ownersrv_tab_label(terminals, tab_id)
    _close_ownersrv_tab(context, tab_id)
    terminals = context.user_data.get("ownersrv_terminals", {})

    was_active = context.user_data.get("ownersrv_active_terminal") == tab_id
    if was_active:
        context.user_data.pop("ownersrv_active_terminal", None)
        if terminals:
            context.user_data["ownersrv_active_terminal"] = next(iter(terminals))

    try:
        await context.bot.send_message(
            chat_id=job.chat_id,
            text=f"⏱ \"{label}\" was auto-closed after {OWNERSRV_TERM_IDLE_TIMEOUT_MINUTES} minutes of inactivity.",
        )
    except Exception:
        pass

    if was_active and terminals:
        new_active = context.user_data["ownersrv_active_terminal"]
        await _send_ownersrv_tab_state(context.bot, job.chat_id, terminals, new_active)


async def _clear_ownersrv_tab_state_msg(bot, term: dict):
    if term is None:
        return
    old = term.pop("tab_state_msg", None)
    if old is None:
        return
    try:
        await bot.delete_message(chat_id=old[0], message_id=old[1])
    except Exception:
        pass


async def _send_ownersrv_tab_state(bot, chat_id: int, terminals: dict, active_id: str):
    if not terminals or active_id not in terminals:
        return
    term = terminals[active_id]
    await _clear_ownersrv_tab_state_msg(bot, term)
    label = _ownersrv_tab_label(terminals, active_id)
    busy = bool(term.get("busy"))
    term_state = term.get("term_state")
    if term_state:
        status = term_state["status"] if not busy else f"{_OSF_SPINNER_FRAMES[0]} Running…"
        text = _format_ownersrv_terminal(label, term_state["command"], term_state["output"], status)
        show_download = len(term_state["output"]) > OWNERSRV_TERM_BODY_CHARS
        keyboard = _ownersrv_terminal_keyboard(
            terminals, active_id, active_id, busy=busy, show_download=show_download, wide=term.get("wide", False),
        )
    else:
        status = "◉ Ready — send a command." if not busy else f"{_OSF_SPINNER_FRAMES[0]} Running…"
        text = _format_ownersrv_terminal(label, "", "", status)
        keyboard = _ownersrv_terminal_keyboard(terminals, active_id, active_id, busy=busy)
    msg = await bot.send_message(chat_id=chat_id, text=text, parse_mode="Markdown", reply_markup=keyboard)
    term["tab_state_msg"] = (msg.chat_id, msg.message_id)


async def _open_ownersrv_tab(context: ContextTypes.DEFAULT_TYPE) -> str:
    pid, master_fd = await asyncio.to_thread(owner_server.open_local_shell)
    tab_id = uuid.uuid4().hex[:8]
    terminals = context.user_data.setdefault("ownersrv_terminals", {})
    terminals[tab_id] = {
        "pid": pid, "master_fd": master_fd, "busy": False,
        "handle_box": {"handle": None}, "term_state": None, "tab_state_msg": None,
        "idle_job": None, "wide": False, "quick_open": False,
    }
    return tab_id


async def admin_ownersrv_terminal_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not is_admin(query.from_user.id):
        await query.answer("⊘ You do not have admin access.", show_alert=True)
        return ConversationHandler.END
    await query.answer()

    terminals = context.user_data.get("ownersrv_terminals")
    if terminals:
        active_id = context.user_data.get("ownersrv_active_terminal") or next(iter(terminals))
        context.user_data["ownersrv_active_terminal"] = active_id
        await query.edit_message_text(f"▣ Resuming {len(terminals)} open terminal tab(s)…")
        _schedule_ownersrv_idle_timeout(context, query.message.chat_id, query.from_user.id, active_id)
        await _send_ownersrv_tab_state(context.bot, query.message.chat_id, terminals, active_id)
        return ADMIN_OWNERSRV_CMD

    tab_id = await _open_ownersrv_tab(context)
    context.user_data["ownersrv_active_terminal"] = tab_id
    terminals = context.user_data["ownersrv_terminals"]

    _schedule_ownersrv_idle_timeout(context, query.message.chat_id, query.from_user.id, tab_id)
    await query.edit_message_text(
        _format_ownersrv_terminal(
            _ownersrv_tab_label(terminals, tab_id), "", "",
            "◉ Ready — send a command. This is a real persistent shell: "
            "`cd`, env vars, and background jobs all carry over between messages.",
        ),
        parse_mode="Markdown",
        reply_markup=_ownersrv_terminal_keyboard(terminals, tab_id),
    )
    return ADMIN_OWNERSRV_CMD


async def admin_ownersrv_tab_new(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not is_admin(query.from_user.id):
        await query.answer("⊘ You do not have admin access.", show_alert=True)
        return ADMIN_OWNERSRV_CMD
    terminals = context.user_data.get("ownersrv_terminals", {})
    if len(terminals) >= OWNERSRV_MAX_TABS:
        await query.answer(f"⚠ Up to {OWNERSRV_MAX_TABS} terminal tabs at once. Close one first.", show_alert=True)
        return ADMIN_OWNERSRV_CMD
    await query.answer()
    tab_id = await _open_ownersrv_tab(context)
    context.user_data["ownersrv_active_terminal"] = tab_id
    terminals = context.user_data["ownersrv_terminals"]
    _schedule_ownersrv_idle_timeout(context, query.message.chat_id, query.from_user.id, tab_id)
    await _send_ownersrv_tab_state(context.bot, query.message.chat_id, terminals, tab_id)
    return ADMIN_OWNERSRV_CMD


async def admin_ownersrv_tab_switch(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not is_admin(query.from_user.id):
        await query.answer("⊘ You do not have admin access.", show_alert=True)
        return ADMIN_OWNERSRV_CMD
    tab_id = query.data.replace(OWNERSRV_TAB_SWITCH_CB_PREFIX, "", 1)
    terminals = context.user_data.get("ownersrv_terminals", {})
    if tab_id not in terminals:
        await query.answer("That tab is no longer open.", show_alert=True)
        return ADMIN_OWNERSRV_CMD
    await query.answer()
    context.user_data["ownersrv_active_terminal"] = tab_id
    _schedule_ownersrv_idle_timeout(context, query.message.chat_id, query.from_user.id, tab_id)
    await _send_ownersrv_tab_state(context.bot, query.message.chat_id, terminals, tab_id)
    return ADMIN_OWNERSRV_CMD


async def admin_ownersrv_tab_close(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not is_admin(query.from_user.id):
        await query.answer("⊘ You do not have admin access.", show_alert=True)
        return ADMIN_OWNERSRV_CMD
    tab_id = query.data.replace(OWNERSRV_TAB_CLOSE_CB_PREFIX, "", 1)
    terminals = context.user_data.get("ownersrv_terminals", {})
    if tab_id not in terminals:
        await query.answer("That tab is already closed.", show_alert=True)
        return ADMIN_OWNERSRV_CMD
    label = _ownersrv_tab_label(terminals, tab_id)
    was_active = context.user_data.get("ownersrv_active_terminal") == tab_id
    _close_ownersrv_tab(context, tab_id)
    terminals = context.user_data.get("ownersrv_terminals", {})

    if was_active:
        context.user_data.pop("ownersrv_active_terminal", None)
        if terminals:
            context.user_data["ownersrv_active_terminal"] = next(iter(terminals))

    if not terminals:
        await query.answer(f"▪ Closed \"{label}\". All terminal tabs are now closed.")
        await _send_owner_server_card(query)
        return ConversationHandler.END

    await query.answer(f"▪ Closed \"{label}\".")
    if was_active:
        new_active = context.user_data["ownersrv_active_terminal"]
        _schedule_ownersrv_idle_timeout(context, query.message.chat_id, query.from_user.id, new_active)
        await _send_ownersrv_tab_state(context.bot, query.message.chat_id, terminals, new_active)
    else:
        active_id = context.user_data.get("ownersrv_active_terminal")
        term = terminals.get(active_id)
        term_state = term.get("term_state") if term else None
        busy = bool(term.get("busy")) if term else False
        show_download = bool(term_state) and len(term_state["output"]) > OWNERSRV_TERM_BODY_CHARS
        try:
            await query.edit_message_reply_markup(
                reply_markup=_ownersrv_terminal_keyboard(
                    terminals, active_id, active_id if term_state else None, busy=busy, show_download=show_download,
                    wide=term.get("wide", False) if term else False,
                )
            )
        except BadRequest:
            pass
    return ADMIN_OWNERSRV_CMD


async def _refresh_ownersrv_terminal_keyboard(query, context: ContextTypes.DEFAULT_TYPE, tab_id: str, term: dict):
    terminals = context.user_data.get("ownersrv_terminals", {})
    term_state = term.get("term_state")
    busy = bool(term.get("busy"))
    show_download = bool(term_state) and len(term_state["output"]) > OWNERSRV_TERM_BODY_CHARS
    try:
        await query.edit_message_reply_markup(
            reply_markup=_ownersrv_terminal_keyboard(
                terminals, context.user_data.get("ownersrv_active_terminal"), tab_id,
                busy=busy, show_download=show_download,
                quick_open=term.get("quick_open", False), quick_manage=term.get("quick_manage", False),
                wide=term.get("wide", False),
            )
        )
    except BadRequest as e:
        if "not modified" not in str(e).lower():
            raise


async def admin_ownersrv_quick_menu_open(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not is_admin(query.from_user.id):
        await query.answer("⊘ You do not have admin access.", show_alert=True)
        return ADMIN_OWNERSRV_CMD
    tab_id = query.data.replace(OWNERSRV_QUICK_MENU_CB_PREFIX, "", 1)
    terminals = context.user_data.get("ownersrv_terminals", {})
    term = terminals.get(tab_id)
    if term is None:
        await query.answer("That tab is no longer open.", show_alert=True)
        return ADMIN_OWNERSRV_CMD
    await query.answer()
    term["quick_open"] = True
    term["quick_manage"] = False
    _schedule_ownersrv_idle_timeout(context, query.message.chat_id, query.from_user.id, tab_id)
    await _refresh_ownersrv_terminal_keyboard(query, context, tab_id, term)
    return ADMIN_OWNERSRV_CMD


async def admin_ownersrv_quick_menu_close(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not is_admin(query.from_user.id):
        await query.answer("⊘ You do not have admin access.", show_alert=True)
        return ADMIN_OWNERSRV_CMD
    tab_id = query.data.replace(OWNERSRV_QUICK_CLOSE_CB_PREFIX, "", 1)
    terminals = context.user_data.get("ownersrv_terminals", {})
    term = terminals.get(tab_id)
    if term is None:
        await query.answer("That tab is no longer open.", show_alert=True)
        return ADMIN_OWNERSRV_CMD
    await query.answer()
    term["quick_open"] = False
    term["quick_manage"] = False
    await _refresh_ownersrv_terminal_keyboard(query, context, tab_id, term)
    return ADMIN_OWNERSRV_CMD


async def admin_ownersrv_quick_manage_open(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not is_admin(query.from_user.id):
        await query.answer("⊘ You do not have admin access.", show_alert=True)
        return ADMIN_OWNERSRV_CMD
    tab_id = query.data.replace(OWNERSRV_QUICK_MANAGE_CB_PREFIX, "", 1)
    terminals = context.user_data.get("ownersrv_terminals", {})
    term = terminals.get(tab_id)
    if term is None:
        await query.answer("That tab is no longer open.", show_alert=True)
        return ADMIN_OWNERSRV_CMD
    await query.answer()
    term["quick_open"] = True
    term["quick_manage"] = True
    await _refresh_ownersrv_terminal_keyboard(query, context, tab_id, term)
    return ADMIN_OWNERSRV_CMD


async def admin_ownersrv_quick_del(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not is_admin(query.from_user.id):
        await query.answer("⊘ You do not have admin access.", show_alert=True)
        return ADMIN_OWNERSRV_CMD
    rest = query.data.replace(OWNERSRV_QUICK_DEL_CB_PREFIX, "", 1)
    idx_str, _, tab_id = rest.partition("_")
    terminals = context.user_data.get("ownersrv_terminals", {})
    term = terminals.get(tab_id)
    if term is None:
        await query.answer("That tab is no longer open.", show_alert=True)
        return ADMIN_OWNERSRV_CMD
    try:
        removed = bot_settings.remove_owner_quick_command(int(idx_str))
    except ValueError:
        removed = False
    await query.answer("✕ Removed." if removed else "Already removed.")
    if not bot_settings.get_owner_quick_commands():
        term["quick_manage"] = False
    await _refresh_ownersrv_terminal_keyboard(query, context, tab_id, term)
    return ADMIN_OWNERSRV_CMD


async def _cleanup_ownersrv_quickadd_messages(context: ContextTypes.DEFAULT_TYPE, chat_id: int, *message_ids):
    """Best-effort delete of the throwaway prompt/reply messages from the quick-add
    conversation, so it doesn't leave a trail behind once it's done."""
    for mid in message_ids:
        if not mid:
            continue
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=mid)
        except BadRequest as e:
            if "message to delete not found" not in str(e).lower():
                logger.debug(f"could not delete quickadd message {mid}: {e}")
        except Exception as e:
            logger.debug(f"could not delete quickadd message {mid}: {e}")


async def admin_ownersrv_quick_add_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not is_admin(query.from_user.id):
        await query.answer("⊘ You do not have admin access.", show_alert=True)
        return ADMIN_OWNERSRV_CMD
    tab_id = query.data.replace(OWNERSRV_QUICK_ADD_CB_PREFIX, "", 1)
    terminals = context.user_data.get("ownersrv_terminals", {})
    term = terminals.get(tab_id)
    if term is None:
        await query.answer("That tab is no longer open.", show_alert=True)
        return ADMIN_OWNERSRV_CMD
    await query.answer()
    context.user_data["ownersrv_quickadd_tab"] = tab_id
    # The terminal message currently on screen (the one the "+ Add command"
    # button was tapped on) - captured now because the label/cmd replies that
    # follow are plain text messages, not callbacks, so there's no `query`
    # left later to edit_message_reply_markup on directly.
    context.user_data["ownersrv_quickadd_terminal_msg"] = (query.message.chat_id, query.message.message_id)
    msg = await query.message.reply_text(
        "+ *Add quick command*\n\nSend the button label (e.g. `x-ui`):",
        parse_mode="Markdown", reply_markup=ReplyKeyboardMarkup([[CANCEL_BUTTON_TEXT]], resize_keyboard=True),
    )
    context.user_data["ownersrv_quickadd_prompt_msg_id"] = msg.message_id
    return ADMIN_OWNERSRV_QUICKADD_LABEL


async def admin_ownersrv_quick_add_label_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    label = (update.message.text or "").strip()
    if not label:
        await update.message.reply_text("Label can't be empty. Send the button label:")
        return ADMIN_OWNERSRV_QUICKADD_LABEL
    context.user_data["ownersrv_quickadd_label"] = label

    prev_prompt_id = context.user_data.pop("ownersrv_quickadd_prompt_msg_id", None)
    await _cleanup_ownersrv_quickadd_messages(
        context, update.effective_chat.id, prev_prompt_id, update.message.message_id,
    )

    msg = await update.message.reply_text(
        f"Label: `{label}`\n\nNow send the shell command to run when it's tapped (e.g. `x-ui status`):",
        parse_mode="Markdown",
    )
    context.user_data["ownersrv_quickadd_prompt_msg_id"] = msg.message_id
    return ADMIN_OWNERSRV_QUICKADD_CMD


async def admin_ownersrv_quick_add_cmd_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cmd = (update.message.text or "").strip()
    if not cmd:
        await update.message.reply_text("Command can't be empty. Send the shell command:")
        return ADMIN_OWNERSRV_QUICKADD_CMD
    label = context.user_data.pop("ownersrv_quickadd_label", cmd)
    tab_id = context.user_data.pop("ownersrv_quickadd_tab", None)
    terminal_msg = context.user_data.pop("ownersrv_quickadd_terminal_msg", None)

    prev_prompt_id = context.user_data.pop("ownersrv_quickadd_prompt_msg_id", None)
    await _cleanup_ownersrv_quickadd_messages(
        context, update.effective_chat.id, prev_prompt_id, update.message.message_id,
    )

    bot_settings.add_owner_quick_command(label, cmd)
    confirm_msg = await update.message.reply_text(
        f"✓ Added to the Quick menu: *{label}* → `{cmd}`",
        parse_mode="Markdown", reply_markup=ReplyKeyboardRemove(),
    )

    terminals = context.user_data.get("ownersrv_terminals", {})
    term = terminals.get(tab_id) if tab_id else None
    if term is not None and terminal_msg is not None:
        term["quick_open"] = True
        term["quick_manage"] = False
        try:
            await context.bot.edit_message_reply_markup(
                chat_id=terminal_msg[0], message_id=terminal_msg[1],
                reply_markup=_ownersrv_terminal_keyboard(
                    terminals, context.user_data.get("ownersrv_active_terminal"), tab_id,
                    quick_open=True, quick_manage=False, wide=term.get("wide", False),
                ),
            )
        except BadRequest as e:
            if "not modified" not in str(e).lower():
                logger.warning(f"could not refresh quick menu after add: {e}")
        except Exception as e:
            logger.warning(f"could not refresh quick menu after add: {e}")

    # The terminal keyboard now shows the new button - the confirmation text has
    # done its job, so clear it too instead of leaving it sitting in the chat.
    await _cleanup_ownersrv_quickadd_messages(context, update.effective_chat.id, confirm_msg.message_id)
    return ADMIN_OWNERSRV_CMD


async def admin_ownersrv_quick_run(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not is_admin(query.from_user.id):
        await query.answer("⊘ You do not have admin access.", show_alert=True)
        return ADMIN_OWNERSRV_CMD
    rest = query.data.replace(OWNERSRV_QUICK_RUN_CB_PREFIX, "", 1)
    idx_str, _, tab_id = rest.partition("_")
    terminals = context.user_data.get("ownersrv_terminals", {})
    term = terminals.get(tab_id)
    if term is None:
        await query.answer("That tab is no longer open.", show_alert=True)
        return ADMIN_OWNERSRV_CMD
    try:
        command_text = _ownersrv_all_quick_commands()[int(idx_str)][1]
    except (ValueError, IndexError):
        await query.answer("Unknown quick command.", show_alert=True)
        return ADMIN_OWNERSRV_CMD
    if term.get("busy"):
        await query.answer("⏳ This tab is already running something.", show_alert=True)
        return ADMIN_OWNERSRV_CMD

    await query.answer(f"▶ {command_text}")
    term["quick_open"] = False
    context.user_data["ownersrv_active_terminal"] = tab_id
    _schedule_ownersrv_idle_timeout(context, query.message.chat_id, query.from_user.id, tab_id)

    if not owner_server.is_local_shell_alive(term.get("pid")):
        owner_server.close_local_shell(term.get("pid"), term.get("master_fd"))
        width, height = OWNERSRV_TERM_WIDE_SIZE if term.get("wide") else OWNERSRV_TERM_NORMAL_SIZE
        pid, master_fd = await asyncio.to_thread(owner_server.open_local_shell, width, height)
        term["pid"], term["master_fd"] = pid, master_fd

    term["busy"] = True
    logger.warning(f"Admin {query.from_user.id} ran an owner-server quick command ({_ownersrv_tab_label(terminals, tab_id)}): {command_text}")
    asyncio.create_task(_stream_ownersrv_command(context, query.message.chat_id, query.from_user.id, tab_id, command_text))
    return ADMIN_OWNERSRV_CMD


async def admin_ownersrv_resize(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not is_admin(query.from_user.id):
        await query.answer("⊘ You do not have admin access.", show_alert=True)
        return ADMIN_OWNERSRV_CMD
    tab_id = query.data.replace(OWNERSRV_RESIZE_CB_PREFIX, "", 1)
    terminals = context.user_data.get("ownersrv_terminals", {})
    term = terminals.get(tab_id)
    if term is None:
        await query.answer("That tab is no longer open.", show_alert=True)
        return ADMIN_OWNERSRV_CMD

    if not owner_server.is_local_shell_alive(term.get("pid")):
        await query.answer("Shell isn't running right now — send a command to start a fresh one first.", show_alert=True)
        return ADMIN_OWNERSRV_CMD

    term["wide"] = not term.get("wide", False)
    width, height = OWNERSRV_TERM_WIDE_SIZE if term["wide"] else OWNERSRV_TERM_NORMAL_SIZE
    master_fd = term.get("master_fd")
    if master_fd is not None:
        try:
            owner_server.resize_local_shell(master_fd, width, height)
        except Exception:
            logger.debug("owner-server terminal resize failed", exc_info=True)
    _schedule_ownersrv_idle_timeout(context, query.message.chat_id, query.from_user.id, tab_id)
    await query.answer(f"Window resized to {width}×{height}.")
    await _refresh_ownersrv_terminal_keyboard(query, context, tab_id, term)
    return ADMIN_OWNERSRV_CMD


async def admin_ownersrv_noop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    return ADMIN_OWNERSRV_CMD


async def admin_ownersrv_terminal_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return ConversationHandler.END
    text = (update.message.text or "").strip()
    if not text:
        return ADMIN_OWNERSRV_CMD

    terminals = context.user_data.get("ownersrv_terminals")
    active_id = context.user_data.get("ownersrv_active_terminal")
    if not terminals or active_id not in terminals:
        tab_id = await _open_ownersrv_tab(context)
        context.user_data["ownersrv_active_terminal"] = tab_id
        terminals = context.user_data["ownersrv_terminals"]
        active_id = tab_id

    term = terminals[active_id]
    _schedule_ownersrv_idle_timeout(context, update.effective_chat.id, update.effective_user.id, active_id)

    if term.get("busy"):
        handle = term["handle_box"].get("handle")
        if handle is None:
            await update.message.reply_text("⏳ Still starting up — try again in a second.")
            return ADMIN_OWNERSRV_CMD
        if not handle.send_raw(text + "\n"):
            await update.message.reply_text("⚠ Could not send that — the shell session may have died.")
        return ADMIN_OWNERSRV_CMD

    if not owner_server.is_local_shell_alive(term.get("pid")):
        owner_server.close_local_shell(term.get("pid"), term.get("master_fd"))
        width, height = OWNERSRV_TERM_WIDE_SIZE if term.get("wide") else OWNERSRV_TERM_NORMAL_SIZE
        pid, master_fd = await asyncio.to_thread(owner_server.open_local_shell, width, height)
        term["pid"], term["master_fd"] = pid, master_fd

    term["busy"] = True
    logger.warning(f"Admin {update.effective_user.id} ran an owner-server terminal command ({_ownersrv_tab_label(terminals, active_id)}): {text}")
    asyncio.create_task(_stream_ownersrv_command(context, update.effective_chat.id, update.effective_user.id, active_id, text))
    return ADMIN_OWNERSRV_CMD


async def _stream_ownersrv_command(context: ContextTypes.DEFAULT_TYPE, chat_id: int, user_id: int, tab_id: str, command_text: str):
    terminals = context.user_data.get("ownersrv_terminals")
    term = terminals.get(tab_id) if terminals else None
    if term is None:
        return

    master_fd = term["master_fd"]
    handle_box = term["handle_box"]
    handle_box["handle"] = None
    chunk_queue = queue_mod.Queue()

    def on_chunk(handle, chunk_text):
        handle_box["handle"] = handle
        if chunk_text:
            chunk_queue.put(chunk_text)

    label = _ownersrv_tab_label(terminals, tab_id)
    spin_frame = 0
    try:
        initial_markup = _ownersrv_terminal_keyboard(
            terminals, context.user_data.get("ownersrv_active_terminal"), tab_id, busy=True,
        )
        msg = await context.bot.send_message(
            chat_id=chat_id,
            text=_format_ownersrv_terminal(label, command_text, "", f"{_OSF_SPINNER_FRAMES[0]} Running…"),
            parse_mode="Markdown",
            reply_markup=initial_markup,
        )
    except Exception as e:
        logger.warning(f"admin: failed to send owner-server terminal message: {e}")
        term["busy"] = False
        return

    task = asyncio.create_task(
        asyncio.to_thread(
            owner_server.run_local_shell_input,
            master_fd, command_text, owner_server.LOCAL_SHELL_CMD_TIMEOUT, on_chunk,
        )
    )

    output_so_far = ""
    last_edit_at = 0.0
    last_sent_text = None
    last_sent_markup = initial_markup
    while not task.done():
        await asyncio.sleep(0.3)

        terminals = context.user_data.get("ownersrv_terminals")
        if not terminals or tab_id not in terminals:
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
        if got_new and now - last_edit_at >= OWNERSRV_TERM_EDIT_INTERVAL:
            spin_frame += 1
            frame = _OSF_SPINNER_FRAMES[spin_frame % len(_OSF_SPINNER_FRAMES)]
            status = f"{frame} Cancelling…" if (handle_box.get("handle") and handle_box["handle"].cancelled) else f"{frame} Running…"
            display_output = owner_server.strip_shell_marker(output_so_far)
            new_text = _format_ownersrv_terminal(label, command_text, display_output, status)
            if new_text != last_sent_text:
                try:
                    active_id = context.user_data.get("ownersrv_active_terminal")
                    kb_terminals = context.user_data.get("ownersrv_terminals") or {tab_id: term}
                    term_now = kb_terminals.get(tab_id)
                    # editMessageText clears the existing keyboard if reply_markup
                    # is omitted (unlike editMessageReplyMarkup) - it does NOT
                    # keep the previous one, so it must be sent on every edit.
                    # Passing along quick_open/quick_manage keeps the Quick menu
                    # (if the user has it open on this tab) from being replaced
                    # back to the normal busy keyboard on the next tick.
                    new_markup = _ownersrv_terminal_keyboard(
                        kb_terminals, active_id, tab_id, busy=True,
                        quick_open=bool(term_now.get("quick_open")) if term_now else False,
                        quick_manage=bool(term_now.get("quick_manage")) if term_now else False,
                    )
                    await msg.edit_text(
                        new_text, parse_mode="Markdown",
                        reply_markup=new_markup,
                    )
                    last_sent_markup = new_markup
                    last_sent_text = new_text
                except BadRequest as e:
                    if "not modified" not in str(e).lower():
                        logger.debug(f"owner-server terminal live edit failed: {e}")
                except Exception as e:
                    logger.debug(f"owner-server terminal live edit failed: {e}")
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

    terminals = context.user_data.get("ownersrv_terminals")
    term = terminals.get(tab_id) if terminals else None
    if term is not None:
        term["busy"] = False
        term["handle_box"]["handle"] = None

    if result.get("shell_exited"):
        _close_all_ownersrv_terminals(context)
        try:
            snap = owner_server.snapshot()
            card_text = owner_server.format_snapshot_text(snap)
        except Exception as e:
            logger.error(f"owner server snapshot failed after shell exit: {e}")
            card_text = f"▪ Shell process exited.\n\n✕ Could not read host stats: {e}"
        try:
            await msg.edit_text(card_text, parse_mode="Markdown", reply_markup=_owner_server_keyboard())
        except BadRequest as e:
            if "not modified" not in str(e).lower():
                logger.debug(f"owner-server terminal exit-card edit failed: {e}")
        except Exception as e:
            logger.debug(f"owner-server terminal exit-card edit failed: {e}")
        return

    if result.get("error"):
        status = f"✕ Execution error: {result['error']}"
    elif result.get("cancelled"):
        status = "⊘ Cancelled. Shell is still open — send the next command."
    elif result.get("timed_out"):
        status = f"⏱ No prompt back after {owner_server.LOCAL_SHELL_CMD_TIMEOUT // 3600}h — still running in the background."
    else:
        status = "✓ Ready — send the next command."

    final_output = result.get("output")
    if final_output is None:
        final_output = owner_server.strip_shell_marker(output_so_far)

    if term is not None:
        term["term_state"] = {"command": command_text, "output": final_output, "status": status}

    final_text = _format_ownersrv_terminal(label, command_text, final_output, status)
    show_download = len(final_output) > OWNERSRV_TERM_BODY_CHARS
    try:
        active_id = context.user_data.get("ownersrv_active_terminal")
        kb_terminals = context.user_data.get("ownersrv_terminals") or ({tab_id: term} if term else {})
        await msg.edit_text(
            final_text, parse_mode="Markdown",
            reply_markup=_ownersrv_terminal_keyboard(
                kb_terminals, active_id, tab_id, busy=False, show_download=show_download,
                wide=term.get("wide", False) if term else False,
            ),
        )
    except BadRequest as e:
        if "not modified" not in str(e).lower():
            logger.debug(f"owner-server terminal final edit failed: {e}")
    except Exception as e:
        logger.debug(f"owner-server terminal final edit failed: {e}")


async def admin_ownersrv_terminal_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not is_admin(query.from_user.id):
        await query.answer("⊘ You do not have admin access.", show_alert=True)
        return ADMIN_OWNERSRV_CMD
    tab_id = query.data.replace(OWNERSRV_TERMINAL_CANCEL_CB_PREFIX, "", 1)
    terminals = context.user_data.get("ownersrv_terminals", {})
    term = terminals.get(tab_id)
    handle = term["handle_box"].get("handle") if term else None
    if handle is None:
        await query.answer("Nothing is running right now.", show_alert=True)
        return ADMIN_OWNERSRV_CMD
    _schedule_ownersrv_idle_timeout(context, query.message.chat_id, query.from_user.id, tab_id)
    handle.cancel()
    await query.answer("Sent Ctrl-C.")
    return ADMIN_OWNERSRV_CMD


async def _admin_ownersrv_terminal_send_raw(update: Update, context: ContextTypes.DEFAULT_TYPE, cb_prefix: str, data: str, label: str):
    query = update.callback_query
    if not is_admin(query.from_user.id):
        await query.answer("⊘ You do not have admin access.", show_alert=True)
        return ADMIN_OWNERSRV_CMD
    tab_id = query.data.replace(cb_prefix, "", 1)
    terminals = context.user_data.get("ownersrv_terminals", {})
    term = terminals.get(tab_id)
    handle = term["handle_box"].get("handle") if term else None
    if handle is None:
        await query.answer("Nothing is running right now.", show_alert=True)
        return ADMIN_OWNERSRV_CMD
    _schedule_ownersrv_idle_timeout(context, query.message.chat_id, query.from_user.id, tab_id)
    if handle.send_raw(data):
        await query.answer(f"{label} sent.")
    else:
        await query.answer("⚠ Could not send that - the session may have dropped.")
    return ADMIN_OWNERSRV_CMD


async def admin_ownersrv_terminal_enter(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await _admin_ownersrv_terminal_send_raw(update, context, OWNERSRV_TERMINAL_ENTER_CB_PREFIX, "\n", "⏎ Enter")


async def admin_ownersrv_terminal_yes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await _admin_ownersrv_terminal_send_raw(update, context, OWNERSRV_TERMINAL_YES_CB_PREFIX, "y\n", "✓ y")


async def admin_ownersrv_terminal_no(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await _admin_ownersrv_terminal_send_raw(update, context, OWNERSRV_TERMINAL_NO_CB_PREFIX, "n\n", "⊘ n")


async def admin_ownersrv_terminal_download(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not is_admin(query.from_user.id):
        await query.answer("⊘ You do not have admin access.", show_alert=True)
        return ADMIN_OWNERSRV_CMD
    tab_id = query.data.replace(OWNERSRV_TERMINAL_DOWNLOAD_CB_PREFIX, "", 1)
    terminals = context.user_data.get("ownersrv_terminals", {})
    term = terminals.get(tab_id)
    term_state = term.get("term_state") if term else None
    if not term_state or not term_state.get("output"):
        await query.answer("No output to download for that tab.", show_alert=True)
        return ADMIN_OWNERSRV_CMD

    await query.answer("⏳ Preparing file...")
    output = _strip_ansi(term_state["output"])[-OWNERSRV_TERM_DOWNLOAD_TAIL_CHARS:]
    buf = io.BytesIO(output.encode("utf-8", errors="replace"))
    buf.name = "owner-terminal-output.txt"
    try:
        await context.bot.send_document(
            chat_id=query.message.chat_id, document=buf, filename="owner-terminal-output.txt",
            caption=f"Full output of: `{term_state['command']}`"[:1024], parse_mode="Markdown",
        )
    except Exception as e:
        await context.bot.send_message(chat_id=query.message.chat_id, text=f"✕ Could not send output file: {e}")
    return ADMIN_OWNERSRV_CMD


async def admin_ownersrv_terminal_exit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not is_admin(query.from_user.id):
        await query.answer("⊘ You do not have admin access.", show_alert=True)
        return ConversationHandler.END
    await query.answer()
    _close_all_ownersrv_terminals(context)
    await _send_owner_server_card(query)
    return ConversationHandler.END


def _osf_human_size(n: int) -> str:
    size = float(n or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f}{unit}" if unit == "B" else f"{size:.1f}{unit}"
        size /= 1024


_OSF_PROGRESS_EDIT_INTERVAL = 1.5


def _osf_progress_bar(pct: float, width: int = 12) -> str:
    filled = min(width, max(0, round(pct / 100 * width)))
    return "[" + "■" * filled + "□" * (width - filled) + "]"


async def _run_local_download_with_progress(dest_dir: str, url: str, timeout: int, max_bytes: int, status_msg):
    progress = {"done": 0, "total": None}

    def _on_chunk(done, total):
        progress["done"] = done
        progress["total"] = total

    task = asyncio.create_task(asyncio.to_thread(
        owner_server.local_download_url, dest_dir, url, timeout, max_bytes, _on_chunk,
    ))
    last_text = None
    while not task.done():
        await asyncio.sleep(_OSF_PROGRESS_EDIT_INTERVAL)
        done, total = progress["done"], progress["total"]
        if total:
            pct = min(100.0, done / total * 100)
            text = f"⏳ Downloading…\n{_osf_progress_bar(pct)}  {pct:.0f}%  ({_osf_human_size(done)}/{_osf_human_size(total)})"
        else:
            text = f"⏳ Downloading…\n{_osf_progress_bar(0)}  0%  ({_osf_human_size(done)})"
        if text != last_text:
            try:
                await status_msg.edit_text(text)
                last_text = text
            except Exception:
                pass
    try:
        dest_path = await task
    except Exception as e:
        return None, str(e)[:300]
    return dest_path, None


_OSF_SPINNER_FRAMES = ["◐", "◓", "◑", "◒"]
_OSF_SPINNER_EDIT_INTERVAL = 1.5


async def _osf_run_with_spinner(func, *args, label: str, query, timeout: int):
    start = time.monotonic()
    task = asyncio.create_task(asyncio.wait_for(asyncio.to_thread(func, *args), timeout=timeout))
    frame = 0
    last_text = None
    while not task.done():
        await asyncio.sleep(_OSF_SPINNER_EDIT_INTERVAL)
        elapsed = int(time.monotonic() - start)
        text = f"{_OSF_SPINNER_FRAMES[frame % len(_OSF_SPINNER_FRAMES)]} {label}  ({elapsed}s)"
        frame += 1
        if text != last_text:
            try:
                await query.edit_message_text(text)
                last_text = text
            except Exception:
                pass
    try:
        result = await task
    except asyncio.TimeoutError:
        return None, True
    except Exception as e:
        logger.warning(f"owner-server maintenance call failed: {e}")
        return e, False
    return result, False


async def _osf_safe_edit(status_msg, context, chat_id, text, **kwargs):
    try:
        await status_msg.edit_text(text, **kwargs)
    except BadRequest as e:
        logger.warning(f"owner-server files: couldn't edit status message ({e}); sending a new one instead")
        try:
            await context.bot.send_message(chat_id=chat_id, text=text, **kwargs)
        except BadRequest as e2:
            logger.warning(f"owner-server files: fallback send_message also failed: {e2}")


def _osf_status_line(fb: dict) -> str:
    count = len(fb["entries"])
    shown = min(count, OWNERSRV_FILES_MAX_LIST_ENTRIES)
    counter = f"{shown} of {count} shown" if count > shown else f"{count} item{'s' if count != 1 else ''}"
    if fb.get("select_mode"):
        n = len(fb.get("selected") or ())
        return f"✓ _{n} selected — tap items to toggle_"
    if fb.get("awaiting_upload"):
        return "↑ _Waiting for a file…_"
    if fb.get("awaiting_url"):
        return "» _Waiting for a URL…_"
    if fb.get("awaiting_path"):
        return "✎ _Waiting for a path…_"
    if fb.get("awaiting_rename_idx") is not None:
        return "✎ _Waiting for a new name…_"
    if fb.get("awaiting_edit_idx") is not None:
        return "✎ _Waiting for new file content…_"
    if fb.get("awaiting_chmod_idx") is not None:
        return "⚿ _Waiting for a new mode (e.g. 755)…_"
    if fb.get("awaiting_chown_idx") is not None:
        return "◦ _Waiting for a new owner (e.g. www-data:www-data)…_"
    if fb.get("awaiting_mkdir"):
        return "＋ _Waiting for a folder name…_"
    return f"_{counter}_"


def _osf_hint(fb: dict) -> str:
    if fb.get("select_mode"):
        return "tap an item to select/deselect it"
    if fb.get("awaiting_upload") or fb.get("awaiting_url") or fb.get("awaiting_path") \
            or fb.get("awaiting_rename_idx") is not None or fb.get("awaiting_edit_idx") is not None \
            or fb.get("awaiting_chmod_idx") is not None or fb.get("awaiting_chown_idx") is not None \
            or fb.get("awaiting_mkdir"):
        return ""
    return "▢ open · ▤ download · ⚙ rename/edit/perms/archive/delete"


def _osf_text(fb: dict) -> str:
    hint = _osf_hint(fb)
    lines = [
        "▣ *Owner Server*  ·  Files",
        f"▢ `{fb['cwd']}`",
        _osf_status_line(fb),
    ]
    if hint:
        lines.append(f"_{hint}_")
    return "\n".join(lines)


def _osf_keyboard(fb: dict) -> InlineKeyboardMarkup:
    entries = fb["entries"]
    if fb.get("select_mode"):
        return _osf_select_keyboard(fb)

    rows = []
    for i, e in enumerate(entries[:OWNERSRV_FILES_MAX_LIST_ENTRIES]):
        actions_btn = InlineKeyboardButton("⚙", callback_data=f"{OWNERSRV_FILES_ACTIONS_CB_PREFIX}{i}")
        if e["is_dir"]:
            rows.append([
                InlineKeyboardButton(f"▢ {e['name']}", callback_data=f"{OWNERSRV_FILES_NAV_CB_PREFIX}{i}"),
                actions_btn,
            ])
        else:
            rows.append([
                InlineKeyboardButton(
                    f"▤ {e['name']}  ·  {_osf_human_size(e['size'])}",
                    callback_data=f"{OWNERSRV_FILES_DL_CB_PREFIX}{i}",
                ),
                actions_btn,
            ])

    if rows:
        cwd_label = fb["cwd"] if fb["cwd"] not in ("/", "") else "/"
        rows.append([InlineKeyboardButton(f"── {cwd_label} ──", callback_data="admin_noop")])

    nav_row = []
    if fb["cwd"] not in ("/", ""):
        nav_row.append(InlineKeyboardButton("← Up", callback_data=OWNERSRV_FILES_UP_CB))
    nav_row.append(InlineKeyboardButton("↻ Refresh", callback_data=OWNERSRV_FILES_REFRESH_CB))
    rows.append(nav_row)

    rows.append([
        InlineKeyboardButton("✎ Go to path", callback_data=OWNERSRV_FILES_GOTO_CB),
        InlineKeyboardButton("↑ Upload", callback_data=OWNERSRV_FILES_UPLOAD_HERE_CB),
        InlineKeyboardButton("» From URL", callback_data=OWNERSRV_FILES_URLDL_CB),
    ])
    rows.append([InlineKeyboardButton("＋ New folder", callback_data=OWNERSRV_FILES_MKDIR_CB)])
    if entries:
        rows.append([InlineKeyboardButton("✓ Select", callback_data=OWNERSRV_FILES_SELMODE_CB)])
    rows.append([InlineKeyboardButton("✕ Close", callback_data=OWNERSRV_FILES_CLOSE_CB)])
    return InlineKeyboardMarkup(rows)


def _osf_select_keyboard(fb: dict) -> InlineKeyboardMarkup:
    entries = fb["entries"]
    selected = fb.get("selected") or set()
    rows = []
    for i, e in enumerate(entries[:OWNERSRV_FILES_MAX_LIST_ENTRIES]):
        checked = e["name"] in selected
        icon = "▢" if e["is_dir"] else "▤"
        rows.append([InlineKeyboardButton(
            f"{'✓' if checked else '◯'} {icon} {e['name']}",
            callback_data=f"{OWNERSRV_FILES_SELTOGGLE_CB_PREFIX}{i}",
        )])
    n = len(selected)
    rows.append([
        InlineKeyboardButton(f"✕ Delete ({n})", callback_data=OWNERSRV_FILES_SELDELCONFIRM_CB),
        InlineKeyboardButton(f"▥ Compress ({n})", callback_data=OWNERSRV_FILES_SELCOMPRESS_CB),
    ])
    rows.append([InlineKeyboardButton("✓ Done selecting", callback_data=OWNERSRV_FILES_SELDONE_CB)])
    return InlineKeyboardMarkup(rows)


async def admin_noop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()


async def _osf_render(query, fb: dict):
    try:
        await query.edit_message_text(_osf_text(fb), reply_markup=_osf_keyboard(fb), parse_mode="Markdown")
    except BadRequest as e:
        if "not modified" not in str(e).lower():
            raise


def _osf_list(path: str):
    try:
        return owner_server.local_listdir(path), None
    except Exception as e:
        return None, str(e)[:300]


ADMIN_OWNERSRV_LAST_CWD_KEY = "admin_ownersrv_last_cwd"


def _osf_remember_cwd(context: ContextTypes.DEFAULT_TYPE, cwd: str):
    context.user_data[ADMIN_OWNERSRV_LAST_CWD_KEY] = cwd


def _osf_entry_at(fb: dict, idx: int):
    entries = fb["entries"]
    if idx < 0 or idx >= len(entries):
        return None
    return entries[idx]


def _osf_selected_entries(fb: dict) -> list:
    names = fb.get("selected") or set()
    out = []
    for e in fb["entries"]:
        if e["name"] in names:
            out.append({"path": owner_server.local_join(fb["cwd"], e["name"]), "is_dir": e["is_dir"], "name": e["name"]})
    return out


async def admin_ownersrv_files_selmode_on(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    fb = context.user_data.get("admin_ownersrv_fb")
    if not fb:
        await query.answer("File browser session expired.", show_alert=True)
        return ConversationHandler.END
    await query.answer()
    fb["select_mode"] = True
    fb["selected"] = set()
    await _osf_render(query, fb)
    return ADMIN_OWNERSRV_FILES


async def admin_ownersrv_files_selmode_off(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    fb = context.user_data.get("admin_ownersrv_fb")
    if not fb:
        await query.answer("File browser session expired.", show_alert=True)
        return ConversationHandler.END
    await query.answer()
    fb["select_mode"] = False
    fb["selected"] = set()
    await _osf_render(query, fb)
    return ADMIN_OWNERSRV_FILES


async def admin_ownersrv_files_seltoggle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    fb = context.user_data.get("admin_ownersrv_fb")
    if not fb:
        await query.answer("File browser session expired.", show_alert=True)
        return ConversationHandler.END
    idx = int(query.data.replace(OWNERSRV_FILES_SELTOGGLE_CB_PREFIX, "", 1))
    entry = _osf_entry_at(fb, idx)
    if entry is None:
        await query.answer("That item isn't listed anymore - try refreshing.", show_alert=True)
        return ADMIN_OWNERSRV_FILES
    selected = fb.setdefault("selected", set())
    if entry["name"] in selected:
        selected.discard(entry["name"])
    else:
        selected.add(entry["name"])
    await query.answer(f"{len(selected)} selected")
    await _osf_render(query, fb)
    return ADMIN_OWNERSRV_FILES


async def admin_ownersrv_files_seldelconfirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    fb = context.user_data.get("admin_ownersrv_fb")
    if not fb:
        await query.answer("File browser session expired.", show_alert=True)
        return ConversationHandler.END
    entries = _osf_selected_entries(fb)
    if not entries:
        await query.answer("Nothing selected.", show_alert=True)
        return ADMIN_OWNERSRV_FILES
    await query.answer()
    names = ", ".join(f"`{e['name']}`" for e in entries[:15])
    more = f" and {len(entries) - 15} more" if len(entries) > 15 else ""
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton(f"✓ Yes, delete {len(entries)}", callback_data=OWNERSRV_FILES_SELDELOK_CB),
         InlineKeyboardButton("✕ Cancel", callback_data=OWNERSRV_FILES_BACK_CB)],
    ])
    await query.edit_message_text(
        f"Delete {len(entries)} item(s)?{more}\n{names}\n\n⚠ Folders are deleted with everything inside them.",
        reply_markup=keyboard, parse_mode="Markdown",
    )
    return ADMIN_OWNERSRV_FILES


async def admin_ownersrv_files_seldelexecute(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    fb = context.user_data.get("admin_ownersrv_fb")
    if not fb:
        await query.answer("File browser session expired.", show_alert=True)
        return ConversationHandler.END
    entries = _osf_selected_entries(fb)
    if not entries:
        await query.answer("Nothing selected.", show_alert=True)
        return ADMIN_OWNERSRV_FILES

    await query.answer("⏳ Deleting...")
    result = await asyncio.to_thread(owner_server.local_delete_many, entries)
    fb["select_mode"] = False
    fb["selected"] = set()
    new_entries, list_err = _osf_list(fb["cwd"])
    if not list_err:
        fb["entries"] = new_entries

    popup = f"✓ Deleted {len(result['deleted'])} item(s)."
    if result["failed"]:
        popup += f" ✕ {len(result['failed'])} failed: " + ", ".join(n for n, _ in result["failed"][:5])
    try:
        await query.answer(popup[:200], show_alert=True)
    except BadRequest:
        pass
    await _osf_render(query, fb)
    return ADMIN_OWNERSRV_FILES


async def admin_ownersrv_files_selcompress(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    fb = context.user_data.get("admin_ownersrv_fb")
    if not fb:
        await query.answer("File browser session expired.", show_alert=True)
        return ConversationHandler.END
    entries = _osf_selected_entries(fb)
    if not entries:
        await query.answer("Nothing selected.", show_alert=True)
        return ADMIN_OWNERSRV_FILES

    await query.answer("⏳ Compressing...")
    archive_name = f"selected_{uuid.uuid4().hex[:6]}"
    try:
        out_path = await asyncio.to_thread(owner_server.local_compress_many, entries, fb["cwd"], archive_name, "zip")
        popup = f"✓ Created {os.path.basename(out_path)} ({len(entries)} item(s))."
        fb["select_mode"] = False
        fb["selected"] = set()
    except Exception as e:
        popup = f"✕ Compress failed: {str(e)[:180]}"

    new_entries, list_err = _osf_list(fb["cwd"])
    if not list_err:
        fb["entries"] = new_entries
    try:
        await query.answer(popup[:200], show_alert=True)
    except BadRequest:
        pass
    await _osf_render(query, fb)
    return ADMIN_OWNERSRV_FILES


async def admin_ownersrv_files_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not is_admin(query.from_user.id):
        await query.answer("⊘ You do not have admin access.", show_alert=True)
        return ConversationHandler.END

    await query.answer("⏳ Opening...")
    saved_cwd = context.user_data.get(ADMIN_OWNERSRV_LAST_CWD_KEY)
    cwd = saved_cwd or owner_server.local_home_dir()
    entries, err = _osf_list(cwd)
    if err and saved_cwd:
        cwd = owner_server.local_home_dir()
        entries, err = _osf_list(cwd)
    if err:
        await query.edit_message_text(f"✕ Could not list \"{cwd}\".\n{err}")
        return ConversationHandler.END
    _osf_remember_cwd(context, cwd)

    fb = {
        "cwd": cwd,
        "entries": entries,
        "chat_id": query.message.chat_id,
        "awaiting_upload": False,
        "awaiting_url": False,
        "awaiting_path": False,
        "awaiting_rename_idx": None,
        "awaiting_edit_idx": None,
        "awaiting_chmod_idx": None,
        "awaiting_chown_idx": None,
        "awaiting_mkdir": False,
        "tail_proc": None,
        "tail_msg_id": None,
        "select_mode": False,
        "selected": set(),
    }
    context.user_data["admin_ownersrv_fb"] = fb
    await _osf_render(query, fb)
    return ADMIN_OWNERSRV_FILES


async def admin_ownersrv_files_nav(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    fb = context.user_data.get("admin_ownersrv_fb")
    if not fb:
        await query.answer("File browser session expired.", show_alert=True)
        return ConversationHandler.END

    idx = int(query.data.replace(OWNERSRV_FILES_NAV_CB_PREFIX, "", 1))
    entry = _osf_entry_at(fb, idx)
    if entry is None or not entry["is_dir"]:
        await query.answer("That folder isn't listed anymore - try refreshing.", show_alert=True)
        return ADMIN_OWNERSRV_FILES

    new_cwd = owner_server.local_join(fb["cwd"], entry["name"])
    new_entries, err = _osf_list(new_cwd)
    if err:
        await query.answer(f"✕ {err}", show_alert=True)
        return ADMIN_OWNERSRV_FILES

    fb["cwd"], fb["entries"] = new_cwd, new_entries
    _osf_remember_cwd(context, new_cwd)
    await query.answer()
    await _osf_render(query, fb)
    return ADMIN_OWNERSRV_FILES


async def admin_ownersrv_files_up(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    fb = context.user_data.get("admin_ownersrv_fb")
    if not fb:
        await query.answer("File browser session expired.", show_alert=True)
        return ConversationHandler.END

    new_cwd = owner_server.local_parent(fb["cwd"])
    new_entries, err = _osf_list(new_cwd)
    if err:
        await query.answer(f"✕ {err}", show_alert=True)
        return ADMIN_OWNERSRV_FILES

    fb["cwd"], fb["entries"] = new_cwd, new_entries
    _osf_remember_cwd(context, new_cwd)
    await query.answer()
    await _osf_render(query, fb)
    return ADMIN_OWNERSRV_FILES


async def admin_ownersrv_files_refresh(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    fb = context.user_data.get("admin_ownersrv_fb")
    if not fb:
        await query.answer("File browser session expired.", show_alert=True)
        return ConversationHandler.END

    new_entries, err = _osf_list(fb["cwd"])
    if err:
        await query.answer(f"✕ {err}", show_alert=True)
        return ADMIN_OWNERSRV_FILES

    fb["entries"] = new_entries
    await query.answer("↻ Refreshed.")
    await _osf_render(query, fb)
    return ADMIN_OWNERSRV_FILES


async def admin_ownersrv_files_download(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    fb = context.user_data.get("admin_ownersrv_fb")
    if not fb:
        await query.answer("File browser session expired.", show_alert=True)
        return ConversationHandler.END

    idx = int(query.data.replace(OWNERSRV_FILES_DL_CB_PREFIX, "", 1))
    entry = _osf_entry_at(fb, idx)
    if entry is None or entry["is_dir"]:
        await query.answer("That file isn't listed anymore - try refreshing.", show_alert=True)
        return ADMIN_OWNERSRV_FILES

    if entry["size"] > owner_server.LOCAL_FILES_MAX_DOWNLOAD_BYTES:
        await query.answer(
            f"✕ \"{entry['name']}\" is {_osf_human_size(entry['size'])} - too large to send here "
            f"(limit {_osf_human_size(owner_server.LOCAL_FILES_MAX_DOWNLOAD_BYTES)}).",
            show_alert=True,
        )
        return ADMIN_OWNERSRV_FILES

    await query.answer("⏳ Downloading...")
    path = owner_server.local_join(fb["cwd"], entry["name"])
    try:
        with open(path, "rb") as f:
            await context.bot.send_document(chat_id=fb["chat_id"], document=f, filename=entry["name"])
    except Exception as e:
        await context.bot.send_message(chat_id=fb["chat_id"], text=f"✕ Download of \"{entry['name']}\" failed: {e}")
    return ADMIN_OWNERSRV_FILES


async def admin_ownersrv_files_goto_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    fb = context.user_data.get("admin_ownersrv_fb")
    if not fb:
        await query.answer("File browser session expired.", show_alert=True)
        return ConversationHandler.END

    fb["awaiting_path"] = True
    await query.answer()
    await context.bot.send_message(
        chat_id=fb["chat_id"],
        text=f"✎ Send the path to open, e.g. `/opt/bot` (relative paths are relative to `{fb['cwd']}`).",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardMarkup([[CANCEL_BUTTON_TEXT]], resize_keyboard=True, one_time_keyboard=True),
    )
    return ADMIN_OWNERSRV_FILES


async def _osf_handle_goto_text(update: Update, context: ContextTypes.DEFAULT_TYPE, fb: dict):
    raw = (update.message.text or "").strip()
    candidate = raw if raw.startswith("/") else owner_server.local_join(fb["cwd"], raw)
    path = owner_server.local_normalize(candidate)

    entries, err = _osf_list(path)
    if err:
        await update.message.reply_text(f"✕ Could not open \"{path}\":\n{err}\n\nSend another path, or tap Cancel.")
        return ADMIN_OWNERSRV_FILES

    fb["awaiting_path"] = False
    fb["cwd"], fb["entries"] = path, entries
    _osf_remember_cwd(context, path)
    await update.message.reply_text("✓ Moved.", reply_markup=ReplyKeyboardRemove())
    await context.bot.send_message(
        chat_id=fb["chat_id"], text=_osf_text(fb), reply_markup=_osf_keyboard(fb), parse_mode="Markdown",
    )
    return ADMIN_OWNERSRV_FILES


async def admin_ownersrv_files_text_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    fb = context.user_data.get("admin_ownersrv_fb")
    if not fb:
        return ConversationHandler.END
    if fb.get("awaiting_edit_idx") is not None:
        return await _osf_handle_edit_save_text(update, context, fb)
    if fb.get("awaiting_rename_idx") is not None:
        return await _osf_handle_rename_text(update, context, fb)
    if fb.get("awaiting_chmod_idx") is not None:
        return await _osf_handle_chmod_text(update, context, fb)
    if fb.get("awaiting_chown_idx") is not None:
        return await _osf_handle_chown_text(update, context, fb)
    if fb.get("awaiting_path"):
        return await _osf_handle_goto_text(update, context, fb)
    if fb.get("awaiting_url"):
        return await _osf_handle_urldl_text(update, context, fb)
    if fb.get("awaiting_mkdir"):
        return await _osf_handle_mkdir_text(update, context, fb)
    await update.message.reply_text("Tap a button below, or use \"✎ Go to path\" / \"» From URL\".")
    return ADMIN_OWNERSRV_FILES


async def admin_ownersrv_files_upload_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    fb = context.user_data.get("admin_ownersrv_fb")
    if not fb:
        await query.answer("File browser session expired.", show_alert=True)
        return ConversationHandler.END

    fb["awaiting_upload"] = True
    await query.answer()
    await context.bot.send_message(
        chat_id=fb["chat_id"],
        text=(
            f"↑ Send the file to upload into:\n`{fb['cwd']}`\n\n"
            f"Send it as a Telegram *document* (not a photo), up to "
            f"{_osf_human_size(owner_server.LOCAL_FILES_MAX_UPLOAD_BYTES)}."
        ),
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardMarkup([[CANCEL_BUTTON_TEXT]], resize_keyboard=True, one_time_keyboard=True),
    )
    return ADMIN_OWNERSRV_FILES


async def admin_ownersrv_files_upload_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    fb = context.user_data.get("admin_ownersrv_fb")
    if not fb:
        return ConversationHandler.END
    if not fb.get("awaiting_upload"):
        await update.message.reply_text("Tap \"↑ Upload\" first, then send the file.")
        return ADMIN_OWNERSRV_FILES

    doc = update.message.document
    if doc.file_size and doc.file_size > owner_server.LOCAL_FILES_MAX_UPLOAD_BYTES:
        await update.message.reply_text(
            f"✕ \"{doc.file_name}\" is {_osf_human_size(doc.file_size)} - too large to upload here "
            f"(limit {_osf_human_size(owner_server.LOCAL_FILES_MAX_UPLOAD_BYTES)})."
        )
        return ADMIN_OWNERSRV_FILES

    tmp_path = os.path.join(tempfile.gettempdir(), f"osfup_{uuid.uuid4().hex}_{doc.file_name}")
    try:
        tg_file = await doc.get_file()
        await tg_file.download_to_drive(tmp_path)
    except Exception as e:
        try:
            os.remove(tmp_path)
        except Exception:
            pass
        await update.message.reply_text(f"✕ Upload of \"{doc.file_name}\" failed: {str(e)[:300]}")
        return ADMIN_OWNERSRV_FILES

    lock = fb.setdefault("upload_batch_lock", asyncio.Lock())
    async with lock:
        buffer = fb.setdefault("upload_buffer", [])
        buffer.append({"tmp_path": tmp_path, "filename": doc.file_name})

        if context.job_queue is not None:
            job_name = f"osf_uploadbatch_{fb['chat_id']}"
            for job in context.job_queue.get_jobs_by_name(job_name):
                job.schedule_removal()
            context.job_queue.run_once(
                _finalize_admin_ownersrv_upload_batch,
                when=OWNERSRV_UPLOAD_BATCH_DEBOUNCE,
                chat_id=fb["chat_id"],
                user_id=update.effective_user.id,
                name=job_name,
            )
        else:
            await _finalize_admin_ownersrv_upload_batch_for(fb, context)

    return ADMIN_OWNERSRV_FILES


async def _finalize_admin_ownersrv_upload_batch(context: ContextTypes.DEFAULT_TYPE):
    fb = context.user_data.get("admin_ownersrv_fb")
    if not fb:
        return
    await _finalize_admin_ownersrv_upload_batch_for(fb, context)


async def _finalize_admin_ownersrv_upload_batch_for(fb: dict, context: ContextTypes.DEFAULT_TYPE):
    buffer = fb.pop("upload_buffer", [])
    if not buffer:
        return
    fb["awaiting_upload"] = False

    status_msg = await context.bot.send_message(
        chat_id=fb["chat_id"],
        text="⏳ Uploading..." if len(buffer) == 1 else f"⏳ Uploading {len(buffer)} files...",
        reply_markup=ReplyKeyboardRemove(),
    )

    for item in buffer:
        item["dest_path"] = owner_server.local_join(fb["cwd"], item["filename"])

    uploaded, overwritten, failed = [], [], []
    for item in buffer:
        existed = os.path.exists(item["dest_path"])
        try:
            os.replace(item["tmp_path"], item["dest_path"])
        except Exception as e:
            try:
                os.remove(item["tmp_path"])
            except Exception:
                pass
            failed.append(f"{item['filename']}: {str(e)[:300]}")
        else:
            (overwritten if existed else uploaded).append(item["filename"])

    lines = []
    if uploaded:
        if len(uploaded) == 1:
            lines.append(f"✓ Uploaded \"{uploaded[0]}\".")
        else:
            lines.append(f"✓ Uploaded {len(uploaded)} files: " + ", ".join(f'"{n}"' for n in uploaded))
    if overwritten:
        if len(overwritten) == 1:
            lines.append(f"✓ Uploaded \"{overwritten[0]}\" (overwritten).")
        else:
            lines.append(f"✓ Uploaded {len(overwritten)} files (overwritten): " + ", ".join(f'"{n}"' for n in overwritten))
    if failed:
        lines.append("✕ Failed: " + "; ".join(failed))

    await _osf_safe_edit(status_msg, context, fb["chat_id"], "\n".join(lines) if lines else "…")

    new_entries, list_err = _osf_list(fb["cwd"])
    if not list_err:
        fb["entries"] = new_entries
    await context.bot.send_message(
        chat_id=fb["chat_id"], text=_osf_text(fb), reply_markup=_osf_keyboard(fb), parse_mode="Markdown",
    )


async def admin_ownersrv_files_urldl_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    fb = context.user_data.get("admin_ownersrv_fb")
    if not fb:
        await query.answer("File browser session expired.", show_alert=True)
        return ConversationHandler.END

    fb["awaiting_url"] = True
    await query.answer()
    await context.bot.send_message(
        chat_id=fb["chat_id"],
        text=(
            f"» Send the URL to download directly into:\n`{fb['cwd']}`\n\n"
            f"Fetched by the bot itself, so the "
            f"{_osf_human_size(owner_server.LOCAL_FILES_MAX_UPLOAD_BYTES)} Telegram upload limit doesn't "
            f"apply here - up to {_osf_human_size(owner_server.LOCAL_FILES_MAX_URL_DOWNLOAD_BYTES)}, "
            f"limited only by disk space."
        ),
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardMarkup([[CANCEL_BUTTON_TEXT]], resize_keyboard=True, one_time_keyboard=True),
    )
    return ADMIN_OWNERSRV_FILES


_OSF_URL_RE = re.compile(r"^https?://", re.IGNORECASE)


async def _osf_handle_urldl_text(update: Update, context: ContextTypes.DEFAULT_TYPE, fb: dict):
    url = (update.message.text or "").strip()
    if not _OSF_URL_RE.match(url):
        await update.message.reply_text("✕ That doesn't look like an http(s) URL. Send a valid URL, or tap Cancel.")
        return ADMIN_OWNERSRV_FILES

    fb["awaiting_url"] = False
    status_msg = await update.message.reply_text(
        f"⏳ Downloading into:\n`{fb['cwd']}`…", parse_mode="Markdown", reply_markup=ReplyKeyboardRemove(),
    )

    dest_path, err = await _run_local_download_with_progress(
        fb["cwd"], url, OSF_URLDL_TIMEOUT, owner_server.LOCAL_FILES_MAX_URL_DOWNLOAD_BYTES, status_msg,
    )
    if err:
        await status_msg.edit_text(f"✕ Download failed: {err}")
        return ADMIN_OWNERSRV_FILES

    new_entries, list_err = _osf_list(fb["cwd"])
    if not list_err:
        fb["entries"] = new_entries
    await status_msg.edit_text(f"✓ Downloaded \"{os.path.basename(dest_path)}\".")
    await context.bot.send_message(
        chat_id=fb["chat_id"], text=_osf_text(fb), reply_markup=_osf_keyboard(fb), parse_mode="Markdown",
    )
    return ADMIN_OWNERSRV_FILES


async def admin_ownersrv_files_mkdir_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    fb = context.user_data.get("admin_ownersrv_fb")
    if not fb:
        await query.answer("File browser session expired.", show_alert=True)
        return ConversationHandler.END

    fb["awaiting_mkdir"] = True
    await query.answer()
    await context.bot.send_message(
        chat_id=fb["chat_id"],
        text=f"＋ Send the name for the new folder inside:\n`{fb['cwd']}`",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardMarkup([[CANCEL_BUTTON_TEXT]], resize_keyboard=True, one_time_keyboard=True),
    )
    return ADMIN_OWNERSRV_FILES


async def _osf_handle_mkdir_text(update: Update, context: ContextTypes.DEFAULT_TYPE, fb: dict):
    name = (update.message.text or "").strip()
    if not name or "/" in name:
        await update.message.reply_text("✕ That's not a valid folder name - it can't be empty or contain \"/\". Send another name, or tap Cancel.")
        return ADMIN_OWNERSRV_FILES

    new_path = owner_server.local_join(fb["cwd"], name)
    fb["awaiting_mkdir"] = False
    status_msg = await update.message.reply_text("⏳ Creating folder...", reply_markup=ReplyKeyboardRemove())

    try:
        owner_server.local_mkdir(new_path)
    except Exception as e:
        await status_msg.edit_text(f"✕ Could not create folder: {str(e)[:300]}")
    else:
        new_entries, list_err = _osf_list(fb["cwd"])
        if not list_err:
            fb["entries"] = new_entries
        await status_msg.edit_text(f"✓ Created folder \"{name}\".")

    await context.bot.send_message(
        chat_id=fb["chat_id"], text=_osf_text(fb), reply_markup=_osf_keyboard(fb), parse_mode="Markdown",
    )
    return ADMIN_OWNERSRV_FILES


async def admin_ownersrv_files_actions_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    fb = context.user_data.get("admin_ownersrv_fb")
    if not fb:
        await query.answer("File browser session expired.", show_alert=True)
        return ConversationHandler.END

    idx = int(query.data.replace(OWNERSRV_FILES_ACTIONS_CB_PREFIX, "", 1))
    entry = _osf_entry_at(fb, idx)
    if entry is None:
        await query.answer("That item isn't listed anymore - try refreshing.", show_alert=True)
        return ADMIN_OWNERSRV_FILES

    kind = "▢ folder" if entry["is_dir"] else "▤ file"
    await query.answer()
    rows = []
    if not entry["is_dir"]:
        rows.append([InlineKeyboardButton("✎ Edit", callback_data=f"{OWNERSRV_FILES_EDIT_CB_PREFIX}{idx}")])
        rows.append([InlineKeyboardButton("▤ Tail -f", callback_data=f"{OWNERSRV_FILES_TAIL_CB_PREFIX}{idx}")])
        if owner_server.is_archive(entry["name"]):
            rows.append([InlineKeyboardButton("▨ Extract here", callback_data=f"{OWNERSRV_FILES_EXTRACT_CB_PREFIX}{idx}")])
    rows.append([InlineKeyboardButton("▥ Compress → .zip", callback_data=f"{OWNERSRV_FILES_COMPRESS_CB_PREFIX}{idx}")])
    rows.append([
        InlineKeyboardButton("⚿ chmod", callback_data=f"{OWNERSRV_FILES_CHMOD_CB_PREFIX}{idx}"),
        InlineKeyboardButton("◦ chown", callback_data=f"{OWNERSRV_FILES_CHOWN_CB_PREFIX}{idx}"),
    ])
    rows.append([InlineKeyboardButton("✎ Rename", callback_data=f"{OWNERSRV_FILES_RENAME_CB_PREFIX}{idx}")])
    rows.append([InlineKeyboardButton("✕ Delete", callback_data=f"{OWNERSRV_FILES_DELCONFIRM_CB_PREFIX}{idx}")])
    rows.append([InlineKeyboardButton("← Back", callback_data=OWNERSRV_FILES_BACK_CB)])
    await query.edit_message_text(
        f"{kind} `{entry['name']}`\n\nWhat would you like to do?",
        reply_markup=InlineKeyboardMarkup(rows), parse_mode="Markdown",
    )
    return ADMIN_OWNERSRV_FILES


async def admin_ownersrv_files_rename_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    fb = context.user_data.get("admin_ownersrv_fb")
    if not fb:
        await query.answer("File browser session expired.", show_alert=True)
        return ConversationHandler.END

    idx = int(query.data.replace(OWNERSRV_FILES_RENAME_CB_PREFIX, "", 1))
    entry = _osf_entry_at(fb, idx)
    if entry is None:
        await query.answer("That item isn't listed anymore - try refreshing.", show_alert=True)
        return ADMIN_OWNERSRV_FILES

    fb["awaiting_rename_idx"] = idx
    await query.answer()
    try:
        await query.edit_message_text(f"✎ Renaming `{entry['name']}`…", parse_mode="Markdown")
    except BadRequest:
        pass
    await context.bot.send_message(
        chat_id=fb["chat_id"],
        text=f"✎ Send the new name for `{entry['name']}` (name only, it stays in the same folder).",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardMarkup([[CANCEL_BUTTON_TEXT]], resize_keyboard=True, one_time_keyboard=True),
    )
    return ADMIN_OWNERSRV_FILES


async def _osf_handle_rename_text(update: Update, context: ContextTypes.DEFAULT_TYPE, fb: dict):
    idx = fb.get("awaiting_rename_idx")
    entry = _osf_entry_at(fb, idx) if idx is not None else None
    if entry is None:
        fb["awaiting_rename_idx"] = None
        await update.message.reply_text("✕ That item isn't listed anymore - try refreshing.", reply_markup=ReplyKeyboardRemove())
        await context.bot.send_message(chat_id=fb["chat_id"], text=_osf_text(fb), reply_markup=_osf_keyboard(fb), parse_mode="Markdown")
        return ADMIN_OWNERSRV_FILES

    new_name = (update.message.text or "").strip()
    if not new_name or "/" in new_name:
        await update.message.reply_text("✕ That's not a valid name - it can't be empty or contain \"/\". Send another name, or tap Cancel.")
        return ADMIN_OWNERSRV_FILES

    old_path = owner_server.local_join(fb["cwd"], entry["name"])
    new_path = owner_server.local_join(fb["cwd"], new_name)
    fb["awaiting_rename_idx"] = None
    status_msg = await update.message.reply_text("⏳ Renaming...", reply_markup=ReplyKeyboardRemove())
    try:
        owner_server.local_rename(old_path, new_path)
    except Exception as e:
        await status_msg.edit_text(f"✕ Rename failed: {str(e)[:300]}")
        await context.bot.send_message(chat_id=fb["chat_id"], text=_osf_text(fb), reply_markup=_osf_keyboard(fb), parse_mode="Markdown")
        return ADMIN_OWNERSRV_FILES

    new_entries, list_err = _osf_list(fb["cwd"])
    if not list_err:
        fb["entries"] = new_entries
    await status_msg.edit_text(f"✓ Renamed \"{entry['name']}\" to \"{new_name}\".")
    await context.bot.send_message(chat_id=fb["chat_id"], text=_osf_text(fb), reply_markup=_osf_keyboard(fb), parse_mode="Markdown")
    return ADMIN_OWNERSRV_FILES


async def admin_ownersrv_files_chmod_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    fb = context.user_data.get("admin_ownersrv_fb")
    if not fb:
        await query.answer("File browser session expired.", show_alert=True)
        return ConversationHandler.END

    idx = int(query.data.replace(OWNERSRV_FILES_CHMOD_CB_PREFIX, "", 1))
    entry = _osf_entry_at(fb, idx)
    if entry is None:
        await query.answer("That item isn't listed anymore - try refreshing.", show_alert=True)
        return ADMIN_OWNERSRV_FILES

    path = owner_server.local_join(fb["cwd"], entry["name"])
    try:
        current = owner_server.local_stat_perms(path)
        current_line = f"Current: `{current['mode']}` (owner `{current['owner']}:{current['group']}`)\n\n"
    except Exception:
        current_line = ""

    fb["awaiting_chmod_idx"] = idx
    await query.answer()
    try:
        await query.edit_message_text(f"⚿ Permissions for `{entry['name']}`…", parse_mode="Markdown")
    except BadRequest:
        pass
    await context.bot.send_message(
        chat_id=fb["chat_id"],
        text=f"⚿ Send the new mode for `{entry['name']}` as 3 octal digits, e.g. `755` or `644`.\n\n{current_line}",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardMarkup([[CANCEL_BUTTON_TEXT]], resize_keyboard=True, one_time_keyboard=True),
    )
    return ADMIN_OWNERSRV_FILES


async def _osf_handle_chmod_text(update: Update, context: ContextTypes.DEFAULT_TYPE, fb: dict):
    idx = fb.get("awaiting_chmod_idx")
    entry = _osf_entry_at(fb, idx) if idx is not None else None
    if entry is None:
        fb["awaiting_chmod_idx"] = None
        await update.message.reply_text("✕ That item isn't listed anymore - try refreshing.", reply_markup=ReplyKeyboardRemove())
        await context.bot.send_message(chat_id=fb["chat_id"], text=_osf_text(fb), reply_markup=_osf_keyboard(fb), parse_mode="Markdown")
        return ADMIN_OWNERSRV_FILES

    mode_str = (update.message.text or "").strip()
    path = owner_server.local_join(fb["cwd"], entry["name"])
    fb["awaiting_chmod_idx"] = None
    try:
        owner_server.local_chmod(path, mode_str)
    except Exception as e:
        await update.message.reply_text(f"✕ chmod failed: {str(e)[:300]}", reply_markup=ReplyKeyboardRemove())
        await context.bot.send_message(chat_id=fb["chat_id"], text=_osf_text(fb), reply_markup=_osf_keyboard(fb), parse_mode="Markdown")
        return ADMIN_OWNERSRV_FILES

    await update.message.reply_text(f"✓ Permissions on \"{entry['name']}\" set to {mode_str}.", reply_markup=ReplyKeyboardRemove())
    await context.bot.send_message(chat_id=fb["chat_id"], text=_osf_text(fb), reply_markup=_osf_keyboard(fb), parse_mode="Markdown")
    return ADMIN_OWNERSRV_FILES


async def admin_ownersrv_files_chown_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    fb = context.user_data.get("admin_ownersrv_fb")
    if not fb:
        await query.answer("File browser session expired.", show_alert=True)
        return ConversationHandler.END

    idx = int(query.data.replace(OWNERSRV_FILES_CHOWN_CB_PREFIX, "", 1))
    entry = _osf_entry_at(fb, idx)
    if entry is None:
        await query.answer("That item isn't listed anymore - try refreshing.", show_alert=True)
        return ADMIN_OWNERSRV_FILES

    path = owner_server.local_join(fb["cwd"], entry["name"])
    try:
        current = owner_server.local_stat_perms(path)
        current_line = f"Current owner: `{current['owner']}:{current['group']}`\n\n"
    except Exception:
        current_line = ""

    fb["awaiting_chown_idx"] = idx
    await query.answer()
    try:
        await query.edit_message_text(f"◦ Owner for `{entry['name']}`…", parse_mode="Markdown")
    except BadRequest:
        pass
    await context.bot.send_message(
        chat_id=fb["chat_id"],
        text=f"◦ Send the new owner for `{entry['name']}` as `user` or `user:group`, e.g. `www-data:www-data`.\n\n{current_line}",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardMarkup([[CANCEL_BUTTON_TEXT]], resize_keyboard=True, one_time_keyboard=True),
    )
    return ADMIN_OWNERSRV_FILES


async def _osf_handle_chown_text(update: Update, context: ContextTypes.DEFAULT_TYPE, fb: dict):
    idx = fb.get("awaiting_chown_idx")
    entry = _osf_entry_at(fb, idx) if idx is not None else None
    if entry is None:
        fb["awaiting_chown_idx"] = None
        await update.message.reply_text("✕ That item isn't listed anymore - try refreshing.", reply_markup=ReplyKeyboardRemove())
        await context.bot.send_message(chat_id=fb["chat_id"], text=_osf_text(fb), reply_markup=_osf_keyboard(fb), parse_mode="Markdown")
        return ADMIN_OWNERSRV_FILES

    owner_spec = (update.message.text or "").strip()
    path = owner_server.local_join(fb["cwd"], entry["name"])
    fb["awaiting_chown_idx"] = None
    try:
        owner_server.local_chown(path, owner_spec)
    except Exception as e:
        await update.message.reply_text(f"✕ chown failed: {str(e)[:300]}", reply_markup=ReplyKeyboardRemove())
        await context.bot.send_message(chat_id=fb["chat_id"], text=_osf_text(fb), reply_markup=_osf_keyboard(fb), parse_mode="Markdown")
        return ADMIN_OWNERSRV_FILES

    await update.message.reply_text(f"✓ Owner of \"{entry['name']}\" set to {owner_spec}.", reply_markup=ReplyKeyboardRemove())
    await context.bot.send_message(chat_id=fb["chat_id"], text=_osf_text(fb), reply_markup=_osf_keyboard(fb), parse_mode="Markdown")
    return ADMIN_OWNERSRV_FILES


async def admin_ownersrv_files_extract(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    fb = context.user_data.get("admin_ownersrv_fb")
    if not fb:
        await query.answer("File browser session expired.", show_alert=True)
        return ConversationHandler.END

    idx = int(query.data.replace(OWNERSRV_FILES_EXTRACT_CB_PREFIX, "", 1))
    entry = _osf_entry_at(fb, idx)
    if entry is None or entry["is_dir"]:
        await query.answer("That file isn't listed anymore - try refreshing.", show_alert=True)
        return ADMIN_OWNERSRV_FILES

    path = owner_server.local_join(fb["cwd"], entry["name"])
    await query.answer("⏳ Extracting...")
    try:
        owner_server.local_extract_archive(path, fb["cwd"])
        popup = f"✓ Extracted \"{entry['name']}\" into {fb['cwd']}."
    except Exception as e:
        popup = f"✕ Extract failed: {str(e)[:180]}"

    new_entries, list_err = _osf_list(fb["cwd"])
    if not list_err:
        fb["entries"] = new_entries
    try:
        await query.answer(popup[:200], show_alert=True)
    except BadRequest:
        pass
    await _osf_render(query, fb)
    return ADMIN_OWNERSRV_FILES


async def admin_ownersrv_files_compress(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    fb = context.user_data.get("admin_ownersrv_fb")
    if not fb:
        await query.answer("File browser session expired.", show_alert=True)
        return ConversationHandler.END

    idx = int(query.data.replace(OWNERSRV_FILES_COMPRESS_CB_PREFIX, "", 1))
    entry = _osf_entry_at(fb, idx)
    if entry is None:
        await query.answer("That item isn't listed anymore - try refreshing.", show_alert=True)
        return ADMIN_OWNERSRV_FILES

    path = owner_server.local_join(fb["cwd"], entry["name"])
    await query.answer("⏳ Compressing...")
    try:
        out_path = owner_server.local_compress_entry(path, entry["is_dir"], fmt="zip")
        popup = f"✓ Created {os.path.basename(out_path)}."
    except Exception as e:
        popup = f"✕ Compress failed: {str(e)[:180]}"

    new_entries, list_err = _osf_list(fb["cwd"])
    if not list_err:
        fb["entries"] = new_entries
    try:
        await query.answer(popup[:200], show_alert=True)
    except BadRequest:
        pass
    await _osf_render(query, fb)
    return ADMIN_OWNERSRV_FILES


def _format_osf_tail(name: str, output: str, status_line: str) -> str:
    body = _strip_ansi(output)[-OWNERSRV_TAIL_BODY_CHARS:].rstrip("\n")
    header = f"▤ *Tail* `{name}`"
    if not body:
        return f"{header}\n{status_line}"
    return f"{header}\n```\n{body}\n\n```\n{status_line}"


def _osf_tail_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("⊘ Stop", callback_data=OWNERSRV_FILES_TAILSTOP_CB)]])


async def admin_ownersrv_files_tail_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    fb = context.user_data.get("admin_ownersrv_fb")
    if not fb:
        await query.answer("File browser session expired.", show_alert=True)
        return ConversationHandler.END

    idx = int(query.data.replace(OWNERSRV_FILES_TAIL_CB_PREFIX, "", 1))
    entry = _osf_entry_at(fb, idx)
    if entry is None or entry["is_dir"]:
        await query.answer("That file isn't listed anymore - try refreshing.", show_alert=True)
        return ADMIN_OWNERSRV_FILES

    if fb.get("tail_proc") is not None:
        owner_server.tail_stop(fb["tail_proc"])
        fb["tail_proc"] = None

    path = owner_server.local_join(fb["cwd"], entry["name"])
    await query.answer("⏳ Starting...")
    try:
        proc = owner_server.tail_start(path)
    except Exception as e:
        await query.answer(f"✕ Could not start tail: {str(e)[:180]}", show_alert=True)
        return ADMIN_OWNERSRV_FILES

    fb["tail_proc"] = proc
    msg = await context.bot.send_message(
        chat_id=fb["chat_id"],
        text=_format_osf_tail(entry["name"], "", "◉ Following — new lines appear here as they're written."),
        parse_mode="Markdown", reply_markup=_osf_tail_keyboard(),
    )
    fb["tail_msg_id"] = msg.message_id
    logger.info(f"Admin {query.from_user.id} started tailing {path} on the owner server")
    asyncio.create_task(_stream_osf_tail(context, fb["chat_id"], entry["name"], msg.message_id))
    return ADMIN_OWNERSRV_FILES


async def _stream_osf_tail(context: ContextTypes.DEFAULT_TYPE, chat_id: int, name: str, message_id: int):
    output_so_far = ""
    last_sent_text = None
    while True:
        await asyncio.sleep(OWNERSRV_TAIL_EDIT_INTERVAL)
        fb = context.user_data.get("admin_ownersrv_fb")
        proc = fb.get("tail_proc") if fb else None
        if proc is None:
            return

        chunk = await asyncio.to_thread(owner_server.tail_read_available, proc)
        fb_now = context.user_data.get("admin_ownersrv_fb")
        if not fb_now or fb_now.get("tail_proc") is not proc:
            return
        if chunk:
            output_so_far += chunk
        alive = owner_server.tail_is_alive(proc)
        status = "◉ Following…" if alive else "▪ Process ended (file may have been deleted)."
        new_text = _format_osf_tail(name, output_so_far, status)
        if new_text != last_sent_text:
            try:
                await context.bot.edit_message_text(
                    new_text, chat_id=chat_id, message_id=message_id,
                    parse_mode="Markdown", reply_markup=_osf_tail_keyboard() if alive else None,
                )
                last_sent_text = new_text
            except BadRequest as e:
                if "not modified" not in str(e).lower():
                    logger.debug(f"owner-server tail live edit failed: {e}")
            except Exception as e:
                logger.debug(f"owner-server tail live edit failed: {e}")
        if not alive:
            fb["tail_proc"] = None
            return


async def admin_ownersrv_files_tail_stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    fb = context.user_data.get("admin_ownersrv_fb")
    proc = fb.get("tail_proc") if fb else None
    if fb and proc is not None:
        fb["tail_proc"] = None
        await asyncio.to_thread(owner_server.tail_stop, proc)
    await query.answer("⊘ Stopped.")
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except BadRequest:
        pass
    return ADMIN_OWNERSRV_FILES


async def admin_ownersrv_files_edit_open(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    fb = context.user_data.get("admin_ownersrv_fb")
    if not fb:
        await query.answer("File browser session expired.", show_alert=True)
        return ConversationHandler.END

    idx = int(query.data.replace(OWNERSRV_FILES_EDIT_CB_PREFIX, "", 1))
    entry = _osf_entry_at(fb, idx)
    if entry is None or entry["is_dir"]:
        await query.answer("That file isn't listed anymore - try refreshing.", show_alert=True)
        return ADMIN_OWNERSRV_FILES

    path = owner_server.local_join(fb["cwd"], entry["name"])
    await query.answer("⏳ Opening...")
    try:
        content = owner_server.local_read_text(path, owner_server.LOCAL_FILES_EDITOR_MAX_BYTES)
    except Exception as e:
        try:
            await query.edit_message_text(f"✕ Could not open \"{entry['name']}\" for editing.\n{str(e)[:300]}")
        except BadRequest:
            pass
        await context.bot.send_message(chat_id=fb["chat_id"], text=_osf_text(fb), reply_markup=_osf_keyboard(fb), parse_mode="Markdown")
        return ADMIN_OWNERSRV_FILES

    fb["awaiting_edit_idx"] = idx
    try:
        await query.edit_message_text(f"✎ Opening `{entry['name']}`…", parse_mode="Markdown")
    except BadRequest:
        pass

    body = html.escape(content) if content else " "
    text = (
        f"✎ <b>{html.escape(entry['name'])}</b>\n"
        f"▢ <code>{html.escape(fb['cwd'])}</code>  ·  {_osf_human_size(entry['size'])}\n\n"
        f"<pre>{body}</pre>\n\n"
        f"✎ Reply with the new content to save, or tap Cancel below."
    )
    await context.bot.send_message(
        chat_id=fb["chat_id"], text=text, parse_mode="HTML",
        reply_markup=ReplyKeyboardMarkup([[CANCEL_BUTTON_TEXT]], resize_keyboard=True, one_time_keyboard=True),
    )
    return ADMIN_OWNERSRV_FILES


async def _osf_handle_edit_save_text(update: Update, context: ContextTypes.DEFAULT_TYPE, fb: dict):
    idx = fb.get("awaiting_edit_idx")
    entry = _osf_entry_at(fb, idx) if idx is not None else None
    if entry is None:
        fb["awaiting_edit_idx"] = None
        await update.message.reply_text("✕ That file isn't listed anymore - try refreshing.", reply_markup=ReplyKeyboardRemove())
        await context.bot.send_message(chat_id=fb["chat_id"], text=_osf_text(fb), reply_markup=_osf_keyboard(fb), parse_mode="Markdown")
        return ADMIN_OWNERSRV_FILES

    new_content = update.message.text or ""
    if len(new_content.encode("utf-8")) > owner_server.LOCAL_FILES_EDITOR_MAX_BYTES:
        await update.message.reply_text(
            f"✕ That's over {owner_server.LOCAL_FILES_EDITOR_MAX_BYTES} bytes - too big to save here. "
            f"Send something shorter, or tap Cancel."
        )
        return ADMIN_OWNERSRV_FILES

    path = owner_server.local_join(fb["cwd"], entry["name"])
    fb["awaiting_edit_idx"] = None
    status_msg = await update.message.reply_text("⏳ Saving...", reply_markup=ReplyKeyboardRemove())
    try:
        owner_server.local_write_text(path, new_content)
    except Exception as e:
        await status_msg.edit_text(f"✕ Save failed: {str(e)[:300]}")
        await context.bot.send_message(chat_id=fb["chat_id"], text=_osf_text(fb), reply_markup=_osf_keyboard(fb), parse_mode="Markdown")
        return ADMIN_OWNERSRV_FILES

    new_entries, list_err = _osf_list(fb["cwd"])
    if not list_err:
        fb["entries"] = new_entries
    await status_msg.edit_text(f"✓ Saved \"{entry['name']}\".")
    await context.bot.send_message(chat_id=fb["chat_id"], text=_osf_text(fb), reply_markup=_osf_keyboard(fb), parse_mode="Markdown")
    return ADMIN_OWNERSRV_FILES


async def admin_ownersrv_files_delete_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    fb = context.user_data.get("admin_ownersrv_fb")
    if not fb:
        await query.answer("File browser session expired.", show_alert=True)
        return ConversationHandler.END

    idx = int(query.data.replace(OWNERSRV_FILES_DELCONFIRM_CB_PREFIX, "", 1))
    entry = _osf_entry_at(fb, idx)
    if entry is None:
        await query.answer("That item isn't listed anymore - try refreshing.", show_alert=True)
        return ADMIN_OWNERSRV_FILES

    await query.answer()
    warn = "\n\n⚠ This will delete the folder and *everything inside it*." if entry["is_dir"] else ""
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("✓ Yes, delete", callback_data=f"{OWNERSRV_FILES_DELOK_CB_PREFIX}{idx}"),
         InlineKeyboardButton("✕ Cancel", callback_data=OWNERSRV_FILES_BACK_CB)],
    ])
    await query.edit_message_text(
        f"Are you sure you want to delete `{entry['name']}`?{warn}",
        reply_markup=keyboard, parse_mode="Markdown",
    )
    return ADMIN_OWNERSRV_FILES


async def admin_ownersrv_files_delete_execute(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    fb = context.user_data.get("admin_ownersrv_fb")
    if not fb:
        await query.answer("File browser session expired.", show_alert=True)
        return ConversationHandler.END

    idx = int(query.data.replace(OWNERSRV_FILES_DELOK_CB_PREFIX, "", 1))
    entry = _osf_entry_at(fb, idx)
    if entry is None:
        await query.answer("That item isn't listed anymore - try refreshing.", show_alert=True)
        return ADMIN_OWNERSRV_FILES

    path = owner_server.local_join(fb["cwd"], entry["name"])
    try:
        owner_server.local_delete_recursive(path, entry["is_dir"])
        err = None
    except Exception as e:
        err = str(e)[:300]

    async def _popup(text: str):
        try:
            await query.answer(text[:200], show_alert=True)
        except BadRequest:
            pass

    if err:
        await _popup(f"✕ Delete failed: {err}")
        await _osf_render(query, fb)
        return ADMIN_OWNERSRV_FILES

    new_entries, list_err = _osf_list(fb["cwd"])
    if not list_err:
        fb["entries"] = new_entries
    await _popup(f"✓ Deleted \"{entry['name']}\".")
    await _osf_render(query, fb)
    return ADMIN_OWNERSRV_FILES


async def admin_ownersrv_files_back(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    fb = context.user_data.get("admin_ownersrv_fb")
    if not fb:
        await query.answer("File browser session expired.", show_alert=True)
        return ConversationHandler.END
    await query.answer()
    await _osf_render(query, fb)
    return ADMIN_OWNERSRV_FILES


async def admin_ownersrv_files_close(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    fb = context.user_data.pop("admin_ownersrv_fb", None)
    if fb and fb.get("tail_proc") is not None:
        owner_server.tail_stop(fb["tail_proc"])
    await query.answer()
    keyboard = InlineKeyboardMarkup(
        [[InlineKeyboardButton("← Back to Owner Server menu", callback_data="admin_ownersrv_menu")]]
    )
    try:
        await query.edit_message_text("▪ Closed the Owner Server file browser.", reply_markup=keyboard)
    except Exception:
        pass
    return ConversationHandler.END
