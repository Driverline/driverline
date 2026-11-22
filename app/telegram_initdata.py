"""Verify Telegram Mini App initData using the HMAC method Telegram documents.

Standalone: standard library only, no imports from the rest of the backend.
The caller passes the bot token in; this module never reads the environment.
"""
import hashlib
import hmac
import json
import time
from typing import Optional
from urllib.parse import parse_qsl

# Tolerate a small clock difference between Telegram and this server.
FUTURE_SKEW_SECONDS = 60


class InitDataError(ValueError):
    """initData is missing, forged, malformed or too old."""


def verify_init_data(
    init_data: str,
    bot_token: str,
    max_age: int = 3600,
    now: Optional[float] = None,
) -> dict:
    """Return {"telegram_id", "username", "auth_date"} or raise InitDataError.

    The Telegram ID comes only from signed data, never from the request body.
    Never log init_data or bot_token.
    """
    if not init_data or not bot_token:
        raise InitDataError("missing init data or bot token")

    pairs = dict(parse_qsl(init_data, keep_blank_values=True))
    received_hash = pairs.pop("hash", "")
    if not received_hash:
        raise InitDataError("missing hash")

    check_string = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    expected = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, received_hash):
        raise InitDataError("bad signature")

    try:
        auth_date = int(pairs["auth_date"])
    except (KeyError, ValueError):
        raise InitDataError("bad auth_date")

    current = time.time() if now is None else now
    age = current - auth_date
    if age > max_age or age < -FUTURE_SKEW_SECONDS:
        raise InitDataError("expired")

    try:
        user = json.loads(pairs["user"])
    except (KeyError, ValueError):
        raise InitDataError("missing user")
    if not isinstance(user, dict) or not isinstance(user.get("id"), int):
        raise InitDataError("bad user")

    return {
        "telegram_id": user["id"],
        "username": user.get("username"),
        "auth_date": auth_date,
    }
