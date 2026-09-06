"""
telegram_auth.py

Verifies the `initData` string a Telegram Mini App sends on launch, per
Telegram's documented algorithm:
https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app

This is the ONLY thing standing between "any random person on the internet"
and "a shell on your servers", so every websocket connection MUST be
validated with `verify_init_data()` before anything else happens - never
trust a bare server_id/user_id passed in a query string alone.
"""
import hashlib
import hmac
import json
import time
from typing import Optional, Tuple
from urllib.parse import parse_qsl

# How old an initData payload is allowed to be before we reject it.
# Telegram re-signs it fresh every time the Mini App is opened, so this
# mainly protects against someone replaying a captured initData string later.
MAX_INIT_DATA_AGE_SECONDS = 3600


def verify_init_data(init_data: str, bot_token: str, max_age: int = MAX_INIT_DATA_AGE_SECONDS) -> Tuple[bool, Optional[dict]]:
    """Returns (is_valid, user_dict). user_dict is the decoded `user` field
    from initData (contains at least "id") when valid, else None."""
    if not init_data or not bot_token:
        return False, None

    try:
        pairs = dict(parse_qsl(init_data, strict_parsing=True))
    except ValueError:
        return False, None

    received_hash = pairs.pop("hash", None)
    if not received_hash:
        return False, None

    auth_date = pairs.get("auth_date")
    if auth_date:
        try:
            if time.time() - int(auth_date) > max_age:
                return False, None
        except ValueError:
            return False, None

    data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))

    # Telegram's two-step HMAC: first derive a secret from the bot token,
    # then HMAC the data_check_string with that secret.
    secret_key = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    computed_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()

    if not hmac.compare_digest(computed_hash, received_hash):
        return False, None

    user = None
    if "user" in pairs:
        try:
            user = json.loads(pairs["user"])
        except (ValueError, TypeError):
            return False, None

    return True, user
