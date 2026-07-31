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
