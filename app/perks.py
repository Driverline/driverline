"""Partner-member perks and the founding-member offer (perks v2).

Who counts as a partner member: a TradeLens member whose Deriv account Deriv's partner records CONFIRM as tagged to you (the
"confirmed" status in Admin > Partner tags). A login alone proves nothing, so nothing here is given on the strength of a login.

Free signals
  * Every new member gets 3 welcome signals (credits.WELCOME).
  * A partner member gets 5 free signals IN TOTAL, shown as "Partner member bonus signals". It is topped up from whatever they already
    hold: 3 welcome + 2 bonus, or 5 bonus when they were confirmed before any welcome. Never 3 + 5. A member who already received more
    than 5 before this change keeps it: nothing is ever taken back.
  * Signals Pro (the MT5 connection) costs $15 instead of $30 for a partner member.
  * Partner members no longer get free notifications. The free 60-day "partner_notify" plans already given keep working until they end.

Founding members (3 places)
  * The first 3 partner members to FUND an MT5 account get notifications and the MT5 connection free for 30 days.
  * Funded means: a LIVE Deriv MT5 account in USD that shows a balance of at least $100 when the EA reports. The first three to show it
    win, in the order they fund, not the order they sign up. A waiting member holds no place and no rank.
  * Until verified a waiting member may connect so the balance can be read, and nothing else about MT5 is unlocked.
  * One funded MT5 login can verify one member only.
  * The balance proves the account holds the money, not where it came from or that it stays there. Admin can see the login and balance.

Everything takes effect only while billing is switched on, exactly like paid plans.
"""
import time
from contextlib import closing

from starlette.exceptions import HTTPException
from starlette.responses import JSONResponse
from starlette.routing import Route

from . import auth, credits, dbx

PARTNER_SIGNALS = 5                # free signals a partner member holds in total (welcome included)
PARTNER_PRO_USD = 15.0
FOUNDER_SLOTS = 3
FOUNDER_MIN_BALANCE = 100.0
FOUNDER_MT5_DAYS = 30
DAY = 86400

SCHEMA = """
    CREATE TABLE IF NOT EXISTS partner_perks(user_id INTEGER PRIMARY KEY, granted_at BIGINT NOT NULL, founder_rank INTEGER,
        founder_status TEXT, founder_verified_at BIGINT, founder_balance DOUBLE PRECISION, last_balance DOUBLE PRECISION,
        last_seen_at BIGINT, note TEXT, founder_login TEXT);
    CREATE UNIQUE INDEX IF NOT EXISTS ux_perks_rank ON partner_perks(founder_rank) WHERE founder_rank IS NOT NULL"""


def _migrate(con):
    """Mirrored by migrations/2026_11_perks_v2.sql. Additive and safe to repeat; no signal, ledger row, balance or plan is touched."""
    con.add_column("partner_perks", "founder_login", "TEXT")      # the funded MT5 login that verified this founder
    con.raw.cursor().execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_perks_login ON partner_perks(founder_login) WHERE founder_login IS NOT NULL")
    # Places used to be reserved at confirmation (ranks 1..10). They are now claimed by funding, so a member still waiting holds none.
    con.raw.cursor().execute("UPDATE partner_perks SET founder_rank=NULL WHERE founder_status='pending'")


def db():
    auth.db().close()
    return dbx.connect("perks", SCHEMA, _migrate)


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


def _verified(con) -> int:
    return con.execute("SELECT COUNT(*) AS c FROM partner_perks WHERE founder_status='verified'").fetchone()["c"]


# ---------------------------------------------------------------- giving the perks (every step safe to repeat)
def _give_sub(uid: int, plan: str, days: int, now: int):
    with closing(credits.db()) as con:
        if not con.execute("SELECT 1 FROM billing_subs WHERE user_id=? AND plan=?", (uid, plan)).fetchone():
            con.execute("INSERT INTO billing_subs(user_id,plan,starts_at,ends_at,order_id) VALUES(?,?,?,?,NULL)",
                        (uid, plan, now, now + days * DAY))
            con.commit()


def _assign_status(con, uid: int):
    """A new partner member waits to fund; if all the places are already taken they are told so (closed) instead of waiting for nothing."""
    row = con.execute("SELECT founder_status FROM partner_perks WHERE user_id=?", (uid,)).fetchone()
    if row["founder_status"] is not None:
        return
    status = "pending" if _verified(con) < FOUNDER_SLOTS else "closed"
    con.execute("UPDATE partner_perks SET founder_status=? WHERE user_id=? AND founder_status IS NULL", (status, uid))
    con.commit()


def _top_up_signals(uid: int):
    """Brings a partner member to PARTNER_SIGNALS free signals in total, from what the ledger says they were given. Adds nothing
    (and writes no row) when they already hold that many or more. Spent signals do not matter: only what was given counts."""
    with credits.GRANT_LOCK, closing(credits.db()) as con:
        given = con.execute("SELECT COALESCE(SUM(delta),0) AS s FROM credit_ledger WHERE user_id=? AND kind IN ('welcome','partner_welcome')",
                            (uid,)).fetchone()["s"]
        extra = PARTNER_SIGNALS - int(given)
        if extra > 0 and credits._credit(con, uid, extra, "partner_welcome", "partner", "Partner member bonus signals"):
            con.commit()


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
        _assign_status(con, uid)
    _top_up_signals(uid)
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
    """True while this member may still connect so their balance can be read: waiting, and a place is still free."""
    with closing(db()) as con:
        r = con.execute("SELECT founder_status FROM partner_perks WHERE user_id=?", (uid,)).fetchone()
        return bool(r and r["founder_status"] == "pending" and _verified(con) < FOUNDER_SLOTS)


def _claim_place(con, uid: int, login: str, bal: float, now: int) -> bool:
    """Takes the lowest free place for this member. The unique indexes on the rank and on the login decide any race: whoever the
    database accepts first gets the place, the other simply tries the next one. Does not commit on success."""
    for _ in range(FOUNDER_SLOTS + 2):
        if con.execute("SELECT 1 FROM partner_perks WHERE founder_login=? AND user_id<>?", (login, uid)).fetchone():
            return False                           # this funded MT5 account already verified another member
        used = {r["founder_rank"] for r in con.execute("SELECT founder_rank FROM partner_perks WHERE founder_rank IS NOT NULL").fetchall()}
        free = next((n for n in range(1, FOUNDER_SLOTS + 1) if n not in used), None)
        if free is None:                           # too late: the places went to members who funded first
            con.execute("UPDATE partner_perks SET founder_status='closed' WHERE user_id=? AND founder_status='pending'", (uid,))
            return False
        try:
            cur = con.execute("UPDATE partner_perks SET founder_status='verified', founder_rank=?, founder_login=?, founder_verified_at=?, "
                              "founder_balance=? WHERE user_id=? AND founder_status='pending'", (free, login, now, bal, uid))
        except Exception as e:
            if not dbx.is_duplicate(e):
                raise
            con.raw.rollback()                     # a place or this login was taken a moment ago: look again
            continue
        if cur.rowcount != 1:
            return False
        if _verified(con) >= FOUNDER_SLOTS:        # that was the last place: everyone still waiting is told it is gone
            con.execute("UPDATE partner_perks SET founder_status='closed' WHERE founder_status='pending'")
        return True
    return False


def verify_founder(uid: int, acc: dict):
    """acc is the account block the EA reported: login, server, currency, balance, demo."""
    login = str(acc.get("login") or "").strip()[:40]
    verified = False
    with closing(db()) as con:
        r = con.execute("SELECT founder_status FROM partner_perks WHERE user_id=?", (uid,)).fetchone()
        if not r or r["founder_status"] != "pending":
            return
        now, bal = int(time.time()), _f(acc.get("balance"))
        live_deriv = (not acc.get("demo")) and "deriv" in str(acc.get("server") or "").lower() \
            and str(acc.get("currency") or "USD").upper() == "USD"
        con.execute("UPDATE partner_perks SET last_balance=?, last_seen_at=? WHERE user_id=?", (bal, now, uid))
        con.commit()
        if live_deriv and bal >= FOUNDER_MIN_BALANCE and login:
            verified = _claim_place(con, uid, login, bal, now)
            con.commit()
    if verified:                                   # outside the block above: one database write at a time
        _give_sub(uid, "founder_mt5", FOUNDER_MT5_DAYS, now)


# ---------------------------------------------------------------- what a member sees
def member_view(uid: int) -> dict:
    with closing(db()) as con:
        r = con.execute("SELECT * FROM partner_perks WHERE user_id=?", (uid,)).fetchone()
        left = max(0, FOUNDER_SLOTS - _verified(con))
    if not r:
        return {"partner": False}
    with closing(credits.db()) as con:
        ends = {x["plan"]: x["ends_at"] for x in con.execute(
            "SELECT plan, ends_at FROM billing_subs WHERE user_id=? AND plan IN ('partner_notify','founder_mt5')", (uid,)).fetchall()}
    f = None
    if r["founder_status"] in ("pending", "verified", "closed"):
        f = {"status": r["founder_status"], "rank": r["founder_rank"], "min_balance": FOUNDER_MIN_BALANCE, "slots": FOUNDER_SLOTS,
             "slots_left": left, "last_balance": r["last_balance"], "until": ends.get("founder_mt5")}
    return {"partner": True, "signals": PARTNER_SIGNALS, "notify_until": ends.get("partner_notify"),
            "pro_usd": PARTNER_PRO_USD, "founder": f}


# ---------------------------------------------------------------- admin
async def admin_view(request):
    auth.require_admin(request)
    with closing(db()) as con:
        rows = [dict(x) for x in con.execute("SELECT * FROM partner_perks ORDER BY granted_at").fetchall()]
    with closing(auth.db()) as con:
        mails = {x["id"]: x["email"] for x in con.execute("SELECT id,email FROM users").fetchall()}
    out = [{"user_id": r["user_id"], "email": mails.get(r["user_id"]), "rank": r["founder_rank"], "status": r["founder_status"],
            "login": r.get("founder_login"), "last_balance": r["last_balance"], "verified_at": r["founder_verified_at"],
            "granted_at": r["granted_at"]} for r in rows]
    verified = sum(1 for r in out if r["status"] == "verified")
    return JSONResponse({"slots": FOUNDER_SLOTS, "verified": verified, "slots_left": max(0, FOUNDER_SLOTS - verified),
                         "partners": len(out), "members": out[:100], "min_balance": FOUNDER_MIN_BALANCE,
                         "signals": PARTNER_SIGNALS})


async def admin_sync(request):
    auth.require_admin(request)
    return JSONResponse({"synced": sync_all()})


async def admin_release(request):
    """Stops a member who is still waiting from claiming a founder place (for example one that looks wrong). They keep the
    ordinary partner perks. It cannot be used on a member who is already verified."""
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
        raise HTTPException(409, "Only a member who is still waiting to fund can be released")
    return JSONResponse({"ok": True})


routes = [
    Route("/api/admin/perks", admin_view, methods=["GET"]),
    Route("/api/admin/perks/sync", admin_sync, methods=["POST"]),
    Route("/api/admin/perks/release", admin_release, methods=["POST"]),
]
