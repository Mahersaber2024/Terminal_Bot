"""
crypto_utils.py
================
Small helper used by ServerManager.server_manager_settings to keep SSH
passwords/private keys encrypted on disk (server_manager_settings.json),
while the rest of the code works with plaintext in memory.

Uses Fernet (symmetric, authenticated encryption) from the `cryptography`
package. The key is derived from CRYPTO_SECRET (set it in your .env - see
.env.example) so restarting the bot with the same secret can still decrypt
previously-saved credentials.

Unlike earlier versions of this file, a missing CRYPTO_SECRET is NOT papered
over with a random throwaway key anymore: that used to let the bot start
"successfully" while silently guaranteeing that every credential saved during
that run would turn into permanently undecryptable garbage the moment the
process restarted (e.g. after a crash, a deploy, or a reboot) with a
different throwaway key. Instead, any attempt to actually encrypt/decrypt
something without CRYPTO_SECRET configured raises a clear, actionable error
- see IS_CONFIGURED below, which main.py checks at startup so this fails
immediately and obviously instead of surfacing as silent data loss later.
"""
import base64
import hashlib
import logging
import os

from cryptography.fernet import Fernet, InvalidToken

logger = logging.getLogger(__name__)

# Below this many characters, CRYPTO_SECRET is weak enough to be worth
# calling out - still allowed (so we don't brick existing installs), but
# logged loudly so whoever's running the bot notices.
MIN_SECRET_LENGTH = 16

_SECRET = (os.getenv("CRYPTO_SECRET") or "").strip()

# main.py checks this at startup and refuses to launch with a clear message
# if it's False, instead of letting the failure surface later as garbled
# passwords the first time someone restarts the process.
IS_CONFIGURED = bool(_SECRET)

_fernet = None
_warned_weak_secret = False


def _get_fernet() -> Fernet:
    """Builds the Fernet cipher lazily, on first actual use, rather than at
    import time - so importing this module never crashes the whole bot just
    because sponsor-channel/admin features (which don't need encryption) are
    being used without CRYPTO_SECRET set; only the SSH-credential code paths
    that truly need it will raise."""
    global _fernet, _warned_weak_secret

    if _fernet is not None:
        return _fernet

    if not IS_CONFIGURED:
        raise RuntimeError(
            "CRYPTO_SECRET is not set, so stored SSH passwords/private keys can't be "
            "encrypted or decrypted. Set CRYPTO_SECRET in your .env to a fixed random "
            "value, e.g.:\n"
            '  python3 -c "import secrets; print(secrets.token_urlsafe(32))"\n'
            "then restart the bot. (A previous version of this file auto-generated a "
            "throwaway secret here instead - that silently made every saved credential "
            "unreadable on the next restart, which is worse than failing now.)"
        )

    if len(_SECRET) < MIN_SECRET_LENGTH and not _warned_weak_secret:
        logger.warning(
            f"CRYPTO_SECRET is shorter than {MIN_SECRET_LENGTH} characters - this makes "
            f"the encryption of stored SSH passwords/keys easier to brute-force. Consider "
            f"replacing it with a longer, randomly generated value."
        )
        _warned_weak_secret = True

    # Fernet needs a 32-byte urlsafe-base64 key; derive one from whatever string
    # the user provided so CRYPTO_SECRET can just be a normal passphrase.
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
        # Most likely CRYPTO_SECRET changed since this was saved, or the
        # value was never encrypted (e.g. hand-edited json). Fail soft
        # rather than crashing the whole settings load.
        logger.error("Failed to decrypt a stored value - wrong CRYPTO_SECRET or corrupted data.")
        return value
