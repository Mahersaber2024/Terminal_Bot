"""
db/config.py
============
PostgreSQL connection settings for Terminal Bot, loaded from environment
variables (same .env file used by the top-level config.py, via
python-dotenv). Add these to your .env:

    DB_HOST=localhost
    DB_PORT=5432
    DB_NAME=terminal_bot
    DB_USER=terminal_bot
    DB_PASSWORD=change-me

This mirrors the pattern used by the nomone.py sample's db/config.py
(get_db_config() returning a dict ready for psycopg2.connect(**cfg)),
adapted to this project's env-var based configuration instead of a
config.json file.
"""
import os

from dotenv import load_dotenv

load_dotenv()


class _Config:
    def get_db_config(self) -> dict:
        return {
            "host": os.getenv("DB_HOST", "localhost"),
            "port": int(os.getenv("DB_PORT", "5432")),
            "database": os.getenv("DB_NAME", "terminal_bot"),
            "user": os.getenv("DB_USER", "terminal_bot"),
            "password": os.getenv("DB_PASSWORD", ""),
        }


# Singleton, imported as `from db.config import config` (same call-site
# shape as the nomone.py sample uses).
config = _Config()
