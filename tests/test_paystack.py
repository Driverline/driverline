"""Tests for the Paystack (Naira) integration (app/paystack.py) and the billing code it shares (app/credits.py).
Run with `python tests/test_paystack.py` (or `python -m pytest tests/test_paystack.py`). Run it as its own process, like test_nowpayments.py.

No network and no real Paystack account is needed: a small fake Paystack API (FakePaystack) is plugged in through httpx.MockTransport and
behaves like the documented endpoints (POST /transaction/initialize, GET /transaction/verify/{reference}, Authorization: Bearer <secret key>).
It can be told to fail, to report any transaction status, and to return a wrong amount, currency, reference, domain, metadata or customer.
The webhook signature is computed independently of the app code (HMAC-SHA512 of the raw body with the secret key).
Only fake keys are used. No real key, and no real money, is involved anywhere in this file."""
import asyncio
import hashlib
import hmac
import io
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
from contextlib import closing, redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
try:
    import httpx
except ImportError:                       # newer Starlette ships its HTTP client as httpx2: same API
    import httpx2 as httpx
    sys.modules["httpx"] = httpx
for _name in ("websockets", "pywebpush", "psycopg", "psycopg.rows"):          # only needed by modules these tests never run
    try:
        __import__(_name)
    except ImportError:
        import types
        sys.modules[_name] = types.ModuleType(_name)

TEST_KEY, LIVE_KEY = "sk_test_FAKE_not_a_real_key_1", "sk_live_FAKE_not_a_real_key_2"
os.environ.update(DRIVERLINE_SECRET="x" * 40, PAYSTACK_SECRET_KEY=LIVE_KEY, PAYSTACK_ALLOW_LIVE="1",
                  DRIVERLINE_BASE_URL="https://tradelens.example", NOWPAYMENTS_API_KEY="np-key", NOWPAYMENTS_IPN_SECRET="np-secret")
os.environ.pop("DATABASE_URL", None)
os.environ.pop("PAYSTACK_PUBLIC_KEY", None)

from app import auth, credits, dbx, nowpay, paystack   # noqa: E402
from starlette.applications import Starlette          # noqa: E402
from starlette.exceptions import HTTPException        # noqa: E402
from starlette.responses import JSONResponse          # noqa: E402
from starlette.testclient import TestClient           # noqa: E402

TMP = Path(tempfile.mkdtemp())
dbx.SQLITE_PATH = TMP / "test.db"
RATE = 1500


async def _http_error(request, exc):
    return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)


APP = Starlette(routes=[*auth.routes, *credits.routes, *nowpay.routes, *paystack.routes], exception_handlers={HTTPException: _http_error})


def current_key():
    return os.environ["PAYSTACK_SECRET_KEY"]


def use_key(kind):
    os.environ["PAYSTACK_SECRET_KEY"] = TEST_KEY if kind == "test" else LIVE_KEY
    paystack._switch["ts"] = 0


# ---------------------------------------------------------------- a fake Paystack
class FakePaystack:
    def __init__(self):
        self.tx, self.calls, self.n = {}, [], 900000
        self.down = False
        self.unknown_ref_status = 400         # what Paystack really answers for an unknown reference
        self.init_override = {}               # fields to change in the reply to initialize
        self.init_hook = None                 # called with the request body while "Paystack" handles initialize
        self.verify_override = {}             # fields to change in every verify reply: {ref: {...}} or {"*": {...}}

    def set(self, ref, **kw):
        self.tx[ref].update(kw)

    def handler(self, request):
        path = request.url.path
        self.calls.append((request.method, path, request.content.decode() if request.content else "", request.headers.get("authorization", "")))
        if self.down:
            raise httpx.ConnectError("paystack down")
        if request.headers.get("authorization") not in ("Bearer " + TEST_KEY, "Bearer " + LIVE_KEY):   # the two keys of the fake account
            return httpx.Response(401, json={"status": False, "message": "Invalid key"})
        if request.method == "POST" and path == "/transaction/initialize":
            b = json.loads(request.content)
            if self.init_hook:
                self.init_hook(b)
            if not isinstance(b.get("amount"), int) or b["amount"] < 100:
                return httpx.Response(400, json={"status": False, "message": "Invalid Amount Sent"})
            if b["reference"] in self.tx:
                return httpx.Response(400, json={"status": False, "message": "Duplicate Transaction Reference"})
            self.n += 1
            self.tx[b["reference"]] = {"id": self.n, "status": "abandoned", "reference": b["reference"], "amount": b["amount"], "currency": b["currency"],
                                       "domain": "test" if request.headers["authorization"].endswith(TEST_KEY) else "live", "metadata": b.get("metadata"),
                                       "customer": {"email": b["email"]}, "gateway_response": "", "callback_url": b.get("callback_url")}
            data = {"authorization_url": f"https://checkout.paystack.com/acc{self.n}", "access_code": f"acc{self.n}", "reference": b["reference"]}
            data.update(self.init_override)
            return httpx.Response(200, json={"status": True, "message": "Authorization URL created", "data": data})
        if request.method == "GET" and path.startswith("/transaction/verify/"):
            ref = path.rsplit("/", 1)[1]
            t = self.tx.get(ref)
            if not t:
                return httpx.Response(self.unknown_ref_status, json={"status": False, "message": "Transaction reference not found."})
            t = {**t, **self.verify_override.get("*", {}), **self.verify_override.get(ref, {})}
            return httpx.Response(200, json={"status": True, "message": "Verification successful", "data": t})
        return httpx.Response(404, json={"status": False, "message": "unknown endpoint " + path})


FAKE = FakePaystack()
paystack._TRANSPORT = httpx.MockTransport(FAKE.handler)

_seq = [0]
_clients = {}


def mk_user(tier="free"):
    _seq[0] += 1
    with closing(auth.db()) as con:
        uid = con.insert("INSERT INTO users(email,pw_hash,tier,created_at) VALUES(?,?,?,?)", (f"p{_seq[0]}@test.example", "hash", tier, "2026-01-01T00:00:00"))
        con.commit()
    return uid


def client(uid):
    if uid not in _clients:
        c = TestClient(APP)
        c.__enter__()
        c.cookies.set(auth.COOKIE, auth.make_token(uid, "hash"))
        _clients[uid] = c
    return _clients[uid]


ANON = TestClient(APP)
ANON.__enter__()


def sign(raw: bytes, secret=None) -> str:
    """Independent of app code: HMAC-SHA512 of the raw body with the secret key, hex."""
    return hmac.new((secret or current_key()).encode(), raw, hashlib.sha512).hexdigest()


def post_webhook(ref, event="charge.success", status="success", sig=None, raw=None, extra=None):
    t = FAKE.tx.get(ref, {})
    body = {"event": event, "data": {"id": t.get("id", 1), "reference": ref, "status": status, "amount": t.get("amount"), "currency": "NGN", **(extra or {})}}
    data = raw if raw is not None else json.dumps(body).encode()
    return ANON.post("/api/pay/paystack/webhook", content=data, headers={"x-paystack-signature": sign(data) if sig is None else sig,
                                                                         "content-type": "application/json"})


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


def buy(uid, product="boost"):
    r = client(uid).post("/api/pay/paystack/order", json={"product": product, "accepted": True})
    assert r.status_code == 200, r.text
    o = r.json()["order"]
    return o["id"], o["ref"], o


def pay(ref, status="success"):
    FAKE.set(ref, status=status)


def process(oid, source="t"):
    return asyncio.run(paystack.process_order(oid, source))


def set_mode(m):
    with closing(credits.db()) as con:
        credits._put(con, "paystack_enabled", paystack.MODES[m])
        con.commit()
    paystack._switch["ts"] = 0


def set_rate(v):
    with closing(credits.db()) as con:
        credits._put(con, "ngn_rate", str(v))
        con.commit()
    paystack._switch["ts"] = 0


def setUpModule():
    with closing(credits.db()) as con:
        credits._put(con, "enabled", "1")
        credits._put(con, "ngn_rate", str(RATE))
        credits._put(con, "pay_trc20", "TManualWallet")
        con.commit()
    credits._cache["ts"] = 0
    set_mode("public")


def tearDownModule():
    for c in list(_clients.values()) + [ANON]:
        c.__exit__(None, None, None)
    shutil.rmtree(TMP, ignore_errors=True)


class Base(unittest.TestCase):
    def setUp(self):
        use_key("live")
        set_mode("public")
        set_rate(RATE)
        credits._cache.update(ts=0.0)
        FAKE.down, FAKE.init_override, FAKE.init_hook, FAKE.verify_override = False, {}, None, {}


# ---------------------------------------------------------------- 1. the webhook signature
class SignatureTests(Base):
    def test_valid_and_invalid_signatures(self):
        raw = b'{"event":"charge.success","data":{"reference":"X"}}'
        k = current_key()
        self.assertTrue(paystack.verify_signature(raw, sign(raw), k))
        self.assertTrue(paystack.verify_signature(raw, sign(raw).upper(), k))                # hex case does not matter
        self.assertFalse(paystack.verify_signature(raw, sign(raw + b" "), k))                # body changed
        self.assertFalse(paystack.verify_signature(raw, sign(raw, "sk_live_other"), k))     # signed with another key
        self.assertFalse(paystack.verify_signature(raw, "", k))
        self.assertFalse(paystack.verify_signature(raw, None, k))
        self.assertFalse(paystack.verify_signature(raw, sign(raw), ""))                      # no key configured: nothing verifies

    def test_signature_is_over_the_raw_bytes_not_a_reserialisation(self):
        raw = b'{ "event" : "charge.success",   "data":{"reference":"X"} }'
        self.assertTrue(paystack.verify_signature(raw, sign(raw), current_key()))
        self.assertFalse(paystack.verify_signature(raw, sign(json.dumps(json.loads(raw)).encode()), current_key()))


# ---------------------------------------------------------------- 2. configuration, modes and who may pay
class ConfigurationTests(Base):
    def test_problems_names_only_never_values(self):
        os.environ.pop("PAYSTACK_SECRET_KEY")
        try:
            self.assertIn("PAYSTACK_SECRET_KEY is not set", paystack.problems())
            os.environ["PAYSTACK_SECRET_KEY"] = "not-a-paystack-key"
            p = " ".join(paystack.problems())
            self.assertIn("sk_test_", p)
            self.assertNotIn("not-a-paystack-key", p)
        finally:
            use_key("live")

    def test_live_key_is_refused_unless_explicitly_allowed(self):
        del os.environ["PAYSTACK_ALLOW_LIVE"]
        try:
            self.assertTrue(any("LIVE key" in p for p in paystack.problems()))
            self.assertFalse(paystack.enabled({"tier": "admin"}))
            u = mk_user()
            self.assertEqual(client(u).post("/api/pay/paystack/order", json={"product": "boost", "accepted": True}).status_code, 409)
            self.assertEqual(client(u).get("/api/billing").json()["paystack"], {"enabled": False})
        finally:
            os.environ["PAYSTACK_ALLOW_LIVE"] = "1"
        self.assertEqual(paystack.problems(), [])

    def test_public_key_must_match_the_secret_keys_mode(self):
        os.environ["PAYSTACK_PUBLIC_KEY"] = "pk_test_FAKE"
        try:
            self.assertTrue(any("PAYSTACK_PUBLIC_KEY" in p for p in paystack.problems()))
            os.environ["PAYSTACK_PUBLIC_KEY"] = "pk_live_FAKE"
            self.assertEqual(paystack.problems(), [])
        finally:
            os.environ.pop("PAYSTACK_PUBLIC_KEY")

    def test_http_callback_base_is_refused(self):
        old = os.environ["DRIVERLINE_BASE_URL"]
        os.environ["DRIVERLINE_BASE_URL"] = "http://tradelens.example"
        try:
            self.assertTrue(any("https" in p for p in paystack.problems()))
            u = mk_user()
            self.assertEqual(client(u).post("/api/pay/paystack/order", json={"product": "boost", "accepted": True}).status_code, 409)
        finally:
            os.environ["DRIVERLINE_BASE_URL"] = old

    def test_urls_use_the_public_domain(self):
        self.assertEqual(paystack.webhook_url(), "https://tradelens.example/api/pay/paystack/webhook")
        self.assertEqual(paystack.callback_url(), "https://tradelens.example/paystack.html")

    def test_off_means_nobody_and_no_rate_means_nobody(self):
        u, admin = mk_user(), mk_user("admin")
        set_mode("off")
        for who in (u, admin):
            self.assertEqual(client(who).post("/api/pay/paystack/order", json={"product": "boost", "accepted": True}).status_code, 409)
        set_mode("public")
        set_rate("")
        self.assertEqual(client(u).post("/api/pay/paystack/order", json={"product": "boost", "accepted": True}).status_code, 409)
        self.assertEqual(client(u).get("/api/billing").json()["paystack"], {"enabled": False})

    def test_public_mode_needs_billing_open_but_the_pilot_does_not(self):
        u, admin = mk_user(), mk_user("admin")
        with closing(credits.db()) as con:
            credits._put(con, "enabled", "0")
            con.commit()
        credits._cache["ts"] = 0
        try:
            self.assertFalse(paystack.enabled({"tier": "free"}))
            set_mode("admin")
            self.assertFalse(paystack.enabled({"tier": "free"}))
            self.assertTrue(paystack.enabled({"tier": "admin"}))
            self.assertEqual(client(admin).get("/api/billing").json()["enabled"], True)      # the admin pilot opens the shop for the admin
            self.assertEqual(client(u).get("/api/billing").json()["enabled"], False)
        finally:
            with closing(credits.db()) as con:
                credits._put(con, "enabled", "1")
                con.commit()
            credits._cache["ts"] = 0

    def test_member_sees_naira_prices_from_the_server(self):
        u = mk_user()
        info = client(u).get("/api/billing").json()["paystack"]
        self.assertTrue(info["enabled"])
        self.assertFalse(info["test"])
        self.assertEqual(info["naira"]["boost"], 10 * RATE)
        self.assertEqual(info["naira"]["pro"], 30 * RATE)
        self.assertEqual(set(info["naira"]), set(credits.CATALOG))
        self.assertNotIn(LIVE_KEY, json.dumps(info))


class TestKeyIsolationTests(Base):
    """A test key must never be usable by members: test payments can only ever touch an admin account."""
    def setUp(self):
        super().setUp()
        use_key("test")

    def tearDown(self):
        use_key("live")

    def test_members_cannot_pay_with_a_test_key_in_any_mode(self):
        u = mk_user()
        client(u).get("/api/billing")                      # grants the one-time welcome signals, unrelated to Paystack
        before = balance(u)
        for m in ("admin", "public"):
            set_mode(m)
            self.assertFalse(paystack.enabled({"tier": "free"}))
            self.assertEqual(client(u).post("/api/pay/paystack/order", json={"product": "boost", "accepted": True}).status_code, 409, m)
            self.assertEqual(client(u).get("/api/billing").json()["paystack"], {"enabled": False}, m)
        self.assertEqual(sql("SELECT * FROM billing_orders WHERE user_id=?", u), [])
        self.assertEqual(balance(u), before)

    def test_admin_can_test_and_everything_is_tagged_as_test(self):
        admin = mk_user("admin")
        set_mode("admin")
        info = client(admin).get("/api/billing").json()["paystack"]
        self.assertTrue(info["enabled"] and info["test"] and info["pilot"])
        oid, ref, o = buy(admin, "topup")
        self.assertTrue(ref.startswith("TLN-T-"))
        self.assertTrue(o["test"])
        self.assertEqual((row(oid)["provider"], row(oid)["pay_network"]), ("paystack-test", "test"))
        self.assertEqual(FAKE.tx[ref]["domain"], "test")
        pay(ref)
        process(oid)
        self.assertEqual((row(oid)["status"], balance(admin)), ("approved", 267))
        self.assertIn("TLN-T-", sql("SELECT note FROM credit_ledger WHERE user_id=? AND kind='purchase'", admin)[0]["note"])

    def test_public_mode_cannot_be_switched_on_with_a_test_key(self):
        admin = mk_user("admin")
        r = client(admin).post("/api/admin/pay/paystack/toggle", json={"mode": "public"})
        self.assertEqual(r.status_code, 409, r.text)
        self.assertIn("live", r.json()["detail"])
        self.assertEqual(client(admin).post("/api/admin/pay/paystack/toggle", json={"mode": "admin"}).status_code, 200)

    def test_a_test_order_cannot_be_fulfilled_under_a_live_key_or_the_reverse(self):
        admin = mk_user("admin")
        set_mode("admin")
        oid, ref, _ = buy(admin, "topup")
        pay(ref)
        use_key("live")
        paystack._switch["ts"] = 0
        os.environ["PAYSTACK_ALLOW_LIVE"] = "1"
        FAKE.set(ref, domain="live")
        r = process(oid)
        self.assertEqual(r["result"], "review", r)
        self.assertEqual(balance(admin), 0)
        self.assertIn("different key mode", row(oid)["review_reason"])

    def test_a_live_transaction_for_a_test_order_is_not_fulfilled(self):
        admin = mk_user("admin")
        set_mode("admin")
        oid, ref, _ = buy(admin, "topup")
        pay(ref)
        FAKE.set(ref, domain="live")
        self.assertEqual(process(oid)["result"], "review")
        self.assertEqual(balance(admin), 0)


# ---------------------------------------------------------------- 3. creating an order
class CreateOrderTests(Base):
    def test_order_is_priced_on_the_server_and_stored_before_paystack_is_called(self):
        u = mk_user()
        seen = {}

        def hook(body):
            seen["rows"] = sql("SELECT status, pay_address, amount_usd, amount_local, currency, method, user_id FROM billing_orders "
                               "WHERE ref=?", body["reference"])
        FAKE.init_hook = hook
        r = client(u).post("/api/pay/paystack/order", json={"product": "boost", "accepted": True,
                                                            "amount": 1, "amount_usd": 0.01, "price": 1, "naira": 1, "user_id": 999, "signals": 99999, "days": 400})
        self.assertEqual(r.status_code, 200, r.text)
        o = r.json()["order"]
        # 1. a pending order existed, with the right owner and price, before Paystack heard about it
        self.assertEqual(len(seen["rows"]), 1)
        self.assertEqual((seen["rows"][0]["status"], seen["rows"][0]["pay_address"]), ("pending", None))
        self.assertEqual((seen["rows"][0]["user_id"], seen["rows"][0]["currency"], seen["rows"][0]["method"]), (u, "NGN", "paystack"))
        # 2. everything came from the catalogue and the rate, nothing from the browser
        call = [c for c in FAKE.calls if c[1] == "/transaction/initialize"][-1]
        body = json.loads(call[2])
        self.assertEqual(body["amount"], 10 * RATE * 100)                        # kobo
        self.assertEqual(body["currency"], "NGN")
        self.assertEqual(body["email"], f"p{_seq[0]}@test.example")
        self.assertEqual(body["reference"], o["ref"])
        self.assertEqual(body["callback_url"], "https://tradelens.example/paystack.html")
        self.assertEqual(body["metadata"]["order_id"], o["id"])
        self.assertEqual(body["metadata"]["user_id"], u)
        r_ = row(o["id"])
        self.assertEqual((r_["amount_usd"], r_["amount_local"], r_["pay_amount"], r_["product"]), (10.0, 10.0 * RATE, 10.0 * RATE, "boost"))
        self.assertEqual((o["amount_naira"], o["status"], o["outcome"]), (10 * RATE, "pending", "pending"))
        self.assertTrue(o["checkout_url"].startswith("https://checkout.paystack.com/"))
        self.assertTrue(o["ref"].startswith("TLN-") and not o["ref"].startswith("TLN-T-"))
        self.assertEqual(balance(u), 0)                                          # nothing is credited by creating an order

    def test_every_attempt_gets_a_unique_reference(self):
        refs = set()
        for _ in range(4):
            u = mk_user()
            refs.add(buy(u, "topup")[1])
        self.assertEqual(len(refs), 4)

    def test_unknown_product_missing_terms_and_unauthenticated(self):
        u = mk_user()
        c = client(u)
        self.assertEqual(c.post("/api/pay/paystack/order", json={"product": "nope", "accepted": True}).status_code, 422)
        self.assertEqual(c.post("/api/pay/paystack/order", json={"product": "boost"}).status_code, 422)
        self.assertEqual(c.post("/api/pay/paystack/order", json={"product": "boost", "accepted": "yes"}).status_code, 422)
        self.assertEqual(c.post("/api/pay/paystack/order", content=b"not json").status_code, 400)
        self.assertEqual(ANON.post("/api/pay/paystack/order", json={"product": "boost", "accepted": True}).status_code, 401)
        self.assertEqual(sql("SELECT * FROM billing_orders WHERE user_id=?", u), [])

    def test_double_click_returns_the_same_order(self):
        u = mk_user()
        a, b = buy(u)[:2], buy(u)[:2]
        self.assertEqual(a, b)
        self.assertEqual(len(sql("SELECT * FROM billing_orders WHERE user_id=?", u)), 1)

    def test_a_different_product_is_a_different_order(self):
        u = mk_user()
        self.assertNotEqual(buy(u, "topup")[0], buy(u, "boost")[0])

    def test_open_order_limit_and_rate_limit(self):
        u = mk_user()
        for p in ("topup", "boost", "vault"):
            buy(u, p)
        r = client(u).post("/api/pay/paystack/order", json={"product": "plus", "accepted": True})
        self.assertEqual(r.status_code, 429, r.text)
        self.assertEqual(len(sql("SELECT * FROM billing_orders WHERE user_id=?", u)), 3)

    def test_overdue_unpaid_orders_are_reclaimed_so_the_member_is_not_locked_out(self):
        u = mk_user()
        ids = [buy(u, p)[0] for p in ("topup", "boost", "vault")]
        past = int(time.time()) - 3600
        sql("UPDATE billing_orders SET expires_at=? WHERE user_id=?", past, u)
        self.assertEqual(client(u).post("/api/pay/paystack/order", json={"product": "plus", "accepted": True}).status_code, 200)
        self.assertTrue(all(row(i)["status"] == "expired" for i in ids))

    def test_an_overdue_order_that_was_actually_paid_is_credited_not_expired_by_reclaim(self):
        u = mk_user()
        ids = [buy(u, p) for p in ("topup", "boost", "vault")]
        pay(ids[0][1])
        sql("UPDATE billing_orders SET expires_at=? WHERE user_id=?", int(time.time()) - 3600, u)
        client(u).post("/api/pay/paystack/order", json={"product": "plus", "accepted": True})
        self.assertEqual(row(ids[0][0])["status"], "approved")
        self.assertEqual(balance(u), 267)

    def test_paystack_outage_creates_no_usable_order(self):
        u = mk_user()
        FAKE.down = True
        r = client(u).post("/api/pay/paystack/order", json={"product": "boost", "accepted": True})
        self.assertEqual(r.status_code, 502)
        self.assertNotIn("checkout", r.text)
        rows = sql("SELECT * FROM billing_orders WHERE user_id=?", u)
        self.assertEqual([(x["status"], x["pay_address"]) for x in rows], [("failed", None)])
        FAKE.down = False
        self.assertEqual(client(u).post("/api/pay/paystack/order", json={"product": "boost", "accepted": True}).status_code, 200)

    def test_bad_paystack_replies_are_not_shown_to_members(self):
        for bad in ({"reference": "SOMETHING-ELSE"}, {"authorization_url": "https://evil.example/pay"}, {"authorization_url": "http://checkout.paystack.com/x"},
                    {"authorization_url": "https://paystack.com.evil.example/x"}, {"authorization_url": "javascript:alert(1)"}, {"authorization_url": ""}):
            u = mk_user()
            FAKE.init_override = bad
            r = client(u).post("/api/pay/paystack/order", json={"product": "boost", "accepted": True})
            self.assertEqual(r.status_code, 502, bad)
            self.assertNotIn("evil", r.text)
            self.assertEqual([x["status"] for x in sql("SELECT status FROM billing_orders WHERE user_id=?", u)], ["failed"], bad)

    def test_partner_member_pays_partner_price_in_naira(self):
        u = mk_user()
        orig = credits._price_fields
        credits._price_fields = lambda user, product: {"usd": 20.0 if product == "pro" else float(credits.CATALOG[product]["usd"])}
        try:
            oid, ref, o = buy(u, "pro")
        finally:
            credits._price_fields = orig
        self.assertEqual(o["amount_naira"], 20 * RATE)
        self.assertEqual(json.loads([c for c in FAKE.calls if c[1] == "/transaction/initialize"][-1][2])["amount"], 20 * RATE * 100)

    def test_naira_follows_the_same_rounding_as_the_manual_flow(self):
        set_rate("1523.45")
        u = mk_user()
        _, _, o = buy(u, "topup")
        self.assertEqual(o["amount_naira"], round(5 * 1523.45))


# ---------------------------------------------------------------- 4. successful payments deliver exactly what was bought
class FulfilmentTests(Base):
    def test_token_purchase_by_webhook(self):
        u = mk_user()
        oid, ref, _ = buy(u, "boost")
        pay(ref)
        r = post_webhook(ref)
        self.assertEqual(r.status_code, 200)
        asyncio.run(paystack.drain())
        o = row(oid)
        self.assertEqual((o["status"], balance(u)), ("approved", 550))
        self.assertEqual((o["provider_status"], o["actually_paid"], o["provider_payment_id"]), ("success", 10.0 * RATE, f"ps:{FAKE.tx[ref]['id']}"))
        self.assertTrue(o["fulfilled_at"])
        self.assertEqual(sql("SELECT delta FROM credit_ledger WHERE user_id=? AND kind='purchase'", u), [{"delta": 550}])
        self.assertEqual(sql("SELECT * FROM billing_subs WHERE user_id=?", u), [])
        kinds = [e["kind"] for e in events(oid)]
        for k in ("created", "provider_init", "webhook", "fulfilled"):
            self.assertIn(k, kinds)

    def test_every_pack_gets_its_catalogue_quantity(self):
        for product in ("topup", "boost", "vault"):
            u = mk_user()
            oid, ref, _ = buy(u, product)
            pay(ref)
            process(oid)
            self.assertEqual(balance(u), credits.CATALOG[product]["signals"], product)

    def test_subscription_purchase_activates_notifications_for_30_days(self):
        u = mk_user()
        user = {"id": u, "tier": "free"}
        self.assertFalse(credits.entitled(user, "notify"))
        oid, ref, _ = buy(u, "plus")
        pay(ref)
        process(oid)
        now = time.time()
        subs = sql("SELECT * FROM billing_subs WHERE user_id=?", u)
        self.assertEqual(len(subs), 1)
        self.assertEqual((subs[0]["plan"], subs[0]["order_id"]), ("plus", oid))
        self.assertAlmostEqual(subs[0]["ends_at"] - subs[0]["starts_at"], 30 * 86400, delta=2)
        self.assertAlmostEqual(subs[0]["starts_at"], now, delta=10)
        self.assertEqual(balance(u), 550)
        self.assertTrue(credits.entitled(user, "notify"))
        self.assertFalse(credits.entitled(user, "mt5"))                          # Plus has no MT5

    def test_mt5_plan_purchase_activates_the_mt5_connection(self):
        u = mk_user()
        user = {"id": u, "tier": "free"}
        self.assertFalse(credits.entitled(user, "mt5"))
        oid, ref, _ = buy(u, "pro")
        pay(ref)
        process(oid)
        self.assertTrue(credits.entitled(user, "mt5"))
        self.assertTrue(credits.entitled(user, "notify"))
        self.assertEqual(balance(u), 550)

    def test_renewal_extends_from_the_end_of_the_current_plan_not_from_today(self):
        u = mk_user()
        for _ in range(2):
            oid, ref, _ = buy(u, "plus")
            pay(ref)
            process(oid)
            sql("UPDATE billing_orders SET status='approved' WHERE id=?", oid)      # so the next purchase starts a fresh order
        first, second = sql("SELECT * FROM billing_subs WHERE user_id=? ORDER BY id", u)
        self.assertEqual(second["starts_at"], first["ends_at"])
        self.assertAlmostEqual(second["ends_at"] - second["starts_at"], 30 * 86400, delta=2)
        self.assertEqual(balance(u), 1100)

    def test_callback_page_status_check_fulfils_when_the_webhook_never_arrives(self):
        u = mk_user()
        oid, ref, _ = buy(u)
        pay(ref)
        r = client(u).get(f"/api/pay/paystack/status?reference={ref}&trxref={ref}&refresh=1")
        self.assertEqual(r.status_code, 200, r.text)
        o = r.json()["order"]
        self.assertEqual((o["status"], o["outcome"], o["balance"], o["signals"]), ("approved", "success", 550, 550))
        self.assertIsNone(o["checkout_url"])

    def test_status_without_refresh_never_asks_paystack_and_never_credits(self):
        u = mk_user()
        oid, ref, _ = buy(u)
        pay(ref)
        n = len(FAKE.calls)
        r = client(u).get(f"/api/pay/paystack/status?reference={ref}")
        self.assertEqual(r.json()["order"]["status"], "pending")
        self.assertEqual((len(FAKE.calls), balance(u)), (n, 0))

    def test_lost_webhook_is_found_by_the_sweep(self):
        u = mk_user()
        oid, ref, _ = buy(u)
        pay(ref)
        out = asyncio.run(paystack.sweep_once())
        self.assertGreaterEqual(out["checked"], 1)
        self.assertEqual((row(oid)["status"], balance(u)), ("approved", 550))

    def test_admin_reconcile_applies_what_paystack_says(self):
        u, admin = mk_user(), mk_user("admin")
        oid, ref, _ = buy(u)
        pay(ref)
        r = client(admin).post(f"/api/admin/pay/paystack/order/{oid}/reconcile")
        self.assertEqual((r.status_code, r.json()["status"]), (200, "approved"), r.text)
        self.assertEqual(balance(u), 550)

    def test_signed_webhook_alone_cannot_fulfil(self):
        """The webhook claims success; Paystack's own API says the transaction is not paid. Nothing may be credited."""
        u = mk_user()
        oid, ref, _ = buy(u)
        FAKE.set(ref, id=FAKE.tx[ref]["id"])
        self.assertEqual(post_webhook(ref, status="success", extra={"amount": 1500000}).status_code, 200)
        asyncio.run(paystack.drain())
        self.assertEqual((row(oid)["status"], balance(u)), ("pending", 0))


class DuplicateAndConcurrencyTests(Base):
    def test_repeated_webhooks_and_callbacks_credit_once(self):
        u = mk_user()
        oid, ref, _ = buy(u, "pro")
        pay(ref)
        for _ in range(5):
            self.assertEqual(post_webhook(ref).status_code, 200)
            asyncio.run(paystack.drain())
            client(u).get(f"/api/pay/paystack/status?reference={ref}&refresh=1")
            process(oid)
        self.assertEqual(balance(u), 550)
        self.assertEqual(len(sql("SELECT * FROM billing_subs WHERE user_id=?", u)), 1)
        self.assertEqual(len(sql("SELECT * FROM credit_ledger WHERE user_id=? AND kind='purchase'", u)), 1)
        self.assertEqual(len(events(oid, "fulfilled")), 1)

    def test_parallel_fulfilment_credits_once(self):
        u = mk_user()
        oid, ref, _ = buy(u, "pro")
        pay(ref)
        d = dict(FAKE.tx[ref])
        results, barrier = [], threading.Barrier(10)

        def go():
            barrier.wait()
            results.append(paystack.apply_transaction(oid, d, "race")["result"])
        ts = [threading.Thread(target=go) for _ in range(10)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertEqual(results.count("fulfilled"), 1, results)
        self.assertEqual(balance(u), 550)
        self.assertEqual(len(sql("SELECT * FROM billing_subs WHERE user_id=?", u)), 1)
        self.assertEqual(len(sql("SELECT * FROM credit_ledger WHERE user_id=? AND kind='purchase'", u)), 1)

    def test_parallel_webhooks_over_http(self):
        u = mk_user()
        oid, ref, _ = buy(u)
        pay(ref)
        codes = []
        ts = [threading.Thread(target=lambda: codes.append(post_webhook(ref).status_code)) for _ in range(6)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        asyncio.run(paystack.drain())
        self.assertEqual(set(codes), {200})
        self.assertEqual((row(oid)["status"], balance(u)), ("approved", 550))

    def test_webhook_racing_the_callback_page_and_the_sweep(self):
        u = mk_user()
        oid, ref, _ = buy(u, "plus")
        pay(ref)
        errs = []
        barrier = threading.Barrier(3)

        def a():
            barrier.wait()
            post_webhook(ref)

        def b():
            barrier.wait()
            client(u).get(f"/api/pay/paystack/status?reference={ref}&refresh=1")

        def c():
            barrier.wait()
            try:
                asyncio.run(paystack.sweep_once())
            except Exception as e:                      # noqa: BLE001
                errs.append(e)
        ts = [threading.Thread(target=f) for f in (a, b, c)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        asyncio.run(paystack.drain())
        self.assertEqual(errs, [])
        self.assertEqual((row(oid)["status"], balance(u)), ("approved", 550))
        self.assertEqual(len(sql("SELECT * FROM billing_subs WHERE user_id=?", u)), 1)

    def test_fulfil_refuses_an_order_that_changed_state_meanwhile(self):
        for state in ("rejected", "review", "expired", "failed", "cancelled", "refunded", "approved"):
            u = mk_user()
            oid, ref, _ = buy(u)
            pay(ref)
            sql("UPDATE billing_orders SET status=? WHERE id=?", state, oid)
            self.assertEqual(paystack._fulfil(oid, "race", dict(FAKE.tx[ref])), "lost_race", state)
            self.assertEqual((row(oid)["status"], balance(u)), (state, 0), state)

    def test_the_ledger_alone_prevents_a_double_credit(self):
        u = mk_user()
        oid, ref, _ = buy(u)
        with closing(credits.db()) as con:
            self.assertTrue(credits.grant_product(con, u, "boost", oid, "X"))
            con.commit()
        pay(ref)
        d = dict(FAKE.tx[ref])
        self.assertEqual(paystack._fulfil(oid, "t", d), "duplicate")              # credit already existed: recorded, not repeated
        self.assertEqual(balance(u), 550)
        self.assertEqual(row(oid)["status"], "approved")

    def test_admin_and_automatic_paths_cannot_both_credit(self):
        u, admin = mk_user(), mk_user("admin")
        oid, ref, _ = buy(u)
        FAKE.set(ref, amount=100)                                                  # wrong amount -> review
        pay(ref)
        process(oid)
        self.assertEqual(row(oid)["status"], "review")
        FAKE.set(ref, amount=10 * RATE * 100)
        process(oid)                                                                # review is never re-opened automatically
        self.assertEqual(client(admin).post(f"/api/admin/pay/paystack/order/{oid}/resolve", json={"action": "approve", "note": "checked"}).status_code, 200)
        process(oid)
        client(admin).post(f"/api/admin/pay/paystack/order/{oid}/resolve", json={"action": "approve", "note": "again"})
        self.assertEqual(balance(u), 550)

    def test_the_same_paystack_transaction_cannot_settle_two_orders(self):
        u1, u2 = mk_user(), mk_user()
        o1, r1, _ = buy(u1)
        o2, r2, _ = buy(u2)
        pay(r1)
        pay(r2)
        FAKE.set(r2, id=FAKE.tx[r1]["id"])                                          # a (hypothetical) provider reply reusing the id
        process(o1)
        self.assertEqual(process(o2)["result"], "review")
        self.assertEqual((balance(u1), balance(u2)), (550, 0))


# ---------------------------------------------------------------- 5. not-yet-paid, failed, abandoned, pending, cancelled
class NonSuccessTests(Base):
    def test_abandoned_means_not_paid_yet_and_changes_nothing(self):
        u = mk_user()
        oid, ref, _ = buy(u)
        self.assertEqual(FAKE.tx[ref]["status"], "abandoned")
        self.assertEqual(process(oid)["result"], "waiting")
        o = client(u).get(f"/api/pay/paystack/status?reference={ref}&refresh=1").json()["order"]
        self.assertEqual((o["status"], o["outcome"], balance(u)), ("pending", "pending", 0))
        self.assertTrue(o["checkout_url"])                                         # the member can still go and pay

    def test_failed_attempt_is_shown_as_failed_and_can_still_succeed_on_retry(self):
        u = mk_user()
        oid, ref, _ = buy(u)
        pay(ref, "failed")
        FAKE.set(ref, gateway_response="Declined")
        o = client(u).get(f"/api/pay/paystack/status?reference={ref}&refresh=1").json()["order"]
        self.assertEqual((o["status"], o["outcome"], balance(u)), ("pending", "failed", 0))
        self.assertTrue(o["checkout_url"])
        self.assertEqual(len(events(oid, "provider_failed")), 1)
        pay(ref, "success")                                                         # the customer retried on the checkout page
        process(oid)
        self.assertEqual((row(oid)["status"], balance(u)), ("approved", 550))

    def test_failed_and_never_retried_closes_as_failed_after_expiry(self):
        u = mk_user()
        oid, ref, _ = buy(u)
        pay(ref, "failed")
        sql("UPDATE billing_orders SET expires_at=? WHERE id=?", int(time.time()) - 3600, oid)
        asyncio.run(paystack.sweep_once())
        self.assertEqual((row(oid)["status"], balance(u)), ("failed", 0))
        o = client(u).get(f"/api/pay/paystack/status?reference={ref}").json()["order"]
        self.assertEqual((o["outcome"], o["checkout_url"]), ("failed", None))

    def test_pending_bank_transfer_waits_and_is_credited_when_it_lands(self):
        u = mk_user()
        oid, ref, _ = buy(u, "plus")
        for st in ("ongoing", "pending", "processing", "queued"):
            pay(ref, st)
            self.assertEqual(process(oid)["result"], "waiting", st)
            self.assertEqual((row(oid)["status"], balance(u)), ("pending", 0), st)
        # delayed confirmation: the order passes its expiry while the bank transfer is still settling
        sql("UPDATE billing_orders SET expires_at=? WHERE id=?", int(time.time()) - 3600, oid)
        asyncio.run(paystack.sweep_once())
        self.assertEqual(row(oid)["status"], "pending")                             # NOT expired: money may be on its way
        pay(ref, "success")
        asyncio.run(paystack.sweep_once())
        self.assertEqual((row(oid)["status"], balance(u)), ("approved", 550))

    def test_a_transfer_stuck_in_progress_for_a_day_goes_to_review_not_to_expiry(self):
        u = mk_user()
        oid, ref, _ = buy(u)
        pay(ref, "ongoing")
        sql("UPDATE billing_orders SET expires_at=? WHERE id=?", int(time.time()) - 2 * 86400, oid)
        process(oid)
        paystack._expire_if_due(oid)
        self.assertEqual(row(oid)["status"], "review")
        self.assertTrue(row(oid)["review_reason"].startswith("stuck"))

    def test_abandoned_order_expires_after_the_check(self):
        u = mk_user()
        oid, ref, _ = buy(u)
        sql("UPDATE billing_orders SET expires_at=? WHERE id=?", int(time.time()) - 3600, oid)
        asyncio.run(paystack.sweep_once())
        self.assertEqual((row(oid)["status"], balance(u)), ("expired", 0))
        o = client(u).get(f"/api/pay/paystack/status?reference={ref}").json()["order"]
        self.assertEqual((o["outcome"], o["checkout_url"]), ("expired", None))

    def test_an_order_is_not_expired_when_paystack_cannot_be_reached(self):
        u = mk_user()
        oid, ref, _ = buy(u)
        sql("UPDATE billing_orders SET expires_at=? WHERE id=?", int(time.time()) - 3600, oid)
        FAKE.down = True
        out = asyncio.run(paystack.sweep_once())
        self.assertGreaterEqual(out["unavailable"], 1)
        self.assertEqual(row(oid)["status"], "pending")
        FAKE.down = False
        pay(ref)
        asyncio.run(paystack.sweep_once())
        self.assertEqual((row(oid)["status"], balance(u)), ("approved", 550))

    def test_payment_after_the_order_expired_goes_to_review_not_credit(self):
        u = mk_user()
        oid, ref, _ = buy(u)
        sql("UPDATE billing_orders SET expires_at=? WHERE id=?", int(time.time()) - 3600, oid)
        asyncio.run(paystack.sweep_once())
        self.assertEqual(row(oid)["status"], "expired")
        pay(ref)
        self.assertEqual(process(oid)["result"], "review")
        self.assertEqual((row(oid)["status"], balance(u)), ("review", 0))
        self.assertTrue(row(oid)["review_reason"].startswith("paid_after_close"))

    def test_member_can_cancel_an_unpaid_order(self):
        u = mk_user()
        oid, ref, _ = buy(u)
        r = client(u).post(f"/api/pay/paystack/order/{oid}/cancel")
        self.assertEqual((r.status_code, r.json()["order"]["outcome"]), (200, "cancelled"), r.text)
        self.assertEqual((row(oid)["status"], balance(u)), ("cancelled", 0))
        self.assertIsNone(r.json()["order"]["checkout_url"])

    def test_cancel_honours_a_payment_that_actually_went_through(self):
        u = mk_user()
        oid, ref, _ = buy(u)
        pay(ref)
        r = client(u).post(f"/api/pay/paystack/order/{oid}/cancel")
        self.assertEqual((r.status_code, r.json()["order"]["outcome"]), (200, "success"), r.text)
        self.assertEqual((row(oid)["status"], balance(u)), ("approved", 550))

    def test_cancel_when_paystack_is_unreachable_changes_nothing(self):
        u = mk_user()
        oid, ref, _ = buy(u)
        FAKE.down = True
        self.assertEqual(client(u).post(f"/api/pay/paystack/order/{oid}/cancel").status_code, 502)
        FAKE.down = False
        self.assertEqual(row(oid)["status"], "pending")

    def test_a_payment_after_cancellation_goes_to_review(self):
        u = mk_user()
        oid, ref, _ = buy(u)
        client(u).post(f"/api/pay/paystack/order/{oid}/cancel")
        pay(ref)
        self.assertEqual(process(oid)["result"], "review")
        self.assertEqual((row(oid)["status"], balance(u)), ("review", 0))

    def test_reversal_before_fulfilment_goes_to_review_after_fulfilment_is_flagged_only(self):
        u = mk_user()
        oid, ref, _ = buy(u)
        pay(ref, "reversed")
        self.assertEqual(process(oid)["result"], "review")
        self.assertEqual(balance(u), 0)
        u2 = mk_user()
        oid2, ref2, _ = buy(u2)
        pay(ref2)
        process(oid2)
        pay(ref2, "reversed")
        self.assertEqual(process(oid2)["result"], "already_final")
        self.assertEqual((row(oid2)["status"], balance(u2)), ("approved", 550))     # never clawed back automatically
        self.assertEqual(len(events(oid2, "anomaly")), 1)

    def test_unknown_status_is_left_alone(self):
        u = mk_user()
        oid, ref, _ = buy(u)
        pay(ref, "something_new")
        self.assertEqual(process(oid)["result"], "unknown_status")
        self.assertEqual(row(oid)["status"], "pending")


# ---------------------------------------------------------------- 6. things that do not fit the order
class MismatchTests(Base):
    def paid_with(self, **over):
        u = mk_user()
        oid, ref, _ = buy(u, "boost")
        pay(ref)
        FAKE.set(ref, **over)
        return u, oid, ref

    def assert_review(self, u, oid, why):
        r = process(oid)
        self.assertEqual(r["result"], "review", r)
        self.assertEqual((row(oid)["status"], balance(u)), ("review", 0))
        self.assertIn(why, row(oid)["review_reason"])
        self.assertEqual(sql("SELECT * FROM billing_subs WHERE user_id=?", u), [])

    def test_underpaid(self):
        self.assert_review(*self.paid_with(amount=10 * RATE * 100 - 100)[:2], "amount")

    def test_overpaid_is_held_for_a_person(self):
        self.assert_review(*self.paid_with(amount=10 * RATE * 100 + 100)[:2], "amount")

    def test_wrong_currency(self):
        self.assert_review(*self.paid_with(currency="USD")[:2], "currency")

    def test_amount_that_is_not_whole_kobo_or_missing(self):
        self.assert_review(*self.paid_with(amount=10 * RATE * 100 + 0.5)[:2], "amount")
        self.assert_review(*self.paid_with(amount=None)[:2], "amount")
        self.assert_review(*self.paid_with(amount="1500000")[:2], "amount")

    def test_metadata_for_another_order_or_user(self):
        u, oid, ref = self.paid_with()
        FAKE.set(ref, metadata={"order_id": oid + 1000, "user_id": u})
        self.assert_review(u, oid, "metadata order_id")
        u, oid, ref = self.paid_with()
        FAKE.set(ref, metadata={"order_id": oid, "user_id": u + 1000})
        self.assert_review(u, oid, "metadata user_id")

    def test_customer_email_of_somebody_else(self):
        u, oid, ref = self.paid_with(customer={"email": "someone.else@example.com"})
        self.assert_review(u, oid, "email")

    def test_missing_transaction_id(self):
        u, oid, ref = self.paid_with(id=None)
        self.assert_review(u, oid, "transaction id")

    def test_transaction_from_the_wrong_domain(self):
        self.assert_review(*self.paid_with(domain="test")[:2], "test")

    def test_a_reply_for_a_different_reference_is_ignored(self):
        u, oid, ref = self.paid_with(reference="TLN-SOMEONE-ELSE")
        r = process(oid)
        self.assertEqual(r["result"], "ignored_mismatch")
        self.assertEqual((row(oid)["status"], balance(u)), ("pending", 0))

    def test_email_comparison_ignores_case_and_spaces_only(self):
        u = mk_user()
        oid, ref, _ = buy(u)
        pay(ref)
        FAKE.set(ref, customer={"email": f"  P{_seq[0]}@TEST.example "})
        self.assertEqual(process(oid)["result"], "fulfilled")

    def test_unmatched_webhook_is_recorded_and_answered_200(self):
        r = post_webhook("TLN-DOESNOTEXIST", extra={"id": 5})
        self.assertEqual(r.status_code, 200)
        self.assertTrue([e for e in sql("SELECT * FROM payment_events WHERE kind='unmatched'") if "DOESNOTEXIST" in (e["detail"] or "")])

    def test_webhook_for_another_providers_reference_is_not_ours(self):
        u = mk_user()
        with closing(credits.db()) as con:
            con.insert("INSERT INTO billing_orders(user_id,product,method,amount_usd,amount_local,currency,ref,status,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                       (u, "boost", "nowpayments", 10, 10, "USDT", "TLP-NOWPAYREF1", "pending", int(time.time())))
            con.commit()
        self.assertEqual(post_webhook("TLP-NOWPAYREF1", extra={"id": 7}).status_code, 200)
        asyncio.run(paystack.drain())
        self.assertEqual((sql("SELECT status FROM billing_orders WHERE ref='TLP-NOWPAYREF1'")[0]["status"], balance(u)), ("pending", 0))


# ---------------------------------------------------------------- 7. the webhook endpoint itself
class WebhookEndpointTests(Base):
    def test_invalid_or_missing_signature_changes_nothing_and_asks_nothing(self):
        u = mk_user()
        oid, ref, _ = buy(u)
        pay(ref)
        n = len(FAKE.calls)
        for sig in ("", "deadbeef", sign(b"other"), sign(json.dumps({"x": 1}).encode(), "sk_live_wrong")):
            self.assertEqual(post_webhook(ref, sig=sig).status_code, 401)
        self.assertEqual(ANON.post("/api/pay/paystack/webhook", content=b"{}").status_code, 401)
        self.assertEqual(len(FAKE.calls), n, "an unsigned request must not even cause a call to Paystack")
        self.assertEqual((row(oid)["status"], balance(u)), ("pending", 0))
        self.assertEqual(events(oid, "webhook"), [])

    def test_a_valid_signature_with_a_tampered_body_is_refused(self):
        u = mk_user()
        oid, ref, _ = buy(u)
        pay(ref)
        good = json.dumps({"event": "charge.success", "data": {"reference": ref}}).encode()
        self.assertEqual(post_webhook(ref, raw=good.replace(b"charge", b"chargX"), sig=sign(good)).status_code, 401)
        self.assertEqual(balance(u), 0)

    def test_fails_closed_without_a_key_and_rejects_oversized_or_non_json(self):
        raw = b'{"event":"x"}'
        k = os.environ.pop("PAYSTACK_SECRET_KEY")
        try:
            self.assertEqual(ANON.post("/api/pay/paystack/webhook", content=raw, headers={"x-paystack-signature": sign(raw, k)}).status_code, 503)
        finally:
            os.environ["PAYSTACK_SECRET_KEY"] = k
        big = b'{"event":"x","pad":"' + b"a" * (paystack.MAX_BODY + 10) + b'"}'
        self.assertEqual(ANON.post("/api/pay/paystack/webhook", content=big, headers={"x-paystack-signature": sign(big)}).status_code, 413)
        junk = b"not json"
        self.assertEqual(ANON.post("/api/pay/paystack/webhook", content=junk, headers={"x-paystack-signature": sign(junk)}).status_code, 400)
        arr = b"[1,2]"
        self.assertEqual(ANON.post("/api/pay/paystack/webhook", content=arr, headers={"x-paystack-signature": sign(arr)}).status_code, 400)

    def test_events_without_a_reference_are_acknowledged_and_ignored(self):
        raw = json.dumps({"event": "transfer.success", "data": {"amount": 5}}).encode()
        self.assertEqual(ANON.post("/api/pay/paystack/webhook", content=raw, headers={"x-paystack-signature": sign(raw)}).status_code, 200)

    def test_provider_outage_during_the_webhook_is_retried_by_the_sweep(self):
        u = mk_user()
        oid, ref, _ = buy(u)
        pay(ref)
        FAKE.down = True
        self.assertEqual(post_webhook(ref).status_code, 200)
        asyncio.run(paystack.drain())
        self.assertEqual((row(oid)["status"], balance(u)), ("pending", 0))
        self.assertTrue(events(oid, "poll_error"))
        FAKE.down = False
        asyncio.run(paystack.sweep_once())
        self.assertEqual((row(oid)["status"], balance(u)), ("approved", 550))

    def test_a_retried_webhook_after_a_failed_check_can_still_succeed(self):
        u = mk_user()
        oid, ref, _ = buy(u)
        pay(ref)
        FAKE.down = True
        post_webhook(ref)
        asyncio.run(paystack.drain())
        FAKE.down = False
        self.assertEqual(post_webhook(ref).status_code, 200)                         # identical body: same dedupe key, still processed
        asyncio.run(paystack.drain())
        self.assertEqual((row(oid)["status"], balance(u)), ("approved", 550))


# ---------------------------------------------------------------- 8. who may see and do what
class AccessTests(Base):
    def test_members_cannot_see_cancel_or_settle_other_members_orders(self):
        a, b = mk_user(), mk_user()
        oid, ref, _ = buy(a)
        pay(ref)
        cb, ca = client(b), client(a)
        self.assertEqual(cb.get(f"/api/pay/paystack/status?reference={ref}").status_code, 404)
        self.assertEqual(cb.get(f"/api/pay/paystack/status?reference={ref}&refresh=1").status_code, 404)
        self.assertEqual(cb.post(f"/api/pay/paystack/order/{oid}/cancel").status_code, 404)
        self.assertEqual(ANON.get(f"/api/pay/paystack/status?reference={ref}").status_code, 401)
        self.assertEqual(ANON.post(f"/api/pay/paystack/order/{oid}/cancel").status_code, 401)
        self.assertEqual((row(oid)["status"], balance(a), balance(b)), ("pending", 0, 0))
        self.assertEqual(ca.get(f"/api/pay/paystack/status?reference={ref}").status_code, 200)

    def test_references_are_validated_and_an_unknown_one_is_a_404(self):
        u = mk_user()
        for ref in ("", "x", "../../etc/passwd", "TLN-" + "A" * 80, "TLN-ABC DEF", "TLN-ABC%00"):
            self.assertEqual(client(u).get("/api/pay/paystack/status", params={"reference": ref}).status_code, 404, ref)
        self.assertEqual(client(u).get("/api/pay/paystack/status?reference=TLN-NOPE123456").status_code, 404)

    def test_the_status_of_a_crypto_order_is_not_reachable_through_the_paystack_endpoint(self):
        u = mk_user()
        with closing(credits.db()) as con:
            con.insert("INSERT INTO billing_orders(user_id,product,method,amount_usd,amount_local,currency,ref,status,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                       (u, "boost", "nowpayments", 10, 10, "USDT", "TLP-MINEMINE01", "pending", int(time.time())))
            con.commit()
        self.assertEqual(client(u).get("/api/pay/paystack/status?reference=TLP-MINEMINE01").status_code, 404)

    def test_members_cannot_use_the_manual_endpoints_on_a_paystack_order(self):
        a = mk_user()
        oid, ref, _ = buy(a)
        c = client(a)
        self.assertEqual(c.post(f"/api/billing/order/{oid}/paid", json={"txid": "0x" + "a" * 40}).status_code, 409)
        self.assertEqual(c.post(f"/api/billing/order/{oid}/cancel").status_code, 409)
        self.assertEqual(c.post("/api/billing/order", json={"product": "boost", "method": "paystack", "accepted": True}).status_code, 422)
        self.assertEqual(row(oid)["status"], "pending")

    def test_members_cannot_reach_admin_endpoints(self):
        a = mk_user()
        oid, ref, _ = buy(a)
        c = client(a)
        for method, path in (("get", "/api/admin/pay/paystack"), ("post", "/api/admin/pay/paystack/toggle"), ("post", "/api/admin/pay/paystack/check"),
                             ("post", f"/api/admin/pay/paystack/order/{oid}/reconcile"), ("post", f"/api/admin/pay/paystack/order/{oid}/resolve"),
                             ("get", f"/api/admin/pay/paystack/order/{oid}/events"), ("post", f"/api/admin/billing/order/{oid}/approve")):
            self.assertEqual(getattr(c, method)(path).status_code, 403, path)
            self.assertEqual(getattr(ANON, method)(path).status_code, 401, path)
        self.assertEqual((row(oid)["status"], balance(a)), ("pending", 0))

    def test_manual_admin_queue_never_offers_paystack_orders(self):
        a, admin = mk_user(), mk_user("admin")
        oid, ref, _ = buy(a)
        q = client(admin).get("/api/admin/billing").json()
        self.assertNotIn(oid, [o["id"] for o in q["open"]])
        self.assertEqual(client(admin).post(f"/api/admin/billing/order/{oid}/approve").status_code, 409)
        self.assertEqual(client(admin).post(f"/api/admin/billing/order/{oid}/reject", json={}).status_code, 409)
        self.assertEqual((row(oid)["status"], balance(a)), ("pending", 0))

    def test_the_orders_list_shows_the_member_their_own_paystack_order_without_internal_data(self):
        a, b = mk_user(), mk_user()
        oid, ref, _ = buy(a)
        d = client(a).get("/api/billing").json()
        mine = [o for o in d["orders"] if o["id"] == oid][0]
        self.assertEqual((mine["method"], mine["method_label"], mine["currency"], mine["ref"]), ("paystack", credits.PAYSTACK_LABEL, "NGN", ref))
        self.assertNotIn("pay_address", mine)
        self.assertNotIn("checkout", json.dumps(mine))
        self.assertEqual([o for o in client(b).get("/api/billing").json()["orders"] if o["id"] == oid], [])

    def test_admin_panel_data_resolve_toggle_and_check(self):
        u, admin = mk_user(), mk_user("admin")
        ca = client(admin)
        oid, ref, _ = buy(u)
        pay(ref)
        FAKE.set(ref, amount=100)
        process(oid)
        v = ca.get("/api/admin/pay/paystack?status=review").json()
        self.assertIn(oid, [o["id"] for o in v["orders"]])
        self.assertEqual((v["mode"], v["key_mode"], v["ngn_rate"], v["problems"]), ("public", "live", float(RATE), []))
        self.assertEqual(v["webhook_url"], "https://tradelens.example/api/pay/paystack/webhook")
        self.assertEqual(v["callback_url"], "https://tradelens.example/paystack.html")
        self.assertEqual(ca.get("/api/admin/pay/paystack?status=bogus").json()["status"], "open")
        self.assertEqual(ca.post(f"/api/admin/pay/paystack/order/{oid}/resolve", json={"action": "approve"}).status_code, 422)         # note required
        self.assertEqual(ca.post(f"/api/admin/pay/paystack/order/{oid}/resolve", json={"action": "maybe", "note": "x"}).status_code, 422)
        self.assertEqual(ca.post(f"/api/admin/pay/paystack/order/{oid}/resolve", json={"action": "approve", "note": "seen it in the dashboard"}).status_code, 200)
        self.assertEqual((row(oid)["status"], balance(u)), ("approved", 550))
        self.assertEqual(ca.post(f"/api/admin/pay/paystack/order/{oid}/resolve", json={"action": "approve", "note": "x"}).status_code, 409)
        evs = ca.get(f"/api/admin/pay/paystack/order/{oid}/events").json()["events"]
        self.assertIn("resolved_approve", [e["kind"] for e in evs])
        chk = ca.post("/api/admin/pay/paystack/check").json()
        self.assertEqual((chk["key_accepted"], chk["key_mode"]), (True, "live"))
        self.assertEqual(ca.post("/api/admin/pay/paystack/toggle", json={"mode": "nonsense"}).status_code, 422)
        self.assertEqual(ca.post("/api/admin/pay/paystack/toggle", json={"mode": "admin"}).status_code, 200)
        self.assertEqual(paystack.mode(), "admin")

    def test_admin_resolve_cannot_touch_an_open_order_and_reject_works(self):
        u, admin = mk_user(), mk_user("admin")
        oid, ref, _ = buy(u)
        self.assertEqual(client(admin).post(f"/api/admin/pay/paystack/order/{oid}/resolve", json={"action": "approve", "note": "x"}).status_code, 409)
        sql("UPDATE billing_orders SET status='review' WHERE id=?", oid)
        self.assertEqual(client(admin).post(f"/api/admin/pay/paystack/order/{oid}/resolve", json={"action": "reject", "note": "no payment seen"}).status_code, 200)
        self.assertEqual((row(oid)["status"], balance(u)), ("rejected", 0))

    def test_toggle_refuses_when_not_ready(self):
        admin = mk_user("admin")
        set_rate("")
        r = client(admin).post("/api/admin/pay/paystack/toggle", json={"mode": "admin"})
        self.assertEqual(r.status_code, 409)
        self.assertIn("rate", r.json()["detail"])
        self.assertEqual(client(admin).post("/api/admin/pay/paystack/toggle", json={"mode": "off"}).status_code, 200)

    def test_admin_check_accepts_the_key_whether_paystack_says_400_or_404_for_the_unknown_reference(self):
        admin = mk_user("admin")
        for code in (400, 404):
            FAKE.unknown_ref_status = code
            chk = client(admin).post("/api/admin/pay/paystack/check").json()
            self.assertEqual((chk["key_accepted"], chk["detail"]), (True, ""), code)
        FAKE.unknown_ref_status = 400

    def test_admin_check_reports_a_refused_key(self):
        admin = mk_user("admin")
        os.environ["PAYSTACK_SECRET_KEY"] = "sk_live_WRONG_not_the_fake_account_key"
        try:
            chk = client(admin).post("/api/admin/pay/paystack/check").json()
        finally:
            use_key("live")
        self.assertEqual(chk["key_accepted"], False)
        self.assertNotIn("WRONG", json.dumps(chk))


# ---------------------------------------------------------------- 9. secrets
class SecretHandlingTests(Base):
    def test_no_key_in_output_responses_events_orders_or_request_bodies(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            u = mk_user()
            FAKE.down = True
            try:
                client(u).post("/api/pay/paystack/order", json={"product": "topup", "accepted": True})
            finally:
                FAKE.down = False
            oid, ref, o = buy(u, "boost")
            post_webhook(ref, sig="bad")
            pay(ref)
            post_webhook(ref)
            asyncio.run(paystack.drain())
            r = client(u).get(f"/api/pay/paystack/status?reference={ref}&refresh=1")
            b = client(u).get("/api/billing")
            admin = mk_user("admin")
            a = client(admin).get("/api/admin/pay/paystack?status=all")
            a2 = client(admin).post("/api/admin/pay/paystack/check")
        dump = "".join((buf.getvalue(), r.text, b.text, a.text, a2.text, json.dumps(o), json.dumps(sql("SELECT * FROM payment_events")),
                        json.dumps(sql("SELECT * FROM billing_orders")), json.dumps(sql("SELECT * FROM credit_ledger"))))
        self.assertNotIn(LIVE_KEY, dump)
        self.assertNotIn("sk_live_", dump)
        for c in FAKE.calls:
            self.assertNotIn(LIVE_KEY, c[2], "the key travels in the Authorization header only, never in a body or a URL")
            self.assertNotIn(LIVE_KEY, c[1])

    def test_wrong_secret_key_is_a_clean_failure(self):
        u = mk_user()
        os.environ["PAYSTACK_SECRET_KEY"] = "sk_live_NOT_THE_ACCOUNTS_KEY"
        buf = io.StringIO()
        try:
            with redirect_stdout(buf):
                r = client(u).post("/api/pay/paystack/order", json={"product": "boost", "accepted": True})
        finally:
            use_key("live")
        self.assertEqual(r.status_code, 502)
        self.assertNotIn("NOT_THE_ACCOUNTS_KEY", r.text + buf.getvalue())
        self.assertEqual([x["status"] for x in sql("SELECT status FROM billing_orders WHERE user_id=?", u)], ["failed"])

    def test_only_the_documented_paystack_endpoints_are_ever_called(self):
        u = mk_user()
        oid, ref, _ = buy(u)
        pay(ref)
        process(oid)
        client(u).post(f"/api/pay/paystack/order/{oid}/cancel")
        for m, p, *_ in FAKE.calls:
            self.assertTrue((m, p) == ("POST", "/transaction/initialize") or (m == "GET" and p.startswith("/transaction/verify/")), (m, p))

    def test_no_recurring_or_card_authorisation_features_are_used(self):
        for call in FAKE.calls:
            self.assertNotIn("subscription", call[1])
            self.assertNotIn("plan", call[1])
        body = json.loads([c for c in FAKE.calls if c[1] == "/transaction/initialize"][-1][2]) if [c for c in FAKE.calls if c[1] == "/transaction/initialize"] else {}
        for k in ("plan", "invoice_limit", "authorization_code", "subaccount", "split_code"):
            self.assertNotIn(k, body)


# ---------------------------------------------------------------- 10. the other payment methods are untouched
class CoexistenceTests(Base):
    def test_crypto_and_manual_orders_still_work_next_to_paystack(self):
        """NOWPayments' own tests (tests/test_nowpayments.py) cover its flow in depth; this checks the two sit side by side in one app."""
        u = mk_user()
        _, ref, _ = buy(u, "topup")
        FAKE.tx.pop(ref)                                           # Paystack knows nothing about NOWPayments orders and vice versa
        r = client(u).post("/api/billing/order", json={"product": "boost", "method": "ngn", "accepted": True})
        self.assertEqual(r.status_code, 409, "manual Naira transfer is not configured in these tests: unchanged behaviour")
        with closing(credits.db()) as con:
            credits._put(con, "bank_name", "Test Bank")
            credits._put(con, "bank_account_number", "0123456789")
            con.commit()
        r = client(u).post("/api/billing/order", json={"product": "boost", "method": "ngn", "accepted": True})
        self.assertEqual(r.status_code, 200, r.text)
        mo = r.json()["order"]
        self.assertEqual((mo["method"], mo["currency"], mo["amount_local"]), ("ngn", "NGN", 10 * RATE))
        admin = mk_user("admin")
        self.assertEqual(client(admin).post(f"/api/admin/billing/order/{mo['id']}/approve").status_code, 200)
        self.assertEqual(balance(u), 550)
        v = client(admin).get("/api/admin/billing").json()
        self.assertEqual(sql("SELECT method FROM billing_orders WHERE id=?", mo["id"])[0]["method"], "ngn")
        self.assertIn(mo["id"], [o["id"] for o in v["recent"]])

    def test_paystack_webhook_and_nowpayments_ipn_are_separate_endpoints(self):
        self.assertEqual(ANON.post("/api/pay/nowpayments/ipn", content=b"{}", headers={"x-paystack-signature": sign(b"{}")}).status_code, 401)
        self.assertEqual(ANON.post("/api/pay/paystack/webhook", content=b"{}", headers={"x-nowpayments-sig": "abc"}).status_code, 401)

    def test_existing_balances_subscriptions_and_invites_are_not_touched_by_a_paystack_purchase(self):
        u = mk_user()
        with closing(credits.db()) as con:
            credits._credit(con, u, 7, "welcome", "welcome", "Welcome")
            con.execute("INSERT INTO billing_subs(user_id,plan,starts_at,ends_at,order_id) VALUES(?,?,?,?,?)",
                        (u, "partner_notify", int(time.time()), int(time.time()) + 86400, None))
            con.commit()
        before_subs = sql("SELECT * FROM billing_subs WHERE user_id=?", u)
        oid, ref, _ = buy(u, "topup")
        pay(ref)
        process(oid)
        self.assertEqual(balance(u), 7 + 267)
        self.assertEqual(sql("SELECT * FROM billing_subs WHERE user_id=?", u), before_subs)
        self.assertEqual(sorted(x["kind"] for x in sql("SELECT kind FROM credit_ledger WHERE user_id=?", u)), ["purchase", "welcome"])


# ---------------------------------------------------------------- 11. database
class MigrationTests(unittest.TestCase):
    def test_no_schema_change_is_needed_and_only_existing_columns_are_used(self):
        src = (ROOT / "app" / "paystack.py").read_text()
        self.assertNotIn("add_column", src)
        self.assertNotIn("CREATE TABLE", src)
        self.assertNotIn("ALTER TABLE", src)
        self.assertNotIn("DROP ", src)
        self.assertNotIn("DELETE FROM", src)
        with closing(credits.db()) as con:
            for col in ("provider", "provider_payment_id", "provider_status", "pay_address", "pay_amount", "pay_currency", "pay_network", "actually_paid",
                        "expires_at", "fulfilled_at", "review_reason", "last_polled_at", "updated_at", "terms_at"):
                self.assertTrue(con.has_column("billing_orders", col), col)

    def test_the_verification_sql_is_read_only(self):
        sql_text = (ROOT / "migrations" / "2026_10_paystack_check.sql").read_text()
        body = "\n".join(l for l in sql_text.splitlines() if not l.strip().startswith("--"))
        import re
        for word in ("INSERT", "UPDATE", "DELETE", "DROP", "ALTER", "CREATE", "TRUNCATE"):
            self.assertIsNone(re.search(r"\b" + word + r"\b", body.upper()), word)


if __name__ == "__main__":
    unittest.main(verbosity=2)
