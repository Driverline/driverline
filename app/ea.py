"""MT5 companion: connection keys, account sync from the EA, risk rules and risk checks."""
import hashlib
import json
import math
import os
import re
import secrets
import time
from contextlib import closing

from starlette.exceptions import HTTPException
from starlette.responses import JSONResponse
from starlette.routing import Route

from . import auth, credits, dbx, journal
from .config import match_key

MAX_BODY = 300_000
CMD_TTL = 60  # seconds a close command stays valid
DEFAULT_RULES = {"max_risk_pct": 1.0, "daily_loss_pct": 3.0, "max_positions": 3}
MAX_ACCOUNTS = max(1, int(os.environ.get("DRIVERLINE_MAX_MT5_ACCOUNTS", "3") or 3))   # MT5 accounts one member can connect
def member_keys_ok(u) -> bool:
    """TradeLens does not give members the EA, so by default only an admin creates connection keys (through MT5 connection requests).
    DRIVERLINE_MEMBER_KEYS=1 brings back self-service keys."""
    return u.get("tier") == "admin" or os.environ.get("DRIVERLINE_MEMBER_KEYS") == "1"


_NO_KEYS = "Your MT5 connection is set up for you by TradeLens support. Use Request MT5 connection on the MT5 page."
_last_report: dict[int, float] = {}   # keyed by MT5 account id
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
    CREATE TABLE IF NOT EXISTS ea_manage(user_id INTEGER PRIMARY KEY, settings TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS ea_accounts(id {ID}, user_id INTEGER NOT NULL, label TEXT NOT NULL, key_hash TEXT UNIQUE NOT NULL,
        created_at BIGINT NOT NULL, last_seen BIGINT, login TEXT);
    CREATE TABLE IF NOT EXISTS ea_acct_state(account_id INTEGER PRIMARY KEY, state TEXT NOT NULL, specs TEXT, updated_at BIGINT NOT NULL);
    CREATE TABLE IF NOT EXISTS ea_acct_rules(account_id INTEGER PRIMARY KEY, max_risk_pct DOUBLE PRECISION NOT NULL,
        daily_loss_pct DOUBLE PRECISION NOT NULL, max_positions INTEGER NOT NULL);
    CREATE TABLE IF NOT EXISTS ea_acct_manage(account_id INTEGER PRIMARY KEY, settings TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS ea_meta(k TEXT PRIMARY KEY, v TEXT);
    CREATE TABLE IF NOT EXISTS mt5_requests(id {ID}, user_id INTEGER NOT NULL, label TEXT NOT NULL, login TEXT NOT NULL, server TEXT NOT NULL,
        pw_enc TEXT, status TEXT NOT NULL, note TEXT, account_id INTEGER, created_at BIGINT NOT NULL, updated_at BIGINT NOT NULL, pw_wiped_at BIGINT);
    CREATE TABLE IF NOT EXISTS mt5_request_log(id {ID}, request_id INTEGER NOT NULL, admin_id INTEGER, action TEXT NOT NULL, created_at BIGINT NOT NULL);
    CREATE TABLE IF NOT EXISTS copy_subs(user_id INTEGER PRIMARY KEY, ends_at BIGINT NOT NULL, note TEXT, admin_id INTEGER, updated_at BIGINT NOT NULL);
    CREATE TABLE IF NOT EXISTS copy_members(account_id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, enabled INTEGER NOT NULL DEFAULT 0,
        mode TEXT NOT NULL DEFAULT 'fixed', fixed_lots DOUBLE PRECISION, risk_pct DOUBLE PRECISION, max_risk_pct DOUBLE PRECISION, updated_at BIGINT NOT NULL);
    CREATE TABLE IF NOT EXISTS copy_flags(id {ID}, master_account_id INTEGER NOT NULL, master_ticket BIGINT NOT NULL, symbol TEXT NOT NULL,
        dir TEXT NOT NULL, open_price DOUBLE PRECISION NOT NULL, sl DOUBLE PRECISION NOT NULL, tp DOUBLE PRECISION NOT NULL,
        status TEXT NOT NULL, admin_id INTEGER, created_at BIGINT NOT NULL, closed_at BIGINT);
    CREATE TABLE IF NOT EXISTS copy_jobs(id {ID}, flag_id INTEGER NOT NULL, account_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
        status TEXT NOT NULL, note TEXT, command_id INTEGER, lots DOUBLE PRECISION, risk_pct DOUBLE PRECISION, ticket BIGINT, before TEXT,
        symbol TEXT, dir TEXT, close_req INTEGER NOT NULL DEFAULT 0, close_cmd INTEGER, close_tries INTEGER NOT NULL DEFAULT 0,
        created_at BIGINT NOT NULL, updated_at BIGINT NOT NULL)"""
_auth_ready = False
OPEN_TTL = 20   # seconds an 'open trade' request stays valid, so a stale order can never fire later


def _migrate(con):
    con.add_column("ea_commands", "payload", "TEXT")
    con.add_column("ea_commands", "account_id", "INTEGER")
    con.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_ea_accounts_label ON ea_accounts(user_id, label)")
    con.execute("CREATE INDEX IF NOT EXISTS ix_ea_commands_account ON ea_commands(account_id, status)")
    con.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_copy_jobs_flag_acct ON copy_jobs(flag_id, account_id)")
    con.execute("CREATE INDEX IF NOT EXISTS ix_copy_jobs_acct ON copy_jobs(account_id, status)")
    con.execute("CREATE INDEX IF NOT EXISTS ix_copy_flags_status ON copy_flags(status)")
    con.execute("CREATE INDEX IF NOT EXISTS ix_mt5_requests_status ON mt5_requests(status, user_id)")
    # One-time, additive: each member's existing connection becomes their first account ("Main"), keeping the SAME key, risk rules and
    # auto-manage settings, so a connected EA keeps working untouched. The old tables are left as they are (rollback safety).
    # The marker stops the copy from running again, so an account a member later removes can never be brought back from the old tables.
    if con.execute("SELECT 1 FROM ea_meta WHERE k='accounts_backfilled'").fetchone():
        return
    try:
        con.execute("INSERT INTO ea_accounts(user_id,label,key_hash,created_at,last_seen) "
                    "SELECT k.user_id,'Main',k.key_hash,k.created_at,k.last_seen FROM ea_keys k "
                    "WHERE NOT EXISTS (SELECT 1 FROM ea_accounts x WHERE x.key_hash=k.key_hash) "
                    "AND NOT EXISTS (SELECT 1 FROM ea_accounts y WHERE y.user_id=k.user_id AND y.label='Main')")
        con.execute("INSERT INTO ea_acct_state(account_id,state,specs,updated_at) "
                    "SELECT a.id,s.state,s.specs,s.updated_at FROM ea_state s JOIN ea_accounts a ON a.user_id=s.user_id AND a.label='Main' "
                    "WHERE NOT EXISTS (SELECT 1 FROM ea_acct_state z WHERE z.account_id=a.id)")
        con.execute("INSERT INTO ea_acct_rules(account_id,max_risk_pct,daily_loss_pct,max_positions) "
                    "SELECT a.id,r.max_risk_pct,r.daily_loss_pct,r.max_positions FROM ea_rules r JOIN ea_accounts a ON a.user_id=r.user_id AND a.label='Main' "
                    "WHERE NOT EXISTS (SELECT 1 FROM ea_acct_rules z WHERE z.account_id=a.id)")
        con.execute("INSERT INTO ea_acct_manage(account_id,settings) SELECT a.id,m.settings FROM ea_manage m JOIN ea_accounts a ON a.user_id=m.user_id AND a.label='Main' "
                    "WHERE NOT EXISTS (SELECT 1 FROM ea_acct_manage z WHERE z.account_id=a.id)")
        con.execute("INSERT INTO ea_meta(k,v) VALUES('accounts_backfilled',?)", (str(int(time.time())),))
        con.commit()
    except Exception as e:
        con.raw.rollback()                       # another server instance did the same copy first: nothing more to do
        if not dbx.is_duplicate(e):
            raise


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


def get_rules(con, aid) -> dict:
    """The risk rules of one MT5 account (aid is the ea_accounts id)."""
    r = con.execute("SELECT max_risk_pct,daily_loss_pct,max_positions FROM ea_acct_rules WHERE account_id=?", (aid,)).fetchone()
    return dict(r) if r else dict(DEFAULT_RULES)


# ---------------------------------------------------------------- accounts: a member can connect several MT5 accounts
def user_accounts(con, uid) -> list:
    return [dict(r) for r in con.execute("SELECT * FROM ea_accounts WHERE user_id=? ORDER BY id", (uid,)).fetchall()]


def _connected(row, now=None) -> bool:
    return bool(row.get("last_seen") and (now or time.time()) - row["last_seen"] < 120)


def resolve(con, uid, raw=None, explicit=False) -> dict:
    """The member's MT5 account a request is about. A given id must belong to the member (404 otherwise, so one member can never
    reach another's account). Without an id, a member with ONE account gets it; with several, anything that acts on an account
    (trades, closes, rules, auto-manage) must say which (explicit=True), and a plain read gets the connected one."""
    rows = user_accounts(con, uid)
    if raw not in (None, ""):
        try:
            aid = int(raw)
        except (TypeError, ValueError):
            raise HTTPException(422, "account must be a number")
        for r in rows:
            if r["id"] == aid:
                return r
        raise HTTPException(404, "That MT5 account was not found")
    if not rows:
        raise HTTPException(409, "No MT5 account is set up yet")
    if len(rows) == 1:
        return rows[0]
    if explicit:
        raise HTTPException(422, "You have more than one MT5 account. Choose which one to use")
    return next((r for r in rows if _connected(r)), rows[0])


def load(con, uid, raw):
    """(account, state row) for something that acts on one account; the account must be named when the member has several."""
    a = resolve(con, uid, raw, explicit=True)
    return a, con.execute("SELECT state, specs FROM ea_acct_state WHERE account_id=?", (a["id"],)).fetchone()


def _label(v) -> str:
    return re.sub(r"[\x00-\x1f\x7f\s]+", " ", str(v or "")).strip()[:30]


def _summary(con, a: dict, now: float) -> dict:
    s = con.execute("SELECT state FROM ea_acct_state WHERE account_id=?", (a["id"],)).fetchone()
    acc = (json.loads(s["state"]).get("account") or {}) if s else {}
    return {"id": a["id"], "label": a["label"], "login": acc.get("login") or a.get("login"), "server": acc.get("server"),
            "demo": acc.get("demo") if s else None, "currency": acc.get("currency"), "balance": acc.get("balance"),
            "equity": acc.get("equity"), "connected": _connected(a, now),
            "last_seen_ago": int(now - a["last_seen"]) if a.get("last_seen") else None}


def _seed(con, uid, aid):
    """A new account starts with the member's existing risk rules and auto-manage settings (from their first account)."""
    first = con.execute("SELECT id FROM ea_accounts WHERE user_id=? AND id<>? ORDER BY id LIMIT 1", (uid, aid)).fetchone()
    if first:
        r = con.execute("SELECT max_risk_pct,daily_loss_pct,max_positions FROM ea_acct_rules WHERE account_id=?", (first["id"],)).fetchone()
        m = con.execute("SELECT settings FROM ea_acct_manage WHERE account_id=?", (first["id"],)).fetchone()
    else:
        r = con.execute("SELECT max_risk_pct,daily_loss_pct,max_positions FROM ea_rules WHERE user_id=?", (uid,)).fetchone()
        m = con.execute("SELECT settings FROM ea_manage WHERE user_id=?", (uid,)).fetchone()
    if r:
        con.execute("INSERT INTO ea_acct_rules(account_id,max_risk_pct,daily_loss_pct,max_positions) VALUES(?,?,?,?)",
                    (aid, r["max_risk_pct"], r["daily_loss_pct"], r["max_positions"]))
    if m:
        con.execute("INSERT INTO ea_acct_manage(account_id,settings) VALUES(?,?)", (aid, m["settings"]))


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
        r = con.execute("SELECT a.id, a.user_id, a.login, a.last_seen, u.blocked, u.tier FROM ea_accounts a JOIN users u ON u.id=a.user_id "
                        "WHERE a.key_hash=?", (_hash(key),)).fetchone()
        if not r or r["blocked"]:
            raise HTTPException(401, "Invalid or revoked connection key")
        if not credits.mt5_can_report({"id": r["user_id"], "tier": r["tier"]}):
            raise HTTPException(403, "The MT5 connection is part of Signals Pro. Renew it in the Billing tab.")
        uid, aid, now = r["user_id"], r["id"], time.time()
        if now - _last_report.get(aid, 0) < 3:
            raise HTTPException(429, "Too many reports; wait a few seconds")
        login = str(acc.get("login") or "")[:40]
        if r["login"] and login and r["login"] != login and r["last_seen"] and now - r["last_seen"] < 120:
            # two MT5 terminals are using ONE key: refuse the newcomer so the two accounts' data never get mixed
            raise HTTPException(409, f"This connection key is already in use by MT5 account {r['login']}. "
                                     "Create a separate key for each MT5 account in the TradeLens MT5 tab.")
        _last_report[aid] = now
        state = {
            "account": {k: acc.get(k) for k in ("login", "server", "currency", "balance", "equity", "margin_free", "demo",
                                           "remote_close", "manage_allowed", "execute_allowed", "pending_allowed", "server_offset")},
            "today": d.get("today") if isinstance(d.get("today"), dict) else {},
            "positions": [p for p in (d.get("positions") or []) if isinstance(p, dict)][:100],
            "orders": [o for o in (d.get("orders") or []) if isinstance(o, dict)][:100],   # pending orders waiting at the broker
        }
        specs = d.get("specs")
        specs_json = json.dumps([s for s in specs if isinstance(s, dict)][:100]) if isinstance(specs, list) else None
        con.execute("INSERT INTO ea_acct_state(account_id,state,specs,updated_at) VALUES(?,?,?,?) "
                    "ON CONFLICT(account_id) DO UPDATE SET state=excluded.state, "
                    "specs=COALESCE(excluded.specs, ea_acct_state.specs), updated_at=excluded.updated_at",
                    (aid, json.dumps(state), specs_json, int(now)))
        con.execute("UPDATE ea_accounts SET last_seen=?, login=COALESCE(NULLIF(?, ''), login) WHERE id=?", (int(now), login, aid))
        con.commit()
        try:                                       # copy trading: follow flagged trades / follow the master closing (never blocks the EA)
            from . import copy_trading
            copy_trading.on_report(con, aid, state)
        except Exception as e:
            con.raw.rollback()
            print(f"[copy] report hook failed: {type(e).__name__}: {e}", flush=True)
        try:                                       # a connection request finished: delete the stored password
            from . import mt5_requests
            mt5_requests.on_report(con, aid)
        except Exception as e:
            con.raw.rollback()
            print(f"[mt5req] report hook failed: {type(e).__name__}", flush=True)
        try:                                       # founding members: read the funded balance the EA reported (perks.py)
            from . import perks
            perks.verify_founder(uid, state["account"])
        except Exception as e:
            print(f"[perks] founder check failed: {type(e).__name__}", flush=True)
        rules = get_rules(con, aid)
    closed = [c for c in (d.get("closed") or []) if isinstance(c, dict)] if isinstance(d.get("closed"), list) else []
    out = risk_check(state, rules)
    fresh: list = []
    out["journal"] = journal.import_mt5(uid, acc.get("login"), closed, int(_f(acc.get("server_offset"))), fresh) if closed else 0
    if fresh:                                  # tell the member their trades closed; runs in the background, never delays the EA
        try:
            import asyncio
            from . import closed_alerts
            asyncio.get_running_loop().run_in_executor(None, closed_alerts.notify_closed, uid, fresh, acc.get("currency"))
        except Exception as e:
            print(f"[push] closed-trade alert not started: {type(e).__name__}", flush=True)
    return JSONResponse(out)


def _key_account(con, request):
    """(account id, user id) of the connection key an EA sent."""
    key = request.headers.get("x-driverline-key", "")
    if not key:
        raise HTTPException(401, "Missing connection key")
    r = con.execute("SELECT a.id, a.user_id, u.blocked, u.tier FROM ea_accounts a JOIN users u ON u.id=a.user_id "
                    "WHERE a.key_hash=?", (_hash(key),)).fetchone()
    if not r or r["blocked"]:
        raise HTTPException(401, "Invalid or revoked connection key")
    if not credits.mt5_allowed({"id": r["user_id"], "tier": r["tier"]}):
        raise HTTPException(403, "The MT5 connection is part of Signals Pro. Renew it in the Billing tab.")
    return r["id"], r["user_id"]


def _acct_param(request, d=None):
    return (d.get("account") if isinstance(d, dict) else None) or request.query_params.get("account")


async def command_create(request):
    """A signed-in member asks one of their own EAs to close one position or all positions."""
    u = auth.require_user(request)
    d = await auth._json(request)
    kind = d.get("kind")
    if kind not in ("close", "close_all", "cancel_order"):
        raise HTTPException(422, "kind must be close, close_all or cancel_order")
    now = int(time.time())
    with closing(db()) as con:
        a, s = load(con, u["id"], _acct_param(request, d))
        aid = a["id"]
        if not (a["last_seen"] and now - a["last_seen"] < 120 and s):
            raise HTTPException(409, "Your MT5 is not connected right now")
        st = json.loads(s["state"])
        if kind == "cancel_order":     # cancelling a waiting pending order is governed by AllowPending, not AllowRemoteClose
            if not st["account"].get("pending_allowed"):
                raise HTTPException(409, "Pending orders are not enabled on your account yet (AllowPending is off). Ask TradeLens support to enable it.")
        elif not st["account"].get("remote_close"):
            raise HTTPException(409, "Closing from the app is not enabled on your account yet (AllowRemoteClose is off). Ask TradeLens support to enable it.")
        positions, ticket = st["positions"], None
        if kind == "cancel_order":
            try:
                ticket = int(d.get("ticket"))
            except (TypeError, ValueError):
                raise HTTPException(422, "ticket must be a number")
            if ticket not in {int(_f(o.get("ticket"))) for o in (st.get("orders") or [])}:
                raise HTTPException(404, "That pending order is not waiting any more")
        elif kind == "close":
            try:
                ticket = int(d.get("ticket"))
            except (TypeError, ValueError):
                raise HTTPException(422, "ticket must be a number")
            if ticket not in {int(_f(p.get("ticket"))) for p in positions}:
                raise HTTPException(404, "That position is not open any more")
        elif kind == "close_all" and not positions:
            raise HTTPException(409, "There are no open positions")
        con.execute("UPDATE ea_commands SET status='expired' WHERE account_id=? AND status='pending' AND created_at<?",
                    (aid, now - CMD_TTL))
        if con.execute("SELECT 1 FROM ea_commands WHERE account_id=? AND status IN ('pending','sent') AND created_at>?",
                       (aid, now - CMD_TTL)).fetchone():
            raise HTTPException(409, "Another close is still in progress")
        cid = con.insert("INSERT INTO ea_commands(user_id,account_id,kind,ticket,created_at,status) VALUES(?,?,?,?,?,'pending')",
                         (u["id"], aid, kind, ticket, now))
        con.commit()
    return JSONResponse({"id": cid, "account": aid})


async def poll(request):
    """The EA asks: is there a command waiting for me? (Each MT5 account has its own key, so it only ever sees its own commands.)"""
    with closing(db()) as con:
        aid, uid = _key_account(con, request)
        now = time.time()
        if now - _last_poll.get(aid, 0) < 1:
            raise HTTPException(429, "Polling too fast")
        _last_poll[aid] = now
        con.execute("UPDATE ea_accounts SET last_seen=? WHERE id=?", (int(now), aid))
        con.execute("UPDATE ea_commands SET status='expired' WHERE account_id=? AND status='pending' AND created_at<?",
                    (aid, int(now) - CMD_TTL))
        con.execute("UPDATE ea_commands SET status='expired' WHERE account_id=? AND status='pending' AND kind IN ('open','pending_order') "
                    "AND created_at<?", (aid, int(now) - OPEN_TTL))
        con.commit()
        try:                                       # copy trading: turn the next copy job for THIS account into a command
            from . import copy_trading
            copy_trading.dispatch(con, aid)
        except Exception as e:
            con.raw.rollback()
            print(f"[copy] dispatch failed: {type(e).__name__}: {e}", flush=True)
        r = con.execute("SELECT id,kind,ticket,payload FROM ea_commands WHERE account_id=? AND status='pending' "
                        "ORDER BY id LIMIT 1", (aid,)).fetchone()
        if r:
            con.execute("UPDATE ea_commands SET status='sent' WHERE id=?", (r["id"],))
        con.commit()
        m = get_manage(con, aid)
    flat = {"be_on": "1" if m["be_on"] else "0", "be_trigger": str(m["be_trigger"]), "be_offset": str(m["be_offset"]),
            "tsl_on": "1" if m["tsl_on"] else "0", "tsl_start": str(m["tsl_start"]), "tsl_dist": str(m["tsl_dist"]),
            "ttp_on": "1" if m["ttp_on"] else "0", "ttp_near": str(m["ttp_near"]), "ttp_step": str(m["ttp_step"]),
            "excluded": ",".join(str(t) for t in m["excluded"])}
    if r and r["kind"] == "open" and r["payload"]:
        p = json.loads(r["payload"])
        flat.update(sym=p["symbol"], dir=p["dir"], lots=str(p["lots"]), sl_dist=str(p["sl_dist"]),
                    tp_dist=str(p["tp_dist"]), ref=str(p["ref"]), maxdev=str(p["maxdev"]), cmt=p["comment"])
    if r and r["kind"] == "pending_order" and r["payload"]:
        p = json.loads(r["payload"])
        num = lambda x: f"{_f(x):.8f}".rstrip("0").rstrip(".") or "0"
        flat.update(sym=p["symbol"], ptype=p["ptype"], lots=num(p["lots"]), price=num(p["price"]), limit=num(p["limit"]),
                    sl=num(p["sl"]), tp=num(p["tp"]), exp_min=str(int(p["exp_min"])), ref=num(p["ref"]),
                    maxdev=num(p["maxdev"]), cmt=p["comment"])
    return JSONResponse({"cmd": r["kind"] if r else "", "id": r["id"] if r else 0,
                         "ticket": (r["ticket"] or 0) if r else 0, **flat})


async def result(request):
    d = await auth._json(request)
    with closing(db()) as con:
        aid, uid = _key_account(con, request)
        try:
            cid = int(d["id"])
        except (KeyError, TypeError, ValueError):
            raise HTTPException(422, "id is required")
        res = json.dumps({"closed": int(_f(d.get("closed"))), "failed": int(_f(d.get("failed"))),
                          "message": str(d.get("message", ""))[:200]})
        con.execute("UPDATE ea_commands SET status=?, result=? WHERE id=? AND account_id=? AND status='sent'",
                    ("done" if d.get("ok") else "failed", res, cid, aid))
        con.commit()
    return JSONResponse({"ok": True})


MANAGE_DEFAULT = {"be_on": False, "be_trigger": 1.0, "be_offset": 0.1,
                  "tsl_on": False, "tsl_start": 1.5, "tsl_dist": 1.0,
                  "ttp_on": False, "ttp_near": 0.3, "ttp_step": 1.0, "excluded": []}
MANAGE_RANGES = {"be_trigger": (0.3, 3.0), "be_offset": (0.0, 0.5), "tsl_start": (0.5, 5.0), "tsl_dist": (0.3, 3.0),
                 "ttp_near": (0.1, 1.5), "ttp_step": (0.3, 3.0)}


def get_manage(con, aid) -> dict:
    r = con.execute("SELECT settings FROM ea_acct_manage WHERE account_id=?", (aid,)).fetchone()
    return {**MANAGE_DEFAULT, **(json.loads(r["settings"]) if r else {})}


def _save_manage(con, aid, s):
    con.execute("INSERT INTO ea_acct_manage(account_id,settings) VALUES(?,?) ON CONFLICT(account_id) DO UPDATE SET settings=excluded.settings",
                (aid, json.dumps(s)))
    con.commit()


async def manage_get(request):
    u = auth.require_user(request)
    with closing(db()) as con:
        a = resolve(con, u["id"], _acct_param(request), explicit=True)
        return JSONResponse(get_manage(con, a["id"]))


async def manage_save(request):
    """Switches and distances for breakeven, trailing stop and trailing take-profit of ONE MT5 account. Distances are in R, where
    1R is the original stop distance of the position."""
    u = auth.require_user(request)
    d = await auth._json(request)
    with closing(db()) as con:
        a = resolve(con, u["id"], _acct_param(request, d), explicit=True)
        s = get_manage(con, a["id"])
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
        _save_manage(con, a["id"], s)
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
        a = resolve(con, u["id"], _acct_param(request, d), explicit=True)
        s = get_manage(con, a["id"])
        ex = {int(t) for t in s["excluded"]}
        (ex.add if d.get("excluded") else ex.discard)(ticket)
        s["excluded"] = sorted(ex)[-200:]
        _save_manage(con, a["id"], s)
    return JSONResponse(s)


def queue_open(con, a: dict, payload: dict, kind: str = "open") -> int:
    """Queue ONE 'open trade' (or 'pending_order') request for ONE of the member's MT5 accounts (a is its ea_accounts row).
    Called only after the member clicked the button."""
    now, aid = int(time.time()), a["id"]
    con.execute("UPDATE ea_commands SET status='expired' WHERE account_id=? AND status='pending' AND created_at<?",
                (aid, now - CMD_TTL))
    if con.execute("SELECT 1 FROM ea_commands WHERE account_id=? AND status IN ('pending','sent') AND created_at>?",
                   (aid, now - CMD_TTL)).fetchone():
        raise HTTPException(409, "Another request is still in progress. Wait a moment and try again.")
    cid = con.insert("INSERT INTO ea_commands(user_id,account_id,kind,ticket,created_at,status,payload) VALUES(?,?,?,?,?,'pending',?)",
                     (a["user_id"], aid, kind, None, now, json.dumps(payload)))
    con.commit()
    return cid


def _new_key(con, uid, label) -> tuple:
    """Create a new MT5 account for the member and its connection key. Returns (id, raw key). The caller holds the member's lock."""
    raw = "dlk_" + secrets.token_urlsafe(24)
    aid = con.insert("INSERT INTO ea_accounts(user_id,label,key_hash,created_at) VALUES(?,?,?,?)", (uid, label, _hash(raw), int(time.time())))
    _seed(con, uid, aid)
    return aid, raw


def _lock(con, uid):
    if con.pg:       # one create/remove at a time per member, so the account limit cannot be beaten by two quick taps
        con.execute("SELECT pg_advisory_xact_lock(?)", (10_000_000_000 + uid,))


async def accounts_list(request):
    u = auth.require_user(request)
    with closing(db()) as con:
        now = time.time()
        return JSONResponse({"accounts": [_summary(con, a, now) for a in user_accounts(con, u["id"])], "max": MAX_ACCOUNTS,
                             "mt5_allowed": credits.mt5_can_report(u)})


async def account_create(request):
    """Add another MT5 account (for example a Demo next to a Live one). Each gets its own connection key."""
    u = auth.require_user(request)
    if not credits.mt5_can_report(u):
        raise HTTPException(403, "The MT5 connection is part of Signals Pro. Buy it in the Billing tab.")
    if not member_keys_ok(u):
        raise HTTPException(403, _NO_KEYS)
    d = await auth._json(request)
    label = _label(d.get("label"))
    if not label:
        raise HTTPException(422, "Give the account a short name, for example Live or Demo")
    with closing(db()) as con:
        _lock(con, u["id"])
        rows = user_accounts(con, u["id"])
        if len(rows) >= MAX_ACCOUNTS:
            raise HTTPException(409, f"You can connect up to {MAX_ACCOUNTS} MT5 accounts. Remove one first.")
        if any(r["label"].lower() == label.lower() for r in rows):
            raise HTTPException(409, "You already have an account with that name")
        try:
            aid, raw = _new_key(con, u["id"], label)
            con.commit()
        except Exception as e:
            con.raw.rollback()
            if dbx.is_duplicate(e):
                raise HTTPException(409, "You already have an account with that name")
            raise
    return JSONResponse({"id": aid, "label": label, "key": raw})


async def account_key(request):
    """Replace the connection key of ONE account (the old key stops working at once). The account's data and settings stay."""
    u = auth.require_user(request)
    if not credits.mt5_can_report(u):
        raise HTTPException(403, "The MT5 connection is part of Signals Pro. Buy it in the Billing tab.")
    if not member_keys_ok(u):
        raise HTTPException(403, _NO_KEYS)
    raw = "dlk_" + secrets.token_urlsafe(24)
    with closing(db()) as con:
        a = resolve(con, u["id"], request.path_params["aid"])
        con.execute("UPDATE ea_accounts SET key_hash=?, created_at=?, last_seen=NULL, login=NULL WHERE id=?", (_hash(raw), int(time.time()), a["id"]))
        con.commit()
    return JSONResponse({"id": a["id"], "key": raw})


async def account_rename(request):
    u = auth.require_user(request)
    d = await auth._json(request)
    label = _label(d.get("label"))
    if not label:
        raise HTTPException(422, "Give the account a short name")
    with closing(db()) as con:
        a = resolve(con, u["id"], request.path_params["aid"])
        if any(r["label"].lower() == label.lower() and r["id"] != a["id"] for r in user_accounts(con, u["id"])):
            raise HTTPException(409, "You already have an account with that name")
        try:
            con.execute("UPDATE ea_accounts SET label=? WHERE id=?", (label, a["id"]))
            con.commit()
        except Exception as e:
            con.raw.rollback()
            if dbx.is_duplicate(e):
                raise HTTPException(409, "You already have an account with that name")
            raise
    return JSONResponse({"id": a["id"], "label": label})


def _remove(con, a: dict):
    aid = a["id"]
    for t in ("ea_acct_state", "ea_acct_rules", "ea_acct_manage", "copy_members"):
        con.execute(f"DELETE FROM {t} WHERE account_id=?", (aid,))
    con.execute("UPDATE copy_jobs SET status='closed', note='Account removed' WHERE account_id=? AND status IN ('waiting','sent','open')", (aid,))
    con.execute("DELETE FROM ea_meta WHERE k='copy_master' AND v=?", (str(aid),))
    con.execute("DELETE FROM ea_commands WHERE account_id=?", (aid,))
    con.execute("DELETE FROM ea_accounts WHERE id=?", (aid,))
    con.commit()
    _last_report.pop(aid, None)
    _last_poll.pop(aid, None)


async def account_delete(request):
    """Disconnect and remove ONE MT5 account: its key stops working and its live data and settings are deleted.
    Journal entries already imported from it stay."""
    u = auth.require_user(request)
    with closing(db()) as con:
        a = resolve(con, u["id"], request.path_params["aid"])
        _remove(con, a)
    return JSONResponse({"ok": True})


async def key_create(request):
    """Older single-account call, kept so a page cached on a phone still works: creates the first account, or replaces the key of the
    only account. With several accounts it must be told which (account=...)."""
    u = auth.require_user(request)
    if not credits.mt5_can_report(u):
        raise HTTPException(403, "The MT5 connection is part of Signals Pro. Buy it in the Billing tab.")
    if not member_keys_ok(u):
        raise HTTPException(403, _NO_KEYS)
    try:
        d = await request.json()
    except Exception:
        d = {}
    with closing(db()) as con:
        _lock(con, u["id"])
        rows = user_accounts(con, u["id"])
        if not rows and not request.query_params.get("account"):
            aid, raw = _new_key(con, u["id"], "Main")
            con.commit()
            return JSONResponse({"key": raw, "id": aid})
        a = resolve(con, u["id"], _acct_param(request, d if isinstance(d, dict) else None), explicit=True)
        raw = "dlk_" + secrets.token_urlsafe(24)
        con.execute("UPDATE ea_accounts SET key_hash=?, created_at=?, last_seen=NULL, login=NULL WHERE id=?", (_hash(raw), int(time.time()), a["id"]))
        con.commit()
    return JSONResponse({"key": raw, "id": a["id"]})


async def key_revoke(request):
    """Older single-account call: removes the only account (several: say which with account=...)."""
    u = auth.require_user(request)
    with closing(db()) as con:
        if user_accounts(con, u["id"]):
            _remove(con, resolve(con, u["id"], request.query_params.get("account"), explicit=True))
    return JSONResponse({"ok": True})


async def status(request):
    u = auth.require_user(request)
    now = time.time()
    with closing(db()) as con:
        rows = user_accounts(con, u["id"])
        accounts = [_summary(con, a, now) for a in rows]
        base = {"mt5_allowed": credits.mt5_can_report(u), "accounts": accounts, "max_accounts": MAX_ACCOUNTS, "self_serve": member_keys_ok(u)}
        if not rows and request.query_params.get("account") not in (None, ""):
            raise HTTPException(404, "That MT5 account was not found")
        if not rows:
            return JSONResponse({**base, "has_key": False, "connected": False, "last_seen_ago": None, "rules": dict(DEFAULT_RULES),
                                 "manage": dict(MANAGE_DEFAULT), "account_id": None})
        a = resolve(con, u["id"], request.query_params.get("account"))
        aid = a["id"]
        s = con.execute("SELECT state, updated_at FROM ea_acct_state WHERE account_id=?", (aid,)).fetchone()
        rules = get_rules(con, aid)
        mg = get_manage(con, aid)
        cmd = con.execute("SELECT id,kind,ticket,status,result,created_at FROM ea_commands WHERE account_id=? "
                          "ORDER BY id DESC LIMIT 1", (aid,)).fetchone()
    seen = a["last_seen"]
    out = {**base, "account_id": aid, "label": a["label"], "has_key": True, "connected": bool(seen and now - seen < 120),
           "last_seen_ago": int(now - seen) if seen else None, "rules": rules, "manage": mg}
    if s:
        st = json.loads(s["state"])
        out.update(account=st["account"], today=st["today"], positions=st["positions"], check=risk_check(st, rules))
        out["can_close"] = bool(out["connected"] and st["account"].get("remote_close"))
        out["can_manage"] = bool(out["connected"] and st["account"].get("manage_allowed"))
        out["can_execute"] = bool(out["connected"] and st["account"].get("execute_allowed"))
        out["can_pending"] = bool(out["connected"] and st["account"].get("pending_allowed"))
        out["orders"] = st.get("orders") or []
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
        a = resolve(con, u["id"], _acct_param(request, d), explicit=True)
        con.execute("INSERT INTO ea_acct_rules VALUES(?,?,?,?) ON CONFLICT(account_id) DO UPDATE SET "
                    "max_risk_pct=excluded.max_risk_pct, daily_loss_pct=excluded.daily_loss_pct, "
                    "max_positions=excluded.max_positions", (a["id"], mr, dl, mp))
        con.commit()
    return JSONResponse({"ok": True})


async def specs(request):
    u = auth.require_user(request)
    with closing(db()) as con:
        if not user_accounts(con, u["id"]) and request.query_params.get("account") in (None, ""):
            return JSONResponse([])
        a = resolve(con, u["id"], request.query_params.get("account"))
        s = con.execute("SELECT specs FROM ea_acct_state WHERE account_id=?", (a["id"],)).fetchone()
    rows = json.loads(s["specs"]) if s and s["specs"] else []
    # "key" tells the pages which TradeLens instrument a broker symbol (XAUUSDm, XAUUSDmicro ...) belongs to
    return JSONResponse([{**x, "key": match_key(x.get("symbol", ""))} for x in rows if isinstance(x, dict)])


routes = [
    Route("/api/ea/report", report, methods=["POST"]),
    Route("/api/ea/poll", poll, methods=["POST"]),
    Route("/api/ea/result", result, methods=["POST"]),
    Route("/api/ea/command", command_create, methods=["POST"]),
    Route("/api/ea/key", key_create, methods=["POST"]),
    Route("/api/ea/key", key_revoke, methods=["DELETE"]),
    Route("/api/ea/accounts", accounts_list, methods=["GET"]),
    Route("/api/ea/accounts", account_create, methods=["POST"]),
    Route("/api/ea/accounts/{aid:int}/key", account_key, methods=["POST"]),
    Route("/api/ea/accounts/{aid:int}/rename", account_rename, methods=["POST"]),
    Route("/api/ea/accounts/{aid:int}", account_delete, methods=["DELETE"]),
    Route("/api/ea/status", status, methods=["GET"]),
    Route("/api/ea/rules", rules_save, methods=["POST"]),
    Route("/api/ea/manage", manage_get, methods=["GET"]),
    Route("/api/ea/manage", manage_save, methods=["POST"]),
    Route("/api/ea/manage/exclude", manage_exclude, methods=["POST"]),
    Route("/api/ea/specs", specs, methods=["GET"]),
]