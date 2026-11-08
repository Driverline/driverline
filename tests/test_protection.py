"""Tests for equity protection settings and events (app/ea.py).
Run: python3 tests/test_protection.py   (own process, temp SQLite database, no network, no broker)."""
import os
import sys
import tempfile
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

from app import auth, credits, dbx, ea, inbox   # noqa: E402
from starlette.applications import Starlette     # noqa: E402
from starlette.exceptions import HTTPException   # noqa: E402
from starlette.responses import JSONResponse     # noqa: E402
from starlette.testclient import TestClient      # noqa: E402

dbx.SQLITE_PATH = Path(tempfile.mkdtemp()) / "t.db"


async def _err(request, exc):
    return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)


APP = Starlette(routes=[*ea.routes, *credits.routes], exception_handlers={HTTPException: _err})
ANON = TestClient(APP)
ANON.__enter__()
_seq = [0]
DAY = 24 * 3600
CLOCK = [1_800_000_000]
ea._now = lambda: CLOCK[0]


def setup_member():
    _seq[0] += 1
    with closing(auth.db()) as con:
        uid = con.insert("INSERT INTO users(email,pw_hash,tier,created_at) VALUES(?,?,?,?)", (f"p{_seq[0]}@t.example", "h", "member", "2026-01-01T00:00:00"))
        con.commit()
    c = TestClient(APP)
    c.__enter__()
    c.cookies.set(auth.COOKIE, auth.make_token(uid, "h"))
    r = c.post("/api/ea/accounts", json={"label": "Main"})
    return uid, c, r.json()["id"], r.json()["key"]


def report(key, events=None, protect_allowed=True, login="1001"):
    ea._last_report.clear()
    body = {"account": {"login": login, "server": "Deriv-Demo", "currency": "USD", "balance": 1000.0, "equity": 1000.0, "demo": True,
                        "manage_allowed": True, "protect_allowed": protect_allowed}, "today": {}, "positions": [], "orders": []}
    if events is not None:
        body["protect_events"] = events
    return ANON.post("/api/ea/report", json=body, headers={"x-driverline-key": key})


def mg(c, a):
    return c.get(f"/api/ea/status?account={a}").json()["manage"]


def save(c, a, **kw):
    return c.post("/api/ea/manage", json={"account": a, **kw})


class Settings(unittest.TestCase):
    def setUp(self):
        self.u, self.c, self.a, self.k = setup_member()
        report(self.k)

    def test_off_by_default_with_the_agreed_numbers(self):
        m = mg(self.c, self.a)
        self.assertEqual((m["prot_mode"], m["prot_risk_pct"], m["prot_cap_pct"], m["prot_grace"]), ("off", 4.5, 5.0, 45))

    def test_existing_auto_manage_is_untouched(self):
        self.assertEqual(save(self.c, self.a, be_on=True).status_code, 200)
        m = mg(self.c, self.a)
        self.assertTrue(m["be_on"])
        self.assertEqual(m["prot_mode"], "off")

    def test_status_reports_whether_the_ea_allows_protection(self):
        self.assertTrue(self.c.get(f"/api/ea/status?account={self.a}").json()["can_protect"])
        report(self.k, protect_allowed=False)
        self.assertFalse(self.c.get(f"/api/ea/status?account={self.a}").json()["can_protect"])

    def test_turning_it_on_or_making_it_stricter_is_immediate(self):
        self.assertEqual(save(self.c, self.a, prot_mode="stop").status_code, 200)
        self.assertEqual(mg(self.c, self.a)["prot_mode"], "stop")
        save(self.c, self.a, prot_mode="close", prot_risk_pct=1.0, prot_cap_pct=3.0, prot_grace=30)
        m = mg(self.c, self.a)
        self.assertEqual((m["prot_mode"], m["prot_risk_pct"], m["prot_cap_pct"], m["prot_grace"]), ("close", 1.0, 3.0, 30))
        self.assertEqual(m["prot_pending"], {})

    def test_making_it_weaker_waits_24_hours(self):
        save(self.c, self.a, prot_mode="stop")
        t0 = CLOCK[0]
        r = save(self.c, self.a, prot_mode="off")
        self.assertEqual(r.json()["prot_mode"], "stop")                       # still on
        self.assertEqual(mg(self.c, self.a)["prot_pending"]["prot_mode"]["at"], t0 + DAY)
        CLOCK[0] = t0 + DAY - 5
        self.assertEqual(mg(self.c, self.a)["prot_mode"], "stop")
        CLOCK[0] = t0 + DAY + 1
        m = mg(self.c, self.a)
        self.assertEqual((m["prot_mode"], m["prot_pending"]), ("off", {}))

    def test_a_bigger_risk_or_cap_or_wait_also_waits(self):
        save(self.c, self.a, prot_mode="stop")
        save(self.c, self.a, prot_risk_pct=4.9, prot_cap_pct=9.0, prot_grace=120)
        m = mg(self.c, self.a)
        self.assertEqual((m["prot_risk_pct"], m["prot_cap_pct"], m["prot_grace"]), (4.5, 5.0, 45))
        self.assertEqual(set(m["prot_pending"]), {"prot_risk_pct", "prot_cap_pct", "prot_grace"})

    def test_asking_for_the_current_value_again_cancels_a_pending_change(self):
        save(self.c, self.a, prot_mode="stop")
        save(self.c, self.a, prot_mode="off")
        self.assertIn("prot_mode", mg(self.c, self.a)["prot_pending"])
        save(self.c, self.a, prot_mode="stop")
        self.assertEqual(mg(self.c, self.a)["prot_pending"], {})

    def test_asking_again_does_not_extend_the_wait(self):
        save(self.c, self.a, prot_mode="stop")
        t0 = CLOCK[0]
        save(self.c, self.a, prot_mode="off")
        CLOCK[0] = t0 + 3600
        save(self.c, self.a, prot_mode="off")
        self.assertEqual(mg(self.c, self.a)["prot_pending"]["prot_mode"]["at"], t0 + DAY)

    def test_while_it_is_off_nothing_waits(self):
        save(self.c, self.a, prot_risk_pct=6.0, prot_cap_pct=12.0)
        m = mg(self.c, self.a)
        self.assertEqual((m["prot_risk_pct"], m["prot_cap_pct"], m["prot_pending"]), (6.0, 12.0, {}))

    def test_validation(self):
        for body in ({"prot_mode": "delete"}, {"prot_risk_pct": 0}, {"prot_risk_pct": 11}, {"prot_cap_pct": 30}, {"prot_grace": 5},
                     {"prot_grace": 999}, {"prot_be_pct": "x"}, {"prot_risk_pct": 4.8, "prot_cap_pct": 3.0}):
            self.assertEqual(save(self.c, self.a, **body).status_code, 422, body)

    def test_settings_are_per_account_and_per_member(self):
        other_u, other_c, other_a, other_k = setup_member()
        report(other_k)
        save(self.c, self.a, prot_mode="close")
        self.assertEqual(mg(other_c, other_a)["prot_mode"], "off")

    def test_the_ea_receives_the_settings_in_its_poll(self):
        save(self.c, self.a, prot_mode="stop", prot_risk_pct=1.5, prot_cap_pct=4.0, prot_grace=60, prot_be_pct=0.5)
        ea._last_poll.clear()
        d = ANON.post("/api/ea/poll", json={}, headers={"x-driverline-key": self.k}).json()
        for k, v in {"prot_mode": "stop", "prot_risk_pct": "1.5", "prot_cap_pct": "4.0", "prot_grace": "60", "prot_be_pct": "0.5"}.items():
            self.assertEqual(str(d.get(k)), v, k)


class Events(unittest.TestCase):
    def setUp(self):
        self.u, self.c, self.a, self.k = setup_member()

    def inbox_bodies(self):
        with closing(inbox.db()) as con:
            return [r["body"] for r in con.execute("SELECT body FROM inbox_messages WHERE user_id=? ORDER BY id", (self.u,)).fetchall()]

    def test_an_event_is_logged_once_and_the_member_is_told(self):
        ev = {"ticket": 777, "symbol": "Volatility 75 Index", "action": "stop_set", "pct": 2.0}
        self.assertEqual(report(self.k, [ev]).status_code, 200)
        report(self.k, [ev])                                                  # the EA repeats it: no second message
        msgs = self.inbox_bodies()
        self.assertEqual(len(msgs), 1)
        self.assertIn("#777", msgs[0])
        self.assertIn("no stop loss", msgs[0])
        self.assertIn("2%", msgs[0])

    def test_unknown_actions_and_bad_tickets_are_ignored_and_never_break_the_report(self):
        r = report(self.k, [{"ticket": "abc", "action": "stop_set"}, {"ticket": 1, "action": "format_disk"}, "junk", None])
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.inbox_bodies(), [])
        self.assertEqual(report(self.k, "not a list").status_code, 200)

    def test_text_from_the_ea_cannot_inject_markup_or_long_text(self):
        report(self.k, [{"ticket": 9, "symbol": "<script>alert(1)</script>" + "A" * 80, "action": "failed", "detail": "<b>x</b>" + "y" * 500}])
        body = self.inbox_bodies()[0]
        self.assertNotIn("<", body)
        self.assertLess(len(body), 400)

    def test_the_close_and_failure_messages_say_what_happened(self):
        report(self.k, [{"ticket": 5, "symbol": "EURUSD", "action": "closed_no_stop"}, {"ticket": 6, "symbol": "EURUSD", "action": "breakeven"}])
        msgs = " ".join(self.inbox_bodies())
        self.assertIn("closed it", msgs)
        self.assertIn("does not limit the loss", msgs)

    def test_removing_the_account_removes_its_log(self):
        report(self.k, [{"ticket": 1, "symbol": "X", "action": "stop_set", "pct": 1}])
        self.assertEqual(self.c.delete(f"/api/ea/accounts/{self.a}").status_code, 200)
        with closing(ea.db()) as con:
            self.assertEqual(con.execute("SELECT COUNT(*) AS n FROM ea_protect_log WHERE account_id=?", (self.a,)).fetchone()["n"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=1)
