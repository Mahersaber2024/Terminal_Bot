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

# Telegram user IDs treated as bot admins.
ADMIN_IDS = [int(x) for x in _env_list("ADMIN_IDS") if x.isdigit()]

# ====================== Sponsor channel gate ======================
# Leave SPONSOR_CHANNELS empty/unset to disable the gate.
# Format: comma-separated "id:title" pairs.
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

# Optional invite link per channel. Format "id:https://t.me/...".
SPONSOR_CHANNEL_LINKS = {}
for _entry in _env_list("SPONSOR_CHANNEL_LINKS"):
    if ":" in _entry:
        _id, _link = _entry.split(":", 1)
        SPONSOR_CHANNEL_LINKS[_id.strip()] = _link.strip()


# ====================== Database (PostgreSQL) settings ======================

DB_HOST = os.getenv("DB_HOST", "localhost")
DB_PORT = int(os.getenv("DB_PORT", "5432"))
DB_NAME = os.getenv("DB_NAME", "terminal_bot")
DB_USER = os.getenv("DB_USER", "terminal_bot")
DB_PASSWORD = os.getenv("DB_PASSWORD", "")


def get_db_config() -> dict:
    """Returns a dict ready to be passed as psycopg2.connect(**cfg)."""
    return {
        "host": DB_HOST,
        "port": DB_PORT,
        "database": DB_NAME,
        "user": DB_USER,
        "password": DB_PASSWORD,
    }
