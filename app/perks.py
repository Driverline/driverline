"""Partner-member perks and the founding-member offer.

Who counts as a partner member: a TradeLens member whose Deriv account Deriv's partner records CONFIRM as tagged to you (the
"confirmed" status in Admin > Partner tags). A login alone proves nothing, so nothing here is given on the strength of a login.

Perks for every partner member (given once, automatically, within minutes of the confirmation):
  * notifications free for 60 days (a free plan row, so every existing notification rule applies unchanged)
  * 5 extra welcome signals, so 10 in total instead of 5
  * Signals Pro (the MT5 connection) at $15 instead of $30

Founding members: the first 10 partner members get a founder slot. A slot holder who connects a LIVE Deriv MT5 account showing at
least $100 gets the MT5 connection free for 30 days (with notifications), granted at the moment the EA reports that balance.
Until verified, a slot holder may connect so the balance can be read, and nothing else about MT5 is unlocked.
The balance is what the MT5 account shows when it connects. It proves the account holds the money, not where it came from.

Everything takes effect only while billing is switched on, exactly like paid plans.
"""
import time
from contextlib import closing

from starlette.exceptions import HTTPException
from starlette.responses import JSONResponse
from starlette.routing import Route

from . import auth, credits, dbx

PARTNER_NOTIFY_DAYS = 60
PARTNER_WELCOME_BONUS = 5          # on top of the 5 everyone gets: 10 in total
PARTNER_PRO_USD = 15.0
FOUNDER_SLOTS = 10
FOUNDER_MIN_BALANCE = 100.0
FOUNDER_MT5_DAYS = 30
DAY = 86400

SCHEMA = """
    CREATE TABLE IF NOT EXISTS partner_perks(user_id INTEGER PRIMARY KEY, granted_at BIGINT NOT NULL, founder_rank INTEGER,
        founder_status TEXT, founder_verified_at BIGINT, founder_balance DOUBLE PRECISION, last_balance DOUBLE PRECISION,
        last_seen_at BIGINT, note TEXT);
    CREATE UNIQUE INDEX IF NOT EXISTS ux_perks_rank ON partner_perks(founder_rank) WHERE founder_rank IS NOT NULL"""


def db():
    auth.db().close()
    return dbx.connect("perks", SCHEMA)


def _f(v, default=0.0) -> float:
    try:
        x = float(v)
        return x if x == x and abs(x) != float("inf") else default
    except (TypeError, ValueError):
        return default


def _confirmed(uid: int) -> bool:
    from . import partner
    with closing(partner.db()) as con:
        return bool(con.execute("SELECT 1 FROM partner_tags WHERE user_id=? AND status='confirmed'", (uid,)).fetchone())


def price_for(user: dict, product: str, base: float) -> float:
    """What this member pays for a product: Pro is $15 for a confirmed partner member."""
    if product == "pro" and not credits.grandfathered(user) and _confirmed(user["id"]):
        return PARTNER_PRO_USD
    return base


# ---------------------------------------------------------------- giving the perks (every step safe to repeat)
def _give_sub(uid: int, plan: str, days: int, now: int):
    with closing(credits.db()) as con:
        if not con.execute("SELECT 1 FROM billing_subs WHERE user_id=? AND plan=?", (uid, plan)).fetchone():
            con.execute("INSERT INTO billing_subs(user_id,plan,starts_at,ends_at,order_id) VALUES(?,?,?,?,NULL)",
                        (uid, plan, now, now + days * DAY))
            con.commit()


def _assign_slot(con, uid: int):
    row = con.execute("SELECT founder_rank, founder_status FROM partner_perks WHERE user_id=?", (uid,)).fetchone()
    if row["founder_status"] is not None:
        return
    used = {r["founder_rank"] for r in con.execute("SELECT founder_rank FROM partner_perks WHERE founder_rank IS NOT NULL").fetchall()}
    free = next((n for n in range(1, FOUNDER_SLOTS + 1) if n not in used), None)
    try:
        if free is None:
            con.execute("UPDATE partner_perks SET founder_status='none' WHERE user_id=? AND founder_status IS NULL", (uid,))
        else:
            con.execute("UPDATE partner_perks SET founder_rank=?, founder_status='pending' WHERE user_id=? AND founder_status IS NULL",
                        (free, uid))
        con.commit()
    except Exception as e:
        if not dbx.is_duplicate(e):          # someone took that slot a moment ago: the next sync tries again
            raise
        con.raw.rollback()


def sync(uid: int) -> bool:
    """Gives a confirmed partner member their perks. True when the member is a confirmed partner member."""
    with closing(auth.db()) as con:
        u = con.execute("SELECT tier FROM users WHERE id=?", (uid,)).fetchone()
    if not u or credits.grandfathered({"tier": u["tier"]}) or not _confirmed(uid):
        return False
    now = int(time.time())
    with closing(db()) as con:
        if not con.execute("SELECT 1 FROM partner_perks WHERE user_id=?", (uid,)).fetchone():
            try:
                con.execute("INSERT INTO partner_perks(user_id,granted_at) VALUES(?,?)", (uid, now))
                con.commit()
            except Exception as e:
                if not dbx.is_duplicate(e):
                    raise
                con.raw.rollback()
        _assign_slot(con, uid)
    with closing(credits.db()) as con:
        if credits._credit(con, uid, PARTNER_WELCOME_BONUS, "partner_welcome", "partner", "Partner member bonus signals"):
            con.commit()
    _give_sub(uid, "partner_notify", PARTNER_NOTIFY_DAYS, now)
    return True


def sync_all() -> int:
    """Run in the background every few minutes: perks for everyone Deriv has confirmed, in the order they signed up."""
    from . import partner
    with closing(partner.db()) as con:
        ids = [r["user_id"] for r in con.execute(
            "SELECT user_id, MIN(first_seen) AS f FROM partner_tags WHERE status='confirmed' AND user_id IS NOT NULL "
            "GROUP BY user_id ORDER BY f").fetchall()]
    n = 0
    for uid in ids:
        try:
            n += 1 if sync(uid) else 0
        except Exception as e:
            print(f"[perks] sync failed for one member: {type(e).__name__}", flush=True)
    return n


# ---------------------------------------------------------------- the founding-member check, run when the EA reports
def founder_pending(uid: int) -> bool:
    with closing(db()) as con:
        r = con.execute("SELECT founder_status FROM partner_perks WHERE user_id=?", (uid,)).fetchone()
    return bool(r and r["founder_status"] == "pending")


def verify_founder(uid: int, acc: dict):
    """acc is the account block the EA reported: login, server, currency, balance, demo."""
    verified = False
    with closing(db()) as con:
        r = con.execute("SELECT founder_status FROM partner_perks WHERE user_id=?", (uid,)).fetchone()
        if not r or r["founder_status"] != "pending":
            return
        now, bal = int(time.time()), _f(acc.get("balance"))
        live_deriv = (not acc.get("demo")) and "deriv" in str(acc.get("server") or "").lower() \
            and str(acc.get("currency") or "USD").upper() == "USD"
        con.execute("UPDATE partner_perks SET last_balance=?, last_seen_at=? WHERE user_id=?", (bal, now, uid))
        if live_deriv and bal >= FOUNDER_MIN_BALANCE:
            cur = con.execute("UPDATE partner_perks SET founder_status='verified', founder_verified_at=?, founder_balance=? "
                              "WHERE user_id=? AND founder_status='pending'", (now, bal, uid))
            verified = cur.rowcount == 1
        con.commit()
    if verified:                                   # outside the block above: one database write at a time
        _give_sub(uid, "founder_mt5", FOUNDER_MT5_DAYS, now)


# ---------------------------------------------------------------- what a member sees
def member_view(uid: int) -> dict:
    with closing(db()) as con:
        r = con.execute("SELECT * FROM partner_perks WHERE user_id=?", (uid,)).fetchone()
    if not r:
        return {"partner": False}
    with closing(credits.db()) as con:
        ends = {x["plan"]: x["ends_at"] for x in con.execute(
            "SELECT plan, ends_at FROM billing_subs WHERE user_id=? AND plan IN ('partner_notify','founder_mt5')", (uid,)).fetchall()}
    f = None
    if r["founder_status"] in ("pending", "verified"):
        f = {"status": r["founder_status"], "rank": r["founder_rank"], "min_balance": FOUNDER_MIN_BALANCE,
             "last_balance": r["last_balance"], "until": ends.get("founder_mt5")}
    return {"partner": True, "notify_until": ends.get("partner_notify"), "bonus": PARTNER_WELCOME_BONUS,
            "pro_usd": PARTNER_PRO_USD, "founder": f}


# ---------------------------------------------------------------- admin
async def admin_view(request):
    auth.require_admin(request)
    with closing(db()) as con:
        rows = [dict(x) for x in con.execute("SELECT * FROM partner_perks ORDER BY granted_at").fetchall()]
    with closing(auth.db()) as con:
        mails = {x["id"]: x["email"] for x in con.execute("SELECT id,email FROM users").fetchall()}
    out = [{"user_id": r["user_id"], "email": mails.get(r["user_id"]), "rank": r["founder_rank"], "status": r["founder_status"],
            "last_balance": r["last_balance"], "verified_at": r["founder_verified_at"], "granted_at": r["granted_at"]} for r in rows]
    taken = sum(1 for r in out if r["status"] in ("pending", "verified"))
    return JSONResponse({"slots": FOUNDER_SLOTS, "taken": taken, "verified": sum(1 for r in out if r["status"] == "verified"),
                         "partners": len(out), "members": out[:100], "min_balance": FOUNDER_MIN_BALANCE})


async def admin_sync(request):
    auth.require_admin(request)
    return JSONResponse({"synced": sync_all()})


async def admin_release(request):
    """Frees a founder slot (for example one that never connected). The member keeps the ordinary partner perks."""
    auth.require_admin(request)
    d = await auth._json(request)
    try:
        uid = int(d.get("user_id"))
    except (TypeError, ValueError):
        raise HTTPException(422, "Invalid member")
    with closing(db()) as con:
        cur = con.execute("UPDATE partner_perks SET founder_status='released', founder_rank=NULL "
                          "WHERE user_id=? AND founder_status='pending'", (uid,))
        con.commit()
    if cur.rowcount != 1:
        raise HTTPException(409, "Only a slot that is still waiting for verification can be released")
    return JSONResponse({"ok": True})


routes = [
    Route("/api/admin/perks", admin_view, methods=["GET"]),
    Route("/api/admin/perks/sync", admin_sync, methods=["POST"]),
    Route("/api/admin/perks/release", admin_release, methods=["POST"]),
]
