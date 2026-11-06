"""Tests for prizes: catalogue, redeeming with points, stock, shipping and refusals (app/prizes.py).
Run: python3 tests/test_prizes.py   (own process, temp SQLite database, no network)."""

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
os.environ.update(DRIVERLINE_SECRET="s" * 40)
os.environ.pop("DATABASE_URL", None)

from app import auth, commission, dbx, inbox, partner, prizes, rewards   # noqa: E402

dbx.SQLITE_PATH = Path(tempfile.mkdtemp()) / "t.db"
from starlette.testclient import TestClient   # noqa: E402
from app import main                          # noqa: E402

_seq = [0]


def mk_user(tier="free"):
    _seq[0] += 1
    with closing(auth.db()) as con:
        uid = con.insert("INSERT INTO users(email,pw_hash,tier,created_at,blocked) VALUES(?,?,?,?,0)",
                         (f"member{_seq[0]}@t.example", "h", tier, "2026-01-01T00:00:00"))
        con.commit()
    return uid


def client(uid):
    c = TestClient(main.app)
    c.cookies.set(auth.COOKIE, auth.make_token(uid, "h"))
    return c



def wipe():
    with closing(rewards.db()) as con:
        for n in ("prizes", "redemptions", "points_ledger", "points_balances"):
            con.execute(f"DELETE FROM {n}")
        con.commit()
    with closing(inbox.db()) as con:
        con.execute("DELETE FROM inbox_messages")
        con.commit()


def earn(uid, pts):
    with closing(rewards.db()) as con:
        rewards.award(con, uid, "adjust", f"t{time.time_ns()}", pts, "test")


DELIVERY = {"full_name": "Ada Obi", "phone": "+234 801 234 5678", "address": "12 Marina Road, Lagos Island, Lagos"}


class Prizes(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ["REWARDS_ON"] = "1"
        os.environ["REWARDS_PRIZES_ON"] = "1"

    @classmethod
    def tearDownClass(cls):
        os.environ.pop("REWARDS_PRIZES_ON", None)

    def setUp(self):
        wipe()
        self.admin, self.u, self.v = mk_user("admin"), mk_user(), mk_user()
        self.ca, self.cu, self.cv = client(self.admin), client(self.u), client(self.v)
        self.items = {p["name"]: p for p in self.ca.get("/api/admin/prizes").json()["prizes"]}      # first look creates the starting catalogue

    def stock(self, name, n):
        r = self.ca.post("/api/admin/prizes/save", json={"id": self.items[name]["id"], "stock": n})
        self.assertEqual(r.status_code, 200)

    def pid(self, name):
        return self.items[name]["id"]

    def test_the_six_starting_prizes_use_100_points_per_dollar_and_start_out_of_stock(self):
        got = {n: (p["points"], p["cost_usd"], p["stock"]) for n, p in self.items.items()}
        self.assertEqual(got, {"TradeLens pen": (500, 5, 0), "Trading journal and pen": (1000, 10, 0), "Powerbank": (5000, 50, 0),
                               "Tablet": (11000, 110, 0), "Phone": (25000, 250, 0), "Laptop": (50000, 500, 0)})
        earn(self.u, 600)
        r = self.cu.post("/api/rewards/redeem", json={"prize_id": self.pid("TradeLens pen"), **DELIVERY})
        self.assertEqual(r.status_code, 409)
        v = self.cu.get("/api/rewards").json()["prizes"]
        self.assertTrue(v["on"])
        self.assertFalse(v["items"][0]["available"])
        self.assertNotIn("cost", json.dumps(v))                                                   # what a prize costs the owner is private

    def test_redeeming_spends_points_not_the_level_and_takes_one_from_stock(self):
        self.stock("TradeLens pen", 2)
        earn(self.u, 1200)
        r = self.cu.post("/api/rewards/redeem", json={"prize_id": self.pid("TradeLens pen"), **DELIVERY})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(rewards.balances(self.u), {"spendable": 700, "lifetime": 1200})
        self.assertEqual(self.cu.get("/api/rewards").json()["level"]["name"], "Silver")           # still Silver
        self.assertEqual(self.ca.get("/api/admin/prizes").json()["prizes"][0]["stock"], 1)
        self.assertEqual(self.cu.post("/api/rewards/redeem", json={"prize_id": self.pid("TradeLens pen"), **DELIVERY}).status_code, 409)   # one open request at a time

    def test_not_enough_points_changes_nothing(self):
        self.stock("Powerbank", 1)
        earn(self.u, 4999)
        r = self.cu.post("/api/rewards/redeem", json={"prize_id": self.pid("Powerbank"), **DELIVERY})
        self.assertEqual(r.status_code, 409)
        self.assertEqual(rewards.balances(self.u)["spendable"], 4999)
        self.assertEqual([p for p in self.ca.get("/api/admin/prizes").json()["prizes"] if p["name"] == "Powerbank"][0]["stock"], 1)
        self.assertEqual(self.ca.get("/api/admin/prizes").json()["requests"], [])

    def test_the_last_one_cannot_be_taken_twice(self):
        self.stock("TradeLens pen", 1)
        earn(self.u, 600)
        earn(self.v, 600)
        self.assertEqual(self.cu.post("/api/rewards/redeem", json={"prize_id": self.pid("TradeLens pen"), **DELIVERY}).status_code, 200)
        self.assertEqual(self.cv.post("/api/rewards/redeem", json={"prize_id": self.pid("TradeLens pen"), **DELIVERY}).status_code, 409)
        self.assertEqual(rewards.balances(self.v)["spendable"], 600)

    def test_cancelling_gives_back_the_points_and_the_stock_once(self):
        self.stock("TradeLens pen", 1)
        earn(self.u, 600)
        rid = self.cu.post("/api/rewards/redeem", json={"prize_id": self.pid("TradeLens pen"), **DELIVERY}).json()["id"]
        self.assertEqual(self.cv.post(f"/api/rewards/redeem/{rid}/cancel").status_code, 404)           # not someone else's
        self.assertEqual(self.cu.post(f"/api/rewards/redeem/{rid}/cancel").status_code, 200)
        self.assertEqual(self.cu.post(f"/api/rewards/redeem/{rid}/cancel").status_code, 409)
        self.assertEqual(rewards.balances(self.u), {"spendable": 600, "lifetime": 600})
        self.assertEqual(self.ca.get("/api/admin/prizes").json()["prizes"][0]["stock"], 1)

    def test_admin_ships_or_rejects_once_and_the_member_is_told(self):
        self.stock("TradeLens pen", 3)
        earn(self.u, 600)
        rid = self.cu.post("/api/rewards/redeem", json={"prize_id": self.pid("TradeLens pen"), **DELIVERY}).json()["id"]
        row = self.ca.get("/api/admin/prizes").json()["requests"][0]
        self.assertEqual((row["full_name"], row["address"]), (DELIVERY["full_name"], DELIVERY["address"]))
        self.assertEqual(self.cu.post("/api/admin/prizes/decide", json={"id": rid, "action": "shipped"}).status_code, 403)
        self.assertEqual(self.ca.post("/api/admin/prizes/decide", json={"id": rid, "action": "reject", "note": ""}).status_code, 422)
        self.assertEqual(self.ca.post("/api/admin/prizes/decide", json={"id": rid, "action": "shipped", "note": "GIG tracking 123"}).status_code, 200)
        self.assertEqual(self.ca.post("/api/admin/prizes/decide", json={"id": rid, "action": "reject", "note": "too late"}).status_code, 409)
        self.assertEqual(self.cu.post(f"/api/rewards/redeem/{rid}/cancel").status_code, 409)
        self.assertEqual(rewards.balances(self.u)["spendable"], 100)                                   # shipped: points stay spent
        with closing(inbox.db()) as con:
            self.assertIn("GIG tracking 123", con.execute("SELECT body FROM inbox_messages WHERE user_id=?", (self.u,)).fetchone()["body"])
        earn(self.u, 500)
        rid2 = self.cu.post("/api/rewards/redeem", json={"prize_id": self.pid("TradeLens pen"), **DELIVERY}).json()["id"]
        self.assertEqual(self.ca.post("/api/admin/prizes/decide", json={"id": rid2, "action": "reject", "note": "Out of the item"}).status_code, 200)
        self.assertEqual(rewards.balances(self.u)["spendable"], 600)                                   # rejected: points came back

    def test_delivery_details_are_checked(self):
        self.stock("TradeLens pen", 5)
        earn(self.u, 600)
        for bad in ({"full_name": "A"}, {"phone": "abc"}, {"address": "short"}, {"full_name": ""}):
            r = self.cu.post("/api/rewards/redeem", json={"prize_id": self.pid("TradeLens pen"), **{**DELIVERY, **bad}})
            self.assertEqual(r.status_code, 422, bad)
        self.assertEqual(self.cu.post("/api/rewards/redeem", json={"prize_id": "x", **DELIVERY}).status_code, 422)
        self.assertEqual(self.cu.post("/api/rewards/redeem", json={"prize_id": 99999, **DELIVERY}).status_code, 404)
        self.assertEqual(rewards.balances(self.u)["spendable"], 600)

    def test_unlimited_stock_inactive_prizes_and_new_prizes(self):
        earn(self.u, 600)
        self.ca.post("/api/admin/prizes/save", json={"id": self.pid("TradeLens pen"), "stock": ""})
        self.assertEqual(self.cu.post("/api/rewards/redeem", json={"prize_id": self.pid("TradeLens pen"), **DELIVERY}).status_code, 200)
        self.assertIsNone(self.ca.get("/api/admin/prizes").json()["prizes"][0]["stock"])
        self.ca.post("/api/admin/prizes/save", json={"id": self.pid("Powerbank"), "active": False})
        names = [i["name"] for i in self.cv.get("/api/rewards").json()["prizes"]["items"]]
        self.assertNotIn("Powerbank", names)
        r = self.ca.post("/api/admin/prizes/save", json={"name": "Cap", "points": 300, "stock": 10, "cost_usd": 3})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.ca.post("/api/admin/prizes/save", json={"name": "Bad"}).status_code, 422)
        self.assertEqual(self.ca.post("/api/admin/prizes/save", json={"id": self.pid("Tablet"), "points": 0}).status_code, 422)
        self.assertEqual(self.ca.post("/api/admin/prizes/save", json={"id": self.pid("Tablet"), "stock": -1}).status_code, 422)

    def test_prizes_are_off_by_default_and_admin_only(self):
        os.environ.pop("REWARDS_PRIZES_ON")
        try:
            self.assertEqual(self.cu.get("/api/rewards").json()["prizes"], {"on": False})
            self.assertEqual(self.cu.post("/api/rewards/redeem", json={"prize_id": 1, **DELIVERY}).status_code, 404)
        finally:
            os.environ["REWARDS_PRIZES_ON"] = "1"
        self.assertEqual(self.cu.get("/api/admin/prizes").status_code, 403)
        self.assertEqual(self.cu.post("/api/admin/prizes/save", json={"id": 1, "stock": 5}).status_code, 403)

    def test_the_leaderboard_ignores_spending(self):
        self.stock("TradeLens pen", 1)
        with closing(rewards.db()) as con:
            rewards.award(con, self.u, "purchase", "o1", 600, "x")
        before = self.cu.get("/api/rewards").json()["all"]["my_points"]          # 600 plus today's visit
        self.cu.post("/api/rewards/redeem", json={"prize_id": self.pid("TradeLens pen"), **DELIVERY})
        self.assertEqual(self.cu.get("/api/rewards").json()["all"]["my_points"], before)


if __name__ == "__main__":
    unittest.main(verbosity=2)
