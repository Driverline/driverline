"""Tests for the Telegram Mini App initData check (app/telegram_initdata.py). No network, no real bot.
Run: python3 tests/test_telegram_initdata.py"""
import hashlib
import hmac
import json
import sys
import unittest
from pathlib import Path
from urllib.parse import urlencode

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.telegram_initdata import InitDataError, verify_init_data   # noqa: E402

TOKEN = "123456:FAKE-TEST-TOKEN"
NOW = 1_800_000_000


def sign(fields: dict, token: str = TOKEN) -> str:
    """Build a correctly signed initData string, the way Telegram does."""
    check = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    digest = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urlencode({**fields, "hash": digest})


def fields(user=None, auth_date=NOW - 10) -> dict:
    user = {"id": 42, "username": "trader"} if user is None else user
    return {"auth_date": str(auth_date), "query_id": "AAH", "user": json.dumps(user)}


class VerifyInitDataTests(unittest.TestCase):
    def test_valid_payload_passes(self):
        out = verify_init_data(sign(fields()), TOKEN, now=NOW)
        self.assertEqual(out["telegram_id"], 42)
        self.assertEqual(out["username"], "trader")

    def test_username_is_optional(self):
        data = sign(fields(user={"id": 7}))
        self.assertIsNone(verify_init_data(data, TOKEN, now=NOW)["username"])

    def test_tampered_user_fails(self):
        good = fields()
        data = sign(good)
        forged = data.replace("%22id%22%3A+42", "%22id%22%3A+99")
        self.assertNotEqual(data, forged)
        with self.assertRaises(InitDataError):
            verify_init_data(forged, TOKEN, now=NOW)

    def test_wrong_token_fails(self):
        with self.assertRaises(InitDataError):
            verify_init_data(sign(fields()), "999:OTHER-TOKEN", now=NOW)

    def test_stale_auth_date_fails(self):
        data = sign(fields(auth_date=NOW - 7200))
        with self.assertRaises(InitDataError):
            verify_init_data(data, TOKEN, max_age=3600, now=NOW)

    def test_far_future_auth_date_fails(self):
        data = sign(fields(auth_date=NOW + 3600))
        with self.assertRaises(InitDataError):
            verify_init_data(data, TOKEN, now=NOW)

    def test_missing_hash_fails(self):
        data = urlencode(fields())
        with self.assertRaises(InitDataError):
            verify_init_data(data, TOKEN, now=NOW)

    def test_missing_user_fails(self):
        f = fields()
        del f["user"]
        with self.assertRaises(InitDataError):
            verify_init_data(sign(f), TOKEN, now=NOW)

    def test_non_integer_user_id_fails(self):
        data = sign(fields(user={"id": "42"}))
        with self.assertRaises(InitDataError):
            verify_init_data(data, TOKEN, now=NOW)

    def test_bad_auth_date_fails(self):
        f = fields()
        f["auth_date"] = "not-a-number"
        with self.assertRaises(InitDataError):
            verify_init_data(sign(f), TOKEN, now=NOW)

    def test_empty_input_fails(self):
        with self.assertRaises(InitDataError):
            verify_init_data("", TOKEN, now=NOW)
        with self.assertRaises(InitDataError):
            verify_init_data(sign(fields()), "", now=NOW)


if __name__ == "__main__":
    unittest.main()
