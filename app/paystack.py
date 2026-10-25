"""Naira payments through Paystack (Checkout: card, bank transfer, USSD ... whatever the Paystack account offers).

What this adds, and what it deliberately does not touch
  * A member picks a product in Billing. The server prices it from credits.CATALOG (never from the browser) and converts it to
    Naira with the SAME ngn_rate setting the manual bank-transfer flow already uses. It stores the order in the EXISTING
    billing_orders table (method "paystack") BEFORE anything is sent to Paystack, then initialises the transaction server-side
    (POST /transaction/initialize) and returns Paystack's checkout address. The order's reference is the Paystack reference.
  * The browser returning from Paystack, and Paystack's webhook (x-paystack-signature, HMAC-SHA512 of the raw body with the secret
    key), only tell us WHICH order to look at. The decision is always taken from the transaction as fetched from the Paystack API
    with our secret key (GET /transaction/verify/{reference}). A forged or replayed request can therefore not credit anything.
  * Only the Paystack status "success" can fulfil an order, and only if reference, currency, amount (in kobo), live/test domain and
    the order / user recorded in the transaction metadata all match. Anything else that needs a person goes to status "review" and
    waits in Admin > Naira payments. Nothing is ever fulfilled on a browser redirect.
  * Fulfilment reuses credits.grant_product (the code the manual approval and NOWPayments use): signals go in through the existing
    ledger, a plan's notification / MT5 days through billing_subs. Three independent guards make a double credit impossible: the
    conditional status UPDATE (only one caller can move the order out of "pending"), the ledger's unique (user, "purchase",
    "order:<id>") row, and one database transaction around all of it.
  * Fixed-duration access, exactly as today: paying again extends a plan from the end of the current one. This module never creates
    a recurring Paystack plan or subscription and never stores a card authorisation.
  * A background loop re-checks open orders with Paystack, so a lost or late webhook costs minutes, not money.

Test keys are safe by construction: a key starting sk_test_ only ever shows the button to ADMIN accounts (whatever the mode), orders
are tagged (provider "paystack-test", reference TLN-T-...), and a sk_live_ key is refused unless PAYSTACK_ALLOW_LIVE=1 is set.

Switched off until: PAYSTACK_SECRET_KEY is set, DRIVERLINE_BASE_URL is the public https address, the ngn_rate setting is filled in,
and the owner switches "Naira payments" on in Admin. Manual payments and NOWPayments keep working either way."""
import asyncio
import hashlib
import hmac
import json
import os
import re
import secrets
import time
from contextlib import closing
from urllib.parse import quote, urlparse

import httpx
from starlette.exceptions import HTTPException
from starlette.responses import JSONResponse
from starlette.routing import Route

from . import auth, credits, dbx

METHOD = credits.PAYSTACK_METHOD
DEFAULT_API_BASE = "https://api.paystack.co"
PROVIDER_TEST, PROVIDER_LIVE = "paystack-test", "paystack"        # billing_orders.provider
IN_FLIGHT = ("ongoing", "pending", "processing", "queued")       # Paystack: money may still be on its way (bank transfer, USSD ...)
CLOSED_UNPAID = ("expired", "failed", "cancelled")               # we closed it without a payment; money arriving later is for a person
MAX_BODY = 256 * 1024
EXPIRY_GRACE = 120                                               # seconds past expiry before an unpaid order is closed
STUCK_IN_FLIGHT = 24 * 3600                                      # provider says "in progress" this long after expiry -> review
LOOKBACK = 3 * 86400                                             # how far back the background loop looks for open orders
_TRANSPORT = None                                                # tests plug a fake Paystack in here (httpx.MockTransport)

# What a member is told for each review reason. The internal wording never reaches the browser.
REVIEW_TEXT = {
    "mismatch": "The payment did not match the order exactly and is being checked by hand.",
    "paid_after_close": "A payment arrived after this order had closed. It is being checked by hand.",
    "stuck": "Your payment is taking unusually long to confirm. It is being checked by hand.",
    "reversed": "Paystack reported this payment as reversed. It is being checked by hand.",
    "duplicate": "This payment looks like a duplicate and is being checked by hand.",
}
STATUS_TEXT = {"pending": "Waiting for your payment", "approved": "Signals added", "failed": "Payment failed", "expired": "Order expired",
               "cancelled": "Cancelled", "review": "Being checked by our team", "rejected": "Not confirmed", "refunded": "Refunded"}


# ---------------------------------------------------------------- configuration (environment, read on every call)
def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _secret() -> str:
    return _env("PAYSTACK_SECRET_KEY")


def api_base() -> str:
    return (_env("PAYSTACK_API_BASE") or DEFAULT_API_BASE).rstrip("/")


def key_mode() -> str:
    """"test", "live" or "" (missing / not a Paystack secret key). Decided only by the key's own prefix."""
    k = _secret()
    return "test" if k.startswith("sk_test_") else "live" if k.startswith("sk_live_") else ""


def provider_name() -> str:
    return PROVIDER_TEST if key_mode() == "test" else PROVIDER_LIVE


def callback_base() -> str:
    return _env("DRIVERLINE_BASE_URL").rstrip("/")


def callback_url() -> str:
    return callback_base() + "/paystack.html"        # Paystack appends ?trxref=<reference>&reference=<reference>


def webhook_url() -> str:
    return callback_base() + "/api/pay/paystack/webhook"


def order_ttl() -> int:
    try:
        return min(max(int(_env("PAYSTACK_ORDER_TTL_MIN") or 60), 10), 1440) * 60
    except ValueError:
        return 3600


def problems() -> list:
    """What is still missing before Naira payments can run. Names only: never values."""
    out = []
    if not _secret():
        out.append("PAYSTACK_SECRET_KEY is not set")
    elif not key_mode():
        out.append("PAYSTACK_SECRET_KEY is not a Paystack secret key (it must start with sk_test_ or sk_live_)")
    elif key_mode() == "live" and _env("PAYSTACK_ALLOW_LIVE") != "1":
        out.append("PAYSTACK_SECRET_KEY is a LIVE key and real-money payments are not enabled (PAYSTACK_ALLOW_LIVE is not 1)")
    pk = _env("PAYSTACK_PUBLIC_KEY")
    if pk and key_mode() and not pk.startswith("pk_" + key_mode() + "_"):
        out.append(f"PAYSTACK_PUBLIC_KEY does not belong to the same mode as the secret key (expected pk_{key_mode()}_...)")
    base = callback_base()
    if not base:
        out.append("DRIVERLINE_BASE_URL is not set (Paystack needs a public address to return to)")
    elif not base.startswith("https://") and _env("PAYSTACK_ALLOW_HTTP_CALLBACK") != "1":
        out.append("DRIVERLINE_BASE_URL must start with https://")
    return out


_switch = {"ts": 0.0, "mode": "off", "rate": None}
MODES = {"off": "0", "admin": "admin", "public": "1"}        # what Admin stores for each mode


def _read_switch():
    """The owner's switch and the Naira rate, cached for 10 seconds. Trouble reading either means off / no rate."""
    if time.time() - _switch["ts"] < 10:
        return
    try:
        with closing(credits.db()) as con:
            r = con.execute("SELECT value FROM billing_settings WHERE key='paystack_enabled'").fetchone()
            g = con.execute("SELECT value FROM billing_settings WHERE key='ngn_rate'").fetchone()
        m = {"1": "public", "admin": "admin"}.get(r["value"] if r else "", "off")
        rate = credits._num(g["value"]) if g else None
    except Exception as e:
        print(f"[paystack] could not read switch: {type(e).__name__}", flush=True)
        m, rate = "off", None
    _switch.update(ts=time.time(), mode=m, rate=rate)


def mode() -> str:
    """"off", "admin" (pilot: only admin accounts see and can use it) or "public". Only decides who may START a payment: money
    already on its way is always processed, whatever the mode."""
    _read_switch()
    return _switch["mode"]


def ngn_rate():
    _read_switch()
    return _switch["rate"]


def enabled(user=None) -> bool:
    """May this member start a Naira payment now? With a TEST key only admin accounts may, whatever the mode. With a live key
    public mode also needs billing to be open to members, as the other payment methods do."""
    if problems() or not ngn_rate() or not user:
        return False
    m, admin = mode(), user.get("tier") == "admin"
    if key_mode() == "test":
        return m in ("admin", "public") and admin
    if m == "public":
        return credits.billing_on()
    return m == "admin" and admin


def naira_for(usd: float, rate: float) -> int:
    """Whole Naira for a USD price: the same rule as the manual bank-transfer flow (round(usd * ngn_rate))."""
    return int(round(float(usd) * float(rate)))


def public_info(user=None) -> dict:
    if not enabled(user):
        return {"enabled": False}
    rate = ngn_rate()
    return {"enabled": True, "label": "Pay in Naira (card, bank transfer, USSD)", "currency": "NGN",
            "pilot": mode() == "admin", "test": key_mode() == "test",
            "naira": {k: naira_for(credits._price_fields(user, k)["usd"], rate) for k in credits.CATALOG}}


def _now() -> int:
    return int(time.time())


def _f(v, default=0.0) -> float:
    try:
        x = float(v)
        return x if x == x and abs(x) != float("inf") else default
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------- the webhook signature
def verify_signature(raw: bytes, header: str, secret: str) -> bool:
    """Paystack signs the raw request body with HMAC-SHA512 and the account's secret key and sends the hex digest in
    x-paystack-signature. The raw bytes are used as received (never re-serialised)."""
    sig = (header or "").strip().lower()
    if not sig or not secret:
        return False
    mac = hmac.new(secret.encode(), raw, hashlib.sha512).hexdigest()
    return hmac.compare_digest(mac, sig)


# ---------------------------------------------------------------- the provider (Paystack REST API)
class ProviderError(Exception):
    def __init__(self, msg: str, status=None, detail: str = ""):
        super().__init__(msg)
        self.status, self.detail = status, detail


async def _request(method: str, path: str, body=None):
    """One call to Paystack with our secret key. The key is only ever placed in the Authorization header: never logged or returned."""
    key = _secret()
    if not key:
        raise ProviderError("not configured")
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(15.0, connect=6.0), transport=_TRANSPORT) as cl:
            r = await cl.request(method, api_base() + path, json=body, headers={"Authorization": "Bearer " + key})
    except httpx.HTTPError as e:
        raise ProviderError(f"network error ({type(e).__name__})")
    if r.status_code >= 400:
        try:
            msg = str(r.json().get("message", ""))[:200]
        except Exception:
            msg = ""
        raise ProviderError(f"HTTP {r.status_code}", r.status_code, msg)
    try:
        return r.json()
    except ValueError:
        raise ProviderError("provider sent invalid JSON")


def _checkout_ok(url) -> bool:
    """Only an https address on paystack.com may be handed to a member's browser."""
    try:
        u = urlparse(str(url))
    except ValueError:
        return False
    host = (u.hostname or "").lower()
    return u.scheme == "https" and (host == "paystack.com" or host.endswith(".paystack.com"))


async def initialize_transaction(email: str, kobo: int, ref: str, order_id: int, user_id: int, product: str) -> dict:
    """POST /transaction/initialize. The reply is checked against what we asked for before a member is sent anywhere."""
    body = {"email": email, "amount": kobo, "currency": "NGN", "reference": ref, "callback_url": callback_url(),
            "metadata": {"order_id": order_id, "user_id": user_id, "product": product, "app": "tradelens"}}
    d = await _request("POST", "/transaction/initialize", body)
    data = d.get("data") if isinstance(d, dict) and d.get("status") is True else None
    if not isinstance(data, dict):
        raise ProviderError("unexpected reply")
    if str(data.get("reference") or "") != ref:
        raise ProviderError("reply is for a different reference")
    if not _checkout_ok(data.get("authorization_url")):
        raise ProviderError("reply has no valid checkout address")
    return {"url": str(data["authorization_url"])}


async def verify_transaction(ref: str) -> dict:
    """GET /transaction/verify/{reference}: the only source of truth about whether money arrived."""
    d = await _request("GET", "/transaction/verify/" + quote(ref, safe=""))
    data = d.get("data") if isinstance(d, dict) and d.get("status") is True else None
    if not isinstance(data, dict):
        raise ProviderError("unexpected reply")
    return data


# ---------------------------------------------------------------- small helpers
_hits: dict = {}


def _limited(key: str, n: int, window: int) -> bool:
    """True when `key` has already done n things in the last `window` seconds (otherwise records this one)."""
    now = time.time()
    if len(_hits) > 5000:
        for k in [k for k, v in _hits.items() if not v or now - v[-1] > 3600]:
            _hits.pop(k, None)
    h = [t for t in _hits.get(key, []) if now - t < window]
    if len(h) >= n:
        _hits[key] = h
        return True
    h.append(now)
    _hits[key] = h
    return False


def _event(con, order_id, source: str, kind: str, status=None, detail: str = "", pid=None, dedupe=None, admin_id=None) -> bool:
    """One audit row in payment_events. `detail` is plain text without credentials. With a dedupe key a repeat is skipped (False);
    that rolls back the open transaction, so only use a key on a write that stands alone."""
    try:
        con.execute("INSERT INTO payment_events(order_id,provider_payment_id,source,kind,provider_status,detail,dedupe_key,admin_id,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?)", (order_id, pid, source, kind, status, str(detail)[:500], dedupe, admin_id, _now()))
    except Exception as e:
        if dedupe and dbx.is_duplicate(e):
            con.raw.rollback()
            return False
        raise
    return True


def _new_ref(test: bool) -> str:
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return ("TLN-T-" if test else "TLN-") + "".join(secrets.choice(alphabet) for _ in range(10))


_REF_RE = re.compile(r"^[A-Za-z0-9.=-]{6,64}$")


def _reason_code(reason) -> str:
    return str(reason or "").split(":", 1)[0]


def _expected_kobo(row) -> int:
    return int(round(float(row["pay_amount"] or 0) * 100))


def outcome(row) -> str:
    """The one word the member's page shows: success, pending, failed, cancelled, expired, review, refunded, rejected.
    A transaction Paystack calls "failed" stays payable (the customer may retry on the checkout page) until the order expires,
    so it is shown as failed while the order itself is still pending."""
    st, ps = row["status"], str(credits._col(row, "provider_status") or "").lower()
    if st == "approved":
        return "success"
    if st == "pending" and ps == "failed":
        return "failed"
    return st


def order_view(row, extra_balance=None) -> dict:
    """What the member's browser may see about a Paystack order. Never the secret key, never raw provider payloads."""
    out = credits._order_public(row)
    p = credits.CATALOG.get(row["product"], {})
    now = _now()
    exp = credits._col(row, "expires_at")
    live = row["status"] == "pending" and (not exp or now <= exp + EXPIRY_GRACE)
    url = credits._col(row, "pay_address")
    out.update(outcome=outcome(row), amount_naira=row["amount_local"], test=credits._col(row, "provider") == PROVIDER_TEST,
               checkout_url=url if live and _checkout_ok(url) else None, provider_status=credits._col(row, "provider_status"),
               signals=p.get("signals"), days=p.get("days") or 0, notify=bool(p.get("notify")), mt5=bool(p.get("mt5")),
               message=REVIEW_TEXT.get(_reason_code(credits._col(row, "review_reason"))) if row["status"] == "review" else None)
    if extra_balance is not None:
        out["balance"] = extra_balance
    return out


# ---------------------------------------------------------------- applying what Paystack says about a transaction
def _validate(row, d: dict, email) -> list:
    """Why a transaction Paystack calls successful still does not fit this order. Empty means it fits exactly."""
    bad = []
    if str(d.get("reference") or "") != row["ref"]:
        bad.append("reference differs")
    if str(d.get("currency") or "").upper() != "NGN" or str(row["currency"] or "").upper() != "NGN":
        bad.append(f"currency {d.get('currency')} differs from {row['currency']}")
    amt, expected = d.get("amount"), _expected_kobo(row)
    if isinstance(amt, bool) or not isinstance(amt, (int, float)) or float(amt) != int(amt):
        bad.append("amount missing or not a whole number of kobo")
    elif int(amt) != expected:
        bad.append(f"amount {int(amt)} kobo differs from the expected {expected}")
    if not d.get("id"):
        bad.append("no transaction id")
    want_domain = "test" if credits._col(row, "provider") == PROVIDER_TEST else "live"
    dom = str(d.get("domain") or "").lower()
    if dom and dom != want_domain:
        bad.append(f"transaction is {dom}, order is {want_domain}")
    if credits._col(row, "provider") != provider_name():
        bad.append("order was created under a different key mode")
    md = d.get("metadata")
    if isinstance(md, dict):
        for k, v in (("order_id", row["id"]), ("user_id", row["user_id"])):
            if k in md and str(md[k]) != str(v):
                bad.append(f"metadata {k} differs")
    cust = d.get("customer")
    if isinstance(cust, dict) and cust.get("email") and email and str(cust["email"]).strip().lower() != str(email).strip().lower():
        bad.append("customer email differs from the member's")
    return bad


def _to_review(con, row, reason: str, source: str, status=None, pid=None) -> str:
    """Parks an order for a person. Only an order that is open or closed-without-fulfilment can be parked."""
    cur = con.execute("UPDATE billing_orders SET status='review', review_reason=?, updated_at=? WHERE id=? "
                      "AND status IN ('pending','expired','failed','cancelled','review')", (reason[:300], _now(), row["id"]))
    if cur.rowcount == 1 and row["status"] != "review":
        _event(con, row["id"], source, "review", status, reason, pid)
    con.commit()
    return "review"


def _fulfil(oid: int, source: str, d: dict) -> str:
    """The one place a Paystack order is paid out. Returns "fulfilled", "duplicate", "review" or "lost_race"."""
    now = _now()
    tid = "ps:" + str(d.get("id") or "")                  # prefixed: the unique index is shared with NOWPayments payment ids
    with closing(credits.db()) as con:
        row = con.execute("SELECT * FROM billing_orders WHERE id=? AND method=?", (oid, METHOD)).fetchone()
        if not row or row["product"] not in credits.CATALOG:
            return "lost_race"
        paid = _f(d.get("amount")) / 100.0
        claim = ("UPDATE billing_orders SET status='approved', provider_status='success', provider_payment_id=?, actually_paid=?, "
                 "fulfilled_at=?, decided_at=?, updated_at=?, review_reason=NULL WHERE id=? AND status='pending'")
        args = (tid, paid, now, now, now, oid)
        # Step 1: take the order. Only one caller can move it out of "pending"; a concurrent one updates zero rows
        # (on PostgreSQL it waits for the first to commit, then re-checks the condition and also updates zero rows).
        try:
            n = con.execute(claim, args).rowcount
        except Exception as e:
            if not dbx.is_duplicate(e):                   # the unique index: one Paystack transaction can settle one order only
                raise
            con.raw.rollback()
            _to_review(con, row, f"duplicate: Paystack transaction {tid} is already attached to another order", source, "success", tid)
            return "review"
        if n != 1:
            con.raw.rollback()
            return "lost_race"
        # Step 2: credit through the existing ledger / subscription code, in the same transaction.
        if not credits.grant_product(con, row["user_id"], row["product"], oid, row["ref"]):
            con.execute(claim, args)                       # the credit already exists (grant rolled back): keep it, record the order
            _event(con, oid, source, "duplicate_guard", "success", "ledger already had this order's credit", tid)
            con.commit()
            return "duplicate"
        _event(con, oid, source, "fulfilled", "success", f"paid {paid:.2f} of {float(row['pay_amount'] or 0):.2f} NGN", tid)
        con.commit()
        return "fulfilled"


def apply_transaction(oid: int, d: dict, source: str) -> dict:
    """Moves an order according to a transaction as Paystack reports it NOW. Safe to call any number of times, in any order,
    from any number of threads: a status never moves backwards and only _fulfil can pay out."""
    ps, now = str(d.get("status") or "").lower(), _now()
    with closing(credits.db()) as con:
        row = con.execute("SELECT * FROM billing_orders WHERE id=? AND method=?", (oid, METHOD)).fetchone()
        if not row:
            return {"result": "no_such_order"}
        status = row["status"]
        if str(d.get("reference") or "") != row["ref"]:
            _event(con, oid, source, "mismatch", ps, "provider reply was for a different reference", None, f"mm:{oid}:{ps}:{now // 3600}")
            con.commit()
            return {"result": "ignored_mismatch", "status": status}
        if status != "approved":                           # what the provider reports; never rewrites a settled order
            con.execute("UPDATE billing_orders SET provider_status=?, last_polled_at=?, updated_at=? WHERE id=? AND status<>'approved'",
                        (ps or None, now, now, oid))
            con.commit()
        if status == "approved":
            if ps in ("reversed", "failed"):
                _event(con, oid, source, "anomaly", ps, f"provider reports {ps} for an order already fulfilled", None, f"an:{oid}:{ps}")
                con.commit()
            return {"result": "already_final", "status": status}
        if status in ("rejected", "refunded"):
            return {"result": "already_final", "status": status}
        if status == "review":
            return {"result": "in_review", "status": status}
        if status in CLOSED_UNPAID:                        # closed here: money after that is for a person
            if ps == "success":
                _to_review(con, row, f"paid_after_close: Paystack says success after the order was {status}", source, ps)
                return {"result": "review", "status": "review"}
            return {"result": "closed", "status": status}
        # ---- the order is open (pending)
        if ps == "success":
            email = credits._emails([row["user_id"]]).get(row["user_id"])
            bad = _validate(row, d, email)
            if bad:
                _to_review(con, row, "mismatch: " + "; ".join(bad), source, ps, str(d.get("id") or "") or None)
                return {"result": "review", "status": "review"}
        elif ps == "reversed":
            _to_review(con, row, "reversed: Paystack reports the payment as reversed", source, ps)
            return {"result": "review", "status": "review"}
        elif ps == "failed":
            _event(con, oid, source, "provider_failed", ps, d.get("gateway_response") or "attempt failed (the customer may retry)", None,
                   f"pf:{oid}:{now // 600}")
            con.commit()
            return {"result": "failed_attempt", "status": status}
        elif ps in IN_FLIGHT or ps in ("abandoned", ""):
            return {"result": "waiting", "status": status}
        else:
            _event(con, oid, source, "unknown_status", ps, "status not recognised: left unchanged", None, f"us:{oid}:{ps}")
            con.commit()
            return {"result": "unknown_status", "status": status}
    r = _fulfil(oid, source, d)
    return {"result": r, "status": "approved" if r in ("fulfilled", "duplicate") else None}


# ---------------------------------------------------------------- asking the provider, then applying
_tasks: set = set()


def _load(oid: int):
    with closing(credits.db()) as con:
        return con.execute("SELECT * FROM billing_orders WHERE id=? AND method=?", (oid, METHOD)).fetchone()


def _load_ref(ref: str):
    with closing(credits.db()) as con:
        return con.execute("SELECT * FROM billing_orders WHERE ref=? AND method=?", (ref, METHOD)).fetchone()


def _poll_failed(oid: int, e: ProviderError, source: str):
    with closing(credits.db()) as con:
        con.execute("UPDATE billing_orders SET last_polled_at=? WHERE id=?", (_now(), oid))
        _event(con, oid, source, "poll_error", None, f"{e} {e.detail}".strip(), None, f"pe:{oid}:{_now() // 300}")
        con.commit()


async def process_order(oid: int, source: str, min_interval: int = 0):
    """Fetches the transaction from Paystack and applies it. Returns apply_transaction's dict, or {"result": "provider_unavailable"}."""
    row = await asyncio.to_thread(_load, oid)
    if not row:
        return {"result": "no_such_order"}
    last = credits._col(row, "last_polled_at") or 0
    if min_interval and _now() - last < min_interval:
        return {"result": "throttled", "status": row["status"]}
    try:
        d = await verify_transaction(row["ref"])
    except ProviderError as e:
        await asyncio.to_thread(_poll_failed, oid, e, source)
        return {"result": "provider_unavailable"}
    return await asyncio.to_thread(apply_transaction, oid, d, source)


def _expire_if_due(oid: int) -> str:
    now = _now()
    with closing(credits.db()) as con:
        row = con.execute("SELECT * FROM billing_orders WHERE id=? AND method=?", (oid, METHOD)).fetchone()
        if not row or row["status"] != "pending" or not row["expires_at"] or now <= row["expires_at"] + EXPIRY_GRACE:
            return "open"
        ps = str(row["provider_status"] or "").lower()
        if ps in IN_FLIGHT:                                # a bank transfer / USSD may still be settling: do not close it
            if now > row["expires_at"] + STUCK_IN_FLIGHT:
                return _to_review(con, row, "stuck: payment in progress but not final a day after expiry", "sweep", ps)
            return "open"
        new = "failed" if ps == "failed" else "expired"
        cur = con.execute("UPDATE billing_orders SET status=?, updated_at=? WHERE id=? AND status='pending'", (new, now, oid))
        if cur.rowcount == 1:
            _event(con, oid, "sweep", new, row["provider_status"], "no successful payment before the order expired")
        con.commit()
        return new


async def sweep_once(limit: int = 40) -> dict:
    """Re-checks open orders with Paystack, then closes the ones that expired unpaid. Called by the loop and by tests."""
    def _open():
        with closing(credits.db()) as con:
            return [r["id"] for r in con.execute(
                "SELECT id FROM billing_orders WHERE method=? AND status='pending' AND created_at>? "
                "ORDER BY COALESCE(last_polled_at,0), id LIMIT ?", (METHOD, _now() - LOOKBACK, limit)).fetchall()]
    out = {"checked": 0, "unavailable": 0}
    if problems():
        return out
    for oid in await asyncio.to_thread(_open):
        r = await process_order(oid, "poll")
        out["checked"] += 1
        if r.get("result") == "provider_unavailable":
            out["unavailable"] += 1
            continue                                       # never close an order we could not verify
        await asyncio.to_thread(_expire_if_due, oid)
    return out


async def reconcile_loop():
    await asyncio.sleep(25)
    while True:
        try:
            await sweep_once()
        except Exception as e:
            print(f"[paystack] sweep failed: {type(e).__name__}", flush=True)
        await asyncio.sleep(60)


async def drain():
    """Waits for background work started by webhooks (used by tests and on shutdown)."""
    while _tasks:
        await asyncio.gather(*list(_tasks), return_exceptions=True)


# ---------------------------------------------------------------- member API
async def _reclaim(user_id: int):
    """A member at the open-order limit: verify, then close, their own overdue unpaid orders so they can start a new one."""
    def _due():
        with closing(credits.db()) as con:
            return [r["id"] for r in con.execute(
                "SELECT id FROM billing_orders WHERE user_id=? AND method=? AND status='pending' AND expires_at IS NOT NULL AND expires_at<? "
                "ORDER BY id LIMIT 10", (user_id, METHOD, _now() - EXPIRY_GRACE)).fetchall()]
    for oid in await asyncio.to_thread(_due):
        r = await process_order(oid, "member_reclaim")
        if r.get("result") != "provider_unavailable":
            await asyncio.to_thread(_expire_if_due, oid)


async def create_order(request):
    user = auth.require_user(request)
    if not enabled(user):
        raise HTTPException(409, "Naira payments are not available yet")
    d = await auth._json(request)
    product = d.get("product")
    if d.get("accepted") is not True:
        raise HTTPException(422, "Please accept the Terms, Refunds and Support policy to continue")
    if product not in credits.CATALOG:
        raise HTTPException(422, "Unknown product")
    if _limited(f"create:{user['id']}", 6, 600):
        raise HTTPException(429, "Too many attempts. Please wait a few minutes.")
    rate = ngn_rate()
    usd = credits._price_fields(user, product)["usd"]
    naira, now = naira_for(usd, rate), _now()
    if naira < 1:
        raise HTTPException(409, "Naira payments are not available for this product right now")
    provider, test = provider_name(), key_mode() == "test"

    def _prepare():
        with closing(credits.db()) as con:
            old = con.execute("SELECT * FROM billing_orders WHERE user_id=? AND method=? AND product=? AND status='pending' AND provider=? "
                              "AND amount_usd=? AND amount_local=? AND pay_address IS NOT NULL AND expires_at>? ORDER BY id DESC LIMIT 1",
                              (user["id"], METHOD, product, provider, usd, naira, now)).fetchone()
            if old:                                        # a double click or a second tab: show the same order, create nothing
                return old, None, False
            open_n = con.execute("SELECT COUNT(*) AS c FROM billing_orders WHERE user_id=? AND status IN ('pending','submitted','confirming')",
                                 (user["id"],)).fetchone()["c"]
            if open_n >= credits.OPEN_ORDERS_MAX:
                return None, None, True
            for _ in range(5):
                ref = _new_ref(test)
                try:                                        # stored BEFORE the member is sent to Paystack
                    oid = con.insert("INSERT INTO billing_orders(user_id,product,method,amount_usd,amount_local,currency,ref,status,created_at,"
                                     "terms_at,provider,pay_currency,pay_amount,pay_network,expires_at,updated_at) "
                                     "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                                     (user["id"], product, METHOD, usd, naira, "NGN", ref, "pending", now, now, provider, "NGN", float(naira),
                                      "test" if test else "live", now + order_ttl(), now))
                    _event(con, oid, "member", "created", None, f"{product} {usd} usd = {naira} NGN ({'test' if test else 'live'})")
                    con.commit()
                    return None, (oid, ref), False
                except Exception as e:
                    if not dbx.is_duplicate(e):
                        raise
                    con.raw.rollback()
            raise HTTPException(503, "Could not create the order. Please try again.")

    old, new, full = await asyncio.to_thread(_prepare)
    if full:
        await _reclaim(user["id"])
        old, new, full = await asyncio.to_thread(_prepare)
        if full:
            raise HTTPException(429, "You already have open orders. Finish one, or wait for it to expire, before starting another.")
    if old:
        return JSONResponse({"order": order_view(old)})
    oid, ref = new
    try:
        init = await initialize_transaction(user["email"], naira * 100, ref, oid, user["id"], product)
    except ProviderError as e:
        print(f"[paystack] could not initialise a transaction: {e} {e.detail}", flush=True)

        def _fail():
            with closing(credits.db()) as con:
                con.execute("UPDATE billing_orders SET status='failed', review_reason=?, updated_at=? WHERE id=? AND status='pending' AND pay_address IS NULL",
                            ("create_failed", _now(), oid))
                _event(con, oid, "member", "create_failed", None, f"{e} {e.detail}".strip())
                con.commit()
        await asyncio.to_thread(_fail)
        raise HTTPException(502, "The payment service is not available right now. Please try again in a few minutes.")

    def _store():
        with closing(credits.db()) as con:
            # pay_address holds Paystack's checkout address for this order (a card order has no deposit address, so the
            # existing column is reused instead of adding one). Only the owner's order_view ever returns it, and only while pending.
            con.execute("UPDATE billing_orders SET pay_address=?, provider_status='initialized', updated_at=? WHERE id=? AND pay_address IS NULL",
                        (init["url"], _now(), oid))
            _event(con, oid, "member", "provider_init", "initialized", "transaction initialised")
            con.commit()
            return con.execute("SELECT * FROM billing_orders WHERE id=?", (oid,)).fetchone()
    row = await asyncio.to_thread(_store)
    return JSONResponse({"order": order_view(row)})


async def get_status(request):
    """The member's own order by reference (the page Paystack returns to), optionally re-checked with Paystack."""
    user = auth.require_user(request)
    ref = request.query_params.get("reference") or request.query_params.get("trxref") or ""
    if not _REF_RE.match(ref):
        raise HTTPException(404, "Order not found")
    row = await asyncio.to_thread(_load_ref, ref)
    if not row or row["user_id"] != user["id"]:
        raise HTTPException(404, "Order not found")
    if request.query_params.get("refresh") == "1" and row["status"] == "pending" and not problems() \
            and not _limited(f"refresh:{row['id']}", 30, 600):
        await process_order(row["id"], "member_refresh", min_interval=4)
        row = await asyncio.to_thread(_load, row["id"])
    bal = await asyncio.to_thread(credits.balance, user["id"]) if row["status"] == "approved" else None
    return JSONResponse({"order": order_view(row, bal)})


async def cancel_order(request):
    """The member abandons an unpaid order. Paystack is asked first, so a payment that did go through is honoured, not cancelled."""
    user, oid = auth.require_user(request), request.path_params["oid"]
    row = await asyncio.to_thread(_load, oid)
    if not row or row["user_id"] != user["id"]:
        raise HTTPException(404, "Order not found")
    if row["status"] == "pending":
        if problems():
            raise HTTPException(409, "Payments are not available right now. Please try again later.")
        r = await process_order(oid, "member_cancel")
        if r.get("result") == "provider_unavailable":
            raise HTTPException(502, "We could not check your payment with Paystack just now. Nothing was changed. Please try again.")

        def _cancel():
            with closing(credits.db()) as con:
                cur = con.execute("UPDATE billing_orders SET status='cancelled', updated_at=? WHERE id=? AND user_id=? AND status='pending'",
                                  (_now(), oid, user["id"]))
                if cur.rowcount == 1:
                    _event(con, oid, "member", "cancelled", None, "cancelled by the member")
                con.commit()
        await asyncio.to_thread(_cancel)
        row = await asyncio.to_thread(_load, oid)
    bal = await asyncio.to_thread(credits.balance, user["id"]) if row["status"] == "approved" else None
    return JSONResponse({"order": order_view(row, bal)})


# ---------------------------------------------------------------- the webhook
def _record_webhook(data: dict, raw: bytes):
    """Stores the verified notification and finds its order by reference. Returns the order id or None."""
    ev = str(data.get("event") or "")[:60]
    obj = data.get("data") if isinstance(data.get("data"), dict) else {}
    ref = str(obj.get("reference") or obj.get("transaction_reference") or "")
    key = "psw:" + hashlib.sha256(raw).hexdigest()
    with closing(credits.db()) as con:
        r = con.execute("SELECT id FROM billing_orders WHERE method=? AND ref=?", (METHOD, ref)).fetchone() if _REF_RE.match(ref) else None
        if r:
            _event(con, r["id"], "webhook", "webhook", str(obj.get("status") or "").lower() or None, f"event {ev}", None, key)
            con.commit()
            return r["id"]
        _event(con, None, "webhook", "unmatched", str(obj.get("status") or "").lower() or None, f"event {ev}: no Paystack order for reference {ref[:64]!r}", None, key)
        con.commit()
        return None


async def webhook(request):
    ip = request.client.host if request.client else "?"
    if _limited(f"wh:{ip}", 300, 60):
        raise HTTPException(429, "Too many requests")
    secret = _secret()
    if not secret:                                          # fail closed: without the key nothing can be verified
        raise HTTPException(503, "Not configured")
    raw = await request.body()
    if len(raw) > MAX_BODY:
        raise HTTPException(413, "Too large")
    if not verify_signature(raw, request.headers.get("x-paystack-signature", ""), secret):
        print(f"[paystack] webhook rejected: bad signature (from {ip})", flush=True)
        raise HTTPException(401, "Invalid signature")
    try:
        data = json.loads(raw)
    except ValueError:
        raise HTTPException(400, "Invalid JSON")
    if not isinstance(data, dict):
        raise HTTPException(400, "Not an event")
    oid = await asyncio.to_thread(_record_webhook, data, raw)
    if oid:                                                  # whatever the event says, the answer comes from Paystack itself
        task = asyncio.create_task(process_order(oid, "webhook"))
        _tasks.add(task)
        task.add_done_callback(_tasks.discard)
        await asyncio.wait({task}, timeout=5.0)             # whatever is not done goes on in the background
    return JSONResponse({"ok": True})                       # 200 also for events we do not use, so Paystack does not retry them


# ---------------------------------------------------------------- admin API
def _admin_rows(status: str, limit: int):
    with closing(credits.db()) as con:
        if status == "open":
            q, a = "status='pending'", ()
        elif status == "all":
            q, a = "1=1", ()
        else:
            q, a = "status=?", (status,)
        rows = con.execute(f"SELECT * FROM billing_orders WHERE method=? AND {q} ORDER BY id DESC LIMIT ?", (METHOD, *a, limit)).fetchall()
        counts = {r["status"]: r["c"] for r in con.execute(
            "SELECT status, COUNT(*) AS c FROM billing_orders WHERE method=? GROUP BY status", (METHOD,)).fetchall()}
        exc = [dict(r) for r in con.execute(
            "SELECT id,order_id,kind,provider_status,detail,source,created_at FROM payment_events "
            "WHERE source IN ('webhook','member','member_refresh','member_cancel','member_reclaim','poll','sweep') AND kind IN "
            "('review','unmatched','anomaly','mismatch','duplicate_guard','create_failed','provider_failed') ORDER BY id DESC LIMIT 30").fetchall()]
    return rows, counts, exc


async def admin_view(request):
    auth.require_admin(request)
    status = request.query_params.get("status", "open")
    if status not in ("open", "all", "review", "approved", "failed", "expired", "cancelled", "refunded", "rejected"):
        status = "open"
    rows, counts, exc = await asyncio.to_thread(_admin_rows, status, 60)
    names = credits._emails([r["user_id"] for r in rows])
    orders = [{**credits._order_public(r), "email": names.get(r["user_id"], "?"), "pay_amount": r["pay_amount"], "actually_paid": r["actually_paid"],
               "review_reason": r["review_reason"], "provider_status": r["provider_status"], "provider_payment_id": r["provider_payment_id"],
               "last_polled_at": r["last_polled_at"], "test": r["provider"] == PROVIDER_TEST} for r in rows]
    base = bool(callback_base())
    return JSONResponse({"mode": mode(), "problems": problems(), "key_mode": key_mode(), "ngn_rate": ngn_rate(), "billing_open": credits.billing_on(),
                         "webhook_url": webhook_url() if base else "", "callback_url": callback_url() if base else "", "api_base": api_base(),
                         "status": status, "counts": counts, "orders": orders, "exceptions": exc})


async def admin_toggle(request):
    u = auth.require_admin(request)
    d = await auth._json(request)
    m = d.get("mode")
    if m not in MODES:
        raise HTTPException(422, "mode must be off, admin or public")
    if m != "off" and problems():
        raise HTTPException(409, "Not ready: " + "; ".join(problems()))
    if m != "off" and not ngn_rate():
        raise HTTPException(409, "Set the Naira rate (ngn_rate) in Billing first: Naira prices come from it.")
    if m == "public" and key_mode() != "live":
        raise HTTPException(409, "Public mode needs a live Paystack key. With a test key only admin accounts can pay.")
    if m == "public" and not credits.billing_on():
        raise HTTPException(409, "Billing is not open to members yet. Open billing first, or stay in admin pilot mode.")

    def _save():
        with closing(credits.db()) as con:
            credits._put(con, "paystack_enabled", MODES[m])
            _event(con, None, "admin", "toggle", None, f"Naira payments: {m}", admin_id=u["id"])
            con.commit()
    await asyncio.to_thread(_save)
    _switch["ts"] = 0.0
    print(f"[paystack] Naira payments set to {m} by admin {u['id']}", flush=True)
    return JSONResponse({"ok": True})


async def admin_check(request):
    """Asks Paystack whether it accepts our key. Looks up a reference that cannot exist: a valid key answers "not found", a wrong key
    is refused. Creates and changes nothing."""
    auth.require_admin(request)
    out = {"configured": not problems(), "problems": problems(), "key_mode": key_mode(), "key_accepted": None, "detail": ""}
    if out["problems"]:
        return JSONResponse(out)
    try:
        await _request("GET", "/transaction/verify/" + quote("tl-connection-check-" + secrets.token_hex(4), safe=""))
        out["key_accepted"] = True
    except ProviderError as e:
        out["key_accepted"] = e.status == 404                 # unknown reference: the key itself was accepted
        out["detail"] = "" if out["key_accepted"] else f"{e} {e.detail}".strip()
    return JSONResponse(out)


async def admin_reconcile(request):
    """Re-reads one order from Paystack and applies it. The safe answer to a lost or late webhook."""
    u, oid = auth.require_admin(request), request.path_params["oid"]
    if problems():
        raise HTTPException(409, "Not configured: " + "; ".join(problems()))
    if not await asyncio.to_thread(_load, oid):
        raise HTTPException(404, "Order not found")
    r = await process_order(oid, f"admin:{u['id']}")
    if r.get("result") == "provider_unavailable":
        raise HTTPException(502, "Paystack could not be reached. Nothing was changed.")
    row = await asyncio.to_thread(_load, oid)
    return JSONResponse({"ok": True, "result": r.get("result"), "status": row["status"], "provider_status": row["provider_status"]})


async def admin_resolve(request):
    """The owner's decision on an order in review: approve (credit it) or reject. A reason is required and recorded."""
    u, oid = auth.require_admin(request), request.path_params["oid"]
    d = await auth._json(request)
    action, note = d.get("action"), str(d.get("note", "")).strip()[:200]
    if action not in ("approve", "reject"):
        raise HTTPException(422, "action must be approve or reject")
    if not note:
        raise HTTPException(422, "A note is required so the record explains the decision")

    def _do():
        now = _now()
        with closing(credits.db()) as con:
            row = con.execute("SELECT * FROM billing_orders WHERE id=? AND method=?", (oid, METHOD)).fetchone()
            if not row:
                raise HTTPException(404, "Order not found")
            if action == "reject":
                cur = con.execute("UPDATE billing_orders SET status='rejected', note=?, decided_at=?, decided_by=?, updated_at=? "
                                  "WHERE id=? AND status='review'", (note, now, u["id"], now, oid))
                if cur.rowcount != 1:
                    raise HTTPException(409, "Only an order in review can be rejected here")
                _event(con, oid, "admin", "resolved_reject", row["provider_status"], note, row["provider_payment_id"], admin_id=u["id"])
                con.commit()
                return
            if row["product"] not in credits.CATALOG:
                raise HTTPException(409, "This order's product no longer exists")
            cur = con.execute("UPDATE billing_orders SET status='approved', note=?, fulfilled_at=?, decided_at=?, decided_by=?, updated_at=? "
                              "WHERE id=? AND status='review'", (note, now, now, u["id"], now, oid))
            if cur.rowcount != 1:
                raise HTTPException(409, "Only an order in review can be approved here")
            if not credits.grant_product(con, row["user_id"], row["product"], oid, row["ref"], u["id"]):
                con.execute("UPDATE billing_orders SET status='approved', note=?, fulfilled_at=?, decided_at=?, decided_by=?, updated_at=? WHERE id=?",
                            (note, now, now, u["id"], now, oid))
            _event(con, oid, "admin", "resolved_approve", row["provider_status"], note, row["provider_payment_id"], admin_id=u["id"])
            con.commit()
    await asyncio.to_thread(_do)
    return JSONResponse({"ok": True})


async def admin_events(request):
    auth.require_admin(request)
    oid = request.path_params["oid"]

    def _get():
        with closing(credits.db()) as con:
            return [dict(r) for r in con.execute(
                "SELECT id,source,kind,provider_status,detail,admin_id,created_at FROM payment_events WHERE order_id=? ORDER BY id", (oid,)).fetchall()]
    return JSONResponse({"events": await asyncio.to_thread(_get)})


routes = [
    Route("/api/pay/paystack/order", create_order, methods=["POST"]),
    Route("/api/pay/paystack/status", get_status, methods=["GET"]),
    Route("/api/pay/paystack/order/{oid:int}/cancel", cancel_order, methods=["POST"]),
    Route("/api/pay/paystack/webhook", webhook, methods=["POST"]),
    Route("/api/admin/pay/paystack", admin_view, methods=["GET"]),
    Route("/api/admin/pay/paystack/toggle", admin_toggle, methods=["POST"]),
    Route("/api/admin/pay/paystack/check", admin_check, methods=["POST"]),
    Route("/api/admin/pay/paystack/order/{oid:int}/reconcile", admin_reconcile, methods=["POST"]),
    Route("/api/admin/pay/paystack/order/{oid:int}/resolve", admin_resolve, methods=["POST"]),
    Route("/api/admin/pay/paystack/order/{oid:int}/events", admin_events, methods=["GET"]),
]
