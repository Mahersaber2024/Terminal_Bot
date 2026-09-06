"""
backup_manager.py

Full-backup feature triggered from the admin panel (see admin.py's
"🗄 Backup" menu) or run automatically on a schedule (see main.py's
job-queue wiring).

This does NOT reimplement the backup logic - it shells out to the
project's own full_backup.sh (the same script you'd run by hand from a
root shell), then takes the .tar.gz it produces, sends it as a Telegram
document to the log group's "🗄 Backups" topic (see
logger_bot.Topics.BACKUP), and deletes it from disk right after. Nothing
about what goes into the archive or how to restore it (see the
BACKUP_INFO.txt full_backup.sh writes inside the archive, and
restore_backup.sh) changes - this module is just the delivery mechanism.

Requires the bot process to be running as root (same as full_backup.sh's
own check), which matches the systemd service installed by install.sh
(User=root).
"""
import asyncio
import glob
import logging
import os
import shutil
import subprocess
import tempfile
from datetime import datetime, timezone

import bot_settings
import logger_bot

logger = logging.getLogger(__name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# This module is meant to live in <install_dir>/backups/, right alongside
# full_backup.sh and restore_backup.sh - NOT in the project root. If that's
# where it is, the actual install root (what full_backup.sh needs as its
# INSTALL_DIR argument - the folder containing .env, ServerManager/, etc.)
# is one level up. Falls back to treating BASE_DIR itself as the install
# root for a flat layout, so this still works either way.
if os.path.basename(BASE_DIR) == "backups":
    INSTALL_DIR = os.path.dirname(BASE_DIR)
else:
    INSTALL_DIR = BASE_DIR

# full_backup.sh should be right next to this file (both in backups/), but
# also check the flat layouts in case someone moved things around - the
# first one found wins.
FULL_BACKUP_SCRIPT_CANDIDATES = (
    os.path.join(BASE_DIR, "full_backup.sh"),
    os.path.join(INSTALL_DIR, "backups", "full_backup.sh"),
    os.path.join(INSTALL_DIR, "full_backup.sh"),
)

JOB_NAME = "full_backup_job"
BACKUP_SCRIPT_TIMEOUT_SECONDS = 900  # pg_dump + tar can take a while on a big DB


class BackupError(Exception):
    """Raised when full_backup.sh itself couldn't be found or failed."""


def _find_backup_script() -> str:
    for path in FULL_BACKUP_SCRIPT_CANDIDATES:
        if os.path.isfile(path):
            return path
    checked = "\n".join(f"  - {p}" for p in FULL_BACKUP_SCRIPT_CANDIDATES)
    raise BackupError(f"full_backup.sh not found. Checked:\n{checked}")


def build_archive() -> str:
    """Runs full_backup.sh into a fresh temp directory and returns the
    path to the .tar.gz it produced. Raises BackupError on failure.

    Runs synchronously (subprocess + script I/O) - call via
    asyncio.to_thread from async code so it doesn't block the event loop.
    """
    script_path = _find_backup_script()
    out_dir = tempfile.mkdtemp(prefix="terminalbot_backup_out_")

    try:
        result = subprocess.run(
            ["bash", script_path, INSTALL_DIR, out_dir],
            capture_output=True, text=True, timeout=BACKUP_SCRIPT_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        shutil.rmtree(out_dir, ignore_errors=True)
        raise BackupError(f"full_backup.sh timed out after {BACKUP_SCRIPT_TIMEOUT_SECONDS}s.")
    except Exception as e:
        shutil.rmtree(out_dir, ignore_errors=True)
        raise BackupError(str(e)[:500])

    if result.returncode != 0:
        shutil.rmtree(out_dir, ignore_errors=True)
        tail = (result.stderr or result.stdout or "").strip()[-500:]
        raise BackupError(f"full_backup.sh failed (exit {result.returncode}): {tail}")

    archives = glob.glob(os.path.join(out_dir, "*.tar.gz"))
    if not archives:
        shutil.rmtree(out_dir, ignore_errors=True)
        raise BackupError("full_backup.sh finished but no .tar.gz was found in its output.")

    return archives[0]


def _format_size(num_bytes: int) -> str:
    mb = num_bytes / (1024 * 1024)
    if mb >= 1:
        return f"{mb:.1f} MB"
    return f"{num_bytes / 1024:.1f} KB"


def _restore_guide_text(archive_name: str) -> str:
    return (
        "📋 How to restore this backup\n\n"
        "1) Install the bot on the target server first (skip this if it's "
        "already installed there):\n"
        "   bash <(curl -fsSL https://raw.githubusercontent.com/Mahersaber2024/Terminal_Bot/main/install.sh)\n\n"
        f"2) Copy this archive ({archive_name}) to that server, e.g. into /root/.\n\n"
        "3) Run:\n"
        f"   cd {INSTALL_DIR}/backups\n"
        f"   sudo bash restore_backup.sh /root/{archive_name} {INSTALL_DIR}\n\n"
        "This restores .env, bot_settings.json, ServerManager data, and the "
        "database, then restarts the service automatically. Full details are "
        "also inside the archive as BACKUP_INFO.txt."
    )


async def run_backup_and_send(bot, chat_id: int = None) -> str:
    """Builds the archive via full_backup.sh, sends it to the log group's
    "🗄 Backups" topic (or to `chat_id` directly if given) along with a
    restore-instructions message, then deletes the local archive either
    way. Never raises - callers just show the returned string to whoever
    triggered it."""
    try:
        archive_path = await asyncio.to_thread(build_archive)
    except BackupError as e:
        return f"✕ Backup failed: {e}"
    except Exception as e:
        logger.error(f"Unexpected error while building backup: {e}")
        return f"✕ Backup failed: {e}"

    archive_name = os.path.basename(archive_path)
    size_text = _format_size(os.path.getsize(archive_path))
    now = datetime.now(timezone.utc)
    caption = (
        "📦 Full backup\n"
        f"🕐 {now.strftime('%Y-%m-%d %H:%M:%S')} UTC\n"
        f"▤ Size: {size_text}\n\n"
        "Database + .env + bot settings + ServerManager data. Restore "
        "instructions follow in the next message."
    )

    target_chat = chat_id if chat_id is not None else bot_settings.get_log_group_id()
    thread_id = None if chat_id is not None else logger_bot.get_thread_id(logger_bot.Topics.BACKUP)

    sent_ok = False
    send_error = None
    if target_chat:
        try:
            with open(archive_path, "rb") as f:
                await bot.send_document(
                    chat_id=target_chat,
                    document=f,
                    filename=archive_name,
                    caption=caption,
                    message_thread_id=thread_id,
                )
            sent_ok = True
            try:
                await bot.send_message(
                    chat_id=target_chat,
                    text=_restore_guide_text(archive_name),
                    message_thread_id=thread_id,
                )
            except Exception as e:
                logger.warning(f"Backup file sent, but the restore-guide message failed: {e}")
        except Exception as e:
            send_error = str(e)
            logger.error(f"Failed to send backup to Telegram: {e}")

    try:
        shutil.rmtree(os.path.dirname(archive_path), ignore_errors=True)
    except Exception as e:
        logger.warning(f"Could not clean up local backup files: {e}")

    if not target_chat:
        return (
            "⚠ Backup created, but no log group is set, so there was nowhere to "
            "send it - it has been deleted from the server. Set a log group first "
            "(▤ Log Group), then try again."
        )
    if not sent_ok:
        return (
            f"✕ Backup was created but could not be sent to Telegram "
            f"({send_error[:200] if send_error else 'unknown error'}) - it has "
            f"still been deleted from the server."
        )

    bot_settings.set_last_backup_at(now.isoformat())
    return f"✓ Backup sent ({size_text}) and removed from the server."


# ====================== Scheduled job ======================

async def backup_job_tick(context):
    """Runs on the JobQueue every bot_settings.get_backup_interval_days()."""
    result = await run_backup_and_send(context.bot)
    logger.info(f"Scheduled backup: {result}")


def get_interval_seconds() -> int:
    return max(1, bot_settings.get_backup_interval_days()) * 86400


def reschedule_job(job_queue, interval_days: int = None):
    """Applies a new interval (if given) and (re)schedules the repeating
    job, removing any previous instance first. Safe to call with
    interval_days=None just to (re)start the job at the currently-saved
    interval, e.g. once at startup."""
    if job_queue is None:
        return
    if interval_days is not None:
        bot_settings.set_backup_interval_days(interval_days)
    for job in job_queue.get_jobs_by_name(JOB_NAME):
        job.schedule_removal()
    interval = get_interval_seconds()
    job_queue.run_repeating(backup_job_tick, interval=interval, first=interval, name=JOB_NAME)
