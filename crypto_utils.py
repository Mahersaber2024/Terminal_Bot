"""
crypto_utils.py
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
        logger.error("Failed to decrypt a stored value - wrong CRYPTO_SECRET or corrupted data.")
        return value
