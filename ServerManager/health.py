# ====================== Server health monitoring ======================

import logging

from telegram.error import TelegramError

from . import settings
from . import engine
from . import owner_server
import bot_settings
import config
import logger_bot

logger = logging.getLogger(__name__)

# Name of the repeating job registered in main.py
JOB_NAME = "servermgr_health_monitor"

# Stored in bot_settings.json, looked up fresh each tick (editable from admin.py)


def get_interval_seconds() -> int:
    return bot_settings.get_health_interval_seconds()


def get_check_timeout() -> int:
    return bot_settings.get_health_check_timeout()


def get_disk_alert_percent() -> int:
    return bot_settings.get_disk_alert_percent()


def get_disk_alert_hysteresis() -> int:
    return bot_settings.get_disk_alert_hysteresis()


def get_cpu_alert_percent() -> int:
    return bot_settings.get_cpu_alert_percent()


def get_ram_alert_percent() -> int:
    return bot_settings.get_ram_alert_percent()


def reschedule_job(job_queue, interval: int = None):
    """Removes the existing repeating health-monitor job and re-adds it with the current interval."""
    if job_queue is None:
        return False
    interval = interval if interval is not None else get_interval_seconds()
    for job in job_queue.get_jobs_by_name(JOB_NAME):
        job.schedule_removal()
    job_queue.run_repeating(health_monitor_tick, interval=interval, first=5, name=JOB_NAME)
    return True


# server_id -> {"up": bool, "disk_alerted": bool}
_last_state = {}

# Same shape as _last_state, but for the Owner Server (never "down")
_owner_state = {"disk_alerted": False, "cpu_alerted": False, "ram_alerted": False}


def get_last_known_status(server_id: str):
    """Last-known up/down state from the periodic monitor, or None if never checked."""
    state = _last_state.get(server_id)
    return state.get("up") if state else None


def _fmt_uptime(seconds) -> str:
    if not seconds:
        return "-"
    seconds = int(seconds)
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, _ = divmod(rem, 60)
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if not days and minutes:
        parts.append(f"{minutes}m")
    return " ".join(parts) or "<1m"


def _bar(pct, width: int = 10) -> str:
    if pct is None:
        return "?"
    filled = min(width, max(0, round(pct / 100 * width)))
    return "█" * filled + "░" * (width - filled)


def format_health_text(label: str, health: dict) -> str:
    if not health.get("ok"):
        return f"[DOWN] *{label}* -- unreachable\n`{health.get('error') or 'connection failed'}`"

    cpu = health.get("cpu_percent")
    mem_used, mem_total = health.get("mem_used_mb"), health.get("mem_total_mb")
    mem_pct = (mem_used / mem_total * 100) if mem_total else None
    disk_pct = health.get("disk_percent")
    disk_used_gb = (health.get("disk_used_kb") or 0) / 1024 / 1024
    disk_total_gb = (health.get("disk_total_kb") or 0) / 1024 / 1024
    uptime = _fmt_uptime(health.get("uptime_seconds"))

    lines = [f"[HEALTH] *{label}* -- [UP] online (up {uptime})", ""]
    lines.append(f"CPU   {_bar(cpu)}  {cpu:.0f}%" if cpu is not None else "CPU   -")
    lines.append(
        f"RAM   {_bar(mem_pct)}  {mem_pct:.0f}%  ({mem_used:.0f}/{mem_total:.0f} MB)"
        if mem_pct is not None else "RAM   -"
    )
    lines.append(
        f"Disk  {_bar(disk_pct)}  {disk_pct:.0f}%  ({disk_used_gb:.1f}/{disk_total_gb:.1f} GB)"
        if disk_pct is not None else "Disk  -"
    )
    return "\n".join(lines)


async def _notify(bot, chat_id, text: str):
    try:
        await bot.send_message(chat_id=int(chat_id), text=text, parse_mode="Markdown")
    except TelegramError as e:
        logger.warning(f"health alert delivery failed for chat {chat_id}: {e}")
    except Exception as e:
        logger.warning(f"health alert delivery failed for chat {chat_id}: {e}")


async def _log_error(bot, error: str, context: str):
    """Best-effort report of an unexpected *bot* exception into the log group -
    i.e. bugs in our code, never anything about a user's own server."""
    try:
        await logger_bot.log_system_error(bot, error, context=context)
    except Exception as e:
        logger.warning(f"could not send system-error log: {e}")


async def health_monitor_tick(context):
    """One full sweep of every monitored server, each check run in a worker thread."""
    import asyncio

    check_timeout = get_check_timeout()
    disk_alert_percent = get_disk_alert_percent()
    disk_alert_hysteresis = get_disk_alert_hysteresis()

    for user_id, server in settings.get_all_servers():
        if not server.get("monitor_enabled", True):
            continue

        sid = server["id"]
        try:
            health = await asyncio.wait_for(
                asyncio.to_thread(engine.check_health, server, check_timeout),
                timeout=check_timeout + 5,
            )
        except asyncio.TimeoutError:
            health = {"ok": False, "error": f"timed out after {check_timeout}s"}
        except Exception as e:
            logger.warning(f"health check crashed for server {sid}: {e}")
            await _log_error(context.bot, str(e), context=f"health check crashed for server '{sid}' (owner {user_id})")
            continue

        prev = _last_state.get(sid, {})
        was_up = prev.get("up")   # None = never checked before
        now_up = bool(health.get("ok"))

        if was_up is not None and now_up != was_up:
            if now_up:
                text = f"[UP] *{server['label']}* is back up -- `{server['host']}`"
            else:
                text = (
                    f"[DOWN] *{server['label']}* ({server['host']}) is unreachable:\n"
                    f"`{health.get('error') or 'connection failed'}`"
                )
            await _notify(context.bot, user_id, text)

        disk_alerted = prev.get("disk_alerted", False)
        disk_pct = health.get("disk_percent") if now_up else None
        if disk_pct is not None:
            if disk_pct >= disk_alert_percent and not disk_alerted:
                disk_text = (
                    f"[DISK] *{server['label']}* disk is at {disk_pct}% (threshold {disk_alert_percent}%) "
                    f"-- `{server['host']}`"
                )
                await _notify(context.bot, user_id, disk_text)
                disk_alerted = True
            elif disk_pct < disk_alert_percent - disk_alert_hysteresis:
                disk_alerted = False

        _last_state[sid] = {"up": now_up, "disk_alerted": disk_alerted}

    if bot_settings.is_owner_monitor_enabled():
        await _check_owner_server(context.bot, disk_alert_percent, disk_alert_hysteresis)


async def _check_owner_server(bot, disk_alert_percent: int, disk_alert_hysteresis: int):
    """Same threshold/hysteresis alerting as the per-server loop, but for the local host - always DMs config.ADMIN_IDS."""
    import asyncio

    try:
        snap = await asyncio.to_thread(owner_server.snapshot)
    except Exception as e:
        logger.warning(f"owner server health check crashed: {e}")
        await _log_error(bot, str(e), context="owner server health check crashed")
        return

    cpu_alert_percent = get_cpu_alert_percent()
    ram_alert_percent = get_ram_alert_percent()
    hysteresis = disk_alert_hysteresis

    async def _alert_all(text: str):
        for admin_id in config.ADMIN_IDS:
            await _notify(bot, admin_id, text)

    cpu_pct = snap.get("cpu_percent")
    if cpu_pct is not None:
        if cpu_pct >= cpu_alert_percent and not _owner_state["cpu_alerted"]:
            await _alert_all(f"[SERVER] *Owner Server* CPU is at {cpu_pct:.0f}% (threshold {cpu_alert_percent}%)")
            _owner_state["cpu_alerted"] = True
        elif cpu_pct < cpu_alert_percent - hysteresis:
            _owner_state["cpu_alerted"] = False

    ram_pct = (snap.get("memory") or {}).get("percent")
    if ram_pct is not None:
        if ram_pct >= ram_alert_percent and not _owner_state["ram_alerted"]:
            await _alert_all(f"[SERVER] *Owner Server* RAM is at {ram_pct:.0f}% (threshold {ram_alert_percent}%)")
            _owner_state["ram_alerted"] = True
        elif ram_pct < ram_alert_percent - hysteresis:
            _owner_state["ram_alerted"] = False

    disk_pct = max((d["percent"] for d in snap.get("disks", []) if d.get("percent") is not None), default=None)
    if disk_pct is not None:
        if disk_pct >= disk_alert_percent and not _owner_state["disk_alerted"]:
            await _alert_all(f"[SERVER] *Owner Server* disk is at {disk_pct:.0f}% (threshold {disk_alert_percent}%)")
            _owner_state["disk_alerted"] = True
        elif disk_pct < disk_alert_percent - hysteresis:
            _owner_state["disk_alerted"] = False
