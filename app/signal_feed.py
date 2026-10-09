"""Live setups ("signals"): rule-based, no Claude, no credits.

The same rules the backtest tested are checked on every new closed candle. A setup is published only if its pooled
backtest result is not clearly negative after costs. Each one carries a risk rating worked out in code, the measured
hit rate of that setup (never an invented percentage), and is tracked to its real outcome so the track record is
honest. Members open a setup to see its levels: that counts towards the member's own daily limit (paid members) or
uses one of the free member's daily analyses."""
import asyncio
import json
import math
import time
from contextlib import closing
from datetime import datetime, timedelta, timezone

from starlette.exceptions import HTTPException
from starlette.responses import JSONResponse
from starlette.routing import Route

from . import auth, dbx, journal, radar
from . import signals as S
from .config import INSTRUMENTS

CFG = {"exec_max_dev_r": 0.5, "min_publish_t": -1.0, "fallback_rr": 1.5, "limit_default": 5, "limit_max": 10, "scan_seconds": 300,
       "view_hours": 12, "min_pooled_n": 300}
try:
    radar._merge(CFG, json.loads(__import__("os").environ.get("DRIVERLINE_FEED_CONFIG", "{}")))
except ValueError:
    pass

SCHEMA = """
    CREATE TABLE IF NOT EXISTS signals(id {ID}, created_at BIGINT NOT NULL, bar_epoch BIGINT NOT NULL,
        instrument TEXT NOT NULL, style TEXT NOT NULL, setup TEXT NOT NULL, direction INTEGER NOT NULL,
        entry DOUBLE PRECISION NOT NULL, stop DOUBLE PRECISION NOT NULL, target DOUBLE PRECISION NOT NULL,
        rr DOUBLE PRECISION NOT NULL, atr DOUBLE PRECISION, risk TEXT NOT NULL, risk_notes TEXT,
        bt_n INTEGER, bt_hit DOUBLE PRECISION, bt_be DOUBLE PRECISION, bt_avg_r DOUBLE PRECISION,
        bt_t DOUBLE PRECISION, status TEXT NOT NULL DEFAULT 'open', expires_at BIGINT NOT NULL,
        closed_at BIGINT, result_r DOUBLE PRECISION, UNIQUE(instrument, style, setup, bar_epoch));
    CREATE INDEX IF NOT EXISTS ix_signals_created ON signals(created_at);
    CREATE TABLE IF NOT EXISTS signal_opens(user_id INTEGER NOT NULL, signal_id BIGINT NOT NULL, day TEXT NOT NULL,
        created_at BIGINT NOT NULL, counted INTEGER NOT NULL DEFAULT 1, PRIMARY KEY(user_id, signal_id));
    CREATE TABLE IF NOT EXISTS user_prefs(user_id INTEGER PRIMARY KEY, signal_limit INTEGER, pending_limit INTEGER,
        pending_day TEXT, notify_styles TEXT, quiet_start INTEGER, quiet_end INTEGER, tz_offset INTEGER);
    CREATE TABLE IF NOT EXISTS signal_notifs(user_id INTEGER NOT NULL, signal_id BIGINT NOT NULL, day TEXT NOT NULL,
        PRIMARY KEY(user_id, signal_id))"""


def db():
    auth.db().close()
    return dbx.connect("feed", SCHEMA)


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def paid(user: dict) -> bool:
    return user["tier"] in ("member", "admin")


# ---------------------------------------------------------------- which setups are published
_combos = {"ts": 0.0, "data": {}}


def allowed_combos(force: bool = False) -> dict:
    """{(style, setup): stats of its best target multiple}, from the latest finished backtest."""
    if not force and _combos["data"] and time.time() - _combos["ts"] < 1800:
        return _combos["data"]
    best: dict = {}
    try:
        with closing(S.db()) as con:
            run = con.execute("SELECT id FROM backtest_runs WHERE status='done' ORDER BY id DESC LIMIT 1").fetchone()
            rows = con.execute("SELECT * FROM backtest_results WHERE run_id=?", (run["id"],)).fetchall() if run else []
        groups: dict = {}
        for r in rows:
            groups.setdefault((r["style"], r["setup"], r["rr"]), []).append(r)
        for (st, su, rr), g in groups.items():
            tot = sum(x["n"] for x in g)
            if tot < CFG["min_pooled_n"] or any(x["sd_r"] is None for x in g):
                continue
            mean = sum(x["avg_r"] * x["n"] for x in g) / tot
            se = math.sqrt(sum(x["n"] * x["sd_r"] ** 2 for x in g)) / tot
            t = mean / se if se > 0 else 0.0
            rec = {"rr": rr, "n": tot, "hit": sum(x["hit_rate"] * x["n"] for x in g) / tot, "be": g[0]["be_rate"],
                   "avg_r": mean, "t": t}
            if t >= CFG["min_publish_t"] and ((st, su) not in best or mean > best[(st, su)]["avg_r"]):
                best[(st, su)] = rec
    except Exception as e:
        print(f"[feed] could not read backtest: {e}", flush=True)
    if not best and not rows:   # no backtest has been run yet: publish everything with the fallback target, flagged
        for st in S.STYLES:
            for su in S.SETUPS:
                best[(st, su)] = {"rr": CFG["fallback_rr"], "n": 0, "hit": None, "be": None, "avg_r": None, "t": None}
    _combos.update(ts=time.time(), data=best)
    return best


def risk_rating(setup: str, key: str, style: str, d: int, ctx_d: int, vol_ratio) -> tuple:
    pts, notes = 0, []
    if radar.family(key) in ("spike", "jump"):
        pts += 2; notes.append("This index can spike through stops")
    if setup == "range_reversal":
        pts += 1; notes.append("Counter-trend setup")
    elif ctx_d and ctx_d != d:
        pts += 1; notes.append("Against the higher-timeframe trend")
    if vol_ratio is not None and vol_ratio >= 1.8:
        pts += 1; notes.append("Volatility is unusually high")
    if style == "scalp":
        pts += 1; notes.append("Short trades are more exposed to noise and costs")
    return ("Low" if pts <= 1 else "Medium" if pts == 2 else "High"), notes


# ---------------------------------------------------------------- scanning and tracking
def evaluate(sig: dict, candles: list, now: int):
    """Real outcome of a published setup, from the candles after it. Same rules as the backtest."""
    per = S.PERIOD[S.STYLES[sig["style"]]["tf"]]
    horizon = S.STYLES[sig["style"]]["horizon"]
    cost_r = S.BT_CFG["cost_atr"] / S.BT_CFG["stop_atr"]
    d, sd = sig["direction"], abs(sig["entry"] - sig["stop"])
    bars = [c for c in candles if c["epoch"] >= sig["bar_epoch"] + per]
    for c in bars[:horizon]:
        hit_stop = (c["low"] <= sig["stop"]) if d > 0 else (c["high"] >= sig["stop"])
        hit_tgt = (c["high"] >= sig["target"]) if d > 0 else (c["low"] <= sig["target"])
        if hit_stop:
            return "stop", -1.0 - cost_r
        if hit_tgt:
            return "target", sig["rr"] - cost_r
    if len(bars) >= horizon and bars[horizon - 1]["epoch"] + per <= now:
        return "timeout", d * (bars[horizon - 1]["close"] - sig["entry"]) / sd - cost_r
    return None


async def scan_signals(data: dict = None) -> list:
    """Checks the latest closed candles for setups and updates open ones. Returns the new signals."""
    if data is None:
        from . import deriv
        data = await deriv.fetch_multi([v[0] for v in INSTRUMENTS.values()], ("M5", "M15", "H1", "H4"), 300)
    combos, now, new = allowed_combos(), int(time.time()), []
    with closing(db()) as con:
        for key, (code, name) in INSTRUMENTS.items():
            d = data.get(code)
            if not d:
                continue
            spiky = radar.family(key) in ("spike", "jump")
            for style, sp in S.STYLES.items():
                if sp["tf"] not in d or sp["ctx"] not in d or len(d[sp["tf"]]) < 150:
                    continue
                per = S.PERIOD[sp["tf"]]
                for row in con.execute("SELECT * FROM signals WHERE instrument=? AND style=? AND status='open'",
                                       (key, style)).fetchall():
                    res = evaluate(dict(row), d[sp["tf"]], now)
                    if res:
                        con.execute("UPDATE signals SET status=?, result_r=?, closed_at=? WHERE id=?",
                                    (res[0], res[1], now, row["id"]))
                F, X = S.build(d[sp["tf"]], spiky), S.build(d[sp["ctx"]], spiky)
                ctx = S.ctx_direction(F, X, per, S.PERIOD[sp["ctx"]])
                i = F["n"] - 1
                if F["epoch"][i] + per > now:      # the newest candle is still forming
                    i -= 1
                if i < 120:
                    continue
                for setup in S.SETUPS:
                    combo = combos.get((style, setup))
                    if not combo:
                        continue
                    dirn = S._detect(setup, F, ctx, i)
                    expires = F["epoch"][i] + 3 * per
                    if not dirn or now > expires:
                        continue
                    if con.execute("SELECT 1 FROM signals WHERE instrument=? AND style=? AND setup=? AND bar_epoch=?",
                                   (key, style, setup, F["epoch"][i])).fetchone():
                        continue
                    entry, atr = F["c"][i], F["atr"][i]
                    sd = S.BT_CFG["stop_atr"] * atr
                    mean_atr = sum(F["atr"][i - 100:i]) / 100
                    risk, notes = risk_rating(setup, key, style, dirn, ctx[i], atr / mean_atr if mean_atr else None)
                    sid = con.insert(
                        "INSERT INTO signals(created_at,bar_epoch,instrument,style,setup,direction,entry,stop,target,rr,atr,"
                        "risk,risk_notes,bt_n,bt_hit,bt_be,bt_avg_r,bt_t,expires_at) "
                        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (now, F["epoch"][i], key, style, setup, dirn, entry, entry - dirn * sd,
                         entry + dirn * combo["rr"] * sd, combo["rr"], atr, risk, "; ".join(notes), combo["n"],
                         combo["hit"], combo["be"], combo["avg_r"], combo["t"], expires))
                    new.append(sid)
        con.commit()
    return new


_task = None


async def signal_loop():
    from . import push
    await asyncio.sleep(20)
    while True:
        try:
            new = await asyncio.wait_for(scan_signals(), timeout=120)
            if new:
                await asyncio.to_thread(push.notify_new, new)
        except Exception as e:
            print(f"[feed] scan failed: {type(e).__name__}: {str(e)[:150]}", flush=True)
        await asyncio.sleep(CFG["scan_seconds"])


# ---------------------------------------------------------------- member limits (self-set, with a pause)
def _prefs(con, uid: int) -> dict:
    r = con.execute("SELECT * FROM user_prefs WHERE user_id=?", (uid,)).fetchone()
    p = dict(r) if r else {}
    p.setdefault("signal_limit", None)
    if p.get("pending_limit") and p.get("pending_day") and _today() >= p["pending_day"]:
        p["signal_limit"], p["pending_limit"], p["pending_day"] = p["pending_limit"], None, None
        con.execute("UPDATE user_prefs SET signal_limit=?, pending_limit=NULL, pending_day=NULL WHERE user_id=?",
                    (p["signal_limit"], uid))
        con.commit()
    p["limit"] = min(CFG["limit_max"], p["signal_limit"] or CFG["limit_default"])
    return p


def budget(con, user: dict) -> dict:
    """Paid members: signals opened today against their own limit. Free members: their daily analyses."""
    if paid(user):
        p = _prefs(con, user["id"])
        used = con.execute("SELECT COUNT(*) AS c FROM signal_opens WHERE user_id=? AND day=? AND counted=1",
                           (user["id"], _today())).fetchone()["c"]
        return {"kind": "limit", "used": used, "limit": p["limit"], "pending": p.get("pending_limit"),
                "max": CFG["limit_max"], "paused": used >= p["limit"]}
    used, limit = auth.quota(user)
    return {"kind": "analyses", "used": used, "limit": limit, "paused": used >= limit}


def _public(row: dict, opened: bool, now: int) -> dict:
    actionable = row["status"] == "open" and now <= row["expires_at"]
    out = {"id": row["id"], "created_at": row["created_at"], "instrument": row["instrument"],
           "name": INSTRUMENTS.get(row["instrument"], ("", row["instrument"]))[1], "style": row["style"],
           "setup": row["setup"], "direction": "BUY" if row["direction"] > 0 else "SELL", "risk": row["risk"],
           "status": row["status"], "actionable": actionable, "expires_at": row["expires_at"],
           "opened": opened, "result_r": row["result_r"]}
    if opened or not actionable:       # finished setups are shown openly, as the track record
        out.update(entry=row["entry"], stop=row["stop"], target=row["target"], rr=row["rr"],
                   risk_notes=row["risk_notes"], bt={"n": row["bt_n"], "hit": row["bt_hit"], "be": row["bt_be"],
                                                      "avg_r": row["bt_avg_r"], "t": row["bt_t"]})
    return out


# ---------------------------------------------------------------- API
async def _json(request):
    return await auth._json(request)


async def view(request):
    user = auth.require_user(request)
    now = int(time.time())
    with closing(db()) as con:
        rows = con.execute("SELECT * FROM signals WHERE created_at>? ORDER BY id DESC LIMIT 60",
                           (now - CFG["view_hours"] * 3600,)).fetchall()
        opened = {r["signal_id"] for r in con.execute("SELECT signal_id FROM signal_opens WHERE user_id=?",
                                                       (user["id"],)).fetchall()}
        b = budget(con, user)
    return JSONResponse({"signals": [_public(dict(r), r["id"] in opened, now) for r in rows], "budget": b,
                         "paid": paid(user), "ts": now})


async def open_signal(request):
    user, sid = auth.require_user(request), request.path_params["sid"]
    now = int(time.time())
    with closing(db()) as con:
        row = con.execute("SELECT * FROM signals WHERE id=?", (sid,)).fetchone()
        if not row:
            raise HTTPException(404, "Signal not found")
        already = con.execute("SELECT 1 FROM signal_opens WHERE user_id=? AND signal_id=?", (user["id"], sid)).fetchone()
        actionable = row["status"] == "open" and now <= row["expires_at"]
        if not already:
            if actionable:
                b = budget(con, user)
                if b["paused"]:
                    raise HTTPException(429, (f"You have opened {b['used']} of your {b['limit']} signals today. "
                                              "You set this limit yourself, and it keeps your trading disciplined. "
                                              "New signals resume tomorrow (00:00 UTC).") if b["kind"] == "limit" else
                                        "You have used your daily analyses. Signals open again tomorrow (00:00 UTC).")
                if b["kind"] == "analyses":
                    auth.consume_quota(user)
            con.execute("INSERT INTO signal_opens(user_id,signal_id,day,created_at,counted) VALUES(?,?,?,?,?)",
                        (user["id"], sid, _today(), now, 1 if actionable else 0))
            con.commit()
        out = _public(dict(row), True, now)
        out["budget"] = budget(con, user)
    return JSONResponse(out)


async def execute_signal(request):
    """The member clicked "Open trade". Queues one order for THEIR OWN EA, with the stop and target attached.
    Lot size 0 or empty means the symbol's minimum lot, which the EA looks up."""
    from . import ea
    user, sid = auth.require_user(request), request.path_params["sid"]
    if not paid(user):
        raise HTTPException(403, "Opening trades from the app is for paid members with MT5 connected")
    d = await _json(request)
    raw = d.get("lots")
    try:
        lots = 0.0 if raw in (None, "", 0, "0") else float(raw)
    except (TypeError, ValueError):
        raise HTTPException(422, "Enter the lot size as a number, or leave it empty for the minimum lot")
    if lots < 0 or lots > 1000 or lots != lots:
        raise HTTPException(422, "That lot size is not valid")
    now = int(time.time())
    with closing(db()) as con:
        sig = con.execute("SELECT * FROM signals WHERE id=?", (sid,)).fetchone()
        if not sig:
            raise HTTPException(404, "Signal not found")
        if not con.execute("SELECT 1 FROM signal_opens WHERE user_id=? AND signal_id=?", (user["id"], sid)).fetchone():
            raise HTTPException(403, "Open the setup first. That is what counts it towards your daily limit.")
        if sig["status"] != "open" or now > sig["expires_at"]:
            raise HTTPException(409, "This setup is no longer fresh enough to trade.")
        with closing(ea.db()) as ec:
            k = ec.execute("SELECT last_seen FROM ea_keys WHERE user_id=?", (user["id"],)).fetchone()
            s = ec.execute("SELECT state, specs FROM ea_state WHERE user_id=?", (user["id"],)).fetchone()
            if not (k and k["last_seen"] and now - k["last_seen"] < 120 and s):
                raise HTTPException(409, "Your MT5 is not connected right now.")
            state = json.loads(s["state"])
            if not state["account"].get("execute_allowed"):
                raise HTTPException(409, "Switch on AllowExecute in the EA settings in MT5 to open trades from the app.")
            rules = ea.get_rules(ec, user["id"])
            if ea.risk_check(state, rules)["status"] == "STOP":
                raise HTTPException(409, f"You reached your own daily loss limit of {rules['daily_loss_pct']:g}%. Opening trades is paused for today.")
            if len(state["positions"]) >= rules["max_positions"]:
                raise HTTPException(409, f"You already have {len(state['positions'])} open positions, your own maximum.")
            name = INSTRUMENTS.get(sig["instrument"], ("", sig["instrument"]))[1]
            want = journal._norm(name)
            symbol = next((x["symbol"] for x in json.loads(s["specs"] or "[]") if journal._norm(x["symbol"]) == want), None)
            if not symbol:
                raise HTTPException(409, f"{name} was not found in your MT5 Market Watch. Add it there first.")
            for r in ec.execute("SELECT payload FROM ea_commands WHERE user_id=? AND kind='open' AND status IN "
                                "('pending','sent','done') AND created_at>?", (user["id"], now - 6 * 3600)).fetchall():
                if r["payload"] and json.loads(r["payload"]).get("signal_id") == sid:
                    raise HTTPException(409, "You already sent this setup to MT5.")
            sd = abs(sig["entry"] - sig["stop"])
            cid = ea.queue_open(ec, user["id"], {
                "symbol": symbol, "dir": "BUY" if sig["direction"] > 0 else "SELL", "lots": lots, "sl_dist": sd,
                "tp_dist": abs(sig["target"] - sig["entry"]), "ref": sig["entry"], "maxdev": CFG["exec_max_dev_r"] * sd,
                "signal_id": sid, "comment": f"TL-{sid}"})
    return JSONResponse({"command_id": cid, "symbol": symbol})


async def set_limit(request):
    user = auth.require_user(request)
    if not paid(user):
        raise HTTPException(403, "Daily limits are for paid members. Free members are limited by their daily analyses.")
    d = await _json(request)
    try:
        v = int(d.get("limit"))
    except (TypeError, ValueError):
        raise HTTPException(422, "Enter a whole number")
    if not 1 <= v <= CFG["limit_max"]:
        raise HTTPException(422, f"Choose between 1 and {CFG['limit_max']}")
    with closing(db()) as con:
        cur = _prefs(con, user["id"])["limit"]
        if v <= cur:
            con.execute("INSERT INTO user_prefs(user_id,signal_limit) VALUES(?,?) ON CONFLICT(user_id) DO UPDATE SET "
                        "signal_limit=excluded.signal_limit, pending_limit=NULL, pending_day=NULL", (user["id"], v))
            msg = "Your new limit applies now."
        else:
            tomorrow = (datetime.now(timezone.utc) + timedelta(days=1)).strftime("%Y-%m-%d")
            con.execute("INSERT INTO user_prefs(user_id,signal_limit,pending_limit,pending_day) VALUES(?,?,?,?) "
                        "ON CONFLICT(user_id) DO UPDATE SET pending_limit=excluded.pending_limit, "
                        "pending_day=excluded.pending_day", (user["id"], cur, v, tomorrow))
            msg = "A higher limit starts tomorrow. Choosing it a day ahead is part of the discipline."
        con.commit()
        b = budget(con, user)
    return JSONResponse({"ok": True, "message": msg, "budget": b})


async def notify_prefs(request):
    user = auth.require_user(request)
    if not paid(user):
        raise HTTPException(403, "Notifications are for paid members")
    with closing(db()) as con:
        if request.method == "POST":
            d = await _json(request)
            styles = [s for s in d.get("styles", []) if s in S.STYLES]
            qs, qe = d.get("quiet_start"), d.get("quiet_end")
            tz = int(d.get("tz_offset") or 0)
            ok = lambda x: x is None or (isinstance(x, int) and 0 <= x <= 23)
            if not (ok(qs) and ok(qe)) or not -840 <= tz <= 840:
                raise HTTPException(422, "Invalid quiet hours")
            con.execute("INSERT INTO user_prefs(user_id,notify_styles,quiet_start,quiet_end,tz_offset) VALUES(?,?,?,?,?) "
                        "ON CONFLICT(user_id) DO UPDATE SET notify_styles=excluded.notify_styles, "
                        "quiet_start=excluded.quiet_start, quiet_end=excluded.quiet_end, tz_offset=excluded.tz_offset",
                        (user["id"], ",".join(styles), qs, qe, tz))
            con.commit()
        p = _prefs(con, user["id"])
    return JSONResponse({"styles": [s for s in (p.get("notify_styles") or "").split(",") if s],
                         "quiet_start": p.get("quiet_start"), "quiet_end": p.get("quiet_end"),
                         "tz_offset": p.get("tz_offset") or 0})


async def record(request):
    auth.require_user(request)
    with closing(db()) as con:
        rows = con.execute("SELECT style, setup, status, result_r, rr FROM signals WHERE status<>'open'").fetchall()
        open_n = con.execute("SELECT COUNT(*) AS c FROM signals WHERE status='open'").fetchone()["c"]
    groups: dict = {}
    for r in rows:
        g = groups.setdefault((r["style"], r["setup"]), {"n": 0, "target": 0, "sum": 0.0, "rr": r["rr"]})
        g["n"] += 1; g["target"] += r["status"] == "target"; g["sum"] += r["result_r"] or 0.0
    out = [{"style": k[0], "setup": k[1], "n": g["n"], "target_rate": g["target"] / g["n"], "avg_r": g["sum"] / g["n"],
            "rr": g["rr"]} for k, g in groups.items()]
    bt = {f"{k[0]}|{k[1]}": v for k, v in allowed_combos().items()}
    return JSONResponse({"live": sorted(out, key=lambda x: -x["n"]), "backtest": bt, "open": open_n})


routes = [
    Route("/api/signals", view, methods=["GET"]),
    Route("/api/signals/record", record, methods=["GET"]),
    Route("/api/signals/limit", set_limit, methods=["POST"]),
    Route("/api/signals/notify", notify_prefs, methods=["GET", "POST"]),
    Route("/api/signals/{sid:int}/open", open_signal, methods=["POST"]),
    Route("/api/signals/{sid:int}/execute", execute_signal, methods=["POST"]),
]
