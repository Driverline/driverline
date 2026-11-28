"""TradeLens Connect controls, STAGE 1: the two actions that can only make an account SAFER.

  * auto_off    switch AutoTrading off for one of the member's MT5 accounts (what the website does when AutoTrading is unticked)
  * protection  make equity protection stricter (mode, risk %, cap %). Anything that would make it weaker is refused here and
                stays on the website, where it waits 24 hours.

Nothing here switches AutoTrading ON, resumes it, edits risk limits, places or closes a trade, or touches an MT5 login.
OFF unless CONNECT_CONTROLS=1 (and Connect itself is on); otherwise every route answers 404 like Connect does.

Two steps, both decided by the server: /controls/prepare checks everything and returns a plain-language summary plus a short code
(HMAC bound to this member, this Telegram account, this action and exact values, valid 2 minutes); /controls/confirm re-checks,
executes with the SAME helpers the website uses (ea.resolve, ea._apply_protection, ea._save_manage), writes an audit row to the
existing auto_events table and answers with the state read back from the database. The Connect token must be less than 5 minutes
old. No new table and no change to any existing function."""
import copy
import hmac
import json
import os
import secrets
import time
from contextlib import closing

from starlette.exceptions import HTTPException
from starlette.responses import JSONResponse
from starlette.routing import Route

from . import connect

ACTIONS = ("auto_off", "protection")
PROT_KEYS = ("prot_mode", "prot_risk_pct", "prot_cap_pct")
CODE_PREFIX, CODE_TTL, FRESH, MAX_PER_HOUR = "tlx1", 120, 300, 10
KINDS = ("connect_auto_off", "connect_prot_change")
NOT_FOUND = connect.NOT_FOUND


def on() -> bool:
    return connect.controls_on()


def _fresh(request):
    """The token must be recent: an older (still valid) one makes the app sign in again through Telegram."""
    c = connect.read_token(connect.bearer(request))
    if not c or int(time.time()) - c["iat"] > FRESH:
        raise HTTPException(401, "Please open TradeLens Connect again")


def _limit(con, uid: int):
    n = con.execute("SELECT COUNT(*) AS c FROM auto_events WHERE user_id=? AND kind IN (?,?) AND created_at>?",
                    (uid, *KINDS, int(time.time()) - 3600)).fetchone()["c"]
    if n >= MAX_PER_HOUR:
        raise HTTPException(429, "Too many changes in the last hour. Try again later or use the website.")


def _num(v, k):
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise HTTPException(422, f"{k} must be a number")
    return float(v)


def _plan(con, u: dict, action: str, account_raw, params: dict) -> dict:
    """Validate against the live state. Returns {aid, label, params, summary, before, after}. Raises 404/409/422, never changes anything."""
    from . import ea
    a = ea.resolve(con, u["id"], account_raw, explicit=True)        # 404 unless the account is the member's own
    aid, label = a["id"], a["label"]
    if action == "auto_off":
        row = con.execute("SELECT enabled FROM auto_settings WHERE account_id=? AND user_id=?", (aid, u["id"])).fetchone()
        if not row or not row["enabled"]:
            raise HTTPException(409, "AutoTrading is already off for this account")
        return {"aid": aid, "label": label, "params": {}, "before": "on", "after": "off",
                "summary": f"Switch AutoTrading OFF for {label}. Trades waiting to be placed will be cancelled. Trades already open are not closed."}
    if not isinstance(params, dict) or not params or set(params) - set(PROT_KEYS):
        raise HTTPException(422, "Choose equity protection mode, risk % or cap % only")
    clean = {}
    if "prot_mode" in params:
        if params["prot_mode"] not in ea.PROT_MODES:
            raise HTTPException(422, "prot_mode must be off, breakeven, stop or close")
        clean["prot_mode"] = params["prot_mode"]
    for k in ("prot_risk_pct", "prot_cap_pct"):
        if k in params:
            clean[k] = _num(params[k], k)
    s = ea.get_manage(con, aid)
    s2, pend0 = copy.deepcopy(s), dict(s.get("prot_pending") or {})
    ea._apply_protection(s2, clean)                                   # the website's own validation; raises 422 on bad values
    pend1 = s2.get("prot_pending") or {}
    if any(pend1.get(k) != pend0.get(k) for k in pend1):
        raise HTTPException(422, "That makes protection weaker. Weaker settings wait 24 hours and are changed on the TradeLens website.")
    changed = [k for k in PROT_KEYS if s2[k] != s[k]]
    if any(ea._prot_looser(k, s2[k], s[k]) for k in changed):
        raise HTTPException(422, "That makes protection weaker. Weaker settings wait 24 hours and are changed on the TradeLens website.")
    if not changed:
        raise HTTPException(422, "Nothing would change")
    names = {"prot_mode": "Equity protection mode", "prot_risk_pct": "Protective stop risk %", "prot_cap_pct": "Cap %"}
    lines = [f"{names[k]}: {s[k]} -> {s2[k]}" for k in changed]
    return {"aid": aid, "label": label, "params": clean, "before": {k: s[k] for k in changed}, "after": {k: s2[k] for k in changed},
            "summary": f"Make equity protection stricter for {label}.\n" + "\n".join(lines) + "\nThis applies at once."}


def _canon(action, aid, params) -> str:
    return json.dumps({"a": action, "i": aid, "p": params}, sort_keys=True, separators=(",", ":"))


def _make_code(uid, tg, action, aid, params, now) -> str:
    claims = {"u": uid, "t": tg, "x": _canon(action, aid, params), "e": now + CODE_TTL, "n": secrets.token_hex(8)}
    body = CODE_PREFIX + "." + connect._b64(json.dumps(claims, sort_keys=True, separators=(",", ":")).encode())
    return body + "." + connect._sig(body)


def _read_code(code, uid, tg, now):
    bad = HTTPException(400, "That confirmation is no longer valid. Start again.")
    try:
        prefix, payload, sig = str(code).split(".")
        if prefix != CODE_PREFIX or not hmac.compare_digest(sig, connect._sig(prefix + "." + payload)):
            raise bad
        c = json.loads(connect._unb64(payload))
        if c["u"] != uid or c["t"] != tg or c["e"] <= now:
            raise bad
        x = json.loads(c["x"])
        return c["n"], x["a"], x["i"], x["p"], c["x"]
    except HTTPException:
        raise
    except Exception:
        raise bad


def _audit(con, aid, uid, kind, detail) -> int:
    n = con.insert("INSERT INTO auto_events(account_id,user_id,kind,detail,created_at) VALUES(?,?,?,?,?)", (aid, uid, kind, str(detail)[:300], int(time.time())))
    con.commit()
    print(f"[connect-controls] account {aid} {kind}", flush=True)
    return n


async def prepare(request):
    """POST {action, account, params?} with a fresh Bearer token. Changes nothing; returns the summary to show and the confirmation code."""
    if not on():
        return NOT_FOUND
    u, lk = connect.require_connect_user(request)
    _fresh(request)
    d = await connect._body(request)
    action = d.get("action")
    if action not in ACTIONS:
        raise HTTPException(400, "Unknown action")
    from . import ea
    with closing(ea.db()) as con:
        _limit(con, u["id"])
        p = _plan(con, u, action, d.get("account"), d.get("params") or {})
    return JSONResponse({"summary": p["summary"], "account": p["label"], "expires_in": CODE_TTL,
                         "code": _make_code(u["id"], lk["telegram_id"], action, p["aid"], p["params"], int(time.time()))})


async def confirm(request):
    """POST {code} with a fresh Bearer token. Re-checks, executes, writes the audit row and returns the state read back."""
    if not on():
        return NOT_FOUND
    u, lk = connect.require_connect_user(request)
    _fresh(request)
    d = await connect._body(request)
    now = int(time.time())
    nonce, action, aid, params, x = _read_code(d.get("code"), u["id"], lk["telegram_id"], now)
    from . import ea
    tag = f"n={nonce} tg={lk['telegram_id']} "
    with closing(ea.db()) as con:
        _limit(con, u["id"])
        if con.execute("SELECT 1 FROM auto_events WHERE user_id=? AND detail LIKE ?", (u["id"], tag.split(" ")[0] + " %")).fetchone():
            raise HTTPException(409, "That confirmation was already used")
        try:
            p = _plan(con, u, action, str(aid), params)
            if _canon(action, p["aid"], p["params"]) != x:
                raise HTTPException(409, "The account changed. Start again.")
        except HTTPException as e:
            _audit(con, aid, u["id"], "connect_rejected", f"{tag}{action} refused: {e.detail}")
            raise
        if action == "auto_off":
            con.execute("UPDATE auto_settings SET enabled=0, updated_at=? WHERE account_id=? AND user_id=?", (now, aid, u["id"]))
            con.execute("UPDATE auto_jobs SET status='skipped', note='You paused AutoTrading', updated_at=? WHERE account_id=? AND status='waiting'", (now, aid))
            con.commit()
            audit = _audit(con, aid, u["id"], "connect_auto_off", f"{tag}AutoTrading on -> off")
            now_on = con.execute("SELECT enabled FROM auto_settings WHERE account_id=?", (aid,)).fetchone()["enabled"]
            state = {"enabled": bool(now_on)}
        else:
            s = ea.get_manage(con, aid)
            ea._apply_protection(s, p["params"])
            ea._save_manage(con, aid, s)
            audit = _audit(con, aid, u["id"], "connect_prot_change", f"{tag}{json.dumps(p['before'])} -> {json.dumps(p['after'])}")
            m = ea.get_manage(con, aid)
            state = {k: m[k] for k in PROT_KEYS}
            state["pending"] = m.get("prot_pending") or {}
    return JSONResponse({"ok": True, "audit_id": audit, "state": state, "account": p["label"]})


routes = [Route("/api/connect/v1/controls/prepare", prepare, methods=["POST"]),
          Route("/api/connect/v1/controls/confirm", confirm, methods=["POST"])]
