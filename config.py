"""
config.py

"""
import os

from dotenv import load_dotenv

load_dotenv()


def _env_list(name: str):
    raw = os.getenv(name, "")
    return [item.strip() for item in raw.split(",") if item.strip()]


# ====================== Telegram / bot settings ======================

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


# ====================== Database (PostgreSQL) settings ======================
# Used by setup_db.py and db/database.py. Add these to your .env:
#
#   DB_HOST=localhost
#   DB_PORT=5432
#   DB_NAME=terminal_bot
#   DB_USER=terminal_bot
#   DB_PASSWORD=change-me

DB_HOST = os.getenv("DB_HOST", "localhost")
DB_PORT = int(os.getenv("DB_PORT", "5432"))
DB_NAME = os.getenv("DB_NAME", "terminal_bot")
DB_USER = os.getenv("DB_USER", "terminal_bot")
DB_PASSWORD = os.getenv("DB_PASSWORD", "")


def get_db_config() -> dict:
    """Returns a dict ready to be passed as psycopg2.connect(**cfg) /
    db.database.Database(get_db_config())."""
    return {
        "host": DB_HOST,
        "port": DB_PORT,
        "database": DB_NAME,
        "user": DB_USER,
        "password": DB_PASSWORD,
    }
