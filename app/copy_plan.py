"""Who may use Copy trading, and the Signals / Copy trading switch for Signals Plus members linked to our IB.

Copy trading is available through:
  * "copy"  Copy Trading plan ($15 / 30 days): MT5 connection + copy trading, no signals, no notifications
  * "pro"   Signals Pro: signals, notifications, MT5 connection AND copy trading, all at the same time
  * "plus"  Signals Plus, ONLY for members whose Deriv account is confirmed as linked to our IB (perks._confirmed), and only INSTEAD of
            signals: the member chooses a mode, "signals" (default) or "copy". Copy mode pauses signals and notifications and gives the
            MT5 connection + copy trading. Every second spent in copy mode costs the Plus allocation pro rata (550 signals / 30 days
            = about 18.3 signals a day), deducted from the signal balance. Switching back to signals deducts what has accrued and
            switches copy trading off.
  * an administrator grant (Admin > Copy trading), kept for gifts and corrections.
A member holding Plus together with the Copy Trading plan or Pro is not in a mode: both are simply active (they paid for both).
Plan-based access counts only while billing is open, like every other plan. Existing members and admins (grandfathered) get nothing
for free here: copy trading always needs one of the above.
"""
import time
from contextlib import closing

from starlette.exceptions import HTTPException
from starlette.responses import JSONResponse
from starlette.routing import Route

from . import auth, credits

KIND = "copy_mode"


def _now() -> int:
    return int(time.time())


def ib_linked(uid: int) -> bool:
    """True when Deriv's partner records CONFIRM this member's account as tagged to us (the same test the partner perks use)."""
    try:
        from . import perks
        return bool(perks._confirmed(uid))
    except Exception as e:
        print(f"[copy-plan] IB check failed: {type(e).__name__}", flush=True)
        return False


def _granted_until(uid: int, ea_con=None) -> int:
    from . import ea
    if ea_con is not None:
        r = ea_con.execute("SELECT ends_at FROM copy_subs WHERE user_id=?", (uid,)).fetchone()
    else:
        with closing(ea.db()) as c:
            r = c.execute("SELECT ends_at FROM copy_subs WHERE user_id=?", (uid,)).fetchone()
    return int(r["ends_at"]) if r and r["ends_at"] > _now() else 0


def _mode(uid: int) -> str:
    with closing(credits.db()) as con:
        r = con.execute("SELECT mode FROM plan_mode WHERE user_id=?", (uid,)).fetchone()
    return r["mode"] if r and r["mode"] in ("signals", "copy") else "signals"


def swap(user: dict, plan_keys: set, ea_con=None):
    """None, or "signals" / "copy": the member's mode when they are on Signals Plus alone and linked to our IB."""
    if credits.grandfathered(user) or "plus" not in plan_keys or plan_keys & {"pro", "copy"}:
        return None
    if not ib_linked(user["id"]) or _granted_until(user["id"], ea_con):
        return None
    return _mode(user["id"])


def _plan_rows(uid: int):
    with closing(credits.db()) as con:
        return [dict(r) for r in con.execute("SELECT plan, starts_at, ends_at FROM billing_subs WHERE user_id=? AND plan IN ('plus','pro','copy')",
                                             (uid,)).fetchall()]


def state(user: dict, ea_con=None) -> dict:
    """What this member has right now. `source` says why copy trading is (or is not) available."""
    uid, now = user["id"], _now()
    out = {"copy": False, "until": 0, "swap": False, "mode": None, "ib": False, "source": None, "plus_active": False}
    rows = [r for r in _plan_rows(uid) if r["ends_at"] > now] if credits.billing_on() else []
    keys = {r["plan"] for r in rows}
    end = lambda p: max([r["ends_at"] for r in rows if r["plan"] == p] or [0])
    granted = _granted_until(uid, ea_con)
    best = max(end("pro"), end("copy"), granted)
    if best:
        out.update(copy=True, until=best, source="pro" if best == end("pro") else "copy" if best == end("copy") else "grant")
    out["plus_active"] = "plus" in keys
    mode = swap(user, keys, ea_con) if keys else None
    if mode:
        out.update(swap=True, mode=mode, ib=True)
        if mode == "copy":
            out.update(copy=True, until=end("plus"), source="plus")
    else:
        out["ib"] = bool("plus" in keys and ib_linked(uid))
    return out


def copy_until(uid: int, ea_con=None) -> int:
    """Seconds-since-epoch until which this member may copy trades, 0 when they may not."""
    from . import ea
    if ea_con is not None:
        r = ea_con.execute("SELECT tier FROM users WHERE id=?", (uid,)).fetchone()
    else:
        with closing(ea.db()) as c:
            r = c.execute("SELECT tier FROM users WHERE id=?", (uid,)).fetchone()
    if not r:
        return 0
    return state({"id": uid, "tier": r["tier"]}, ea_con)["until"]


def copy_active(user: dict) -> bool:
    return state(user)["copy"]


def signals_paused(user: dict) -> bool:
    """True while a Plus + IB member has switched to copy mode: their signals and notifications are off."""
    if credits.grandfathered(user) or not credits.billing_on():
        return False
    keys = {r["plan"] for r in _plan_rows(user["id"]) if r["ends_at"] > _now()}
    return swap(user, keys) == "copy"


# ---------------------------------------------------------------- the cost of copy mode
def _rate() -> float:
    p = credits.CATALOG["plus"]
    return p["signals"] / (p["days"] * 86400.0)


def _covered(uid: int, a: int, b: int) -> int:
    """Seconds between a and b during which a Signals Plus plan was active (overlapping plans are counted once)."""
    iv = sorted((max(r["starts_at"], a), min(r["ends_at"], b)) for r in _plan_rows(uid) if r["plan"] == "plus")
    total, cur_end = 0, a
    for s, e in iv:
        if e <= s:
            continue
        s = max(s, cur_end)
        if e > s:
            total += e - s
            cur_end = e
    return total


def accrue(uid: int, now: int = None) -> int:
    """Charge the signals for the time spent in copy mode so far. Safe to call any number of times: it moves its own cursor forward,
    and the signal balance never goes below zero. Returns the number of signals deducted by this call."""
    now = now or _now()
    with closing(credits.db()) as con:
        r = con.execute("SELECT mode, since, carry FROM plan_mode WHERE user_id=?", (uid,)).fetchone()
        if not r or r["mode"] != "copy" or not r["since"] or now <= r["since"]:
            return 0
        due = _covered(uid, r["since"], now) * _rate() + (r["carry"] or 0.0)
        n = int(due)
        taken = 0
        if n > 0:
            bal = con.execute("SELECT balance FROM credit_balances WHERE user_id=?", (uid,)).fetchone()
            taken = min(n, bal["balance"] if bal else 0)
            if taken > 0:
                credits._credit(con, uid, -taken, KIND, f"copymode:{r['since']}-{now}", "Signals used while Copy trading was on")
        con.execute("UPDATE plan_mode SET since=?, carry=?, updated_at=? WHERE user_id=?", (now, due - n, now, uid))
        con.commit()
    return taken


def set_mode(user: dict, mode: str) -> dict:
    if mode not in ("signals", "copy"):
        raise HTTPException(422, "mode must be signals or copy")
    st = state(user)
    if not st["swap"]:
        raise HTTPException(409, "Switching between Signals and Copy trading is for Signals Plus members whose Deriv account is linked to TradeLens, "
                                 "who do not also have Signals Pro or the Copy Trading plan.")
    uid, now = user["id"], _now()
    deducted = 0
    if st["mode"] != mode:
        if st["mode"] == "copy":
            deducted = accrue(uid, now)
        with closing(credits.db()) as con:
            con.execute("INSERT INTO plan_mode(user_id,mode,since,carry,updated_at) VALUES(?,?,?,0,?) ON CONFLICT(user_id) DO UPDATE SET "
                        "mode=excluded.mode, since=excluded.since, carry=0, updated_at=excluded.updated_at",
                        (uid, mode, now if mode == "copy" else None, now))
            con.commit()
        if mode == "signals":                       # copy trading switches off: no new copies; copies already open still close with the master trade
            from . import ea
            with closing(ea.db()) as e:
                e.execute("UPDATE copy_members SET enabled=0, updated_at=? WHERE user_id=?", (now, uid))
                e.execute("UPDATE copy_jobs SET status='skipped', note='You switched back to Signals' WHERE user_id=? AND status='waiting'", (uid,))
                e.commit()
    return {"mode": mode, "deducted": deducted}


def view(user: dict) -> dict:
    """For the member's screens."""
    try:
        accrue(user["id"])
    except Exception as e:
        print(f"[copy-plan] accrue failed: {type(e).__name__}", flush=True)
    st = state(user)
    return {"source": st["source"], "swap": st["swap"], "mode": st["mode"], "ib": st["ib"], "plus_active": st["plus_active"],
            "per_day": round(_rate() * 86400, 1)}


# ---------------------------------------------------------------- API
async def mode_set(request):
    u = auth.require_user(request)
    d = await auth._json(request)
    return JSONResponse(set_mode(u, d.get("mode")))


routes = [Route("/api/copy/me/mode", mode_set, methods=["POST"])]
