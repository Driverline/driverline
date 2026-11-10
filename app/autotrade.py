"""AutoTrading: TradeLens places the trades from published signals by itself, inside limits the trader sets.

The trader picks, per MT5 account, which Signal types (Scalp, Day, Swing, H4 gold) and which symbols may be traded, how big each trade is,
and the most that may be open / opened per day. Nothing here changes how a trade is placed or protected: an auto trade is an ordinary
'open' command for the member's OWN EA, with the signal's stop and target attached, and it goes through exactly the same checks as the
"Open trade" button (AllowExecute, the member's own daily loss limit and position cap, equity-protection pre-check). Auto manage and
equity protection then look after it like any other trade.

Pull model, like copy trading (copy_trading.py):
  1. on_new_signals(): after each scan, one 'waiting' job per matching account (unique per signal and account).
  2. dispatch(): called from the EA poll. Turns at most ONE waiting job of that account into an 'open' command, or skips it with a reason.
     A job is never placed late (JOB_TTL), never when the signal is no longer open, and never twice.
  3. on_report(): follows the command (sent -> open -> closed / failed) and finds the trade's ticket.

Access: private for now. ea_meta 'auto_mode' is 'testers' (admin plus the listed testers, the default) or 'off' (nobody).
There is deliberately no public mode yet (see docs/AUTOTRADING.md).
"""
import json
import time
from contextlib import closing
from datetime import datetime, timezone

from starlette.exceptions import HTTPException
from starlette.responses import JSONResponse
from starlette.routing import Route

from . import auth, credits, ea, journal
from .config import INSTRUMENTS, match_key

JOB_TTL = 120            # seconds a job may wait for the EA to collect it; after that it is skipped
TICKET_WAIT = 60         # seconds to look for the new position before giving up on finding its ticket
MODES = ("fixed", "risk")
RATINGS = ("Low", "Medium", "High")
STYLE_ORDER = ("scalp", "day", "swing", "h4")
STYLE_LABEL = {"scalp": "Scalp", "day": "Day", "swing": "Swing", "h4": "H4 gold"}
DEFAULTS = {"enabled": 0, "styles": [], "instruments": [], "mode": "risk", "fixed_lots": None, "risk_pct": 1.0, "max_risk_pct": 2.0,
            "max_open": 3, "max_per_day": 5, "max_rating": "Medium"}
LIMITS = {"max_open": (1, 10), "max_per_day": (1, 50)}


def _now() -> int:
    return int(time.time())


def _f(v, d=0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return d


def _day_start(now: int) -> int:
    return now - now % 86400


# ---------------------------------------------------------------- who may use it
def mode(con) -> str:
    r = con.execute("SELECT v FROM ea_meta WHERE k='auto_mode'").fetchone()
    return r["v"] if r and r["v"] in ("off", "testers") else "testers"


def testers(con) -> list:
    r = con.execute("SELECT v FROM ea_meta WHERE k='auto_testers'").fetchone()
    try:
        return [int(x) for x in json.loads(r["v"])] if r and r["v"] else []
    except (ValueError, TypeError):
        return []


def _user(uid: int):
    with closing(auth.db()) as con:
        r = con.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    return dict(r) if r and not r["blocked"] else None


def allowed(con, user: dict) -> str:
    """'' when this member may use AutoTrading, otherwise the reason (shown to nobody but the admin)."""
    from . import signal_feed
    if mode(con) == "off":
        return "AutoTrading is switched off"
    if user["tier"] != "admin" and user["id"] not in testers(con):
        return "AutoTrading is not available yet"
    if not (signal_feed.paid(user) or credits.entitled(user, "mt5")):
        return "AutoTrading is for paid members with MT5 connected"
    return ""


# ---------------------------------------------------------------- settings
def _row_settings(r) -> dict:
    s = dict(DEFAULTS)
    if r:
        s.update({k: r[k] for k in ("enabled", "mode", "fixed_lots", "risk_pct", "max_risk_pct", "max_open", "max_per_day", "max_rating")
                  if r[k] is not None})
        for k in ("styles", "instruments"):
            try:
                s[k] = [str(x) for x in json.loads(r[k] or "[]")]
            except ValueError:
                s[k] = []
    s["enabled"] = int(bool(s["enabled"]))
    return s


def get_settings(con, aid: int) -> dict:
    return _row_settings(con.execute("SELECT * FROM auto_settings WHERE account_id=?", (aid,)).fetchone())


def catalog() -> dict:
    from . import signal_feed as F
    have = {k[1] for k in F.allowed_combos()}
    if F.CFG.get("validated_enabled"):
        have |= {k[1] for k in F.VALIDATED}
    styles = [{"key": k, "label": STYLE_LABEL[k]} for k in STYLE_ORDER if k in have]
    paused = set(F.CFG.get("paused_instruments") or [])
    ins = [{"key": k, "name": v[1]} for k, v in INSTRUMENTS.items() if k not in paused]
    return {"styles": styles, "instruments": ins}


# ---------------------------------------------------------------- 1. new signals -> waiting jobs
def on_new_signals(ids: list) -> int:
    """Called after each scan. One waiting job per enabled account whose choices match the signal. Returns how many were created."""
    from . import signal_feed as F
    made, now = 0, _now()
    if not ids:
        return 0
    with closing(F.db()) as fc:
        sigs = [dict(r) for sid in ids for r in fc.execute("SELECT * FROM signals WHERE id=?", (sid,)).fetchall()]
    with closing(ea.db()) as con:
        rows = [dict(r) for r in con.execute("SELECT * FROM auto_settings WHERE enabled=1").fetchall()]
        ok_user: dict = {}
        for r in rows:
            s = _row_settings(r)
            uid = r["user_id"]
            if uid not in ok_user:
                u = _user(uid)
                ok_user[uid] = bool(u and not allowed(con, u))
            if not ok_user[uid]:
                continue
            for sig in sigs:
                if sig["style"] not in s["styles"] or sig["instrument"] not in s["instruments"]:
                    continue
                if RATINGS.index(sig["risk"] if sig["risk"] in RATINGS else "High") > RATINGS.index(s["max_rating"]):
                    continue
                if con.execute("SELECT 1 FROM auto_jobs WHERE signal_id=? AND account_id=?", (sig["id"], r["account_id"])).fetchone():
                    continue
                con.execute("INSERT INTO auto_jobs(signal_id,account_id,user_id,status,created_at,updated_at,symbol,dir) "
                            "VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(signal_id, account_id) DO NOTHING",
                            (sig["id"], r["account_id"], uid, "waiting", now, now, sig["instrument"],
                             "BUY" if sig["direction"] > 0 else "SELL"))
                made += 1
        con.commit()
    return made


# ---------------------------------------------------------------- 2. the EA asked for work
def _state(con, aid):
    r = con.execute("SELECT state, specs FROM ea_acct_state WHERE account_id=?", (aid,)).fetchone()
    if not r:
        return None, []
    return json.loads(r["state"]), [x for x in json.loads(r["specs"] or "[]") if isinstance(x, dict)]


def _tickets(st) -> set:
    return {int(_f(p.get("ticket"))) for p in (st or {}).get("positions", [])}


def _set(con, jid, **kw):
    kw["updated_at"] = _now()
    con.execute(f"UPDATE auto_jobs SET {','.join(f'{k}=?' for k in kw)} WHERE id=?", (*kw.values(), jid))


def dispatch(con, aid: int):
    """Called from poll(): turn at most ONE waiting auto job of this account into an open command (or skip it, saying why)."""
    from . import copy_trading, signal_feed as F
    now = _now()
    if con.execute("SELECT 1 FROM ea_commands WHERE account_id=? AND status IN ('pending','sent') AND created_at>?",
                   (aid, now - ea.CMD_TTL)).fetchone():
        return
    job = con.execute("SELECT * FROM auto_jobs WHERE account_id=? AND status='waiting' ORDER BY id LIMIT 1", (aid,)).fetchone()
    if not job:
        return
    job = dict(job)

    def skip(why):
        _set(con, job["id"], status="skipped", note=why)
        con.commit()

    if now - job["created_at"] > JOB_TTL:
        return skip("Your EA was not connected in time, so this trade was skipped (auto trades are never placed late)")
    acct = con.execute("SELECT * FROM ea_accounts WHERE id=?", (aid,)).fetchone()
    s = get_settings(con, aid)
    user = _user(job["user_id"])
    if not acct or not s["enabled"]:
        return skip("AutoTrading is off for this account")
    if not user or allowed(con, user):
        return skip("AutoTrading is not available on your account")
    with closing(F.db()) as fc:
        sig = fc.execute("SELECT * FROM signals WHERE id=?", (job["signal_id"],)).fetchone()
    if not sig or sig["status"] != "open" or now > sig["expires_at"]:
        return skip("The setup was no longer fresh enough to trade")
    if sig["style"] not in s["styles"] or sig["instrument"] not in s["instruments"]:
        return skip("You changed your AutoTrading choices before this trade was placed")
    st, specs = _state(con, aid)
    if not st or not ea._connected(dict(acct), now):
        return skip("Your MT5 was not connected")
    acc = st["account"]
    if not acc.get("execute_allowed"):
        return skip("Trading from the app is not enabled on your account (AllowExecute is off)")
    rules = ea.get_rules(con, aid)
    if ea.risk_check(st, rules)["status"] == "STOP":
        return skip(f"You reached your own daily loss limit of {rules['daily_loss_pct']:g}%")
    if len(st["positions"]) >= rules["max_positions"]:
        return skip(f"You already have {len(st['positions'])} open positions, your own maximum")
    live = con.execute("SELECT COUNT(*) AS c FROM auto_jobs WHERE account_id=? AND status IN ('sent','open')", (aid,)).fetchone()["c"]
    if live >= s["max_open"]:
        return skip(f"{live} auto trades are already open, your AutoTrading maximum")
    today = con.execute("SELECT COUNT(*) AS c FROM auto_jobs WHERE account_id=? AND placed_at>=?", (aid, _day_start(now))).fetchone()["c"]
    if today >= s["max_per_day"]:
        return skip(f"{today} auto trades were already placed today, your AutoTrading daily maximum")
    name = INSTRUMENTS.get(sig["instrument"], ("", sig["instrument"]))[1]
    want = journal._norm(name)
    spec = (next((x for x in specs if journal._norm(str(x.get("symbol", ""))) == want), None)
            or next((x for x in specs if match_key(str(x.get("symbol", ""))) == sig["instrument"]), None))
    if not spec:
        return skip(f"{name} was not found in your MT5 Market Watch")
    if any(p.get("symbol") == spec["symbol"] for p in st["positions"]):
        return skip(f"You already have an open {spec['symbol']} trade, so AutoTrading did not add another")
    sd = abs(sig["entry"] - sig["stop"])
    cap = min([c for c in (_f(s["max_risk_pct"]), _f(rules.get("max_risk_pct"))) if c > 0] or [0])
    lots, pct, why = copy_trading.size(spec, _f(acc.get("balance")), sd, s["mode"], s["fixed_lots"], s["risk_pct"], cap)
    if lots is None:
        _set(con, job["id"], status="skipped", note=why, risk_pct=pct)
        con.commit()
        return
    blocked = ea.protect_precheck(acc, ea.get_manage(con, aid), spec, lots, sd, name)
    if blocked:
        return skip(blocked)
    payload = {"symbol": spec["symbol"], "dir": "BUY" if sig["direction"] > 0 else "SELL", "lots": lots, "sl_dist": sd,
               "tp_dist": abs(sig["target"] - sig["entry"]), "ref": sig["entry"], "maxdev": F.CFG["exec_max_dev_r"] * sd,
               "signal_id": sig["id"], "comment": f"TLA-{sig['id']}", "auto_job": job["id"]}
    cid = con.insert("INSERT INTO ea_commands(user_id,account_id,kind,ticket,created_at,status,payload) VALUES(?,?,?,?,?,'pending',?)",
                     (job["user_id"], aid, "open", None, now, json.dumps(payload)))
    _set(con, job["id"], status="sent", command_id=cid, lots=lots, risk_pct=pct, placed_at=now, symbol=spec["symbol"],
         dir=payload["dir"], before=json.dumps(sorted(_tickets(st))))
    con.commit()


# ---------------------------------------------------------------- 3. the EA reported
def _cmd_msg(con, cid) -> str:
    r = con.execute("SELECT result FROM ea_commands WHERE id=?", (cid,)).fetchone()
    try:
        return str(json.loads(r["result"]).get("message") or "") if r and r["result"] else ""
    except ValueError:
        return ""


def on_report(con, aid: int, state: dict):
    now, have = _now(), _tickets(state)
    for j in con.execute("SELECT * FROM auto_jobs WHERE account_id=? AND status IN ('sent','open')", (aid,)).fetchall():
        j = dict(j)
        if j["status"] == "sent":
            c = con.execute("SELECT status FROM ea_commands WHERE id=?", (j["command_id"],)).fetchone()
            if not c or c["status"] in ("failed", "expired"):
                _set(con, j["id"], status="failed", note=_cmd_msg(con, j["command_id"]) or "Your EA did not place the trade")
            elif c["status"] == "done":
                _set(con, j["id"], status="open")
                j["status"], j["updated_at"] = "open", now
        if j["status"] == "open":
            if j["ticket"] is None:
                before = set(json.loads(j["before"] or "[]"))
                cand = [p for p in state.get("positions", []) if int(_f(p.get("ticket"))) not in before and p.get("symbol") == j["symbol"]
                        and str(p.get("type")).upper() == j["dir"]]
                if len(cand) > 1:
                    cand = [p for p in cand if abs(_f(p.get("volume")) - _f(j["lots"])) < 1e-6] or cand
                if len(cand) == 1:
                    _set(con, j["id"], ticket=int(_f(cand[0].get("ticket"))))
                elif not cand and now - j["updated_at"] > TICKET_WAIT:
                    _set(con, j["id"], status="closed")                      # opened and already closed (stop or target hit)
            elif j["ticket"] not in have:
                _set(con, j["id"], status="closed")
    con.commit()


# ---------------------------------------------------------------- member API
def _job_out(j: dict) -> dict:
    sig_name = INSTRUMENTS.get(j["symbol"], ("", j["symbol"]))[1] if j.get("symbol") in INSTRUMENTS else j.get("symbol")
    return {"id": j["id"], "signal_id": j["signal_id"], "account": j["account_id"], "status": j["status"], "note": j["note"] or "",
            "symbol": sig_name, "dir": j["dir"], "lots": j["lots"], "risk_pct": j["risk_pct"], "at": j["created_at"]}


async def me_get(request):
    u = auth.require_user(request)
    with closing(ea.db()) as con:
        why = allowed(con, u)
        if why:
            return JSONResponse({"available": False})
        accts = [{"id": a["id"], "label": a["label"], "settings": get_settings(con, a["id"]),
                  "acknowledged": bool((con.execute("SELECT ack_at FROM auto_settings WHERE account_id=?", (a["id"],)).fetchone() or {"ack_at": None})["ack_at"]),
                  "own_limit_pct": ea.get_rules(con, a["id"])["max_risk_pct"]} for a in ea.user_accounts(con, u["id"])]
        jobs = [_job_out(dict(r)) for r in con.execute("SELECT * FROM auto_jobs WHERE user_id=? ORDER BY id DESC LIMIT 30", (u["id"],)).fetchall()]
    return JSONResponse({"available": True, "accounts": accts, "catalog": catalog(), "recent": jobs, "defaults": DEFAULTS,
                         "ratings": list(RATINGS), "now": _now()})


def _num(d, k, lo, hi):
    v = d.get(k)
    if v in (None, ""):
        return None
    try:
        v = float(v)
    except (TypeError, ValueError):
        raise HTTPException(422, f"{k} must be a number")
    if not lo <= v <= hi or v != v:
        raise HTTPException(422, f"{k} must be between {lo:g} and {hi:g}")
    return v


def _int(d, k):
    lo, hi = LIMITS[k]
    try:
        v = int(d.get(k, DEFAULTS[k]))
    except (TypeError, ValueError):
        raise HTTPException(422, f"{k} must be a whole number")
    if not lo <= v <= hi:
        raise HTTPException(422, f"{k} must be between {lo} and {hi}")
    return v


async def me_settings(request):
    u = auth.require_user(request)
    d = await auth._json(request)
    cat = catalog()
    styles = d.get("styles") or []
    ins = d.get("instruments") or []
    if not isinstance(styles, list) or not isinstance(ins, list):
        raise HTTPException(422, "Choose signal types and symbols from the lists")
    if any(x not in {c["key"] for c in cat["styles"]} for x in styles):
        raise HTTPException(422, "One of those signal types is not available")
    if any(x not in {c["key"] for c in cat["instruments"]} for x in ins):
        raise HTTPException(422, "One of those symbols is not available")
    md = d.get("mode", "risk")
    if md not in MODES:
        raise HTTPException(422, "mode must be fixed or risk")
    rating = d.get("max_rating", DEFAULTS["max_rating"])
    if rating not in RATINGS:
        raise HTTPException(422, "Choose Low, Medium or High")
    fixed, risk, cap = _num(d, "fixed_lots", 0.0001, 1000), _num(d, "risk_pct", 0.05, 10), _num(d, "max_risk_pct", 0.05, 10)
    if md == "risk" and risk is None:
        raise HTTPException(422, "risk_pct is required for percentage risk")
    mo, mpd = _int(d, "max_open"), _int(d, "max_per_day")
    enabled = bool(d.get("enabled"))
    if enabled and not (styles and ins):
        raise HTTPException(422, "Choose at least one signal type and one symbol before switching AutoTrading on")
    now = _now()
    with closing(ea.db()) as con:
        why = allowed(con, u)
        if why:
            raise HTTPException(403, why)
        a = ea.resolve(con, u["id"], ea._acct_param(request, d), explicit=True)
        old = con.execute("SELECT ack_at FROM auto_settings WHERE account_id=?", (a["id"],)).fetchone()
        ack = old["ack_at"] if old and old["ack_at"] else None
        if enabled and not ack:
            if d.get("acknowledge") is not True:
                raise HTTPException(422, "Please tick the box to confirm you understand AutoTrading places real trades for you", )
            ack = now
        con.execute("INSERT INTO auto_settings(account_id,user_id,enabled,styles,instruments,mode,fixed_lots,risk_pct,max_risk_pct,max_open,"
                    "max_per_day,max_rating,ack_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(account_id) DO UPDATE SET enabled=excluded.enabled, styles=excluded.styles, instruments=excluded.instruments, "
                    "mode=excluded.mode, fixed_lots=excluded.fixed_lots, risk_pct=excluded.risk_pct, max_risk_pct=excluded.max_risk_pct, "
                    "max_open=excluded.max_open, max_per_day=excluded.max_per_day, max_rating=excluded.max_rating, ack_at=excluded.ack_at, "
                    "updated_at=excluded.updated_at",
                    (a["id"], u["id"], int(enabled), json.dumps(styles), json.dumps(ins), md, fixed, risk, cap, mo, mpd, rating, ack, now))
        if not enabled:                                       # pausing cancels anything still waiting
            con.execute("UPDATE auto_jobs SET status='skipped', note='You paused AutoTrading', updated_at=? WHERE account_id=? AND status='waiting'",
                        (now, a["id"]))
        con.commit()
        out = get_settings(con, a["id"])
    return JSONResponse({"ok": True, "account": a["id"], "settings": out})


# ---------------------------------------------------------------- admin
async def admin_get(request):
    auth.require_admin(request)
    with closing(ea.db()) as con:
        ids = testers(con)
        m = mode(con)
        on = con.execute("SELECT COUNT(*) AS c FROM auto_settings WHERE enabled=1").fetchone()["c"]
        recent = con.execute("SELECT status, COUNT(*) AS c FROM auto_jobs WHERE created_at>? GROUP BY status", (_now() - 7 * 86400,)).fetchall()
    with closing(auth.db()) as ac:
        emails = [r["email"] for r in ac.execute(f"SELECT email FROM users WHERE id IN ({','.join('?' * len(ids))})", ids).fetchall()] if ids else []
    return JSONResponse({"mode": m, "testers": emails, "accounts_on": on, "jobs_7d": {r["status"]: r["c"] for r in recent}})


async def admin_set(request):
    auth.require_admin(request)
    d = await auth._json(request)
    m = d.get("mode")
    if m not in ("off", "testers"):
        raise HTTPException(422, "mode must be off or testers. There is no public mode yet.")
    raw = d.get("testers") or []
    if not isinstance(raw, list) or len(raw) > 50:
        raise HTTPException(422, "testers must be a list of e-mail addresses (up to 50)")
    ids, missing = [], []
    with closing(auth.db()) as ac:
        for e in raw:
            r = ac.execute("SELECT id FROM users WHERE lower(email)=?", (str(e).strip().lower(),)).fetchone()
            (ids if r else missing).append(r["id"] if r else str(e))
    if missing:
        raise HTTPException(422, "No member with: " + ", ".join(missing[:5]))
    with closing(ea.db()) as con:
        for k, v in (("auto_mode", m), ("auto_testers", json.dumps(sorted(set(ids))))):
            con.execute("INSERT INTO ea_meta(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, v))
        if m == "off":
            con.execute("UPDATE auto_jobs SET status='skipped', note='AutoTrading was switched off', updated_at=? WHERE status='waiting'", (_now(),))
        con.commit()
    return await admin_get(request)


routes = [
    Route("/api/auto/me", me_get, methods=["GET"]),
    Route("/api/auto/settings", me_settings, methods=["POST"]),
    Route("/api/auto/admin", admin_get, methods=["GET"]),
    Route("/api/auto/admin", admin_set, methods=["POST"]),
]
