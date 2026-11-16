"""The member wallet: money kept on TradeLens, spent on its services.

  * A member adds money through the SAME channels as any purchase (automatic USDT, Naira bank transfer, manual USDT). A top-up is an
    ordinary billing order for a wallet product (credits.CATALOG "wallet10" ... "wallet250"); when the order is paid, credits.grant_product
    adds the money here instead of adding signals. So every existing safeguard (provider checks, admin review, one credit per order) applies.
  * Wallet money is not used for anything until the member pays for a product with it (POST /api/wallet/pay). That is one database
    transaction: the debit, the wallet ledger row, the order (created already approved) and the product itself (credits.grant_product).
    Any failure rolls all of it back, so money can never be taken without the product, nor the product given without the money.
  * Money is whole cents (integers). The balance can never go below zero (a CHECK in the table and a conditional UPDATE).
  * The wallet ledger (wallet_ledger) has one row per change and refuses a second row for the same (member, kind, ref).
  * Referral points and rewards count the PURCHASE paid from the wallet (an approved order), not the top-up, so nothing is counted twice.
  * Master switch (Admin > Crypto payments): off (default, nothing visible), admin (pilot) or public. Money already in a wallet can always be seen.
  * There is no withdrawal. Refunds are decided by an administrator and recorded as an adjustment.
Additive: own tables (mirrored by migrations/2026_11_wallet.sql); nothing existing is edited except where credits.py calls this module."""
import secrets
from contextlib import closing

from starlette.exceptions import HTTPException
from starlette.responses import JSONResponse
from starlette.routing import Route

from . import auth, credits

MODES = ("off", "admin", "public")
DUP_WINDOW = 15          # seconds: the same product paid twice from the wallet inside this window is refused (a double tap)
ADJUST_MAX_USD = 10000


def mode(con=None) -> str:
    try:
        if con is None:
            with closing(credits.db()) as c:
                return mode(c)
        r = con.execute("SELECT value FROM billing_settings WHERE key='wallet_mode'").fetchone()
        return r["value"] if r and r["value"] in MODES else "off"
    except Exception as e:
        print(f"[wallet] could not read mode: {type(e).__name__}", flush=True)
        return "off"


def allowed(user: dict, con=None) -> bool:
    m = mode(con)
    return m == "public" or (m == "admin" and user["tier"] == "admin")


def cents(con, uid: int) -> int:
    r = con.execute("SELECT balance_cents FROM wallet_balances WHERE user_id=?", (uid,)).fetchone()
    return int(r["balance_cents"]) if r else 0


def credit(con, uid: int, amount_cents: int, kind: str, ref: str, note: str = "", admin_id=None) -> bool:
    """Adds money. Does NOT commit. False (and the transaction rolled back) if this (member, kind, ref) was already recorded."""
    if amount_cents <= 0:
        raise ValueError("credit must be positive")
    try:
        con.execute("INSERT INTO wallet_ledger(user_id,delta_cents,kind,ref,note,admin_id,created_at) VALUES(?,?,?,?,?,?,?)",
                    (uid, amount_cents, kind, ref, note, admin_id, credits._now()))
    except Exception as e:
        if credits.dbx.is_duplicate(e):
            con.raw.rollback()
            return False
        raise
    con.execute("INSERT INTO wallet_balances(user_id,balance_cents) VALUES(?,?) ON CONFLICT(user_id) DO UPDATE SET "
                "balance_cents=wallet_balances.balance_cents+excluded.balance_cents", (uid, amount_cents))
    return True


def debit(con, uid: int, amount_cents: int, kind: str, ref: str, note: str = "", admin_id=None) -> bool:
    """Takes money. Does NOT commit. False when the balance is not enough (nothing changed)."""
    if amount_cents <= 0:
        raise ValueError("debit must be positive")
    if con.execute("UPDATE wallet_balances SET balance_cents=balance_cents-? WHERE user_id=? AND balance_cents>=?",
                   (amount_cents, uid, amount_cents)).rowcount != 1:
        con.raw.rollback()
        return False
    con.execute("INSERT INTO wallet_ledger(user_id,delta_cents,kind,ref,note,admin_id,created_at) VALUES(?,?,?,?,?,?,?)",
                (uid, -amount_cents, kind, ref, note, admin_id, credits._now()))
    return True


def _usd(c: int) -> float:
    return round(c / 100.0, 2)


KIND_LABEL = {"topup": "Top-up", "purchase": "Purchase", "adjust": "Adjustment by support"}


def view(user: dict) -> dict:
    """What the member's pages need. Nothing is shown while the wallet is off, unless the member already holds money."""
    with closing(credits.db()) as con:
        c = cents(con, user["id"])
        if not allowed(user, con) and c <= 0:
            return {"enabled": False}
        ledger = [{"label": KIND_LABEL.get(r["kind"], r["kind"]), "note": r["note"], "usd": _usd(r["delta_cents"]), "created_at": r["created_at"]}
                  for r in con.execute("SELECT kind, note, delta_cents, created_at FROM wallet_ledger WHERE user_id=? ORDER BY id DESC LIMIT 20",
                                       (user["id"],)).fetchall()]
        on = allowed(user, con)
    return {"enabled": True, "can_add": on, "balance_usd": _usd(c),
            "topups": [{"key": k, "label": v["label"], "usd": v["usd"], "signals": 0, "days": 0} for k, v in credits.CATALOG.items() if v.get("wallet")] if on else [],
            "ledger": ledger}


async def me(request):
    user = auth.require_user(request)
    try:
        d = view(user)
    except Exception as e:
        print(f"[wallet] balance failed: {type(e).__name__}", flush=True)
        d = {"enabled": False}
    return JSONResponse({"enabled": d["enabled"], "balance_usd": d.get("balance_usd", 0)})


async def pay(request):
    """Pay for a product from the wallet balance."""
    user = auth.require_user(request)
    d = await auth._json(request)
    product = d.get("product")
    if d.get("accepted") is not True:
        raise HTTPException(422, "Please accept the Terms, Refunds and Support policy to continue")
    p = credits.CATALOG.get(product) if isinstance(product, str) else None
    if not p or p.get("wallet"):
        raise HTTPException(422, "Unknown product")
    if not credits.billing_on():
        raise HTTPException(409, "Purchases are not open yet")
    usd = credits._price_fields(user, product)["usd"]
    price, now = int(round(float(usd) * 100)), credits._now()
    with closing(credits.db()) as con:
        if not allowed(user, con) and cents(con, user["id"]) <= 0:
            raise HTTPException(409, "The wallet is not open yet")
        if con.execute("SELECT 1 FROM billing_orders WHERE user_id=? AND method=? AND product=? AND created_at>?",
                       (user["id"], credits.WALLET_METHOD, product, now - DUP_WINDOW)).fetchone():
            raise HTTPException(429, "You just paid for this. Please wait a moment and check your plan before paying again.")
        try:
            alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
            ref = "TL-" + "".join(secrets.choice(alphabet) for _ in range(6))
            if cents(con, user["id"]) < price:
                raise HTTPException(402, "Your wallet balance is not enough for this. Add money to your wallet first.")
            oid = con.insert("INSERT INTO billing_orders(user_id,product,method,amount_usd,amount_local,currency,ref,status,created_at,terms_at,"
                             "decided_at,fulfilled_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                             (user["id"], product, credits.WALLET_METHOD, float(usd), float(usd), "USD", ref, "approved", now, now, now, now))
            if not debit(con, user["id"], price, "purchase", f"order:{oid}", f"{p['label']} ({ref})"):
                raise HTTPException(402, "Your wallet balance is not enough for this. Add money to your wallet first.")
            if not credits.grant_product(con, user["id"], product, oid, ref):
                con.raw.rollback()
                raise HTTPException(409, "This purchase was already recorded")
            con.commit()
        except HTTPException:
            con.raw.rollback()
            raise
        except Exception as e:
            con.raw.rollback()
            print(f"[wallet] payment failed and was rolled back: {type(e).__name__}", flush=True)
            raise HTTPException(500, "The payment could not be completed. Nothing was taken from your wallet.")
        left = cents(con, user["id"])
    try:
        from . import admin_alerts
        admin_alerts.payment(user["id"], p["label"], float(usd), "Wallet payment", ref)
    except Exception:
        pass
    return JSONResponse({"ok": True, "ref": ref, "label": p["label"], "days": p["days"], "signals": p["signals"],
                         "balance_usd": _usd(left), "signal_balance": credits.balance(user["id"])})


# ---------------------------------------------------------------- admin
async def admin_get(request):
    auth.require_admin(request)
    with closing(credits.db()) as con:
        total = con.execute("SELECT COALESCE(SUM(balance_cents),0) AS s, COUNT(*) AS n FROM wallet_balances WHERE balance_cents>0").fetchone()
        return JSONResponse({"mode": mode(con), "held_usd": _usd(int(total["s"])), "holders": int(total["n"])})


async def admin_mode(request):
    u = auth.require_admin(request)
    d = await auth._json(request)
    if d.get("mode") not in MODES:
        raise HTTPException(422, "Mode must be off, admin or public")
    with closing(credits.db()) as con:
        credits._put(con, "wallet_mode", d["mode"])
        con.commit()
    print(f"[wallet] mode set to {d['mode']} by admin {u['id']}", flush=True)
    return JSONResponse({"ok": True, "mode": d["mode"]})


async def admin_adjust(request):
    """Add or remove money for a member (a refund, a correction). A note is required and everything is recorded with the admin's id."""
    u = auth.require_admin(request)
    d = await auth._json(request)
    email = str(d.get("email", "")).strip().lower()
    note = str(d.get("note", "")).strip()[:200]
    try:
        amount = round(float(d.get("amount_usd")), 2)
    except (TypeError, ValueError):
        raise HTTPException(422, "Enter the amount in dollars")
    if amount == 0 or abs(amount) > ADJUST_MAX_USD or amount != amount:
        raise HTTPException(422, f"The amount must be between -{ADJUST_MAX_USD} and {ADJUST_MAX_USD}, and not zero")
    if len(note) < 3:
        raise HTTPException(422, "Write a note saying why")
    with closing(auth.db()) as ac:
        r = ac.execute("SELECT id FROM users WHERE lower(email)=?", (email,)).fetchone()
    if not r:
        raise HTTPException(404, "No member has that email")
    uid, c = r["id"], int(round(abs(amount) * 100))
    ref = "adj-" + secrets.token_hex(6)
    with closing(credits.db()) as con:
        ok = credit(con, uid, c, "adjust", ref, note, u["id"]) if amount > 0 else debit(con, uid, c, "adjust", ref, note, u["id"])
        if not ok:
            raise HTTPException(409, "That would take the wallet below zero" if amount < 0 else "Already recorded")
        con.commit()
        left = cents(con, uid)
    print(f"[wallet] admin {u['id']} adjusted member {uid} by {amount}", flush=True)
    return JSONResponse({"ok": True, "balance_usd": _usd(left)})


routes = [
    Route("/api/wallet", me, methods=["GET"]),
    Route("/api/wallet/pay", pay, methods=["POST"]),
    Route("/api/admin/wallet", admin_get, methods=["GET"]),
    Route("/api/admin/wallet/mode", admin_mode, methods=["POST"]),
    Route("/api/admin/wallet/adjust", admin_adjust, methods=["POST"]),
]
