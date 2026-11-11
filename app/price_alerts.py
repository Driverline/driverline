"""Price alerts: "tell me when a symbol gets close to this price", so a manual trader has time to open MT5 and decide (enter, move a take profit, ...).

No MT5 is needed: prices come from Deriv's public feed, the same one the signal scanner uses, and the alert is a push notification (plus an
inbox message when the member has no push device set up).

The alert fires a little BEFORE the level, on purpose. Deriv's price and the member's MT5 price can differ slightly, and the member needs
time to act. The distance ("early warning") is worked out when the alert is created, from how far that symbol normally moves in 15 minutes
(the average M15 candle range of the last 12 hours): 1x, 2x (default) or 4x that range, and never more than half the way from the
current price to the level. A level closer than one M15 range to the current price is refused (it would fire at once).

An alert fires once. Each member has at most MAX_ACTIVE active alerts, can create at most PER_DAY alerts a day, and every alert expires
(7 days by default, 30 at most), so the server never watches an unbounded list. The checker makes one request per symbol that has an
alert, every CHECK_SECONDS, and looks at the last three one-minute candles so a quick spike through the zone is not missed.
PRICE_ALERTS=0 switches the feature off.
"""
import asyncio
import math
import os
import re
import time
from contextlib import closing

from starlette.exceptions import HTTPException
from starlette.responses import JSONResponse
from starlette.routing import Route

from . import auth, credits, dbx
from .config import INSTRUMENTS

SCHEMA = """
    CREATE TABLE IF NOT EXISTS price_alerts(id {ID}, user_id INTEGER NOT NULL, instrument TEXT NOT NULL, level DOUBLE PRECISION NOT NULL,
        dir TEXT NOT NULL, buffer DOUBLE PRECISION NOT NULL, trigger_price DOUBLE PRECISION NOT NULL, price0 DOUBLE PRECISION, early INTEGER NOT NULL,
        note TEXT, status TEXT NOT NULL, created_at BIGINT NOT NULL, expires_at BIGINT NOT NULL, fired_at BIGINT, fired_price DOUBLE PRECISION,
        delivered INTEGER);
    CREATE INDEX IF NOT EXISTS ix_price_alerts_status ON price_alerts(status, instrument);
    CREATE INDEX IF NOT EXISTS ix_price_alerts_user ON price_alerts(user_id, id)"""

MAX_ACTIVE = 10            # active alerts per member
PER_DAY = 30               # alerts a member may create in 24 hours
MAX_SYMBOLS = 40           # symbols the checker looks at in one round
CHECK_SECONDS = 30
EARLY = {1: ("A little early", 1.0), 2: ("Early", 2.0), 3: ("Very early", 4.0)}   # label, multiple of the average 15-minute range
DEFAULT_EARLY = 2
DEFAULT_DAYS, MAX_DAYS = 7, 30
NOTE_MAX = 80
PREVIEW_GAP = 3            # seconds between price look-ups from one member (each one asks Deriv)
_last_lookup: dict = {}


def enabled() -> bool:
    return os.environ.get("PRICE_ALERTS", "1") != "0"


def db():
    auth.db().close()
    return dbx.connect("price_alerts", SCHEMA)


def _now() -> int:
    return int(time.time())


def fmt(x: float) -> str:
    return f"{x:,.{4 if abs(x) < 10 else 2}f}"


def _entitled(user: dict) -> bool:
    return credits.entitled(user, "notify")


# ---------------------------------------------------------------- price and buffer
def zone(price: float, level: float, rng: float, early: int) -> dict:
    """Which way the price must travel, and where it fires. Raises ValueError with a message the member can read."""
    if not (price > 0 and rng > 0):
        raise ValueError("The price is not available right now. Try again in a moment.")
    dist = abs(level - price)
    if dist < rng:
        raise ValueError(f"That level is too close to the current price ({fmt(price)}). This symbol moves about {fmt(rng)} in 15 minutes, so the alert "
                         "would fire straight away. Choose a level further away.")
    buf = min(EARLY[early][1] * rng, dist / 2.0)
    up = level > price
    return {"dir": "up" if up else "down", "buffer": buf, "trigger": level - buf if up else level + buf, "distance": dist}


async def market(key: str, data: dict = None) -> dict:
    """{price, range}: the latest price and the average 15-minute candle range of the last 12 hours, from Deriv."""
    code = INSTRUMENTS[key][0]
    if data is None:
        from . import deriv
        data = await deriv.fetch_multi([code], ("M15",), 60)
    c = (data.get(code) or {}).get("M15") or []
    if len(c) < 20:
        raise HTTPException(503, "The price for this symbol is not available right now. Try again in a moment.")
    rs = [x["high"] - x["low"] for x in c[:-1][-48:]]
    return {"price": c[-1]["close"], "range": sum(rs) / len(rs)}


def _throttle(uid: int):
    now = time.time()
    if now - _last_lookup.get(uid, 0) < PREVIEW_GAP:
        raise HTTPException(429, "Please wait a few seconds between price checks.")
    _last_lookup[uid] = now
    if len(_last_lookup) > 5000:
        for k in [k for k, t in _last_lookup.items() if now - t > 60]:
            _last_lookup.pop(k, None)


# ---------------------------------------------------------------- the checker
async def check_once(data: dict = None, now: int = None) -> int:
    """One round. `data` ({deriv symbol code: {"M1": candles}}) lets a test supply prices. Returns how many alerts fired."""
    now = now or _now()
    with closing(db()) as con:
        con.execute("UPDATE price_alerts SET status='expired' WHERE status='active' AND expires_at<=?", (now,))
        con.commit()
        rows = [dict(r) for r in con.execute("SELECT * FROM price_alerts WHERE status='active' ORDER BY id").fetchall()]
    if not rows:
        return 0
    keys = list(dict.fromkeys(r["instrument"] for r in rows if r["instrument"] in INSTRUMENTS))[:MAX_SYMBOLS]
    if data is None:
        from . import deriv
        data = await deriv.fetch_multi([INSTRUMENTS[k][0] for k in keys], ("M1",), 3)
    fired = []
    with closing(db()) as con:
        for r in rows:
            if r["instrument"] not in keys:
                continue
            cs = [c for c in (data.get(INSTRUMENTS[r["instrument"]][0]) or {}).get("M1", []) if c["epoch"] >= r["created_at"] - 60]
            if not cs:
                continue
            hit = max(c["high"] for c in cs) >= r["trigger_price"] if r["dir"] == "up" else min(c["low"] for c in cs) <= r["trigger_price"]
            if not hit:
                continue
            cur = con.execute("UPDATE price_alerts SET status='fired', fired_at=?, fired_price=? WHERE id=? AND status='active'",
                              (now, cs[-1]["close"], r["id"]))
            if cur.rowcount:
                fired.append((r, cs[-1]["close"]))
        con.commit()
    for r, price in fired:
        try:
            ok = await asyncio.to_thread(deliver, r, price)
        except Exception as e:
            print(f"[alerts] delivery failed: {type(e).__name__}", flush=True)
            ok = 0
        with closing(db()) as con:
            con.execute("UPDATE price_alerts SET delivered=? WHERE id=?", (int(bool(ok)), r["id"]))
            con.commit()
    return len(fired)


def message(r: dict, price: float) -> tuple:
    name = INSTRUMENTS.get(r["instrument"], ("", r["instrument"]))[1]
    title = f"Price alert: {name}"
    body = (f"{name} is close to {fmt(r['level'])} (now about {fmt(price)}). Open MT5 to decide. "
            "Prices here come from Deriv, so your MT5 may differ slightly." + (f" Your note: {r['note']}" if r.get("note") else ""))
    return title, body


def deliver(r: dict, price: float, sender=None) -> int:
    """Push to the member's devices. If none received it, leave an inbox message so the alert is not lost. Returns the number delivered."""
    from . import push
    sender = sender or push._send
    title, body = message(r, price)
    with closing(auth.db()) as con:
        u = con.execute("SELECT id, tier, blocked FROM users WHERE id=?", (r["user_id"],)).fetchone()
    if not u or u["blocked"]:
        return 0
    sent = 0
    if _entitled({"id": u["id"], "tier": u["tier"]}):
        with closing(push.db()) as con:
            subs = [dict(s) for s in con.execute("SELECT * FROM push_subs WHERE user_id=?", (r["user_id"],)).fetchall()]
        gone = []
        for sub in subs:
            try:
                if sender(sub, {"title": title, "body": body, "url": "/alerts.html", "tag": f"alert-{r['id']}"}):
                    sent += 1
                else:
                    gone.append(sub["id"])
            except Exception as e:
                print(f"[alerts] push failed: {type(e).__name__}", flush=True)
        if gone:
            with closing(push.db()) as con:
                for i in gone:
                    con.execute("DELETE FROM push_subs WHERE id=?", (i,))
                con.commit()
    if not sent:
        from . import inbox
        with closing(inbox.db()) as ic:
            ic.insert("INSERT INTO inbox_messages(user_id,sender,admin_id,body,created_at) VALUES(?,?,?,?,?)",
                      (r["user_id"], "admin", 0, (title + ". " + body)[:1000], _now()))
            ic.commit()
    return sent


async def alert_loop():
    await asyncio.sleep(45)
    while True:
        try:
            if enabled():
                await asyncio.wait_for(check_once(), timeout=90)
        except Exception as e:
            print(f"[alerts] check failed: {type(e).__name__}: {str(e)[:150]}", flush=True)
        await asyncio.sleep(CHECK_SECONDS)


# ---------------------------------------------------------------- API
def _out(r) -> dict:
    d = dict(r)
    d["name"] = INSTRUMENTS.get(d["instrument"], ("", d["instrument"]))[1]
    return {k: d[k] for k in ("id", "instrument", "name", "level", "dir", "buffer", "trigger_price", "price0", "early", "note", "status",
                              "created_at", "expires_at", "fired_at", "fired_price", "delivered")}


def _meta() -> dict:
    return {"instruments": [{"key": k, "name": v[1]} for k, v in INSTRUMENTS.items()],
            "early": [{"key": k, "label": v[0]} for k, v in EARLY.items()], "default_early": DEFAULT_EARLY,
            "max_active": MAX_ACTIVE, "per_day": PER_DAY, "default_days": DEFAULT_DAYS, "max_days": MAX_DAYS, "note_max": NOTE_MAX}


async def me_get(request):
    u = auth.require_user(request)
    if not enabled():
        return JSONResponse({"available": False, "reason": "Price alerts are switched off right now."})
    if not _entitled(u):
        return JSONResponse({"available": False, "reason": "Price alerts are part of notifications, which need an active Signals plan."})
    with closing(db()) as con:
        act = [_out(r) for r in con.execute("SELECT * FROM price_alerts WHERE user_id=? AND status='active' ORDER BY id DESC", (u["id"],)).fetchall()]
        old = [_out(r) for r in con.execute("SELECT * FROM price_alerts WHERE user_id=? AND status<>'active' ORDER BY id DESC LIMIT 20", (u["id"],)).fetchall()]
    return JSONResponse({"available": True, "active": act, "recent": old, "now": _now(), **_meta()})


def _parse(d: dict) -> tuple:
    key = str(d.get("instrument") or "")
    if key not in INSTRUMENTS:
        raise HTTPException(422, "Choose a symbol from the list")
    try:
        level = float(d.get("level"))
    except (TypeError, ValueError):
        raise HTTPException(422, "Enter the price as a number")
    if not (math.isfinite(level) and level > 0):
        raise HTTPException(422, "Enter a price above zero")
    try:
        early = int(d.get("early", DEFAULT_EARLY))
    except (TypeError, ValueError):
        early = 0
    if early not in EARLY:
        raise HTTPException(422, "Choose how early you want to be warned")
    return key, level, early


async def _look(u, key, level, early) -> tuple:
    _throttle(u["id"])
    m = await market(key)
    try:
        z = zone(m["price"], level, m["range"], early)
    except ValueError as e:
        raise HTTPException(422, str(e))
    return m, z


async def preview(request):
    u = auth.require_user(request)
    if not enabled() or not _entitled(u):
        raise HTTPException(403, "Price alerts are not available on your account")
    q = request.query_params
    key, level, early = _parse({"instrument": q.get("instrument"), "level": q.get("level"), "early": q.get("early", DEFAULT_EARLY)})
    m, z = await _look(u, key, level, early)
    return JSONResponse({"price": m["price"], "range": m["range"], "dir": z["dir"], "buffer": z["buffer"], "trigger": z["trigger"]})


async def create(request):
    u = auth.require_user(request)
    d = await auth._json(request)
    if not enabled() or not _entitled(u):
        raise HTTPException(403, "Price alerts are not available on your account")
    key, level, early = _parse(d)
    note = re.sub(r"[\x00-\x1f\x7f<>]", " ", str(d.get("note") or "")).strip()[:NOTE_MAX]
    try:
        days = int(d.get("days", DEFAULT_DAYS))
    except (TypeError, ValueError):
        days = 0
    if not 1 <= days <= MAX_DAYS:
        raise HTTPException(422, f"An alert can last 1 to {MAX_DAYS} days")
    now = _now()
    with closing(db()) as con:
        if con.execute("SELECT COUNT(*) AS c FROM price_alerts WHERE user_id=? AND status='active'", (u["id"],)).fetchone()["c"] >= MAX_ACTIVE:
            raise HTTPException(409, f"You already have {MAX_ACTIVE} active alerts. Cancel one or wait for it to fire.")
        if con.execute("SELECT COUNT(*) AS c FROM price_alerts WHERE user_id=? AND created_at>?", (u["id"], now - 86400)).fetchone()["c"] >= PER_DAY:
            raise HTTPException(429, f"You can set up to {PER_DAY} alerts a day.")
        for r in con.execute("SELECT level FROM price_alerts WHERE user_id=? AND instrument=? AND status='active'", (u["id"], key)).fetchall():
            if abs(r["level"] - level) <= abs(level) * 0.0001:
                raise HTTPException(409, "You already have an active alert at that price.")
    m, z = await _look(u, key, level, early)
    with closing(db()) as con:
        aid = con.insert("INSERT INTO price_alerts(user_id,instrument,level,dir,buffer,trigger_price,price0,early,note,status,created_at,expires_at) "
                         "VALUES(?,?,?,?,?,?,?,?,?,'active',?,?)",
                         (u["id"], key, level, z["dir"], z["buffer"], z["trigger"], m["price"], early, note, now, now + days * 86400))
        con.commit()
        r = con.execute("SELECT * FROM price_alerts WHERE id=?", (aid,)).fetchone()
    return JSONResponse({"ok": True, "alert": _out(r), "price": m["price"]})


async def cancel(request):
    u = auth.require_user(request)
    try:
        aid = int(request.path_params["aid"])
    except (TypeError, ValueError):
        raise HTTPException(404, "Alert not found")
    with closing(db()) as con:
        cur = con.execute("UPDATE price_alerts SET status='cancelled' WHERE id=? AND user_id=? AND status='active'", (aid, u["id"]))
        con.commit()
    if not cur.rowcount:
        raise HTTPException(404, "Alert not found, or it already fired")
    return JSONResponse({"ok": True})


routes = [
    Route("/api/alerts", me_get, methods=["GET"]),
    Route("/api/alerts", create, methods=["POST"]),
    Route("/api/alerts/preview", preview, methods=["GET"]),
    Route("/api/alerts/{aid:int}/cancel", cancel, methods=["POST"]),
]
