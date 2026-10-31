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
    CREATE TABLE IF NOT EXISTS plan_mode(user_id INTEGER PRIMARY KEY, mode TEXT NOT NULL, since BIGINT, carry DOUBLE PRECISION NOT NULL DEFAULT 0, updated_at BIGINT NOT NULL);
    CREATE TABLE IF NOT EXISTS billing_settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS payment_events(id {ID}, order_id BIGINT, provider_payment_id TEXT, source TEXT NOT NULL,
        kind TEXT NOT NULL, provider_status TEXT, detail TEXT, dedupe_key TEXT UNIQUE, admin_id INTEGER, created_at BIGINT NOT NULL);
    CREATE INDEX IF NOT EXISTS ix_pevents_order ON payment_events(order_id, id);
    CREATE INDEX IF NOT EXISTS ix_pevents_kind ON payment_events(kind, id)"""

WELCOME = 5            # one-time welcome signals for a new member
OPEN_ORDERS_MAX = 3    # unfinished orders one member may hold at a time
ORDER_STALE_DAYS = 7   # an unsubmitted order older than this is closed automatically
CATALOG = {
    "topup": {"label": "Signal Top-Up", "usd": 5, "signals": 267, "days": 0, "notify": False, "mt5": False},
    "boost": {"label": "Signal Boost", "usd": 10, "signals": 550, "days": 0, "notify": False, "mt5": False},
    "vault": {"label": "Signal Vault", "usd": 100, "signals": 5500, "days": 0, "notify": False, "mt5": False},
    "plus": {"label": "Signals Plus", "usd": 15, "signals": 550, "days": 30, "notify": True, "mt5": False, "copy_ib": True},
    "pro": {"label": "Signals Pro", "usd": 30, "signals": 550, "days": 30, "notify": True, "mt5": True, "copy": True},
    # Copy trading on its own: MT5 connection + copy trading, no signals and no notifications (see copy_plan.py)
    "copy": {"label": "Copy Trading", "usd": 15, "signals": 0, "days": 30, "notify": False, "mt5": True, "copy": True},
}
# Free plans given by perks.py. They are not products: they cannot be bought and do not appear in the shop.
GRANTS = {
    "partner_notify": {"label": "Partner notifications (free)", "notify": True, "mt5": False},
    "founder_mt5": {"label": "Founding member: MT5 connection (free)", "notify": True, "mt5": True},
}
METHODS = {"trc20": "USDT (TRC20)", "bep20": "USDT (BEP20)"}
LEGACY_LABELS = {"ngn": "Naira bank transfer (old form)"}   # only to label old orders; the manual flow no longer creates them
SETTING_KEYS = ("pay_trc20", "pay_bep20", "bank_name", "bank_account_name", "bank_account_number", "ngn_rate", "note",
                "support", "deriv_enabled", "deriv_app_id", "deriv_ib_link", "deriv_cashier_url", "nowpayments_enabled")
AUTO_METHOD = "nowpayments"      # billing_orders.method of automatic crypto orders. Deliberately NOT in METHODS: the manual
                                 # order endpoint refuses it, so only nowpay.py can create or settle such an order.
AUTO_LABEL = "USDT (TRC20), automatic"
NAIRA_METHOD = "moniepoint"      # billing_orders.method of "Pay with Naira" orders (naira.py): a bank transfer to the Moniepoint business
                                 # account that an administrator verifies. Also NOT in METHODS: only naira.py can create or settle one.
NAIRA_LABEL = "Naira bank transfer (Moniepoint)"
PROVIDER_METHODS = (AUTO_METHOD, NAIRA_METHOD)      # orders with their own module and admin panel, never touched by the manual flow below


def _migrate(con):
    con.add_column("billing_orders", "terms_at", "BIGINT")      # when the member accepted the terms for this order
    # Automatic crypto payments (nowpay.py). Every column is nullable and additive: existing orders are untouched and
    # the manual flow above ignores these columns. Mirrored by migrations/2026_10_nowpayments.sql.
    for col, ddl in (("provider", "TEXT"), ("provider_payment_id", "TEXT"), ("provider_status", "TEXT"),
                     ("pay_address", "TEXT"), ("pay_amount", "DOUBLE PRECISION"), ("pay_currency", "TEXT"),
                     ("pay_network", "TEXT"), ("actually_paid", "DOUBLE PRECISION"), ("expires_at", "BIGINT"),
                     ("fulfilled_at", "BIGINT"), ("review_reason", "TEXT"), ("last_polled_at", "BIGINT"),
                     ("updated_at", "BIGINT")):
        con.add_column("billing_orders", col, ddl)
    # One provider payment can belong to one order only: this is the database-level guard against cross-matching.
    con.raw.cursor().execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_orders_provider_pid ON billing_orders(provider_payment_id) "
                             "WHERE provider_payment_id IS NOT NULL")
    # Manual Naira payments (naira.py). Additive and nullable: the payment narration of an order, unique across orders, and a guard that
    # one verified bank transaction (billing_orders.txid) can settle one Naira order only. Mirrored by migrations/2026_10_naira.sql.
    con.add_column("billing_orders", "narration", "TEXT")
    con.raw.cursor().execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_orders_narration ON billing_orders(narration) WHERE narration IS NOT NULL")
    con.raw.cursor().execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_orders_bank_txid ON billing_orders(txid) "
                             "WHERE method='moniepoint' AND txid IS NOT NULL")


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


def grant_product(con, uid: int, product: str, order_id: int, ref: str, admin_id=None) -> bool:
    """Gives a member what a paid order buys: the signals, and for a plan its notifications / MT5 days. The single place
    that decides this, used by the manual approval in Admin and by automatic crypto fulfilment (nowpay.py).
    Does NOT commit. False means the signals for this order were already credited (the ledger refuses a second
    ("purchase", "order:<id>") row), in which case nothing else is changed either."""
    p = CATALOG[product]
    fresh = _credit(con, uid, p["signals"], "purchase", f"order:{order_id}", f"{p['label']} ({ref})", admin_id)
    if fresh and p["days"]:
        now = _now()
        last = con.execute("SELECT MAX(ends_at) AS e FROM billing_subs WHERE user_id=? AND plan=? AND ends_at>?",
                           (uid, product, now)).fetchone()["e"]
        start = max(now, last or 0)
        con.execute("INSERT INTO billing_subs(user_id,plan,starts_at,ends_at,order_id) VALUES(?,?,?,?,?)",
                    (uid, product, start, start + p["days"] * 86400, order_id))
    return fresh


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
    """feature is "notify", "mt5" or "copy". Strict: free accounts only have it through an active plan while billing is on.
    Copy trading is never free for anyone (copy_plan.py). A Signals Plus member linked to our IB who switched to copy mode has the MT5
    connection but their signals and notifications are paused."""
    if feature == "copy":
        from . import copy_plan
        return copy_plan.copy_active(user)
    if grandfathered(user):
        return True
    if not billing_on():
        return False
    keys = {p["plan"] for p in active_plans(user["id"])}
    if "plus" in keys:
        try:
            from . import copy_plan
            if copy_plan.swap(user, keys) == "copy":
                if feature == "mt5":
                    return True
                keys.discard("plus")
        except Exception as e:
            print(f"[copy-plan] entitlement check failed: {type(e).__name__}", flush=True)
    return any((CATALOG.get(k) or GRANTS.get(k) or {}).get(feature) for k in keys)


def mt5_allowed(user: dict) -> bool:
    """Connecting an MT5 stays open to everyone until billing is switched on, as it is today."""
    return (not billing_on()) or entitled(user, "mt5")


def mt5_can_report(user: dict) -> bool:
    """mt5_allowed, plus a founding member whose funding is still to be verified: they must connect so the EA can report it."""
    if mt5_allowed(user):
        return True
    try:
        from . import copy_plan
        if copy_plan.state(user)["swap"]:        # Signals Plus linked to our IB: may connect so Copy trading can be switched on (copy_plan.py)
            return True
    except Exception as e:
        print(f"[copy-plan] connect check failed: {type(e).__name__}", flush=True)
    try:
        from . import perks
        return perks.founder_pending(user["id"])
    except Exception:
        return False


def _price_fields(user: dict, product: str) -> dict:
    """The price this member pays (partner members pay less for Pro) and, when different, the list price."""
    base = float(CATALOG[product]["usd"])
    try:
        from . import perks
        price = float(perks.price_for(user, product, base))
    except Exception:
        price = base
    return {"usd": price, **({"list_usd": base} if price != base else {})}


def _copy_view(user: dict):
    try:
        from . import copy_plan
        return copy_plan.view(user)
    except Exception as e:
        print(f"[copy-plan] view failed: {type(e).__name__}", flush=True)
        return None


def _partner_view(user: dict):
    try:
        from . import perks
        return perks.member_view(user["id"])
    except Exception:
        return None


# ---------------------------------------------------------------- member API
def _label(product: str) -> str:
    return (CATALOG.get(product) or GRANTS.get(product) or {}).get("label", product)


def _col(r, name):
    """A column that may be NULL (or absent on a row read before the columns existed)."""
    try:
        return r[name]
    except (KeyError, IndexError):
        return None


def _order_public(r) -> dict:
    out = {"id": r["id"], "ref": r["ref"], "product": r["product"], "label": _label(r["product"]),
           "method": r["method"],
           "method_label": AUTO_LABEL if r["method"] == AUTO_METHOD else NAIRA_LABEL if r["method"] == NAIRA_METHOD
           else METHODS.get(r["method"]) or LEGACY_LABELS.get(r["method"], r["method"]),
           "amount_usd": r["amount_usd"], "amount_local": r["amount_local"], "currency": r["currency"],
           "status": r["status"], "txid": None if r["method"] == NAIRA_METHOD else r["txid"],   # a Naira order's txid is the admin's bank reference
           "note": None if r["method"] == NAIRA_METHOD else r["note"], "created_at": r["created_at"]}
    if r["method"] in PROVIDER_METHODS:   # the transaction reference a member can quote to support
        out.update(provider_payment_id=_col(r, "provider_payment_id"), provider_status=_col(r, "provider_status"),
                   fulfilled_at=_col(r, "fulfilled_at"), expires_at=_col(r, "expires_at"))
    return out


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
        try:
            from . import perks
            perks.sync(user["id"])          # partner perks, if Deriv has confirmed this member
        except Exception as e:
            print(f"[perks] sync failed: {type(e).__name__}", flush=True)
    cp = _copy_view(user)                   # also charges the signals used while in copy mode, so the balance below is current
    with closing(db()) as con:
        s = settings(con)
        orders = [_order_public(r) for r in con.execute(
            "SELECT * FROM billing_orders WHERE user_id=? ORDER BY id DESC LIMIT 10", (user["id"],)).fetchall()]
        ledger = [dict(r) for r in con.execute(
            "SELECT delta, kind, note, created_at FROM credit_ledger WHERE user_id=? ORDER BY id DESC LIMIT 20",
            (user["id"],)).fetchall()]
    plans = active_plans(user["id"])
    try:
        from . import nowpay
        crypto = nowpay.public_info(user)
    except Exception as e:
        print(f"[nowpay] public info failed: {type(e).__name__}", flush=True)
        crypto = {"enabled": False}
    try:
        from . import naira as naira_pay
        naira = naira_pay.public_info(user)
    except Exception as e:
        print(f"[naira] public info failed: {type(e).__name__}", flush=True)
        naira = {"enabled": False}
    shop = on or bool(crypto.get("enabled")) or bool(naira.get("enabled"))   # the admin pilot may open the shop for the admin while billing is closed
    if on:
        pay = _pay_info(s)
    elif shop:
        pay = _pay_info({k: "" for k in SETTING_KEYS})
    else:
        pay = None
    return JSONResponse({
        "enabled": shop, "grandfathered": grandfathered(user), "balance": balance(user["id"]), "welcome": WELCOME,
        "partner": _partner_view(user), "crypto": crypto, "naira": naira,
        "copy_plan": cp,
        "catalog": [{"key": k, **{f: v[f] for f in ("label", "usd", "signals", "days", "notify", "mt5")},
                     "copy": bool(v.get("copy")), "copy_ib": bool(v.get("copy_ib")), **_price_fields(user, k)}
                    for k, v in CATALOG.items()],
        "plans": [{"plan": p["plan"], "label": _label(p["plan"]), "ends_at": p["ends_at"]} for p in plans],
        "pay": pay, "methods": METHODS, "orders": orders, "ledger": ledger})


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
    usd, now = _price_fields(user, product)["usd"], _now()
    with closing(db()) as con:
        s = settings(con)
        con.execute("UPDATE billing_orders SET status='expired' WHERE user_id=? AND status='pending' AND created_at<?",
                    (user["id"], now - ORDER_STALE_DAYS * 86400))
        if method == "trc20" and not s["pay_trc20"] or method == "bep20" and not s["pay_bep20"]:
            raise HTTPException(409, "This payment method is not available yet")
        local, cur = usd, "USDT"
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
                          "WHERE id=? AND user_id=? AND status='pending' AND method NOT IN (?,?)",
                          (txid, _now(), oid, user["id"], *PROVIDER_METHODS))
        if cur.rowcount != 1:
            raise HTTPException(409, "This order cannot be submitted (already sent, closed or not yours)")
        con.commit()
    return JSONResponse({"ok": True})


async def order_cancel(request):
    user, oid = auth.require_user(request), request.path_params["oid"]
    with closing(db()) as con:
        cur = con.execute("UPDATE billing_orders SET status='cancelled' WHERE id=? AND user_id=? AND status='pending' AND method NOT IN (?,?)",
                          (oid, user["id"], *PROVIDER_METHODS))
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
        # Automatic crypto orders have their own panel (nowpay.py): they never wait here for a manual approval.
        open_rows = con.execute("SELECT * FROM billing_orders WHERE status IN ('submitted','pending') AND method NOT IN (?,?) "
                                "ORDER BY CASE status WHEN 'submitted' THEN 0 ELSE 1 END, id DESC LIMIT 60",
                                PROVIDER_METHODS).fetchall()
        done_rows = con.execute("SELECT * FROM billing_orders WHERE status IN ('approved','rejected') AND method NOT IN (?,?) "
                                "ORDER BY id DESC LIMIT 15", PROVIDER_METHODS).fetchall()
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
        if row["method"] in PROVIDER_METHODS:  # automatic orders are settled by the payment provider, or resolved in their own panel
            raise HTTPException(409, "This order has its own payment panel (Crypto payments or Naira payments)")
        cur = con.execute("UPDATE billing_orders SET status='approved', decided_at=?, decided_by=? "
                          "WHERE id=? AND status IN ('pending','submitted')", (now, u["id"], oid))
        if cur.rowcount != 1:
            raise HTTPException(409, "This order has already been decided")
        if not grant_product(con, row["user_id"], row["product"], oid, row["ref"], u["id"]):
            # the credit already exists: keep it, just record the approval
            con.execute("UPDATE billing_orders SET status='approved', decided_at=?, decided_by=? WHERE id=?",
                        (now, u["id"], oid))
        con.commit()
    return JSONResponse({"ok": True})


async def admin_reject(request):
    u, oid = auth.require_admin(request), request.path_params["oid"]
    d = await auth._json(request)
    note = str(d.get("note", "")).strip()[:200]
    with closing(db()) as con:
        cur = con.execute("UPDATE billing_orders SET status='rejected', note=?, decided_at=?, decided_by=? "
                          "WHERE id=? AND status IN ('pending','submitted') AND method NOT IN (?,?)",
                          (note or "Payment could not be confirmed", _now(), u["id"], oid, *PROVIDER_METHODS))
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
