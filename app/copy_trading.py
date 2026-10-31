"""Copy trading: the admin FLAGS a trade on the master MT5 account and it is copied to every subscribed member who switched copying on.

Design (see docs/COPY_TRADING.md):
- Only flagged trades are copied. A flag needs a stop loss and a take profit on the master position, so a copy is never unprotected.
- Pull model: flagging creates one `copy_jobs` row per eligible follower account. Each follower's EA collects its own job on its next poll
  (dispatch), when sizing is worked out from that account's fresh balance and contract specs. Nothing is pushed and nothing waits for
  a busy account: a job that cannot be taken in JOB_TTL seconds is skipped (never executed late).
- Sizing: fixed lot (default = the symbol's minimum lot) or percent risk. Either way, if the lot would risk more than the cap
  (the member's own limit), NOTHING is traded and the reason is recorded.
- SL and TP are the master's distances from the master's entry, applied to the follower's own fill, and the EA refuses a fill that is too
  far from the master's price (same slippage guard as one-click trades).
- Closing: when the master trade closes (or the admin ends it), the follower's copy is closed too. The follower's ticket is found by the
  new position that appeared after the copy command; if that is ambiguous the copy is left for the member to close (never guessed).
- The member's EA still decides: it executes only with AllowExecute (and closes only with AllowRemoteClose) switched on.
Every public function here is called from ea.py inside try/except, so a bug in copy trading can never stop MT5 reporting or polling."""
import json
import math
import time
from contextlib import closing

from starlette.exceptions import HTTPException
from starlette.responses import JSONResponse
from starlette.routing import Route

from . import auth, copy_plan, ea, journal, signal_feed

JOB_TTL = 120            # seconds a copy job may wait for the follower's EA to collect it, then it is skipped
TICKET_WAIT = 60         # seconds to look for the follower's new position before giving up on finding its ticket
SUB_MAX_DAYS = 366
MODES = ("fixed", "risk")
OPEN_STATES = ("waiting", "sent", "open")
_f = ea._f


# ---------------------------------------------------------------- pure sizing (unit tested on its own)
def _dec(step: float) -> int:
    t = f"{step:.10f}".rstrip("0")
    return len(t.split(".")[1]) if "." in t else 0


def size(spec: dict, balance: float, sl_dist: float, mode: str, fixed_lots, risk_pct, cap_pct):
    """(lots, risk_pct_of_balance, reason). lots is None when the trade must NOT be placed; reason says why.
    fixed: the member's fixed lot, or the symbol's minimum lot when none is set. risk: lots so the stop loss risks risk_pct of balance.
    cap_pct: the member's own limit per trade: a lot that would risk more than it is never traded."""
    unit = _f(spec.get("tick_value")) / _f(spec.get("tick_size")) if _f(spec.get("tick_size")) > 0 and _f(spec.get("tick_value")) > 0 \
        else _f(spec.get("contract_size"))
    vmin, vstep, vmax = _f(spec.get("vol_min")), _f(spec.get("vol_step")), _f(spec.get("vol_max"))
    if not (unit > 0 and vmin > 0 and vstep > 0 and sl_dist > 0):
        return None, None, "Contract details for this symbol are missing from your MT5, so the risk cannot be worked out"
    if not balance > 0:
        return None, None, "Your account balance is not known yet"
    per_lot = sl_dist * unit
    if mode == "risk":
        want = balance * _f(risk_pct) / 100.0 / per_lot
        lots = math.floor(want / vstep + 1e-9) * vstep
        if lots < vmin:
            return None, None, f"Even the minimum lot ({vmin:g}) risks more than your {_f(risk_pct):g}% for this trade"
    else:
        lots = _f(fixed_lots) if _f(fixed_lots) > 0 else vmin
        lots = max(vmin, math.floor(lots / vstep + 1e-9) * vstep)
    if vmax > 0:
        lots = min(lots, vmax)
    lots = round(lots, max(_dec(vstep), 2))
    pct = lots * per_lot / balance * 100.0
    if cap_pct and pct > cap_pct + 1e-9:
        return None, round(pct, 2), f"Lot {lots:g} would risk {pct:.2f}% of your balance, above your {cap_pct:g}% limit, so it was not traded"
    return lots, round(pct, 2), ""


# ---------------------------------------------------------------- small helpers
def _now() -> int:
    return int(time.time())


def master_id(con):
    r = con.execute("SELECT v FROM ea_meta WHERE k='copy_master'").fetchone()
    try:
        return int(r["v"]) if r else None
    except (TypeError, ValueError):
        return None


def subscribed(con, uid) -> int:
    """Seconds-since-epoch until which the member may copy (Copy Trading plan, Signals Pro, Signals Plus in copy mode for IB-linked members,
    or an admin grant), or 0 when they may not. See copy_plan.py."""
    from . import copy_plan
    try:
        return copy_plan.copy_until(uid, con)
    except Exception as e:
        print(f"[copy] entitlement check failed: {type(e).__name__}", flush=True)
        return 0


def _state(con, aid):
    r = con.execute("SELECT state, specs FROM ea_acct_state WHERE account_id=?", (aid,)).fetchone()
    if not r:
        return None, []
    return json.loads(r["state"]), [x for x in json.loads(r["specs"] or "[]") if isinstance(x, dict)]


def _tickets(st) -> set:
    return {int(_f(p.get("ticket"))) for p in (st or {}).get("positions", [])}


def _set(con, jid, **kw):
    kw["updated_at"] = _now()
    cols = ",".join(f"{k}=?" for k in kw)
    con.execute(f"UPDATE copy_jobs SET {cols} WHERE id=?", (*kw.values(), jid))


def _busy(con, aid, now) -> bool:
    return bool(con.execute("SELECT 1 FROM ea_commands WHERE account_id=? AND status IN ('pending','sent') AND created_at>?",
                            (aid, now - ea.CMD_TTL)).fetchone())


def _cmd_msg(con, cid) -> str:
    r = con.execute("SELECT result FROM ea_commands WHERE id=?", (cid,)).fetchone()
    try:
        return str(json.loads(r["result"]).get("message") or "") if r and r["result"] else ""
    except ValueError:
        return ""


# ---------------------------------------------------------------- flagging (admin) -> one job per eligible follower
def create_flag(con, admin_id: int, ticket: int) -> dict:
    mid = master_id(con)
    if not mid:
        raise HTTPException(409, "Choose the master MT5 account first")
    acct = con.execute("SELECT * FROM ea_accounts WHERE id=?", (mid,)).fetchone()
    st, _ = _state(con, mid)
    if not acct or not st or not ea._connected(dict(acct)):
        raise HTTPException(409, "The master MT5 account is not connected right now")
    pos = next((p for p in st["positions"] if int(_f(p.get("ticket"))) == ticket), None)
    if not pos:
        raise HTTPException(404, "That position is not open on the master account")
    op, sl, tp = _f(pos.get("open")), _f(pos.get("sl")), _f(pos.get("tp"))
    if not (op > 0 and sl > 0 and tp > 0):
        raise HTTPException(422, "Set both a stop loss and a take profit on this trade in MT5 first. Copies are never placed without them.")
    d = "BUY" if str(pos.get("type")).upper() == "BUY" else "SELL"
    if (d == "BUY" and not sl < op < tp) or (d == "SELL" and not tp < op < sl):
        raise HTTPException(422, "The stop loss and take profit are on the wrong sides of the entry price")
    if con.execute("SELECT 1 FROM copy_flags WHERE master_account_id=? AND master_ticket=? AND status='open'", (mid, ticket)).fetchone():
        raise HTTPException(409, "This trade is already flagged")
    now = _now()
    fid = con.insert("INSERT INTO copy_flags(master_account_id,master_ticket,symbol,dir,open_price,sl,tp,status,admin_id,created_at) "
                     "VALUES(?,?,?,?,?,?,?,'open',?,?)", (mid, ticket, str(pos.get("symbol")), d, op, sl, tp, admin_id, now))
    n = 0
    for m in con.execute("SELECT m.account_id, m.user_id FROM copy_members m JOIN ea_accounts a ON a.id=m.account_id "
                         "WHERE m.enabled=1 AND m.account_id<>?", (mid,)).fetchall():
        if not subscribed(con, m["user_id"]):
            continue
        con.execute("INSERT INTO copy_jobs(flag_id,account_id,user_id,status,symbol,dir,created_at,updated_at) VALUES(?,?,?,'waiting',?,?,?,?)",
                    (fid, m["account_id"], m["user_id"], str(pos.get("symbol")), d, now, now))
        n += 1
    con.commit()
    return {"id": fid, "followers": n, "symbol": str(pos.get("symbol")), "dir": d}


def request_close(con, fid: int) -> int:
    """Ask every follower copy of this flag to close. Returns how many copies were affected."""
    now = _now()
    con.execute("UPDATE copy_jobs SET status='skipped', note='The trade was ended before your copy was placed', updated_at=? "
                "WHERE flag_id=? AND status='waiting'", (now, fid))
    cur = con.execute("UPDATE copy_jobs SET close_req=1, updated_at=? WHERE flag_id=? AND status IN ('sent','open') AND close_req=0", (now, fid))
    con.commit()
    return cur.rowcount


# ---------------------------------------------------------------- EA hooks
def dispatch(con, aid: int):
    """Called from poll(): the follower's EA asked for work. Turn at most ONE copy job of this account into a command."""
    now = _now()
    if _busy(con, aid, now):
        return
    job = con.execute("SELECT j.*, f.status AS fstatus, f.open_price, f.sl, f.tp FROM copy_jobs j JOIN copy_flags f ON f.id=j.flag_id "
                      "WHERE j.account_id=? AND ((j.status='open' AND j.close_req=1 AND j.close_cmd IS NULL AND j.ticket IS NOT NULL) "
                      "OR j.status='waiting') ORDER BY (j.status='waiting'), j.id LIMIT 1", (aid,)).fetchone()
    if not job:
        return
    job = dict(job)
    st, specs = _state(con, aid)
    if job["status"] == "open":                                      # close the copy
        if not st or job["ticket"] not in _tickets(st):
            return                                                   # on_report will mark it closed
        if not st["account"].get("remote_close"):
            _set(con, job["id"], note="The trade ended but AllowRemoteClose is off in your EA, so close this trade yourself in MT5")
            con.execute("UPDATE copy_jobs SET close_cmd=0 WHERE id=?", (job["id"],))           # do not retry
            con.commit()
            return
        cid = con.insert("INSERT INTO ea_commands(user_id,account_id,kind,ticket,created_at,status) VALUES(?,?,?,?,?,'pending')",
                         (job["user_id"], aid, "close", job["ticket"], now))
        _set(con, job["id"], close_cmd=cid, close_tries=job["close_tries"] + 1)
        con.commit()
        return
    # an open copy
    def skip(why):
        _set(con, job["id"], status="skipped", note=why)
        con.commit()
    if now - job["created_at"] > JOB_TTL:
        return skip("Your EA was not connected in time, so this trade was skipped (copies are never placed late)")
    if job["fstatus"] != "open":
        return skip("The trade had already ended")
    acct = con.execute("SELECT * FROM ea_accounts WHERE id=?", (aid,)).fetchone()
    mem = con.execute("SELECT * FROM copy_members WHERE account_id=?", (aid,)).fetchone()
    if not acct or not mem or not mem["enabled"]:
        return skip("Copy trading is off for this account")
    if not subscribed(con, job["user_id"]):
        return skip("Your copy trading subscription is not active")
    if not st:
        return skip("No data from your MT5 yet")
    acc = st["account"]
    if not acc.get("execute_allowed"):
        return skip("Trading from the app is not enabled on your account (AllowExecute is off)")
    rules = ea.get_rules(con, aid)
    if ea.risk_check(st, rules)["status"] == "STOP":
        return skip(f"You reached your own daily loss limit of {rules['daily_loss_pct']:g}%")
    if len(st["positions"]) >= rules["max_positions"]:
        return skip(f"You already have {len(st['positions'])} open positions, your own maximum")
    want = journal._norm(job["symbol"])
    spec = next((x for x in specs if journal._norm(str(x.get("symbol", ""))) == want and x.get("symbol") == job["symbol"]), None) \
        or next((x for x in specs if journal._norm(str(x.get("symbol", ""))) == want), None)
    if not spec:
        return skip(f"{job['symbol']} was not found in your MT5 Market Watch")
    sd = abs(job["open_price"] - job["sl"])
    cap = _f(mem["max_risk_pct"]) or _f(rules.get("max_risk_pct"))
    lots, pct, why = size(spec, _f(acc.get("balance")), sd, mem["mode"], mem["fixed_lots"], mem["risk_pct"], cap)
    if lots is None:
        _set(con, job["id"], status="skipped", note=why, risk_pct=pct)
        con.commit()
        return
    payload = {"symbol": spec["symbol"], "dir": job["dir"], "lots": lots, "sl_dist": sd, "tp_dist": abs(job["tp"] - job["open_price"]),
               "ref": job["open_price"], "maxdev": signal_feed.CFG["exec_max_dev_r"] * sd, "comment": f"TLC-{job['flag_id']}", "copy_job": job["id"]}
    cid = con.insert("INSERT INTO ea_commands(user_id,account_id,kind,ticket,created_at,status,payload) VALUES(?,?,?,?,?,'pending',?)",
                     (job["user_id"], aid, "open", None, now, json.dumps(payload)))
    _set(con, job["id"], status="sent", command_id=cid, lots=lots, risk_pct=pct, before=json.dumps(sorted(_tickets(st))), symbol=spec["symbol"])
    con.commit()


def on_report(con, aid: int, state: dict):
    """Called after an account's report was stored."""
    now = _now()
    mid = master_id(con)
    have = _tickets(state)
    if mid == aid:                                                   # the master: did a flagged trade close?
        for f in con.execute("SELECT id, master_ticket FROM copy_flags WHERE master_account_id=? AND status='open'", (aid,)).fetchall():
            if f["master_ticket"] not in have:
                con.execute("UPDATE copy_flags SET status='closed', closed_at=? WHERE id=?", (now, f["id"]))
                request_close(con, f["id"])
        con.commit()
    for j in con.execute("SELECT * FROM copy_jobs WHERE account_id=? AND status IN ('sent','open')", (aid,)).fetchall():
        j = dict(j)
        if j["status"] == "sent":
            c = con.execute("SELECT status FROM ea_commands WHERE id=?", (j["command_id"],)).fetchone()
            if not c or c["status"] in ("failed", "expired"):
                _set(con, j["id"], status="failed", note=_cmd_msg(con, j["command_id"]) or "Your EA did not place the trade")
            elif c["status"] == "done":
                _set(con, j["id"], status="open")
                j["status"] = "open"
                j["updated_at"] = now
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
                    _set(con, j["id"], status="closed", note="The copy is no longer open")          # opened and already closed (SL/TP hit)
                elif len(cand) > 1 and now - j["updated_at"] > TICKET_WAIT:
                    _set(con, j["id"], note="Could not tell which of your trades is this copy, so it will not be closed automatically")
            elif j["ticket"] not in have:
                _set(con, j["id"], status="closed", note=j["note"] or "")
            elif j["close_req"] and j["close_cmd"]:
                c = con.execute("SELECT status FROM ea_commands WHERE id=?", (j["close_cmd"],)).fetchone()
                if c and c["status"] in ("failed", "expired"):
                    if j["close_tries"] < 3:
                        con.execute("UPDATE copy_jobs SET close_cmd=NULL WHERE id=?", (j["id"],))       # try again on the next poll
                    else:
                        _set(con, j["id"], note="Could not close this copy automatically. Please close it in MT5")
    con.commit()


# ---------------------------------------------------------------- member API
def _job_out(r: dict) -> dict:
    return {"id": r["id"], "flag_id": r["flag_id"], "account_id": r["account_id"], "symbol": r["symbol"], "dir": r["dir"], "status": r["status"],
            "note": r["note"] or "", "lots": r["lots"], "risk_pct": r["risk_pct"], "created_at": r["created_at"]}


def _num(d, k, lo, hi, allow_none=True):
    v = d.get(k)
    if v in (None, ""):
        if allow_none:
            return None
        raise HTTPException(422, f"{k} is required")
    try:
        x = float(v)
    except (TypeError, ValueError):
        raise HTTPException(422, f"{k} must be a number")
    if not (lo <= x <= hi) or x != x:
        raise HTTPException(422, f"{k} must be between {lo:g} and {hi:g}")
    return x


async def me_get(request):
    u = auth.require_user(request)
    now = _now()
    with closing(ea.db()) as con:
        ends = subscribed(con, u["id"])
        mid = master_id(con)
        accts = []
        for a in ea.user_accounts(con, u["id"]):
            m = con.execute("SELECT * FROM copy_members WHERE account_id=?", (a["id"],)).fetchone()
            rules = ea.get_rules(con, a["id"])
            accts.append({"id": a["id"], "label": a["label"], "is_master": a["id"] == mid, "enabled": bool(m and m["enabled"]),
                          "mode": m["mode"] if m else "fixed", "fixed_lots": m["fixed_lots"] if m else None,
                          "risk_pct": m["risk_pct"] if m else 1.0, "max_risk_pct": (m["max_risk_pct"] if m and m["max_risk_pct"] else None),
                          "own_limit_pct": rules["max_risk_pct"]})
        jobs = [_job_out(dict(r)) for r in con.execute("SELECT * FROM copy_jobs WHERE user_id=? ORDER BY id DESC LIMIT 20", (u["id"],)).fetchall()]
    return JSONResponse({"subscribed": bool(ends), "ends_at": ends or None, "now": now, "accounts": accts, "recent": jobs,
                         "plan": copy_plan.view(u)})


async def me_settings(request):
    u = auth.require_user(request)
    d = await auth._json(request)
    mode = d.get("mode", "fixed")
    if mode not in MODES:
        raise HTTPException(422, "mode must be fixed or risk")
    enabled = bool(d.get("enabled"))
    fixed = _num(d, "fixed_lots", 0.0001, 1000)
    risk = _num(d, "risk_pct", 0.05, 10)
    cap = _num(d, "max_risk_pct", 0.05, 10)
    if mode == "risk" and risk is None:
        raise HTTPException(422, "risk_pct is required for percentage risk")
    with closing(ea.db()) as con:
        a = ea.resolve(con, u["id"], ea._acct_param(request, d), explicit=True)
        if a["id"] == master_id(con):
            raise HTTPException(409, "This is the master account, it cannot copy itself")
        if enabled and copy_plan.state(u, con)["swap"] and copy_plan.state(u, con)["mode"] == "signals":
            # Signals Plus linked to our IB: copy trading replaces signals while it is on. The member must agree to that.
            if d.get("switch_mode") is not True:
                return JSONResponse({"detail": f"On Signals Plus, Copy trading replaces your signals while it is on. Your signals and notifications pause, and about "
                                               f"{copy_plan.view(u)['per_day']:g} signals a day are used from your balance. Switch to Copy trading?", "need_switch": True},
                                    status_code=409)
            copy_plan.set_mode(u, "copy")
        if enabled and not subscribed(con, u["id"]):
            raise HTTPException(403, "Copy trading needs the Copy Trading plan or Signals Pro (or Signals Plus if your Deriv account is linked to TradeLens). "
                                     "See the Billing tab.")
        con.execute("INSERT INTO copy_members(account_id,user_id,enabled,mode,fixed_lots,risk_pct,max_risk_pct,updated_at) VALUES(?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(account_id) DO UPDATE SET enabled=excluded.enabled, mode=excluded.mode, fixed_lots=excluded.fixed_lots, "
                    "risk_pct=excluded.risk_pct, max_risk_pct=excluded.max_risk_pct, updated_at=excluded.updated_at",
                    (a["id"], u["id"], 1 if enabled else 0, mode, fixed, risk, cap, _now()))
        con.commit()
    return JSONResponse({"ok": True, "account": a["id"]})


# ---------------------------------------------------------------- admin API
async def admin_get(request):
    u = auth.require_admin(request)
    now = _now()
    with closing(ea.db()) as con:
        mine = ea.user_accounts(con, u["id"])
        mid = master_id(con)
        master, positions = None, []
        if mid:
            row = con.execute("SELECT * FROM ea_accounts WHERE id=?", (mid,)).fetchone()
            if row:
                master = {"id": mid, "label": row["label"], "user_id": row["user_id"], "connected": ea._connected(dict(row))}
                st, _ = _state(con, mid)
                flagged = {r["master_ticket"]: r["id"] for r in con.execute(
                    "SELECT id, master_ticket FROM copy_flags WHERE master_account_id=? AND status='open'", (mid,)).fetchall()}
                for p in (st or {}).get("positions", []):
                    tk = int(_f(p.get("ticket")))
                    positions.append({"ticket": tk, "symbol": p.get("symbol"), "type": p.get("type"), "volume": p.get("volume"),
                                      "open": p.get("open"), "sl": p.get("sl"), "tp": p.get("tp"), "profit": p.get("profit"),
                                      "flag_id": flagged.get(tk), "can_flag": bool(_f(p.get("sl")) > 0 and _f(p.get("tp")) > 0)})
        flags = []
        for f in con.execute("SELECT * FROM copy_flags ORDER BY id DESC LIMIT 15").fetchall():
            counts = {r["status"]: r["c"] for r in con.execute("SELECT status, COUNT(*) AS c FROM copy_jobs WHERE flag_id=? GROUP BY status", (f["id"],)).fetchall()}
            flags.append({**{k: f[k] for k in ("id", "symbol", "dir", "open_price", "sl", "tp", "status", "created_at", "closed_at")}, "counts": counts})
        followers = sum(1 for m in con.execute("SELECT user_id FROM copy_members WHERE enabled=1").fetchall() if subscribed(con, m["user_id"]))
        subs = [{"user_id": r["user_id"], "email": r["email"], "ends_at": r["ends_at"], "note": r["note"], "active": r["ends_at"] > now}
                for r in con.execute("SELECT s.user_id, s.ends_at, s.note, u.email FROM copy_subs s JOIN users u ON u.id=s.user_id ORDER BY s.ends_at DESC LIMIT 200").fetchall()]
    return JSONResponse({"master": master, "my_accounts": [{"id": a["id"], "label": a["label"]} for a in mine], "positions": positions,
                         "flags": flags, "followers": followers, "subscribers": subs, "now": now})


async def admin_master(request):
    u = auth.require_admin(request)
    d = await auth._json(request)
    with closing(ea.db()) as con:
        a = ea.resolve(con, u["id"], d.get("account"), explicit=True)
        if con.execute("SELECT 1 FROM copy_flags WHERE status='open'").fetchone():
            raise HTTPException(409, "End the open flagged trades before changing the master account")
        con.execute("DELETE FROM copy_members WHERE account_id=?", (a["id"],))
        con.execute("INSERT INTO ea_meta(k,v) VALUES('copy_master',?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (str(a["id"]),))
        con.commit()
    return JSONResponse({"ok": True, "master": a["id"]})


async def admin_flag(request):
    u = auth.require_admin(request)
    d = await auth._json(request)
    try:
        ticket = int(d.get("ticket"))
    except (TypeError, ValueError):
        raise HTTPException(422, "ticket must be a number")
    with closing(ea.db()) as con:
        return JSONResponse(create_flag(con, u["id"], ticket))


async def admin_flag_close(request):
    auth.require_admin(request)
    fid = request.path_params["fid"]
    with closing(ea.db()) as con:
        f = con.execute("SELECT status FROM copy_flags WHERE id=?", (fid,)).fetchone()
        if not f:
            raise HTTPException(404, "Flag not found")
        con.execute("UPDATE copy_flags SET status='closed', closed_at=? WHERE id=? AND status='open'", (_now(), fid))
        n = request_close(con, fid)
    return JSONResponse({"ok": True, "closing": n})


async def admin_flag_jobs(request):
    auth.require_admin(request)
    with closing(ea.db()) as con:
        rows = con.execute("SELECT j.*, u.email FROM copy_jobs j JOIN users u ON u.id=j.user_id WHERE j.flag_id=? ORDER BY j.id",
                           (request.path_params["fid"],)).fetchall()
    return JSONResponse([{**_job_out(dict(r)), "email": r["email"], "close_req": bool(r["close_req"])} for r in rows])


async def admin_sub(request):
    u = auth.require_admin(request)
    d = await auth._json(request)
    with closing(ea.db()) as con:
        uid = d.get("user_id")
        if uid is None and d.get("email"):
            r = con.execute("SELECT id FROM users WHERE LOWER(email)=LOWER(?)", (str(d["email"]).strip(),)).fetchone()
            uid = r["id"] if r else None
        r = con.execute("SELECT id FROM users WHERE id=?", (uid,)).fetchone() if uid is not None else None
        if not r:
            raise HTTPException(404, "No member with that email")
        uid = r["id"]
        now = _now()
        if d.get("revoke"):
            con.execute("UPDATE copy_subs SET ends_at=?, updated_at=?, admin_id=? WHERE user_id=?", (now, now, u["id"], uid))
            con.execute("UPDATE copy_members SET enabled=0, updated_at=? WHERE user_id=?", (now, uid))
            con.execute("UPDATE copy_jobs SET status='skipped', note='Copy trading subscription ended' WHERE user_id=? AND status='waiting'", (uid,))
            con.commit()
            return JSONResponse({"ok": True, "revoked": True})
        try:
            days = int(d.get("days"))
        except (TypeError, ValueError):
            raise HTTPException(422, "days must be a whole number")
        if not 1 <= days <= SUB_MAX_DAYS:
            raise HTTPException(422, f"days must be between 1 and {SUB_MAX_DAYS}")
        cur = con.execute("SELECT ends_at FROM copy_subs WHERE user_id=?", (uid,)).fetchone()
        start = max(now, cur["ends_at"] if cur else 0)
        ends = start + days * 86400
        con.execute("INSERT INTO copy_subs(user_id,ends_at,note,admin_id,updated_at) VALUES(?,?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET "
                    "ends_at=excluded.ends_at, note=excluded.note, admin_id=excluded.admin_id, updated_at=excluded.updated_at",
                    (uid, ends, str(d.get("note") or "")[:200], u["id"], now))
        con.commit()
    return JSONResponse({"ok": True, "ends_at": ends})


routes = [
    Route("/api/copy/me", me_get, methods=["GET"]),
    Route("/api/copy/me/settings", me_settings, methods=["POST"]),
    Route("/api/copy/admin", admin_get, methods=["GET"]),
    Route("/api/copy/admin/master", admin_master, methods=["POST"]),
    Route("/api/copy/admin/flag", admin_flag, methods=["POST"]),
    Route("/api/copy/admin/flag/{fid:int}/close", admin_flag_close, methods=["POST"]),
    Route("/api/copy/admin/flag/{fid:int}", admin_flag_jobs, methods=["GET"]),
    Route("/api/copy/admin/subscription", admin_sub, methods=["POST"]),
]
