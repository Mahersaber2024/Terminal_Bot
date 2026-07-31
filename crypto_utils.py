"""
crypto_utils.py
================
Small helper used by ServerManager.server_manager_settings to keep SSH
passwords encrypted on disk (server_manager_settings.json), while the rest
of the code works with plaintext in memory.

Uses Fernet (symmetric, authenticated encryption) from the `cryptography`
package. The key is derived once from CRYPTO_SECRET (set it in your .env -
see .env.example) so restarting the bot with the same secret can still
decrypt previously-saved passwords. If CRYPTO_SECRET is missing, a random
key is generated and printed once - fine for quick testing, but anything
saved will become unreadable the next time the process starts, so set a
real secret for anything you care about.
"""
import base64
import hashlib
import logging
import os

from cryptography.fernet import Fernet, InvalidToken

logger = logging.getLogger(__name__)

_SECRET = os.getenv("CRYPTO_SECRET")

if not _SECRET:
    _SECRET = Fernet.generate_key().decode()
    print(
        "\n⚠️  CRYPTO_SECRET is not set - generated a temporary one for this run only.\n"
        "   Saved server passwords will NOT be readable after a restart.\n"
        "   Set CRYPTO_SECRET in your .env to a fixed random value to persist them.\n"
    )

# Fernet needs a 32-byte urlsafe-base64 key; derive one from whatever string
# the user provided so CRYPTO_SECRET can just be a normal passphrase.
_key = base64.urlsafe_b64encode(hashlib.sha256(_SECRET.encode()).digest())
_fernet = Fernet(_key)


def encrypt_value(value: str) -> str:
    if value is None:
        return value
    return _fernet.encrypt(value.encode()).decode()


def decrypt_value(value: str) -> str:
    if value is None:
        return value
    try:
        return _fernet.decrypt(value.encode()).decode()
    except InvalidToken:
        # Most likely CRYPTO_SECRET changed since this was saved, or the
        # value was never encrypted (e.g. hand-edited json). Fail soft
        # rather than crashing the whole settings load.
        logger.error("Failed to decrypt a stored value - wrong CRYPTO_SECRET or corrupted data.")
        return value
