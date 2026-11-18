"""Tests for the journal's account scope and date range. Run: python3 tests/test_journal_scope.py (temp SQLite, no network)."""
import sys
import time
import unittest
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_copy_trading as T                                          # noqa: E402
from test_copy_trading import add, mk_user, q, ea                      # noqa: E402
from app import auth, journal                                          # noqa: E402
from starlette.applications import Starlette                           # noqa: E402
from starlette.testclient import TestClient                            # noqa: E402

APP = Starlette(routes=[*ea.routes, *journal.routes], exception_handlers={})
ANON = TestClient(APP)
_cl = {}


def cl(uid):
    if uid not in _cl:
        c = TestClient(APP)
        c.cookies.set(auth.COOKIE, auth.make_token(uid, "h"))
        _cl[uid] = c
    return _cl[uid]


def deal(i, closed, profit=1.0, sym="Volatility 50 Index"):
    return {"id": i, "symbol": sym, "type": "BUY", "open": 100.0, "close": 101.0, "volume": 1.0, "profit": profit, "sl": 99.0, "tp": 102.0,
            "opened": closed - 600, "closed": closed, "reason": "tp"}


def report(key, login, closed=(), balance=100.0):
    ea._last_report.clear()
    acc = {"login": login, "server": "Deriv-Server", "currency": "USD", "balance": balance, "equity": balance, "demo": False, "server_offset": 0}
    r = ANON.post("/api/ea/report", json={"account": acc, "today": {}, "positions": [], "orders": [], "specs": [], "closed": list(closed)},
                  headers={"x-driverline-key": key})
    assert r.status_code == 200, r.text
    return r


def day(ts):
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d")


class Scope(unittest.TestCase):
    def setUp(self):
        self.u = mk_user()
        self.a, self.k = add(self.u, "Main")
        self.now = int(time.time())

    def get(self, path="", **p):
        return cl(self.u).get("/api/journal" + path, params=p)

    def start(self, login, ago):
        q("UPDATE mt5_sessions SET started_at=? WHERE ea_account_id=? AND login=?", self.now - ago, self.a, login)

    def test_only_the_connected_login_and_only_from_when_it_connected(self):
        report(self.k, "111", [deal(1, self.now - 5000), deal(2, self.now - 100)])
        self.assertEqual(q("SELECT COUNT(*) AS c FROM trades WHERE user_id=?", self.u)[0]["c"], 2)    # stored in full, never deleted
        self.assertEqual(self.get().json(), [])                                                          # it connected just now: both deals are from before
        self.start("111", 1000)                                                                          # connected 1000 s ago: the older deal is still before it
        ids = [t["mt5_id"] for t in self.get().json()]
        self.assertEqual(ids, ["111:2"])
        self.assertEqual(self.get("/stats").json()["overall"]["trades"], 1)

    def test_switching_the_login_on_the_terminal_starts_a_new_session_and_records_do_not_mix(self):
        report(self.k, "111", [deal(1, self.now - 50)])
        self.start("111", 1000)
        q("UPDATE ea_accounts SET last_seen=? WHERE id=?", self.now - 600, self.a)
        q("UPDATE mt5_sessions SET last_seen=? WHERE ea_account_id=?", self.now - 600, self.a)
        report(self.k, "222", [deal(7, self.now - 5, profit=-2.0)])
        self.assertEqual(len(q("SELECT * FROM mt5_sessions WHERE ea_account_id=?", self.a)), 2)
        self.start("222", 100)
        cur = [t["mt5_id"] for t in self.get().json()]
        self.assertEqual(cur, ["222:7"])                                                                # default = the connected login
        accts = self.get("/accounts").json()["accounts"]
        self.assertEqual([(x["login"], x["current"], x["connected"]) for x in accts], [("222", True, True), ("111", False, False)])
        self.assertEqual([t["mt5_id"] for t in self.get(account="111").json()], ["111:1"])              # the earlier login stays viewable
        self.assertEqual(self.get("/stats", account="222").json()["overall"]["total_pnl"], -2.0)

    def test_manual_trades_are_their_own_view_and_the_default_without_mt5(self):
        c = cl(self.u)
        self.assertEqual(c.post("/api/journal", json={"instrument": "v50", "direction": "BUY", "entry": 1.0}).status_code, 200)
        self.assertEqual(len(self.get().json()), 1)                                                      # no MT5 connected: manual journal
        report(self.k, "111", [])
        self.assertEqual(self.get().json(), [])                                                          # connected login: its own empty record
        self.assertEqual(len(self.get(account="manual").json()), 1)

    def test_date_range_filters_and_validation(self):
        report(self.k, "111", [deal(1, self.now - 3 * 86400), deal(2, self.now - 100)])
        self.start("111", 10 * 86400)
        self.assertEqual(len(self.get().json()), 2)
        t0 = day(self.now) + "T00:00:00+00:00"
        self.assertEqual([t["mt5_id"] for t in self.get(**{"from": t0}).json()], ["111:2"])             # today only
        self.assertEqual([t["mt5_id"] for t in self.get(**{"to": t0}).json()], ["111:1"])               # before today
        self.assertEqual(self.get(**{"from": day(self.now - 5 * 86400), "to": day(self.now - 2 * 86400)}).json()[0]["mt5_id"], "111:1")
        self.assertEqual(self.get(**{"from": "yesterday"}).status_code, 422)
        self.assertEqual(self.get(account="1;DROP").status_code, 422)

    def test_other_members_records_are_never_visible(self):
        report(self.k, "111", [deal(1, self.now - 50)])
        self.start("111", 1000)
        other = mk_user()
        self.assertEqual(cl(other).get("/api/journal", params={"account": "111"}).json(), [])
        self.assertEqual(cl(other).get("/api/journal/accounts").json()["accounts"], [])


if __name__ == "__main__":
    unittest.main()
