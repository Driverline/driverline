"""Tests for copy trading (app/copy_trading.py). Run: python3 tests/test_copy_trading.py   (own process, temp SQLite database, no network, no broker)."""
import json
import os
import sys
import tempfile
import time
import unittest
from contextlib import closing
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
try:
    import httpx  # noqa: F401
except ImportError:
    import httpx2 as httpx
    sys.modules["httpx"] = httpx
import types
for _n in ("websockets", "pywebpush", "psycopg", "psycopg.rows"):
    try:
        __import__(_n)
    except ImportError:
        sys.modules[_n] = types.ModuleType(_n)
os.environ.update(DRIVERLINE_SECRET="s" * 40, DRIVERLINE_MEMBER_KEYS="1")
os.environ.pop("DATABASE_URL", None)

from app import auth, credits, dbx, ea, copy_trading as CT, pending_orders, signal_feed     # noqa: E402
from starlette.applications import Starlette                           # noqa: E402
from starlette.exceptions import HTTPException                         # noqa: E402
from starlette.responses import JSONResponse                           # noqa: E402
from starlette.testclient import TestClient                            # noqa: E402

TMP = Path(tempfile.mkdtemp())
dbx.SQLITE_PATH = TMP / "t.db"


async def _err(request, exc):
    return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)


APP = Starlette(routes=[*ea.routes, *CT.routes], exception_handlers={HTTPException: _err})
_seq, _clients = [0], {}
ANON = TestClient(APP)
ANON.__enter__()
SPECS = [{"symbol": "EURUSDm", "tick_size": 0.00001, "tick_value": 1.0, "vol_min": 0.01, "vol_step": 0.01, "vol_max": 100, "contract_size": 100000}]


def mk_user(tier="member"):
    _seq[0] += 1
    with closing(auth.db()) as con:
        uid = con.insert("INSERT INTO users(email,pw_hash,tier,created_at) VALUES(?,?,?,?)", (f"a{_seq[0]}@t.example", "h", tier, "2026-01-01T00:00:00"))
        con.commit()
    return uid


def cl(uid):
    if uid not in _clients:
        c = TestClient(APP)
        c.__enter__()
        c.cookies.set(auth.COOKIE, auth.make_token(uid, "h"))
        _clients[uid] = c
    return _clients[uid]


def q(sqltext, *a):
    with closing(ea.db()) as con:
        r = con.execute(sqltext, a).fetchall()
        con.commit()
    return [dict(x) for x in r]


def add(uid, label):
    r = cl(uid).post("/api/ea/accounts", json={"label": label})
    assert r.status_code == 200, r.text
    return r.json()["id"], r.json()["key"]


def eareport(key, login="1", positions=(), balance=1000.0, demo=True, **flags):
    ea._last_report.clear()
    acc = {"login": login, "server": "Deriv-Demo", "currency": "USD", "balance": balance, "equity": balance, "demo": demo,
           "remote_close": True, "manage_allowed": True, "execute_allowed": True, "pending_allowed": True, **flags}
    body = {"account": acc, "today": {"closed_pnl": 0}, "positions": list(positions), "orders": [], "specs": SPECS}
    return ANON.post("/api/ea/report", json=body, headers={"x-driverline-key": key})


def pos(ticket, typ="BUY", symbol="EURUSDm", volume=0.1, op=1.1000, sl=1.0990, tp=1.1030):
    return {"ticket": ticket, "symbol": symbol, "type": typ, "volume": volume, "open": op, "sl": sl, "tp": tp, "profit": 0, "risk": 1}


def eapoll(key):
    ea._last_poll.clear()
    return ANON.post("/api/ea/poll", json={}, headers={"x-driverline-key": key})



SUBS = lambda uid, days=30: q("INSERT INTO copy_subs(user_id,ends_at,note,updated_at) VALUES(?,?,?,?)", uid, int(time.time()) + days * 86400, "t", int(time.time()))
SPEC = SPECS[0]


class SizeTests(unittest.TestCase):
    def test_fixed_default_is_the_minimum_lot_and_respects_the_limit(self):
        lots, pct, why = CT.size(SPEC, 1000, 0.0010, "fixed", None, None, 1.0)
        self.assertEqual((lots, pct, why), (0.01, 0.1, ""))
        lots, pct, why = CT.size(SPEC, 50, 0.0010, "fixed", None, None, 1.0)       # minimum lot already risks 2%
        self.assertIsNone(lots)
        self.assertEqual(pct, 2.0)
        self.assertIn("above your 1% limit", why)

    def test_fixed_custom_lot_is_rounded_down_to_the_step_and_checked(self):
        self.assertEqual(CT.size(SPEC, 10000, 0.0010, "fixed", 0.057, None, 1.0)[0], 0.05)
        self.assertEqual(CT.size(SPEC, 10000, 0.0010, "fixed", 0.001, None, 1.0)[0], 0.01)     # never below the minimum lot
        self.assertIsNone(CT.size(SPEC, 1000, 0.0010, "fixed", 0.5, None, 1.0)[0])               # 0.5 lot = 5%: over the limit

    def test_percentage_risk_sizes_from_the_stop_distance(self):
        lots, pct, _ = CT.size(SPEC, 1000, 0.0010, "risk", None, 0.5, 1.0)
        self.assertEqual((lots, pct), (0.05, 0.5))
        lots, pct, why = CT.size(SPEC, 1000, 0.0100, "risk", None, 0.5, 1.0)                    # 10x wider stop: even 0.01 lot risks 1% > 0.5%
        self.assertIsNone(lots)
        self.assertIn("minimum lot", why)
        self.assertLessEqual(CT.size(SPEC, 1e9, 0.0010, "risk", None, 5, 5)[0], 100)               # never above the symbol's maximum

    def test_missing_information_never_trades(self):
        self.assertIsNone(CT.size({}, 1000, 0.001, "fixed", None, None, 1.0)[0])
        self.assertIsNone(CT.size(SPEC, 0, 0.001, "fixed", None, None, 1.0)[0])
        self.assertIsNone(CT.size(SPEC, 1000, 0, "fixed", None, None, 1.0)[0])


class World:
    """An admin with a master account, plus followers. Every test builds its own."""
    def setUp(self):
        q("DELETE FROM ea_meta WHERE k='copy_master'")
        q("UPDATE copy_flags SET status='closed' WHERE status='open'")
        q("UPDATE copy_members SET enabled=0")
        q("UPDATE copy_jobs SET status='closed' WHERE status IN ('waiting','sent','open')")
        self.admin = mk_user("admin")
        self.m, self.km = add(self.admin, "Master")
        assert cl(self.admin).post("/api/copy/admin/master", json={"account": self.m}).status_code == 200
        eareport(self.km, "9000", [pos(900)])
        self.f1 = self.follower("f1")
        self.f2 = self.follower("f2")

    def follower(self, tag, sub=True, enabled=True, balance=1000.0, mode="fixed", **flags):
        u = mk_user()
        a, k = add(u, tag)
        eareport(k, "L" + tag, [], balance, **flags)
        if sub:
            SUBS(u)
        if enabled:
            r = cl(u).post("/api/copy/me/settings", json={"enabled": True, "mode": mode, "risk_pct": 0.5})
            assert r.status_code == 200, r.text
        return {"u": u, "a": a, "k": k, "login": "L" + tag}

    def flag(self, ticket=900):
        return cl(self.admin).post("/api/copy/admin/flag", json={"ticket": ticket})

    def jobs(self, fid):
        return q("SELECT * FROM copy_jobs WHERE flag_id=? ORDER BY id", fid)

    def job(self, fid, f):
        return next(j for j in self.jobs(fid) if j["account_id"] == f["a"])


class FlagTests(World, unittest.TestCase):
    def test_only_subscribed_enabled_followers_get_a_job_and_the_master_never(self):
        nosub = self.follower("ns", sub=False, enabled=False)
        off = self.follower("off", enabled=False)
        r = self.flag()
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["followers"], 2)
        got = {j["account_id"] for j in self.jobs(r.json()["id"])}
        self.assertEqual(got, {self.f1["a"], self.f2["a"]})
        self.assertNotIn(nosub["a"], got)
        self.assertNotIn(off["a"], got)
        self.assertNotIn(self.m, got)

    def test_a_trade_must_have_stop_and_take_profit_on_the_right_sides(self):
        for p in (pos(901, sl=0), pos(902, tp=0), pos(903, sl=1.2), pos(904, typ="SELL")):
            eareport(self.km, "9000", [p])
            r = self.flag(p["ticket"])
            self.assertEqual(r.status_code, 422, (p, r.text))
        eareport(self.km, "9000", [pos(905, typ="SELL", sl=1.1010, tp=1.0970)])
        self.assertEqual(self.flag(905).status_code, 200)
        self.assertEqual(self.flag(77777).status_code, 404)
        self.assertEqual(q("SELECT COUNT(*) AS c FROM copy_flags WHERE master_ticket IN (901,902,903,904)")[0]["c"], 0)

    def test_flagging_twice_and_permissions(self):
        self.assertEqual(self.flag().status_code, 200)
        self.assertEqual(self.flag().status_code, 409)
        m = mk_user()
        for method, path, body in (("post", "/api/copy/admin/flag", {"ticket": 900}), ("get", "/api/copy/admin", None),
                                   ("post", "/api/copy/admin/master", {"account": 1}), ("post", "/api/copy/admin/subscription", {"email": "x", "days": 1})):
            r = getattr(cl(m), method)(path, **({"json": body} if body is not None else {}))
            self.assertEqual(r.status_code, 403, path)
            self.assertIn(getattr(ANON, method)(path, **({"json": body} if body is not None else {})).status_code, (401, 403))

    def test_the_master_cannot_be_changed_while_a_trade_is_flagged_and_cannot_copy_itself(self):
        self.flag()
        other, _ = add(self.admin, "Other")
        self.assertEqual(cl(self.admin).post("/api/copy/admin/master", json={"account": other}).status_code, 409)
        SUBS(self.admin)
        r = cl(self.admin).post("/api/copy/me/settings", json={"enabled": True, "account": self.m})
        self.assertEqual(r.status_code, 409)


class DispatchTests(World, unittest.TestCase):
    def test_the_followers_ea_gets_a_correctly_sized_open_command_with_its_own_protection(self):
        fid = self.flag().json()["id"]
        p = eapoll(self.f1["k"]).json()
        self.assertEqual((p["cmd"], p["sym"], p["dir"], p["lots"]), ("open", "EURUSDm", "BUY", "0.01"))
        self.assertAlmostEqual(float(p["sl_dist"]), 0.0010, 7)
        self.assertAlmostEqual(float(p["tp_dist"]), 0.0030, 7)
        self.assertAlmostEqual(float(p["ref"]), 1.1, 7)
        self.assertEqual(p["cmt"], f"TLC-{fid}")
        self.assertGreater(float(p["maxdev"]), 0)
        j = self.job(fid, self.f1)
        self.assertEqual((j["status"], j["lots"], j["risk_pct"]), ("sent", 0.01, 0.1))
        self.assertEqual(eapoll(self.m and self.km).json()["cmd"], "")                        # the master is never told to trade
        self.assertEqual(eapoll(self.f1["k"]).json()["cmd"], "")                              # and a job is handed out once

    def test_each_follower_is_sized_from_their_own_balance_and_mode(self):
        small = self.follower("small", balance=50.0)                                          # minimum lot would risk 2% > their 1% limit
        pct = self.follower("pct", balance=2000.0, mode="risk")                              # 0.5% of 2000 = 10 -> 0.10 lot at 100/lot
        fid = self.flag().json()["id"]
        self.assertEqual(eapoll(pct["k"]).json()["lots"], "0.1")
        self.assertEqual(eapoll(small["k"]).json()["cmd"], "")
        j = self.job(fid, small)
        self.assertEqual(j["status"], "skipped")
        self.assertIn("above your 1% limit", j["note"])
        self.assertEqual(q("SELECT COUNT(*) AS c FROM ea_commands WHERE account_id=? AND kind='open'", small["a"])[0]["c"], 0)

    def test_the_members_own_cap_overrides_and_can_be_tighter(self):
        tight = self.follower("tight")
        r = cl(tight["u"]).post("/api/copy/me/settings", json={"enabled": True, "mode": "fixed", "max_risk_pct": 0.05})
        self.assertEqual(r.status_code, 200)
        fid = self.flag().json()["id"]
        self.assertEqual(eapoll(tight["k"]).json()["cmd"], "")                                # 0.1% > 0.05%
        self.assertEqual(self.job(fid, tight)["status"], "skipped")

    def test_guards_skip_with_a_reason_and_trade_nothing(self):
        noexec = self.follower("ne", execute_allowed=False)
        stopped = self.follower("st")
        cl(stopped["u"]).post("/api/ea/rules", json={"max_risk_pct": 1, "daily_loss_pct": 1, "max_positions": 1})
        eareport(stopped["k"], "Lst", [pos(1), pos(2)])
        nospec = self.follower("nsp")
        eareport(self.km, "9000", [pos(900, symbol="XAUUSD")])
        fid = self.flag().json()["id"]
        for f, word in ((noexec, "AllowExecute"), (stopped, "maximum"), (nospec, "Market Watch")):
            self.assertEqual(eapoll(f["k"]).json()["cmd"], "", word)
            j = self.job(fid, f)
            self.assertEqual(j["status"], "skipped", word)
            self.assertIn(word, j["note"])

    def test_a_busy_account_waits_and_an_old_job_is_never_executed_late(self):
        fid = self.flag().json()["id"]
        cl(self.f1["u"]).post("/api/ea/command", json={"kind": "close_all", "account": self.f1["a"]})      # nothing to close -> 409, so create one directly
        q("INSERT INTO ea_commands(user_id,account_id,kind,created_at,status) VALUES(?,?,?,?,?)", self.f1["u"], self.f1["a"], "close", int(time.time()), "sent")
        eapoll(self.f1["k"])
        self.assertEqual(self.job(fid, self.f1)["status"], "waiting")                         # retried on the next poll
        q("UPDATE ea_commands SET status='done' WHERE account_id=?", self.f1["a"])
        self.assertEqual(eapoll(self.f1["k"]).json()["cmd"], "open")
        q("UPDATE copy_jobs SET created_at=? WHERE account_id=? AND status='waiting'", int(time.time()) - CT.JOB_TTL - 5, self.f2["a"])
        self.assertEqual(eapoll(self.f2["k"]).json()["cmd"], "")
        j = self.job(fid, self.f2)
        self.assertEqual(j["status"], "skipped")
        self.assertIn("not connected in time", j["note"])

    def test_subscription_ending_or_copying_switched_off_stops_a_waiting_job(self):
        a = self.follower("a1")
        b = self.follower("b1")
        fid = self.flag().json()["id"]
        q("UPDATE copy_subs SET ends_at=? WHERE user_id=?", int(time.time()) - 5, a["u"])
        cl(b["u"]).post("/api/copy/me/settings", json={"enabled": False})
        self.assertEqual(eapoll(a["k"]).json()["cmd"], "")
        self.assertEqual(eapoll(b["k"]).json()["cmd"], "")
        self.assertIn("subscription", self.job(fid, a)["note"])
        self.assertIn("off", self.job(fid, b)["note"])


class LifecycleTests(World, unittest.TestCase):
    def open_copy(self, f, fid, tickets):
        eapoll(f["k"])
        cid = q("SELECT command_id FROM copy_jobs WHERE flag_id=? AND account_id=?", fid, f["a"])[0]["command_id"]
        ANON.post("/api/ea/result", json={"id": cid, "ok": True}, headers={"x-driverline-key": f["k"]})
        eareport(f["k"], f["login"], [pos(t, volume=0.01) for t in tickets])

    def test_copy_opens_then_closes_when_the_master_trade_closes(self):
        fid = self.flag().json()["id"]
        self.open_copy(self.f1, fid, [5001])
        j = self.job(fid, self.f1)
        self.assertEqual((j["status"], j["ticket"]), ("open", 5001))
        eareport(self.km, "9000", [])                                                         # the master trade is gone
        self.assertEqual(q("SELECT status FROM copy_flags WHERE id=?", fid)[0]["status"], "closed")
        self.assertEqual(self.job(fid, self.f1)["close_req"], 1)
        p = eapoll(self.f1["k"]).json()
        self.assertEqual((p["cmd"], p["ticket"]), ("close", 5001))
        ANON.post("/api/ea/result", json={"id": p["id"], "ok": True, "closed": 1}, headers={"x-driverline-key": self.f1["k"]})
        eareport(self.f1["k"], self.f1["login"], [])
        self.assertEqual(self.job(fid, self.f1)["status"], "closed")
        self.assertEqual(eapoll(self.f1["k"]).json()["cmd"], "")                              # nothing more to do

    def test_a_copy_that_already_hit_its_own_stop_is_simply_marked_closed(self):
        fid = self.flag().json()["id"]
        self.open_copy(self.f1, fid, [5001])
        eareport(self.f1["k"], self.f1["login"], [])                                                       # the follower's SL/TP closed it first
        self.assertEqual(self.job(fid, self.f1)["status"], "closed")
        eareport(self.km, "9000", [])
        self.assertEqual(eapoll(self.f1["k"]).json()["cmd"], "")

    def test_a_close_only_touches_the_copy_never_the_members_other_trades(self):
        fid = self.flag().json()["id"]
        eareport(self.f1["k"], self.f1["login"], [pos(7000, volume=0.5)])                                  # the member's own, older trade on the same symbol
        self.open_copy(self.f1, fid, [7000, 5001])
        self.assertEqual(self.job(fid, self.f1)["ticket"], 5001)
        eareport(self.km, "9000", [])
        self.assertEqual(eapoll(self.f1["k"]).json()["ticket"], 5001)

    def test_an_ambiguous_ticket_is_never_guessed(self):
        fid = self.flag().json()["id"]
        self.open_copy(self.f1, fid, [5001, 5002])                                            # two identical new trades: cannot tell
        self.assertIsNone(self.job(fid, self.f1)["ticket"])
        eareport(self.km, "9000", [])
        self.assertEqual(eapoll(self.f1["k"]).json()["cmd"], "")

    def test_remote_close_off_leaves_a_note_and_a_failed_open_is_reported(self):
        fid = self.flag().json()["id"]
        eapoll(self.f1["k"])
        cid = self.job(fid, self.f1)["command_id"]
        ANON.post("/api/ea/result", json={"id": cid, "ok": False, "message": "Broker refused: no money"}, headers={"x-driverline-key": self.f1["k"]})
        eareport(self.f1["k"], self.f1["login"], [])
        j = self.job(fid, self.f1)
        self.assertEqual((j["status"], j["note"]), ("failed", "Broker refused: no money"))
        self.open_copy(self.f2, fid, [6001])
        eareport(self.f2["k"], self.f2["login"], [pos(6001, volume=0.01)], remote_close=False)
        eareport(self.km, "9000", [])
        self.assertEqual(eapoll(self.f2["k"]).json()["cmd"], "")
        self.assertIn("AllowRemoteClose", self.job(fid, self.f2)["note"])

    def test_admin_can_end_a_flagged_trade_and_a_waiting_copy_is_cancelled(self):
        fid = self.flag().json()["id"]
        self.open_copy(self.f1, fid, [5001])
        r = cl(self.admin).post(f"/api/copy/admin/flag/{fid}/close")
        self.assertEqual((r.status_code, r.json()["closing"]), (200, 1))
        self.assertEqual(self.job(fid, self.f2)["status"], "skipped")
        self.assertEqual(eapoll(self.f2["k"]).json()["cmd"], "")
        self.assertEqual(eapoll(self.f1["k"]).json()["cmd"], "close")
        jobs = cl(self.admin).get(f"/api/copy/admin/flag/{fid}").json()
        self.assertEqual(len(jobs), 2)
        self.assertTrue(all("email" in j for j in jobs))

    def test_a_removed_follower_account_stops_being_copied(self):
        fid = self.flag().json()["id"]
        cl(self.f1["u"]).delete(f"/api/ea/accounts/{self.f1['a']}")
        self.assertEqual(self.job(fid, self.f1)["status"], "closed")
        self.assertEqual(q("SELECT * FROM copy_members WHERE account_id=?", self.f1["a"]), [])


class MemberTests(World, unittest.TestCase):
    def test_subscription_is_needed_to_switch_copying_on(self):
        u = mk_user()
        a, k = add(u, "Mine")
        r = cl(u).post("/api/copy/me/settings", json={"enabled": True})
        self.assertEqual(r.status_code, 403)
        self.assertEqual(cl(u).post("/api/copy/me/settings", json={"enabled": False}).status_code, 200)
        me = cl(u).get("/api/copy/me").json()
        self.assertFalse(me["subscribed"])
        SUBS(u)
        self.assertEqual(cl(u).post("/api/copy/me/settings", json={"enabled": True, "mode": "risk", "risk_pct": 0.5}).status_code, 200)
        me = cl(u).get("/api/copy/me").json()
        self.assertTrue(me["subscribed"])
        self.assertEqual((me["accounts"][0]["enabled"], me["accounts"][0]["mode"], me["accounts"][0]["risk_pct"]), (True, "risk", 0.5))

    def test_validation_and_account_choice(self):
        u = mk_user()
        add(u, "A")
        add(u, "B")
        SUBS(u)
        self.assertEqual(cl(u).post("/api/copy/me/settings", json={"enabled": True}).status_code, 422)         # two accounts: must say which
        aid = cl(u).get("/api/ea/accounts").json()["accounts"][0]["id"]
        for bad in ({"mode": "all"}, {"fixed_lots": -1}, {"fixed_lots": "x"}, {"risk_pct": 99}, {"mode": "risk"}, {"max_risk_pct": 0}):
            r = cl(u).post("/api/copy/me/settings", json={"enabled": True, "account": aid, **bad})
            self.assertEqual(r.status_code, 422, bad)
        other = mk_user()
        self.assertEqual(cl(other).post("/api/copy/me/settings", json={"enabled": True, "account": aid}).status_code, 404)

    def test_revoking_a_subscription_switches_copying_off_and_cancels_waiting_copies(self):
        fid = self.flag().json()["id"]
        email = q("SELECT email FROM users WHERE id=?", self.f1["u"])[0]["email"]
        r = cl(self.admin).post("/api/copy/admin/subscription", json={"email": email, "revoke": True})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.job(fid, self.f1)["status"], "skipped")
        self.assertFalse(cl(self.f1["u"]).get("/api/copy/me").json()["accounts"][0]["enabled"])
        self.assertEqual(eapoll(self.f1["k"]).json()["cmd"], "")

    def test_granting_days_stacks_and_is_validated(self):
        u = mk_user()
        email = q("SELECT email FROM users WHERE id=?", u)[0]["email"]
        a = cl(self.admin).post("/api/copy/admin/subscription", json={"email": email, "days": 30}).json()["ends_at"]
        b = cl(self.admin).post("/api/copy/admin/subscription", json={"email": email, "days": 30}).json()["ends_at"]
        self.assertEqual(b - a, 30 * 86400)
        for bad in ({"email": email, "days": 0}, {"email": email, "days": 9999}, {"email": email, "days": "x"}, {"email": "nobody@x.example", "days": 5}):
            self.assertIn(cl(self.admin).post("/api/copy/admin/subscription", json=bad).status_code, (404, 422), bad)

    def test_the_admin_screen_lists_positions_flags_and_subscribers(self):
        fid = self.flag().json()["id"]
        d = cl(self.admin).get("/api/copy/admin").json()
        self.assertEqual(d["master"]["id"], self.m)
        self.assertEqual([p["flag_id"] for p in d["positions"]], [fid])
        self.assertTrue(d["positions"][0]["can_flag"])
        self.assertEqual(d["flags"][0]["counts"], {"waiting": 2})
        self.assertGreaterEqual(d["followers"], 2)
        self.assertTrue(any(s["active"] for s in d["subscribers"]))


if __name__ == "__main__":
    unittest.main(verbosity=2)
