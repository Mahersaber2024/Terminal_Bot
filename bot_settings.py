"""
bot_settings.py
"""
import json
import logging
import os

import config

logger = logging.getLogger(__name__)

SETTINGS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bot_settings.json")

_cache = None


def _seed_defaults() -> dict:
    channels = []
    for ch in config.SPONSOR_CHANNELS:
        channels.append({
            "id": ch["id"],
            "title": ch["title"],
            "link": config.SPONSOR_CHANNEL_LINKS.get(str(ch["id"]), ""),
        })
    return {
        "sponsor_channels": channels,
        # If .env already had SPONSOR_CHANNELS configured, keep the gate on
        # by default so behaviour doesn't silently change; an empty list
        # means there's nothing to require anyway.
        "membership_required": bool(channels),
        # Card-to-card payment info shown to users paying for a subscription
        # or wallet top-up (see admin.py "Payment Settings" and
        # subscription.py sub_pay_card_*). Empty until an admin sets it via
        # the admin panel.
        "card_number": "",
        "card_holder": "",
        "card_bank": "",
        # Server-health monitoring knobs (see ServerManager/health.py and
        # admin.py "🩺 Monitoring Settings"). Seeded from the SERVERMGR_*
        # env vars / previous hardcoded defaults so upgrading doesn't change
        # existing behaviour - from here on these are only changed via the
        # admin panel, not the .env file.
        "health_interval_seconds": int(os.getenv("SERVERMGR_HEALTH_INTERVAL", "300")),
        "health_check_timeout": int(os.getenv("SERVERMGR_HEALTH_TIMEOUT", "12")),
        "disk_alert_percent": int(os.getenv("SERVERMGR_DISK_ALERT_PERCENT", "90")),
        "disk_alert_hysteresis": 5,
        "cpu_alert_percent": 90,
        "ram_alert_percent": 90,
        # Owner Server (the host the bot itself runs on) - see
        # ServerManager/owner_server.py and admin.py's "🖥 Owner Server"
        # menu. Admin-only; on by default so upgrading doesn't silently
        # turn host monitoring off.
        "owner_monitor_enabled": True,
        # IANA timezone name (e.g. "Europe/Berlin") the Owner Server card's
        # clock is rendered in - independent of whatever TZ the host OS
        # itself is set to. See ServerManager/owner_server.server_time_info()
        # and admin.py's "🕐 Set Timezone" button. Defaults to UTC so a
        # fresh install always shows something valid.
        "owner_timezone": os.getenv("OWNER_TIMEZONE", "UTC"),
        # Admin-added buttons for the Owner Server Terminal's "⚡️ Quick"
        # menu (see admin.py's OWNERSRV_QUICK_COMMANDS for the built-in
        # ones this list is appended to). Each entry is {"label", "cmd"}.
        # Managed entirely from the "➕ Add command" / "🗑 Remove" buttons
        # in that menu - never touched by the .env file.
        "owner_quick_commands": [],
        # Telegram group id used as the activity log group (see
        # logger_bot.py). Set via the /setloggroup command run inside the
        # target group. None until an admin sets it.
        "log_group_id": None,
        # Display name appended to log-group topic titles (see
        # logger_bot.py's _topic_display_name), e.g. "New Users(Terminal Bot)".
        # Defaults to "Terminal Bot"; changeable via the "▤ Log Group" admin menu.
        "bot_name": "Terminal Bot",
        # ====================== Full backup settings ======================
        # How often the automatic full-backup job (see backup_manager.py and
        # main.py's job-queue wiring) runs, in days. Changeable from the
        # admin panel's "🗄 Backup" menu; changing it also reschedules the
        # background job (see backup_manager.reschedule_job()).
        "backup_interval_days": int(os.getenv("BACKUP_INTERVAL_DAYS", "3")),
        # ISO-8601 UTC timestamp of the last successfully-sent backup (manual
        # or automatic). None until the first backup is sent. Purely
        # informational - shown in the "🗄 Backup" menu.
        "last_backup_at": None,
    }


def _load() -> dict:
    global _cache
    if _cache is not None:
        return _cache
    data = None
    if os.path.exists(SETTINGS_FILE):
        try:
            with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            logger.error(f"Error loading bot_settings.json: {e}")
            data = None
    if data is None:
        data = _seed_defaults()
    data.setdefault("sponsor_channels", [])
    data.setdefault("membership_required", bool(data["sponsor_channels"]))
    data.setdefault("card_number", "")
    data.setdefault("card_holder", "")
    data.setdefault("card_bank", "")
    data.setdefault("health_interval_seconds", int(os.getenv("SERVERMGR_HEALTH_INTERVAL", "300")))
    data.setdefault("health_check_timeout", int(os.getenv("SERVERMGR_HEALTH_TIMEOUT", "12")))
    data.setdefault("disk_alert_percent", int(os.getenv("SERVERMGR_DISK_ALERT_PERCENT", "90")))
    data.setdefault("disk_alert_hysteresis", 5)
    data.setdefault("cpu_alert_percent", 90)
    data.setdefault("ram_alert_percent", 90)
    data.setdefault("owner_monitor_enabled", True)
    data.setdefault("owner_timezone", os.getenv("OWNER_TIMEZONE", "UTC"))
    data.setdefault("owner_quick_commands", [])
    data.setdefault("log_group_id", None)
    data.setdefault("bot_name", "Terminal Bot")
    data.setdefault("backup_interval_days", int(os.getenv("BACKUP_INTERVAL_DAYS", "3")))
    data.setdefault("last_backup_at", None)
    _cache = data
    return data


def _save(data: dict):
    global _cache
    with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    _cache = data


# ====================== Sponsor channel gate ======================

def get_sponsor_channels() -> list:
    """List of {"id", "title", "link"} dicts currently required to join."""
    return list(_load()["sponsor_channels"])


def is_membership_required() -> bool:
    return bool(_load()["membership_required"])

def set_membership_required(value: bool):
    data = _load()
    data["membership_required"] = bool(value)
    _save(data)


def add_sponsor_channel(channel_id, title: str = "", link: str = "") -> dict:
    """channel_id can be a numeric chat id (int/str) or an @username."""
    raw = str(channel_id).strip()
    try:
        stored_id = int(raw)
    except ValueError:
        stored_id = raw if raw.startswith("@") else f"@{raw.lstrip('@')}"

    channel = {
        "id": stored_id,
        "title": (title or raw).strip(),
        "link": (link or "").strip(),
    }
    data = _load()
    data["sponsor_channels"].append(channel)
    _save(data)
    return channel


def remove_sponsor_channel(index: int) -> bool:
    data = _load()
    channels = data["sponsor_channels"]
    if 0 <= index < len(channels):
        channels.pop(index)
        _save(data)
        return True
    return False


# ====================== Payment settings (card-to-card) ======================
# Bank card an admin sets via admin.py's "💳 Payment Settings" menu, and that
# subscription.py shows to users paying by card-to-card for a subscription
# or wallet top-up.

def get_card_number() -> str:
    return _load().get("card_number", "")


def get_card_holder() -> str:
    return _load().get("card_holder", "")


def get_card_bank() -> str:
    return _load().get("card_bank", "")


def is_card_payment_configured() -> bool:
    """True once an admin has set at least a card number - subscription.py
    blocks the card-to-card payment flow until this is true."""
    return bool(get_card_number())


def set_card_info(card_number: str = "", card_holder: str = "", card_bank: str = ""):
    data = _load()
    data["card_number"] = (card_number or "").strip()
    data["card_holder"] = (card_holder or "").strip()
    data["card_bank"] = (card_bank or "").strip()
    _save(data)


# ====================== Server-health monitoring settings ======================
# Read by ServerManager/health.py on every tick (so threshold/timeout/
# hysteresis changes apply immediately), and by main.py when scheduling the
# repeating job. Editable from admin.py's "🩺 Monitoring Settings" menu -
# changing the interval also reschedules the background job, see
# ServerManager/health.reschedule_job().

def get_health_interval_seconds() -> int:
    return int(_load().get("health_interval_seconds", 300))


def get_health_check_timeout() -> int:
    return int(_load().get("health_check_timeout", 12))


def get_disk_alert_percent() -> int:
    return int(_load().get("disk_alert_percent", 90))


def get_disk_alert_hysteresis() -> int:
    return int(_load().get("disk_alert_hysteresis", 5))


def set_health_interval_seconds(value: int):
    data = _load()
    data["health_interval_seconds"] = int(value)
    _save(data)


def set_health_check_timeout(value: int):
    data = _load()
    data["health_check_timeout"] = int(value)
    _save(data)


def set_disk_alert_percent(value: int):
    data = _load()
    data["disk_alert_percent"] = int(value)
    _save(data)


def set_disk_alert_hysteresis(value: int):
    data = _load()
    data["disk_alert_hysteresis"] = int(value)
    _save(data)


def get_cpu_alert_percent() -> int:
    return int(_load().get("cpu_alert_percent", 90))


def get_ram_alert_percent() -> int:
    return int(_load().get("ram_alert_percent", 90))


def set_cpu_alert_percent(value: int):
    data = _load()
    data["cpu_alert_percent"] = int(value)
    _save(data)


def set_ram_alert_percent(value: int):
    data = _load()
    data["ram_alert_percent"] = int(value)
    _save(data)


# ====================== Owner Server (host) monitoring toggle ======================
# Whether the background health-monitor job (ServerManager/health.py) also
# sweeps the local host, in addition to every registered remote server.
# Alerts go to every id in config.ADMIN_IDS, never to regular users - see
# health.py's owner-server tick and admin.py's "🖥 Owner Server" menu.

def is_owner_monitor_enabled() -> bool:
    return bool(_load().get("owner_monitor_enabled", True))


def set_owner_monitor_enabled(value: bool):
    data = _load()
    data["owner_monitor_enabled"] = bool(value)
    _save(data)


# ====================== Owner Server (host) clock timezone ======================
# Purely a display setting for the Owner Server card's date/time line - it
# does not touch the host's actual system timezone. Validated against the
# IANA tz database by the caller (admin.py) before being saved here;
# server_time_info() falls back to UTC on its own if an invalid name ever
# ends up on disk.

def get_owner_timezone() -> str:
    return _load().get("owner_timezone", "UTC")


def set_owner_timezone(value: str):
    data = _load()
    data["owner_timezone"] = (value or "UTC").strip()
    _save(data)


# ====================== Owner Server Terminal - custom quick commands ======================
# Admin-added buttons appended to admin.py's built-in OWNERSRV_QUICK_COMMANDS
# in the "⚡️ Quick" menu (see admin.py's _ownersrv_all_quick_commands()).
# Each entry is {"label": <button text>, "cmd": <shell command>}.

def get_owner_quick_commands() -> list:
    return list(_load().get("owner_quick_commands", []))


def add_owner_quick_command(label: str, cmd: str) -> dict:
    entry = {"label": (label or cmd).strip(), "cmd": (cmd or "").strip()}
    data = _load()
    data.setdefault("owner_quick_commands", []).append(entry)
    _save(data)
    return entry


def remove_owner_quick_command(index: int) -> bool:
    data = _load()
    commands = data.setdefault("owner_quick_commands", [])
    if 0 <= index < len(commands):
        commands.pop(index)
        _save(data)
        return True
    return False


# ====================== Log group (logger_bot.py) ======================
# Telegram group used for activity logs. Set via the /setloggroup command
# run inside the target group (see admin.py's admin_set_log_group).

def get_log_group_id():
    return _load().get("log_group_id")


def set_log_group_id(group_id: int):
    data = _load()
    data["log_group_id"] = int(group_id)
    _save(data)


# ====================== Bot display name (logger_bot.py topics) ======================
# Suffix appended to log-group topic titles, e.g. "New Users(Terminal Bot)".
# Set via the "▤ Log Group" admin menu's "✎ Set Bot Name" button
# (see admin.py's admin_botname_set_start/_input).

def get_bot_name() -> str:
    return _load().get("bot_name", "Terminal Bot")


def set_bot_name(value: str):
    data = _load()
    data["bot_name"] = (value or "").strip()
    _save(data)


# ====================== Full backup settings (backup_manager.py) ======================
# Interval (days) between automatic full backups, and when the last one was
# successfully sent. Editable from the admin panel's "🗄 Backup" menu -
# changing the interval also reschedules the background job, see
# backup_manager.reschedule_job().

def get_backup_interval_days() -> int:
    return int(_load().get("backup_interval_days", 3))


def set_backup_interval_days(value: int):
    data = _load()
    data["backup_interval_days"] = int(value)
    _save(data)


def get_last_backup_at():
    """Returns an ISO-8601 UTC timestamp string, or None if no backup has
    ever been sent yet."""
    return _load().get("last_backup_at")


def set_last_backup_at(value: str):
    data = _load()
    data["last_backup_at"] = value
    _save(data)
