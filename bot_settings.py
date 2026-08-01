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
