"""Reminders before a paid service ends, and a notice when it has ended.

A member's services are what their plans give them: signal notifications, the MT5 connection (which also covers AutoTrading) and copy trading.
For each service the END is the latest end among every plan that includes it (plans stack, so renewing moves it later). A member is told:
  * "3d"    between 3 days and 1 day before the end,
  * "1d"    in the last day before the end,
  * "ended" when it has ended (only within ENDED_WINDOW, so a long-lapsed plan is never announced).
Each (member, service, stage, end time) is sent once, recorded in expiry_notices BEFORE sending, so a repeat run, a restart or two server instances can
never send it twice. Renewing changes the end time, which starts a fresh cycle. All services due in one run go out as ONE message, in the member's Messages
and as a push to their devices. Nothing is sent for blocked members, for members who keep a service regardless of plans (members and admins keep
notifications and the MT5 connection), or while billing is not open. A member inside their quiet hours is simply handled on a later run.
DRIVERLINE_EXPIRY=0 switches the job off. Read-only for everything except the member's inbox and push."""
import asyncio
import os
import time
from contextlib import closing
from datetime import datetime, timezone

from starlette.exceptions import HTTPException
from starlette.responses import JSONResponse
from starlette.routing import Route

from . import auth, credits, push

DAY = 86400
STAGE_3D, STAGE_1D = 3 * DAY, 1 * DAY
ENDED_WINDOW = 3 * DAY
INTERVAL = 30 * 60

SERVICES = {"notify": ("Signal notifications", "your signal notifications will stop"),
            "mt5": ("MT5 connection and AutoTrading", "your MT5 connection and AutoTrading will stop"),
            "copy": ("Copy trading", "copy trading will stop")}
ENDED_TEXT = {"notify": "signal notifications are off", "mt5": "your MT5 connection and AutoTrading are off", "copy": "copy trading is off"}


def enabled() -> bool:
    return os.environ.get("DRIVERLINE_EXPIRY", "1") != "0"


def _day(ts: int) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%d %b %Y")


def service_ends(uid: int, subs: list, copy_end: int = 0) -> dict:
    """{service: latest end} over every plan the member has had (rows are (plan, ends_at)); copy_end is an administrator grant or referral reward."""
    out = {}
    for plan, ends in subs:
        meta = credits.CATALOG.get(plan) or credits.GRANTS.get(plan) or {}
        for svc in SERVICES:
            if meta.get(svc):
                out[svc] = max(out.get(svc, 0), int(ends))
    if copy_end:
        out["copy"] = max(out.get("copy", 0), int(copy_end))
    return out


def stage_of(end: int, now: int):
    rem = end - now
    if 0 < rem <= STAGE_1D:
        return "1d"
    if STAGE_1D < rem <= STAGE_3D:
        return "3d"
    if -ENDED_WINDOW <= rem <= 0:
        return "ended"
    return None


def compose(due: list, now: int) -> dict:
    """due = [(service, stage, end)]. One title and body for everything that is due for this member."""
    ended = [d for d in due if d[1] == "ended"]
    soon = [d for d in due if d[1] != "ended"]
    parts, url = [], "/billing.html"
    for svc, stage, end in sorted(soon, key=lambda d: d[2]):
        days = max(1, -(-(end - now) // DAY))
        parts.append(f"{SERVICES[svc][0]} ends on {_day(end)} (in {days} day{'s' if days != 1 else ''}), then {SERVICES[svc][1]}.")
    for svc, stage, end in sorted(ended, key=lambda d: d[2]):
        parts.append(f"{SERVICES[svc][0]} ended on {_day(end)}, so {ENDED_TEXT[svc]}.")
    if ended and not soon:
        title = "Your TradeLens plan has ended"
        tail = "Renew in the Billing tab to switch it back on. Your settings and records are kept."
    elif soon and not ended:
        one_day = all(d[1] == "1d" for d in soon)
        title = "Your TradeLens plan ends tomorrow" if one_day else "Your TradeLens plan ends soon"
        tail = "Renew in the Billing tab before then to avoid any interruption. Days you add are put on top of what you have left."
    else:
        title = "Your TradeLens plan: action needed"
        tail = "Renew in the Billing tab to keep or restore these services. Your settings and records are kept."
    return {"title": title, "body": " ".join(parts) + " " + tail, "url": url, "tag": "plan-expiry"}


def _claim(con, uid: int, svc: str, stage: str, end: int, now: int) -> bool:
    try:
        con.execute("INSERT INTO expiry_notices(user_id,service,stage,end_ts,sent_at) VALUES(?,?,?,?,?)", (uid, svc, stage, end, now))
        con.commit()
        return True
    except Exception as e:
        con.raw.rollback()
        if credits.dbx.is_duplicate(e):
            return False
        raise


def _deliver(uid: int, msg: dict, sender, now: int):
    from . import inbox
    with closing(inbox.db()) as ic:
        ic.insert("INSERT INTO inbox_messages(user_id,sender,admin_id,body,created_at) VALUES(?,?,?,?,?)", (uid, "admin", 0, (msg["title"] + ". " + msg["body"])[:1000], now))
        ic.commit()
    with closing(push.db()) as pc:
        subs = [dict(r) for r in pc.execute("SELECT * FROM push_subs WHERE user_id=?", (uid,)).fetchall()]
    gone = []
    for sub in subs:
        try:
            if not sender(sub, msg):
                gone.append(sub["id"])
        except Exception as e:
            print(f"[expiry] push failed: {type(e).__name__}", flush=True)
    if gone:
        with closing(push.db()) as pc:
            for i in gone:
                pc.execute("DELETE FROM push_subs WHERE id=?", (i,))
            pc.commit()


def due_for_all(now: int):
    """[(uid, {service: end})] for members who have any service ending in the next 3 days or ended in the last 3."""
    lo, hi = now - ENDED_WINDOW, now + STAGE_3D
    with closing(credits.db()) as con:
        ids = {r["user_id"] for r in con.execute("SELECT DISTINCT user_id FROM billing_subs WHERE ends_at>=? AND ends_at<=?", (lo, hi)).fetchall()}
        subs = {}
        copy_end = {}
        if ids:
            for r in con.execute("SELECT user_id, plan, ends_at FROM billing_subs").fetchall():
                if r["user_id"] in ids:
                    subs.setdefault(r["user_id"], []).append((r["plan"], r["ends_at"]))
    try:
        from . import ea
        with closing(ea.db()) as ec:
            for r in ec.execute("SELECT user_id, ends_at FROM copy_subs WHERE ends_at>=? AND ends_at<=?", (lo, hi)).fetchall():
                ids.add(r["user_id"])
            for r in ec.execute("SELECT user_id, ends_at FROM copy_subs").fetchall():
                if r["user_id"] in ids:
                    copy_end[r["user_id"]] = max(copy_end.get(r["user_id"], 0), int(r["ends_at"]))
    except Exception as e:
        print(f"[expiry] copy grants unreadable: {type(e).__name__}", flush=True)
    return [(u, service_ends(u, subs.get(u, []), copy_end.get(u, 0))) for u in sorted(ids)]


def run_once(now: int = None, sender=None) -> int:
    """One pass. Returns the number of members messaged."""
    if not credits.billing_on():
        return 0
    now = int(now or time.time())
    sender = sender or push._send
    sent = 0
    for uid, ends in due_for_all(now):
        with closing(auth.db()) as ac:
            u = ac.execute("SELECT id, tier, blocked FROM users WHERE id=?", (uid,)).fetchone()
        if not u or u["blocked"] or u["tier"] == "admin":
            continue
        user = {"id": uid, "tier": u["tier"]}
        todo = []
        for svc, end in ends.items():
            st = stage_of(end, now)
            if not st:
                continue
            if svc != "copy" and credits.grandfathered(user):          # members keep notifications and the MT5 connection whatever their plans say
                continue
            if st == "ended" and credits.entitled(user, svc):          # something else still provides it: nothing has stopped
                continue
            todo.append((svc, st, end))
        if not todo:
            continue
        try:
            from . import signal_feed as F
            with closing(F.db()) as fc:
                quiet = push._quiet(F._prefs(fc, uid), datetime.now(timezone.utc))
        except Exception:
            quiet = False
        if quiet:                                  # not at night: the next run (30 minutes later) tries again; every stage has hours of room
            continue
        with closing(credits.db()) as con:
            fresh = [t for t in todo if _claim(con, uid, t[0], t[1], t[2], now)]
        if not fresh:
            continue
        try:
            _deliver(uid, compose(fresh, now), sender, now)
            sent += 1
        except Exception as e:
            print(f"[expiry] could not message member: {type(e).__name__}", flush=True)
    return sent


async def loop():
    await asyncio.sleep(120)                       # let the server finish starting
    while True:
        try:
            n = await asyncio.get_running_loop().run_in_executor(None, run_once)
            if n:
                print(f"[expiry] reminded {n} member(s)", flush=True)
        except Exception as e:
            print(f"[expiry] pass failed: {type(e).__name__}: {e}", flush=True)
        await asyncio.sleep(INTERVAL)


async def admin_list(request):
    """Members whose services end within 7 days or ended within the last 3, soonest first."""
    auth.require_admin(request)
    now = int(time.time())
    out = []
    with closing(credits.db()) as con:
        rows = con.execute("SELECT user_id, plan, ends_at FROM billing_subs").fetchall()
    subs = {}
    for r in rows:
        subs.setdefault(r["user_id"], []).append((r["plan"], r["ends_at"]))
    try:
        from . import ea
        with closing(ea.db()) as ec:
            cp = {r["user_id"]: r["ends_at"] for r in ec.execute("SELECT user_id, ends_at FROM copy_subs").fetchall()}
    except Exception:
        cp = {}
    with closing(auth.db()) as ac:
        for uid in sorted(set(subs) | set(cp)):
            ends = {s: e for s, e in service_ends(uid, subs.get(uid, []), cp.get(uid, 0)).items() if now - ENDED_WINDOW <= e <= now + 7 * DAY}
            if not ends:
                continue
            u = ac.execute("SELECT email, tier FROM users WHERE id=?", (uid,)).fetchone()
            if u and u["tier"] != "admin":
                out.append({"user_id": uid, "email": u["email"], "services": [{"service": SERVICES[s][0], "ends_at": e, "ended": e <= now} for s, e in sorted(ends.items(), key=lambda x: x[1])]})
    out.sort(key=lambda r: min(x["ends_at"] for x in r["services"]))
    return JSONResponse({"now": now, "members": out[:200]})


routes = [Route("/api/admin/expiring", admin_list, methods=["GET"])]
