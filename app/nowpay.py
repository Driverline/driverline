"""Automatic USDT (TRC20) payments through NOWPayments.

What this adds, and what it deliberately does not touch
  * A member picks a product in Billing. The server prices it from credits.CATALOG (never from the browser), asks NOWPayments
    for a payment (POST /payment), stores the order in the EXISTING billing_orders table (method "nowpayments") and shows the
    deposit address, exact USDT amount and network that NOWPayments returned.
  * NOWPayments calls our IPN endpoint when the payment changes. The signature (x-nowpayments-sig, HMAC-SHA512 with the IPN
    secret) is checked first. The IPN only tells us WHICH order to look at: the decision is always taken from the payment as
    fetched from the NOWPayments API with our API key (GET /payment/{id}). A forged or replayed body can therefore not
    credit anything.
  * Only the provider status "finished" can fulfil an order, and only if the payment id, order id, price, currency and the
    amount actually paid all match the order. Anything else that needs a person (underpaid, paid after the order closed, a
    mismatch) goes to status "review" and waits in Admin > Crypto payments. Nothing is ever fulfilled on a browser redirect.
  * Fulfilment reuses credits.grant_product (the same code the manual Admin approval uses): the signals go in through the
    existing ledger, and a plan's notification / MT5 days through billing_subs. Three independent guards make a double credit
    impossible: the conditional status UPDATE (only one caller can move the order to "approved"), the ledger's unique
    (user, "purchase", "order:<id>") row, and all of it inside one database transaction.
  * A background loop re-checks open orders with the provider, so a lost or late IPN costs minutes, not money.
  * Receiving money needs no payout permission. No withdrawal / payout endpoint is called anywhere in this module.

Switched off until: NOWPAYMENTS_API_KEY and NOWPAYMENTS_IPN_SECRET are set, DRIVERLINE_BASE_URL is the public https address,
billing is open, and the owner switches "Automatic crypto payments" on in Admin. Manual payments keep working either way."""
import asyncio
import hashlib
import hmac
import json
import os
import secrets
import time
from contextlib import closing
from datetime import datetime, timezone
from decimal import Decimal

import httpx
from starlette.exceptions import HTTPException
from starlette.responses import JSONResponse
from starlette.routing import Route

from . import auth, credits, dbx

AUTO = credits.AUTO_METHOD
DEFAULT_API_BASE = "https://api.nowpayments.io/v1"
DEFAULT_PAY_CURRENCY = "usdttrc20"                      # NOWPayments' code for USDT on the TRON network
NETWORKS = {"usdttrc20": "TRON (TRC20)", "usdtbsc": "BNB Smart Chain (BEP20)"}
SHORT = {"usdttrc20": "TRC20", "usdtbsc": "BEP20"}
OPEN = ("pending", "confirming")                       # waiting for money / money seen, not final
FINAL = ("approved", "rejected", "cancelled", "refunded")
PRICE_TOLERANCE = 0.005                                # USD; the price echoed by the provider must equal ours
UNDERPAY_EPS = 1e-8                                    # float noise only: a real shortfall is never tolerated
OVERPAY_NOTE_RATIO = 1.001                             # above this the extra is recorded for the owner
MAX_BODY = 64 * 1024
EXPIRY_GRACE = 120                                     # seconds past expiry before an unpaid order is closed
STUCK_CONFIRMING = 24 * 3600                           # payment seen but never finished this long after expiry -> review
LOOKBACK = 3 * 86400                                   # how far back the background loop looks for open orders
_TRANSPORT = None                                      # tests plug a fake NOWPayments in here (httpx.MockTransport)

# What a member is told for each review reason. The internal wording never reaches the browser.
REVIEW_TEXT = {
    "underpaid": "We received less than the order total. Support will contact you, or you can quote the reference to support.",
    "paid_after_close": "A payment arrived after this order had closed. It is being checked by hand.",
    "mismatch": "The payment did not match the order exactly and is being checked by hand.",
    "stuck": "Your payment is taking unusually long to confirm. It is being checked by hand.",
    "extra_deposit": "An additional deposit was seen for this order. It is being checked by hand.",
}


# ---------------------------------------------------------------- configuration (environment, read on every call)
def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def api_base() -> str:
    return (_env("NOWPAYMENTS_API_BASE") or DEFAULT_API_BASE).rstrip("/")


def pay_currencies() -> list:
    """The coins/networks a member may choose from, first = the default. NOWPAYMENTS_PAY_CURRENCIES is a comma list (for example
    "usdttrc20,usdtbsc"); unset, it falls back to NOWPAYMENTS_PAY_CURRENCY, then to USDT TRC20 only. Only codes this module has a
    label for are accepted, so a typo can never put an unknown currency in front of a member."""
    raw = _env("NOWPAYMENTS_PAY_CURRENCIES") or _env("NOWPAYMENTS_PAY_CURRENCY") or DEFAULT_PAY_CURRENCY
    out = []
    for c in raw.lower().replace(";", ",").split(","):
        c = c.strip()
        if c in NETWORKS and c not in out:
            out.append(c)
    return out or [DEFAULT_PAY_CURRENCY]


def pay_currency() -> str:
    """The default currency (the first one offered)."""
    return pay_currencies()[0]


def network_label(code=None) -> str:
    code = code or pay_currency()
    return NETWORKS.get(code, code.upper())


def callback_base() -> str:
    return _env("DRIVERLINE_BASE_URL").rstrip("/")


def ipn_url() -> str:
    return callback_base() + "/api/pay/nowpayments/ipn"


def order_ttl() -> int:
    try:
        return min(max(int(_env("NOWPAYMENTS_ORDER_TTL_MIN") or 60), 10), 1440) * 60
    except ValueError:
        return 3600


def problems() -> list:
    """What is still missing before automatic payments can run. Names only: never values."""
    out = []
    if not _env("NOWPAYMENTS_API_KEY"):
        out.append("NOWPAYMENTS_API_KEY is not set")
    if not _env("NOWPAYMENTS_IPN_SECRET"):
        out.append("NOWPAYMENTS_IPN_SECRET is not set")
    base = callback_base()
    if not base:
        out.append("DRIVERLINE_BASE_URL is not set (NOWPayments needs a public address to call)")
    elif not base.startswith("https://") and _env("NOWPAYMENTS_ALLOW_HTTP_CALLBACK") != "1":
        out.append("DRIVERLINE_BASE_URL must start with https://")
    return out


_switch = {"ts": 0.0, "mode": "off"}
MODES = {"off": "0", "admin": "admin", "public": "1"}      # what Admin stores for each mode


def mode() -> str:
    """The owner's switch in Admin: "off", "admin" (pilot: only admin accounts see and can use it) or "public".
    Trouble reading it means off. This only decides who may START a payment: money that is already on its way is
    always processed, whatever the mode."""
    if time.time() - _switch["ts"] < 10:
        return _switch["mode"]
    try:
        with closing(credits.db()) as con:
            r = con.execute("SELECT value FROM billing_settings WHERE key='nowpayments_enabled'").fetchone()
        m = {"1": "public", "admin": "admin"}.get(r["value"] if r else "", "off")
    except Exception as e:
        print(f"[nowpay] could not read switch: {type(e).__name__}", flush=True)
        m = "off"
    _switch.update(ts=time.time(), mode=m)
    return m


def enabled(user=None) -> bool:
    """May this member start an automatic payment now? Public mode also needs billing to be open to members, as manual
    payments do. The admin pilot does not, so the owner can test while billing is still closed to everyone else."""
    if problems():
        return False
    m = mode()
    if m == "public":
        return credits.billing_on()
    return m == "admin" and bool(user) and user.get("tier") == "admin"


def public_info(user=None) -> dict:
    if not enabled(user):
        return {"enabled": False}
    return {"enabled": True, "label": credits.AUTO_LABEL, "currency": "USDT", "network": network_label(),
            "pilot": mode() == "admin",
            "options": [{"code": c, "network": network_label(c), "short": SHORT.get(c, c.upper())} for c in pay_currencies()]}


def _now() -> int:
    return int(time.time())


def _f(v, default=0.0) -> float:
    try:
        x = float(v)
        return x if x == x and abs(x) != float("inf") else default
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------- the IPN signature
def _js_number(v) -> str:
    """A number the way JavaScript's JSON.stringify writes it (3.0 -> 3, 0.00001 -> 0.00001, 1e-7 -> 1e-7)."""
    if isinstance(v, int) and abs(v) <= 2 ** 53:
        return str(v)
    f = float(v)                                        # JavaScript has no integers: a big one is a double there too
    if f != f or f in (float("inf"), float("-inf")):
        return "null"
    if f == 0:
        return "0"
    sign, f = ("-", -f) if f < 0 else ("", f)
    _, digits, exp = Decimal(repr(f)).as_tuple()
    ds = list(digits)
    while len(ds) > 1 and ds[-1] == 0:
        ds.pop()
        exp += 1
    s, k = "".join(map(str, ds)), len(ds)
    n = exp + k                                         # value = 0.s x 10^n, ECMAScript Number::toString
    if k <= n <= 21:
        return sign + s + "0" * (n - k)
    if 0 < n <= 21:
        return sign + s[:n] + "." + s[n:]
    if -6 < n <= 0:
        return sign + "0." + "0" * (-n) + s
    e = n - 1
    mant = s if k == 1 else s[0] + "." + s[1:]
    return sign + mant + "e" + ("+" if e >= 0 else "-") + str(abs(e))


def _canon(o, js: bool) -> str:
    if isinstance(o, dict):
        return "{" + ",".join(json.dumps(str(k), ensure_ascii=False) + ":" + _canon(o[k], js) for k in sorted(o)) + "}"
    if isinstance(o, list):
        return "[" + ",".join(_canon(x, js) for x in o) + "]"
    if o is True:
        return "true"
    if o is False:
        return "false"
    if o is None:
        return "null"
    if isinstance(o, (int, float)):
        return _js_number(o) if js else json.dumps(o)
    return json.dumps(o, ensure_ascii=False)


def verify_signature(raw: bytes, header: str, secret: str) -> bool:
    """NOWPayments signs the JSON body with its keys sorted alphabetically (nested objects too) using HMAC-SHA512 and the IPN
    secret, and sends the hex digest in x-nowpayments-sig. Their examples re-serialise the parsed body, and languages differ in
    how they print numbers, so the same sorted body is tried in the JavaScript number style, the Python style and as received.
    Every candidate is an HMAC under the secret over the same content, so accepting any of them gives a forger nothing."""
    sig = (header or "").strip().lower()
    if not sig or not secret:
        return False
    try:
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return False
    cands = (_canon(data, True), _canon(data, False), raw.decode("utf-8", "replace"))
    ok = False
    for c in cands:                                      # no early exit: same work whichever candidate matches
        mac = hmac.new(secret.encode(), c.encode(), hashlib.sha512).hexdigest()
        ok = hmac.compare_digest(mac, sig) or ok
    return ok


# ---------------------------------------------------------------- the provider (NOWPayments REST API)
class ProviderError(Exception):
    def __init__(self, msg: str, status=None, detail: str = ""):
        super().__init__(msg)
        self.status, self.detail = status, detail


async def _request(method: str, path: str, body=None):
    """One call to NOWPayments with our API key. The key is only ever placed in the request header: never logged or returned."""
    key = _env("NOWPAYMENTS_API_KEY")
    if not key:
        raise ProviderError("not configured")
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(12.0, connect=6.0), transport=_TRANSPORT) as cl:
            r = await cl.request(method, api_base() + path, json=body, headers={"x-api-key": key})
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


def _parse_time(v):
    try:
        return int(datetime.fromisoformat(str(v).replace("Z", "+00:00")).astimezone(timezone.utc).timestamp())
    except (TypeError, ValueError):
        return None


async def create_provider_payment(ref: str, usd: float, label: str, currency=None) -> dict:
    """POST /payment. The reply is checked against what we asked for before a member is shown any address."""
    currency = currency or pay_currency()
    body = {"price_amount": usd, "price_currency": "usd", "pay_currency": currency, "ipn_callback_url": ipn_url(),
            "order_id": ref, "order_description": f"{label} ({ref})"}
    d = await _request("POST", "/payment", body)
    if not isinstance(d, dict):
        raise ProviderError("unexpected reply")
    pid, addr, amt = str(d.get("payment_id") or ""), str(d.get("pay_address") or ""), _f(d.get("pay_amount"))
    if not pid or not addr or amt <= 0:
        raise ProviderError("reply is missing the payment id, address or amount")
    if str(d.get("pay_currency") or "").lower() != currency:
        raise ProviderError("reply is for a different currency")
    if d.get("order_id") not in (None, "", ref):
        raise ProviderError("reply is for a different order")
    if d.get("price_amount") not in (None, "") and abs(_f(d["price_amount"], -1) - usd) > PRICE_TOLERANCE:
        raise ProviderError("reply has a different price")
    now = _now()
    exp = _parse_time(d.get("expiration_estimate_date") or d.get("valid_until"))
    exp = min(max(exp, now + 300), now + 7 * 86400) if exp else now + order_ttl()
    return {"payment_id": pid, "pay_address": addr, "pay_amount": amt, "pay_currency": currency,
            "network": str(d.get("network") or "") or network_label(currency), "status": str(d.get("payment_status") or "waiting").lower(),
            "expires_at": exp}


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
    """One audit row. `detail` is plain text without credentials. With a dedupe key a repeat is skipped (False); that rolls back
    the open transaction, so only use a key on a write that stands alone."""
    try:
        con.execute("INSERT INTO payment_events(order_id,provider_payment_id,source,kind,provider_status,detail,dedupe_key,admin_id,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?)", (order_id, pid, source, kind, status, str(detail)[:500], dedupe, admin_id, _now()))
    except Exception as e:
        if dedupe and dbx.is_duplicate(e):
            con.raw.rollback()
            return False
        raise
    return True


def _new_ref() -> str:
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return "TLP-" + "".join(secrets.choice(alphabet) for _ in range(10))


def _reason_code(reason) -> str:
    return str(reason or "").split(":", 1)[0]


def order_view(row, extra_balance=None) -> dict:
    """What the member's browser may see about an automatic order."""
    out = credits._order_public(row)
    p = credits.CATALOG.get(row["product"], {})
    live = row["status"] in OPEN
    out.update(network=network_label(str(credits._col(row, "pay_currency") or "") or None), asset="USDT",
               pay_amount=credits._col(row, "pay_amount"), actually_paid=credits._col(row, "actually_paid"),
               pay_address=credits._col(row, "pay_address") if live else None,
               signals=p.get("signals"), days=p.get("days") or 0, notify=bool(p.get("notify")), mt5=bool(p.get("mt5")),
               message=REVIEW_TEXT.get(_reason_code(credits._col(row, "review_reason"))) if row["status"] == "review" else None)
    if extra_balance is not None:
        out["balance"] = extra_balance
    return out


# ---------------------------------------------------------------- applying what the provider says about a payment
def _validate(row, p: dict) -> list:
    """Why a payment the provider calls finished still does not fit this order. Empty means it fits exactly."""
    bad = []
    if str(p.get("payment_id") or "") != str(row["provider_payment_id"]):
        bad.append("payment id differs")
    if str(p.get("order_id") or "") != str(row["ref"]):
        bad.append("order id differs")
    if str(p.get("price_currency") or "").lower() != "usd" or abs(_f(p.get("price_amount"), -1) - float(row["amount_usd"])) > PRICE_TOLERANCE:
        bad.append(f"price {p.get('price_amount')} {p.get('price_currency')} differs from {row['amount_usd']} usd")
    if str(p.get("pay_currency") or "").lower() != str(row["pay_currency"] or "").lower():
        bad.append(f"currency {p.get('pay_currency')} differs from {row['pay_currency']}")
    if p.get("pay_address") not in (None, "") and str(p["pay_address"]) != str(row["pay_address"]):
        bad.append("deposit address differs")
    if _f(p.get("actually_paid")) < float(row["pay_amount"] or 0) - UNDERPAY_EPS:
        bad.append(f"paid {_f(p.get('actually_paid'))} of {row['pay_amount']}")
    return bad


def _to_review(con, row, reason: str, source: str, status=None, pid=None) -> str:
    """Parks an order for a person. Only an order that is open or already closed-without-fulfilment can be parked."""
    cur = con.execute("UPDATE billing_orders SET status='review', review_reason=?, updated_at=? WHERE id=? "
                      "AND status IN ('pending','confirming','expired','failed','review')", (reason[:300], _now(), row["id"]))
    if cur.rowcount == 1 and row["status"] != "review":
        _event(con, row["id"], source, "review", status, reason, pid)
    con.commit()
    return "review"


def _fulfil(oid: int, source: str, p: dict) -> str:
    """The one place an automatic order is paid out. Returns "fulfilled", "duplicate" or "lost_race"."""
    now = _now()
    with closing(credits.db()) as con:
        row = con.execute("SELECT * FROM billing_orders WHERE id=? AND method=?", (oid, AUTO)).fetchone()
        if not row or row["product"] not in credits.CATALOG:
            return "lost_race"
        paid = _f(p.get("actually_paid"))
        claim = ("UPDATE billing_orders SET status='approved', provider_status='finished', actually_paid=?, fulfilled_at=?, "
                 "decided_at=?, updated_at=?, review_reason=NULL WHERE id=? AND status IN ('pending','confirming')")
        # Step 1: take the order. Only one caller can move it out of an open state; a concurrent one updates zero rows
        # (on PostgreSQL it waits for the first to commit, then re-checks the condition and also updates zero rows).
        if con.execute(claim, (paid, now, now, now, oid)).rowcount != 1:
            con.raw.rollback()
            return "lost_race"
        # Step 2: credit through the existing ledger / subscription code, in the same transaction.
        if not credits.grant_product(con, row["user_id"], row["product"], oid, row["ref"]):
            con.execute(claim, (paid, now, now, now, oid))        # the credit already exists (grant rolled back): keep it, record the order
            _event(con, oid, source, "duplicate_guard", "finished", "ledger already had this order's credit", row["provider_payment_id"])
            con.commit()
            return "duplicate"
        _event(con, oid, source, "fulfilled", "finished", f"paid {paid} of {row['pay_amount']} {row['pay_currency']}",
               row["provider_payment_id"])
        if paid > float(row["pay_amount"] or 0) * OVERPAY_NOTE_RATIO + UNDERPAY_EPS:
            _event(con, oid, source, "overpaid", "finished", f"paid {paid}, expected {row['pay_amount']}: the extra is not credited",
                   row["provider_payment_id"])
        con.commit()
        return "fulfilled"


def apply_payment(oid: int, p: dict, source: str) -> dict:
    """Moves an order according to a payment as the provider reports it NOW. Safe to call any number of times, in any order,
    from any number of threads: a status never moves backwards and only _fulfil can pay out."""
    ps, now = str(p.get("payment_status") or "").lower(), _now()
    with closing(credits.db()) as con:
        row = con.execute("SELECT * FROM billing_orders WHERE id=? AND method=?", (oid, AUTO)).fetchone()
        if not row or not row["provider_payment_id"]:
            return {"result": "no_such_order"}
        status, pid = row["status"], row["provider_payment_id"]
        if str(p.get("payment_id") or "") != str(pid):
            _event(con, oid, source, "mismatch", ps, "provider reply was for a different payment", pid, f"mm:{oid}:{ps}:{now // 3600}")
            con.commit()
            return {"result": "ignored_mismatch", "status": status}
        if status != "approved":                         # what the provider reports; never rewrites a settled order
            con.execute("UPDATE billing_orders SET provider_status=?, actually_paid=?, last_polled_at=?, updated_at=? "
                        "WHERE id=? AND status<>'approved'", (ps or None, _f(p.get("actually_paid")), now, now, oid))
            con.commit()
        if status in FINAL:
            if status == "approved" and ps in ("refunded", "failed", "partially_paid"):
                _event(con, oid, source, "anomaly", ps, f"provider reports {ps} for an order already fulfilled", pid, f"an:{oid}:{ps}")
                con.commit()
            return {"result": "already_final", "status": status}
        if status == "review":
            return {"result": "in_review", "status": status}
        if status in ("expired", "failed"):              # closed here or by the provider; money after that is for a person
            if ps in ("finished", "partially_paid", "confirming", "confirmed", "sending"):
                _to_review(con, row, f"paid_after_close: provider says {ps} after the order was {status}", source, ps, pid)
                return {"result": "review", "status": "review"}
            return {"result": "closed", "status": status}
        # ---- the order is open (pending / confirming)
        if ps == "finished":
            bad = _validate(row, p)
            if bad:
                _to_review(con, row, "mismatch: " + "; ".join(bad), source, ps, pid)
                return {"result": "review", "status": "review"}
    if ps == "finished":
        r = _fulfil(oid, source, p)
        return {"result": r, "status": "approved" if r in ("fulfilled", "duplicate") else None}
    with closing(credits.db()) as con:
        if ps == "partially_paid":
            row = con.execute("SELECT * FROM billing_orders WHERE id=?", (oid,)).fetchone()
            _to_review(con, row, f"underpaid: received {_f(p.get('actually_paid'))} of {row['pay_amount']}", source, ps, pid)
            return {"result": "review", "status": "review"}
        new = {"confirming": "confirming", "confirmed": "confirming", "sending": "confirming",
               "failed": "failed", "expired": "expired", "refunded": "refunded"}.get(ps)
        if ps == "waiting":
            return {"result": "waiting", "status": status}
        if not new:
            _event(con, oid, source, "unknown_status", ps, "status not recognised: left unchanged", pid, f"us:{oid}:{ps}")
            con.commit()
            return {"result": "unknown_status", "status": status}
        # monotonic: pending -> confirming is allowed, confirming -> pending is not (it is never produced above)
        cur = con.execute("UPDATE billing_orders SET status=?, updated_at=? WHERE id=? AND status IN ('pending','confirming') AND status<>?",
                          (new, now, oid, new))
        if cur.rowcount == 1:
            _event(con, oid, source, "status", ps, f"order is now {new}", pid)
        con.commit()
        return {"result": "updated", "status": new if cur.rowcount == 1 else status}


# ---------------------------------------------------------------- asking the provider, then applying
_tasks: set = set()


def _load(oid: int):
    with closing(credits.db()) as con:
        return con.execute("SELECT * FROM billing_orders WHERE id=? AND method=?", (oid, AUTO)).fetchone()


def _poll_failed(oid: int, e: ProviderError, source: str):
    with closing(credits.db()) as con:
        con.execute("UPDATE billing_orders SET last_polled_at=? WHERE id=?", (_now(), oid))
        _event(con, oid, source, "poll_error", None, f"{e} {e.detail}".strip(), None, f"pe:{oid}:{_now() // 300}")
        con.commit()


async def process_order(oid: int, source: str, min_interval: int = 0):
    """Fetches the payment from NOWPayments and applies it. Returns apply_payment's dict, or {"result": "provider_unavailable"}."""
    row = await asyncio.to_thread(_load, oid)
    if not row or not row["provider_payment_id"]:
        return {"result": "no_such_order"}
    last = credits._col(row, "last_polled_at") or 0
    if min_interval and _now() - last < min_interval:
        return {"result": "throttled", "status": row["status"]}
    try:
        p = await _request("GET", f"/payment/{row['provider_payment_id']}")
        if not isinstance(p, dict):
            raise ProviderError("unexpected reply")
    except ProviderError as e:
        await asyncio.to_thread(_poll_failed, oid, e, source)
        return {"result": "provider_unavailable"}
    return await asyncio.to_thread(apply_payment, oid, p, source)


def _expire_if_due(oid: int) -> str:
    now = _now()
    with closing(credits.db()) as con:
        row = con.execute("SELECT * FROM billing_orders WHERE id=? AND method=?", (oid, AUTO)).fetchone()
        if not row or not row["expires_at"] or now <= row["expires_at"] + EXPIRY_GRACE:
            return "open"
        if row["status"] == "pending":                    # nothing was ever seen on-chain: close it
            cur = con.execute("UPDATE billing_orders SET status='expired', updated_at=? WHERE id=? AND status='pending'", (now, oid))
            if cur.rowcount == 1:
                _event(con, oid, "sweep", "expired", row["provider_status"], "no payment before the order expired")
            con.commit()
            return "expired"
        if row["status"] == "confirming" and now > row["expires_at"] + STUCK_CONFIRMING:
            return _to_review(con, row, "stuck: payment seen but not finished a day after expiry", "sweep", row["provider_status"])
        return "open"


async def sweep_once(limit: int = 40) -> dict:
    """Re-checks open orders with the provider, then closes the ones that expired unpaid. Called by the loop and by tests."""
    def _open():
        with closing(credits.db()) as con:
            return [r["id"] for r in con.execute(
                "SELECT id FROM billing_orders WHERE method=? AND status IN ('pending','confirming') AND provider_payment_id IS NOT NULL "
                "AND created_at>? ORDER BY COALESCE(last_polled_at,0), id LIMIT ?", (AUTO, _now() - LOOKBACK, limit)).fetchall()]
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
    await asyncio.sleep(20)
    while True:
        try:
            await sweep_once()
        except Exception as e:
            print(f"[nowpay] sweep failed: {type(e).__name__}", flush=True)
        await asyncio.sleep(60)


# ---------------------------------------------------------------- member API
async def create_order(request):
    user = auth.require_user(request)
    if not enabled(user):
        raise HTTPException(409, "Automatic crypto payments are not available yet")
    d = await auth._json(request)
    product = d.get("product")
    if d.get("accepted") is not True:
        raise HTTPException(422, "Please accept the Terms, Refunds and Support policy to continue")
    if product not in credits.CATALOG:
        raise HTTPException(422, "Unknown product")
    currency = str(d.get("currency") or pay_currency()).lower()
    if currency not in pay_currencies():
        raise HTTPException(422, "Choose one of the offered networks")
    if _limited(f"create:{user['id']}", 6, 600):
        raise HTTPException(429, "Too many attempts. Please wait a few minutes.")
    usd, now, label = credits._price_fields(user, product)["usd"], _now(), credits.CATALOG[product]["label"]

    def _prepare():
        with closing(credits.db()) as con:
            old = con.execute("SELECT * FROM billing_orders WHERE user_id=? AND method=? AND product=? AND status IN ('pending','confirming') "
                              "AND provider_payment_id IS NOT NULL AND amount_usd=? AND pay_currency=? AND expires_at>? ORDER BY id DESC LIMIT 1",
                              (user["id"], AUTO, product, usd, currency, now)).fetchone()
            if old:                                        # a double click or a second tab: show the same order, create nothing
                return old, None
            open_n = con.execute("SELECT COUNT(*) AS c FROM billing_orders WHERE user_id=? AND status IN ('pending','submitted','confirming')",
                                 (user["id"],)).fetchone()["c"]
            if open_n >= credits.OPEN_ORDERS_MAX:
                raise HTTPException(429, "You already have open orders. Finish one, or wait for it to expire, before starting another.")
            for _ in range(5):
                ref = _new_ref()
                try:
                    oid = con.insert("INSERT INTO billing_orders(user_id,product,method,amount_usd,amount_local,currency,ref,status,created_at,"
                                     "terms_at,provider,pay_currency,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                                     (user["id"], product, AUTO, usd, usd, "USDT", ref, "pending", now, now, "nowpayments",
                                      currency, now))
                    _event(con, oid, "member", "created", None, f"{product} {usd} usd")
                    con.commit()
                    return None, (oid, ref)
                except Exception as e:
                    if not dbx.is_duplicate(e):
                        raise
                    con.raw.rollback()
            raise HTTPException(503, "Could not create the order. Please try again.")

    old, new = await asyncio.to_thread(_prepare)
    if old:
        return JSONResponse({"order": order_view(old)})
    oid, ref = new
    try:
        pay = await create_provider_payment(ref, usd, label, currency)
    except ProviderError as e:
        print(f"[nowpay] could not create a payment: {e} {e.detail}", flush=True)

        def _fail():
            with closing(credits.db()) as con:
                con.execute("UPDATE billing_orders SET status='failed', review_reason=?, updated_at=? WHERE id=? AND provider_payment_id IS NULL",
                            ("create_failed", _now(), oid))
                _event(con, oid, "member", "create_failed", None, f"{e} {e.detail}".strip())
                con.commit()
        await asyncio.to_thread(_fail)
        raise HTTPException(502, "The payment service is not available right now. Please try again in a few minutes.")

    def _store():
        with closing(credits.db()) as con:
            try:
                con.execute("UPDATE billing_orders SET provider_payment_id=?, provider_status=?, pay_address=?, pay_amount=?, pay_currency=?, "
                            "pay_network=?, expires_at=?, updated_at=? WHERE id=? AND provider_payment_id IS NULL",
                            (pay["payment_id"], pay["status"], pay["pay_address"], pay["pay_amount"], pay["pay_currency"],
                             pay["network"], pay["expires_at"], _now(), oid))
                _event(con, oid, "member", "provider_payment", pay["status"], f"payment created, expires {pay['expires_at']}", pay["payment_id"])
                con.commit()
            except Exception as e:
                if dbx.is_duplicate(e):
                    con.raw.rollback()
                    return None
                raise
            return con.execute("SELECT * FROM billing_orders WHERE id=?", (oid,)).fetchone()
    row = await asyncio.to_thread(_store)
    if not row:
        raise HTTPException(502, "The payment service returned an unexpected reply. Please try again.")
    return JSONResponse({"order": order_view(row)})


async def get_order(request):
    """The member's own order, optionally re-checked with the provider (at most once every 10 seconds per order)."""
    user, oid = auth.require_user(request), request.path_params["oid"]
    row = await asyncio.to_thread(_load, oid)
    if not row or row["user_id"] != user["id"]:
        raise HTTPException(404, "Order not found")
    if request.query_params.get("refresh") == "1" and row["status"] in OPEN and not problems() \
            and not _limited(f"refresh:{oid}", 30, 600):
        await process_order(oid, "member_refresh", min_interval=10)
        row = await asyncio.to_thread(_load, oid)
    bal = await asyncio.to_thread(credits.balance, user["id"]) if row["status"] == "approved" else None
    return JSONResponse({"order": order_view(row, bal)})


# ---------------------------------------------------------------- the IPN (webhook)
def _record_ipn(data: dict, raw: bytes):
    """Stores the verified notification and finds its order. Returns (order id or None, how it matched)."""
    pid, ref, ps = str(data.get("payment_id") or ""), str(data.get("order_id") or ""), str(data.get("payment_status") or "").lower()
    key = "ipn:" + hashlib.sha256(raw).hexdigest()
    with closing(credits.db()) as con:
        r = con.execute("SELECT id FROM billing_orders WHERE method=? AND provider_payment_id=?", (AUTO, pid)).fetchone()
        if r:
            _event(con, r["id"], "ipn", "ipn", ps, f"order {ref}", pid, key)
            con.commit()
            return r["id"], "payment"
        parent = str(data.get("parent_payment_id") or "")
        r = con.execute("SELECT * FROM billing_orders WHERE method=? AND provider_payment_id=?", (AUTO, parent)).fetchone() if parent else None
        if r:                                              # a further deposit to the same address: for a person to look at
            _event(con, r["id"], "ipn", "ipn", ps, f"additional deposit {pid} for order {ref}", pid, key)
            if r["status"] in ("pending", "confirming", "expired", "failed", "review"):
                _to_review(con, r, f"extra_deposit: payment {pid} ({ps}) arrived for this order", "ipn", ps, pid)
            else:
                _event(con, r["id"], "ipn", "anomaly", ps, f"additional deposit {pid} for a settled order", pid)
                con.commit()
            return None, "child"
        _event(con, None, "ipn", "unmatched", ps, f"no order for payment {pid} (order_id {ref})", pid, key)
        con.commit()
        return None, "unmatched"


async def ipn(request):
    ip = request.client.host if request.client else "?"
    if _limited(f"ipn:{ip}", 300, 60):
        raise HTTPException(429, "Too many requests")
    secret = _env("NOWPAYMENTS_IPN_SECRET")
    if not secret:                                        # fail closed: without the secret nothing can be verified
        raise HTTPException(503, "Not configured")
    raw = await request.body()
    if len(raw) > MAX_BODY:
        raise HTTPException(413, "Too large")
    if not verify_signature(raw, request.headers.get("x-nowpayments-sig", ""), secret):
        print(f"[nowpay] IPN rejected: bad signature (from {ip})", flush=True)
        raise HTTPException(401, "Invalid signature")
    try:
        data = json.loads(raw)
    except ValueError:
        raise HTTPException(400, "Invalid JSON")
    if not isinstance(data, dict) or not data.get("payment_id"):
        raise HTTPException(400, "Not a payment notification")
    oid, how = await asyncio.to_thread(_record_ipn, data, raw)
    if oid:
        task = asyncio.create_task(process_order(oid, "ipn"))
        _tasks.add(task)
        task.add_done_callback(_tasks.discard)
        await asyncio.wait({task}, timeout=2.0)          # NOWPayments allows 3 s: whatever is not done goes on in the background
    return JSONResponse({"ok": True})


async def drain():
    """Waits for background work started by IPNs (used by tests and on shutdown)."""
    while _tasks:
        await asyncio.gather(*list(_tasks), return_exceptions=True)


# ---------------------------------------------------------------- admin API
def _admin_rows(status: str, limit: int):
    with closing(credits.db()) as con:
        if status == "open":
            q, a = "status IN ('pending','confirming')", ()
        elif status == "all":
            q, a = "1=1", ()
        else:
            q, a = "status=?", (status,)
        rows = con.execute(f"SELECT * FROM billing_orders WHERE method=? AND {q} ORDER BY id DESC LIMIT ?", (AUTO, *a, limit)).fetchall()
        counts = {r["status"]: r["c"] for r in con.execute(
            "SELECT status, COUNT(*) AS c FROM billing_orders WHERE method=? GROUP BY status", (AUTO,)).fetchall()}
        exc = [dict(r) for r in con.execute(
            "SELECT id,order_id,kind,provider_status,detail,source,created_at FROM payment_events "
            "WHERE kind IN ('review','unmatched','anomaly','overpaid','mismatch','duplicate_guard','create_failed') ORDER BY id DESC LIMIT 30").fetchall()]
    return rows, counts, exc


async def admin_view(request):
    auth.require_admin(request)
    status = request.query_params.get("status", "open")
    if status not in ("open", "all", "review", "approved", "failed", "expired", "refunded", "rejected"):
        status = "open"
    rows, counts, exc = await asyncio.to_thread(_admin_rows, status, 60)
    names = credits._emails([r["user_id"] for r in rows])
    orders = [{**credits._order_public(r), "email": names.get(r["user_id"], "?"), "pay_amount": r["pay_amount"],
               "actually_paid": r["actually_paid"], "network": network_label(r["pay_currency"] or None),
               "review_reason": r["review_reason"], "provider_status": r["provider_status"], "provider_payment_id": r["provider_payment_id"],
               "last_polled_at": r["last_polled_at"], "pay_address": r["pay_address"]} for r in rows]
    return JSONResponse({"mode": mode(), "problems": problems(), "billing_open": credits.billing_on(), "ipn_url": ipn_url() if callback_base() else "",
                         "api_base": api_base(), "pay_currency": ", ".join(pay_currencies()), "status": status, "counts": counts,
                         "orders": orders, "exceptions": exc})


async def admin_toggle(request):
    u = auth.require_admin(request)
    d = await auth._json(request)
    m = d.get("mode")
    if m not in MODES:
        raise HTTPException(422, "mode must be off, admin or public")
    if m != "off" and problems():
        raise HTTPException(409, "Not ready: " + "; ".join(problems()))
    if m == "public" and not credits.billing_on():
        raise HTTPException(409, "Billing is not open to members yet. Open billing first, or stay in admin pilot mode.")

    def _save():
        with closing(credits.db()) as con:
            credits._put(con, "nowpayments_enabled", MODES[m])
            _event(con, None, "admin", "toggle", None, f"automatic crypto payments: {m}", admin_id=u["id"])
            con.commit()
    await asyncio.to_thread(_save)
    _switch["ts"] = 0.0
    print(f"[nowpay] automatic payments set to {m} by admin {u['id']}", flush=True)
    return JSONResponse({"ok": True})


async def admin_check(request):
    """Asks NOWPayments whether it is reachable with our key and offers the currency we sell in. Changes nothing."""
    auth.require_admin(request)
    out = {"configured": not problems(), "problems": problems(), "api_reachable": None, "currency_listed": None, "currencies": {}, "detail": ""}
    if out["problems"]:
        return JSONResponse(out)
    try:
        st = await _request("GET", "/status")
        out["api_reachable"] = bool(isinstance(st, dict) and str(st.get("message", "")).upper() == "OK")
        out["detail"] = str(st.get("message", ""))[:100] if isinstance(st, dict) else ""
    except ProviderError as e:
        out["api_reachable"], out["detail"] = False, f"{e} {e.detail}".strip()
        return JSONResponse(out)
    try:
        cur = await _request("GET", "/currencies")
        lst = cur.get("currencies") if isinstance(cur, dict) else cur
        if isinstance(lst, list):
            have = {str(x).lower() for x in lst}
            out["currencies"] = {c: c in have for c in pay_currencies()}
            out["currency_listed"] = all(out["currencies"].values())
    except ProviderError as e:
        out["detail"] = (out["detail"] + f" currencies: {e}").strip()
    return JSONResponse(out)


async def admin_reconcile(request):
    """Re-reads one order from the provider and applies it. The safe answer to a lost or late notification."""
    u, oid = auth.require_admin(request), request.path_params["oid"]
    if problems():
        raise HTTPException(409, "Not configured: " + "; ".join(problems()))
    row = await asyncio.to_thread(_load, oid)
    if not row:
        raise HTTPException(404, "Order not found")
    if not row["provider_payment_id"]:
        raise HTTPException(409, "This order never got a provider payment")
    r = await process_order(oid, f"admin:{u['id']}")
    if r.get("result") == "provider_unavailable":
        raise HTTPException(502, "NOWPayments could not be reached. Nothing was changed.")
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
            row = con.execute("SELECT * FROM billing_orders WHERE id=? AND method=?", (oid, AUTO)).fetchone()
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
    Route("/api/pay/crypto/order", create_order, methods=["POST"]),
    Route("/api/pay/crypto/order/{oid:int}", get_order, methods=["GET"]),
    Route("/api/pay/nowpayments/ipn", ipn, methods=["POST"]),
    Route("/api/admin/pay/crypto", admin_view, methods=["GET"]),
    Route("/api/admin/pay/crypto/toggle", admin_toggle, methods=["POST"]),
    Route("/api/admin/pay/crypto/check", admin_check, methods=["POST"]),
    Route("/api/admin/pay/crypto/order/{oid:int}/reconcile", admin_reconcile, methods=["POST"]),
    Route("/api/admin/pay/crypto/order/{oid:int}/resolve", admin_resolve, methods=["POST"]),
    Route("/api/admin/pay/crypto/order/{oid:int}/events", admin_events, methods=["GET"]),
]
