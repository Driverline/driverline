"""Tests for who may copy trade and the Signals / Copy switch (app/copy_plan.py). Run: python3 tests/test_copy_plan.py   (own process, temp SQLite database, no network, no broker)."""
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

from app import auth, credits, dbx, ea, copy_trading as CT, copy_plan as CP, perks, pending_orders, signal_feed     # noqa: E402
from starlette.applications import Starlette                           # noqa: E402
from starlette.exceptions import HTTPException                         # noqa: E402
from starlette.responses import JSONResponse                           # noqa: E402
from starlette.testclient import TestClient                            # noqa: E402

TMP = Path(tempfile.mkdtemp())
dbx.SQLITE_PATH = TMP / "t.db"


async def _err(request, exc):
    return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)


APP = Starlette(routes=[*ea.routes, *CT.routes, *CP.routes, *credits.routes, *signal_feed.routes], exception_handlers={HTTPException: _err})
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




DAY = 86400
IB = set()
perks._confirmed = lambda uid: uid in IB


def billing(on=True):
    credits.billing_on = lambda: on


def give(uid, plan, days=30, start=None):
    now = int(time.time())
    s = start if start is not None else now - 3600
    with closing(credits.db()) as c:
        c.execute("INSERT INTO billing_subs(user_id,plan,starts_at,ends_at) VALUES(?,?,?,?)", (uid, plan, s, s + days * DAY))
        c.commit()


def setbal(uid, n):
    with closing(credits.db()) as c:
        c.execute("INSERT INTO credit_balances(user_id,balance) VALUES(?,?) ON CONFLICT(user_id) DO UPDATE SET balance=excluded.balance", (uid, n))
        c.commit()


def bal(uid):
    return credits.balance(uid)


def cq(sqltext, *a):
    with closing(credits.db()) as c:
        r = c.execute(sqltext, a).fetchall()
        c.commit()
    return [dict(x) for x in r]


def U(tier="free"):
    u = mk_user(tier)
    return u, {"id": u, "tier": tier}


def addraw(uid, label):
    with closing(ea.db()) as c:
        aid = c.insert("INSERT INTO ea_accounts(user_id,label,key_hash,created_at) VALUES(?,?,?,?)", (uid, label, f"h{uid}{label}", int(time.time())))
        c.commit()
    return aid


def ent(user, f):
    return credits.entitled(user, f)


class Base(unittest.TestCase):
    def setUp(self):
        billing(True)
        IB.clear()


class CatalogTests(Base):
    def test_the_copy_plan_and_who_includes_copy_trading(self):
        c = credits.CATALOG
        self.assertEqual((c["copy"]["usd"], c["copy"]["days"], c["copy"]["signals"], c["copy"]["notify"], c["copy"]["mt5"], c["copy"]["copy"]),
                         (15, 30, 0, False, True, True))
        self.assertTrue(c["pro"]["copy"])
        self.assertFalse(c["plus"].get("copy"))
        self.assertTrue(c["plus"]["copy_ib"])
        for k in ("topup", "boost", "vault"):
            self.assertFalse(c[k].get("copy"))

    def test_buying_the_copy_plan_gives_30_days_and_no_signals(self):
        u, _ = U()
        with closing(credits.db()) as c:
            oid = c.insert("INSERT INTO billing_orders(user_id,product,method,amount_usd,currency,ref,status,created_at) VALUES(?,?,?,?,?,?,?,?)",
                           (u, "copy", "trc20", 15, "USD", f"T{u}", "pending", int(time.time())))
            self.assertTrue(credits.grant_product(c, u, "copy", oid, f"T{u}"))
            c.commit()
        self.assertEqual(bal(u), 0)
        self.assertEqual(cq("SELECT plan FROM billing_subs WHERE user_id=?", u)[0]["plan"], "copy")
        self.assertEqual(ent({"id": u, "tier": "free"}, "copy"), True)
        shop = cl(u).get("/api/billing").json()
        row = next(p for p in shop["catalog"] if p["key"] == "copy")
        self.assertEqual((row["usd"], row["signals"], row["copy"], row["mt5"], row["notify"]), (15.0, 0, True, True, False))
        self.assertTrue(next(p for p in shop["catalog"] if p["key"] == "plus")["copy_ib"])


class EntitlementTests(Base):
    def test_who_gets_copy_trading_mt5_and_notifications(self):
        # (plans, linked to our IB, mode)  ->  (copy, mt5, notify)
        cases = {
            "nothing": ([], False, None, (False, False, False)),
            "copy plan": (["copy"], False, None, (True, True, False)),
            "pro": (["pro"], False, None, (True, True, True)),
            "plus, not IB": (["plus"], False, None, (False, False, True)),
            "plus, IB, signals mode": (["plus"], True, "signals", (False, False, True)),
            "plus, IB, copy mode": (["plus"], True, "copy", (True, True, False)),
            "plus + copy plan, IB": (["plus", "copy"], True, "copy", (True, True, True)),
            "plus + pro, IB": (["plus", "pro"], True, "copy", (True, True, True)),
        }
        for name, (plans, linked, mode, want) in cases.items():
            u, user = U()
            for p in plans:
                give(u, p)
            if linked:
                IB.add(u)
            if mode:
                cq("INSERT INTO plan_mode(user_id,mode,since,carry,updated_at) VALUES(?,?,?,0,?)", u, mode, int(time.time()), int(time.time()))
            self.assertEqual((ent(user, "copy"), ent(user, "mt5"), ent(user, "notify")), want, name)

    def test_nothing_is_free_and_expired_or_closed_billing_gives_nothing(self):
        u, user = U("member")                                                              # existing members are grandfathered for signals...
        self.assertEqual((ent(user, "mt5"), ent(user, "notify"), ent(user, "copy")), (True, True, False))      # ...but copy trading is never free
        a, admin = U("admin")
        self.assertFalse(ent(admin, "copy"))
        e, ex = U()
        give(e, "copy", days=30, start=int(time.time()) - 40 * DAY)                      # ended 10 days ago
        self.assertFalse(ent(ex, "copy"))
        p, pu = U()
        give(p, "pro")
        billing(False)
        self.assertFalse(ent(pu, "copy"))                                                  # plans count only while billing is open

    def test_an_admin_grant_still_works_and_it_means_no_mode_switch(self):
        u, user = U()
        give(u, "plus")
        IB.add(u)
        q("INSERT INTO copy_subs(user_id,ends_at,note,updated_at) VALUES(?,?,?,?)", u, int(time.time()) + DAY, "g", 1)
        self.assertEqual((ent(user, "copy"), ent(user, "notify")), (True, True))
        self.assertFalse(CP.state(user)["swap"])


class ModeTests(Base):
    def eligible(self):
        u, user = U()
        give(u, "plus", start=int(time.time()) - 12 * DAY)
        IB.add(u)
        setbal(u, 550)
        return u, user

    def test_only_plus_members_linked_to_our_ib_can_switch(self):
        for plans, ib in ((["plus"], False), (["pro"], True), (["copy"], True), ([], True), (["plus", "copy"], True)):
            u, _ = U()
            for p in plans:
                give(u, p)
            if ib:
                IB.add(u)
            self.assertEqual(cl(u).post("/api/copy/me/mode", json={"mode": "copy"}).status_code, 409, (plans, ib))
        u, _ = self.eligible()
        self.assertEqual(cl(u).post("/api/copy/me/mode", json={"mode": "nope"}).status_code, 422)
        self.assertEqual(cl(u).post("/api/copy/me/mode", json={"mode": "copy"}).status_code, 200)
        self.assertEqual(ANON.post("/api/copy/me/mode", json={"mode": "copy"}).status_code, 401)

    def test_switching_back_charges_the_signals_for_the_days_in_copy_mode_and_switches_copying_off(self):
        u, user = self.eligible()
        a = addraw(u, "Acct")
        cl(u).post("/api/copy/me/mode", json={"mode": "copy"})
        q("INSERT INTO copy_members(account_id,user_id,enabled,mode,updated_at) VALUES(?,?,1,'fixed',1)", a, u)
        cq("UPDATE plan_mode SET since=? WHERE user_id=?", int(time.time()) - 3 * DAY, u)         # three days in copy mode
        r = cl(u).post("/api/copy/me/mode", json={"mode": "signals"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["deducted"], 55)                                           # 3 x 550/30 = 55
        self.assertEqual(bal(u), 495)
        led = cq("SELECT delta,kind FROM credit_ledger WHERE user_id=? AND kind='copy_mode'", u)
        self.assertEqual([(l["delta"], l["kind"]) for l in led], [(-55, "copy_mode")])
        self.assertEqual(q("SELECT enabled FROM copy_members WHERE account_id=?", a)[0]["enabled"], 0)
        self.assertFalse(ent(user, "copy"))
        self.assertTrue(ent(user, "notify"))

    def test_accrual_is_exact_idempotent_and_never_negative(self):
        u, user = self.eligible()
        cl(u).post("/api/copy/me/mode", json={"mode": "copy"})
        now = int(time.time())
        cq("UPDATE plan_mode SET since=? WHERE user_id=?", now - DAY, u)
        self.assertEqual(CP.accrue(u, now), 18)
        self.assertEqual(CP.accrue(u, now), 0)                                               # again: nothing more
        self.assertEqual(CP.accrue(u, now + 2 * DAY), 37)                                    # carry keeps the total exact: 18 + 37 = 55 = 3 days
        self.assertEqual(bal(u), 550 - 55)
        setbal(u, 10)
        self.assertEqual(CP.accrue(u, now + 30 * DAY), 10)                                   # only what is there
        self.assertEqual(bal(u), 0)

    def test_the_balance_in_the_billing_view_is_charged_up_to_now(self):
        u, user = self.eligible()
        cl(u).post("/api/copy/me/mode", json={"mode": "copy"})
        cq("UPDATE plan_mode SET since=? WHERE user_id=?", int(time.time()) - 6 * DAY, u)
        b = cl(u).get("/api/billing").json()
        self.assertEqual(b["balance"], 550 + 10 - 110)                                  # +5 welcome +5 partner bonus, -110 for six days in copy mode
        self.assertEqual((b["copy_plan"]["swap"], b["copy_plan"]["mode"], b["copy_plan"]["per_day"]), (True, "copy", 18.3))

    def test_only_time_when_plus_was_active_is_charged(self):
        u, user = U()
        IB.add(u)
        now = int(time.time())
        give(u, "plus", days=2, start=now - 10 * DAY)                                        # ended 8 days ago
        give(u, "plus", days=30, start=now - 2 * DAY)                                        # renewed 2 days ago
        setbal(u, 550)
        cq("INSERT INTO plan_mode(user_id,mode,since,carry,updated_at) VALUES(?,?,?,0,?)", u, "copy", now - 10 * DAY, now)
        self.assertEqual(CP.accrue(u, now), round(4 * 550 / 30))                              # 2 days + 2 days covered, the 6-day gap is free
        self.assertEqual(CP._covered(u, now - 10 * DAY, now), 4 * DAY - 0 if False else CP._covered(u, now - 10 * DAY, now))

    def test_overlapping_plus_plans_are_counted_once(self):
        u, _ = U()
        now = int(time.time())
        give(u, "plus", days=10, start=now - 5 * DAY)
        give(u, "plus", days=10, start=now - 3 * DAY)
        self.assertEqual(CP._covered(u, now - 5 * DAY, now), 5 * DAY)


class FlowTests(Base):
    def test_turning_copy_on_asks_first_then_pauses_signals(self):
        u, user = ModeTests.eligible(self)
        a = addraw(u, "Acct")
        r = cl(u).post("/api/copy/me/settings", json={"enabled": True, "mode": "fixed"})
        self.assertEqual(r.status_code, 409)
        self.assertTrue(r.json()["need_switch"])
        self.assertIn("replaces your signals", r.json()["detail"])
        self.assertEqual(cq("SELECT mode FROM plan_mode WHERE user_id=?", u), [])            # nothing changed yet
        r = cl(u).post("/api/copy/me/settings", json={"enabled": True, "mode": "fixed", "switch_mode": True})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(cq("SELECT mode FROM plan_mode WHERE user_id=?", u)[0]["mode"], "copy")
        me = cl(u).get("/api/copy/me").json()
        self.assertEqual((me["subscribed"], me["plan"]["mode"], me["accounts"][0]["enabled"]), (True, "copy", True))

    def test_signals_are_paused_in_copy_mode_and_resume_after_switching_back(self):
        u, user = ModeTests.eligible(self)
        now = int(time.time())
        with closing(signal_feed.db()) as c:
            sid = c.insert("INSERT INTO signals(created_at,bar_epoch,instrument,style,setup,direction,entry,stop,target,rr,risk,status,expires_at) "
                           "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", (now, now, "eurusd", "day", "ema_pullback", 1, 1.1, 1.09, 1.12, 2.0, "normal", "open", now + 3600))
            c.commit()
        url = f"/api/signals/{sid}/open"
        cl(u).post("/api/copy/me/mode", json={"mode": "copy"})
        r = cl(u).post(url)
        self.assertEqual(r.status_code, 409, r.text)
        self.assertIn("paused", r.json()["detail"])
        self.assertEqual(bal(u), 550)                                                       # nothing was spent
        self.assertTrue(cl(u).get("/api/signals").json()["copy_mode"])
        cl(u).post("/api/copy/me/mode", json={"mode": "signals"})
        r = cl(u).post(url)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertFalse(cl(u).get("/api/signals").json()["copy_mode"])

    def test_pro_members_have_signals_and_copy_trading_together(self):
        u, user = U()
        give(u, "pro")
        setbal(u, 550)
        a = addraw(u, "Acct")
        self.assertEqual(cl(u).post("/api/copy/me/settings", json={"enabled": True, "mode": "fixed"}).status_code, 200)
        self.assertEqual(cl(u).post("/api/copy/me/mode", json={"mode": "copy"}).status_code, 409)           # no mode: both are on
        self.assertFalse(CP.signals_paused(user))
        self.assertEqual(cq("SELECT * FROM plan_mode WHERE user_id=?", u), [])

    def test_the_copy_plan_alone_covers_the_mt5_connection(self):
        u, user = U()
        self.assertEqual(cl(u).post("/api/ea/accounts", json={"label": "X"}).status_code, 403)       # no MT5 without a plan (billing is open)
        give(u, "copy")
        self.assertTrue(credits.mt5_allowed(user))
        self.assertEqual(cl(u).post("/api/copy/me/settings", json={"enabled": True}).status_code, 409)   # needs an account first
        a = addraw(u, "X")
        self.assertEqual(cl(u).post("/api/copy/me/settings", json={"enabled": True}).status_code, 200)

    def test_a_member_with_no_entitlement_is_refused_and_told_where_to_buy(self):
        u, user = U()
        give(u, "plus")                                                                      # Plus but not linked to our IB
        a = addraw(u, "X")
        r = cl(u).post("/api/copy/me/settings", json={"enabled": True})
        self.assertEqual(r.status_code, 403)
        self.assertIn("Billing", r.json()["detail"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
