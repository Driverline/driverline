"""Tests for the safe-direction controls (app/connect_controls.py). Run: python3 tests/test_connect_controls.py"""
import asyncio
import json
import os
import sys
import tempfile
import time
import unittest
import uuid
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
from app import connect, connect_controls as C, ea                 # noqa: E402

SECRET = "c" * 40
_n = [0]


def member(blocked=0):
    _n[0] += 1
    with closing(auth.db()) as con:
        uid = con.insert("INSERT INTO users(email,pw_hash,tier,created_at,blocked) VALUES(?,?,?,?,?)",
                         (f"c{_n[0]}@t.example", auth.hash_pw("x" * 12), "member", "2026-01-01T00:00:00", blocked))
        con.commit()
    tg = 700000 + _n[0]
    with closing(connect.db()) as con:
        con.execute("INSERT INTO telegram_links(user_id,telegram_id,source,linked_at,last_seen_at) VALUES(?,?,?,?,?)", (uid, tg, "test", int(time.time()), int(time.time())))
        con.commit()
    with closing(auth.db()) as con:
        u = dict(con.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone())
    return uid, tg, connect.issue_token(u, tg)


def account(uid, label="Main"):
    with closing(ea.db()) as con:
        aid = con.insert("INSERT INTO ea_accounts(user_id,label,key_hash,created_at,last_seen,login) VALUES(?,?,?,?,?,?)",
                         (uid, label, uuid.uuid4().hex, int(time.time()), int(time.time()), "1"))
        con.commit()
    return aid


def auto_on(uid, aid, waiting=True):
    with closing(ea.db()) as con:
        con.execute("INSERT INTO auto_settings(account_id,user_id,enabled,styles,instruments,mode,risk_pct,max_risk_pct,max_open,max_per_day,max_rating,loss_limit,pending,ack_at,updated_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (aid, uid, 1, "[]", "[]", "risk", 1.0, 2.0, 3, 5, "Medium", 3, "{}", 1, 1))
        if waiting:
            con.execute("INSERT INTO auto_jobs(account_id,user_id,signal_id,status,created_at,updated_at) VALUES(?,?,?,?,?,?)", (aid, uid, 1, "waiting", 1, 1))
        con.commit()


def set_manage(aid, **kw):
    with closing(ea.db()) as con:
        s = ea.get_manage(con, aid)
        s.update(kw)
        ea._save_manage(con, aid, s)


def get_manage(aid):
    with closing(ea.db()) as con:
        return ea.get_manage(con, aid)


class Req:
    def __init__(self, body, token):
        self._b, self.query_params = body, {}
        self.headers = {"authorization": f"Bearer {token}"} if token else {}

    async def json(self):
        return self._b


def call(fn, body, token):
    try:
        r = asyncio.run(fn(Req(body, token)))
        return r.status_code, json.loads(r.body)
    except HTTPException as e:
        return e.status_code, {"detail": e.detail}


def events(kind, aid):
    with closing(ea.db()) as con:
        return [dict(r) for r in con.execute("SELECT * FROM auto_events WHERE kind=? AND account_id=?", (kind, aid)).fetchall()]


class Controls(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {"CONNECT_SECRET": SECRET, "CONNECT_BOT_TOKEN": "1:t", "CONNECT_CONTROLS": "1"})
        self.env.start()
        self.addCleanup(self.env.stop)

    def run_it(self, token, action, aid, params=None):
        st, p = call(C.prepare, {"action": action, "account": str(aid), "params": params or {}}, token)
        if st != 200:
            return st, p, None
        return (*call(C.confirm, {"code": p["code"]}, token), p)

    def test_off_unless_controls_switched_on(self):
        _, _, tok = member()
        del os.environ["CONNECT_CONTROLS"]
        self.assertEqual(call(C.prepare, {"action": "auto_off"}, tok)[0], 404)
        self.assertEqual(call(C.confirm, {"code": "x"}, tok)[0], 404)
        self.assertFalse(connect.controls_on())

    def test_needs_a_valid_fresh_token(self):
        self.assertEqual(call(C.prepare, {"action": "auto_off"}, None)[0], 401)
        uid, tg, _ = member()
        with closing(auth.db()) as con:
            u = dict(con.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone())
        old = connect.issue_token(u, tg, now=time.time() - 600)
        self.assertEqual(call(C.prepare, {"action": "auto_off", "account": "1"}, old)[0], 401)

    def test_auto_off_two_steps_audit_and_cancels_waiting_trades(self):
        uid, tg, tok = member()
        aid = account(uid)
        auto_on(uid, aid)
        st, p = call(C.prepare, {"action": "auto_off", "account": str(aid)}, tok)
        self.assertEqual(st, 200)
        self.assertIn("OFF", p["summary"])
        with closing(ea.db()) as con:       # preparing changes nothing
            self.assertEqual(con.execute("SELECT enabled FROM auto_settings WHERE account_id=?", (aid,)).fetchone()["enabled"], 1)
        st, r = call(C.confirm, {"code": p["code"]}, tok)
        self.assertEqual((st, r["ok"], r["state"]), (200, True, {"enabled": False}))
        with closing(ea.db()) as con:
            self.assertEqual(con.execute("SELECT status FROM auto_jobs WHERE account_id=?", (aid,)).fetchone()["status"], "skipped")
        ev = events("connect_auto_off", aid)
        self.assertEqual(len(ev), 1)
        self.assertIn(str(tg), ev[0]["detail"])
        self.assertEqual(r["audit_id"], ev[0]["id"])

    def test_a_confirmation_cannot_be_used_twice_or_by_someone_else(self):
        uid, tg, tok = member()
        aid = account(uid)
        auto_on(uid, aid)
        _, p = call(C.prepare, {"action": "auto_off", "account": str(aid)}, tok)
        _, _, tok2 = member()
        self.assertEqual(call(C.confirm, {"code": p["code"]}, tok2)[0], 400)
        self.assertEqual(call(C.confirm, {"code": p["code"]}, tok)[0], 200)
        self.assertEqual(call(C.confirm, {"code": p["code"]}, tok)[0], 409)
        self.assertEqual(call(C.confirm, {"code": p["code"][:-3] + "abc"}, tok)[0], 400)

    def test_an_expired_confirmation_is_refused(self):
        uid, tg, tok = member()
        aid = account(uid)
        auto_on(uid, aid)
        old = C._make_code(uid, tg, "auto_off", aid, {}, int(time.time()) - 1000)
        self.assertEqual(call(C.confirm, {"code": old}, tok)[0], 400)

    def test_a_foreign_or_unknown_account_is_404_and_already_off_is_409(self):
        uid, tg, tok = member()
        other_uid, _, _ = member()
        theirs = account(other_uid)
        auto_on(other_uid, theirs)
        self.assertEqual(call(C.prepare, {"action": "auto_off", "account": str(theirs)}, tok)[0], 404)
        mine = account(uid)
        self.assertEqual(call(C.prepare, {"action": "auto_off", "account": str(mine)}, tok)[0], 409)
        with closing(ea.db()) as con:
            self.assertEqual(con.execute("SELECT enabled FROM auto_settings WHERE account_id=?", (theirs,)).fetchone()["enabled"], 1)

    def test_blocked_member_and_unknown_action(self):
        uid, tg, tok = member()
        self.assertEqual(call(C.prepare, {"action": "auto_on", "account": "1"}, tok)[0], 400)
        self.assertEqual(call(C.prepare, {"action": "close_trade", "account": "1"}, tok)[0], 400)
        with closing(auth.db()) as con:
            con.execute("UPDATE users SET blocked=1 WHERE id=?", (uid,))
            con.commit()
        self.assertEqual(call(C.prepare, {"action": "auto_off", "account": "1"}, tok)[0], 401)

    def test_stricter_protection_applies_at_once_with_audit(self):
        uid, tg, tok = member()
        aid = account(uid)
        set_manage(aid, prot_mode="breakeven", prot_risk_pct=4.5, prot_cap_pct=4.5)
        st, r, _ = self.run_it(tok, "protection", aid, {"prot_mode": "close", "prot_risk_pct": 3.0})
        self.assertEqual(st, 200, r)
        m = get_manage(aid)
        self.assertEqual((m["prot_mode"], m["prot_risk_pct"]), ("close", 3.0))
        self.assertEqual(r["state"]["prot_mode"], "close")
        self.assertEqual(len(events("connect_prot_change", aid)), 1)

    def test_weaker_protection_is_refused_and_nothing_is_queued(self):
        uid, tg, tok = member()
        aid = account(uid)
        set_manage(aid, prot_mode="stop", prot_risk_pct=4.5, prot_cap_pct=6.0)
        for params in ({"prot_mode": "off"}, {"prot_mode": "breakeven"}, {"prot_risk_pct": 5.0}, {"prot_cap_pct": 8.0}):
            st, r, _ = self.run_it(tok, "protection", aid, params)
            self.assertEqual(st, 422, params)
        m = get_manage(aid)
        self.assertEqual((m["prot_mode"], m["prot_risk_pct"], m["prot_cap_pct"], m["prot_pending"]), ("stop", 4.5, 6.0, {}))

    def test_protection_rejects_other_fields_no_change_and_bad_numbers(self):
        uid, tg, tok = member()
        aid = account(uid)
        set_manage(aid, prot_mode="stop", prot_risk_pct=4.5, prot_cap_pct=4.5)
        for params in ({}, {"be_on": False}, {"prot_grace": 10}, {"prot_mode": "stop"}, {"prot_risk_pct": "x"}, {"prot_risk_pct": True}, {"prot_risk_pct": 0.01},
                       {"prot_mode": "nonsense"}):
            self.assertEqual(self.run_it(tok, "protection", aid, params)[0], 422, params)

    def test_state_changing_between_prepare_and_confirm_is_rejected_and_audited(self):
        uid, tg, tok = member()
        aid = account(uid)
        set_manage(aid, prot_mode="breakeven", prot_risk_pct=4.5, prot_cap_pct=4.5)
        _, p = call(C.prepare, {"action": "protection", "account": str(aid), "params": {"prot_mode": "close"}}, tok)
        set_manage(aid, prot_mode="close")
        self.assertEqual(call(C.confirm, {"code": p["code"]}, tok)[0], 422)
        self.assertEqual(len(events("connect_rejected", aid)), 1)

    def test_rate_limit(self):
        uid, tg, tok = member()
        aid = account(uid)
        with closing(ea.db()) as con:
            for _ in range(C.MAX_PER_HOUR):
                con.execute("INSERT INTO auto_events(account_id,user_id,kind,detail,created_at) VALUES(?,?,?,?,?)", (aid, uid, "connect_auto_off", "x", int(time.time())))
            con.commit()
        self.assertEqual(call(C.prepare, {"action": "auto_off", "account": str(aid)}, tok)[0], 429)

    def test_read_routes_say_whether_controls_are_on(self):
        uid, tg, tok = member()
        st, body = asyncio.run(connect.mt5(Req(None, tok))).status_code, None
        r = asyncio.run(connect.mt5(Req(None, tok)))
        self.assertTrue(json.loads(r.body)["controls"])
        del os.environ["CONNECT_CONTROLS"]
        r = asyncio.run(connect.mt5(Req(None, tok)))
        self.assertFalse(json.loads(r.body)["controls"])

    def test_it_never_touches_the_website_settings_table_for_protection_or_other_accounts(self):
        uid, tg, tok = member()
        aid, other = account(uid), account(uid, "Second")
        auto_on(uid, other, waiting=False)
        set_manage(aid, prot_mode="stop", prot_risk_pct=4.5, prot_cap_pct=4.5)
        self.assertEqual(self.run_it(tok, "protection", aid, {"prot_mode": "close"})[0], 200)
        with closing(ea.db()) as con:
            self.assertEqual(con.execute("SELECT enabled FROM auto_settings WHERE account_id=?", (other,)).fetchone()["enabled"], 1)


if __name__ == "__main__":
    unittest.main()
