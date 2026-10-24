"""Tests for the NOWPayments integration (app/nowpay.py) and the billing code it shares (app/credits.py).
Run with `python tests/test_nowpayments.py` or `python -m pytest tests/test_nowpayments.py`.

No network and no real NOWPayments account is needed: a small fake NOWPayments API (FakeNow) is plugged in through
httpx.MockTransport and behaves like the documented endpoints (POST /payment, GET /payment/{id}, GET /status, GET /currencies,
x-api-key header). It can be told to fail, to report any payment status, and to underpay or overpay.
The IPN signature is cross-checked against Node's real JSON.stringify when node is installed."""
import asyncio
import hashlib
import hmac
import io
import json
import os
import shutil
import subprocess
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

API_KEY, IPN_SECRET = "test-api-key-SECRET-1", "test-ipn-secret-SECRET-2"
os.environ.update(DRIVERLINE_SECRET="x" * 40, NOWPAYMENTS_API_KEY=API_KEY, NOWPAYMENTS_IPN_SECRET=IPN_SECRET,
                  DRIVERLINE_BASE_URL="https://tradelens.example", NOWPAYMENTS_API_BASE="https://api-sandbox.nowpayments.io/v1")
os.environ.pop("DATABASE_URL", None)

from app import auth, credits, dbx, nowpay            # noqa: E402
from starlette.applications import Starlette          # noqa: E402
from starlette.exceptions import HTTPException        # noqa: E402
from starlette.responses import JSONResponse          # noqa: E402
from starlette.testclient import TestClient           # noqa: E402

TMP = Path(tempfile.mkdtemp())
dbx.SQLITE_PATH = TMP / "test.db"


async def _http_error(request, exc):
    return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)


APP = Starlette(routes=[*auth.routes, *credits.routes, *nowpay.routes], exception_handlers={HTTPException: _http_error})


# ---------------------------------------------------------------- a fake NOWPayments
class FakeNow:
    def __init__(self):
        self.payments, self.calls, self.n = {}, [], 5000000
        self.down = False
        self.create_override = {}              # fields to change in the reply to POST /payment
        self.currencies = ["btc", "eth", "usdttrc20", "usdtbsc"]

    def set(self, pid, **kw):
        self.payments[str(pid)].update(kw)

    def handler(self, request):
        path = request.url.path.replace("/v1", "", 1)
        self.calls.append((request.method, path, request.content.decode() if request.content else ""))
        if self.down:
            raise httpx.ConnectError("provider down")
        if request.headers.get("x-api-key") != API_KEY:
            return httpx.Response(401, json={"status": False, "message": "Invalid api key"})
        if request.method == "GET" and path == "/status":
            return httpx.Response(200, json={"message": "OK"})
        if request.method == "GET" and path == "/currencies":
            return httpx.Response(200, json={"currencies": self.currencies})
        if request.method == "POST" and path == "/payment":
            b = json.loads(request.content)
            if b["price_amount"] < 1:
                return httpx.Response(400, json={"status": False, "statusCode": 400, "code": "AMOUNT_MINIMAL_ERROR", "message": "too small"})
            self.n += 1
            p = {"payment_id": self.n, "payment_status": "waiting", "pay_address": f"TFakeAddress{self.n}", "price_amount": b["price_amount"],
                 "price_currency": b["price_currency"], "pay_amount": round(b["price_amount"] * 1.0013, 6), "pay_currency": b["pay_currency"],
                 "order_id": b["order_id"], "order_description": b["order_description"], "actually_paid": 0, "ipn_callback_url": b["ipn_callback_url"],
                 "created_at": "2026-10-24T05:00:00.000Z", "updated_at": "2026-10-24T05:00:00.000Z", "purchase_id": "1",
                 "network": "trx"}
            p.update(self.create_override)
            self.payments[str(self.n)] = p
            return httpx.Response(201, json=p)
        if request.method == "GET" and path.startswith("/payment/"):
            p = self.payments.get(path.rsplit("/", 1)[1])
            return httpx.Response(200, json=p) if p else httpx.Response(404, json={"status": False, "message": "not found"})
        return httpx.Response(404, json={"message": "unknown endpoint " + path})


FAKE = FakeNow()
nowpay._TRANSPORT = httpx.MockTransport(FAKE.handler)

_seq = [0]
_clients = {}


def mk_user(tier="free"):
    _seq[0] += 1
    with closing(auth.db()) as con:
        uid = con.insert("INSERT INTO users(email,pw_hash,tier,created_at) VALUES(?,?,?,?)", (f"u{_seq[0]}@test.example", "hash", tier, "2026-01-01T00:00:00"))
        con.commit()
    return uid


def client(uid, tier="free"):
    if uid not in _clients:
        c = TestClient(APP)
        c.__enter__()
        c.cookies.set(auth.COOKIE, auth.make_token(uid, "hash"))
        _clients[uid] = c
    return _clients[uid]


ANON = TestClient(APP)
ANON.__enter__()


def sign(body: dict, secret=IPN_SECRET) -> str:
    """Independent of app code: JSON of the body with keys sorted at every level, HMAC-SHA512, hex."""
    s = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hmac.new(secret.encode(), s.encode(), hashlib.sha512).hexdigest()


def post_ipn(body: dict, sig=None, raw=None):
    data = raw if raw is not None else json.dumps(body)          # default separators on purpose: the receiver must re-serialise
    return ANON.post("/api/pay/nowpayments/ipn", content=data, headers={"x-nowpayments-sig": sign(body) if sig is None else sig,
                                                                        "content-type": "application/json"})


def ipn_for(pid, **over):
    p = FAKE.payments[str(pid)]
    body = {k: p[k] for k in ("payment_id", "payment_status", "pay_address", "price_amount", "price_currency", "pay_amount", "pay_currency",
                              "order_id", "order_description", "actually_paid", "purchase_id")}
    body.update(parent_payment_id=None, invoice_id=None, outcome_amount=body["pay_amount"], outcome_currency="usdttrc20")
    body.update(over)
    return body


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
    r = client(uid).post("/api/pay/crypto/order", json={"product": product, "accepted": True})
    assert r.status_code == 200, r.text
    o = r.json()["order"]
    return o["id"], o["provider_payment_id"] if "provider_payment_id" in o else row(o["id"])["provider_payment_id"], o


def pay(pid, status="finished", paid=None):
    p = FAKE.payments[str(pid)]
    FAKE.set(pid, payment_status=status, actually_paid=p["pay_amount"] if paid is None else paid)


def setUpModule():
    with closing(credits.db()) as con:
        credits._put(con, "enabled", "1")
        credits._put(con, "nowpayments_enabled", "1")
        credits._put(con, "pay_trc20", "TManualWallet")
        con.commit()
    credits._cache["ts"] = 0
    nowpay._switch["ts"] = 0


def tearDownModule():
    for c in list(_clients.values()) + [ANON]:
        c.__exit__(None, None, None)
    shutil.rmtree(TMP, ignore_errors=True)


# ---------------------------------------------------------------- 1. the signature
class SignatureTests(unittest.TestCase):
    def test_valid_and_invalid_signatures(self):
        body = {"payment_id": 1, "payment_status": "finished", "nested": {"b": 2, "a": [3, {"z": 1, "y": "é \"\\"}]}, "pay_amount": 3.0, "x": 0.00001}
        raw = json.dumps(body).encode()
        self.assertTrue(nowpay.verify_signature(raw, sign(body), IPN_SECRET))
        self.assertTrue(nowpay.verify_signature(raw, sign(body).upper(), IPN_SECRET), "hex case must not matter")
        self.assertFalse(nowpay.verify_signature(raw, sign(body, "wrong-secret"), IPN_SECRET))
        self.assertFalse(nowpay.verify_signature(raw, "", IPN_SECRET))
        self.assertFalse(nowpay.verify_signature(raw, sign(body), ""))
        self.assertFalse(nowpay.verify_signature(raw.replace(b"finished", b"waiting"), sign(body), IPN_SECRET), "tampered body")
        self.assertFalse(nowpay.verify_signature(b"not json", sign(body), IPN_SECRET))

    @unittest.skipUnless(shutil.which("node"), "node not installed")
    def test_canonical_form_matches_javascript(self):
        """The signed text must equal JSON.stringify of the key-sorted body, character for character, as NOWPayments' own Node example builds it."""
        raw = ('{"b":3.0,"a":{"y":0.00001,"x":1e-7,"w":[1.50,2e21,123456.789,-0.0,1e21,100,0.1,1.7976931348623157e308,5e-324],"v":"é☃\\u0001/"},'
               '"c":null,"d":true,"e":12345678901234567890,"f":0.000001,"g":1e-6,"h":4.35,"i":1e300}')
        js = ("const s=require('fs').readFileSync(0,'utf8');const so=o=>Array.isArray(o)?o.map(so):(o&&typeof o==='object')?"
              "Object.keys(o).sort().reduce((r,k)=>(r[k]=so(o[k]),r),{}):o;process.stdout.write(JSON.stringify(so(JSON.parse(s))))")
        want = subprocess.run(["node", "-e", js], input=raw, capture_output=True, text=True, check=True).stdout
        self.assertEqual(nowpay._canon(json.loads(raw), True), want)


# ---------------------------------------------------------------- 2. creating an order
class CreateOrderTests(unittest.TestCase):
    def test_creates_order_with_server_side_price_and_shows_instructions(self):
        uid = mk_user()
        r = client(uid).post("/api/pay/crypto/order", json={"product": "boost", "accepted": True, "usd": 0.01, "amount": 1, "user_id": 999,
                                                            "signals": 1000000, "price_amount": 1})
        self.assertEqual(r.status_code, 200, r.text)
        o = r.json()["order"]
        self.assertEqual((o["status"], o["amount_usd"], o["method"], o["network"], o["asset"]), ("pending", 10.0, "nowpayments", "TRON (TRC20)", "USDT"))
        self.assertTrue(o["pay_address"].startswith("TFakeAddress"))
        self.assertGreater(o["pay_amount"], 10.0)
        self.assertEqual(o["signals"], 550)
        sent = json.loads([c for c in FAKE.calls if c[0] == "POST" and c[1] == "/payment"][-1][2])
        self.assertEqual((sent["price_amount"], sent["price_currency"], sent["pay_currency"]), (10.0, "usd", "usdttrc20"))
        self.assertEqual(sent["ipn_callback_url"], "https://tradelens.example/api/pay/nowpayments/ipn")
        self.assertTrue(sent["order_id"].startswith("TLP-"))
        db_row = row(o["id"])
        self.assertEqual((db_row["user_id"], db_row["product"], db_row["provider"]), (uid, "boost", "nowpayments"))
        self.assertEqual(balance(uid), 0, "creating an order must not credit anything")
        self.assertNotIn(API_KEY, r.text)
        self.assertNotIn(IPN_SECRET, r.text)

    def test_rejections(self):
        uid = mk_user()
        c = client(uid)
        self.assertEqual(ANON.post("/api/pay/crypto/order", json={"product": "boost", "accepted": True}).status_code, 401)
        self.assertEqual(c.post("/api/pay/crypto/order", json={"product": "nope", "accepted": True}).status_code, 422)
        self.assertEqual(c.post("/api/pay/crypto/order", json={"product": "boost"}).status_code, 422, "terms must be accepted")
        self.assertEqual(c.post("/api/pay/crypto/order", content="x", headers={"content-type": "application/json"}).status_code, 400)
        self.assertEqual(c.post("/api/pay/crypto/order", json={"product": "boost", "accepted": "yes"}).status_code, 422)

    def test_billing_closed_or_switch_off(self):
        uid = mk_user()
        for key, off, on in (("enabled", "0", "1"), ("nowpayments_enabled", "0", "1")):
            sql("UPDATE billing_settings SET value=? WHERE key=?", off, key)
            credits._cache["ts"] = 0
            nowpay._switch["ts"] = 0
            try:
                self.assertEqual(client(uid).post("/api/pay/crypto/order", json={"product": "topup", "accepted": True}).status_code, 409)
                self.assertFalse(client(uid).get("/api/billing").json()["crypto"]["enabled"])
            finally:
                sql("UPDATE billing_settings SET value=? WHERE key=?", on, key)
                credits._cache["ts"] = 0
                nowpay._switch["ts"] = 0
        self.assertTrue(client(uid).get("/api/billing").json()["crypto"]["enabled"])

    def test_missing_configuration_keeps_it_off(self):
        uid = mk_user()
        old = os.environ.pop("NOWPAYMENTS_IPN_SECRET")
        try:
            self.assertEqual(client(uid).post("/api/pay/crypto/order", json={"product": "topup", "accepted": True}).status_code, 409)
            self.assertEqual(post_ipn({"payment_id": 1}).status_code, 503, "IPN fails closed without a secret")
        finally:
            os.environ["NOWPAYMENTS_IPN_SECRET"] = old

    def test_http_callback_url_refused(self):
        old = os.environ["DRIVERLINE_BASE_URL"]
        os.environ["DRIVERLINE_BASE_URL"] = "http://tradelens.example"
        try:
            self.assertTrue(any("https" in p for p in nowpay.problems()))
        finally:
            os.environ["DRIVERLINE_BASE_URL"] = old

    def test_double_click_returns_the_same_order(self):
        uid = mk_user()
        a = client(uid).post("/api/pay/crypto/order", json={"product": "topup", "accepted": True}).json()["order"]
        n = len(FAKE.payments)
        b = client(uid).post("/api/pay/crypto/order", json={"product": "topup", "accepted": True}).json()["order"]
        self.assertEqual(a["id"], b["id"])
        self.assertEqual(len(FAKE.payments), n, "no second payment may be created at the provider")

    def test_open_order_limit_and_rate_limit(self):
        uid = mk_user()
        for p in ("topup", "boost", "vault"):
            self.assertEqual(client(uid).post("/api/pay/crypto/order", json={"product": p, "accepted": True}).status_code, 200)
        self.assertEqual(client(uid).post("/api/pay/crypto/order", json={"product": "plus", "accepted": True}).status_code, 429)
        uid2 = mk_user()
        codes = [client(uid2).post("/api/pay/crypto/order", json={"product": "nope", "accepted": True}).status_code for _ in range(1)]
        self.assertEqual(codes, [422])
        for _ in range(8):
            r = client(uid2).post("/api/pay/crypto/order", json={"product": "topup", "accepted": True})
        self.assertIn(r.status_code, (200, 429))

    def test_provider_outage_creates_no_usable_order(self):
        uid = mk_user()
        FAKE.down = True
        try:
            r = client(uid).post("/api/pay/crypto/order", json={"product": "topup", "accepted": True})
        finally:
            FAKE.down = False
        self.assertEqual(r.status_code, 502)
        self.assertNotIn("provider down", r.text)
        o = sql("SELECT * FROM billing_orders WHERE user_id=?", uid)
        self.assertEqual([x["status"] for x in o], ["failed"])
        self.assertIsNone(o[0]["pay_address"])

    def test_bad_provider_replies_are_not_shown_to_members(self):
        for override in ({"pay_currency": "usdtbsc"}, {"pay_address": ""}, {"price_amount": 1}, {"order_id": "someone-else"}, {"payment_id": ""}):
            uid = mk_user()
            FAKE.create_override = override
            try:
                r = client(uid).post("/api/pay/crypto/order", json={"product": "boost", "accepted": True})
            finally:
                FAKE.create_override = {}
            self.assertEqual(r.status_code, 502, override)
            self.assertEqual([x["status"] for x in sql("SELECT status FROM billing_orders WHERE user_id=?", uid)], ["failed"])

    def test_partner_member_pays_partner_price(self):
        try:
            from app import perks  # noqa: F401
        except Exception:
            self.skipTest("partner module needs packages that are not installed here")
        uid = mk_user()
        from app import partner
        with closing(partner.db()) as con:
            con.execute("INSERT INTO partner_tags(account_id,user_id,status,first_seen) VALUES(?,?,?,?)", (f"CR{uid}", uid, "confirmed", 1))
            con.commit()
        o = client(uid).post("/api/pay/crypto/order", json={"product": "pro", "accepted": True}).json()["order"]
        self.assertEqual(o["amount_usd"], 15.0)


# ---------------------------------------------------------------- 3. paying and fulfilment
class FulfilmentTests(unittest.TestCase):
    def test_successful_payment_is_fulfilled_automatically(self):
        uid = mk_user()
        oid, pid, _ = buy(uid, "boost")
        pay(pid, "finished")
        r = post_ipn(ipn_for(pid))
        self.assertEqual(r.status_code, 200)
        asyncio.run(nowpay.drain())
        self.assertEqual(row(oid)["status"], "approved")
        self.assertEqual(balance(uid), 550)
        led = sql("SELECT * FROM credit_ledger WHERE user_id=? AND kind='purchase'", uid)
        self.assertEqual([(x["delta"], x["ref"]) for x in led], [(550, f"order:{oid}")])
        self.assertEqual([e["kind"] for e in events(oid)], ["created", "provider_payment", "ipn", "fulfilled"])
        v = client(uid).get(f"/api/pay/crypto/order/{oid}").json()["order"]
        self.assertEqual((v["status"], v["balance"], v["pay_address"]), ("approved", 550, None), "address is hidden once settled")
        bill = client(uid).get("/api/billing").json()
        self.assertEqual(bill["balance"], 550 + credits.WELCOME, "the purchase plus the one-time welcome signals billing_view gives a free member")
        self.assertEqual(bill["orders"][0]["provider_payment_id"], str(pid))

    def test_plan_purchase_activates_entitlements_and_stacks(self):
        uid = mk_user()
        user = {"id": uid, "tier": "free"}
        self.assertFalse(credits.entitled(user, "notify"))
        ends = []
        for _ in range(2):
            oid, pid, _o = buy(uid, "pro")
            pay(pid)
            post_ipn(ipn_for(pid))
            asyncio.run(nowpay.drain())
            self.assertEqual(row(oid)["status"], "approved")
            ends.append(max(p["ends_at"] for p in credits.active_plans(uid)))
        self.assertTrue(credits.entitled(user, "notify"))
        self.assertTrue(credits.entitled(user, "mt5"))
        self.assertEqual(balance(uid), 1100)
        self.assertAlmostEqual(ends[1] - ends[0], 30 * 86400, delta=5, msg="a second Pro extends the first")

    def test_invalid_signature_changes_nothing(self):
        uid = mk_user()
        oid, pid, _ = buy(uid)
        pay(pid)
        body = ipn_for(pid)
        for sig in ("", "0" * 128, sign(body, "attacker-secret"), sign({**body, "payment_status": "waiting"})):
            self.assertEqual(post_ipn(body, sig=sig).status_code, 401)
        asyncio.run(nowpay.drain())
        self.assertEqual((row(oid)["status"], balance(uid)), ("pending", 0))
        self.assertEqual(events(oid, "ipn"), [])

    def test_duplicate_and_out_of_order_callbacks_credit_once(self):
        uid = mk_user()
        oid, pid, _ = buy(uid)
        pay(pid, "confirming")
        confirming = ipn_for(pid)
        pay(pid, "finished")
        finished = ipn_for(pid)
        for body in (finished, finished, confirming, finished, confirming):      # duplicates, and an older status arriving last
            self.assertEqual(post_ipn(body).status_code, 200)
            asyncio.run(nowpay.drain())
        self.assertEqual((row(oid)["status"], balance(uid)), ("approved", 550))
        self.assertEqual(len(sql("SELECT * FROM credit_ledger WHERE user_id=? AND kind='purchase'", uid)), 1)
        self.assertEqual(len(events(oid, "fulfilled")), 1)

    def test_a_signed_notification_alone_cannot_fulfil(self):
        """The decision comes from the provider API. If the API says waiting, a finished notification does nothing."""
        uid = mk_user()
        oid, pid, _ = buy(uid)
        body = ipn_for(pid, payment_status="finished", actually_paid=FAKE.payments[str(pid)]["pay_amount"])
        self.assertEqual(post_ipn(body).status_code, 200)
        asyncio.run(nowpay.drain())
        self.assertEqual((row(oid)["status"], balance(uid)), ("pending", 0))

    def test_provider_outage_during_notification_is_retried_by_the_sweep(self):
        uid = mk_user()
        oid, pid, _ = buy(uid)
        pay(pid)
        FAKE.down = True
        try:
            self.assertEqual(post_ipn(ipn_for(pid)).status_code, 200)
            asyncio.run(nowpay.drain())
            self.assertEqual((row(oid)["status"], balance(uid)), ("pending", 0))
            self.assertIn("poll_error", [e["kind"] for e in events(oid)])
        finally:
            FAKE.down = False
        asyncio.run(nowpay.sweep_once())
        self.assertEqual((row(oid)["status"], balance(uid)), ("approved", 550))

    def test_lost_notification_is_found_by_the_sweep_and_by_admin_reconcile(self):
        a, b = mk_user(), mk_user()
        oa, pa, _ = buy(a)
        ob, pb, _ = buy(b)
        pay(pa)
        pay(pb)
        asyncio.run(nowpay.sweep_once())
        self.assertEqual((row(oa)["status"], balance(a)), ("approved", 550))
        admin = mk_user("admin")
        r = client(admin).post(f"/api/admin/pay/crypto/order/{ob}/reconcile")
        self.assertEqual((r.status_code, r.json()["status"]), (200, "approved"))
        self.assertEqual(balance(b), 550)
        self.assertEqual(client(admin).post(f"/api/admin/pay/crypto/order/{ob}/reconcile").status_code, 200)
        self.assertEqual(balance(b), 550)

    def test_member_refresh_asks_the_provider_and_is_throttled(self):
        uid = mk_user()
        oid, pid, _ = buy(uid)
        pay(pid, "confirming")
        n = len(FAKE.calls)
        v = client(uid).get(f"/api/pay/crypto/order/{oid}?refresh=1").json()["order"]
        self.assertEqual(v["status"], "confirming")
        client(uid).get(f"/api/pay/crypto/order/{oid}?refresh=1")
        self.assertEqual(len(FAKE.calls) - n, 1, "second refresh within 10 s must not call the provider")
        pay(pid, "finished")
        sql("UPDATE billing_orders SET last_polled_at=0 WHERE id=?", oid)
        self.assertEqual(client(uid).get(f"/api/pay/crypto/order/{oid}?refresh=1").json()["order"]["status"], "approved")

    def test_status_progression_and_terminal_states(self):
        uid = mk_user()
        oid, pid, _ = buy(uid)
        for provider, local in (("waiting", "pending"), ("confirming", "confirming"), ("waiting", "confirming"), ("confirmed", "confirming"), ("sending", "confirming")):
            FAKE.set(pid, payment_status=provider)
            asyncio.run(nowpay.process_order(oid, "test"))
            self.assertEqual(row(oid)["status"], local, provider)
        self.assertEqual(balance(uid), 0)
        for provider, local in (("failed", "failed"), ("expired", "expired"), ("refunded", "refunded")):
            u = mk_user()
            o, p, _x = buy(u, "topup")
            FAKE.set(p, payment_status=provider)
            asyncio.run(nowpay.process_order(o, "test"))
            self.assertEqual((row(o)["status"], balance(u)), (local, 0), provider)

    def test_unknown_status_is_left_alone(self):
        uid = mk_user()
        oid, pid, _ = buy(uid)
        FAKE.set(pid, payment_status="something_new")
        asyncio.run(nowpay.process_order(oid, "test"))
        self.assertEqual(row(oid)["status"], "pending")
        self.assertIn("unknown_status", [e["kind"] for e in events(oid)])

    def test_expiry_sweep(self):
        uid = mk_user()
        oid, pid, _ = buy(uid)
        sql("UPDATE billing_orders SET expires_at=? WHERE id=?", int(time.time()) - 600, oid)
        FAKE.down = True
        try:
            asyncio.run(nowpay.sweep_once())
            self.assertEqual(row(oid)["status"], "pending", "an order that could not be verified is never closed")
        finally:
            FAKE.down = False
        asyncio.run(nowpay.sweep_once())
        self.assertEqual(row(oid)["status"], "expired")

    def test_payment_that_arrives_after_expiry_goes_to_review_not_credit(self):
        uid = mk_user()
        oid, pid, _ = buy(uid)
        sql("UPDATE billing_orders SET expires_at=? WHERE id=?", int(time.time()) - 600, oid)
        asyncio.run(nowpay.sweep_once())
        self.assertEqual(row(oid)["status"], "expired")
        pay(pid)
        post_ipn(ipn_for(pid))
        asyncio.run(nowpay.drain())
        r = row(oid)
        self.assertEqual((r["status"], balance(uid)), ("review", 0))
        self.assertTrue(r["review_reason"].startswith("paid_after_close"))
        asyncio.run(nowpay.sweep_once())
        self.assertEqual(row(oid)["status"], "review", "review is never auto-fulfilled")

    def test_confirming_order_is_not_expired_while_money_is_on_the_way(self):
        uid = mk_user()
        oid, pid, _ = buy(uid)
        pay(pid, "confirming")
        sql("UPDATE billing_orders SET expires_at=? WHERE id=?", int(time.time()) - 600, oid)
        asyncio.run(nowpay.sweep_once())
        self.assertEqual(row(oid)["status"], "confirming")
        pay(pid, "finished")
        asyncio.run(nowpay.sweep_once())
        self.assertEqual((row(oid)["status"], balance(uid)), ("approved", 550))


# ---------------------------------------------------------------- 4. money that does not match
class MismatchTests(unittest.TestCase):
    def _review(self, **provider_changes):
        uid = mk_user()
        oid, pid, _ = buy(uid)
        pay(pid)
        FAKE.set(pid, **provider_changes)
        post_ipn(ipn_for(pid))
        asyncio.run(nowpay.drain())
        return uid, oid, row(oid)

    def test_underpayment_partially_paid(self):
        uid, oid, r = self._review(payment_status="partially_paid", actually_paid=5.0)
        self.assertEqual((r["status"], balance(uid)), ("review", 0))
        self.assertTrue(r["review_reason"].startswith("underpaid"))
        m = client(uid).get(f"/api/pay/crypto/order/{oid}").json()["order"]
        self.assertIn("less than", m["message"])
        self.assertNotIn("underpaid:", json.dumps(m))

    def test_finished_but_short_is_not_fulfilled(self):
        uid, oid, r = self._review(actually_paid=9.99)
        self.assertEqual((r["status"], balance(uid)), ("review", 0))

    def test_overpayment_is_fulfilled_once_and_flagged(self):
        uid, oid, r = self._review(actually_paid=25.0)
        self.assertEqual((r["status"], balance(uid)), ("approved", 550))
        self.assertEqual(len(events(oid, "overpaid")), 1)

    def test_wrong_currency_or_network(self):
        for field, value in (("pay_currency", "usdtbsc"), ("pay_currency", "btc")):
            uid, oid, r = self._review(**{field: value})
            self.assertEqual((r["status"], balance(uid)), ("review", 0), f"{field}={value}")
            self.assertIn("currency", r["review_reason"])

    def test_wrong_price_order_id_address_and_payment_id(self):
        for change, text in (({"price_amount": 3}, "price"), ({"price_currency": "eur"}, "price"), ({"order_id": "TLP-OTHER"}, "order id"),
                             ({"pay_address": "TAttackerAddress"}, "address")):
            uid, oid, r = self._review(**change)
            self.assertEqual((r["status"], balance(uid)), ("review", 0), change)
            self.assertIn(text, r["review_reason"])
        uid = mk_user()
        oid, pid, _ = buy(uid)
        pay(pid)
        FAKE.set(pid, payment_id=int(pid) + 777)
        asyncio.run(nowpay.process_order(oid, "test"))
        self.assertEqual((row(oid)["status"], balance(uid)), ("pending", 0), "a reply for a different payment is ignored")
        self.assertIn("mismatch", [e["kind"] for e in events(oid)])

    def test_unmatched_notification_is_recorded_for_the_owner(self):
        body = {"payment_id": 99999999, "payment_status": "finished", "order_id": "TLP-NOPE", "pay_amount": 1, "actually_paid": 1}
        self.assertEqual(post_ipn(body).status_code, 200)
        self.assertTrue(sql("SELECT * FROM payment_events WHERE kind='unmatched' AND provider_payment_id='99999999'"))

    def test_extra_deposit_to_the_same_address_goes_to_review(self):
        uid = mk_user()
        oid, pid, _ = buy(uid)
        body = {"payment_id": 424242, "parent_payment_id": pid, "payment_status": "finished", "order_id": row(oid)["ref"], "pay_amount": 3, "actually_paid": 3}
        self.assertEqual(post_ipn(body).status_code, 200)
        r = row(oid)
        self.assertEqual((r["status"], balance(uid)), ("review", 0))
        self.assertTrue(r["review_reason"].startswith("extra_deposit"))

    def test_refund_after_fulfilment_is_flagged_not_clawed_back(self):
        uid = mk_user()
        oid, pid, _ = buy(uid)
        pay(pid)
        asyncio.run(nowpay.process_order(oid, "test"))
        FAKE.set(pid, payment_status="refunded")
        asyncio.run(nowpay.process_order(oid, "test"))
        self.assertEqual((row(oid)["status"], balance(uid)), ("approved", 550))
        self.assertIn("anomaly", [e["kind"] for e in events(oid)])


# ---------------------------------------------------------------- 5. who may do what
class AccessTests(unittest.TestCase):
    def test_members_cannot_see_or_settle_other_orders(self):
        a, b = mk_user(), mk_user()
        oid, pid, _ = buy(a)
        self.assertEqual(client(b).get(f"/api/pay/crypto/order/{oid}").status_code, 404)
        self.assertEqual(ANON.get(f"/api/pay/crypto/order/{oid}").status_code, 401)

    def test_members_cannot_use_the_manual_endpoints_on_an_automatic_order(self):
        a = mk_user()
        oid, pid, _ = buy(a)
        c = client(a)
        self.assertEqual(c.post(f"/api/billing/order/{oid}/paid", json={"txid": "0x" + "a" * 40}).status_code, 409)
        self.assertEqual(c.post(f"/api/billing/order/{oid}/cancel").status_code, 409)
        self.assertEqual(c.post("/api/billing/order", json={"product": "boost", "method": "nowpayments", "accepted": True}).status_code, 422)
        self.assertEqual(row(oid)["status"], "pending")

    def test_members_cannot_reach_admin_endpoints(self):
        a = mk_user()
        oid, pid, _ = buy(a)
        c = client(a)
        for method, path in (("get", "/api/admin/pay/crypto"), ("post", "/api/admin/pay/crypto/toggle"), ("post", "/api/admin/pay/crypto/check"),
                             ("post", f"/api/admin/pay/crypto/order/{oid}/reconcile"), ("post", f"/api/admin/pay/crypto/order/{oid}/resolve"),
                             ("get", f"/api/admin/pay/crypto/order/{oid}/events"), ("post", f"/api/admin/billing/order/{oid}/approve")):
            self.assertEqual(getattr(c, method)(path).status_code, 403, path)
            self.assertEqual(getattr(ANON, method)(path).status_code, 401, path)
        self.assertEqual((row(oid)["status"], balance(a)), ("pending", 0))

    def test_manual_admin_queue_never_offers_automatic_orders(self):
        a, admin = mk_user(), mk_user("admin")
        oid, pid, _ = buy(a)
        q = client(admin).get("/api/admin/billing").json()
        self.assertNotIn(oid, [o["id"] for o in q["open"]])
        self.assertEqual(client(admin).post(f"/api/admin/billing/order/{oid}/approve").status_code, 409)
        self.assertEqual(client(admin).post(f"/api/admin/billing/order/{oid}/reject", json={}).status_code, 409)
        self.assertEqual((row(oid)["status"], balance(a)), ("pending", 0))

    def test_admin_resolve_review(self):
        admin = mk_user("admin")
        for action in ("approve", "reject"):
            uid = mk_user()
            oid, pid, _ = buy(uid, "plus")
            pay(pid, "partially_paid", paid=1.0)
            asyncio.run(nowpay.process_order(oid, "test"))
            self.assertEqual(row(oid)["status"], "review")
            c = client(admin)
            self.assertEqual(c.post(f"/api/admin/pay/crypto/order/{oid}/resolve", json={"action": action}).status_code, 422, "a note is required")
            self.assertEqual(c.post(f"/api/admin/pay/crypto/order/{oid}/resolve", json={"action": "bogus", "note": "x"}).status_code, 422)
            self.assertEqual(c.post(f"/api/admin/pay/crypto/order/{oid}/resolve", json={"action": action, "note": "customer topped up by hand"}).status_code, 200)
            self.assertEqual(c.post(f"/api/admin/pay/crypto/order/{oid}/resolve", json={"action": action, "note": "again"}).status_code, 409)
            if action == "approve":
                self.assertEqual((row(oid)["status"], balance(uid)), ("approved", 550))
                self.assertTrue(credits.entitled({"id": uid, "tier": "free"}, "notify"))
            else:
                self.assertEqual((row(oid)["status"], balance(uid)), ("rejected", 0))
            self.assertEqual(events(oid)[-1]["admin_id"], admin)

    def test_admin_resolve_cannot_touch_an_open_order(self):
        admin, uid = mk_user("admin"), mk_user()
        oid, pid, _ = buy(uid)
        r = client(admin).post(f"/api/admin/pay/crypto/order/{oid}/resolve", json={"action": "approve", "note": "x"})
        self.assertEqual((r.status_code, balance(uid)), (409, 0))

    def test_admin_panel_data(self):
        admin, uid = mk_user("admin"), mk_user()
        oid, pid, _ = buy(uid)
        d = client(admin).get("/api/admin/pay/crypto?status=all").json()
        self.assertEqual((d["mode"], d["problems"]), ("public", []))
        self.assertEqual(d["ipn_url"], "https://tradelens.example/api/pay/nowpayments/ipn")
        self.assertIn(oid, [o["id"] for o in d["orders"]])
        self.assertNotIn(API_KEY, json.dumps(d))
        self.assertNotIn(IPN_SECRET, json.dumps(d))
        ev = client(admin).get(f"/api/admin/pay/crypto/order/{oid}/events").json()["events"]
        self.assertEqual([e["kind"] for e in ev], ["created", "provider_payment"])
        chk = client(admin).post("/api/admin/pay/crypto/check").json()
        self.assertEqual((chk["api_reachable"], chk["currency_listed"]), (True, True))
        FAKE.currencies = ["btc"]
        try:
            self.assertFalse(client(admin).post("/api/admin/pay/crypto/check").json()["currency_listed"])
        finally:
            FAKE.currencies = ["btc", "eth", "usdttrc20", "usdtbsc"]

    def test_admin_toggle_modes(self):
        admin = mk_user("admin")
        c = client(admin)
        t = lambda m: c.post("/api/admin/pay/crypto/toggle", json={"mode": m})
        try:
            self.assertEqual(t("off").status_code, 200)
            self.assertEqual(nowpay.mode(), "off")
            self.assertEqual(t("admin").status_code, 200)
            self.assertEqual(nowpay.mode(), "admin")
            self.assertEqual(t("bogus").status_code, 422)
            self.assertEqual(c.post("/api/admin/pay/crypto/toggle", json={"enabled": True}).status_code, 422)
            old = os.environ.pop("NOWPAYMENTS_API_KEY")
            try:
                self.assertEqual(t("admin").status_code, 409)
                self.assertEqual(t("off").status_code, 200, "switching OFF is always allowed")
            finally:
                os.environ["NOWPAYMENTS_API_KEY"] = old
            sql("UPDATE billing_settings SET value='0' WHERE key='enabled'")
            credits._cache["ts"] = 0
            self.assertEqual(t("public").status_code, 409, "public needs billing open")
            self.assertEqual(t("admin").status_code, 200, "the pilot does not")
        finally:
            sql("UPDATE billing_settings SET value='1' WHERE key='enabled'")
            credits._cache["ts"] = 0
            t("public")
        self.assertEqual(nowpay.mode(), "public")


class PilotModeTests(unittest.TestCase):
    """Admin pilot: only admin accounts can see or start an automatic payment; everyone else is unaffected."""

    def setUp(self):
        self.admin, self.member = mk_user("admin"), mk_user()
        client(self.admin).post("/api/admin/pay/crypto/toggle", json={"mode": "admin"})

    def tearDown(self):
        sql("UPDATE billing_settings SET value='1' WHERE key='enabled'")
        credits._cache["ts"] = 0
        self.assertEqual(client(self.admin).post("/api/admin/pay/crypto/toggle", json={"mode": "public"}).status_code, 200)

    def test_only_admin_sees_and_uses_it(self):
        self.assertTrue(client(self.admin).get("/api/billing").json()["crypto"]["enabled"])
        self.assertTrue(client(self.admin).get("/api/billing").json()["crypto"]["pilot"])
        self.assertFalse(client(self.member).get("/api/billing").json()["crypto"]["enabled"])
        self.assertEqual(client(self.member).post("/api/pay/crypto/order", json={"product": "topup", "accepted": True}).status_code, 409)
        self.assertEqual(client(self.admin).post("/api/pay/crypto/order", json={"product": "topup", "accepted": True}).status_code, 200)

    def test_works_while_billing_is_closed_to_members(self):
        sql("UPDATE billing_settings SET value='0' WHERE key='enabled'")
        credits._cache["ts"] = 0
        a, m = client(self.admin).get("/api/billing").json(), client(self.member).get("/api/billing").json()
        self.assertEqual((a["enabled"], a["crypto"]["enabled"]), (True, True))
        self.assertEqual(a["pay"]["trc20"], "", "no manual wallet details leak while billing is closed")
        self.assertEqual((m["enabled"], m["crypto"]["enabled"], m["pay"]), (False, False, None))
        o = client(self.admin).post("/api/pay/crypto/order", json={"product": "boost", "accepted": True})
        self.assertEqual(o.status_code, 200, o.text)
        self.assertEqual(client(self.member).post("/api/billing/order", json={"product": "topup", "method": "trc20", "accepted": True}).status_code, 409)

    def test_payments_already_started_still_complete_when_mode_goes_off(self):
        oid, pid, _ = buy(self.admin, "boost")
        client(self.admin).post("/api/admin/pay/crypto/toggle", json={"mode": "off"})
        self.assertEqual(client(self.admin).post("/api/pay/crypto/order", json={"product": "topup", "accepted": True}).status_code, 409)
        pay(pid)
        post_ipn(ipn_for(pid))
        asyncio.run(nowpay.drain())
        self.assertEqual((row(oid)["status"], balance(self.admin)), ("approved", 550))
        self.assertEqual(client(self.admin).get(f"/api/pay/crypto/order/{oid}").status_code, 200)


class TwoNetworkTests(unittest.TestCase):
    """NOWPAYMENTS_PAY_CURRENCIES=usdttrc20,usdtbsc: the member picks TRC20 or BEP20."""

    def setUp(self):
        self._old = {k: os.environ.pop(k, None) for k in ("NOWPAYMENTS_PAY_CURRENCIES", "NOWPAYMENTS_PAY_CURRENCY")}
        os.environ["NOWPAYMENTS_PAY_CURRENCIES"] = "usdttrc20, USDTBSC"

    def tearDown(self):
        os.environ.pop("NOWPAYMENTS_PAY_CURRENCIES", None)
        for k, v in self._old.items():
            if v is not None:
                os.environ[k] = v

    def order(self, uid, product="boost", currency=None):
        body = {"product": product, "accepted": True, **({"currency": currency} if currency else {})}
        return client(uid).post("/api/pay/crypto/order", json=body)

    def test_default_when_unset_is_trc20_only_and_unknown_codes_are_ignored(self):
        os.environ.pop("NOWPAYMENTS_PAY_CURRENCIES")
        self.assertEqual(nowpay.pay_currencies(), ["usdttrc20"])
        os.environ["NOWPAYMENTS_PAY_CURRENCIES"] = "usdtbsc;doge,usdtbsc,usdttrc20"
        self.assertEqual(nowpay.pay_currencies(), ["usdtbsc", "usdttrc20"])
        os.environ["NOWPAYMENTS_PAY_CURRENCIES"] = "nonsense"
        self.assertEqual(nowpay.pay_currencies(), ["usdttrc20"])
        os.environ["NOWPAYMENTS_PAY_CURRENCIES"] = "usdttrc20,usdtbsc"
        os.environ.pop("NOWPAYMENTS_PAY_CURRENCIES")
        os.environ["NOWPAYMENTS_PAY_CURRENCY"] = "usdtbsc"
        try:
            self.assertEqual(nowpay.pay_currencies(), ["usdtbsc"], "the older single setting still works")
        finally:
            os.environ.pop("NOWPAYMENTS_PAY_CURRENCY")

    def test_member_sees_both_options(self):
        uid = mk_user()
        info = client(uid).get("/api/billing").json()["crypto"]
        self.assertEqual([(o["code"], o["short"]) for o in info["options"]], [("usdttrc20", "TRC20"), ("usdtbsc", "BEP20")])

    def test_bep20_order_is_created_stored_and_fulfilled(self):
        uid = mk_user()
        r = self.order(uid, "boost", "usdtbsc")
        self.assertEqual(r.status_code, 200, r.text)
        o = r.json()["order"]
        self.assertEqual(o["network"], "BNB Smart Chain (BEP20)")
        sent = json.loads([c for c in FAKE.calls if c[0] == "POST" and c[1] == "/payment"][-1][2])
        self.assertEqual(sent["pay_currency"], "usdtbsc")
        self.assertEqual(row(o["id"])["pay_currency"], "usdtbsc")
        pid = row(o["id"])["provider_payment_id"]
        pay(pid)
        post_ipn(ipn_for(pid, pay_currency="usdtbsc"))
        asyncio.run(nowpay.drain())
        self.assertEqual((row(o["id"])["status"], balance(uid)), ("approved", 550))

    def test_default_is_the_first_currency_and_trc20_still_works(self):
        uid = mk_user()
        o = self.order(uid, "topup").json()["order"]
        self.assertEqual((o["network"], row(o["id"])["pay_currency"]), ("TRON (TRC20)", "usdttrc20"))

    def test_currency_not_offered_is_refused(self):
        uid = mk_user()
        for bad in ("btc", "usdterc20", "USDT", ""):
            if bad:
                self.assertEqual(self.order(uid, "topup", bad).status_code, 422, bad)
        os.environ["NOWPAYMENTS_PAY_CURRENCIES"] = "usdttrc20"
        self.assertEqual(self.order(uid, "topup", "usdtbsc").status_code, 422, "BEP20 is refused when it is not switched on")

    def test_each_network_gets_its_own_order_and_a_double_click_reuses_it(self):
        uid = mk_user()
        a = self.order(uid, "topup", "usdttrc20").json()["order"]
        b = self.order(uid, "topup", "usdtbsc").json()["order"]
        self.assertNotEqual(a["id"], b["id"])
        n = len(FAKE.payments)
        self.assertEqual(self.order(uid, "topup", "usdtbsc").json()["order"]["id"], b["id"])
        self.assertEqual(len(FAKE.payments), n)

    def test_paying_in_the_other_currency_is_not_fulfilled(self):
        """A BEP20 order whose payment arrives as TRC20 (or the reverse) must not credit: it goes to review."""
        for chosen, arrived in (("usdtbsc", "usdttrc20"), ("usdttrc20", "usdtbsc")):
            uid = mk_user()
            o = self.order(uid, "boost", chosen).json()["order"]
            pid = row(o["id"])["provider_payment_id"]
            pay(pid)
            FAKE.set(pid, pay_currency=arrived)
            asyncio.run(nowpay.process_order(o["id"], "t"))
            r = row(o["id"])
            self.assertEqual((r["status"], balance(uid)), ("review", 0), chosen)
            self.assertIn("currency", r["review_reason"])

    def test_admin_check_reports_each_network(self):
        admin = mk_user("admin")
        d = client(admin).post("/api/admin/pay/crypto/check").json()
        self.assertEqual(d["currencies"], {"usdttrc20": True, "usdtbsc": True})
        FAKE.currencies = ["btc", "usdttrc20"]
        try:
            d = client(admin).post("/api/admin/pay/crypto/check").json()
        finally:
            FAKE.currencies = ["btc", "eth", "usdttrc20", "usdtbsc"]
        self.assertEqual((d["currencies"], d["currency_listed"]), ({"usdttrc20": True, "usdtbsc": False}, False))
        self.assertEqual(client(admin).get("/api/admin/pay/crypto").json()["pay_currency"], "usdttrc20, usdtbsc")


# ---------------------------------------------------------------- 6. concurrency
class ConcurrencyTests(unittest.TestCase):
    def test_parallel_fulfilment_credits_once(self):
        uid = mk_user()
        oid, pid, _ = buy(uid, "pro")
        pay(pid)
        p = dict(FAKE.payments[str(pid)])
        results, barrier = [], threading.Barrier(10)

        def go():
            barrier.wait()
            results.append(nowpay.apply_payment(oid, p, "race")["result"])
        ts = [threading.Thread(target=go) for _ in range(10)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertEqual(results.count("fulfilled"), 1, results)
        self.assertEqual(balance(uid), 550)
        self.assertEqual(len(sql("SELECT * FROM billing_subs WHERE user_id=?", uid)), 1)
        self.assertEqual(len(sql("SELECT * FROM credit_ledger WHERE user_id=? AND kind='purchase'", uid)), 1)

    def test_parallel_notifications_over_http(self):
        uid = mk_user()
        oid, pid, _ = buy(uid)
        pay(pid)
        body = ipn_for(pid)
        codes = []
        ts = [threading.Thread(target=lambda: codes.append(post_ipn(body).status_code)) for _ in range(6)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        asyncio.run(nowpay.drain())
        self.assertEqual(set(codes), {200})
        self.assertEqual((row(oid)["status"], balance(uid)), ("approved", 550))

    def test_fulfil_refuses_an_order_that_changed_state_meanwhile(self):
        """A payout racing with an admin decision (or an expiry) must lose: only an open order can be paid out."""
        for state in ("rejected", "review", "expired", "failed", "cancelled", "refunded", "approved"):
            uid = mk_user()
            oid, pid, _ = buy(uid)
            pay(pid)
            sql("UPDATE billing_orders SET status=? WHERE id=?", state, oid)
            self.assertEqual(nowpay._fulfil(oid, "race", dict(FAKE.payments[str(pid)])), "lost_race", state)
            self.assertEqual((row(oid)["status"], balance(uid)), (state, 0), state)

    def test_the_ledger_alone_prevents_a_double_credit(self):
        """Even if the order status guard were bypassed, the ledger refuses a second purchase row for the same order."""
        uid = mk_user()
        oid, pid, _ = buy(uid)
        with closing(credits.db()) as con:
            self.assertTrue(credits.grant_product(con, uid, "boost", oid, "X"))
            con.commit()
        with closing(credits.db()) as con:
            self.assertFalse(credits.grant_product(con, uid, "boost", oid, "X"))
            con.commit()
        self.assertEqual(balance(uid), 550)

    def test_admin_and_automatic_paths_cannot_both_credit(self):
        uid, admin = mk_user(), mk_user("admin")
        oid, pid, _ = buy(uid)
        pay(pid, "partially_paid", paid=2)
        asyncio.run(nowpay.process_order(oid, "t"))
        pay(pid, "finished")
        asyncio.run(nowpay.process_order(oid, "t"))
        self.assertEqual(client(admin).post(f"/api/admin/pay/crypto/order/{oid}/resolve", json={"action": "approve", "note": "x"}).status_code, 200)
        asyncio.run(nowpay.process_order(oid, "t"))
        self.assertEqual(balance(uid), 550)


# ---------------------------------------------------------------- 7. secrets and logs
class SecretHandlingTests(unittest.TestCase):
    def test_no_secret_in_output_responses_events_or_orders(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            uid = mk_user()
            FAKE.down = True
            try:
                client(uid).post("/api/pay/crypto/order", json={"product": "topup", "accepted": True})
            finally:
                FAKE.down = False
            oid, pid, o = buy(uid, "boost")
            post_ipn(ipn_for(pid), sig="bad")
            pay(pid)
            post_ipn(ipn_for(pid))
            asyncio.run(nowpay.drain())
            r = client(uid).get(f"/api/pay/crypto/order/{oid}")
        dump = buf.getvalue() + r.text + json.dumps(sql("SELECT * FROM payment_events")) + json.dumps(sql("SELECT * FROM billing_orders"))
        for secret in (API_KEY, IPN_SECRET):
            self.assertNotIn(secret, dump)
        self.assertTrue(all(c[2] == "" or API_KEY not in c[2] for c in FAKE.calls), "the key travels in the header only")

    def test_wrong_api_key_is_a_clean_failure(self):
        old = os.environ["NOWPAYMENTS_API_KEY"]
        os.environ["NOWPAYMENTS_API_KEY"] = "wrong-key"
        try:
            uid = mk_user()
            r = client(uid).post("/api/pay/crypto/order", json={"product": "topup", "accepted": True})
        finally:
            os.environ["NOWPAYMENTS_API_KEY"] = old
        self.assertEqual(r.status_code, 502)
        self.assertNotIn("wrong-key", r.text)

    def test_no_withdrawal_or_payout_endpoint_is_ever_called(self):
        src = (ROOT / "app" / "nowpay.py").read_text()
        for word in ("payout", "withdraw", "/auth"):
            self.assertNotIn(f'"{word}', src.lower().replace("payouts", ""), word)
        self.assertEqual({(m, p.split("/")[1]) for m, p, _ in FAKE.calls}, {("POST", "payment"), ("GET", "payment"), ("GET", "status"), ("GET", "currencies")})


# ---------------------------------------------------------------- 8. migrations and the manual flow
class MigrationTests(unittest.TestCase):
    def test_every_new_table_gets_row_level_security_on_postgres(self):
        found = dbx._CREATE_TABLE.findall(credits.SCHEMA)
        self.assertIn("payment_events", found)
        self.assertIn("billing_orders", found)

    def test_migration_is_additive_and_repeatable(self):
        """Old-style database (no new columns, real rows) -> new code opens it: rows untouched, columns added, running twice is harmless."""
        d = Path(tempfile.mkdtemp())
        db = d / "old.db"
        import sqlite3
        raw = sqlite3.connect(db)
        raw.executescript("""
            CREATE TABLE credit_balances(user_id INTEGER PRIMARY KEY, balance INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE credit_ledger(id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, delta INTEGER NOT NULL, kind TEXT NOT NULL,
                ref TEXT NOT NULL DEFAULT '', note TEXT, admin_id INTEGER, created_at BIGINT NOT NULL);
            CREATE TABLE billing_orders(id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, product TEXT NOT NULL, method TEXT NOT NULL,
                amount_usd DOUBLE PRECISION NOT NULL, amount_local DOUBLE PRECISION, currency TEXT NOT NULL, ref TEXT UNIQUE NOT NULL, txid TEXT,
                status TEXT NOT NULL, note TEXT, created_at BIGINT NOT NULL, submitted_at BIGINT, decided_at BIGINT, decided_by INTEGER, terms_at BIGINT);
            CREATE TABLE billing_subs(id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, plan TEXT NOT NULL, starts_at BIGINT NOT NULL, ends_at BIGINT NOT NULL, order_id BIGINT);
            CREATE TABLE billing_settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
            INSERT INTO billing_orders(user_id,product,method,amount_usd,amount_local,currency,ref,txid,status,created_at) VALUES(7,'plus','trc20',15,15,'USDT','TL-OLD111','abc123456','approved',1700000000);
            INSERT INTO credit_ledger(user_id,delta,kind,ref,created_at) VALUES(7,550,'purchase','order:1',1700000001);
            INSERT INTO credit_balances VALUES(7,550);
            INSERT INTO billing_subs(user_id,plan,starts_at,ends_at,order_id) VALUES(7,'plus',1700000000,1702592000,1);
        """)
        raw.commit()
        raw.close()
        script = (f"import sys,os,json;sys.path.insert(0,{str(ROOT)!r});os.environ.pop('DATABASE_URL',None)\n"
                  "import httpx2,types;sys.modules.setdefault('httpx',httpx2)\n"
                  "from app import dbx,credits;from pathlib import Path\n"
                  f"dbx.SQLITE_PATH=Path({str(db)!r})\n"
                  "from contextlib import closing\n"
                  "for _ in range(2):\n"
                  "    dbx._ready.clear()\n"
                  "    with closing(credits.db()) as con:\n"
                  "        r=dict(con.execute('SELECT * FROM billing_orders').fetchone());b=con.execute('SELECT balance FROM credit_balances WHERE user_id=7').fetchone()[0]\n"
                  "        s=con.execute('SELECT COUNT(*) FROM billing_subs').fetchone()[0];t=con.execute(\"SELECT name FROM sqlite_master WHERE name='payment_events'\").fetchone()\n"
                  "print(json.dumps([r,b,s,bool(t)]))")
        out = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True)
        self.assertEqual(out.returncode, 0, out.stderr)
        r, bal, subs, has_events = json.loads(out.stdout.strip().splitlines()[-1])
        self.assertEqual((r["ref"], r["status"], r["txid"], r["product"], bal, subs, has_events), ("TL-OLD111", "approved", "abc123456", "plus", 550, 1, True))
        self.assertIsNone(r["provider_payment_id"])
        shutil.rmtree(d, ignore_errors=True)


class ManualFlowRegressionTests(unittest.TestCase):
    """The existing manual flow (pay outside the app, send the reference, admin approves) must behave exactly as before."""

    def test_manual_order_approve_and_plan(self):
        uid, admin = mk_user(), mk_user("admin")
        c, a = client(uid), client(admin)
        r = c.post("/api/billing/order", json={"product": "plus", "method": "trc20", "accepted": True})
        self.assertEqual(r.status_code, 200, r.text)
        o = r.json()["order"]
        self.assertEqual((o["status"], o["method_label"], o["amount_usd"]), ("pending", "USDT (TRC20)", 15))
        self.assertNotIn("provider_payment_id", o)
        self.assertEqual(c.post(f"/api/billing/order/{o['id']}/paid", json={"txid": "short"}).status_code, 422)
        self.assertEqual(c.post(f"/api/billing/order/{o['id']}/paid", json={"txid": "abcdef123456"}).status_code, 200)
        self.assertIn(o["id"], [x["id"] for x in a.get("/api/admin/billing").json()["open"]])
        self.assertEqual(a.post(f"/api/admin/billing/order/{o['id']}/approve").status_code, 200)
        self.assertEqual(a.post(f"/api/admin/billing/order/{o['id']}/approve").status_code, 409)
        self.assertEqual(balance(uid), 550)
        self.assertEqual([p["plan"] for p in credits.active_plans(uid)], ["plus"])
        self.assertTrue(credits.entitled({"id": uid, "tier": "free"}, "notify"))
        self.assertFalse(credits.entitled({"id": uid, "tier": "free"}, "mt5"))

    def test_manual_cancel_reject_adjust_and_spend(self):
        uid, admin = mk_user(), mk_user("admin")
        c, a = client(uid), client(admin)
        o = c.post("/api/billing/order", json={"product": "topup", "method": "trc20", "accepted": True}).json()["order"]
        self.assertEqual(c.post(f"/api/billing/order/{o['id']}/cancel").status_code, 200)
        o2 = c.post("/api/billing/order", json={"product": "topup", "method": "trc20", "accepted": True}).json()["order"]
        self.assertEqual(a.post(f"/api/admin/billing/order/{o2['id']}/reject", json={"note": "nope"}).status_code, 200)
        self.assertEqual(balance(uid), 0)
        with closing(auth.db()) as con:
            email = con.execute("SELECT email FROM users WHERE id=?", (uid,)).fetchone()["email"]
        self.assertEqual(a.post("/api/admin/billing/adjust", json={"email": email, "delta": 3, "note": "gift"}).status_code, 200)
        self.assertTrue(credits.spend(uid, "signal:1"))
        self.assertTrue(credits.spend(uid, "signal:1"), "the same signal is never charged twice")
        self.assertEqual(balance(uid), 2)
        credits.ensure_welcome(uid)
        credits.ensure_welcome(uid)
        self.assertEqual(balance(uid), 7)

    def test_invite_signup_and_daily_quota_unchanged(self):
        with closing(auth.db()) as con:
            con.execute("INSERT INTO invites(code,tier,uses_left) VALUES('NP-TEST','free',1)")
            con.commit()
        r = ANON.post("/api/auth/signup", json={"email": "invitee@test.example", "password": "password123", "invite": "NP-TEST"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(ANON.post("/api/auth/signup", json={"email": "again@test.example", "password": "password123", "invite": "NP-TEST"}).status_code, 403)
        me = ANON.get("/api/auth/me").json()
        self.assertEqual((me["tier"], me["used"], me["limit"]), ("free", 0, auth.LIMITS["free"]))
        u = {"id": 424242, "tier": "free"}
        self.assertEqual([auth.reserve_quota(u) for _ in range(auth.LIMITS["free"] + 1)], [True] * auth.LIMITS["free"] + [False])
        auth.release_quota(u)
        self.assertTrue(auth.reserve_quota(u))


if __name__ == "__main__":
    unittest.main(verbosity=2)
