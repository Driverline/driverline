"""Tests for price alerts (app/price_alerts.py). Run: python3 tests/test_price_alerts.py   (own process, temp SQLite database, no network)."""
import asyncio
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
os.environ.update(DRIVERLINE_SECRET="s" * 40)
os.environ.pop("DATABASE_URL", None)

from app import auth, dbx, inbox, price_alerts as PA, push          # noqa: E402
from starlette.applications import Starlette                         # noqa: E402
from starlette.exceptions import HTTPException                       # noqa: E402
from starlette.responses import JSONResponse                         # noqa: E402
from starlette.testclient import TestClient                          # noqa: E402

TMP = Path(tempfile.mkdtemp())
dbx.SQLITE_PATH = TMP / "t.db"


async def _err(request, exc):
    return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)


APP = Starlette(routes=[*PA.routes], exception_handlers={HTTPException: _err})
_seq, _clients = [0], {}
PRICE, RANGE = 38000.0, 100.0          # a calm V75: M15 candles about 100 points tall


def candles15():
    now = int(time.time())
    base = now - now % 900
    out = [{"epoch": base - 900 * (60 - i), "open": PRICE, "high": PRICE + RANGE / 2, "low": PRICE - RANGE / 2, "close": PRICE} for i in range(60)]
    return out


async def fake_market(key, data=None):
    return {"price": PRICE, "range": RANGE}


REAL_MARKET = PA.market
PA.market = fake_market
PA.PREVIEW_GAP = 0


def mk_user(tier="member"):
    _seq[0] += 1
    with closing(auth.db()) as con:
        uid = con.insert("INSERT INTO users(email,pw_hash,tier,created_at) VALUES(?,?,?,?)", (f"p{_seq[0]}@t.example", "h", tier, "2026-01-01T00:00:00"))
        con.commit()
    return uid


def cl(uid):
    if uid not in _clients:
        c = TestClient(APP)
        c.__enter__()
        c.cookies.set(auth.COOKIE, auth.make_token(uid, "h"))
        _clients[uid] = c
    return _clients[uid]


def mk(uid, level=38500.0, **kw):
    return cl(uid).post("/api/alerts", json={"instrument": "v75", "level": level, **kw})


def rows(uid):
    with closing(PA.db()) as con:
        return [dict(r) for r in con.execute("SELECT * FROM price_alerts WHERE user_id=? ORDER BY id", (uid,)).fetchall()]


def m1(high, low, epoch=None):
    return {"M1": [{"epoch": epoch or int(time.time()), "open": low, "high": high, "low": low, "close": high}]}


def inbox_msgs(uid):
    with closing(inbox.db()) as c:
        return [r["body"] for r in c.execute("SELECT body FROM inbox_messages WHERE user_id=?", (uid,)).fetchall()]


class Zone(unittest.TestCase):
    def test_fires_early_not_at_the_level(self):
        z = PA.zone(38000, 38500, 100, 2)
        self.assertEqual(z["dir"], "up")
        self.assertEqual(z["buffer"], 200)             # 2 x the 15-minute range
        self.assertEqual(z["trigger"], 38300)
        z = PA.zone(38000, 37500, 100, 2)
        self.assertEqual((z["dir"], z["trigger"]), ("down", 37700))

    def test_buffer_never_more_than_half_the_way(self):
        z = PA.zone(38000, 38250, 100, 3)              # 4 x 100 = 400 would pass the price
        self.assertEqual(z["buffer"], 125)
        self.assertGreater(z["trigger"], 38000)

    def test_too_close_is_refused(self):
        with self.assertRaises(ValueError):
            PA.zone(38000, 38050, 100, 2)

    def test_options(self):
        self.assertEqual(PA.zone(38000, 39000, 100, 1)["buffer"], 100)
        self.assertEqual(PA.zone(38000, 39000, 100, 3)["buffer"], 400)


class Api(unittest.TestCase):
    def test_create_list_cancel(self):
        u = mk_user()
        r = mk(u, note="check <b>entry</b>")
        self.assertEqual(r.status_code, 200, r.text)
        a = r.json()["alert"]
        self.assertEqual((a["dir"], a["trigger_price"], a["status"]), ("up", 38300, "active"))
        self.assertNotIn("<", a["note"])
        me = cl(u).get("/api/alerts").json()
        self.assertTrue(me["available"])
        self.assertEqual(len(me["active"]), 1)
        self.assertEqual(cl(u).post(f"/api/alerts/{a['id']}/cancel").status_code, 200)
        self.assertEqual(cl(u).post(f"/api/alerts/{a['id']}/cancel").status_code, 404)
        self.assertEqual(rows(u)[0]["status"], "cancelled")

    def test_only_the_owner_can_cancel(self):
        a, b = mk_user(), mk_user()
        aid = mk(a).json()["alert"]["id"]
        self.assertEqual(cl(b).post(f"/api/alerts/{aid}/cancel").status_code, 404)
        self.assertEqual(rows(a)[0]["status"], "active")

    def test_limit_of_active_alerts(self):
        u = mk_user()
        for i in range(PA.MAX_ACTIVE):
            self.assertEqual(mk(u, 38300.0 + 200 * (i + 1)).status_code, 200)
        r = mk(u, 41000.0)
        self.assertEqual(r.status_code, 409)
        self.assertIn(str(PA.MAX_ACTIVE), r.json()["detail"])

    def test_daily_limit(self):
        u = mk_user()
        old = PA.PER_DAY, PA.MAX_ACTIVE
        PA.PER_DAY, PA.MAX_ACTIVE = 3, 50
        try:
            for i in range(3):
                self.assertEqual(mk(u, 38300.0 + 200 * i).status_code, 200)
            self.assertEqual(mk(u, 40000.0).status_code, 429)
        finally:
            PA.PER_DAY, PA.MAX_ACTIVE = old

    def test_duplicate_and_bad_input(self):
        u = mk_user()
        self.assertEqual(mk(u, 38500.0).status_code, 200)
        self.assertEqual(mk(u, 38500.0).status_code, 409)
        self.assertEqual(mk(u, 38050.0).status_code, 422)                      # too close
        self.assertEqual(cl(u).post("/api/alerts", json={"instrument": "nope", "level": 1}).status_code, 422)
        self.assertEqual(mk(u, "abc").status_code, 422)
        self.assertEqual(mk(u, -5).status_code, 422)
        self.assertEqual(mk(u, "nan").status_code, 422)
        self.assertEqual(mk(u, 39500.0, early=9).status_code, 422)
        self.assertEqual(mk(u, 39500.0, days=99).status_code, 422)

    def test_free_member_without_plan_and_signed_out(self):
        f = mk_user("free")
        self.assertFalse(cl(f).get("/api/alerts").json()["available"])
        self.assertEqual(mk(f).status_code, 403)
        self.assertIn(TestClient(APP).get("/api/alerts").status_code, (401, 403))

    def test_preview(self):
        u = mk_user()
        p = cl(u).get("/api/alerts/preview?instrument=v75&level=38500&early=2").json()
        self.assertEqual((p["price"], p["trigger"], p["buffer"]), (38000, 38300, 200))

    def test_switched_off(self):
        os.environ["PRICE_ALERTS"] = "0"
        try:
            u = mk_user()
            self.assertFalse(cl(u).get("/api/alerts").json()["available"])
            self.assertEqual(mk(u).status_code, 403)
        finally:
            del os.environ["PRICE_ALERTS"]


class Checker(unittest.TestCase):
    def setUp(self):
        with closing(PA.db()) as c:                       # other tests' alerts must not take part
            c.execute("DELETE FROM price_alerts")
            c.commit()
        self.sent = []
        self._send = push._send
        push._send = lambda sub, body: self.sent.append((sub, body)) or True

    def tearDown(self):
        push._send = self._send

    def run_check(self, data, now=None):
        return asyncio.run(PA.check_once(data, now))

    def sub(self, uid):
        with closing(push.db()) as c:
            c.execute("INSERT INTO push_subs(user_id,endpoint,p256dh,auth,created_at) VALUES(?,?,?,?,?)", (uid, f"https://x/{uid}", "k", "a", int(time.time())))
            c.commit()

    def test_fires_once_when_the_zone_is_reached(self):
        u = mk_user()
        self.sub(u)
        mk(u, 38500.0, note="look at a sell")
        code = PA.INSTRUMENTS["v75"][0]
        self.assertEqual(self.run_check({code: m1(38250, 38000)}), 0)           # not yet: trigger is 38300
        self.assertEqual(self.run_check({code: m1(38310, 38200)}), 1)
        self.assertEqual(self.run_check({code: m1(38400, 38200)}), 0)           # never twice
        a = rows(u)[0]
        self.assertEqual((a["status"], a["delivered"]), ("fired", 1))
        self.assertEqual(len(self.sent), 1)
        body = self.sent[0][1]
        self.assertIn("close to 38,500", body["body"])
        self.assertIn("look at a sell", body["body"])
        self.assertEqual(body["url"], "/alerts.html")

    def test_falling_alert_and_a_wick_between_checks(self):
        u = mk_user()
        mk(u, 37500.0)
        code = PA.INSTRUMENTS["v75"][0]
        self.assertEqual(self.run_check({code: {"M1": [{"epoch": int(time.time()), "open": 38000, "high": 38000, "low": 37650, "close": 37900}]}}), 1)

    def test_old_candles_do_not_fire(self):
        u = mk_user()
        mk(u, 38500.0)
        code = PA.INSTRUMENTS["v75"][0]
        self.assertEqual(self.run_check({code: m1(38400, 38300, epoch=int(time.time()) - 3600)}), 0)

    def test_no_push_device_goes_to_the_inbox(self):
        u = mk_user()
        mk(u, 38500.0)
        code = PA.INSTRUMENTS["v75"][0]
        self.assertEqual(self.run_check({code: m1(38400, 38300)}), 1)
        self.assertEqual(rows(u)[0]["delivered"], 0)
        self.assertTrue(any("Price alert: Volatility 75" in m for m in inbox_msgs(u)))

    def test_expiry(self):
        u = mk_user()
        mk(u, 38500.0, days=1)
        self.run_check({}, now=int(time.time()) + 2 * 86400)
        self.assertEqual(rows(u)[0]["status"], "expired")


class RealMarket(unittest.TestCase):
    def test_price_and_range_from_candles(self):
        key = PA.INSTRUMENTS["v75"][0]
        m = asyncio.run(REAL_MARKET("v75", {key: {"M15": candles15()}}))
        self.assertEqual(m["price"], PRICE)
        self.assertAlmostEqual(m["range"], RANGE)
        with self.assertRaises(HTTPException):
            asyncio.run(REAL_MARKET("v75", {key: {"M15": candles15()[:5]}}))


if __name__ == "__main__":
    unittest.main(verbosity=1)
