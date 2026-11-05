"""Referrals, points and levels (Phase 1). Money rewards (free signals, free connections, commission, prizes) come in later phases.

  * Referral: every member has a permanent code and link (/login.html?rc=CODE). static/track.js keeps the code in a cookie for 30 days
    (first touch wins) and signup attaches it to the new member. One referrer per member, never changeable, no self-referral. A
    referral QUALIFIES once the referred member has a Deriv account linked, which is what pays the referrer.
  * Points: one ledger (points_ledger, unique per member + kind + ref, so a retry can never award twice). Points are collected by
    sweeps that READ what the app already records (usage, journal, Deriv trades, orders, MT5 accounts, copy members), so the payment,
    trade and login flows are never touched or slowed. A sweep runs when a member opens the app (at most every 10 minutes), and when
    a referrer opens the Rewards page. Only activity from the day Rewards was switched on counts (rewards_meta.start).
  * Level = LIFETIME points (never goes down). Spendable points are kept apart for prizes (Phase 3), so redeeming never lowers a level.
  * Phase 2 (bonus rewards) only runs when ALSO REWARDS_BONUS_ON=1, so points can go live before anything of value is given away:
      - free signals: 5% of the signals of a Signal Vault / Vault Lite a qualified referral buys (credited through the credits ledger)
      - a free 30-day MT5 connection (+ notifications + copy trading) for every 3 DIFFERENT qualified referrals who bought a plan
        that includes MT5 or Copy trading. Both are granted when the purchase is confirmed (no hold: the terms say no refunds).
  * Everything is OFF unless REWARDS_ON=1. Tables are new and additive (migrations/2026_11_rewards.sql); nothing existing changes.
Days are UTC days. Failures inside a sweep are logged by type only and never reach the member."""
import asyncio
import json
import os
import re
import secrets
import time
from contextlib import closing
from datetime import datetime, timedelta, timezone

from starlette.exceptions import HTTPException
from starlette.responses import JSONResponse
from starlette.routing import Route

from . import auth, dbx

SCHEMA = """
    CREATE TABLE IF NOT EXISTS ref_codes(user_id INTEGER PRIMARY KEY, code TEXT UNIQUE NOT NULL, created_at BIGINT NOT NULL);
    CREATE TABLE IF NOT EXISTS referrals(referred_id INTEGER PRIMARY KEY, referrer_id INTEGER NOT NULL, code TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending', created_at BIGINT NOT NULL, qualified_at BIGINT);
    CREATE INDEX IF NOT EXISTS ix_referrals_referrer ON referrals(referrer_id);
    CREATE TABLE IF NOT EXISTS points_ledger(id {ID}, user_id INTEGER NOT NULL, delta INTEGER NOT NULL, lifetime INTEGER NOT NULL,
        kind TEXT NOT NULL, ref TEXT NOT NULL, day TEXT, note TEXT, admin_id INTEGER, created_at BIGINT NOT NULL,
        UNIQUE(user_id, kind, ref));
    CREATE INDEX IF NOT EXISTS ix_points_user ON points_ledger(user_id, id);
    CREATE INDEX IF NOT EXISTS ix_points_day ON points_ledger(user_id, kind, day);
    CREATE TABLE IF NOT EXISTS points_balances(user_id INTEGER PRIMARY KEY, spendable INTEGER NOT NULL DEFAULT 0,
        lifetime INTEGER NOT NULL DEFAULT 0);
    CREATE TABLE IF NOT EXISTS rewards_meta(key TEXT PRIMARY KEY, value TEXT NOT NULL)"""

# ------------------------------------------------------------------ the rules (approved by the owner, Nov 2026)
LEVELS = [("Bronze", 0), ("Silver", 1000), ("Gold", 5000), ("Diamond", 20000), ("Platinum", 60000)]
POINTS = {"daily": 2, "streak7": 10, "streak30": 50, "analysis": 1, "trade_live": 3, "trade_demo": 1, "mt5_conn": 50,
          "copy_first": 100, "copy_month": 20, "ref_qualify": 50, "ref_first_buy": 100}
PER_USD = 10               # points per USD of a confirmed purchase
REF_PER_USD = 2            # referrer's points per USD of a referral's later purchases
ANALYSIS_DAILY_CAP = 5
TRADE_DAILY_CAP = 5
MT5_ACCOUNT_CAP = 2
SIGNAL_SHARE = 0.05        # of the signals in a Signal Vault / Vault Lite bought by a qualified referral
SIGNAL_PRODUCTS = ("vault", "vaultlite")
FREE_EVERY = 3             # one free connection per this many different referrals who bought an MT5 / Copy trading plan
FREE_DAYS = 30
BAD_PAYMENT = ("refunded", "failed", "partially_paid")
THROTTLE = 600             # seconds between automatic sweeps of one member
RULES = [
    {"what": "Open the app each day", "points": "2 (+10 at a 7-day streak, +50 at 30 days)"},
    {"what": "AI analysis done", "points": "1 each, up to 5 a day"},
    {"what": "Trade closed or placed", "points": "3 on a live account, 1 on demo; up to 5 trades a day"},
    {"what": "Connect an MT5 account", "points": "50 per account, up to 2"},
    {"what": "Start Copy Trading", "points": "100 once, then 20 each month it is active"},
    {"what": "Buy a bundle or subscription", "points": "10 per USD paid"},
    {"what": "A friend you referred links their Deriv account", "points": "50"},
    {"what": "That friend's first purchase", "points": "100"},
    {"what": "That friend's later purchases", "points": "2 per USD"},
]
CODE_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"
CODE_RE = re.compile(r"^[a-z0-9]{6,12}$")
COOKIE = "dl_rc"

_last: dict = {}


def enabled() -> bool:
    return os.environ.get("REWARDS_ON", "").strip() == "1"


def bonus_enabled() -> bool:
    """Phase 2: real rewards (signals, free connections). Needs REWARDS_ON=1 too."""
    return enabled() and os.environ.get("REWARDS_BONUS_ON", "").strip() == "1"


def db():
    auth.db().close()
    return dbx.connect("rewards", SCHEMA)


def _now() -> int:
    return int(time.time())


def _day(ts: float = None) -> str:
    return time.strftime("%Y-%m-%d", time.gmtime(_now() if ts is None else ts))


def _prev(day: str) -> str:
    return (datetime.strptime(day, "%Y-%m-%d") - timedelta(days=1)).strftime("%Y-%m-%d")


def start() -> int:
    """When Rewards was first used. Only activity after this moment earns points."""
    with closing(db()) as con:
        r = con.execute("SELECT value FROM rewards_meta WHERE key='start'").fetchone()
        if r:
            return int(r["value"])
        con.execute("INSERT INTO rewards_meta(key,value) VALUES('start',?) ON CONFLICT(key) DO NOTHING", (str(_now()),))
        con.commit()
        return int(con.execute("SELECT value FROM rewards_meta WHERE key='start'").fetchone()["value"])


# ------------------------------------------------------------------ levels
def level_for(lifetime: int) -> dict:
    lifetime = max(0, int(lifetime or 0))
    idx = max(i for i, (_, at) in enumerate(LEVELS) if lifetime >= at)
    name, at = LEVELS[idx]
    nxt = LEVELS[idx + 1] if idx + 1 < len(LEVELS) else None
    pct = 100 if not nxt else int(100 * (lifetime - at) / (nxt[1] - at))
    return {"name": name, "next": nxt[0] if nxt else None, "next_at": nxt[1] if nxt else None, "percent": min(100, pct)}


def mask(email: str) -> str:
    local = str(email or "").split("@")[0]
    return (local[:2] or "?") + "***"


# ------------------------------------------------------------------ the ledger
def award(con, uid: int, kind: str, ref: str, pts: int, note: str = "", day: str = None, admin_id=None) -> bool:
    """Adds one ledger row and moves the balances. False if (member, kind, ref) was already awarded. Commits by itself, so a
    later failure in a sweep cannot undo an earlier award."""
    pts = int(pts)
    if not pts:
        return False
    if con.execute("SELECT 1 FROM points_ledger WHERE user_id=? AND kind=? AND ref=?", (uid, kind, ref)).fetchone():
        return False
    try:
        con.execute("INSERT INTO points_ledger(user_id,delta,lifetime,kind,ref,day,note,admin_id,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (uid, pts, pts, kind, ref, day, (note or "")[:200], admin_id, _now()))
    except Exception as e:
        if dbx.is_duplicate(e):                  # another sweep won the race
            con.raw.rollback()
            return False
        raise
    con.execute("INSERT INTO points_balances(user_id,spendable,lifetime) VALUES(?,?,?) ON CONFLICT(user_id) DO UPDATE SET "
                "spendable=points_balances.spendable+excluded.spendable, lifetime=points_balances.lifetime+excluded.lifetime",
                (uid, pts, pts))
    con.commit()
    return True


def balances(uid: int) -> dict:
    with closing(db()) as con:
        r = con.execute("SELECT spendable, lifetime FROM points_balances WHERE user_id=?", (uid,)).fetchone()
    return {"spendable": int(r["spendable"]) if r else 0, "lifetime": int(r["lifetime"]) if r else 0}


# ------------------------------------------------------------------ referral codes
def code_for(uid: int) -> str:
    with closing(db()) as con:
        r = con.execute("SELECT code FROM ref_codes WHERE user_id=?", (uid,)).fetchone()
        if r:
            return r["code"]
        for _ in range(20):
            code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(8))
            try:
                con.execute("INSERT INTO ref_codes(user_id,code,created_at) VALUES(?,?,?)", (uid, code, _now()))
                con.commit()
                return code
            except Exception as e:
                if not dbx.is_duplicate(e):
                    raise
                con.raw.rollback()
                r = con.execute("SELECT code FROM ref_codes WHERE user_id=?", (uid,)).fetchone()
                if r:
                    return r["code"]
    raise RuntimeError("could not make a referral code")


def on_signup(uid: int, request):
    """Attach the referrer named by the visitor's cookie to this new member (first touch, never changed)."""
    if not enabled():
        return
    code = str(request.cookies.get(COOKIE, "") or "").strip().lower()
    if not CODE_RE.match(code):
        return
    with closing(db()) as con:
        r = con.execute("SELECT user_id FROM ref_codes WHERE code=?", (code,)).fetchone()
        if not r or r["user_id"] == uid:
            return
        con.execute("INSERT INTO referrals(referred_id,referrer_id,code,status,created_at) VALUES(?,?,?,'pending',?) "
                    "ON CONFLICT(referred_id) DO NOTHING", (uid, r["user_id"], code, _now()))
        con.commit()


# ------------------------------------------------------------------ reading what the app already records
def _guard(name, fn, *a):
    try:
        return fn(*a)
    except Exception as e:
        print(f"[rewards] {name}: {type(e).__name__}", flush=True)


def _live_logins(uid: int) -> set:
    """MT5 logins the EA last reported as a LIVE account. Anything not clearly live counts as demo."""
    from . import ea
    out = set()
    with closing(ea.db()) as con:
        rows = con.execute("SELECT a.login, s.state FROM ea_accounts a LEFT JOIN ea_acct_state s ON s.account_id=a.id "
                           "WHERE a.user_id=?", (uid,)).fetchall()
    for r in rows:
        try:
            acc = (json.loads(r["state"]) or {}).get("account") or {}
            if acc.get("demo") is not None and not acc["demo"] and r["login"]:
                out.add(str(r["login"]).strip())
        except (TypeError, ValueError, AttributeError):
            pass
    return out


def _qualify(uid: int):
    """If this member was referred and now has a Deriv account linked, the referral counts and the referrer is paid."""
    from . import deriv_login
    with closing(db()) as con:
        ref = con.execute("SELECT referrer_id FROM referrals WHERE referred_id=? AND status='pending'", (uid,)).fetchone()
        if not ref:
            return
        with closing(deriv_login.db()) as d:
            linked = d.execute("SELECT 1 FROM deriv_accounts WHERE user_id=?", (uid,)).fetchone()
        if not linked:
            return
        cur = con.execute("UPDATE referrals SET status='qualified', qualified_at=? WHERE referred_id=? AND status='pending'", (_now(), uid))
        con.commit()
        if cur.rowcount == 1:
            award(con, ref["referrer_id"], "ref_qualify", f"u{uid}", POINTS["ref_qualify"], "A referral linked their Deriv account")


def _analyses(uid: int, st: int):
    with closing(auth.db()) as a:
        rows = a.execute("SELECT day, n FROM usage WHERE user_id=? AND day>=?", (uid, _day(st))).fetchall()
    with closing(db()) as con:
        for r in rows:
            for i in range(1, min(int(r["n"] or 0), ANALYSIS_DAILY_CAP) + 1):
                award(con, uid, "analysis", f"{r['day']}:{i}", POINTS["analysis"], "AI analysis", r["day"])


def _trade_points(con, uid: int, ref: str, day: str, live: bool):
    if con.execute("SELECT COUNT(*) AS n FROM points_ledger WHERE user_id=? AND kind='trade' AND day=?", (uid, day)).fetchone()["n"] >= TRADE_DAILY_CAP:
        return
    award(con, uid, "trade", ref, POINTS["trade_live" if live else "trade_demo"], "Live trade" if live else "Demo trade", day)


def _trades(uid: int, st: int):
    from . import deriv_trade, journal
    live = _live_logins(uid)
    iso = datetime.fromtimestamp(st, timezone.utc).isoformat(timespec="seconds")
    with closing(journal.db()) as j:
        mt5 = j.execute("SELECT mt5_id, closed_at FROM trades WHERE user_id=? AND status='closed' AND mt5_id IS NOT NULL AND closed_at>=? "
                        "ORDER BY closed_at", (uid, iso)).fetchall()
    with closing(deriv_trade.db()) as d:
        dv = d.execute("SELECT id, account_type, created_at FROM deriv_trades WHERE user_id=? AND status='placed' AND created_at>=? "
                       "ORDER BY created_at", (uid, st)).fetchall()
    events = [(str(r["closed_at"])[:10], f"m:{r['mt5_id']}", str(r["mt5_id"]).split(":", 1)[0] in live, str(r["closed_at"])) for r in mt5]
    events += [(_day(r["created_at"]), f"d:{r['id']}", str(r["account_type"] or "").lower() == "real", f"{_day(r['created_at'])}T00") for r in dv]
    events.sort(key=lambda e: e[3])
    with closing(db()) as con:
        for day, ref, is_live, _ in events:
            _trade_points(con, uid, ref, day, is_live)


def _orders(uid: int, st: int):
    """Own purchase points, and the referrer's points from this member's purchases (only once the referral qualified)."""
    from . import credits
    with closing(credits.db()) as c:
        rows = c.execute("SELECT id, amount_usd, provider_status, COALESCE(fulfilled_at, decided_at, created_at) AS t FROM billing_orders "
                         "WHERE user_id=? AND status='approved' ORDER BY COALESCE(fulfilled_at, decided_at, created_at), id", (uid,)).fetchall()
    with closing(db()) as con:
        ref = con.execute("SELECT referrer_id, status FROM referrals WHERE referred_id=?", (uid,)).fetchone()
        first = True
        for r in rows:
            if int(r["t"] or 0) < st:
                continue
            usd = float(r["amount_usd"] or 0)
            pts, ref_id, day = int(round(usd * PER_USD)), f"order:{r['id']}", _day(r["t"])
            if r["provider_status"] in ("refunded", "failed", "partially_paid"):         # money did not stay: take the points back once
                if con.execute("SELECT 1 FROM points_ledger WHERE user_id=? AND kind='purchase' AND ref=?", (uid, ref_id)).fetchone():
                    _reverse(con, uid, "purchase", ref_id)
                    if ref:
                        _reverse(con, ref["referrer_id"], "ref_buy", ref_id)
                        if first:
                            _reverse(con, ref["referrer_id"], "ref_first_buy", f"u{uid}")
                continue
            award(con, uid, "purchase", ref_id, pts, f"Purchase ${usd:g}", day)
            if ref and ref["status"] == "qualified":
                if first:
                    award(con, ref["referrer_id"], "ref_first_buy", f"u{uid}", POINTS["ref_first_buy"], "A referral's first purchase", day)
                else:
                    award(con, ref["referrer_id"], "ref_buy", ref_id, int(round(usd * REF_PER_USD)), "A referral's purchase", day)
            first = False


def _reverse(con, uid: int, kind: str, ref: str):
    row = con.execute("SELECT delta FROM points_ledger WHERE user_id=? AND kind=? AND ref=?", (uid, kind, ref)).fetchone()
    if not row or not row["delta"]:
        return
    if con.execute("SELECT 1 FROM points_ledger WHERE user_id=? AND kind=? AND ref=?", (uid, kind + "_rev", ref)).fetchone():
        return
    try:
        con.execute("INSERT INTO points_ledger(user_id,delta,lifetime,kind,ref,day,note,created_at) VALUES(?,?,?,?,?,?,?,?)",
                    (uid, -row["delta"], -row["delta"], kind + "_rev", ref, None, "Payment was refunded or failed", _now()))
        con.execute("UPDATE points_balances SET spendable=spendable-?, lifetime=lifetime-? WHERE user_id=?", (row["delta"], row["delta"], uid))
        con.commit()
    except Exception as e:
        if not dbx.is_duplicate(e):
            raise
        con.raw.rollback()


def _mt5_and_copy(uid: int):
    from . import credits, ea
    now = _now()
    with closing(ea.db()) as e:
        accts = e.execute("SELECT login FROM ea_accounts WHERE user_id=? AND login IS NOT NULL AND login<>'' ORDER BY id", (uid,)).fetchall()
        copy_on = bool(e.execute("SELECT 1 FROM copy_members WHERE user_id=? AND enabled=1", (uid,)).fetchone())
        copy_sub = e.execute("SELECT ends_at FROM copy_subs WHERE user_id=?", (uid,)).fetchone()
    with closing(credits.db()) as c:
        plans = [r["plan"] for r in c.execute("SELECT plan FROM billing_subs WHERE user_id=? AND ends_at>?", (uid, now)).fetchall()]
    plan_ok = any((credits.CATALOG.get(p) or {}).get("copy") for p in plans) or bool(copy_sub and int(copy_sub["ends_at"] or 0) > now)
    with closing(db()) as con:
        for r in accts[:MT5_ACCOUNT_CAP]:
            login = str(r["login"]).strip()
            if not con.execute("SELECT 1 FROM points_ledger WHERE kind='mt5_conn' AND ref=? AND user_id<>?", (login, uid)).fetchone():
                award(con, uid, "mt5_conn", login, POINTS["mt5_conn"], "MT5 account connected")
        if copy_on:
            award(con, uid, "copy_first", "once", POINTS["copy_first"], "Copy Trading started")
            if plan_ok:
                award(con, uid, "copy_month", time.strftime("%Y-%m", time.gmtime(now)), POINTS["copy_month"], "Copy Trading active this month")


def sweep_user(uid: int):
    """Collect every point this member has earned from what the app already recorded. Safe to run any number of times."""
    if not enabled():
        return
    st = start()
    _guard("qualify", _qualify, uid)
    _guard("analyses", _analyses, uid, st)
    _guard("trades", _trades, uid, st)
    _guard("orders", _orders, uid, st)
    _guard("mt5", _mt5_and_copy, uid)
    _guard("bonus", _bonus_for_referrer_of, uid)


# ------------------------------------------------------------------ Phase 2: signals and free connections for the referrer
def _qualifying_orders(referrer: int, st: int) -> list:
    """Approved, still-paid orders of the referrer's QUALIFIED referrals, oldest first."""
    from . import credits
    with closing(db()) as con:
        ids = [r["referred_id"] for r in con.execute("SELECT referred_id FROM referrals WHERE referrer_id=? AND status='qualified'", (referrer,)).fetchall()]
    if not ids:
        return []
    with closing(credits.db()) as c:
        rows = c.execute(f"SELECT id, user_id, product, provider_status, COALESCE(fulfilled_at, decided_at, created_at) AS t FROM billing_orders "
                         f"WHERE status='approved' AND user_id IN ({','.join('?' * len(ids))}) "
                         f"ORDER BY COALESCE(fulfilled_at, decided_at, created_at), id", ids).fetchall()
    return [r for r in rows if int(r["t"] or 0) >= st and r["provider_status"] not in BAD_PAYMENT]


def _grant_free_mt5(uid: int, k: int, now: int) -> int:
    """Reward number k: 30 days of MT5 connection + notifications (a billing_subs row marked with order_id -k, so repeating is harmless),
    and copy trading for the same days (copy_subs never shortened). Returns when it ends."""
    from . import credits, ea
    with closing(credits.db()) as c:
        row = c.execute("SELECT ends_at FROM billing_subs WHERE user_id=? AND plan='ref_mt5' AND order_id=?", (uid, -k)).fetchone()
        if row:
            ends = int(row["ends_at"])
        else:
            last = c.execute("SELECT MAX(ends_at) AS e FROM billing_subs WHERE user_id=? AND plan='ref_mt5' AND ends_at>?", (uid, now)).fetchone()["e"]
            begin = max(now, int(last or 0))
            ends = begin + FREE_DAYS * 86400
            c.execute("INSERT INTO billing_subs(user_id,plan,starts_at,ends_at,order_id) VALUES(?,?,?,?,?)", (uid, "ref_mt5", begin, ends, -k))
            c.commit()
    with closing(ea.db()) as e:
        e.execute("INSERT INTO copy_subs(user_id,ends_at,note,admin_id,updated_at) VALUES(?,?,?,NULL,?) ON CONFLICT(user_id) DO UPDATE SET "
                  "ends_at=CASE WHEN copy_subs.ends_at>excluded.ends_at THEN copy_subs.ends_at ELSE excluded.ends_at END, updated_at=excluded.updated_at",
                  (uid, ends, "Referral reward", now))
        e.commit()
    return ends


def sweep_referrer(referrer: int):
    """Free signals and free connections the referrer has earned from their referrals' purchases. Safe to repeat."""
    if not bonus_enabled():
        return
    from . import credits
    now, orders = _now(), _qualifying_orders(referrer, start())
    with closing(credits.db()) as c:
        for o in orders:
            if o["product"] in SIGNAL_PRODUCTS and o["product"] in credits.CATALOG:
                n = int(round(credits.CATALOG[o["product"]]["signals"] * SIGNAL_SHARE))
                if n > 0 and credits._credit(c, referrer, n, "ref_signals", f"order:{o['id']}",
                                             f"Referral reward: {int(SIGNAL_SHARE * 100)}% of a {credits.CATALOG[o['product']]['label']}"):
                    c.commit()
    buyers = {o["user_id"] for o in orders if (credits.CATALOG.get(o["product"]) or {}).get("mt5") or (credits.CATALOG.get(o["product"]) or {}).get("copy")}
    for k in range(1, len(buyers) // FREE_EVERY + 1):
        _grant_free_mt5(referrer, k, now)


def _bonus_for_referrer_of(uid: int):
    with closing(db()) as con:
        r = con.execute("SELECT referrer_id FROM referrals WHERE referred_id=? AND status='qualified'", (uid,)).fetchone()
    if r:
        sweep_referrer(r["referrer_id"])


def bonus_view(uid: int) -> dict:
    from . import credits
    now = _now()
    orders = _qualifying_orders(uid, start()) if bonus_enabled() else []
    buyers = {o["user_id"] for o in orders if (credits.CATALOG.get(o["product"]) or {}).get("mt5") or (credits.CATALOG.get(o["product"]) or {}).get("copy")}
    with closing(credits.db()) as c:
        ends = c.execute("SELECT MAX(ends_at) AS e FROM billing_subs WHERE user_id=? AND plan='ref_mt5' AND ends_at>?", (uid, now)).fetchone()["e"]
        sig = c.execute("SELECT COALESCE(SUM(delta),0) AS s FROM credit_ledger WHERE user_id=? AND kind='ref_signals'", (uid,)).fetchone()["s"]
    return {"on": bonus_enabled(), "buyers": len(buyers), "free_earned": len(buyers) // FREE_EVERY, "next_in": FREE_EVERY - len(buyers) % FREE_EVERY,
            "every": FREE_EVERY, "days": FREE_DAYS, "free_until": int(ends) if ends else None, "signals_earned": int(sig or 0),
            "signal_percent": int(SIGNAL_SHARE * 100)}


def _streak(con, uid: int, today: str):
    days = {r["ref"] for r in con.execute("SELECT ref FROM points_ledger WHERE user_id=? AND kind='daily' ORDER BY id DESC LIMIT 400", (uid,)).fetchall()}
    n, d = 0, today
    while d in days:
        n, d = n + 1, _prev(d)
    return n


def touch(uid: int, force: bool = False):
    """The member opened the app: today's visit and streak, then a sweep. Throttled so busy pages cost nothing."""
    if not enabled():
        return
    now = time.time()
    if not force and now - _last.get(uid, 0) < THROTTLE:
        return
    _last[uid] = now
    if len(_last) > 20000:
        _last.clear()
    today = _day()
    try:
        with closing(db()) as con:
            award(con, uid, "daily", today, POINTS["daily"], "Daily visit", today)
            n = _streak(con, uid, today)
            if n >= 7:
                start_day = today
                for _ in range(n - 1):
                    start_day = _prev(start_day)
                award(con, uid, "streak7", start_day, POINTS["streak7"], "7-day streak")
                if n >= 30:
                    award(con, uid, "streak30", start_day, POINTS["streak30"], "30-day streak")
    except Exception as e:
        print(f"[rewards] daily: {type(e).__name__}", flush=True)
    sweep_user(uid)


def touch_soon(uid: int):
    """Fire and forget from a request handler: the page never waits for points."""
    if not enabled() or time.time() - _last.get(uid, 0) < THROTTLE:
        return
    try:
        asyncio.get_running_loop().run_in_executor(None, touch, uid)
    except RuntimeError:
        pass


def sweep_referred(referrer: int):
    with closing(db()) as con:
        ids = [r["referred_id"] for r in con.execute("SELECT referred_id FROM referrals WHERE referrer_id=? ORDER BY created_at DESC LIMIT 200", (referrer,)).fetchall()]
    for rid in ids:
        sweep_user(rid)
    sweep_referrer(referrer)


# ------------------------------------------------------------------ boards and member view
def _board(period: str, me: int, limit: int = 10) -> dict:
    where, args = ("AND created_at>=?", [int(datetime.now(timezone.utc).replace(day=1, hour=0, minute=0, second=0, microsecond=0).timestamp())]) \
        if period == "month" else ("", [])
    with closing(db()) as con:
        rows = con.execute(f"SELECT user_id, SUM(lifetime) AS s FROM points_ledger WHERE kind<>'adjust' {where} GROUP BY user_id "
                           f"HAVING SUM(lifetime)>0 ORDER BY s DESC, user_id LIMIT ?", (*args, limit)).fetchall()
        mine = con.execute(f"SELECT COALESCE(SUM(lifetime),0) AS s FROM points_ledger WHERE user_id=? AND kind<>'adjust' {where}", (me, *args)).fetchone()["s"]
        ahead = con.execute(f"SELECT COUNT(*) AS n FROM (SELECT user_id FROM points_ledger WHERE kind<>'adjust' {where} GROUP BY user_id "
                            f"HAVING SUM(lifetime)>?) x", (*args, mine)).fetchone()["n"] if mine > 0 else None
    with closing(auth.db()) as a:
        emails = {r["id"]: r["email"] for r in a.execute(
            f"SELECT id,email FROM users WHERE id IN ({','.join('?' * len(rows))})", [r["user_id"] for r in rows]).fetchall()} if rows else {}
    top = [{"rank": i + 1, "name": mask(emails.get(r["user_id"])), "points": int(r["s"]), "me": r["user_id"] == me} for i, r in enumerate(rows)]
    return {"top": top, "my_points": int(mine), "my_rank": (ahead + 1) if ahead is not None else None}


def member_view(uid: int, base: str) -> dict:
    code = code_for(uid)
    bal = balances(uid)
    with closing(db()) as con:
        refs = con.execute("SELECT referred_id, status, created_at FROM referrals WHERE referrer_id=? ORDER BY created_at DESC LIMIT 100", (uid,)).fetchall()
        recent = [dict(r) for r in con.execute("SELECT delta, kind, note, created_at FROM points_ledger WHERE user_id=? ORDER BY id DESC LIMIT 20", (uid,)).fetchall()]
    with closing(auth.db()) as a:
        emails = {r["id"]: r["email"] for r in a.execute(
            f"SELECT id,email FROM users WHERE id IN ({','.join('?' * len(refs))})", [r["referred_id"] for r in refs]).fetchall()} if refs else {}
    return {"enabled": True, "code": code, "link": f"{base}/login.html?rc={code}", "points": bal, "level": level_for(bal["lifetime"]),
            "levels": [{"name": n, "at": a} for n, a in LEVELS], "rules": RULES,
            "referrals": {"total": len(refs), "qualified": sum(1 for r in refs if r["status"] == "qualified"),
                          "list": [{"name": mask(emails.get(r["referred_id"])), "status": r["status"], "at": r["created_at"]} for r in refs]},
            "recent": recent, "month": _board("month", uid), "all": _board("all", uid), "bonus": bonus_view(uid)}


def _base(request) -> str:
    return os.environ.get("DRIVERLINE_BASE_URL", "").rstrip("/") or str(request.base_url).rstrip("/")


async def api_member(request):
    u = auth.require_user(request)
    if not enabled():
        return JSONResponse({"enabled": False})

    def work():
        touch(u["id"], force=True)
        sweep_referred(u["id"])
        return member_view(u["id"], _base(request))
    return JSONResponse(await asyncio.to_thread(work))


# ------------------------------------------------------------------ admin
def admin_overview() -> dict:
    with closing(db()) as con:
        tot = con.execute("SELECT COUNT(*) AS n, COALESCE(SUM(lifetime),0) AS l, COALESCE(SUM(spendable),0) AS s FROM points_balances").fetchone()
        refs = {r["status"]: r["n"] for r in con.execute("SELECT status, COUNT(*) AS n FROM referrals GROUP BY status").fetchall()}
        top = con.execute("SELECT referrer_id, COUNT(*) AS n FROM referrals WHERE status='qualified' GROUP BY referrer_id ORDER BY n DESC LIMIT 10").fetchall()
        led = con.execute("SELECT user_id, delta, kind, note, created_at FROM points_ledger ORDER BY id DESC LIMIT 25").fetchall()
    ids = {r["referrer_id"] for r in top} | {r["user_id"] for r in led}
    with closing(auth.db()) as a:
        emails = {r["id"]: r["email"] for r in a.execute(
            f"SELECT id,email FROM users WHERE id IN ({','.join('?' * len(ids))})", list(ids)).fetchall()} if ids else {}
    return {"enabled": enabled(), "started": (start() if enabled() else None),
            "totals": {"members_with_points": tot["n"], "lifetime_issued": int(tot["l"]), "spendable_held": int(tot["s"])},
            "referrals": {"pending": refs.get("pending", 0), "qualified": refs.get("qualified", 0)},
            "top_referrers": [{"email": emails.get(r["referrer_id"], "?"), "qualified": r["n"]} for r in top],
            "recent": [{"email": emails.get(r["user_id"], "?"), "delta": r["delta"], "kind": r["kind"], "note": r["note"], "at": r["created_at"]} for r in led]}


async def admin_view(request):
    auth.require_admin(request)
    return JSONResponse(await asyncio.to_thread(admin_overview))


async def admin_adjust(request):
    admin = auth.require_admin(request)
    d = await auth._json(request)
    email, reason = str(d.get("email", "")).strip().lower(), str(d.get("reason", "")).strip()
    try:
        pts = int(d.get("points"))
    except (TypeError, ValueError):
        raise HTTPException(422, "Points must be a whole number")
    if not pts or abs(pts) > 100000:
        raise HTTPException(422, "Points must be between -100000 and 100000, and not 0")
    if not 3 <= len(reason) <= 200:
        raise HTTPException(422, "Give a reason (3 to 200 characters)")
    with closing(auth.db()) as a:
        u = a.execute("SELECT id FROM users WHERE email=?", (email,)).fetchone()
    if not u:
        raise HTTPException(404, "No member with that email")
    with closing(db()) as con:
        if pts < 0 and balances(u["id"])["spendable"] + pts < 0:
            raise HTTPException(409, "That would take the member below 0 spendable points")
        award(con, u["id"], "adjust", secrets.token_hex(6), pts, reason, None, admin["id"])
    return JSONResponse({"ok": True, "points": balances(u["id"])})


routes = [
    Route("/api/rewards", api_member, methods=["GET"]),
    Route("/api/admin/rewards", admin_view, methods=["GET"]),
    Route("/api/admin/rewards/adjust", admin_adjust, methods=["POST"]),
]
