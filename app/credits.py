"""Signal credits, plans and manually verified payments.

How it works:
  * A member's signal balance is a number that only ever changes through the ledger (credit_ledger), one row per
    change, so every credit and every spend can be audited. credit_balances holds the running total and is updated in
    the same transaction as the ledger row.
  * Opening a setup costs 1 signal. The same signal is never charged twice (the ledger refuses a second "spend" row
    for the same member and signal).
  * Packs add signals that never expire. Plans (Plus, Pro) add signals and switch on notifications (and, for Pro,
    the MT5 connection) for 30 days.
  * Members pay outside the app (USDT or bank transfer), send the transaction reference, and the owner approves the
    order in the Admin panel. Approving credits the signals exactly once.
  * Everything is OFF until the owner switches billing on in Admin. Existing members (tier member or admin) are
    unaffected either way: they keep exactly what they have today.
Payment details (wallet addresses, bank account) are stored in the database and edited in Admin, never in the code."""
import secrets
import time
from contextlib import closing

from starlette.exceptions import HTTPException
from starlette.responses import JSONResponse
from starlette.routing import Route

from . import auth, dbx

SCHEMA = """
    CREATE TABLE IF NOT EXISTS credit_balances(user_id INTEGER PRIMARY KEY, balance INTEGER NOT NULL DEFAULT 0);
    CREATE TABLE IF NOT EXISTS credit_ledger(id {ID}, user_id INTEGER NOT NULL, delta INTEGER NOT NULL, kind TEXT NOT NULL,
        ref TEXT NOT NULL DEFAULT '', note TEXT, admin_id INTEGER, created_at BIGINT NOT NULL);
    CREATE UNIQUE INDEX IF NOT EXISTS ux_ledger_ref ON credit_ledger(user_id, kind, ref) WHERE ref <> '';
    CREATE INDEX IF NOT EXISTS ix_ledger_user ON credit_ledger(user_id, id);
    CREATE TABLE IF NOT EXISTS billing_orders(id {ID}, user_id INTEGER NOT NULL, product TEXT NOT NULL, method TEXT NOT NULL,
        amount_usd DOUBLE PRECISION NOT NULL, amount_local DOUBLE PRECISION, currency TEXT NOT NULL,
        ref TEXT UNIQUE NOT NULL, txid TEXT, status TEXT NOT NULL, note TEXT, created_at BIGINT NOT NULL,
        submitted_at BIGINT, decided_at BIGINT, decided_by INTEGER);
    CREATE INDEX IF NOT EXISTS ix_orders_user ON billing_orders(user_id);
    CREATE INDEX IF NOT EXISTS ix_orders_status ON billing_orders(status);
    CREATE TABLE IF NOT EXISTS billing_subs(id {ID}, user_id INTEGER NOT NULL, plan TEXT NOT NULL, starts_at BIGINT NOT NULL,
        ends_at BIGINT NOT NULL, order_id BIGINT);
    CREATE INDEX IF NOT EXISTS ix_subs_user ON billing_subs(user_id);
    CREATE TABLE IF NOT EXISTS billing_settings(key TEXT PRIMARY KEY, value TEXT NOT NULL)"""

WELCOME = 5            # one-time welcome signals for a new member
OPEN_ORDERS_MAX = 3    # unfinished orders one member may hold at a time
ORDER_STALE_DAYS = 7   # an unsubmitted order older than this is closed automatically
CATALOG = {
    "topup": {"label": "Signal Top-Up", "usd": 3, "signals": 160, "days": 0, "notify": False, "mt5": False},
    "boost": {"label": "Signal Boost", "usd": 10, "signals": 550, "days": 0, "notify": False, "mt5": False},
    "vault": {"label": "Signal Vault", "usd": 100, "signals": 5500, "days": 0, "notify": False, "mt5": False},
    "plus": {"label": "Signals Plus", "usd": 15, "signals": 550, "days": 30, "notify": True, "mt5": False},
    "pro": {"label": "Signals Pro", "usd": 30, "signals": 550, "days": 30, "notify": True, "mt5": True},
}
METHODS = {"trc20": "USDT (TRC20)", "bep20": "USDT (BEP20)", "ngn": "Naira bank transfer"}
SETTING_KEYS = ("pay_trc20", "pay_bep20", "bank_name", "bank_account_name", "bank_account_number", "ngn_rate", "note",
                "support", "deriv_enabled", "deriv_app_id", "deriv_ib_link", "deriv_cashier_url")


def _migrate(con):
    con.add_column("billing_orders", "terms_at", "BIGINT")      # when the member accepted the terms for this order


def db():
    auth.db().close()
    return dbx.connect("credits", SCHEMA, _migrate)


def _now() -> int:
    return int(time.time())


# ---------------------------------------------------------------- settings and the master switch
_cache = {"ts": 0.0, "on": False}


def billing_on() -> bool:
    """True once the owner has opened billing. Any trouble reading it means "off", the safe, unchanged behaviour."""
    if time.time() - _cache["ts"] < 10:
        return _cache["on"]
    try:
        with closing(db()) as con:
            r = con.execute("SELECT value FROM billing_settings WHERE key='enabled'").fetchone()
        on = bool(r and r["value"] == "1")
    except Exception as e:
        print(f"[billing] could not read switch: {type(e).__name__}", flush=True)
        on = False
    _cache.update(ts=time.time(), on=on)
    return on


def settings(con) -> dict:
    out = {k: "" for k in SETTING_KEYS}
    for r in con.execute("SELECT key, value FROM billing_settings").fetchall():
        if r["key"] in out:
            out[r["key"]] = r["value"]
    return out


def _put(con, key: str, value: str):
    con.execute("INSERT INTO billing_settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value))


# ---------------------------------------------------------------- the ledger
def _credit(con, uid: int, delta: int, kind: str, ref: str = "", note: str = "", admin_id=None) -> bool:
    """Adds one ledger row and moves the balance, atomically. False if this (member, kind, ref) already exists."""
    try:
        con.execute("INSERT INTO credit_ledger(user_id,delta,kind,ref,note,admin_id,created_at) VALUES(?,?,?,?,?,?,?)",
                    (uid, delta, kind, ref, note, admin_id, _now()))
    except Exception as e:
        if dbx.is_duplicate(e):
            con.raw.rollback()
            return False
        raise
    con.execute("INSERT INTO credit_balances(user_id,balance) VALUES(?,?) ON CONFLICT(user_id) DO UPDATE SET "
                "balance=credit_balances.balance+excluded.balance", (uid, delta))
    return True


def balance(uid: int) -> int:
    with closing(db()) as con:
        r = con.execute("SELECT balance FROM credit_balances WHERE user_id=?", (uid,)).fetchone()
    return r["balance"] if r else 0


def spend(uid: int, ref: str) -> bool:
    """Takes 1 signal for `ref` (for example "signal:42"). Charging the same ref twice is safe and takes only once.
    False means there was nothing left to spend."""
    with closing(db()) as con:
        if con.execute("SELECT 1 FROM credit_ledger WHERE user_id=? AND kind='spend' AND ref=?", (uid, ref)).fetchone():
            return True
        cur = con.execute("UPDATE credit_balances SET balance=balance-1 WHERE user_id=? AND balance>=1", (uid,))
        if cur.rowcount != 1:
            con.raw.rollback()
            return False
        try:
            con.execute("INSERT INTO credit_ledger(user_id,delta,kind,ref,created_at) VALUES(?,?,?,?,?)",
                        (uid, -1, "spend", ref, _now()))
        except Exception as e:
            con.raw.rollback()
            if dbx.is_duplicate(e):      # a parallel request charged it first: that one counts, this one is free
                return True
            raise
        con.commit()
        return True


def ensure_welcome(uid: int):
    """The one-time welcome signals. Safe to call any number of times."""
    with closing(db()) as con:
        if con.execute("SELECT 1 FROM credit_ledger WHERE user_id=? AND kind='welcome'", (uid,)).fetchone():
            return
        if _credit(con, uid, WELCOME, "welcome", "welcome", "Welcome signals"):
            con.commit()


# ---------------------------------------------------------------- who is entitled to what
def grandfathered(user: dict) -> bool:
    """Members and the admin keep exactly what they had before billing existed."""
    return user["tier"] in ("member", "admin")


def active_plans(uid: int) -> list:
    with closing(db()) as con:
        rows = con.execute("SELECT plan, ends_at FROM billing_subs WHERE user_id=? AND ends_at>? ORDER BY ends_at DESC",
                           (uid, _now())).fetchall()
    return [dict(r) for r in rows]


def entitled(user: dict, feature: str) -> bool:
    """feature is "notify" or "mt5". Strict: free accounts only have it through an active plan while billing is on."""
    if grandfathered(user):
        return True
    if not billing_on():
        return False
    return any(CATALOG[p["plan"]].get(feature) for p in active_plans(user["id"]) if p["plan"] in CATALOG)


def mt5_allowed(user: dict) -> bool:
    """Connecting an MT5 stays open to everyone until billing is switched on, as it is today."""
    return (not billing_on()) or entitled(user, "mt5")


# ---------------------------------------------------------------- member API
def _label(product: str) -> str:
    return CATALOG.get(product, {}).get("label", product)


def _order_public(r) -> dict:
    return {"id": r["id"], "ref": r["ref"], "product": r["product"], "label": _label(r["product"]),
            "method": r["method"], "method_label": METHODS.get(r["method"], r["method"]), "amount_usd": r["amount_usd"],
            "amount_local": r["amount_local"], "currency": r["currency"], "status": r["status"], "txid": r["txid"],
            "note": r["note"], "created_at": r["created_at"]}


def _pay_info(s: dict) -> dict:
    rate = _num(s["ngn_rate"])
    return {"trc20": s["pay_trc20"], "bep20": s["pay_bep20"], "ngn_rate": rate or None,
            "bank": {"name": s["bank_name"], "account_name": s["bank_account_name"],
                     "account_number": s["bank_account_number"]} if s["bank_account_number"] else None,
            "note": s["note"], "support": s["support"]}


def _num(v):
    try:
        x = float(v)
        return x if x > 0 else None
    except (TypeError, ValueError):
        return None


async def billing_view(request):
    user = auth.require_user(request)
    on = billing_on()
    if on and not grandfathered(user):
        ensure_welcome(user["id"])
    with closing(db()) as con:
        s = settings(con)
        orders = [_order_public(r) for r in con.execute(
            "SELECT * FROM billing_orders WHERE user_id=? ORDER BY id DESC LIMIT 10", (user["id"],)).fetchall()]
        ledger = [dict(r) for r in con.execute(
            "SELECT delta, kind, note, created_at FROM credit_ledger WHERE user_id=? ORDER BY id DESC LIMIT 20",
            (user["id"],)).fetchall()]
    plans = active_plans(user["id"])
    return JSONResponse({
        "enabled": on, "grandfathered": grandfathered(user), "balance": balance(user["id"]), "welcome": WELCOME,
        "catalog": [{"key": k, **{f: v[f] for f in ("label", "usd", "signals", "days", "notify", "mt5")}}
                    for k, v in CATALOG.items()],
        "plans": [{"plan": p["plan"], "label": _label(p["plan"]), "ends_at": p["ends_at"]} for p in plans],
        "pay": _pay_info(s) if on else None, "methods": METHODS, "orders": orders, "ledger": ledger})


async def public_support(request):
    """The support contact, shown on the public terms page (no sign-in needed). Nothing else is exposed."""
    try:
        with closing(db()) as con:
            r = con.execute("SELECT value FROM billing_settings WHERE key='support'").fetchone()
        return JSONResponse({"support": r["value"] if r else ""})
    except Exception:
        return JSONResponse({"support": ""})


async def order_create(request):
    user = auth.require_user(request)
    if not billing_on():
        raise HTTPException(409, "Purchases are not open yet")
    d = await auth._json(request)
    product, method = d.get("product"), d.get("method")
    if d.get("accepted") is not True:
        raise HTTPException(422, "Please accept the Terms, Refunds and Support policy to continue")
    if product not in CATALOG:
        raise HTTPException(422, "Unknown product")
    if method not in METHODS:
        raise HTTPException(422, "Choose a payment method")
    usd, now = float(CATALOG[product]["usd"]), _now()
    with closing(db()) as con:
        s = settings(con)
        con.execute("UPDATE billing_orders SET status='expired' WHERE user_id=? AND status='pending' AND created_at<?",
                    (user["id"], now - ORDER_STALE_DAYS * 86400))
        if method == "trc20" and not s["pay_trc20"] or method == "bep20" and not s["pay_bep20"]:
            raise HTTPException(409, "This payment method is not available yet")
        local, cur = usd, "USDT"
        if method == "ngn":
            rate = _num(s["ngn_rate"])
            if not (rate and s["bank_account_number"]):
                raise HTTPException(409, "Naira payments are not available yet")
            local, cur = round(usd * rate), "NGN"
        open_n = con.execute("SELECT COUNT(*) AS c FROM billing_orders WHERE user_id=? AND status IN ('pending','submitted')",
                             (user["id"],)).fetchone()["c"]
        if open_n >= OPEN_ORDERS_MAX:
            raise HTTPException(429, "You already have open orders. Finish or cancel one before starting another.")
        alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
        ref = "TL-" + "".join(secrets.choice(alphabet) for _ in range(6))
        oid = con.insert("INSERT INTO billing_orders(user_id,product,method,amount_usd,amount_local,currency,ref,status,"
                         "created_at,terms_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                         (user["id"], product, method, usd, local, cur, ref, "pending", now, now))
        con.commit()
        row = con.execute("SELECT * FROM billing_orders WHERE id=?", (oid,)).fetchone()
    return JSONResponse({"order": _order_public(row), "pay": _pay_info(s)})


async def order_paid(request):
    user, oid = auth.require_user(request), request.path_params["oid"]
    d = await auth._json(request)
    txid = str(d.get("txid", "")).strip()
    if not 6 <= len(txid) <= 200:
        raise HTTPException(422, "Enter the transaction ID or the bank transfer reference (at least 6 characters)")
    with closing(db()) as con:
        cur = con.execute("UPDATE billing_orders SET status='submitted', txid=?, submitted_at=? "
                          "WHERE id=? AND user_id=? AND status='pending'", (txid, _now(), oid, user["id"]))
        if cur.rowcount != 1:
            raise HTTPException(409, "This order cannot be submitted (already sent, closed or not yours)")
        con.commit()
    return JSONResponse({"ok": True})


async def order_cancel(request):
    user, oid = auth.require_user(request), request.path_params["oid"]
    with closing(db()) as con:
        cur = con.execute("UPDATE billing_orders SET status='cancelled' WHERE id=? AND user_id=? AND status='pending'",
                          (oid, user["id"]))
        if cur.rowcount != 1:
            raise HTTPException(409, "Only an order that has not been submitted can be cancelled")
        con.commit()
    return JSONResponse({"ok": True})


# ---------------------------------------------------------------- admin API
def _emails(ids) -> dict:
    ids = sorted(set(ids))
    if not ids:
        return {}
    with closing(auth.db()) as con:
        rows = con.execute(f"SELECT id, email FROM users WHERE id IN ({','.join('?' * len(ids))})", ids).fetchall()
    return {r["id"]: r["email"] for r in rows}


async def admin_view(request):
    auth.require_admin(request)
    with closing(db()) as con:
        s = settings(con)
        on = con.execute("SELECT value FROM billing_settings WHERE key='enabled'").fetchone()
        open_rows = con.execute("SELECT * FROM billing_orders WHERE status IN ('submitted','pending') "
                                "ORDER BY CASE status WHEN 'submitted' THEN 0 ELSE 1 END, id DESC LIMIT 60").fetchall()
        done_rows = con.execute("SELECT * FROM billing_orders WHERE status IN ('approved','rejected') "
                                "ORDER BY id DESC LIMIT 15").fetchall()
    names = _emails([r["user_id"] for r in list(open_rows) + list(done_rows)])
    shape = lambda r: {**_order_public(r), "email": names.get(r["user_id"], "?"), "submitted_at": r["submitted_at"]}
    return JSONResponse({"enabled": bool(on and on["value"] == "1"), "settings": s,
                         "open": [shape(r) for r in open_rows], "recent": [shape(r) for r in done_rows]})


async def admin_settings(request):
    u = auth.require_admin(request)
    d = await auth._json(request)
    with closing(db()) as con:
        for k in SETTING_KEYS:
            if k in d:
                v = str(d[k]).strip()
                if len(v) > 300:
                    raise HTTPException(422, f"{k} is too long")
                if k == "ngn_rate" and v and not _num(v):
                    raise HTTPException(422, "The Naira rate must be a positive number")
                _put(con, k, v)
        if "enabled" in d:
            if not isinstance(d["enabled"], bool):
                raise HTTPException(422, "enabled must be true or false")
            _put(con, "enabled", "1" if d["enabled"] else "0")
            print(f"[billing] switch set to {d['enabled']} by admin {u['id']}", flush=True)
        con.commit()
    _cache["ts"] = 0.0
    return JSONResponse({"ok": True})


async def admin_approve(request):
    u, oid = auth.require_admin(request), request.path_params["oid"]
    now = _now()
    with closing(db()) as con:
        row = con.execute("SELECT * FROM billing_orders WHERE id=?", (oid,)).fetchone()
        if not row:
            raise HTTPException(404, "Order not found")
        p = CATALOG.get(row["product"])
        if not p:
            raise HTTPException(409, "This order's product no longer exists")
        cur = con.execute("UPDATE billing_orders SET status='approved', decided_at=?, decided_by=? "
                          "WHERE id=? AND status IN ('pending','submitted')", (now, u["id"], oid))
        if cur.rowcount != 1:
            raise HTTPException(409, "This order has already been decided")
        fresh = _credit(con, row["user_id"], p["signals"], "purchase", f"order:{oid}", f"{p['label']} ({row['ref']})", u["id"])
        if not fresh:                      # the credit already exists: keep it, just record the approval
            con.execute("UPDATE billing_orders SET status='approved', decided_at=?, decided_by=? WHERE id=?",
                        (now, u["id"], oid))
        elif p["days"]:
            last = con.execute("SELECT MAX(ends_at) AS e FROM billing_subs WHERE user_id=? AND plan=? AND ends_at>?",
                               (row["user_id"], row["product"], now)).fetchone()["e"]
            start = max(now, last or 0)
            con.execute("INSERT INTO billing_subs(user_id,plan,starts_at,ends_at,order_id) VALUES(?,?,?,?,?)",
                        (row["user_id"], row["product"], start, start + p["days"] * 86400, oid))
        con.commit()
    return JSONResponse({"ok": True})


async def admin_reject(request):
    u, oid = auth.require_admin(request), request.path_params["oid"]
    d = await auth._json(request)
    note = str(d.get("note", "")).strip()[:200]
    with closing(db()) as con:
        cur = con.execute("UPDATE billing_orders SET status='rejected', note=?, decided_at=?, decided_by=? "
                          "WHERE id=? AND status IN ('pending','submitted')", (note or "Payment could not be confirmed", _now(), u["id"], oid))
        if cur.rowcount != 1:
            raise HTTPException(409, "This order has already been decided")
        con.commit()
    return JSONResponse({"ok": True})


async def admin_adjust(request):
    """Add or remove signals by hand (a goodwill gift, a correction). Always recorded with the admin's note."""
    u = auth.require_admin(request)
    d = await auth._json(request)
    email = str(d.get("email", "")).strip().lower()
    try:
        delta = int(d.get("delta"))
    except (TypeError, ValueError):
        raise HTTPException(422, "Enter a whole number of signals")
    note = str(d.get("note", "")).strip()[:200]
    if delta == 0 or abs(delta) > 100000:
        raise HTTPException(422, "Enter a number between 1 and 100000 (negative to remove)")
    if not note:
        raise HTTPException(422, "A note is required so the ledger explains the change")
    with closing(auth.db()) as con:
        r = con.execute("SELECT id FROM users WHERE lower(email)=?", (email,)).fetchone()
    if not r:
        raise HTTPException(404, "No member with that email")
    with closing(db()) as con:
        cur_bal = con.execute("SELECT balance FROM credit_balances WHERE user_id=?", (r["id"],)).fetchone()
        if delta < 0 and (cur_bal["balance"] if cur_bal else 0) + delta < 0:
            raise HTTPException(409, "That would take the balance below zero")
        _credit(con, r["id"], delta, "adjust", secrets.token_hex(6), note, u["id"])
        con.commit()
    return JSONResponse({"ok": True, "balance": balance(r["id"])})


routes = [
    Route("/api/public/support", public_support, methods=["GET"]),
    Route("/api/billing", billing_view, methods=["GET"]),
    Route("/api/billing/order", order_create, methods=["POST"]),
    Route("/api/billing/order/{oid:int}/paid", order_paid, methods=["POST"]),
    Route("/api/billing/order/{oid:int}/cancel", order_cancel, methods=["POST"]),
    Route("/api/admin/billing", admin_view, methods=["GET"]),
    Route("/api/admin/billing/settings", admin_settings, methods=["POST"]),
    Route("/api/admin/billing/order/{oid:int}/approve", admin_approve, methods=["POST"]),
    Route("/api/admin/billing/order/{oid:int}/reject", admin_reject, methods=["POST"]),
    Route("/api/admin/billing/adjust", admin_adjust, methods=["POST"]),
]
