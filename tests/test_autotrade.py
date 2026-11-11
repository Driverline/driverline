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


def eareport(key, positions=(), balance=20000.0, specs=None, currency="USD", **flags):
    ea._last_report.clear()
    extra = flags.pop("extra", {})
    acc = {"login": "1", "server": "Deriv-Demo", "currency": currency, "balance": balance, "equity": flags.pop("equity", balance), "demo": True,
           "remote_close": True, "manage_allowed": True, "execute_allowed": True, "pending_allowed": True, **flags}
    body = {"account": acc, "today": {"closed_pnl": 0}, "positions": list(positions), "orders": [], "specs": specs or SPECS, **extra}
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


def spec(sym, vmin=0.01):
    return {"symbol": sym, "tick_size": 0.01, "tick_value": 0.01, "vol_min": vmin, "vol_step": 0.01, "vol_max": 100, "contract_size": 1}


def inbox_msgs(uid):
    from app import inbox
    with closing(inbox.db()) as c:
        return [r["body"] for r in c.execute("SELECT body FROM inbox_messages WHERE user_id=? ORDER BY id", (uid,)).fetchall()]


class Tiers(unittest.TestCase):
    def test_boundaries(self):
        want = {25: 1, 25.99: 1, 26: 2, 59: 2, 59.99: 2, 60: 3, 159: 3, 160: 4, 599: 4, 600: 5, 1499: 5, 1500: 6, 10499: 6, 10500: 7, 50000: 7}
        for eq, t in want.items():
            self.assertEqual(AT.tier_of(eq), t, eq)

    def test_symbol_lists(self):
        self.assertEqual(AT.eligible_keys(1), {"v10", "v50"})
        self.assertEqual(AT.eligible_keys(2), {"v10", "v25", "v50", "xauusd"})
        self.assertIn("v50_1s", AT.eligible_keys(3))
        self.assertNotIn("v75", AT.eligible_keys(3))
        self.assertIn("step", AT.eligible_keys(4))
        self.assertNotIn("boom300", AT.eligible_keys(4))
        self.assertIn("crash300", AT.eligible_keys(5))
        self.assertNotIn("boom500", AT.eligible_keys(5))
        self.assertIn("crash1000", AT.eligible_keys(6))
        self.assertEqual(AT.eligible_keys(7), set(AT.INSTRUMENTS))
        self.assertEqual(AT.eligible_keys(None), set())

    def test_gold_h4_only_tiers_2_to_6(self):
        for t in (2, 3, 4, 5, 6):
            self.assertEqual(AT.trade_allowed(t, "xauusd", "h4"), "")
            self.assertIn("H4", AT.trade_allowed(t, "xauusd", "day"))
        self.assertEqual(AT.trade_allowed(7, "xauusd", "day"), "")
        self.assertIn("Not available", AT.trade_allowed(1, "xauusd", "h4"))
        self.assertIn("could not be verified", AT.trade_allowed(None, "v10", "day"))
        self.assertEqual(AT.gold_label(3), "XAUUSDmicro (H4 only)")
        self.assertEqual(AT.gold_label(6), "XAUUSD (H4 only)")

    def test_gold_contract_variant(self):
        sp = [spec("XAUUSD"), spec("XAUUSDmicro")]
        self.assertEqual(AT._pick_symbol(sp, "xauusd", 3)[0]["symbol"], "XAUUSDmicro")
        self.assertEqual(AT._pick_symbol(sp, "xauusd", 6)[0]["symbol"], "XAUUSD")
        self.assertIsNone(AT._pick_symbol([spec("XAUUSD")], "xauusd", 3)[0])
        self.assertIsNone(AT._pick_symbol([spec("XAUUSDmicro")], "xauusd", 6)[0])
        self.assertIn(AT._pick_symbol(sp, "xauusd", 7)[0]["symbol"], ("XAUUSD", "XAUUSDmicro"))


class Eligibility(unittest.TestCase):
    def setup(self, equity, **over):
        s = Setup(instruments=over.pop("instruments", ["v10"]), **over)
        eareport(s.key, balance=equity, specs=[spec("Volatility 10"), spec("Volatility 75"), spec("XAUUSDmicro"), spec("XAUUSD")])
        return s

    def test_api_refuses_locked_symbol_and_logs_it(self):
        s = self.setup(85.0)                                           # tier 3
        r = s.save(instruments=["v75"])
        self.assertEqual(r.status_code, 422)
        self.assertIn("Not available for your current account equity", r.json()["detail"])
        self.assertEqual(q("SELECT kind FROM auto_events WHERE account_id=?", s.aid)[0]["kind"], "rejected_setting")
        self.assertEqual(s.save(instruments=["v10"]).status_code, 200)
        me = cl(s.uid).get("/api/auto/me").json()["accounts"][0]["eligibility"]
        self.assertEqual((me["tier"], me["equity"], me["ok"]), (3, 85.0, True))
        self.assertIn("v50_1s", me["keys"])
        self.assertNotIn("v75", me["keys"])
        self.assertEqual(me["gold"], "XAUUSDmicro (H4 only)")

    def test_cannot_switch_on_when_equity_unverified(self):
        s = Setup()
        r = s.save()                                                   # nothing reported yet in this account's state is fine, then stale it
        q("UPDATE ea_accounts SET last_seen=? WHERE id=?", int(time.time()) - 600, s.aid)
        self.assertEqual(s.save().status_code, 409)

    def test_non_usd_refused(self):
        s = Setup(instruments=["v10"])
        eareport(s.key, balance=500, currency="EUR")
        r = s.save()
        self.assertEqual(r.status_code, 409)
        self.assertIn("USD", r.json()["detail"])

    def test_dispatch_revalidates_every_trade(self):
        s = Setup(instruments=["v75"], styles=["day"])
        eareport(s.key, balance=1000.0, specs=[spec("Volatility 75")])           # tier 5: v75 ok
        self.assertEqual(s.save().status_code, 200)
        sid = signal(inst="v75")
        AT.on_new_signals([sid])
        eareport(s.key, balance=100.0, specs=[spec("Volatility 75")])            # equity fell to tier 3 before the EA collected it
        eapoll(s.key)
        j = s.jobs()[0]
        self.assertEqual(j["status"], "skipped")
        self.assertIn("Not available for your current account equity", j["note"])
        self.assertEqual(q("SELECT kind FROM auto_events WHERE account_id=? AND kind='rejected_tier'", s.aid)[0]["kind"], "rejected_tier")
        self.assertEqual(q("SELECT * FROM ea_commands WHERE account_id=?", s.aid), [])

    def test_stale_equity_blocks_new_trades(self):
        s = Setup(instruments=["v10"])
        eareport(s.key, balance=1000.0, specs=[spec("Volatility 10")])
        s.save()
        sid = signal(inst="v10")
        AT.on_new_signals([sid])
        q("UPDATE ea_acct_state SET updated_at=? WHERE account_id=?", int(time.time()) - 600, s.aid)
        eapoll(s.key)
        self.assertIn("equity cannot be verified", s.jobs()[0]["note"])

    def test_gold_day_signal_refused_by_execution_layer(self):
        s = Setup(instruments=["xauusd"], styles=["day"])
        eareport(s.key, balance=100.0, specs=[spec("XAUUSDmicro")])
        self.assertEqual(s.save().status_code, 200)
        sid = signal(inst="xauusd", style="day")
        AT.on_new_signals([sid])
        eapoll(s.key)
        self.assertEqual(s.jobs()[0]["status"], "skipped")
        self.assertIn("H4", s.jobs()[0]["note"])

    def test_gold_h4_places_micro_in_tier_3(self):
        signal_feed.allowed_combos = lambda force=False: {("synthetic", "day", "x"): {}, ("metal", "h4", "x"): {}}
        try:
            s = Setup(instruments=["xauusd"], styles=["h4"], fixed_lots=0.01, max_risk_pct=10)
            eareport(s.key, balance=100.0, specs=[spec("XAUUSD"), spec("XAUUSDmicro")])
            self.assertEqual(s.save().status_code, 200)
            sid = signal(inst="xauusd", style="h4")
            AT.on_new_signals([sid])
            eapoll(s.key)
            cmd = q("SELECT payload FROM ea_commands WHERE account_id=?", s.aid)
            self.assertEqual(json.loads(cmd[0]["payload"])["symbol"], "XAUUSDmicro")
        finally:
            signal_feed.allowed_combos = lambda force=False: {("synthetic", "day", "ema_pullback"): {}, ("synthetic", "scalp", "ema_pullback"): {}}

    def test_tier_change_notifies_never_closes_never_activates(self):
        s = Setup(instruments=["v75"], styles=["day"])
        eareport(s.key, balance=700.0, specs=[spec("Volatility 75")])
        self.assertEqual(s.save().status_code, 200)
        eareport(s.key, balance=700.0, specs=[spec("Volatility 75")], positions=[pos(3, symbol="Volatility 75")])
        eareport(s.key, balance=550.0, specs=[spec("Volatility 75")], positions=[pos(3, symbol="Volatility 75")])   # tier 5 -> 4
        msgs = inbox_msgs(s.uid)
        self.assertTrue(any("tier 4, was tier 5" in m for m in msgs), msgs)
        self.assertEqual(q("SELECT * FROM ea_commands WHERE account_id=? AND kind IN ('close','close_all')", s.aid), [])
        ev = q("SELECT detail FROM auto_events WHERE account_id=? AND kind='tier_change'", s.aid)
        self.assertEqual(len(ev), 1)
        eareport(s.key, balance=2000.0, specs=[spec("Volatility 75")])      # up to tier 6: nothing new switched on
        self.assertEqual(get_sel(s), ["v75"])


def get_sel(s):
    with closing(ea.db()) as c:
        return AT.get_settings(c, s.aid)["instruments"]


class Loosening(unittest.TestCase):
    def test_first_setup_immediate_then_loosening_waits(self):
        s = Setup(instruments=["v10"], max_open=2)
        eareport(s.key, balance=1000.0, specs=[spec("Volatility 10")])
        self.assertEqual(s.save().status_code, 200)
        self.assertEqual(get_sel(s), ["v10"])
        r = s.save(max_open=5, instruments=["v10", "v50"]).json()
        self.assertEqual(r["settings"]["max_open"], 2)                    # still the old value
        self.assertEqual(r["settings"]["instruments"], ["v10"])
        self.assertIn("max_open", r["pending"])
        self.assertIn("instruments", r["pending"])
        me = cl(s.uid).get("/api/auto/me").json()["accounts"][0]
        self.assertEqual(me["form"]["max_open"], 5)                         # the form shows the waiting value
        # saving the same form again does not restart the clock
        at = r["pending"]["max_open"]["at"]
        r2 = s.save(max_open=5, instruments=["v10", "v50"]).json()
        self.assertEqual(r2["pending"]["max_open"]["at"], at)
        # tightening is immediate and cancels a waiting loosening
        r3 = s.save(max_open=1, instruments=["v10"]).json()
        self.assertEqual(r3["settings"]["max_open"], 1)
        self.assertEqual(r3["pending"], {})

    def test_matured_change_applies(self):
        s = Setup(instruments=["v10"], max_open=2)
        eareport(s.key, balance=1000.0, specs=[spec("Volatility 10")])
        s.save()
        s.save(max_open=6, max_per_day=9, max_rating="High")
        with closing(ea.db()) as c:
            row = json.loads(c.execute("SELECT pending FROM auto_settings WHERE account_id=?", (s.aid,)).fetchone()["pending"])
            for f in row:
                row[f]["at"] = int(time.time()) - 5
            c.execute("UPDATE auto_settings SET pending=? WHERE account_id=?", (json.dumps(row), s.aid))
            c.commit()
            got = AT.get_settings(c, s.aid)
        self.assertEqual((got["max_open"], got["max_per_day"], got["max_rating"]), (6, 9, "High"))
        self.assertEqual(got["pending"], {})

    def test_merge_rules(self):
        cur = {**AT.DEFAULTS, "styles": ["day"], "instruments": ["v10", "v50"]}
        want = {**cur, "instruments": ["v10"], "risk_pct": 0.5, "loss_limit": 2}
        eff, pend = AT.merge(cur, want, {}, 1000)
        self.assertEqual((eff["instruments"], eff["risk_pct"], eff["loss_limit"], pend), (["v10"], 0.5, 2, {}))
        want = {**cur, "risk_pct": 2.0, "loss_limit": 5, "max_rating": "High"}
        eff, pend = AT.merge(cur, want, {}, 1000)
        self.assertEqual(sorted(pend), ["loss_limit", "max_rating", "risk_pct"])
        self.assertEqual(eff["risk_pct"], 1.0)
        self.assertEqual(pend["risk_pct"]["at"], 1000 + 24 * 3600)


class LossPause(unittest.TestCase):
    def run_trades(self, s, profits, balance=1000.0):
        for i, pr in enumerate(profits):
            sid = signal(inst="v10")
            eareport(s.key, balance=balance, specs=[spec("Volatility 10")])
            AT.on_new_signals([sid])
            eapoll(s.key)
            if not s.jobs()[-1]["status"] == "sent":
                return
            q("UPDATE ea_commands SET status='done' WHERE account_id=?", s.aid)
            eareport(s.key, balance=balance, specs=[spec("Volatility 10")], positions=[pos(100 + i, symbol="Volatility 10")])
            now = int(time.time())
            eareport(s.key, balance=balance, specs=[spec("Volatility 10")], extra={"closed": [{
                "id": 100 + i, "symbol": "Volatility 10", "type": "BUY", "volume": 0.01, "open": 1, "close": 1, "profit": pr, "opened": now - 60, "closed": now}]})

    def start(self, limit=3):
        s = Setup(instruments=["v10"], max_open=5, max_per_day=20, loss_limit=limit, mode="fixed", fixed_lots=0.01, max_risk_pct=10)
        eareport(s.key, balance=1000.0, specs=[spec("Volatility 10")])
        self.assertEqual(s.save().status_code, 200, s.save().text)
        return s

    def test_three_losses_pause_and_resume_needs_confirmation(self):
        s = self.start()
        self.run_trades(s, [-5, -5, -5])
        me = cl(s.uid).get("/api/auto/me").json()["accounts"][0]
        self.assertEqual(me["paused_reason"], "losses")
        self.assertEqual(me["settings"]["enabled"], 0)
        self.assertTrue(any("paused itself after 3 losing" in m for m in inbox_msgs(s.uid)))
        self.assertEqual(s.save().status_code, 409)                            # plain re-enable is refused
        self.assertEqual(s.save(resume=True).status_code, 200)
        self.assertIsNone(cl(s.uid).get("/api/auto/me").json()["accounts"][0]["paused_reason"])
        self.run_trades(s, [-5, -5])                                            # the old losses no longer count
        self.assertEqual(cl(s.uid).get("/api/auto/me").json()["accounts"][0]["settings"]["enabled"], 1)

    def test_a_win_ends_the_run_and_breakeven_is_neutral(self):
        s = self.start()
        self.run_trades(s, [-5, -5, 8, -5, -5])
        self.assertEqual(cl(s.uid).get("/api/auto/me").json()["accounts"][0]["settings"]["enabled"], 1)
        self.run_trades(s, [0.2, -5])                                           # breakeven in between does not break the run
        self.assertEqual(cl(s.uid).get("/api/auto/me").json()["accounts"][0]["paused_reason"], "losses")

    def test_placed_message_in_inbox(self):
        s = self.start()
        self.run_trades(s, [3])
        self.assertTrue(any("placed a BUY Volatility 10 trade" in m for m in inbox_msgs(s.uid)))


class Admin(unittest.TestCase):
    def test_admin_view(self):
        s = Setup(instruments=["v10"])
        eareport(s.key, balance=20.0, specs=[spec("Volatility 10")])
        s.save()
        eareport(s.key, balance=20.0, specs=[spec("Volatility 10")])
        adm = Setup()
        d = cl(adm.uid).get("/api/auto/admin").json()
        row = next(a for a in d["accounts"] if a["account"] == s.aid)
        self.assertEqual((row["tier"], row["equity"], row["status"]), (1, 20.0, "ok"))
        self.assertEqual(row["symbols"], ["Volatility 10"])
        self.assertTrue(row["last_sync"])
        mem = Setup(tier="member")
        self.assertEqual(cl(mem.uid).get("/api/auto/admin").status_code, 403)


class Protection(unittest.TestCase):
    def test_autotrading_does_not_touch_protection_settings(self):
        s = Setup(instruments=["v10"])
        eareport(s.key, balance=1000.0, specs=[spec("Volatility 10")])
        with closing(ea.db()) as c:
            before = ea.get_manage(c, s.aid)
        s.save()
        with closing(ea.db()) as c:
            after = ea.get_manage(c, s.aid)
        self.assertEqual(before, after)
        self.assertEqual(after["prot_risk_pct"], 4.5)
        pv = cl(s.uid).get("/api/auto/me").json()["accounts"][0]["protection"]
        self.assertTrue(pv["is_default"])


V75_REAL = {"symbol": "Volatility 75 Index", "vol_max": 15, "vol_min": 0.01, "vol_step": 0.001, "tick_size": 0.01, "tick_value": 0.0001, "contract_size": 1}


class WrongBrokerTickValue(unittest.TestCase):
    """Nov 2026: Deriv reports Volatility 75 with tick value 0.0001 for tick size 0.01 (100x too small). Risk sizing took 6.322 lots for a
    726-point stop on a $1,062 account and lost $947. The risk of one lot over that stop is about $726, so 4.5% ($47.79) allows about 0.065 lot."""
    def test_v75_lot_is_about_a_16th_of_a_lot_not_six(self):
        from app import copy_trading
        lots, pct, why = copy_trading.size(V75_REAL, 1062.0, 725.9141678571032, "risk", None, 4.5, 4.5)
        self.assertEqual(why, "")
        self.assertAlmostEqual(lots, 0.065, places=3)
        self.assertLessEqual(pct, 4.5)

    def test_unit_helper(self):
        from app.config import unit_per_lot
        self.assertEqual(unit_per_lot(V75_REAL), 1.0)
        ok = {"symbol": "Volatility 100 Index", "tick_size": 0.01, "tick_value": 0.01, "contract_size": 1}
        self.assertEqual(unit_per_lot(ok), 1.0)
        self.assertAlmostEqual(unit_per_lot({"symbol": "EURUSDm", "tick_size": 0.00001, "tick_value": 1.0, "contract_size": 100000}), 100000.0, places=3)
        # a forex pair keeps the broker's own tick value (JPY pairs are worth far less than the contract size per unit)
        self.assertAlmostEqual(unit_per_lot({"symbol": "USDJPY", "tick_size": 0.001, "tick_value": 0.67, "contract_size": 100000}), 670.0)
        self.assertEqual(unit_per_lot({"symbol": "Volatility 75 Index", "contract_size": 1}), 1.0)

    def test_ea_reported_unit_is_used_when_larger(self):
        from app.config import unit_per_lot
        self.assertEqual(unit_per_lot({"symbol": "Boom 500 Index", "tick_size": 0.001, "tick_value": 0.0001, "contract_size": 1, "unit": 1.0}), 1.0)
        self.assertEqual(unit_per_lot({"symbol": "EURUSD", "tick_size": 0.00001, "tick_value": 0.5, "contract_size": 100000, "unit": 100000.0}), 100000.0)
        self.assertAlmostEqual(unit_per_lot({"symbol": "USDJPY", "tick_size": 0.001, "tick_value": 0.67, "contract_size": 100000, "unit": 667.0}), 670.0)
        self.assertAlmostEqual(unit_per_lot({"symbol": "EURUSD", "tick_size": 0.00001, "tick_value": 1.0, "contract_size": 100000, "unit": 0}), 100000.0, places=3)

    def test_equity_protection_precheck_uses_the_same_unit(self):
        from app import pending_orders
        pct = pending_orders._risk_pct(V75_REAL, 6.322, 725.9, 1062.0)
        self.assertGreater(pct, 400)                                  # it used to say about 4.5
        pct = pending_orders._risk_pct(V75_REAL, 0.065, 725.9, 1062.0)
        self.assertAlmostEqual(pct, 4.44, places=1)

    def test_autotrading_end_to_end_on_the_real_v75_spec(self):
        s = Setup(instruments=["v75"], styles=["swing"], mode="risk", risk_pct=4.5, max_risk_pct=4.5)
        sp = [V75_REAL]
        eareport(s.key, balance=1062.0, specs=sp)
        q("INSERT INTO ea_acct_rules(account_id,max_risk_pct,daily_loss_pct,max_positions) VALUES(?,?,?,?) ON CONFLICT(account_id) DO UPDATE SET max_risk_pct=5", s.aid, 5.0, 5.0, 5)
        self.assertEqual(s.save(styles=["day"], mode="risk", risk_pct=4.5, max_risk_pct=4.5).status_code, 200)
        sid = signal(inst="v75")
        with closing(signal_feed.db()) as c:
            c.execute("UPDATE signals SET entry=38582.45, stop=39308.36, target=37493.58, direction=-1 WHERE id=?", (sid,))
            c.commit()
        AT.on_new_signals([sid])
        eareport(s.key, balance=1062.0, specs=sp)
        eapoll(s.key)
        cmd = q("SELECT payload FROM ea_commands WHERE account_id=?", s.aid)
        self.assertEqual(len(cmd), 1, s.jobs())
        lots = json.loads(cmd[0]["payload"])["lots"]
        self.assertLess(lots, 0.07)
        self.assertGreater(lots, 0.05)


if __name__ == "__main__":
    unittest.main(verbosity=1)
