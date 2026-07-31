"""
config.py
=========
Central place for Terminal Bot settings, loaded from environment variables
(and a local .env file via python-dotenv). Copy .env.example to .env and
fill in the values before running main.py.
"""
import os

from dotenv import load_dotenv

load_dotenv()


def _env_list(name: str):
    raw = os.getenv(name, "")
    return [item.strip() for item in raw.split(",") if item.strip()]


BOT_TOKEN = os.getenv("BOT_TOKEN", "")

# Telegram user IDs who should be treated as bot admins (currently informational -
# wire this into your own admin-only commands/handlers as needed).
ADMIN_IDS = [int(x) for x in _env_list("ADMIN_IDS") if x.isdigit()]

# ====================== Sponsor channel gate ======================
# Every channel listed here must be joined by a user before they can use the
# bot. Leave SPONSOR_CHANNELS in .env empty/unset to disable the gate entirely.
#
# Format in .env (comma-separated "id:title" pairs), e.g.:
#   SPONSOR_CHANNELS=-1001234567890:My Channel,-1009876543210:Second Channel
#
# The bot must be an admin in each channel listed, or the membership check
# (getChatMember) will fail/raise for every user.
SPONSOR_CHANNELS = []
for _entry in _env_list("SPONSOR_CHANNELS"):
    if ":" in _entry:
        _id, _title = _entry.split(":", 1)
    else:
        _id, _title = _entry, _entry
    _id = _id.strip()
    try:
        _id = int(_id)
    except ValueError:
        pass
    SPONSOR_CHANNELS.append({"id": _id, "title": _title.strip()})

# Optional: invite link shown to users for each channel (falls back to the
# channel id/username if not provided). Format "id:https://t.me/...".
SPONSOR_CHANNEL_LINKS = {}
for _entry in _env_list("SPONSOR_CHANNEL_LINKS"):
    if ":" in _entry:
        _id, _link = _entry.split(":", 1)
        SPONSOR_CHANNEL_LINKS[_id.strip()] = _link.strip()
