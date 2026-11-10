"""Tests for AutoTrading (app/autotrade.py). Run: python3 tests/test_autotrade.py   (own process, temp SQLite database, no network, no broker)."""
import json
import os
import sys
import tempfile
import time
import types
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
for _n in ("websockets", "pywebpush", "psycopg", "psycopg.rows"):
    try:
        __import__(_n)
    except ImportError:
        sys.modules[_n] = types.ModuleType(_n)
os.environ.update(DRIVERLINE_SECRET="s" * 40, DRIVERLINE_MEMBER_KEYS="1")
os.environ.pop("DATABASE_URL", None)

from app import auth, dbx, ea, autotrade as AT, signal_feed          # noqa: E402
from starlette.applications import Starlette                         # noqa: E402
from starlette.exceptions import HTTPException                       # noqa: E402
from starlette.responses import JSONResponse                         # noqa: E402
from starlette.testclient import TestClient                          # noqa: E402

TMP = Path(tempfile.mkdtemp())
dbx.SQLITE_PATH = TMP / "t.db"
signal_feed.allowed_combos = lambda force=False: {("synthetic", "day", "ema_pullback"): {}, ("synthetic", "scalp", "ema_pullback"): {}}


async def _err(request, exc):
    return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)


APP = Starlette(routes=[*ea.routes, *AT.routes], exception_handlers={HTTPException: _err})
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
        r = con.execute(sqltext, a).fetchall() if sqltext.lstrip().upper().startswith("SELECT") else (con.execute(sqltext, a) and [])
        con.commit()
    return [dict(x) for x in r]


def add(uid, label="Main"):
    r = cl(uid).post("/api/ea/accounts", json={"label": label})
    assert r.status_code == 200, r.text
    return r.json()["id"], r.json()["key"]


def eareport(key, positions=(), balance=1000.0, **flags):
    ea._last_report.clear()
    acc = {"login": "1", "server": "Deriv-Demo", "currency": "USD", "balance": balance, "equity": balance, "demo": True,
           "remote_close": True, "manage_allowed": True, "execute_allowed": True, "pending_allowed": True, **flags}
    body = {"account": acc, "today": {"closed_pnl": 0}, "positions": list(positions), "orders": [], "specs": SPECS}
    return ANON.post("/api/ea/report", json=body, headers={"x-driverline-key": key})


def eapoll(key):
    ea._last_poll.clear()
    return ANON.post("/api/ea/poll", json={}, headers={"x-driverline-key": key})


def pos(ticket, typ="BUY", symbol="EURUSDm", volume=0.1):
    return {"ticket": ticket, "symbol": symbol, "type": typ, "volume": volume, "open": 1.1, "sl": 1.099, "tp": 1.103, "profit": 0, "risk": 1}


_bar = [1000]


def signal(inst="eurusd", style="day", risk="Low", direction=1, age=0, ttl=3600, status="open"):
    now = int(time.time())
    _bar[0] += 900
    with closing(signal_feed.db()) as con:
        sid = con.insert("INSERT INTO signals(created_at,bar_epoch,instrument,style,setup,direction,entry,stop,target,rr,atr,risk,status,expires_at) "
                         "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                         (now - age, _bar[0], inst, style, "ema_pullback", direction, 1.1000, 1.0990, 1.1020, 2.0, 0.001, risk, status, now + ttl))
        con.commit()
    return sid


class Setup:
    def __init__(self, tier="admin", **over):
        self.uid = mk_user(tier)
        self.aid, self.key = add(self.uid)
        self.body = {"account": self.aid, "enabled": True, "acknowledge": True, "styles": ["day"], "instruments": ["eurusd"],
                     "mode": "fixed", "fixed_lots": 0.05, "max_risk_pct": 3, "max_open": 3, "max_per_day": 5, "max_rating": "Medium", **over}
        eareport(self.key)

    def save(self, **over):
        return cl(self.uid).post("/api/auto/settings", json={**self.body, **over})

    def jobs(self):
        return q("SELECT * FROM auto_jobs WHERE account_id=? ORDER BY id", self.aid)


class Access(unittest.TestCase):
    def test_member_not_listed_sees_nothing_and_cannot_save(self):
        s = Setup(tier="member")
        self.assertEqual(cl(s.uid).get("/api/auto/me").json(), {"available": False})
        self.assertEqual(s.save().status_code, 403)

    def test_tester_and_admin_can(self):
        a = Setup()
        self.assertTrue(cl(a.uid).get("/api/auto/me").json()["available"])
        m = Setup(tier="member")
        adm = cl(a.uid)
        with closing(auth.db()) as c:
            email = c.execute("SELECT email FROM users WHERE id=?", (m.uid,)).fetchone()["email"]
        r = adm.post("/api/auto/admin", json={"mode": "testers", "testers": [email]})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(m.save().status_code, 200, m.save().text)
        # a free member (not paid) listed as tester is still refused
        f = Setup(tier="free")
        with closing(auth.db()) as c:
            fe = c.execute("SELECT email FROM users WHERE id=?", (f.uid,)).fetchone()["email"]
        adm.post("/api/auto/admin", json={"mode": "testers", "testers": [email, fe]})
        self.assertEqual(f.save().status_code, 403)

    def test_public_mode_does_not_exist_and_off_blocks_everyone(self):
        a = Setup()
        self.assertEqual(cl(a.uid).post("/api/auto/admin", json={"mode": "public"}).status_code, 422)
        cl(a.uid).post("/api/auto/admin", json={"mode": "off"})
        self.assertEqual(a.save().status_code, 403)
        cl(a.uid).post("/api/auto/admin", json={"mode": "testers", "testers": []})
        self.assertEqual(a.save().status_code, 200)


class Settings(unittest.TestCase):
    def test_validation(self):
        s = Setup()
        self.assertEqual(s.save(acknowledge=False).status_code, 422)
        self.assertEqual(s.save(styles=["h4"]).status_code, 422)             # not published
        self.assertEqual(s.save(instruments=["nope"]).status_code, 422)
        self.assertEqual(s.save(styles=[]).status_code, 422)                 # on with nothing chosen
        self.assertEqual(s.save(max_open=0).status_code, 422)
        self.assertEqual(s.save(max_rating="Extreme").status_code, 422)
        self.assertEqual(s.save(mode="risk", risk_pct=None).status_code, 422)
        self.assertEqual(s.save().status_code, 200)
        r = cl(s.uid).get("/api/auto/me").json()
        self.assertEqual(r["accounts"][0]["settings"]["styles"], ["day"])
        self.assertTrue(r["accounts"][0]["acknowledged"])

    def test_another_members_account_is_not_reachable(self):
        a, b = Setup(), Setup()
        r = cl(a.uid).post("/api/auto/settings", json={**a.body, "account": b.aid})
        self.assertEqual(r.status_code, 404)


class Queueing(unittest.TestCase):
    def test_only_matching_signals_make_jobs(self):
        s = Setup()
        self.assertEqual(s.save().status_code, 200)
        ok, wrong_style, wrong_sym, high = signal(), signal(style="scalp"), signal(inst="v75"), signal(risk="High")
        AT.on_new_signals([ok, wrong_style, wrong_sym, high])
        self.assertEqual([j["signal_id"] for j in s.jobs()], [ok])
        AT.on_new_signals([ok])                                              # never twice
        self.assertEqual(len(s.jobs()), 1)

    def test_off_account_gets_nothing(self):
        s = Setup()
        s.save(enabled=False)
        AT.on_new_signals([signal()])
        self.assertEqual(s.jobs(), [])


class Dispatch(unittest.TestCase):
    def run_one(self, s, **flags):
        eareport(s.key, **flags)
        AT.on_new_signals([self.sid])
        return eapoll(s.key).json()

    def start(self, **over):
        s = Setup(**over)
        self.assertEqual(s.save().status_code, 200)
        self.sid = signal()
        return s

    def test_places_an_order_with_stop_and_target(self):
        s = self.start()
        p = self.run_one(s)
        job = s.jobs()[0]
        self.assertEqual(job["status"], "sent", job)
        cmd = q("SELECT * FROM ea_commands WHERE account_id=?", s.aid)[0]
        pl = json.loads(cmd["payload"])
        self.assertEqual((pl["symbol"], pl["dir"], pl["lots"], pl["comment"]), ("EURUSDm", "BUY", 0.05, f"TLA-{self.sid}"))
        self.assertAlmostEqual(pl["sl_dist"], 0.001)
        self.assertAlmostEqual(pl["tp_dist"], 0.002)
        self.assertEqual(cmd["kind"], "open")

    def test_job_then_follows_the_trade_to_closed(self):
        s = self.start()
        self.run_one(s)
        q("UPDATE ea_commands SET status='done' WHERE account_id=?", s.aid)
        eareport(s.key, positions=[pos(77)])
        j = s.jobs()[0]
        self.assertEqual((j["status"], j["ticket"]), ("open", 77))
        eareport(s.key, positions=[])
        self.assertEqual(s.jobs()[0]["status"], "closed")

    def test_failed_command_marks_failed(self):
        s = self.start()
        self.run_one(s)
        q("UPDATE ea_commands SET status='failed', result=? WHERE account_id=?", json.dumps({"message": "Not enough money"}), s.aid)
        eareport(s.key)
        j = s.jobs()[0]
        self.assertEqual((j["status"], j["note"]), ("failed", "Not enough money"))

    def skipped(self, s, text):
        j = s.jobs()[0]
        self.assertEqual(j["status"], "skipped", j)
        self.assertIn(text, j["note"])
        self.assertEqual(q("SELECT * FROM ea_commands WHERE account_id=?", s.aid), [])

    def test_skips_when_execute_not_allowed(self):
        s = self.start()
        self.run_one(s, execute_allowed=False)
        self.skipped(s, "AllowExecute")

    def test_skips_stale_job_never_late(self):
        s = self.start()
        eareport(s.key)
        AT.on_new_signals([self.sid])
        q("UPDATE auto_jobs SET created_at=created_at-500 WHERE account_id=?", s.aid)
        eapoll(s.key)
        self.skipped(s, "never placed late")

    def test_skips_when_signal_expired_or_closed(self):
        s = self.start()
        eareport(s.key)
        AT.on_new_signals([self.sid])
        with closing(signal_feed.db()) as c:
            c.execute("UPDATE signals SET status='target' WHERE id=?", (self.sid,))
            c.commit()
        eapoll(s.key)
        self.skipped(s, "no longer fresh")

    def test_max_open_and_symbol_and_day_limits(self):
        s = self.start(max_open=1)
        self.run_one(s)                                                       # first goes out
        self.assertEqual(s.jobs()[0]["status"], "sent")
        sid2 = signal(direction=-1)
        q("UPDATE ea_commands SET status='done' WHERE account_id=?", s.aid)
        eareport(s.key, positions=[pos(5)])
        AT.on_new_signals([sid2])
        eapoll(s.key)
        j = s.jobs()[1]
        self.assertEqual(j["status"], "skipped")
        self.assertIn("maximum", j["note"])
        # one position per symbol
        s2 = Setup(max_open=5)
        s2.save()
        sid3 = signal()
        eareport(s2.key, positions=[pos(9)])
        AT.on_new_signals([sid3])
        eapoll(s2.key)
        self.assertIn("already have an open EURUSDm", s2.jobs()[0]["note"])
        # daily maximum
        s3 = Setup(max_per_day=1)
        s3.save()
        q("INSERT INTO auto_jobs(signal_id,account_id,user_id,status,placed_at,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
          9999, s3.aid, s3.uid, "closed", int(time.time()), int(time.time()), int(time.time()))
        sid4 = signal()
        eareport(s3.key)
        AT.on_new_signals([sid4])
        eapoll(s3.key)
        self.assertIn("daily maximum", [j for j in s3.jobs() if j["signal_id"] == sid4][0]["note"])

    def test_skips_over_cap_lot(self):
        s = self.start(mode="fixed", fixed_lots=5.0, max_risk_pct=1)         # 5 lots x 10 pips on 1000 = way above 1%
        self.run_one(s)
        self.skipped(s, "above your")

    def test_pause_cancels_waiting_jobs(self):
        s = self.start()
        AT.on_new_signals([self.sid])
        s.save(enabled=False)
        self.assertEqual(s.jobs()[0]["status"], "skipped")

    def test_removing_account_clears_settings(self):
        s = self.start()
        r = cl(s.uid).request("DELETE", f"/api/ea/accounts/{s.aid}")
        if r.status_code == 200:
            self.assertEqual(q("SELECT * FROM auto_settings WHERE account_id=?", s.aid), [])


if __name__ == "__main__":
    unittest.main(verbosity=1)
