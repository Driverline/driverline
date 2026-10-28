"""Tests for Pay with Naira (app/naira.py): manual Moniepoint bank transfers verified by an administrator, and the billing code it shares
(app/credits.py). Run with `python tests/test_naira.py` (or `python -m pytest tests/test_naira.py`). Run it as its own process, like the other suites.

No network is involved: there is no bank API and no payment provider in this flow. Everything an administrator would check in the
Moniepoint account is simulated by the test passing the bank reference and amount to the approval endpoint."""
import asyncio
import json
import re
import shutil
import sys
import tempfile
import threading
import time
import unittest
import os
from contextlib import closing
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
try:
    import httpx  # noqa: F401
except ImportError:                       # newer Starlette ships its HTTP client as httpx2: same API
    import httpx2 as httpx
    sys.modules["httpx"] = httpx
for _name in ("websockets", "pywebpush", "psycopg", "psycopg.rows"):          # only needed by modules these tests never run
    try:
        __import__(_name)
    except ImportError:
        import types
        sys.modules[_name] = types.ModuleType(_name)

os.environ.update(DRIVERLINE_SECRET="x" * 40, DRIVERLINE_BASE_URL="https://tradelens.example", NOWPAYMENTS_API_KEY="np-key",
                  NOWPAYMENTS_IPN_SECRET="np-secret")
os.environ.pop("DATABASE_URL", None)

from app import auth, credits, dbx, naira, nowpay      # noqa: E402
from starlette.applications import Starlette          # noqa: E402
from starlette.exceptions import HTTPException        # noqa: E402
from starlette.responses import JSONResponse          # noqa: E402
from starlette.testclient import TestClient           # noqa: E402

TMP = Path(tempfile.mkdtemp())
dbx.SQLITE_PATH = TMP / "test.db"
RATE = 1500
BANK = {"bank_name": "Moniepoint MFB", "bank_account_name": "Driverline Test Ltd", "bank_account_number": "0123456789", "support": "support@test.example"}


async def _http_error(request, exc):
    return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)


APP = Starlette(routes=[*auth.routes, *credits.routes, *nowpay.routes, *naira.routes], exception_handlers={HTTPException: _http_error})
_seq = [0]
_clients = {}


def mk_user(tier="free"):
    _seq[0] += 1
    with closing(auth.db()) as con:
        uid = con.insert("INSERT INTO users(email,pw_hash,tier,created_at) VALUES(?,?,?,?)", (f"n{_seq[0]}@test.example", "hash", tier, "2026-01-01T00:00:00"))
        con.commit()
    return uid


def email_of(uid):
    return f"n{uid}@test.example" if False else sql_users(uid)


def sql_users(uid):
    with closing(auth.db()) as con:
        return con.execute("SELECT email FROM users WHERE id=?", (uid,)).fetchone()["email"]


def client(uid):
    if uid not in _clients:
        c = TestClient(APP)
        c.__enter__()
        c.cookies.set(auth.COOKIE, auth.make_token(uid, "hash"))
        _clients[uid] = c
    return _clients[uid]


ANON = TestClient(APP)
ANON.__enter__()


def row(oid):
    with closing(credits.db()) as con:
        return dict(con.execute("SELECT * FROM billing_orders WHERE id=?", (oid,)).fetchone())


def sql(q, *a):
    with closing(credits.db()) as con:
        r = con.execute(q, a).fetchall()
        con.commit()
    return [dict(x) for x in r]


def events(oid, kind=None):
    return [e for e in sql("SELECT * FROM payment_events WHERE order_id=? ORDER BY id", oid) if kind in (None, e["kind"])]


def balance(uid):
    return credits.balance(uid)


def put(**kv):
    with closing(credits.db()) as con:
        for k, v in kv.items():
            credits._put(con, k, str(v))
        con.commit()
    naira.reset_cache()
    credits._cache["ts"] = 0.0


def buy(uid, product="boost"):
    r = client(uid).post("/api/pay/naira/order", json={"product": product, "accepted": True})
    assert r.status_code == 200, r.text
    o = r.json()["order"]
    return o["id"], o


def approve(admin, oid, ref="MNP-REF-0001", amount=None, note="", accept=False, expect=200):
    amount = row(oid)["pay_amount"] if amount is None else amount
    r = client(admin).post(f"/api/admin/pay/naira/order/{oid}/approve", json={"bank_ref": ref, "amount_received": amount, "note": note, "accept_difference": accept})
    assert r.status_code == expect, (r.status_code, r.text)
    return r.json()


_ref = [0]


def fresh_ref():
    _ref[0] += 1
    return f"MNP-{_ref[0]:06d}"


def setUpModule():
    put(enabled="1", ngn_rate=RATE, naira_enabled="1", pay_trc20="TManualWallet", **BANK)


def tearDownModule():
    for c in list(_clients.values()) + [ANON]:
        c.__exit__(None, None, None)
    shutil.rmtree(TMP, ignore_errors=True)


class Base(unittest.TestCase):
    def setUp(self):
        put(enabled="1", ngn_rate=RATE, naira_enabled="1", **BANK)


# ---------------------------------------------------------------- 1. configuration, modes, who may start an order
class ConfigurationTests(Base):
    def test_off_means_nobody_missing_details_mean_nobody(self):
        u, admin = mk_user(), mk_user("admin")
        put(naira_enabled="0")
        for who in (u, admin):
            self.assertEqual(client(who).post("/api/pay/naira/order", json={"product": "boost", "accepted": True}).status_code, 409)
            self.assertEqual(client(who).get("/api/billing").json()["naira"], {"enabled": False})
        put(naira_enabled="1", bank_account_number="")
        self.assertTrue(any("account number" in p for p in naira.problems()))
        self.assertEqual(client(u).post("/api/pay/naira/order", json={"product": "boost", "accepted": True}).status_code, 409)
        put(bank_account_number=BANK["bank_account_number"], ngn_rate="")
        self.assertTrue(any("rate" in p for p in naira.problems()))
        self.assertEqual(client(u).get("/api/billing").json()["naira"], {"enabled": False})
        put(ngn_rate=RATE, bank_name="")
        self.assertTrue(any("bank name" in p for p in naira.problems()))

    def test_pilot_is_admin_only_and_works_while_billing_is_closed(self):
        u, admin = mk_user(), mk_user("admin")
        put(naira_enabled="admin", enabled="0")
        self.assertEqual(client(u).post("/api/pay/naira/order", json={"product": "boost", "accepted": True}).status_code, 409)
        self.assertEqual(client(u).get("/api/billing").json()["enabled"], False)
        info = client(admin).get("/api/billing").json()
        self.assertTrue(info["enabled"] and info["naira"]["enabled"] and info["naira"]["pilot"])
        self.assertEqual(client(admin).post("/api/pay/naira/order", json={"product": "boost", "accepted": True}).status_code, 200)

    def test_public_needs_billing_open(self):
        u = mk_user()
        put(naira_enabled="1", enabled="0")
        self.assertFalse(naira.enabled({"tier": "free"}))
        put(enabled="1")
        self.assertTrue(naira.enabled({"tier": "free"}))
        self.assertEqual(client(u).post("/api/pay/naira/order", json={"product": "boost", "accepted": True}).status_code, 200)

    def test_member_sees_server_computed_naira_prices_for_every_product(self):
        u = mk_user()
        info = client(u).get("/api/billing").json()["naira"]
        self.assertEqual(set(info["naira"]), set(credits.CATALOG))
        for k, v in credits.CATALOG.items():
            self.assertEqual(info["naira"][k], v["usd"] * RATE, k)
        self.assertEqual(info["label"], "Pay with Naira")

    def test_the_old_manual_naira_option_is_gone_and_usdt_manual_remains(self):
        u = mk_user()
        d = client(u).get("/api/billing").json()
        self.assertNotIn("ngn", d["methods"])
        self.assertEqual(set(d["methods"]), {"trc20", "bep20"})
        r = client(u).post("/api/billing/order", json={"product": "boost", "method": "ngn", "accepted": True})
        self.assertEqual(r.status_code, 422)
        self.assertEqual(sql("SELECT * FROM billing_orders WHERE user_id=?", u), [])


# ---------------------------------------------------------------- 2. creating an order
class CreateOrderTests(Base):
    def test_order_is_created_on_the_server_with_everything_the_member_needs(self):
        u = mk_user()
        r = client(u).post("/api/pay/naira/order", json={"product": "boost", "accepted": True, "amount": 1, "amount_local": 1, "price": 1, "user_id": 999,
                                                         "signals": 99999, "days": 400, "status": "approved", "narration": "TL-HACKED"})
        self.assertEqual(r.status_code, 200, r.text)
        o = r.json()["order"]
        db = row(o["id"])
        self.assertEqual((db["user_id"], db["product"], db["method"], db["currency"], db["status"]), (u, "boost", "moniepoint", "NGN", "pending"))
        self.assertEqual((db["amount_usd"], db["amount_local"], db["pay_amount"]), (10.0, 10.0 * RATE, 10.0 * RATE))
        self.assertTrue(db["terms_at"] and db["created_at"] and db["expires_at"] > db["created_at"])
        self.assertRegex(o["ref"], r"^TLM-[A-Z2-9]{8}$")
        self.assertRegex(o["narration"], r"^TL-[A-Z2-9]{8}$")
        self.assertEqual(db["narration"], o["narration"])                         # persisted, not just displayed
        self.assertNotEqual(o["narration"], "TL-HACKED")
        self.assertEqual((o["amount_naira"], o["status"], o["label"], o["currency"]), (10 * RATE, "pending", "Signal Boost", "NGN"))
        self.assertEqual(o["account"], {"bank_name": "Moniepoint MFB", "account_name": "Driverline Test Ltd", "account_number": "0123456789"})
        self.assertEqual(o["support"], "support@test.example")
        self.assertIn("created", [e["kind"] for e in events(o["id"])])
        self.assertEqual(balance(u), 0)                                           # creating an order grants nothing
        self.assertEqual(sql("SELECT * FROM billing_subs WHERE user_id=?", u), [])

    def test_the_narration_does_not_contain_the_email_or_the_user_id(self):
        u = mk_user()
        _, o = buy(u)
        self.assertNotIn(sql_users(u).split("@")[0].upper(), o["narration"])
        self.assertNotIn("@", o["narration"] + o["ref"])

    def test_every_product_category_can_be_ordered_at_its_catalogue_price(self):
        for product, p in credits.CATALOG.items():           # token packs, 30-day plans, and the plan with the MT5 connection
            u = mk_user()
            oid, o = buy(u, product)
            self.assertEqual((o["amount_naira"], o["signals"], o["days"], o["notify"], o["mt5"]),
                             (p["usd"] * RATE, p["signals"], p["days"], p["notify"], p["mt5"]), product)

    def test_narrations_and_references_are_unique(self):
        seen, refs = set(), set()
        for _ in range(30):
            u = mk_user()
            _, o = buy(u, "topup")
            seen.add(o["narration"])
            refs.add(o["ref"])
        self.assertEqual((len(seen), len(refs)), (30, 30))

    def test_validation_and_authentication(self):
        u = mk_user()
        c = client(u)
        self.assertEqual(c.post("/api/pay/naira/order", json={"product": "nope", "accepted": True}).status_code, 422)
        self.assertEqual(c.post("/api/pay/naira/order", json={"product": "boost"}).status_code, 422)
        self.assertEqual(c.post("/api/pay/naira/order", json={"product": "boost", "accepted": "yes"}).status_code, 422)
        self.assertEqual(c.post("/api/pay/naira/order", content=b"not json").status_code, 400)
        self.assertEqual(ANON.post("/api/pay/naira/order", json={"product": "boost", "accepted": True}).status_code, 401)
        self.assertEqual(sql("SELECT * FROM billing_orders WHERE user_id=?", u), [])

    def test_double_click_returns_the_same_order(self):
        u = mk_user()
        a, b = buy(u)[1], buy(u)[1]
        self.assertEqual((a["id"], a["narration"]), (b["id"], b["narration"]))
        self.assertEqual(len(sql("SELECT * FROM billing_orders WHERE user_id=?", u)), 1)
        self.assertNotEqual(buy(u, "topup")[0], a["id"])

    def test_open_order_limit(self):
        u = mk_user()
        for p in ("topup", "boost", "vault"):
            buy(u, p)
        self.assertEqual(client(u).post("/api/pay/naira/order", json={"product": "plus", "accepted": True}).status_code, 429)
        self.assertEqual(len(sql("SELECT * FROM billing_orders WHERE user_id=?", u)), 3)

    def test_stale_unpaid_orders_expire_so_a_member_is_not_locked_out(self):
        u = mk_user()
        ids = [buy(u, p)[0] for p in ("topup", "boost", "vault")]
        sql("UPDATE billing_orders SET expires_at=? WHERE user_id=?", int(time.time()) - 10, u)
        self.assertEqual(client(u).post("/api/pay/naira/order", json={"product": "plus", "accepted": True}).status_code, 200)
        self.assertTrue(all(row(i)["status"] == "expired" for i in ids))

    def test_the_member_never_sees_internal_data(self):
        u, admin = mk_user(), mk_user("admin")
        oid, o = buy(u)
        approve(admin, oid, "SECRET-BANK-REF", amount=1)                            # underpaid -> review, with an internal reason
        v = client(u).get(f"/api/pay/naira/order/{oid}").json()["order"]
        dump = json.dumps(v) + json.dumps(client(u).get("/api/billing").json())
        for hidden in ("SECRET-BANK-REF", "underpaid", "review_reason", "decided_by", "received 1.00"):
            self.assertNotIn(hidden, dump)
        self.assertEqual((v["status"], v["note"], v["txid"]), ("review", None, None))
        self.assertIn("checked by our team", v["message"])


# ---------------------------------------------------------------- 3. a pending order grants nothing
class PendingTests(Base):
    def test_pending_review_flagged_and_noted_orders_grant_nothing(self):
        u, admin = mk_user(), mk_user("admin")
        oid, o = buy(u, "pro")
        before = (balance(u), sql("SELECT * FROM billing_subs WHERE user_id=?", u))
        client(u).post(f"/api/pay/naira/order/{oid}/claim", json={"note": "paid from ADA OBI, ref 123456"})
        client(u).get(f"/api/pay/naira/order/{oid}")
        self.assertEqual(row(oid)["status"], "pending")
        client(admin).post(f"/api/admin/pay/naira/order/{oid}/flag", json={"note": "narration missing"})
        self.assertEqual(row(oid)["status"], "review")
        self.assertEqual((balance(u), sql("SELECT * FROM billing_subs WHERE user_id=?", u)), before)
        self.assertFalse(credits.entitled({"id": u, "tier": "free"}, "mt5"))

    def test_the_customer_note_is_information_only(self):
        u, admin = mk_user(), mk_user("admin")
        oid, _ = buy(u)
        self.assertEqual(client(u).post(f"/api/pay/naira/order/{oid}/claim", json={"note": "  Ada Obi \x00 / ref 99  "}).status_code, 200)
        self.assertEqual(client(u).post(f"/api/pay/naira/order/{oid}/claim", json={"note": ""}).status_code, 422)
        self.assertEqual(client(u).post(f"/api/pay/naira/order/{oid}/claim", json={}).status_code, 422)
        self.assertEqual((row(oid)["status"], balance(u)), ("pending", 0))
        shown = [o for o in client(admin).get("/api/admin/pay/naira?status=all").json()["orders"] if o["id"] == oid][0]
        self.assertEqual(shown["claim"], "Ada Obi / ref 99")

    def test_claim_is_owner_only_and_limited(self):
        a, b = mk_user(), mk_user()
        oid, _ = buy(a)
        self.assertEqual(client(b).post(f"/api/pay/naira/order/{oid}/claim", json={"note": "x"}).status_code, 404)
        self.assertEqual(ANON.post(f"/api/pay/naira/order/{oid}/claim", json={"note": "x"}).status_code, 401)
        codes = [client(a).post(f"/api/pay/naira/order/{oid}/claim", json={"note": f"n{i}"}).status_code for i in range(7)]
        self.assertEqual(codes[:5], [200] * 5)
        self.assertEqual(codes[5:], [429, 429])

    def test_member_can_cancel_only_a_pending_order(self):
        u, admin = mk_user(), mk_user("admin")
        oid, _ = buy(u)
        self.assertEqual(client(u).post(f"/api/pay/naira/order/{oid}/cancel").status_code, 200)
        self.assertEqual(row(oid)["status"], "cancelled")
        self.assertEqual(client(u).post(f"/api/pay/naira/order/{oid}/cancel").status_code, 409)
        oid2, _ = buy(u, "topup")
        client(admin).post(f"/api/admin/pay/naira/order/{oid2}/flag", json={"note": "checking"})
        self.assertEqual(client(u).post(f"/api/pay/naira/order/{oid2}/cancel").status_code, 409)       # an admin is checking it
        self.assertEqual(row(oid2)["status"], "review")


# ---------------------------------------------------------------- 4. approval fulfils exactly what was bought
class ApprovalTests(Base):
    def test_token_pack_is_credited_with_the_catalogue_quantity(self):
        u, admin = mk_user(), mk_user("admin")
        for product in ("topup", "boost", "vault"):
            oid, _ = buy(u, product)
            r = approve(admin, oid, fresh_ref())
            self.assertEqual((r["result"], r["status"]), ("approved", "approved"))
        self.assertEqual(balance(u), sum(credits.CATALOG[p]["signals"] for p in ("topup", "boost", "vault")))
        self.assertEqual(sql("SELECT * FROM billing_subs WHERE user_id=?", u), [])

    def test_subscription_activates_notifications_for_30_days_and_renewal_extends(self):
        u, admin = mk_user(), mk_user("admin")
        user = {"id": u, "tier": "free"}
        self.assertFalse(credits.entitled(user, "notify"))
        oid, _ = buy(u, "plus")
        approve(admin, oid, fresh_ref())
        subs = sql("SELECT * FROM billing_subs WHERE user_id=?", u)
        self.assertEqual((len(subs), subs[0]["plan"], subs[0]["order_id"]), (1, "plus", oid))
        self.assertAlmostEqual(subs[0]["ends_at"] - subs[0]["starts_at"], 30 * 86400, delta=2)
        self.assertTrue(credits.entitled(user, "notify"))
        self.assertFalse(credits.entitled(user, "mt5"))
        self.assertEqual(balance(u), 550)
        oid2, _ = buy(u, "plus")
        approve(admin, oid2, fresh_ref())
        first, second = sql("SELECT * FROM billing_subs WHERE user_id=? ORDER BY id", u)
        self.assertEqual(second["starts_at"], first["ends_at"])                    # renewal extends from the end of the current plan
        self.assertEqual(balance(u), 1100)

    def test_mt5_plan_grants_the_mt5_entitlement(self):
        u, admin = mk_user(), mk_user("admin")
        user = {"id": u, "tier": "free"}
        self.assertFalse(credits.entitled(user, "mt5"))
        oid, _ = buy(u, "pro")
        approve(admin, oid, fresh_ref())
        self.assertTrue(credits.entitled(user, "mt5") and credits.entitled(user, "notify"))
        self.assertEqual(balance(u), 550)

    def test_the_record_shows_who_approved_what_and_when(self):
        u, admin = mk_user(), mk_user("admin")
        oid, _ = buy(u, "boost")
        approve(admin, oid, "moniepoint/abc-123", note="seen in app")
        r = row(oid)
        self.assertEqual((r["status"], r["txid"], r["actually_paid"], r["decided_by"], r["review_reason"]), ("approved", "MONIEPOINT/ABC-123", 15000.0, admin, None))
        self.assertTrue(r["decided_at"] and r["fulfilled_at"])
        ev = events(oid)
        self.assertEqual([e["kind"] for e in ev if e["kind"] in ("created", "approved", "fulfilled")], ["created", "approved", "fulfilled"])
        approved = [e for e in ev if e["kind"] == "approved"][0]
        self.assertEqual(approved["admin_id"], admin)
        self.assertIn("MONIEPOINT/ABC-123", approved["detail"])
        self.assertIn("seen in app", approved["detail"])
        hist = client(admin).get(f"/api/admin/pay/naira/order/{oid}/events").json()["events"]
        self.assertEqual([e["admin_email"] for e in hist if e["kind"] == "approved"], [sql_users(admin)])
        v = client(u).get(f"/api/pay/naira/order/{oid}").json()["order"]
        self.assertEqual((v["status"], v["balance"], v["signals"]), ("approved", 550, 550))
        self.assertNotIn("account", v)

    def test_input_validation(self):
        u, admin = mk_user(), mk_user("admin")
        oid, _ = buy(u)
        ca = client(admin)
        url = f"/api/admin/pay/naira/order/{oid}/approve"
        for bad in ({}, {"bank_ref": "abc", "amount_received": 15000}, {"bank_ref": "bad ref!", "amount_received": 15000}, {"bank_ref": "A" * 101, "amount_received": 15000},
                    {"bank_ref": "REF-1234", "amount_received": 0}, {"bank_ref": "REF-1234", "amount_received": -5}, {"bank_ref": "REF-1234", "amount_received": "abc"},
                    {"bank_ref": "REF-1234", "amount_received": True}, {"bank_ref": "REF-1234", "amount_received": 10 ** 12}, {"bank_ref": "REF-1234"}):
            self.assertEqual(ca.post(url, json=bad).status_code, 422, bad)
        self.assertEqual(ca.post("/api/admin/pay/naira/order/999999/approve", json={"bank_ref": "REF-1234", "amount_received": 1}).status_code, 404)
        self.assertEqual((row(oid)["status"], balance(u)), ("pending", 0))
        self.assertEqual(approve(admin, oid, "REF-1234", amount="15,000.00")["result"], "approved")      # a typed amount with a comma is accepted


# ---------------------------------------------------------------- 5. underpaid, overpaid, rejected, unmatched
class DifferenceTests(Base):
    def test_underpayment_is_parked_and_grants_nothing(self):
        u, admin = mk_user(), mk_user("admin")
        oid, _ = buy(u, "plus")
        r = approve(admin, oid, "UNDER-0001", amount=10000)
        self.assertEqual((r["result"], r["status"]), ("review", "review"))
        self.assertIn("underpaid", r["reason"])
        o = row(oid)
        self.assertEqual((o["status"], o["actually_paid"], o["txid"], o["fulfilled_at"]), ("review", 10000.0, "UNDER-0001", None))
        self.assertEqual((balance(u), sql("SELECT * FROM billing_subs WHERE user_id=?", u)), (0, []))
        self.assertEqual(len(events(oid, "underpaid")), 1)

    def test_overpayment_is_parked_and_grants_nothing(self):
        u, admin = mk_user(), mk_user("admin")
        oid, _ = buy(u)
        r = approve(admin, oid, "OVER-0001", amount=20000)
        self.assertEqual(r["result"], "review")
        self.assertIn("overpaid", r["reason"])
        self.assertEqual((row(oid)["status"], balance(u)), ("review", 0))

    def test_a_difference_can_only_be_accepted_explicitly_with_a_note(self):
        u, admin = mk_user(), mk_user("admin")
        oid, _ = buy(u)
        approve(admin, oid, "DIFF-0001", amount=14000)
        approve(admin, oid, "DIFF-0001", amount=14000, accept=True, expect=422)             # a note is required
        self.assertEqual((row(oid)["status"], balance(u)), ("review", 0))
        r = approve(admin, oid, "DIFF-0001", amount=14000, accept=True, note="customer topped up by transfer to the other account, verified")
        self.assertEqual((r["result"], row(oid)["status"], balance(u)), ("approved", "approved", 550))
        self.assertIn("difference accepted", [e for e in events(oid, "approved")][0]["detail"])

    def test_an_exact_amount_in_review_needs_a_note_to_approve(self):
        u, admin = mk_user(), mk_user("admin")
        oid, _ = buy(u)
        client(admin).post(f"/api/admin/pay/naira/order/{oid}/flag", json={"note": "narration missing"})
        approve(admin, oid, "FLAG-0001", expect=422)
        self.assertEqual(approve(admin, oid, "FLAG-0001", note="matched by amount and time")["result"], "approved")
        self.assertEqual(balance(u), 550)

    def test_reject_records_an_internal_reason_the_member_never_sees(self):
        u, admin = mk_user(), mk_user("admin")
        oid, _ = buy(u)
        ca = client(admin)
        self.assertEqual(ca.post(f"/api/admin/pay/naira/order/{oid}/reject", json={}).status_code, 422)
        self.assertEqual(ca.post(f"/api/admin/pay/naira/order/{oid}/reject", json={"reason": "no credit of this amount that day (internal)"}).status_code, 200)
        o = row(oid)
        self.assertEqual((o["status"], o["decided_by"], balance(u)), ("rejected", admin, 0))
        self.assertIn("no credit", o["review_reason"])
        v = client(u).get(f"/api/pay/naira/order/{oid}").json()["order"]
        self.assertNotIn("no credit", json.dumps(v))
        self.assertEqual(v["status"], "rejected")
        self.assertEqual(ca.post(f"/api/admin/pay/naira/order/{oid}/reject", json={"reason": "again"}).status_code, 409)
        approve(admin, oid, "REJ-0001", expect=409)                                          # a rejected order is final
        self.assertEqual(balance(u), 0)

    def test_review_orders_can_be_rejected_and_approved_orders_cannot(self):
        u, admin = mk_user(), mk_user("admin")
        oid, _ = buy(u)
        approve(admin, oid, "R-0001", amount=1)
        self.assertEqual(client(admin).post(f"/api/admin/pay/naira/order/{oid}/reject", json={"reason": "short payment not made good"}).status_code, 200)
        oid2, _ = buy(u, "topup")
        approve(admin, oid2, "R-0002")
        self.assertEqual(client(admin).post(f"/api/admin/pay/naira/order/{oid2}/reject", json={"reason": "x"}).status_code, 409)
        self.assertEqual(client(admin).post(f"/api/admin/pay/naira/order/{oid2}/flag", json={"note": "x"}).status_code, 409)
        self.assertEqual(row(oid2)["status"], "approved")

    def test_expired_and_cancelled_orders_can_be_approved_only_with_a_note(self):
        u, admin = mk_user(), mk_user("admin")
        for state in ("expired", "cancelled"):
            oid, _ = buy(u, "topup" if state == "expired" else "boost")
            sql("UPDATE billing_orders SET status=? WHERE id=?", state, oid)
            approve(admin, oid, fresh_ref(), expect=422)
            self.assertEqual(approve(admin, oid, fresh_ref(), note="customer paid late, funds confirmed")["result"], "approved")
        self.assertEqual(balance(u), 267 + 550)

    def test_flagging_unmatched_bank_credits(self):
        admin, u = mk_user("admin"), mk_user()
        ca = client(admin)
        for bad in ({}, {"bank_ref": "x", "note": "n"}, {"bank_ref": "UNM-0001"}, {"bank_ref": "UNM-0001", "note": "n", "amount": "abc"}):
            self.assertEqual(ca.post("/api/admin/pay/naira/unmatched", json=bad).status_code, 422, bad)
        r = ca.post("/api/admin/pay/naira/unmatched", json={"bank_ref": "unm-0001", "amount": 15000, "narration_seen": "TL-WRONG", "note": "no matching order"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(ca.post("/api/admin/pay/naira/unmatched", json={"bank_ref": "UNM-0001", "note": "again"}).status_code, 409)       # not flagged twice
        listed = ca.get("/api/admin/pay/naira").json()["unmatched"]
        self.assertEqual([x["bank_ref"] for x in listed if x["bank_ref"] == "UNM-0001"], ["UNM-0001"])
        self.assertIn("TL-WRONG", [x for x in listed if x["bank_ref"] == "UNM-0001"][0]["detail"])
        self.assertEqual(balance(u), 0)                                                       # flagging never credits anyone
        oid, _ = buy(u)                                                                       # the admin later matches it to an order
        approve(admin, oid, "UNM-0001")
        self.assertNotIn("UNM-0001", [x["bank_ref"] for x in ca.get("/api/admin/pay/naira").json()["unmatched"]])
        self.assertEqual(balance(u), 550)
        self.assertEqual(client(u).post("/api/admin/pay/naira/unmatched", json={"bank_ref": "UNM-0002", "note": "x"}).status_code, 403)


# ---------------------------------------------------------------- 6. exactly once
class ExactlyOnceTests(Base):
    def test_duplicate_clicks_credit_once(self):
        u, admin = mk_user(), mk_user("admin")
        oid, _ = buy(u, "pro")
        first = approve(admin, oid, "ONCE-0001")
        for _ in range(4):
            again = approve(admin, oid, "ONCE-0001")
            self.assertEqual(again["result"], "already_approved")
        self.assertEqual(first["result"], "approved")
        self.assertEqual(balance(u), 550)
        self.assertEqual(len(sql("SELECT * FROM billing_subs WHERE user_id=?", u)), 1)
        self.assertEqual(len(sql("SELECT * FROM credit_ledger WHERE user_id=? AND kind='purchase'", u)), 1)
        self.assertEqual((len(events(oid, "approved")), len(events(oid, "fulfilled"))), (1, 1))

    def test_a_second_administrator_with_a_different_reference_cannot_credit_again(self):
        u, a1, a2 = mk_user(), mk_user("admin"), mk_user("admin")
        oid, _ = buy(u)
        approve(a1, oid, "TWO-0001")
        self.assertEqual(approve(a2, oid, "TWO-0002")["result"], "already_approved")
        self.assertEqual((balance(u), row(oid)["txid"], row(oid)["decided_by"]), (550, "TWO-0001", a1))

    def test_concurrent_approvals_credit_once(self):
        u, admin = mk_user(), mk_user("admin")
        oid, _ = buy(u, "pro")
        results, errors, barrier = [], [], threading.Barrier(10)

        def go(i):
            barrier.wait()
            try:
                results.append(naira._approve(oid, admin, f"RACE-{i:04d}", 30.0 * RATE, "", False)["result"])
            except HTTPException as e:
                errors.append(e.status_code)
        ts = [threading.Thread(target=go, args=(i,)) for i in range(10)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertEqual(results.count("approved"), 1, (results, errors))
        self.assertTrue(set(results) <= {"approved", "already_approved"}, results)
        self.assertEqual(balance(u), 550)
        self.assertEqual(len(sql("SELECT * FROM billing_subs WHERE user_id=?", u)), 1)
        self.assertEqual(len(sql("SELECT * FROM credit_ledger WHERE user_id=? AND kind='purchase'", u)), 1)

    def test_concurrent_approvals_over_http_credit_once(self):
        u, a1, a2 = mk_user(), mk_user("admin"), mk_user("admin")
        oid, _ = buy(u)
        codes = []
        ts = [threading.Thread(target=lambda a=a, i=i: codes.append(client(a).post(
            f"/api/admin/pay/naira/order/{oid}/approve", json={"bank_ref": f"HTTP-{i:04d}", "amount_received": 15000}).status_code))
              for i, a in enumerate([a1, a2] * 3)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertEqual(set(codes), {200}, codes)
        self.assertEqual((row(oid)["status"], balance(u)), ("approved", 550))

    def test_approve_racing_a_reject_and_a_cancel_has_one_winner(self):
        for first in ("approve", "reject"):
            u, admin = mk_user(), mk_user("admin")
            oid, _ = buy(u)
            out, barrier = {}, threading.Barrier(3)

            def a():
                barrier.wait()
                out["a"] = client(admin).post(f"/api/admin/pay/naira/order/{oid}/approve", json={"bank_ref": fresh_ref(), "amount_received": 15000}).status_code

            def b():
                barrier.wait()
                out["b"] = client(admin).post(f"/api/admin/pay/naira/order/{oid}/reject", json={"reason": "race"}).status_code

            def c():
                barrier.wait()
                out["c"] = client(u).post(f"/api/pay/naira/order/{oid}/cancel").status_code
            ts = [threading.Thread(target=f) for f in (a, b, c)]
            [t.start() for t in ts]
            [t.join() for t in ts]
            status = row(oid)["status"]
            self.assertIn(status, ("approved", "rejected", "cancelled"))
            self.assertEqual(balance(u), 550 if status == "approved" else 0, (status, out))
            self.assertEqual(len(sql("SELECT * FROM credit_ledger WHERE user_id=? AND kind='purchase'", u)), 1 if status == "approved" else 0)

    def test_the_ledger_alone_prevents_a_double_credit(self):
        u, admin = mk_user(), mk_user("admin")
        oid, _ = buy(u)
        with closing(credits.db()) as con:
            self.assertTrue(credits.grant_product(con, u, "boost", oid, "X"))
            con.commit()
        r = approve(admin, oid, "LEDGER-0001")                                                # the credit existed: recorded, not repeated
        self.assertEqual((r["result"], row(oid)["status"], balance(u)), ("already_credited", "approved", 550))
        self.assertEqual(len(events(oid, "duplicate_guard")), 1)

    def test_one_bank_transaction_cannot_settle_two_orders(self):
        u1, u2, admin = mk_user(), mk_user(), mk_user("admin")
        o1, _ = buy(u1)
        o2, _ = buy(u2)
        approve(admin, o1, "SAME-0001")
        r = client(admin).post(f"/api/admin/pay/naira/order/{o2}/approve", json={"bank_ref": "same-0001", "amount_received": 15000})   # case does not matter
        self.assertEqual(r.status_code, 409, r.text)
        self.assertIn("already attached", r.json()["detail"])
        r = client(admin).post(f"/api/admin/pay/naira/order/{o2}/approve", json={"bank_ref": "SAME-0001", "amount_received": 14000})      # not even as a review
        self.assertEqual(r.status_code, 409)
        self.assertEqual((row(o2)["status"], balance(u2), balance(u1)), ("pending", 0, 550))
        self.assertEqual(approve(admin, o2, "SAME-0002")["result"], "approved")


# ---------------------------------------------------------------- 7. only administrators; members cannot do any of it
class AccessTests(Base):
    def test_members_and_anonymous_cannot_reach_any_admin_endpoint(self):
        u = mk_user()
        oid, _ = buy(u)
        c = client(u)
        calls = (("get", "/api/admin/pay/naira"), ("post", "/api/admin/pay/naira/toggle"), ("post", "/api/admin/pay/naira/unmatched"),
                 ("post", f"/api/admin/pay/naira/order/{oid}/approve"), ("post", f"/api/admin/pay/naira/order/{oid}/reject"),
                 ("post", f"/api/admin/pay/naira/order/{oid}/flag"), ("get", f"/api/admin/pay/naira/order/{oid}/events"))
        for method, path in calls:
            self.assertEqual(getattr(c, method)(path).status_code, 403, path)
            self.assertEqual(getattr(ANON, method)(path).status_code, 401, path)
        r = c.post(f"/api/admin/pay/naira/order/{oid}/approve", json={"bank_ref": "SELF-0001", "amount_received": 15000, "note": "i paid", "accept_difference": True})
        self.assertEqual(r.status_code, 403)
        self.assertEqual((row(oid)["status"], balance(u)), ("pending", 0))

    def test_a_blocked_or_demoted_admin_loses_access_at_once(self):
        u, admin = mk_user(), mk_user("admin")
        oid, _ = buy(u)
        sql_tier = lambda t: (lambda con: (con.execute("UPDATE users SET tier=? WHERE id=?", (t, admin)), con.commit()))(auth.db())
        with closing(auth.db()) as con:
            con.execute("UPDATE users SET tier='member' WHERE id=?", (admin,))
            con.commit()
        try:
            self.assertEqual(client(admin).post(f"/api/admin/pay/naira/order/{oid}/approve", json={"bank_ref": "DEM-0001", "amount_received": 15000}).status_code, 403)
        finally:
            with closing(auth.db()) as con:
                con.execute("UPDATE users SET tier='admin' WHERE id=?", (admin,))
                con.commit()
        self.assertEqual(balance(u), 0)

    def test_members_cannot_see_or_change_other_members_orders(self):
        a, b = mk_user(), mk_user()
        oid, _ = buy(a)
        self.assertEqual(client(b).get(f"/api/pay/naira/order/{oid}").status_code, 404)
        self.assertEqual(client(b).post(f"/api/pay/naira/order/{oid}/cancel").status_code, 404)
        self.assertEqual(ANON.get(f"/api/pay/naira/order/{oid}").status_code, 401)
        self.assertEqual(client(b).get("/api/pay/naira/order/999999").status_code, 404)
        self.assertEqual([o for o in client(b).get("/api/billing").json()["orders"] if o["id"] == oid], [])
        self.assertEqual(client(a).get(f"/api/pay/naira/order/{oid}").status_code, 200)

    def test_nothing_the_member_can_call_changes_the_amount_or_the_status(self):
        u = mk_user()
        oid, o = buy(u)
        c = client(u)
        for path in (f"/api/billing/order/{oid}/paid", f"/api/billing/order/{oid}/cancel"):
            self.assertEqual(c.post(path, json={"txid": "0x" + "a" * 40}).status_code, 409, path)
        self.assertEqual(c.post("/api/billing/order", json={"product": "boost", "method": "moniepoint", "accepted": True}).status_code, 422)
        for verb in ("put", "patch", "delete"):
            self.assertIn(c.request(verb.upper(), f"/api/pay/naira/order/{oid}", json={"amount_naira": 1, "status": "approved"}).status_code, (404, 405))
        r = row(oid)
        self.assertEqual((r["status"], r["pay_amount"], r["narration"]), ("pending", 15000.0, o["narration"]))

    def test_the_manual_admin_queue_never_offers_naira_orders(self):
        u, admin = mk_user(), mk_user("admin")
        oid, _ = buy(u)
        q = client(admin).get("/api/admin/billing").json()
        self.assertNotIn(oid, [o["id"] for o in q["open"]])
        self.assertEqual(client(admin).post(f"/api/admin/billing/order/{oid}/approve").status_code, 409)
        self.assertEqual(client(admin).post(f"/api/admin/billing/order/{oid}/reject", json={}).status_code, 409)
        self.assertEqual((row(oid)["status"], balance(u)), ("pending", 0))
        approve(admin, oid, "QUEUE-0001")
        self.assertNotIn(oid, [o["id"] for o in client(admin).get("/api/admin/billing").json()["recent"]])

    def test_toggle(self):
        admin = mk_user("admin")
        ca = client(admin)
        self.assertEqual(ca.post("/api/admin/pay/naira/toggle", json={"mode": "nonsense"}).status_code, 422)
        self.assertEqual(ca.post("/api/admin/pay/naira/toggle", json={"mode": "admin"}).status_code, 200)
        self.assertEqual(naira.mode(), "admin")
        put(enabled="0")
        r = ca.post("/api/admin/pay/naira/toggle", json={"mode": "public"})
        self.assertEqual(r.status_code, 409)
        put(enabled="1", bank_name="")
        self.assertEqual(ca.post("/api/admin/pay/naira/toggle", json={"mode": "public"}).status_code, 409)
        self.assertEqual(ca.post("/api/admin/pay/naira/toggle", json={"mode": "off"}).status_code, 200)
        self.assertEqual(naira.mode(), "off")

    def test_existing_orders_stay_approvable_when_the_feature_is_switched_off(self):
        u, admin = mk_user(), mk_user("admin")
        oid, _ = buy(u)
        put(naira_enabled="0")
        self.assertEqual(client(u).get(f"/api/pay/naira/order/{oid}").status_code, 200)
        self.assertEqual(approve(admin, oid, "OFF-0001")["result"], "approved")
        self.assertEqual(balance(u), 550)


# ---------------------------------------------------------------- 8. the admin list: filter and search
class AdminListTests(Base):
    def setUp(self):
        super().setUp()
        self.admin = mk_user("admin")
        self.u = mk_user()
        self.o1, self.n1 = buy(self.u, "boost")
        self.o2, self.n2 = buy(self.u, "topup")
        self.sref = fresh_ref()
        approve(self.admin, self.o2, self.sref)
        self.ca = client(self.admin)

    def ids(self, **params):
        r = self.ca.get("/api/admin/pay/naira", params=params)
        self.assertEqual(r.status_code, 200, r.text)
        return [o["id"] for o in r.json()["orders"]]

    def test_status_filters(self):
        self.assertIn(self.o1, self.ids())
        self.assertNotIn(self.o2, self.ids())                                                  # open = pending + review
        self.assertEqual(self.o2 in self.ids(status="approved"), True)
        self.assertIn(self.o1, self.ids(status="pending"))
        self.assertNotIn(self.o1, self.ids(status="approved"))
        self.assertTrue({self.o1, self.o2} <= set(self.ids(status="all")))
        self.assertEqual(self.ids(status="garbage")[:1] == self.ids()[:1], True)               # an unknown status falls back to open

    def test_search_by_reference_narration_email_amount_and_bank_reference(self):
        email = sql_users(self.u)
        for q in (self.n1["ref"], self.n1["ref"].lower(), self.n1["narration"], self.n1["narration"][3:9], email, email.split("@")[0]):
            self.assertIn(self.o1, self.ids(status="all", q=q), q)
        self.assertEqual(self.ids(status="all", q="15000").count(self.o1), 1)                 # amount asked for
        self.assertIn(self.o2, self.ids(status="all", q="7500"))
        self.assertIn(self.o2, self.ids(status="all", q=self.sref))
        self.assertIn(self.o2, self.ids(status="all", q=self.sref.lower()[:8]))
        self.assertNotIn(self.o1, self.ids(status="all", q=self.sref))
        self.assertEqual(self.ids(status="all", q="no-such-thing-anywhere"), [])
        for evil in ("'; DROP TABLE billing_orders; --", "%", "_", '" OR 1=1 --'):
            self.assertEqual(self.ca.get("/api/admin/pay/naira", params={"status": "all", "q": evil}).status_code, 200, evil)
        self.assertTrue(sql("SELECT COUNT(*) AS c FROM billing_orders")[0]["c"] > 0)

    def test_search_by_date(self):
        today = time.strftime("%Y-%m-%d", time.gmtime())
        self.assertIn(self.o1, self.ids(status="all", **{"from": today, "to": today}))
        self.assertNotIn(self.o1, self.ids(status="all", **{"from": "2999-01-01"}))
        self.assertNotIn(self.o1, self.ids(status="all", **{"to": "2000-01-01"}))
        self.assertIn(self.o1, self.ids(status="all", **{"from": "not-a-date"}))                 # an invalid date is ignored, not an error

    def test_paging_and_the_page_content(self):
        r = self.ca.get("/api/admin/pay/naira", params={"status": "all", "limit": 1}).json()
        self.assertEqual((len(r["orders"]), r["total"] >= 2), (1, True))
        r2 = self.ca.get("/api/admin/pay/naira", params={"status": "all", "limit": 1, "offset": 1}).json()
        self.assertNotEqual(r["orders"][0]["id"], r2["orders"][0]["id"])
        o = [x for x in self.ca.get("/api/admin/pay/naira", params={"status": "all"}).json()["orders"] if x["id"] == self.o2][0]
        self.assertEqual((o["email"], o["status"], o["bank_ref"], o["received"], o["expected"], o["decided_by"]),
                         (sql_users(self.u), "approved", self.sref, 7500.0, 7500.0, sql_users(self.admin)))
        self.assertEqual(r["mode"], "public")
        self.assertEqual(r["bank"]["account_number"], "0123456789")
        self.assertEqual(r["problems"], [])


# ---------------------------------------------------------------- 9. PayPal-style regressions: the other payment methods are untouched
class RegressionTests(Base):
    def test_manual_usdt_order_still_works_end_to_end(self):
        u, admin = mk_user(), mk_user("admin")
        put(pay_trc20="TManualWallet")
        r = client(u).post("/api/billing/order", json={"product": "boost", "method": "trc20", "accepted": True})
        self.assertEqual(r.status_code, 200, r.text)
        oid = r.json()["order"]["id"]
        self.assertEqual(client(u).post(f"/api/billing/order/{oid}/paid", json={"txid": "0x" + "b" * 40}).status_code, 200)
        self.assertIn(oid, [o["id"] for o in client(admin).get("/api/admin/billing").json()["open"]])
        self.assertEqual(client(admin).post(f"/api/admin/billing/order/{oid}/approve").status_code, 200)
        self.assertEqual(balance(u), 550)
        self.assertEqual(client(admin).post(f"/api/admin/billing/order/{oid}/approve").status_code, 409)
        self.assertEqual(balance(u), 550)

    def test_manual_cancel_reject_adjust_and_spend_still_work(self):
        u, admin = mk_user(), mk_user("admin")
        put(pay_trc20="TManualWallet")
        oid = client(u).post("/api/billing/order", json={"product": "topup", "method": "trc20", "accepted": True}).json()["order"]["id"]
        self.assertEqual(client(u).post(f"/api/billing/order/{oid}/cancel").status_code, 200)
        oid = client(u).post("/api/billing/order", json={"product": "topup", "method": "trc20", "accepted": True}).json()["order"]["id"]
        self.assertEqual(client(admin).post(f"/api/admin/billing/order/{oid}/reject", json={"note": "no"}).status_code, 200)
        self.assertEqual(client(admin).post("/api/admin/billing/adjust", json={"email": sql_users(u), "delta": 5, "note": "gift"}).status_code, 200)
        self.assertTrue(credits.spend(u, "signal:1"))
        self.assertEqual(balance(u), 4)

    def test_a_crypto_order_and_a_naira_order_live_side_by_side(self):
        u = mk_user()
        _, o = buy(u)
        self.assertEqual(client(u).get("/api/billing").json()["crypto"]["enabled"], False)       # crypto stays off here: no NOWPayments key in this suite
        self.assertEqual([x["method"] for x in client(u).get("/api/billing").json()["orders"]], ["moniepoint"])
        self.assertEqual(client(u).get("/api/billing").json()["orders"][0]["method_label"], credits.NAIRA_LABEL)

    def test_old_orders_of_the_retired_methods_are_kept_and_still_listed(self):
        """Historical rows (the old manual Naira form, and the retired card integration's test orders) are never deleted or rewritten."""
        u = mk_user()
        retired = "pay" + "stack"
        with closing(credits.db()) as con:
            con.insert("INSERT INTO billing_orders(user_id,product,method,amount_usd,amount_local,currency,ref,status,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                       (u, "boost", "ngn", 10, 15000, "NGN", "TL-OLD001", "approved", int(time.time())))
            con.insert("INSERT INTO billing_orders(user_id,product,method,amount_usd,amount_local,currency,ref,status,created_at,provider) VALUES(?,?,?,?,?,?,?,?,?,?)",
                       (u, "topup", retired, 5, 7500, "NGN", "TLN-T-OLD0001", "approved", int(time.time()), retired + "-test"))
            con.commit()
        r = client(u).get("/api/billing")
        self.assertEqual(r.status_code, 200)
        shown = {o["ref"]: o for o in r.json()["orders"]}
        self.assertEqual(shown["TL-OLD001"]["method_label"], credits.LEGACY_LABELS["ngn"])
        self.assertEqual(shown["TLN-T-OLD0001"]["status"], "approved")
        self.assertEqual(len(sql("SELECT * FROM billing_orders WHERE user_id=?", u)), 2)

    def test_existing_balances_subscriptions_and_invites_are_not_touched_by_a_naira_purchase(self):
        u, admin = mk_user(), mk_user("admin")
        with closing(credits.db()) as con:
            credits._credit(con, u, 7, "welcome", "welcome", "Welcome")
            con.execute("INSERT INTO billing_subs(user_id,plan,starts_at,ends_at,order_id) VALUES(?,?,?,?,?)", (u, "partner_notify", int(time.time()), int(time.time()) + 86400, None))
            con.commit()
        before = sql("SELECT * FROM billing_subs WHERE user_id=?", u)
        invites = sql("SELECT * FROM invites")
        oid, _ = buy(u, "topup")
        approve(admin, oid, fresh_ref())
        self.assertEqual(balance(u), 7 + 267)
        self.assertEqual(sql("SELECT * FROM billing_subs WHERE user_id=?", u), before)
        self.assertEqual(sql("SELECT * FROM invites"), invites)
        self.assertEqual(sorted(x["kind"] for x in sql("SELECT kind FROM credit_ledger WHERE user_id=?", u)), ["purchase", "welcome"])


# ---------------------------------------------------------------- 10. the old card integration is really gone
class RemovalTests(unittest.TestCase):
    WORD = re.compile("pay" + "stack", re.I)

    def test_no_active_reference_remains_in_the_application(self):
        hits = []
        for folder, exts in (("app", (".py",)), ("static", (".html", ".js", ".css", ".json")), ("scripts", (".py",)), ("tests", (".py",)), ("migrations", (".sql",)),
                             ("docs", (".md",))):
            for f in (ROOT / folder).rglob("*"):
                if f.suffix in exts and "__pycache__" not in f.parts and f.name not in (Path(__file__).name, "NAIRA_MONIEPOINT.md"):   # that doc has the clean-up steps
                    for n, line in enumerate(f.read_text(errors="ignore").splitlines(), 1):
                        if self.WORD.search(line):
                            hits.append(f"{f.relative_to(ROOT)}:{n}")
        self.assertEqual(hits, [], "leftover references: " + ", ".join(hits))

    def test_the_removed_files_are_gone(self):
        for rel in ("app/pay" + "stack.py", "static/pay" + "stack.html", "tests/test_pay" + "stack.py", "scripts/pay" + "stack_check.py", "docs/PAY" + "STACK.md"):
            self.assertFalse((ROOT / rel).exists(), rel)

    def test_no_such_route_or_environment_variable_is_used(self):
        for path in ("/api/pay/" + "paystack/order", "/api/pay/" + "paystack/webhook", "/api/admin/pay/" + "paystack"):
            self.assertIn(ANON.post(path, json={}).status_code, (404, 405), path)
        main_src = (ROOT / "app" / "main.py").read_text()
        self.assertNotRegex(main_src, self.WORD)
        for f in (ROOT / "app").glob("*.py"):
            self.assertNotIn("PAY" + "STACK_", f.read_text(), f.name)


# ---------------------------------------------------------------- 11. database
class MigrationTests(unittest.TestCase):
    def test_new_columns_and_unique_guards_exist_and_hold(self):
        with closing(credits.db()) as con:
            self.assertTrue(con.has_column("billing_orders", "narration"))
        u = mk_user()
        now = int(time.time())
        ins = ("INSERT INTO billing_orders(user_id,product,method,amount_usd,amount_local,currency,ref,status,created_at,narration,txid) VALUES(?,?,?,?,?,?,?,?,?,?,?)")
        with closing(credits.db()) as con:
            con.insert(ins, (u, "boost", "moniepoint", 10, 15000, "NGN", "TLM-DBTEST01", "pending", now, "TL-DBTEST01", "DBREF-1"))
            con.commit()
        for ref, narr, txid, method in (("TLM-DBTEST02", "TL-DBTEST01", None, "moniepoint"), ("TLM-DBTEST03", "TL-DBTEST03", "DBREF-1", "moniepoint")):
            with closing(credits.db()) as con:
                with self.assertRaises(Exception) as cm:
                    con.insert(ins, (u, "boost", method, 10, 15000, "NGN", ref, "pending", now, narr, txid))
                self.assertTrue(dbx.is_duplicate(cm.exception))
                con.raw.rollback()
        with closing(credits.db()) as con:                      # the bank-reference guard applies to Naira orders only: manual USDT orders may share a txid
            for i in range(2):
                con.insert(ins, (u, "boost", "trc20", 10, 10, "USDT", f"TL-DBUSDT{i}", "submitted", now, None, "0xSAMEHASH"))
            con.commit()

    def test_the_migration_file_is_additive_and_matches_the_code(self):
        text = (ROOT / "migrations" / "2026_10_naira.sql").read_text()
        body = "\n".join(l for l in text.splitlines() if not l.strip().startswith("--"))
        for word in ("DROP", "DELETE", "TRUNCATE", "UPDATE", "RENAME"):
            self.assertIsNone(re.search(r"\b" + word + r"\b", body.upper()), word)
        for needle in ("ADD COLUMN IF NOT EXISTS narration", "ux_orders_narration", "ux_orders_bank_txid", "method = 'moniepoint'"):
            self.assertIn(needle, body)
        src = (ROOT / "app" / "credits.py").read_text()
        for needle in ('"narration"', "ux_orders_narration", "ux_orders_bank_txid"):
            self.assertIn(needle, src)
        mod = (ROOT / "app" / "naira.py").read_text()
        for bad in ("CREATE TABLE", "DROP ", "DELETE FROM", "ALTER TABLE"):
            self.assertNotIn(bad, mod)


# ---------------------------------------------------------------- 12. the separate Payments admin page
class PaymentsPageTests(unittest.TestCase):
    def test_payment_tools_live_on_their_own_page_and_the_admin_page_links_to_it(self):
        admin = (ROOT / "static" / "admin.html").read_text()
        pay = (ROOT / "static" / "admin-payments.html").read_text()
        for fn in ("loadNaira", "loadCrypto", "loadBilling"):
            self.assertIn("async function " + fn, pay)
            self.assertNotIn(fn, admin)
        self.assertIn("/admin-payments.html", admin)
        self.assertIn("/admin.html", pay)                                   # and back
        self.assertIn("/admin-payments.html", (ROOT / "static" / "auth.js").read_text())
        for other in ("loadEngine", "loadResearch", "loadBacktest", "loadPartner"):
            self.assertIn(other, admin)                                     # the rest of the admin page is untouched

    def test_the_page_calls_only_admin_protected_endpoints(self):
        pay = (ROOT / "static" / "admin-payments.html").read_text()
        self.assertIn("api('/overview')", pay)                              # a non-admin gets the "admins only" message


if __name__ == "__main__":
    unittest.main(verbosity=2)
