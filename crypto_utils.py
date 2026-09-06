"""
crypto_utils.py

"""
import base64
import hashlib
import logging
import os

from cryptography.fernet import Fernet, InvalidToken

logger = logging.getLogger(__name__)

# Below this length, CRYPTO_SECRET is weak (allowed, but logged).
MIN_SECRET_LENGTH = 16

_SECRET = (os.getenv("CRYPTO_SECRET") or "").strip()

# main.py checks this at startup and refuses to launch if False.
IS_CONFIGURED = bool(_SECRET)

_fernet = None
_warned_weak_secret = False


def _get_fernet() -> Fernet:
    global _fernet, _warned_weak_secret

    if _fernet is not None:
        return _fernet

    if not IS_CONFIGURED:
        raise RuntimeError(
            "CRYPTO_SECRET is not set, so stored SSH passwords/private keys can't be "
            "encrypted or decrypted. Set CRYPTO_SECRET in your .env to a fixed random "
            "value, e.g.:\n"
            '  python3 -c "import secrets; print(secrets.token_urlsafe(32))"\n'
            "then restart the bot."
        )

    if len(_SECRET) < MIN_SECRET_LENGTH and not _warned_weak_secret:
        logger.warning(
            f"CRYPTO_SECRET is shorter than {MIN_SECRET_LENGTH} characters - consider "
            f"replacing it with a longer, randomly generated value."
        )
        _warned_weak_secret = True

    # Fernet needs a 32-byte urlsafe-base64 key; derive one from the secret.
    key = base64.urlsafe_b64encode(hashlib.sha256(_SECRET.encode()).digest())
    _fernet = Fernet(key)
    return _fernet


def encrypt_value(value: str) -> str:
    if value is None:
        return value
    return _get_fernet().encrypt(value.encode()).decode()


def decrypt_value(value: str) -> str:
    if value is None:
        return value
    try:
        return _get_fernet().decrypt(value.encode()).decode()
    except InvalidToken:
        # Likely CRYPTO_SECRET changed, or the value was never encrypted.
        logger.error("Failed to decrypt a stored value - wrong CRYPTO_SECRET or corrupted data.")
        return value
