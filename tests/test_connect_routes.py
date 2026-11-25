"""Tests for the /api/connect/v1 handlers (app/connect.py), called directly. Run: python3 tests/test_connect_routes.py"""
import asyncio
import hashlib
import hmac
import json
import os
import sys
import tempfile
import time
import types
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlencode

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.update(DRIVERLINE_SECRET="s" * 40)
os.environ.pop("DATABASE_URL", None)

from starlette.exceptions import HTTPException                    # noqa: E402
from app import auth, dbx                                          # noqa: E402

dbx.SQLITE_PATH = Path(tempfile.mkdtemp()) / "t.db"
from app import connect                                            # noqa: E402

BOT = "123456:TEST_bot_token_for_tests_only"
SECRET = "c" * 40
PW = "correct horse battery"
_n = [0]
_tg = [500_000]


def sign(tg, token=BOT, age=10):
    f = {"auth_date": str(int(time.time()) - age), "query_id": "q", "user": json.dumps({"id": tg, "first_name": "A", "username": "al"})}
    chk = "\n".join(f"{k}={v}" for k, v in sorted(f.items()))
    sec = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    f["hash"] = hmac.new(sec, chk.encode(), hashlib.sha256).hexdigest()
    return urlencode(f)


def mk(tier="free", pw=PW):
    _n[0] += 1
    email = f"r{_n[0]}@t.example"
    with closing(auth.db()) as con:
        uid = con.insert("INSERT INTO users(email,pw_hash,tier,created_at) VALUES(?,?,?,?)", (email, auth.hash_pw(pw), tier, "2026-01-01T00:00:00"))
        con.commit()
    return uid, email


def newtg():
    _tg[0] += 1
    return _tg[0]


class Req:
    def __init__(self, body=None, token=None):
        self._b = body
        self.headers = {"authorization": f"Bearer {token}"} if token else {}

    async def json(self):
        if self._b is None:
            raise ValueError("no body")
        return self._b


def call(fn, body=None, token=None):
    """(status, json) of a handler, including the HTTPException a real server would turn into an answer."""
    try:
        r = asyncio.run(fn(Req(body, token)))
        return r.status_code, json.loads(r.body)
    except HTTPException as e:
        return e.status_code, {"detail": e.detail}


def events(name=None):
    with closing(connect.db()) as con:
        q = "SELECT * FROM connect_events" + (" WHERE event=?" if name else "")
        return [dict(r) for r in con.execute(q, (name,) if name else ()).fetchall()]


class RouteTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {"CONNECT_SECRET": SECRET, "CONNECT_BOT_TOKEN": BOT})
        self.env.start()
        self.addCleanup(self.env.stop)
        auth._fails.clear()
        connect._link_tries.clear()

    def do_link(self, tg=None, email=None, pw=PW, uid_email=None):
        tg = tg or newtg()
        if not email:
            _, email = mk()
        return tg, email, call(connect.link, {"init_data": sign(tg), "email": email, "password": pw})

    # ---- off by default
    def test_every_route_is_404_without_connect_secret(self):
        del os.environ["CONNECT_SECRET"]
        for fn in (connect.session, connect.link, connect.unlink, connect.me, connect.entitlements, connect.events):
            self.assertEqual(call(fn, {"init_data": sign(1)})[0], 404, fn.__name__)

    def test_routes_list_has_the_seven_paths(self):
        self.assertEqual({r.path for r in connect.routes}, {f"/api/connect/v1/{x}" for x in ("session", "link", "unlink", "me", "entitlements", "signals", "events")})

    # ---- session
    def test_unlinked_telegram_user_gets_linked_false_and_no_token(self):
        st, b = call(connect.session, {"init_data": sign(newtg())})
        self.assertEqual((st, b), (200, {"linked": False}))

    def test_forged_old_or_missing_init_data_is_401(self):
        for bad in (sign(newtg(), token="1:other"), sign(newtg(), age=7200), "", "x=y"):
            self.assertEqual(call(connect.session, {"init_data": bad})[0], 401)
        self.assertEqual(call(connect.session, {})[0], 401)
        self.assertEqual(call(connect.session, None)[0], 400)

    def test_without_a_bot_token_nothing_verifies(self):
        del os.environ["CONNECT_BOT_TOKEN"]
        self.assertEqual(call(connect.session, {"init_data": sign(newtg())})[0], 401)

    # ---- link
    def test_link_then_session_returns_a_working_token(self):
        tg, email, (st, b) = self.do_link()
        self.assertEqual(st, 200)
        self.assertTrue(b["linked"] and b["token"].startswith("tlc1."))
        st2, b2 = call(connect.session, {"init_data": sign(tg)})
        self.assertTrue(st2 == 200 and b2["linked"])
        self.assertEqual(call(connect.me, token=b2["token"])[0], 200)
        self.assertEqual(len(events("link_completed")) >= 1, True)

    def test_me_masks_the_email_and_has_no_secrets(self):
        tg, email, (_, b) = self.do_link()
        st, me = call(connect.me, token=b["token"])
        self.assertNotIn(email, json.dumps(me))
        self.assertIn("***@", me["email"])
        for bad in ("pw_hash", "password", "scrypt"):
            self.assertNotIn(bad, json.dumps(me))
        self.assertIn("flags", me["entitlements"])

    def test_wrong_password_and_unknown_email_look_the_same(self):
        _, e = mk()
        a = call(connect.link, {"init_data": sign(newtg()), "email": e, "password": "nope"})
        b = call(connect.link, {"init_data": sign(newtg()), "email": "ghost@t.example", "password": "nope"})
        self.assertEqual(a, b)
        self.assertEqual(a[0], 401)

    def test_blocked_account_cannot_link(self):
        uid, e = mk()
        with closing(auth.db()) as con:
            con.execute("UPDATE users SET blocked=1 WHERE id=?", (uid,))
            con.commit()
        self.assertEqual(self.do_link(email=e)[2][0], 403)

    def test_link_shares_the_website_lockout_counter(self):
        _, e = mk()
        for _ in range(5):
            call(connect.link, {"init_data": sign(newtg()), "email": e, "password": "bad"})
        self.assertEqual(call(connect.link, {"init_data": sign(newtg()), "email": e, "password": PW})[0], 429)

    def test_one_telegram_account_is_limited_to_five_tries(self):
        tg = newtg()
        for _ in range(5):
            call(connect.link, {"init_data": sign(tg), "email": f"x{_}@t.example", "password": "bad"})
        self.assertEqual(call(connect.link, {"init_data": sign(tg), "email": "y@t.example", "password": "bad"})[0], 429)

    def test_a_telegram_account_cannot_be_linked_to_two_members(self):
        tg, _, (st, _b) = self.do_link()
        self.assertEqual(st, 200)
        self.assertEqual(self.do_link(tg=tg)[2][0], 409)

    def test_a_member_cannot_be_linked_to_two_telegram_accounts(self):
        _, e = mk()
        self.assertEqual(self.do_link(email=e)[2][0], 200)
        self.assertEqual(self.do_link(email=e)[2][0], 409)
        self.assertGreaterEqual(len(events("link_conflict")), 1)

    def test_link_ignores_a_telegram_id_sent_in_the_body(self):
        tg, e = newtg(), mk()[1]
        st, b = call(connect.link, {"init_data": sign(tg), "email": e, "password": PW, "telegram_id": 1})
        self.assertEqual(st, 200)
        with closing(connect.db()) as con:
            row = con.execute("SELECT telegram_id FROM telegram_links WHERE telegram_id=?", (tg,)).fetchone()
        self.assertIsNotNone(row)

    # ---- unlink / token death
    def test_unlink_ends_the_token_and_allows_relinking(self):
        tg, e, (_, b) = self.do_link()
        self.assertEqual(call(connect.unlink, token=b["token"])[0], 200)
        self.assertEqual(call(connect.me, token=b["token"])[0], 401)
        self.assertEqual(call(connect.session, {"init_data": sign(tg)})[1], {"linked": False})
        self.assertEqual(call(connect.link, {"init_data": sign(tg), "email": e, "password": PW})[0], 200)
        self.assertGreaterEqual(len(events("unlink")), 1)

    def test_protected_routes_need_a_token(self):
        for fn in (connect.me, connect.entitlements, connect.unlink):
            self.assertEqual(call(fn)[0], 401)
            self.assertEqual(call(fn, token="garbage")[0], 401)
        self.assertEqual(call(connect.events, {"event": "unlink"})[0], 401)

    def test_a_website_session_token_does_not_open_connect_routes(self):
        uid, _ = mk()
        with closing(auth.db()) as con:
            u = dict(con.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone())
        self.assertEqual(call(connect.me, token=auth.make_token(u["id"], u["pw_hash"]))[0], 401)

    # ---- events
    def test_events_accept_only_known_names_and_small_props(self):
        _, _, (_, b) = self.do_link()
        t = b["token"]
        self.assertEqual(call(connect.events, {"event": "upgrade_tapped", "props": {"plan": "pro"}}, t)[0], 200)
        self.assertEqual(call(connect.events, {"event": "made_up"}, t)[0], 400)
        self.assertEqual(call(connect.events, {"event": "signal_viewed", "props": {"x": "y" * 400}}, t)[0], 200)
        rows = events("signal_viewed")
        self.assertIsNone(rows[-1]["props"])
        self.assertEqual(json.loads(events("upgrade_tapped")[-1]["props"]), {"plan": "pro"})

    def test_entitlements_route_matches_me(self):
        _, _, (_, b) = self.do_link()
        self.assertEqual(call(connect.entitlements, token=b["token"])[1], call(connect.me, token=b["token"])[1]["entitlements"])

    def test_signals_route_is_read_only_and_needs_the_token(self):
        self.assertEqual(call(connect.signals)[0], 401)
        _, _, (_, b) = self.do_link()
        st, body = call(connect.signals, token=b["token"])
        self.assertEqual(st, 200)
        self.assertIn("signals", body)
        self.assertIn("budget", body)
        self.assertNotIn("pw_hash", json.dumps(body))
        from app import signal_feed
        with closing(signal_feed.db()) as con:
            self.assertEqual(con.execute("SELECT COUNT(*) AS c FROM signal_opens").fetchone()["c"], 0)

    def test_nothing_sensitive_is_logged_or_stored_in_events(self):
        _, _, (_, b) = self.do_link()
        blob = json.dumps(events())
        for bad in (PW, BOT, b["token"]):
            self.assertNotIn(bad, blob)


if __name__ == "__main__":
    unittest.main()
