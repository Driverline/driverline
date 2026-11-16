"""Tests for the wallet (app/wallet.py): top-ups through the existing order flow, paying from the balance, safety, and the admin tools.
Run: python3 tests/test_wallet.py   (own process, temp SQLite database, no network)."""
import os
import sys
import tempfile
import threading
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

from app import auth, dbx                                          # noqa: E402

dbx.SQLITE_PATH = Path(tempfile.mkdtemp()) / "t.db"
from starlette.testclient import TestClient                        # noqa: E402
from app import credits, main, rewards, wallet                     # noqa: E402

_seq = [0]


def mk(tier="free"):
    _seq[0] += 1
    with closing(auth.db()) as con:
        uid = con.insert("INSERT INTO users(email,pw_hash,tier,created_at) VALUES(?,?,?,?)", (f"w{_seq[0]}@t.example", "h", tier, "2026-01-01T00:00:00"))
        con.commit()
    c = TestClient(main.app)
    c.cookies.set(auth.COOKIE, auth.make_token(uid, "h"))
    return uid, c


def sql(q, *a):
    with closing(credits.db()) as con:
        r = con.execute(q, a)
        rows = r.fetchall() if q.lstrip().upper().startswith("SELECT") else None
        con.commit()
        return rows


def setting(k, v):
    sql("INSERT INTO billing_settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", k, v)
    credits._cache["ts"] = 0


def wallet_cents(uid):
    r = sql("SELECT balance_cents FROM wallet_balances WHERE user_id=?", uid)
    return r[0]["balance_cents"] if r else 0


def fund(uid, usd):
    with closing(credits.db()) as con:
        wallet.credit(con, uid, int(usd * 100), "adjust", "t" + os.urandom(4).hex(), "test")
        con.commit()


class W(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.aid, cls.a = mk("admin")
        setting("enabled", "1")
        setting("pay_trc20", "TXaddress")

    def setUp(self):
        setting("wallet_mode", "public")
        self.uid, self.c = mk("free")

    def topup(self, c, product="wallet25"):
        o = c.post("/api/billing/order", json={"product": product, "method": "trc20", "accepted": True})
        self.assertEqual(o.status_code, 200, o.text)
        oid = o.json()["order"]["id"]
        self.assertEqual(c.post(f"/api/billing/order/{oid}/paid", json={"txid": "abcdef123456"}).status_code, 200)
        return oid

    # ---- off by default and gated
    def test_nothing_is_visible_or_orderable_while_the_wallet_is_off(self):
        setting("wallet_mode", "off")
        self.assertEqual(self.c.get("/api/wallet").json()["enabled"], False)
        self.assertEqual(self.c.get("/api/billing").json()["wallet"], {"enabled": False})
        r = self.c.post("/api/billing/order", json={"product": "wallet25", "method": "trc20", "accepted": True})
        self.assertEqual(r.status_code, 409)
        r = self.c.post("/api/pay/naira/order", json={"product": "wallet25", "accepted": True})
        self.assertEqual(r.status_code, 409)
        self.assertNotEqual(self.c.post("/api/pay/crypto/order", json={"product": "wallet25", "accepted": True}).status_code, 200)
        self.assertEqual(self.c.post("/api/wallet/pay", json={"product": "copy", "accepted": True}).status_code, 409)

    def test_admin_pilot_only_the_admin_sees_it(self):
        setting("wallet_mode", "admin")
        self.assertEqual(self.c.get("/api/wallet").json()["enabled"], False)
        self.assertEqual(self.a.get("/api/wallet").json()["enabled"], True)
        self.assertEqual(self.c.post("/api/billing/order", json={"product": "wallet25", "method": "trc20", "accepted": True}).status_code, 409)

    def test_money_already_held_stays_visible_when_the_wallet_is_switched_off(self):
        fund(self.uid, 12)
        setting("wallet_mode", "off")
        d = self.c.get("/api/wallet").json()
        self.assertEqual((d["enabled"], d["balance_usd"]), (True, 12.0))
        self.assertEqual(self.c.get("/api/billing").json()["wallet"]["can_add"], False)

    # ---- top-up
    def test_a_top_up_goes_through_the_normal_order_and_credits_once(self):
        oid = self.topup(self.c)
        self.assertEqual(wallet_cents(self.uid), 0)                                    # nothing until the order is approved
        self.assertEqual(self.a.post(f"/api/admin/billing/order/{oid}/approve").status_code, 200)
        self.assertEqual(wallet_cents(self.uid), 2500)
        self.assertEqual(self.c.get("/api/wallet").json()["balance_usd"], 25.0)
        self.assertEqual(credits.balance(self.uid), 0)                                 # no signals came with it
        self.assertEqual(self.a.post(f"/api/admin/billing/order/{oid}/approve").status_code, 409)
        self.assertEqual(wallet_cents(self.uid), 2500)
        with closing(credits.db()) as con:
            self.assertFalse(wallet.credit(con, self.uid, 2500, "topup", f"order:{oid}", "again"))   # the ledger refuses a repeat
        self.assertEqual(wallet_cents(self.uid), 2500)

    def test_the_shop_list_does_not_contain_wallet_products(self):
        d = self.c.get("/api/billing").json()
        self.assertFalse([p for p in d["catalog"] if p["key"].startswith("wallet")])
        self.assertEqual([t["key"] for t in d["wallet"]["topups"]], list(credits.WALLET_KEYS))

    # ---- paying from the wallet
    def test_paying_for_copy_trading_from_the_wallet(self):
        fund(self.uid, 40)
        r = self.c.post("/api/wallet/pay", json={"product": "copy", "accepted": True})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["balance_usd"], 25.0)
        self.assertEqual(wallet_cents(self.uid), 2500)
        self.assertEqual(len(sql("SELECT 1 FROM billing_subs WHERE user_id=? AND plan='copy' AND ends_at>strftime('%s','now')", self.uid)), 1)
        o = sql("SELECT method, status, amount_usd FROM billing_orders WHERE user_id=? AND product='copy'", self.uid)
        self.assertEqual((o[0]["method"], o[0]["status"], o[0]["amount_usd"]), ("wallet", "approved", 15.0))
        led = sql("SELECT delta_cents, kind FROM wallet_ledger WHERE user_id=? AND kind='purchase'", self.uid)
        self.assertEqual([(x["delta_cents"], x["kind"]) for x in led], [(-1500, "purchase")])
        self.assertTrue(any(x["label"] == "Purchase" for x in self.c.get("/api/billing").json()["wallet"]["ledger"]))

    def test_not_enough_money_changes_nothing(self):
        fund(self.uid, 10)
        r = self.c.post("/api/wallet/pay", json={"product": "copy", "accepted": True})
        self.assertEqual(r.status_code, 402)
        self.assertEqual(wallet_cents(self.uid), 1000)
        self.assertEqual(sql("SELECT 1 FROM billing_orders WHERE user_id=? AND product='copy'", self.uid), [])
        self.assertEqual(sql("SELECT 1 FROM billing_subs WHERE user_id=?", self.uid), [])

    def test_a_failure_in_the_middle_rolls_the_whole_payment_back(self):
        fund(self.uid, 40)
        real = credits.grant_product
        credits.grant_product = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
        try:
            r = self.c.post("/api/wallet/pay", json={"product": "copy", "accepted": True})
        finally:
            credits.grant_product = real
        self.assertEqual(r.status_code, 500)
        self.assertEqual(wallet_cents(self.uid), 4000)
        self.assertEqual(sql("SELECT 1 FROM billing_orders WHERE user_id=? AND product='copy'", self.uid), [])
        self.assertEqual(sql("SELECT 1 FROM wallet_ledger WHERE user_id=? AND kind='purchase'", self.uid), [])

    def test_a_double_tap_pays_once(self):
        fund(self.uid, 100)
        self.assertEqual(self.c.post("/api/wallet/pay", json={"product": "copy", "accepted": True}).status_code, 200)
        self.assertEqual(self.c.post("/api/wallet/pay", json={"product": "copy", "accepted": True}).status_code, 429)
        self.assertEqual(wallet_cents(self.uid), 8500)

    def test_parallel_payments_can_never_overspend(self):
        fund(self.uid, 20)                                 # enough for one signals pack ($10) but the two requests are for different packs
        out = []

        def go(prod):
            out.append(self.c.post("/api/wallet/pay", json={"product": prod, "accepted": True}).status_code)
        ts = [threading.Thread(target=go, args=(p,)) for p in ("boost", "plus")]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertGreaterEqual(wallet_cents(self.uid), 0)
        paid = sum(-r["delta_cents"] for r in sql("SELECT delta_cents FROM wallet_ledger WHERE user_id=? AND kind='purchase'", self.uid))
        self.assertEqual(wallet_cents(self.uid), 2000 - paid)

    def test_rules(self):
        fund(self.uid, 100)
        self.assertEqual(self.c.post("/api/wallet/pay", json={"product": "copy"}).status_code, 422)                       # terms
        self.assertEqual(self.c.post("/api/wallet/pay", json={"product": "wallet25", "accepted": True}).status_code, 422)   # not wallet for wallet
        self.assertEqual(self.c.post("/api/wallet/pay", json={"product": "nope", "accepted": True}).status_code, 422)
        self.assertEqual(TestClient(main.app).post("/api/wallet/pay", json={"product": "copy", "accepted": True}).status_code, 401)
        setting("enabled", "0")
        try:
            self.assertEqual(self.c.post("/api/wallet/pay", json={"product": "copy", "accepted": True}).status_code, 409)
        finally:
            setting("enabled", "1")
        self.assertEqual(wallet_cents(self.uid), 10000)

    # ---- rewards count the purchase, not the top-up
    def test_referral_points_count_the_spend_once_not_the_top_up(self):
        uid, c = mk("free")
        oid = self.topup(c, "wallet100")
        self.a.post(f"/api/admin/billing/order/{oid}/approve")
        self.assertEqual(c.post("/api/wallet/pay", json={"product": "copy", "accepted": True}).status_code, 200)
        rewards._orders(uid, 0)
        with closing(rewards.db()) as con:
            rows = [r["ref"] for r in con.execute("SELECT ref FROM points_ledger WHERE user_id=? AND kind='purchase'", (uid,)).fetchall()]
        wo = sql("SELECT id FROM billing_orders WHERE user_id=? AND product='copy'", uid)[0]["id"]
        self.assertEqual(rows, [f"order:{wo}"])

    # ---- admin
    def test_admin_adjust_and_limits(self):
        email = f"w{self.uid}@t.example"
        r = self.a.post("/api/admin/wallet/adjust", json={"email": email, "amount_usd": 30, "note": "refund order TL-X"})
        self.assertEqual((r.status_code, r.json()["balance_usd"]), (200, 30.0))
        self.assertEqual(self.a.post("/api/admin/wallet/adjust", json={"email": email, "amount_usd": -50, "note": "too much"}).status_code, 409)
        self.assertEqual(wallet_cents(self.uid), 3000)
        self.assertEqual(self.a.post("/api/admin/wallet/adjust", json={"email": email, "amount_usd": -10, "note": "correction"}).json()["balance_usd"], 20.0)
        for bad in ({"amount_usd": 0, "note": "xxx"}, {"amount_usd": 99999, "note": "xxx"}, {"amount_usd": 5, "note": ""}, {"amount_usd": "a", "note": "xxx"}):
            self.assertEqual(self.a.post("/api/admin/wallet/adjust", json={"email": email, **bad}).status_code, 422)
        self.assertEqual(self.a.post("/api/admin/wallet/adjust", json={"email": "no@one.example", "amount_usd": 5, "note": "xxx"}).status_code, 404)

    def test_members_cannot_use_the_admin_tools(self):
        for path, body in (("/api/admin/wallet/adjust", {"email": "x", "amount_usd": 5, "note": "xxx"}), ("/api/admin/wallet/mode", {"mode": "public"})):
            self.assertEqual(self.c.post(path, json=body).status_code, 403)
        self.assertEqual(self.c.get("/api/admin/wallet").status_code, 403)

    def test_admin_mode_switch_and_totals(self):
        fund(self.uid, 7)
        self.assertEqual(self.a.post("/api/admin/wallet/mode", json={"mode": "bogus"}).status_code, 422)
        self.assertEqual(self.a.post("/api/admin/wallet/mode", json={"mode": "off"}).status_code, 200)
        d = self.a.get("/api/admin/wallet").json()
        self.assertEqual(d["mode"], "off")
        self.assertGreaterEqual(d["held_usd"], 7)

    def test_the_balance_cannot_go_negative_in_the_database(self):
        with closing(credits.db()) as con:
            with self.assertRaises(Exception):
                con.execute("INSERT INTO wallet_balances(user_id,balance_cents) VALUES(?,?)", (987654, -1))
            con.raw.rollback()


if __name__ == "__main__":
    unittest.main()
