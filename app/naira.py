"""Pay with Naira: a bank transfer to the TradeLens Moniepoint business account, verified by an administrator.

How it works, and what it deliberately does not do
  * A member picks a product in Billing and chooses "Pay with Naira". The server prices it from credits.CATALOG (never from the browser),
    converts it with the existing ngn_rate setting (round(usd * ngn_rate), as the old manual Naira flow did), saves a pending order in the
    EXISTING billing_orders table (method "moniepoint") and generates a unique payment narration on the server (TL-XXXXXXXX).
    The member is shown the account name, bank, account number, the exact amount and the narration, each with its own copy button.
    The account details come from the billing settings the owner already edits in Admin (bank_name, bank_account_name,
    bank_account_number): nothing banking-related is in the code or the environment.
  * Nothing is automatic. A bank alert, a receipt, a narration or a customer's word never fulfils an order. An administrator checks the
    credit in the Moniepoint account, finds the order (search by reference, narration, email, amount, bank reference or date), records the
    bank transaction reference and the amount actually received, and presses Approve. Only an exact amount is approved; a different
    amount parks the order in "review" and grants nothing until an administrator explicitly decides (accept the difference with a
    note, or reject).
  * Approval reuses credits.grant_product, the code the manual USDT approval and NOWPayments use: signals go in through the existing
    ledger, a plan's notification / MT5 days through billing_subs. Three independent guards make a double credit impossible: the
    conditional status UPDATE (only one caller can move the order to "approved"), the ledger's unique (user, "purchase", "order:<id>")
    row, and one database transaction around all of it. A unique index also stops one bank transaction settling two orders.
  * Every step is written to payment_events with the administrator's id (the existing audit table). The member only ever sees generic
    text: internal reasons and bank references stay on the admin side.
  * No bank API or webhook is used. There is no automated reconciliation.

Switched off until: ngn_rate and the bank details are filled in under Admin > Billing, and the owner switches "Pay with Naira" on in
Admin > Naira payments. Manual USDT and NOWPayments keep working either way."""
import asyncio
import re
import secrets
import time
from contextlib import closing
from datetime import datetime, timezone

from starlette.exceptions import HTTPException
from starlette.responses import JSONResponse
from starlette.routing import Route

from . import auth, credits, dbx

METHOD = credits.NAIRA_METHOD
PROVIDER = "moniepoint"
APPROVABLE = ("pending", "review", "expired", "cancelled")       # an administrator may approve these once the funds are confirmed
AMOUNT_EPS = 0.005                                               # Naira: the amount received must equal the amount asked for
MAX_AMOUNT = 100_000_000
_BANK_REF_RE = re.compile(r"^[A-Za-z0-9._/#:=-]{4,100}$")
_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"                  # no 0/O/1/I: easy to read out and to type into a banking app

# What a member is told. The internal reasons never reach the browser.
MESSAGES = {
    "pending": "Waiting for your transfer. Send the exact amount with the narration below. Your signals are added after our team "
               "confirms the money in our account.",
    "review": "Your payment is being checked by our team. If you need to ask about it, quote the order reference to support.",
    "approved": "Payment confirmed. Your purchase has been added.",
    "rejected": "We could not confirm this payment. If you did send money, contact support with the order reference.",
    "expired": "This order has closed. If you already sent the money, contact support with the order reference.",
    "cancelled": "This order was cancelled.",
}


# ---------------------------------------------------------------- settings (the billing settings the owner already edits in Admin)
_cache = {"ts": 0.0, "s": {}}
MODES = {"off": "0", "admin": "admin", "public": "1"}            # what Admin stores for each mode


def _now() -> int:
    return int(time.time())


def settings() -> dict:
    """The few billing settings this module needs, cached for 10 seconds. Trouble reading them means "not configured"."""
    if time.time() - _cache["ts"] < 10:
        return _cache["s"]
    s = {}
    try:
        with closing(credits.db()) as con:
            for r in con.execute("SELECT key, value FROM billing_settings WHERE key IN "
                                 "('naira_enabled','ngn_rate','bank_name','bank_account_name','bank_account_number','support')").fetchall():
                s[r["key"]] = r["value"]
    except Exception as e:
        print(f"[naira] could not read settings: {type(e).__name__}", flush=True)
        s = {}
    _cache.update(ts=time.time(), s=s)
    return s


def reset_cache():
    _cache["ts"] = 0.0


def mode() -> str:
    """"off", "admin" (pilot: only admin accounts see and can use it) or "public". Only decides who may START an order: an order that
    already exists is always shown and can always be approved by an administrator, whatever the mode."""
    return {"1": "public", "admin": "admin"}.get(settings().get("naira_enabled", ""), "off")


def rate():
    return credits._num(settings().get("ngn_rate"))


def bank() -> dict:
    s = settings()
    return {"bank_name": (s.get("bank_name") or "").strip(), "account_name": (s.get("bank_account_name") or "").strip(),
            "account_number": (s.get("bank_account_number") or "").strip()}


def problems() -> list:
    """What is still missing before Pay with Naira can run. Names only."""
    out = []
    if not rate():
        out.append("the Naira rate (ngn_rate) is not set in Billing settings")
    b = bank()
    for k, label in (("bank_name", "bank name"), ("account_name", "bank account name"), ("account_number", "bank account number")):
        if not b[k]:
            out.append(f"the {label} is not set in Billing settings")
    return out


def enabled(user=None) -> bool:
    """May this member start a Naira order now? Public mode also needs billing to be open to members, as the other methods do. The admin
    pilot does not, so the owner can test while billing is still closed to everyone else."""
    if problems() or not user:
        return False
    m = mode()
    if m == "public":
        return credits.billing_on()
    return m == "admin" and user.get("tier") == "admin"


def naira_for(usd: float, r: float) -> int:
    """Whole Naira for a USD price: the same rule as the old manual Naira flow, round(usd * ngn_rate)."""
    return int(round(float(usd) * float(r)))


def public_info(user=None) -> dict:
    if not enabled(user):
        return {"enabled": False}
    r = rate()
    return {"enabled": True, "label": "Pay with Naira", "pilot": mode() == "admin",
            "naira": {k: naira_for(credits._price_fields(user, k)["usd"], r) for k in credits.CATALOG}}


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
    """One audit row in payment_events. `detail` is plain text. With a dedupe key a repeat is skipped (False); that rolls back the open
    transaction, so only use a key on a write that stands alone."""
    try:
        con.execute("INSERT INTO payment_events(order_id,provider_payment_id,source,kind,provider_status,detail,dedupe_key,admin_id,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?)", (order_id, pid, source, kind, status, str(detail)[:500], dedupe, admin_id, _now()))
    except Exception as e:
        if dedupe and dbx.is_duplicate(e):
            con.raw.rollback()
            return False
        raise
    return True


def _rand(n: int) -> str:
    return "".join(secrets.choice(_ALPHABET) for _ in range(n))


def _clean(v, limit: int) -> str:
    """Plain single-line text from a form field: control characters removed, length capped."""
    return re.sub(r"[\x00-\x20\x7f]+", " ", str(v or "")).strip()[:limit]


def _money(v):
    """A positive amount in Naira (a number or a numeric string) rounded to kobo, or None."""
    if isinstance(v, bool):
        return None
    try:
        x = float(str(v).replace(",", "").strip())
    except ValueError:
        return None
    return round(x, 2) if x == x and 0 < x <= MAX_AMOUNT else None


def _bank_ref(v):
    r = str(v or "").strip()
    return r.upper() if _BANK_REF_RE.match(r) else None


def order_view(row, extra_balance=None) -> dict:
    """What the member's browser may see about a Naira order: instructions while it is open, generic text always. Never the bank
    reference, the internal reason, the admin who decided, or anybody else's data."""
    out = credits._order_public(row)
    out.update(note=None, txid=None)                               # `note` / `txid` are internal for these orders
    p = credits.CATALOG.get(row["product"], {})
    st = row["status"]
    out.update(narration=credits._col(row, "narration"), amount_naira=row["pay_amount"], signals=p.get("signals"), days=p.get("days") or 0,
               notify=bool(p.get("notify")), mt5=bool(p.get("mt5")), message=MESSAGES.get(st, ""),
               expires_at=credits._col(row, "expires_at"))
    if st in ("pending", "review"):
        out["account"] = bank()
        out["support"] = (settings().get("support") or "").strip()
    if extra_balance is not None:
        out["balance"] = extra_balance
    return out


# ---------------------------------------------------------------- member API
async def create_order(request):
    user = auth.require_user(request)
    if not enabled(user):
        raise HTTPException(409, "Pay with Naira is not available yet")
    d = await auth._json(request)
    product = d.get("product")
    if d.get("accepted") is not True:
        raise HTTPException(422, "Please accept the Terms, Refunds and Support policy to continue")
    if product not in credits.CATALOG:
        raise HTTPException(422, "Unknown product")
    if _limited(f"create:{user['id']}", 6, 600):
        raise HTTPException(429, "Too many attempts. Please wait a few minutes.")
    usd = credits._price_fields(user, product)["usd"]
    naira, now = naira_for(usd, rate()), _now()
    if naira < 1:
        raise HTTPException(409, "Pay with Naira is not available for this product right now")

    def _prepare():
        with closing(credits.db()) as con:
            con.execute("UPDATE billing_orders SET status='expired', updated_at=? WHERE user_id=? AND method=? AND status='pending' "
                        "AND expires_at IS NOT NULL AND expires_at<?", (now, user["id"], METHOD, now))
            old = con.execute("SELECT * FROM billing_orders WHERE user_id=? AND method=? AND product=? AND status='pending' AND amount_local=? "
                              "AND expires_at>? ORDER BY id DESC LIMIT 1", (user["id"], METHOD, product, naira, now)).fetchone()
            if old:                                        # a double click or a second tab: show the same order, create nothing
                con.commit()
                return old
            open_n = con.execute("SELECT COUNT(*) AS c FROM billing_orders WHERE user_id=? AND status IN ('pending','submitted')",
                                 (user["id"],)).fetchone()["c"]
            if open_n >= credits.OPEN_ORDERS_MAX:
                con.commit()
                raise HTTPException(429, "You already have open orders. Finish or cancel one before starting another.")
            for _ in range(6):
                ref, narration = "TLM-" + _rand(8), "TL-" + _rand(8)
                try:
                    oid = con.insert("INSERT INTO billing_orders(user_id,product,method,amount_usd,amount_local,currency,ref,status,created_at,"
                                     "terms_at,provider,pay_currency,pay_amount,narration,expires_at,updated_at) "
                                     "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                                     (user["id"], product, METHOD, usd, naira, "NGN", ref, "pending", now, now, PROVIDER, "NGN", float(naira),
                                      narration, now + credits.ORDER_STALE_DAYS * 86400, now))
                    _event(con, oid, "member", "created", "pending", f"{product} {usd} usd = {naira} NGN, narration {narration}")
                    con.commit()
                    return con.execute("SELECT * FROM billing_orders WHERE id=?", (oid,)).fetchone()
                except Exception as e:
                    if not dbx.is_duplicate(e):
                        raise
                    con.raw.rollback()                     # ref or narration collided (astronomically rare): draw new ones
            raise HTTPException(503, "Could not create the order. Please try again.")
    row = await asyncio.to_thread(_prepare)
    return JSONResponse({"order": order_view(row)})


def _own(oid: int, user_id: int):
    with closing(credits.db()) as con:
        row = con.execute("SELECT * FROM billing_orders WHERE id=? AND method=?", (oid, METHOD)).fetchone()
    if not row or row["user_id"] != user_id:
        raise HTTPException(404, "Order not found")
    return row


async def get_order(request):
    user, oid = auth.require_user(request), request.path_params["oid"]
    row = await asyncio.to_thread(_own, oid, user["id"])
    bal = await asyncio.to_thread(credits.balance, user["id"]) if row["status"] == "approved" else None
    return JSONResponse({"order": order_view(row, bal)})


async def claim(request):
    """"I have sent it": an optional note (the sender's account name or the bank's reference) that helps an administrator find the
    transfer. Information only: it never changes the status and never grants anything."""
    user, oid = auth.require_user(request), request.path_params["oid"]
    d = await auth._json(request)
    note = _clean(d.get("note"), 200)
    if not note:
        raise HTTPException(422, "Write the name on the account you paid from, or the bank's reference for the transfer")
    row = await asyncio.to_thread(_own, oid, user["id"])
    if row["status"] not in ("pending", "review"):
        raise HTTPException(409, "This order is closed")
    if _limited(f"claim:{oid}", 5, 3600):
        raise HTTPException(429, "Too many notes for this order. Please wait, or contact support.")

    def _save():
        with closing(credits.db()) as con:
            _event(con, oid, "member", "customer_note", row["status"], note)
            con.commit()
    await asyncio.to_thread(_save)
    return JSONResponse({"ok": True})


async def cancel(request):
    """The member abandons an order they have not paid. Only a pending order can be cancelled, and never one an administrator is checking."""
    user, oid = auth.require_user(request), request.path_params["oid"]
    await asyncio.to_thread(_own, oid, user["id"])

    def _do():
        with closing(credits.db()) as con:
            cur = con.execute("UPDATE billing_orders SET status='cancelled', updated_at=? WHERE id=? AND user_id=? AND method=? AND status='pending'",
                              (_now(), oid, user["id"], METHOD))
            if cur.rowcount != 1:
                raise HTTPException(409, "Only an order that is still waiting for payment can be cancelled")
            _event(con, oid, "member", "cancelled", "cancelled", "cancelled by the member")
            con.commit()
    await asyncio.to_thread(_do)
    return JSONResponse({"ok": True})


# ---------------------------------------------------------------- admin: approval
def _approve(oid: int, admin_id: int, bank_ref: str, amount: float, note: str, accept: bool) -> dict:
    """The one place a Naira order is paid out. An exact amount is approved and fulfilled; any other amount is parked in review
    (nothing granted) unless the administrator explicitly accepts the difference with a note."""
    now = _now()
    with closing(credits.db()) as con:
        row = con.execute("SELECT * FROM billing_orders WHERE id=? AND method=?", (oid, METHOD)).fetchone()
        if not row:
            raise HTTPException(404, "Order not found")
        if row["product"] not in credits.CATALOG:
            raise HTTPException(409, "This order's product no longer exists")
        if row["status"] == "approved":                    # a second click, a second admin, a retry: a no-op, never a second credit
            return {"result": "already_approved", "status": "approved"}
        if row["status"] not in APPROVABLE:
            raise HTTPException(409, f"This order is {row['status']} and cannot be approved")
        expected = float(row["pay_amount"] or 0)
        exact = abs(amount - expected) <= AMOUNT_EPS
        if not exact and not accept:                       # underpaid / overpaid: record it and wait for a person's decision
            kind = "underpaid" if amount < expected else "overpaid"
            reason = f"{kind}: received {amount:.2f} of {expected:.2f} NGN"
            try:
                cur = con.execute("UPDATE billing_orders SET status='review', review_reason=?, txid=?, actually_paid=?, updated_at=? "
                                  "WHERE id=? AND status IN ('pending','review','expired','cancelled')", (reason, bank_ref, amount, now, oid))
            except Exception as e:
                if not dbx.is_duplicate(e):
                    raise
                con.raw.rollback()
                raise HTTPException(409, "That bank transaction reference is already attached to another order")
            if cur.rowcount != 1:
                con.raw.rollback()
                raise HTTPException(409, "This order changed while you were working. Reload and check it.")
            _event(con, oid, "admin", kind, "review", f"{reason}; bank ref {bank_ref}" + (f"; {note}" if note else ""), bank_ref, admin_id=admin_id)
            con.commit()
            return {"result": "review", "status": "review", "reason": reason}
        if (not exact or row["status"] != "pending") and not note:
            raise HTTPException(422, "A note is required to approve " + ("an amount that differs from the order" if not exact
                                                                         else f"an order that is {row['status']}"))
        claim = ("UPDATE billing_orders SET status='approved', txid=?, actually_paid=?, fulfilled_at=?, decided_at=?, decided_by=?, updated_at=?, "
                 "review_reason=NULL WHERE id=? AND status IN ('pending','review','expired','cancelled')")
        args = (bank_ref, amount, now, now, admin_id, now, oid)
        # Step 1: take the order. Only one caller can move it to "approved"; a concurrent one updates zero rows (on PostgreSQL it waits
        # for the first to commit, then re-checks the condition and also updates zero rows).
        try:
            n = con.execute(claim, args).rowcount
        except Exception as e:
            if not dbx.is_duplicate(e):
                raise
            con.raw.rollback()                             # the unique index: one bank transaction can settle one order only
            raise HTTPException(409, "That bank transaction reference is already attached to another order")
        if n != 1:
            con.raw.rollback()
            current = con.execute("SELECT status FROM billing_orders WHERE id=?", (oid,)).fetchone()
            if current and current["status"] == "approved":
                return {"result": "already_approved", "status": "approved"}
            raise HTTPException(409, "This order changed while you were working. Reload and check it.")
        # Step 2: credit through the existing ledger / subscription code, in the same transaction.
        fresh = credits.grant_product(con, row["user_id"], row["product"], oid, row["ref"], admin_id)
        if not fresh:
            con.execute(claim, args)                       # the credit already exists (grant rolled back the claim): keep it, record the order
        p = credits.CATALOG[row["product"]]
        _event(con, oid, "admin", "approved", "approved", f"bank ref {bank_ref}; received {amount:.2f} of {expected:.2f} NGN" +
               ("; difference accepted" if not exact else "") + (f"; {note}" if note else ""), bank_ref, admin_id=admin_id)
        _event(con, oid, "system", "fulfilled" if fresh else "duplicate_guard", "approved",
               (f"{p['signals']} signals" + (f", {p['days']} days of {row['product']}" if p["days"] else "")) if fresh
               else "ledger already had this order's credit", bank_ref)
        con.commit()
        return {"result": "approved" if fresh else "already_credited", "status": "approved"}


async def admin_approve(request):
    u, oid = auth.require_admin(request), request.path_params["oid"]
    d = await auth._json(request)
    ref, amount = _bank_ref(d.get("bank_ref")), _money(d.get("amount_received"))
    if not ref:
        raise HTTPException(422, "Enter the bank transaction reference exactly as it shows in the Moniepoint account (4 to 100 letters, numbers, - _ . / #)")
    if amount is None:
        raise HTTPException(422, "Enter the amount actually received, in Naira")
    r = await asyncio.to_thread(_approve, oid, u["id"], ref, amount, _clean(d.get("note"), 200), d.get("accept_difference") is True)
    return JSONResponse({"ok": True, **r})


async def admin_reject(request):
    u, oid = auth.require_admin(request), request.path_params["oid"]
    d = await auth._json(request)
    reason = _clean(d.get("reason"), 200)
    if not reason:
        raise HTTPException(422, "An internal reason is required so the record explains the decision")

    def _do():
        now = _now()
        with closing(credits.db()) as con:
            row = con.execute("SELECT * FROM billing_orders WHERE id=? AND method=?", (oid, METHOD)).fetchone()
            if not row:
                raise HTTPException(404, "Order not found")
            cur = con.execute("UPDATE billing_orders SET status='rejected', review_reason=?, decided_at=?, decided_by=?, updated_at=? "
                              "WHERE id=? AND status IN ('pending','review','expired','cancelled')", (reason, now, u["id"], now, oid))
            if cur.rowcount != 1:
                raise HTTPException(409, f"This order is {row['status']} and cannot be rejected")
            _event(con, oid, "admin", "rejected", "rejected", reason, row["txid"], admin_id=u["id"])
            con.commit()
    await asyncio.to_thread(_do)
    return JSONResponse({"ok": True})


async def admin_flag(request):
    """Park an order for investigation (for example a transfer that arrived with the wrong narration). Grants nothing."""
    u, oid = auth.require_admin(request), request.path_params["oid"]
    d = await auth._json(request)
    note = _clean(d.get("note"), 200)
    if not note:
        raise HTTPException(422, "A note is required so the record explains why this order is being investigated")

    def _do():
        with closing(credits.db()) as con:
            row = con.execute("SELECT * FROM billing_orders WHERE id=? AND method=?", (oid, METHOD)).fetchone()
            if not row:
                raise HTTPException(404, "Order not found")
            cur = con.execute("UPDATE billing_orders SET status='review', review_reason=?, updated_at=? WHERE id=? AND status IN ('pending','expired','cancelled','review')",
                              ("flagged: " + note, _now(), oid))
            if cur.rowcount != 1:
                raise HTTPException(409, f"This order is {row['status']} and cannot be flagged")
            _event(con, oid, "admin", "flagged", "review", note, None, admin_id=u["id"])
            con.commit()
    await asyncio.to_thread(_do)
    return JSONResponse({"ok": True})


async def admin_unmatched(request):
    """Record a bank credit that matches no order, so it is not forgotten. It is listed until an order is approved with the same bank reference."""
    u = auth.require_admin(request)
    d = await auth._json(request)
    ref = _bank_ref(d.get("bank_ref"))
    note = _clean(d.get("note"), 200)
    amount = _money(d.get("amount")) if d.get("amount") not in (None, "") else None
    if not ref:
        raise HTTPException(422, "Enter the bank transaction reference (4 to 100 letters, numbers, - _ . / #)")
    if d.get("amount") not in (None, "") and amount is None:
        raise HTTPException(422, "The amount must be a positive number of Naira")
    if not note:
        raise HTTPException(422, "A note is required (what you know about this transfer)")
    seen = _clean(d.get("narration_seen"), 60)

    def _do():
        with closing(credits.db()) as con:
            detail = "; ".join(x for x in ((f"amount {amount:.2f} NGN" if amount else ""), (f"narration seen: {seen}" if seen else ""), note) if x)
            if not _event(con, None, "admin", "unmatched_bank", "unmatched", detail, ref, "nb:" + ref, u["id"]):
                raise HTTPException(409, "That bank transaction is already flagged")
            con.commit()
    await asyncio.to_thread(_do)
    return JSONResponse({"ok": True})


# ---------------------------------------------------------------- admin: lists, search, history, switch
def _day(v):
    try:
        return int(datetime.strptime(str(v), "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp())
    except ValueError:
        return None


def _admin_rows(status: str, q: str, d_from, d_to, limit: int, offset: int):
    where, args = ["o.method=?"], [METHOD]
    if status == "open":
        where.append("o.status IN ('pending','review')")
    elif status != "all":
        where.append("o.status=?")
        args.append(status)
    if q:
        like = "%" + q.lower() + "%"
        parts = ["lower(o.ref) LIKE ?", "lower(COALESCE(o.narration,'')) LIKE ?", "lower(u.email) LIKE ?", "lower(COALESCE(o.txid,'')) LIKE ?"]
        qa = [like] * 4
        amt = _money(q)
        if amt is not None:
            parts += ["o.amount_local=?", "o.actually_paid=?"]
            qa += [amt, amt]
        where.append("(" + " OR ".join(parts) + ")")
        args += qa
    if d_from is not None:
        where.append("o.created_at>=?")
        args.append(d_from)
    if d_to is not None:
        where.append("o.created_at<?")
        args.append(d_to + 86400)
    w = " AND ".join(where)
    with closing(credits.db()) as con:
        rows = con.execute(
            "SELECT o.*, u.email AS email, a.email AS decided_by_email, (SELECT e.detail FROM payment_events e WHERE e.order_id=o.id AND "
            "e.kind='customer_note' ORDER BY e.id DESC LIMIT 1) AS claim FROM billing_orders o JOIN users u ON u.id=o.user_id "
            f"LEFT JOIN users a ON a.id=o.decided_by WHERE {w} ORDER BY o.id DESC LIMIT ? OFFSET ?", (*args, limit, offset)).fetchall()
        total = con.execute(f"SELECT COUNT(*) AS c FROM billing_orders o JOIN users u ON u.id=o.user_id WHERE {w}", tuple(args)).fetchone()["c"]
        counts = {r["status"]: r["c"] for r in con.execute(
            "SELECT status, COUNT(*) AS c FROM billing_orders WHERE method=? GROUP BY status", (METHOD,)).fetchall()}
        unmatched = [dict(r) for r in con.execute(
            "SELECT e.id, e.provider_payment_id AS bank_ref, e.detail, e.admin_id, e.created_at FROM payment_events e WHERE e.kind='unmatched_bank' "
            "AND NOT EXISTS (SELECT 1 FROM billing_orders o WHERE o.method=? AND o.txid=e.provider_payment_id) ORDER BY e.id DESC LIMIT 50",
            (METHOD,)).fetchall()]
    return rows, total, counts, unmatched


async def admin_view(request):
    auth.require_admin(request)
    qp = request.query_params
    status = qp.get("status", "open")
    if status not in ("open", "all", "pending", "review", "approved", "rejected", "expired", "cancelled"):
        status = "open"
    q = _clean(qp.get("q"), 80)
    try:
        limit, offset = min(max(int(qp.get("limit", 40)), 1), 100), max(int(qp.get("offset", 0)), 0)
    except ValueError:
        limit, offset = 40, 0
    rows, total, counts, unmatched = await asyncio.to_thread(_admin_rows, status, q, _day(qp.get("from")), _day(qp.get("to")), limit, offset)
    orders = []
    for r in rows:
        p = credits.CATALOG.get(r["product"], {})
        orders.append({"id": r["id"], "ref": r["ref"], "narration": r["narration"], "email": r["email"], "product": r["product"],
                       "label": credits._label(r["product"]), "signals": p.get("signals"), "days": p.get("days") or 0,
                       "amount_usd": r["amount_usd"], "expected": r["pay_amount"], "received": r["actually_paid"], "bank_ref": r["txid"],
                       "status": r["status"], "review_reason": r["review_reason"], "claim": r["claim"], "created_at": r["created_at"],
                       "decided_at": r["decided_at"], "decided_by": r["decided_by_email"], "fulfilled_at": r["fulfilled_at"]})
    return JSONResponse({"mode": mode(), "problems": problems(), "billing_open": credits.billing_on(), "rate": rate(), "bank": bank(),
                         "status": status, "q": q, "total": total, "counts": counts, "orders": orders, "unmatched": unmatched})


async def admin_events(request):
    auth.require_admin(request)
    oid = request.path_params["oid"]

    def _get():
        with closing(credits.db()) as con:
            return [dict(r) for r in con.execute(
                "SELECT e.id,e.source,e.kind,e.provider_status,e.detail,e.admin_id,e.created_at,a.email AS admin_email FROM payment_events e "
                "LEFT JOIN users a ON a.id=e.admin_id WHERE e.order_id=? ORDER BY e.id", (oid,)).fetchall()]
    return JSONResponse({"events": await asyncio.to_thread(_get)})


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
            credits._put(con, "naira_enabled", MODES[m])
            _event(con, None, "admin", "toggle", None, f"Pay with Naira: {m}", admin_id=u["id"])
            con.commit()
    await asyncio.to_thread(_save)
    reset_cache()
    print(f"[naira] Pay with Naira set to {m} by admin {u['id']}", flush=True)
    return JSONResponse({"ok": True})


routes = [
    Route("/api/pay/naira/order", create_order, methods=["POST"]),
    Route("/api/pay/naira/order/{oid:int}", get_order, methods=["GET"]),
    Route("/api/pay/naira/order/{oid:int}/claim", claim, methods=["POST"]),
    Route("/api/pay/naira/order/{oid:int}/cancel", cancel, methods=["POST"]),
    Route("/api/admin/pay/naira", admin_view, methods=["GET"]),
    Route("/api/admin/pay/naira/toggle", admin_toggle, methods=["POST"]),
    Route("/api/admin/pay/naira/unmatched", admin_unmatched, methods=["POST"]),
    Route("/api/admin/pay/naira/order/{oid:int}/approve", admin_approve, methods=["POST"]),
    Route("/api/admin/pay/naira/order/{oid:int}/reject", admin_reject, methods=["POST"]),
    Route("/api/admin/pay/naira/order/{oid:int}/flag", admin_flag, methods=["POST"]),
    Route("/api/admin/pay/naira/order/{oid:int}/events", admin_events, methods=["GET"]),
]
