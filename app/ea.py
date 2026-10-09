"""MT5 companion: connection keys, account sync from the EA, risk rules and risk checks."""
import hashlib
import json
import math
import secrets
import time
from contextlib import closing

from starlette.exceptions import HTTPException
from starlette.responses import JSONResponse
from starlette.routing import Route

from . import auth, dbx, journal

MAX_BODY = 300_000
CMD_TTL = 60  # seconds a close command stays valid
DEFAULT_RULES = {"max_risk_pct": 1.0, "daily_loss_pct": 3.0, "max_positions": 3}
_last_report: dict[int, float] = {}
_last_poll: dict[int, float] = {}


EA_SCHEMA = """
    CREATE TABLE IF NOT EXISTS ea_keys(user_id INTEGER PRIMARY KEY, key_hash TEXT UNIQUE NOT NULL,
        created_at BIGINT NOT NULL, last_seen BIGINT);
    CREATE TABLE IF NOT EXISTS ea_state(user_id INTEGER PRIMARY KEY, state TEXT NOT NULL,
        specs TEXT, updated_at BIGINT NOT NULL);
    CREATE TABLE IF NOT EXISTS ea_rules(user_id INTEGER PRIMARY KEY, max_risk_pct DOUBLE PRECISION NOT NULL,
        daily_loss_pct DOUBLE PRECISION NOT NULL, max_positions INTEGER NOT NULL);
    CREATE TABLE IF NOT EXISTS ea_commands(id {ID}, user_id INTEGER NOT NULL,
        kind TEXT NOT NULL, ticket BIGINT, created_at BIGINT NOT NULL, status TEXT NOT NULL, result TEXT, payload TEXT);
    CREATE TABLE IF NOT EXISTS ea_manage(user_id INTEGER PRIMARY KEY, settings TEXT NOT NULL)"""
_auth_ready = False
OPEN_TTL = 20   # seconds an 'open trade' request stays valid, so a stale order can never fire later


def _migrate(con):
    con.add_column("ea_commands", "payload", "TEXT")


def db():
    global _auth_ready
    if not _auth_ready:  # the users table must exist before keys are joined to it
        auth.db().close()
        _auth_ready = True
    return dbx.connect("ea", EA_SCHEMA, _migrate)


def _hash(k: str) -> str:
    return hashlib.sha256(k.encode()).hexdigest()


def _f(v, default=0.0) -> float:
    try:
        x = float(v)
        return x if math.isfinite(x) else default
    except (TypeError, ValueError):
        return default


def get_rules(con, uid) -> dict:
    r = con.execute("SELECT max_risk_pct,daily_loss_pct,max_positions FROM ea_rules WHERE user_id=?", (uid,)).fetchone()
    return dict(r) if r else dict(DEFAULT_RULES)


def risk_check(state: dict, rules: dict) -> dict:
    acc, pos = state.get("account") or {}, state.get("positions") or []
    bal = _f(acc.get("balance"))
    pnl = _f((state.get("today") or {}).get("closed_pnl")) + sum(_f(p.get("profit")) for p in pos)
    risk = sum(_f(p.get("risk")) for p in pos)
    pct = lambda x: (x / bal * 100) if bal > 0 else 0.0
    rank, level, warns = {"OK": 0, "WARN": 1, "STOP": 2}, "OK", []

    def flag(lv, msg):
        nonlocal level
        warns.append(msg)
        if rank[lv] > rank[level]:
            level = lv

    limit = rules["daily_loss_pct"]
    if pct(-pnl) >= limit:
        flag("STOP", f"Daily loss limit of {limit:g}% reached. Stop for today.")
    elif pct(-pnl) >= limit * 0.7:
        flag("WARN", "Close to your daily loss limit.")
    nosl = [p for p in pos if _f(p.get("sl")) <= 0]
    if nosl:
        flag("WARN", f"{len(nosl)} open position(s) have no stop loss.")
    big = [p for p in pos if pct(_f(p.get("risk"))) > rules["max_risk_pct"]]
    if big:
        flag("WARN", f"{len(big)} position(s) risk more than {rules['max_risk_pct']:g}%.")
    if len(pos) > rules["max_positions"]:
        flag("WARN", f"{len(pos)} open positions (your max is {rules['max_positions']}).")
    mode = "DEMO" if acc.get("demo") else "LIVE"
    return {
        "status": level,
        "line1": f"{mode} | Open {len(pos)} | Risk {risk:.2f} ({pct(risk):.1f}%)",
        "line2": f"Today {pnl:+.2f} ({pct(pnl):+.1f}%) | limit -{limit:g}%",
        "line3": warns[0] if warns else "All checks passed.",
        "warnings": warns, "risk_money": round(risk, 2), "risk_pct": round(pct(risk), 2),
        "today_pnl": round(pnl, 2), "today_pct": round(pct(pnl), 2),
    }


async def report(request):
    """Called by the MT5 EA with the connection key. No browser login involved."""
    key = request.headers.get("x-driverline-key", "")
    if not key:
        raise HTTPException(401, "Missing connection key")
    raw = await request.body()
    if len(raw) > MAX_BODY:
        raise HTTPException(413, "Payload too large")
    try:
        d = json.loads(raw or b"{}")
    except ValueError:
        raise HTTPException(400, "Invalid JSON")
    acc = d.get("account") if isinstance(d, dict) else None
    if not isinstance(acc, dict):
        raise HTTPException(422, "account is required")
    with closing(db()) as con:
        r = con.execute("SELECT k.user_id, u.blocked FROM ea_keys k JOIN users u ON u.id=k.user_id "
                        "WHERE k.key_hash=?", (_hash(key),)).fetchone()
        if not r or r["blocked"]:
            raise HTTPException(401, "Invalid or revoked connection key")
        uid, now = r["user_id"], time.time()
        if now - _last_report.get(uid, 0) < 3:
            raise HTTPException(429, "Too many reports; wait a few seconds")
        _last_report[uid] = now
        state = {
            "account": {k: acc.get(k) for k in ("login", "server", "currency", "balance", "equity", "margin_free", "demo",
                                           "remote_close", "manage_allowed", "execute_allowed", "server_offset")},
            "today": d.get("today") if isinstance(d.get("today"), dict) else {},
            "positions": [p for p in (d.get("positions") or []) if isinstance(p, dict)][:100],
        }
        specs = d.get("specs")
        specs_json = json.dumps([s for s in specs if isinstance(s, dict)][:100]) if isinstance(specs, list) else None
        con.execute("INSERT INTO ea_state(user_id,state,specs,updated_at) VALUES(?,?,?,?) "
                    "ON CONFLICT(user_id) DO UPDATE SET state=excluded.state, "
                    "specs=COALESCE(excluded.specs, ea_state.specs), updated_at=excluded.updated_at",
                    (uid, json.dumps(state), specs_json, int(now)))
        con.execute("UPDATE ea_keys SET last_seen=? WHERE user_id=?", (int(now), uid))
        con.commit()
        rules = get_rules(con, uid)
    closed = [c for c in (d.get("closed") or []) if isinstance(c, dict)] if isinstance(d.get("closed"), list) else []
    out = risk_check(state, rules)
    out["journal"] = journal.import_mt5(uid, acc.get("login"), closed, int(_f(acc.get("server_offset")))) if closed else 0
    return JSONResponse(out)


def _key_user(con, request) -> int:
    key = request.headers.get("x-driverline-key", "")
    if not key:
        raise HTTPException(401, "Missing connection key")
    r = con.execute("SELECT k.user_id, u.blocked FROM ea_keys k JOIN users u ON u.id=k.user_id "
                    "WHERE k.key_hash=?", (_hash(key),)).fetchone()
    if not r or r["blocked"]:
        raise HTTPException(401, "Invalid or revoked connection key")
    return r["user_id"]


async def command_create(request):
    """A signed-in member asks their own EA to close one position or all positions."""
    u = auth.require_user(request)
    d = await auth._json(request)
    kind = d.get("kind")
    if kind not in ("close", "close_all"):
        raise HTTPException(422, "kind must be close or close_all")
    now = int(time.time())
    with closing(db()) as con:
        k = con.execute("SELECT last_seen FROM ea_keys WHERE user_id=?", (u["id"],)).fetchone()
        s = con.execute("SELECT state FROM ea_state WHERE user_id=?", (u["id"],)).fetchone()
        if not (k and k["last_seen"] and now - k["last_seen"] < 120 and s):
            raise HTTPException(409, "Your MT5 is not connected right now")
        st = json.loads(s["state"])
        if not st["account"].get("remote_close"):
            raise HTTPException(409, "Remote close is switched off in your EA settings (AllowRemoteClose)")
        positions, ticket = st["positions"], None
        if kind == "close":
            try:
                ticket = int(d.get("ticket"))
            except (TypeError, ValueError):
                raise HTTPException(422, "ticket must be a number")
            if ticket not in {int(_f(p.get("ticket"))) for p in positions}:
                raise HTTPException(404, "That position is not open any more")
        elif not positions:
            raise HTTPException(409, "There are no open positions")
        con.execute("UPDATE ea_commands SET status='expired' WHERE user_id=? AND status='pending' AND created_at<?",
                    (u["id"], now - CMD_TTL))
        if con.execute("SELECT 1 FROM ea_commands WHERE user_id=? AND status IN ('pending','sent') AND created_at>?",
                       (u["id"], now - CMD_TTL)).fetchone():
            raise HTTPException(409, "Another close is still in progress")
        cid = con.insert("INSERT INTO ea_commands(user_id,kind,ticket,created_at,status) VALUES(?,?,?,?,'pending')",
                         (u["id"], kind, ticket, now))
        con.commit()
    return JSONResponse({"id": cid})


async def poll(request):
    """The EA asks: is there a close command waiting for me?"""
    with closing(db()) as con:
        uid = _key_user(con, request)
        now = time.time()
        if now - _last_poll.get(uid, 0) < 1:
            raise HTTPException(429, "Polling too fast")
        _last_poll[uid] = now
        con.execute("UPDATE ea_keys SET last_seen=? WHERE user_id=?", (int(now), uid))
        con.execute("UPDATE ea_commands SET status='expired' WHERE user_id=? AND status='pending' AND created_at<?",
                    (uid, int(now) - CMD_TTL))
        con.execute("UPDATE ea_commands SET status='expired' WHERE user_id=? AND status='pending' AND kind='open' "
                    "AND created_at<?", (uid, int(now) - OPEN_TTL))
        r = con.execute("SELECT id,kind,ticket,payload FROM ea_commands WHERE user_id=? AND status='pending' "
                        "ORDER BY id LIMIT 1", (uid,)).fetchone()
        if r:
            con.execute("UPDATE ea_commands SET status='sent' WHERE id=?", (r["id"],))
        con.commit()
        m = get_manage(con, uid)
    flat = {"be_on": "1" if m["be_on"] else "0", "be_trigger": str(m["be_trigger"]), "be_offset": str(m["be_offset"]),
            "tsl_on": "1" if m["tsl_on"] else "0", "tsl_start": str(m["tsl_start"]), "tsl_dist": str(m["tsl_dist"]),
            "ttp_on": "1" if m["ttp_on"] else "0", "ttp_near": str(m["ttp_near"]), "ttp_step": str(m["ttp_step"]),
            "excluded": ",".join(str(t) for t in m["excluded"])}
    if r and r["kind"] == "open" and r["payload"]:
        p = json.loads(r["payload"])
        flat.update(sym=p["symbol"], dir=p["dir"], lots=str(p["lots"]), sl_dist=str(p["sl_dist"]),
                    tp_dist=str(p["tp_dist"]), ref=str(p["ref"]), maxdev=str(p["maxdev"]), cmt=p["comment"])
    return JSONResponse({"cmd": r["kind"] if r else "", "id": r["id"] if r else 0,
                         "ticket": (r["ticket"] or 0) if r else 0, **flat})


async def result(request):
    d = await auth._json(request)
    with closing(db()) as con:
        uid = _key_user(con, request)
        try:
            cid = int(d["id"])
        except (KeyError, TypeError, ValueError):
            raise HTTPException(422, "id is required")
        res = json.dumps({"closed": int(_f(d.get("closed"))), "failed": int(_f(d.get("failed"))),
                          "message": str(d.get("message", ""))[:200]})
        con.execute("UPDATE ea_commands SET status=?, result=? WHERE id=? AND user_id=? AND status='sent'",
                    ("done" if d.get("ok") else "failed", res, cid, uid))
        con.commit()
    return JSONResponse({"ok": True})


MANAGE_DEFAULT = {"be_on": False, "be_trigger": 1.0, "be_offset": 0.1,
                  "tsl_on": False, "tsl_start": 1.5, "tsl_dist": 1.0,
                  "ttp_on": False, "ttp_near": 0.3, "ttp_step": 1.0, "excluded": []}
MANAGE_RANGES = {"be_trigger": (0.3, 3.0), "be_offset": (0.0, 0.5), "tsl_start": (0.5, 5.0), "tsl_dist": (0.3, 3.0),
                 "ttp_near": (0.1, 1.5), "ttp_step": (0.3, 3.0)}


def get_manage(con, uid) -> dict:
    r = con.execute("SELECT settings FROM ea_manage WHERE user_id=?", (uid,)).fetchone()
    return {**MANAGE_DEFAULT, **(json.loads(r["settings"]) if r else {})}


def _save_manage(con, uid, s):
    con.execute("INSERT INTO ea_manage(user_id,settings) VALUES(?,?) ON CONFLICT(user_id) DO UPDATE SET settings=excluded.settings",
                (uid, json.dumps(s)))
    con.commit()


async def manage_get(request):
    u = auth.require_user(request)
    with closing(db()) as con:
        return JSONResponse(get_manage(con, u["id"]))


async def manage_save(request):
    """Switches and distances for breakeven, trailing stop and trailing take-profit. Distances are in R, where
    1R is the original stop distance of the position."""
    u = auth.require_user(request)
    d = await auth._json(request)
    with closing(db()) as con:
        s = get_manage(con, u["id"])
        for k in ("be_on", "tsl_on", "ttp_on"):
            if k in d:
                if not isinstance(d[k], bool):
                    raise HTTPException(422, f"{k} must be true or false")
                s[k] = d[k]
        for k, (lo, hi) in MANAGE_RANGES.items():
            if k in d:
                v = _f(d[k], None)
                if v is None or not lo <= v <= hi:
                    raise HTTPException(422, f"{k} must be between {lo:g} and {hi:g}")
                s[k] = v
        if s["tsl_dist"] > s["tsl_start"]:
            raise HTTPException(422, "The trailing distance cannot be larger than the profit needed to start trailing")
        _save_manage(con, u["id"], s)
    return JSONResponse(s)


async def manage_exclude(request):
    """Switch auto-manage off (or on) for one open position."""
    u = auth.require_user(request)
    d = await auth._json(request)
    try:
        ticket = int(d.get("ticket"))
    except (TypeError, ValueError):
        raise HTTPException(422, "ticket must be a number")
    with closing(db()) as con:
        s = get_manage(con, u["id"])
        ex = {int(t) for t in s["excluded"]}
        (ex.add if d.get("excluded") else ex.discard)(ticket)
        s["excluded"] = sorted(ex)[-200:]
        _save_manage(con, u["id"], s)
    return JSONResponse(s)


def queue_open(con, uid: int, payload: dict) -> int:
    """Queue ONE 'open trade' request for this member's EA. Called only after the member clicked the button."""
    now = int(time.time())
    con.execute("UPDATE ea_commands SET status='expired' WHERE user_id=? AND status='pending' AND created_at<?",
                (uid, now - CMD_TTL))
    if con.execute("SELECT 1 FROM ea_commands WHERE user_id=? AND status IN ('pending','sent') AND created_at>?",
                   (uid, now - CMD_TTL)).fetchone():
        raise HTTPException(409, "Another request is still in progress. Wait a moment and try again.")
    cid = con.insert("INSERT INTO ea_commands(user_id,kind,ticket,created_at,status,payload) VALUES(?,?,?,?,'pending',?)",
                     (uid, "open", None, now, json.dumps(payload)))
    con.commit()
    return cid


async def key_create(request):
    u = auth.require_user(request)
    raw = "dlk_" + secrets.token_urlsafe(24)
    with closing(db()) as con:
        con.execute("INSERT INTO ea_keys(user_id,key_hash,created_at) VALUES(?,?,?) "
                    "ON CONFLICT(user_id) DO UPDATE SET key_hash=excluded.key_hash, "
                    "created_at=excluded.created_at, last_seen=NULL", (u["id"], _hash(raw), int(time.time())))
        con.commit()
    return JSONResponse({"key": raw})


async def key_revoke(request):
    u = auth.require_user(request)
    with closing(db()) as con:
        con.execute("DELETE FROM ea_keys WHERE user_id=?", (u["id"],))
        con.commit()
    return JSONResponse({"ok": True})


async def status(request):
    u = auth.require_user(request)
    with closing(db()) as con:
        k = con.execute("SELECT last_seen FROM ea_keys WHERE user_id=?", (u["id"],)).fetchone()
        s = con.execute("SELECT state, updated_at FROM ea_state WHERE user_id=?", (u["id"],)).fetchone()
        rules = get_rules(con, u["id"])
        mg = get_manage(con, u["id"])
        cmd = con.execute("SELECT id,kind,ticket,status,result,created_at FROM ea_commands WHERE user_id=? "
                          "ORDER BY id DESC LIMIT 1", (u["id"],)).fetchone()
    now = time.time()
    seen = k["last_seen"] if k else None
    out = {"has_key": bool(k), "connected": bool(seen and now - seen < 120),
           "last_seen_ago": int(now - seen) if seen else None, "rules": rules, "manage": mg}
    if s:
        st = json.loads(s["state"])
        out.update(account=st["account"], today=st["today"], positions=st["positions"], check=risk_check(st, rules))
        out["can_close"] = bool(out["connected"] and st["account"].get("remote_close"))
        out["can_manage"] = bool(out["connected"] and st["account"].get("manage_allowed"))
        out["can_execute"] = bool(out["connected"] and st["account"].get("execute_allowed"))
    if cmd and now - cmd["created_at"] < 600:
        stat = cmd["status"]
        if stat == "pending" and now - cmd["created_at"] > CMD_TTL:
            stat = "expired"
        elif stat == "sent" and now - cmd["created_at"] > CMD_TTL:
            stat = "no reply"
        out["command"] = {"id": cmd["id"], "kind": cmd["kind"], "ticket": cmd["ticket"], "status": stat,
                          "result": json.loads(cmd["result"]) if cmd["result"] else None}
    return JSONResponse(out)


async def rules_save(request):
    u = auth.require_user(request)
    d = await auth._json(request)
    try:
        mr, dl, mp = float(d["max_risk_pct"]), float(d["daily_loss_pct"]), int(d["max_positions"])
    except (KeyError, TypeError, ValueError):
        raise HTTPException(422, "Enter all three numbers")
    if not (0.1 <= mr <= 10 and 0.5 <= dl <= 20 and 1 <= mp <= 50):
        raise HTTPException(422, "Risk per trade 0.1 to 10%, daily loss 0.5 to 20%, positions 1 to 50")
    with closing(db()) as con:
        con.execute("INSERT INTO ea_rules VALUES(?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET "
                    "max_risk_pct=excluded.max_risk_pct, daily_loss_pct=excluded.daily_loss_pct, "
                    "max_positions=excluded.max_positions", (u["id"], mr, dl, mp))
        con.commit()
    return JSONResponse({"ok": True})


async def specs(request):
    u = auth.require_user(request)
    with closing(db()) as con:
        s = con.execute("SELECT specs FROM ea_state WHERE user_id=?", (u["id"],)).fetchone()
    return JSONResponse(json.loads(s["specs"]) if s and s["specs"] else [])


routes = [
    Route("/api/ea/report", report, methods=["POST"]),
    Route("/api/ea/poll", poll, methods=["POST"]),
    Route("/api/ea/result", result, methods=["POST"]),
    Route("/api/ea/command", command_create, methods=["POST"]),
    Route("/api/ea/key", key_create, methods=["POST"]),
    Route("/api/ea/key", key_revoke, methods=["DELETE"]),
    Route("/api/ea/status", status, methods=["GET"]),
    Route("/api/ea/rules", rules_save, methods=["POST"]),
    Route("/api/ea/manage", manage_get, methods=["GET"]),
    Route("/api/ea/manage", manage_save, methods=["POST"]),
    Route("/api/ea/manage/exclude", manage_exclude, methods=["POST"]),
    Route("/api/ea/specs", specs, methods=["GET"]),
]
