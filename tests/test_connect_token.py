"""Tests for the Connect token (app/connect.py): signing, expiry, tampering, separation from the website session, and that blocking,
unlinking, relinking and a password reset end a token at once. Run: python3 tests/test_connect_token.py   (temp SQLite database, no network)."""
import os
import sys
import tempfile
import time
import types
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.update(DRIVERLINE_SECRET="s" * 40)
os.environ.pop("DATABASE_URL", None)

from starlette.exceptions import HTTPException                    # noqa: E402
from app import auth, dbx                                          # noqa: E402

dbx.SQLITE_PATH = Path(tempfile.mkdtemp()) / "t.db"
from app import connect                                            # noqa: E402

SECRET = "c" * 40
_seq = [0]
_tg = [10_000]


def mk(tier="free"):
    _seq[0] += 1
    with closing(auth.db()) as con:
        uid = con.insert("INSERT INTO users(email,pw_hash,tier,created_at) VALUES(?,?,?,?)",
                         (f"t{_seq[0]}@t.example", "h", tier, "2026-01-01T00:00:00"))
        con.commit()
    return uid


def user(uid):
    with closing(auth.db()) as con:
        return dict(con.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone())


def link(uid, tg=None):
    _tg[0] += 1
    tg = tg or _tg[0]
    now = int(time.time())
    with closing(connect.db()) as con:
        con.insert("INSERT INTO telegram_links(user_id,telegram_id,telegram_username,source,linked_at,last_seen_at) VALUES(?,?,?,?,?,?)",
                   (uid, tg, "x", "mini_app", now, now))
        con.commit()
    return tg


def req(token=None, scheme="Bearer"):
    return types.SimpleNamespace(headers={"authorization": f"{scheme} {token}"} if token is not None else {})


def status_of(fn, *a):
    try:
        fn(*a)
    except HTTPException as e:
        return e.status_code
    return None


class ConnectTokenTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {"CONNECT_SECRET": SECRET})
        self.env.start()
        self.addCleanup(self.env.stop)

    def member(self):
        uid = mk()
        tg = link(uid)
        return uid, tg, connect.issue_token(user(uid), tg)

    # ---- the token itself
    def test_round_trip(self):
        u = user(mk())
        c = connect.read_token(connect.issue_token(u, 777))
        self.assertEqual((c["uid"], c["tg"], c["aud"]), (u["id"], 777, "connect"))

    def test_lifetime_is_fifteen_minutes(self):
        u = user(mk())
        tok = connect.issue_token(u, 5, now=1000)
        self.assertIsNotNone(connect.read_token(tok, now=1000 + connect.TOKEN_TTL - 1))
        self.assertIsNone(connect.read_token(tok, now=1000 + connect.TOKEN_TTL))
        self.assertEqual(connect.TOKEN_TTL, 900)

    def test_token_from_the_future_is_refused(self):
        tok = connect.issue_token(user(mk()), 5, now=10_000)
        self.assertIsNone(connect.read_token(tok, now=1000))

    def test_tampered_payload_is_refused(self):
        a, b = user(mk()), user(mk())
        t1, t2 = connect.issue_token(a, 5, now=1000), connect.issue_token(b, 5, now=1000)
        pre, pay1, sig1 = t1.split(".")
        forged = ".".join([pre, t2.split(".")[1], sig1])      # someone else's payload under my signature
        self.assertIsNone(connect.read_token(forged, now=1000))

    def test_garbage_is_refused(self):
        for bad in ("", "x", "a.b", "tlc1.x.y", "tlc1..", None, 5, "tlc1." + "A" * 50 + "." + "0" * 64):
            self.assertIsNone(connect.read_token(bad), bad)

    def test_wrong_audience_is_refused_even_when_correctly_signed(self):
        import json
        claims = {"aud": "other", "uid": 1, "tg": 2, "pt": "x", "iat": int(time.time()), "exp": int(time.time()) + 60}
        body = "tlc1." + connect._b64(json.dumps(claims).encode())
        self.assertIsNone(connect.read_token(body + "." + connect._sig(body)))

    def test_another_secret_is_refused(self):
        tok = connect.issue_token(user(mk()), 5)
        with patch.dict(os.environ, {"CONNECT_SECRET": "d" * 40}):
            self.assertIsNone(connect.read_token(tok))

    def test_without_a_secret_connect_is_off_and_nothing_is_accepted(self):
        tok = connect.issue_token(user(mk()), 5)
        del os.environ["CONNECT_SECRET"]
        self.assertFalse(connect.enabled())
        self.assertIsNone(connect.read_token(tok))
        with self.assertRaises(RuntimeError):
            connect.issue_token(user(mk()), 5)

    def test_a_short_secret_does_not_count(self):
        os.environ["CONNECT_SECRET"] = "short"
        self.assertFalse(connect.enabled())

    # ---- separation from the website session
    def test_website_session_cannot_be_used_as_a_connect_token(self):
        u = user(mk())
        self.assertIsNone(connect.read_token(auth.make_token(u["id"], u["pw_hash"])))

    def test_connect_token_cannot_be_used_as_a_website_session(self):
        u = user(mk())
        self.assertIsNone(auth.read_token(connect.issue_token(u, 5)))

    def test_separation_holds_even_if_both_secrets_were_the_same(self):
        u = user(mk())
        with patch.dict(os.environ, {"CONNECT_SECRET": "s" * 40}):
            self.assertIsNone(connect.read_token(auth.make_token(u["id"], u["pw_hash"])))
            self.assertIsNone(auth.read_token(connect.issue_token(u, 5)))

    # ---- the per-request check
    def test_a_good_token_returns_the_member_and_the_link(self):
        uid, tg, tok = self.member()
        u, lk = connect.require_connect_user(req(tok))
        self.assertEqual((u["id"], lk["telegram_id"]), (uid, tg))

    def test_missing_or_wrong_scheme_is_401(self):
        _, _, tok = self.member()
        self.assertEqual(status_of(connect.require_connect_user, req()), 401)
        self.assertEqual(status_of(connect.require_connect_user, req(tok, scheme="Basic")), 401)
        self.assertEqual(status_of(connect.require_connect_user, req("nonsense")), 401)

    def test_scheme_is_case_insensitive(self):
        _, _, tok = self.member()
        self.assertIsNone(status_of(connect.require_connect_user, req(tok, scheme="bearer")))

    def test_blocking_the_account_ends_the_token_at_once(self):
        uid, _, tok = self.member()
        with closing(auth.db()) as con:
            con.execute("UPDATE users SET blocked=1 WHERE id=?", (uid,))
            con.commit()
        self.assertEqual(status_of(connect.require_connect_user, req(tok)), 401)

    def test_unlinking_ends_the_token_at_once(self):
        uid, _, tok = self.member()
        with closing(connect.db()) as con:
            con.execute("UPDATE telegram_links SET revoked_at=? WHERE user_id=?", (int(time.time()), uid))
            con.commit()
        self.assertEqual(status_of(connect.require_connect_user, req(tok)), 401)

    def test_linking_another_telegram_account_ends_the_old_token(self):
        uid, tg, old = self.member()
        with closing(connect.db()) as con:
            con.execute("UPDATE telegram_links SET revoked_at=? WHERE user_id=?", (int(time.time()), uid))
            con.commit()
        new_tg = link(uid)
        self.assertEqual(status_of(connect.require_connect_user, req(old)), 401)
        self.assertIsNone(status_of(connect.require_connect_user, req(connect.issue_token(user(uid), new_tg))))

    def test_a_password_reset_ends_the_token_at_once(self):
        uid, _, tok = self.member()
        with closing(auth.db()) as con:
            con.execute("UPDATE users SET pw_hash=? WHERE id=?", (auth.hash_pw("a brand new password"), uid))
            con.commit()
        self.assertEqual(status_of(connect.require_connect_user, req(tok)), 401)

    def test_a_token_for_a_telegram_account_that_was_never_linked_is_refused(self):
        uid = mk()
        link(uid, tg=424242)
        self.assertEqual(status_of(connect.require_connect_user, req(connect.issue_token(user(uid), 999999))), 401)

    def test_every_failure_gives_the_same_message(self):
        uid, _, tok = self.member()
        msgs = set()
        for r in (req(), req("x"), req(connect.issue_token(user(mk()), 1))):
            try:
                connect.require_connect_user(r)
            except HTTPException as e:
                msgs.add(e.detail)
        self.assertEqual(len(msgs), 1)


if __name__ == "__main__":
    unittest.main()
