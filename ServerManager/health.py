# ====================== Server health monitoring ======================

import logging

from telegram.error import TelegramError

from . import settings
from . import engine
import bot_settings

logger = logging.getLogger(__name__)

# Name of the repeating job registered in main.py - shared here so
# reschedule_job() (called from admin.py after an interval change) finds
# the same job without either module having to hardcode the string twice.
JOB_NAME = "servermgr_health_monitor"

# All four knobs below used to be fixed at startup (env vars / a hardcoded
# constant). They're now stored in bot_settings.json and editable from
# admin.py's "🩺 Monitoring Settings" menu, so we look them up fresh each
# time rather than caching them at import time - that way a change takes
# effect on the very next tick. The interval is the one exception: it's
# also baked into the job_queue's schedule, so changing it additionally
# requires reschedule_job() to actually take effect (admin.py does this).


def get_interval_seconds() -> int:
    return bot_settings.get_health_interval_seconds()


def get_check_timeout() -> int:
    return bot_settings.get_health_check_timeout()


def get_disk_alert_percent() -> int:
    return bot_settings.get_disk_alert_percent()


def get_disk_alert_hysteresis() -> int:
    return bot_settings.get_disk_alert_hysteresis()


def reschedule_job(job_queue, interval: int = None):
    """Removes the existing repeating health-monitor job (if any) and
    re-adds it with the current interval. Called by admin.py right after
    the interval setting is changed, since JobQueue doesn't let us mutate
    a job's interval in place. `first=5` so the new schedule is felt
    quickly rather than waiting a full interval."""
    if job_queue is None:
        return False
    interval = interval if interval is not None else get_interval_seconds()
    for job in job_queue.get_jobs_by_name(JOB_NAME):
        job.schedule_removal()
    job_queue.run_repeating(health_monitor_tick, interval=interval, first=5, name=JOB_NAME)
    return True


# server_id -> {"up": bool, "disk_alerted": bool}
_last_state = {}


def get_last_known_status(server_id: str):
    """Last-known up/down state from the periodic monitor - None if this
    server has never been checked yet (just added, monitoring disabled for
    it, or the bot was restarted since)."""
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
        return f"🔴 *{label}* — unreachable\n`{health.get('error') or 'connection failed'}`"

    cpu = health.get("cpu_percent")
    mem_used, mem_total = health.get("mem_used_mb"), health.get("mem_total_mb")
    mem_pct = (mem_used / mem_total * 100) if mem_total else None
    disk_pct = health.get("disk_percent")
    disk_used_gb = (health.get("disk_used_kb") or 0) / 1024 / 1024
    disk_total_gb = (health.get("disk_total_kb") or 0) / 1024 / 1024
    uptime = _fmt_uptime(health.get("uptime_seconds"))

    lines = [f"🩺 *{label}* — 🟢 online (up {uptime})", ""]
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


async def health_monitor_tick(context):
    """job_queue.run_repeating() callback - one full sweep of every
    monitored server. Runs each SSH check in a worker thread (via
    asyncio.to_thread) so a slow/unreachable server never blocks the bot's
    event loop or the rest of the sweep."""
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
            continue

        prev = _last_state.get(sid, {})
        was_up = prev.get("up")   # None = never checked before (e.g. just added, or since last restart)
        now_up = bool(health.get("ok"))

        if was_up is not None and now_up != was_up:
            if now_up:
                text = f"✅ *{server['label']}* is back up — `{server['host']}`"
            else:
                text = (
                    f"🔴 *{server['label']}* ({server['host']}) is unreachable:\n"
                    f"`{health.get('error') or 'connection failed'}`"
                )
            await _notify(context.bot, user_id, text)

        disk_alerted = prev.get("disk_alerted", False)
        disk_pct = health.get("disk_percent") if now_up else None
        if disk_pct is not None:
            if disk_pct >= disk_alert_percent and not disk_alerted:
                await _notify(
                    context.bot, user_id,
                    f"💾 *{server['label']}* disk is at {disk_pct}% (threshold {disk_alert_percent}%) "
                    f"— `{server['host']}`",
                )
                disk_alerted = True
            elif disk_pct < disk_alert_percent - disk_alert_hysteresis:
                disk_alerted = False

        _last_state[sid] = {"up": now_up, "disk_alerted": disk_alerted}
