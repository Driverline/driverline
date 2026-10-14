"""Take a signal as a trade on the member's OWN Deriv account.

How it works, step by step:
  1. The member taps "Connect Deriv to trade" on a signal. We send them to Deriv's own OAuth page (authorization code
     with PKCE, scope "trade"), using the same registered redirect URL as Continue with Deriv (/auth/deriv/callback).
  2. Deriv sends them back. We swap the one-time code for an access token. Deriv tokens last about one hour, so the token
     is stored ENCRYPTED with its expiry, used only for the member's own trades, and deleted when it expires or when the
     member taps Disconnect. After it expires the member simply taps Connect again.
  3. On a signal the member has already opened, they enter how much they are willing to lose if the stop is hit. We turn
     the setup into a Deriv Multiplier contract (MULTUP for a BUY, MULTDOWN for a SELL) with a stop loss and take profit
     worked out from the setup's own stop and target distances, and we ask Deriv to price it (a quote).
  4. The member reads the summary and taps "Place trade". Only then do we buy the contract through Deriv's
     authenticated WebSocket, with TradeLens's markup (DERIV_MARKUP_PCT, default 2, Deriv's maximum is 3) attached.
The member always places the trade themselves. Nothing here ever trades without that tap, and nothing moves money in or out.

Environment variables (all optional):
  DERIV_TRADING=0       switches trading off everywhere without a redeploy of code
  DERIV_MARKUP_PCT=2    markup percentage sent with every purchase (0 to 3)
  DERIV_MAX_RISK=50     most a member may risk on one trade, in USD
  DERIV_MAX_STAKE=100   largest stake allowed on one trade, in USD
"""
import asyncio
import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import time
from contextlib import closing
from urllib.parse import urlencode

import httpx
import websockets
from cryptography.fernet import Fernet, InvalidToken
from starlette.exceptions import HTTPException
from starlette.responses import JSONResponse, RedirectResponse
from starlette.routing import Route

from . import auth, dbx, deriv, deriv_login
from .config import DERIV_WS_URL, INSTRUMENTS, market

API = "https://api.derivws.com"
ACCOUNTS_URL = API + "/trading/v1/options/accounts"
WS_PREFIX = "wss://api.derivws.com/"
ID_RE = re.compile(r"^[A-Za-z0-9_-]{3,40}$")
STATE_TTL = 600
PLAN_TTL = 120            # seconds a quote stays valid before the member must preview again
STOP_OUT_CAP = 0.8        # the multiplier is chosen so the stop sits well inside Deriv's own stop-out level
PRICE_SLACK = 1.10        # the most we allow Deriv to charge above the quoted price (a safety ceiling, not a target)
MIN_RISK = 0.5
# Used only when Deriv does not tell us which multipliers an index offers; each one is checked by a real quote.
PROBE = [3000, 2000, 1000, 500, 400, 300, 200, 100, 60, 50, 40, 30, 20, 10, 5, 3, 2]


def _num(name: str, default: float) -> float:
    try:
        v = float(os.environ.get(name, default))
        return v if v == v else default
    except (TypeError, ValueError):
        return default


MARKUP = min(3.0, max(0.0, _num("DERIV_MARKUP_PCT", 2.0)))
MAX_RISK = max(MIN_RISK, _num("DERIV_MAX_RISK", 50.0))
MAX_STAKE = max(1.0, _num("DERIV_MAX_STAKE", 100.0))

SCHEMA = """
    CREATE TABLE IF NOT EXISTS deriv_trade_states(state TEXT PRIMARY KEY, verifier TEXT NOT NULL, user_id INTEGER NOT NULL,
        created_at BIGINT NOT NULL);
    CREATE TABLE IF NOT EXISTS deriv_tokens(user_id INTEGER PRIMARY KEY, token TEXT NOT NULL, expires_at BIGINT NOT NULL,
        created_at BIGINT NOT NULL);
    CREATE TABLE IF NOT EXISTS deriv_trades(id {ID}, user_id INTEGER NOT NULL, signal_id BIGINT NOT NULL,
        account_id TEXT NOT NULL, account_type TEXT, symbol TEXT NOT NULL, contract_type TEXT NOT NULL,
        multiplier DOUBLE PRECISION, stake DOUBLE PRECISION, stop_loss DOUBLE PRECISION, take_profit DOUBLE PRECISION,
        max_price DOUBLE PRECISION, buy_price DOUBLE PRECISION, contract_id TEXT, markup_pct DOUBLE PRECISION,
        status TEXT NOT NULL, error TEXT, created_at BIGINT NOT NULL);
    CREATE INDEX IF NOT EXISTS ix_dtrades_user ON deriv_trades(user_id, id);
    CREATE UNIQUE INDEX IF NOT EXISTS ux_dtrades_active ON deriv_trades(user_id, signal_id) WHERE status IN ('sending','placed')"""


def _migrate(con):
    con.add_column("deriv_trades", "result_status", "TEXT")
    con.add_column("deriv_trades", "profit", "DOUBLE PRECISION")
    con.add_column("deriv_trades", "checked_at", "BIGINT")


def db():
    auth.db().close()
    return dbx.connect("dtrade", SCHEMA, _migrate)


def _enabled():
    """(trading is on, the registered Deriv app id)."""
    app_id = deriv_login._cfg()["app_id"]
    return bool(app_id) and os.environ.get("DERIV_TRADING", "1") != "0", app_id


# ---------------------------------------------------------------- the short-lived token, kept encrypted
def _fernet() -> Fernet:
    key = base64.urlsafe_b64encode(hashlib.sha256(b"tradelens-deriv-token|" + auth._secret()).digest())
    return Fernet(key)


def _save_token(uid: int, token: str, expires_in) -> int:
    try:
        life = int(expires_in)
    except (TypeError, ValueError):
        life = 3600
    life = max(60, min(life, 86400))
    now = int(time.time())
    with closing(db()) as con:
        con.execute("INSERT INTO deriv_tokens(user_id,token,expires_at,created_at) VALUES(?,?,?,?) "
                    "ON CONFLICT(user_id) DO UPDATE SET token=excluded.token, expires_at=excluded.expires_at, "
                    "created_at=excluded.created_at", (uid, _fernet().encrypt(token.encode()).decode(), now + life - 30, now))
        con.commit()
    return now + life - 30


def _load_token(uid: int):
    """The member's token, or None when there is none, it has expired, or it cannot be read."""
    with closing(db()) as con:
        r = con.execute("SELECT token, expires_at FROM deriv_tokens WHERE user_id=?", (uid,)).fetchone()
        if not r:
            return None
        if r["expires_at"] <= time.time():
            con.execute("DELETE FROM deriv_tokens WHERE user_id=?", (uid,))
            con.commit()
            return None
    try:
        return _fernet().decrypt(r["token"].encode()).decode()
    except (InvalidToken, ValueError):
        _drop_token(uid)
        return None


def _drop_token(uid: int):
    with closing(db()) as con:
        con.execute("DELETE FROM deriv_tokens WHERE user_id=?", (uid,))
        con.commit()


def _headers(app_id: str, token: str) -> dict:
    return {"Authorization": f"Bearer {token}", "Deriv-App-ID": app_id, "accept": "application/json"}


# ---------------------------------------------------------------- Deriv accounts
def parse_accounts(data) -> list:
    """Every Options account found in Deriv's answer, whatever shape it wraps the list in."""
    out, seen = [], set()

    def walk(x, depth=0):
        if depth > 5:
            return
        if isinstance(x, dict):
            aid = x.get("account_id") or x.get("loginid")
            if isinstance(aid, str) and ID_RE.match(aid) and aid not in seen:
                seen.add(aid)
                bal = x.get("balance")
                out.append({"id": aid, "type": str(x.get("account_type") or "").lower(),
                            "currency": str(x.get("currency") or "USD").upper(),
                            "balance": float(bal) if isinstance(bal, (int, float)) else None,
                            "status": str(x.get("status") or "active").lower()})
            for v in x.values():
                if isinstance(v, (dict, list)):
                    walk(v, depth + 1)
        elif isinstance(x, list):
            for v in x:
                walk(v, depth + 1)

    walk(data)
    return out[:20]


async def _accounts(uid: int, app_id: str, token: str) -> list:
    try:
        async with httpx.AsyncClient(timeout=20) as cl:
            r = await cl.get(ACCOUNTS_URL, headers=_headers(app_id, token))
    except httpx.HTTPError:
        raise HTTPException(502, "Deriv did not answer just now. Please try again in a moment.")
    if r.status_code in (401, 403):
        _drop_token(uid)
        raise HTTPException(409, "Your Deriv connection has expired. Tap Connect Deriv to trade again.")
    if r.status_code >= 400:
        raise HTTPException(502, "Deriv could not list your accounts just now. Please try again.")
    return parse_accounts(r.json())


def _session(user: dict):
    on, app_id = _enabled()
    if not on:
        raise HTTPException(503, "Trading with Deriv is not open yet.")
    token = _load_token(user["id"])
    if not token:
        raise HTTPException(409, "Connect your Deriv account first (tap Connect Deriv to trade).")
    return app_id, token


# ---------------------------------------------------------------- the trade plan (pure maths, no network)
class PlanError(Exception):
    pass


def multiplier_options(resp) -> list:
    """Multipliers Deriv offers for this index, read from its contracts_for answer. Empty if it cannot be read."""
    found = set()

    def walk(x, depth=0):
        if depth > 8:
            return
        if isinstance(x, dict):
            if str(x.get("contract_type", "")).upper() in ("MULTUP", "MULTDOWN"):
                for v in x.get("multiplier_range") or []:
                    if isinstance(v, (int, float)) and v > 0:
                        found.add(float(v))
            for v in x.values():
                if isinstance(v, (dict, list)):
                    walk(v, depth + 1)
        elif isinstance(x, list):
            for v in x:
                walk(v, depth + 1)

    walk(resp)
    return sorted(found)


def stop_fraction(sig) -> float:
    e = float(sig["entry"])
    f = abs(e - float(sig["stop"])) / e if e > 0 else 0.0
    if not f > 0:
        raise PlanError("This setup has no usable stop distance.")
    return f


def candidates(sig, options) -> list:
    """Multipliers whose stop-out is comfortably beyond the setup's stop, largest first."""
    f = stop_fraction(sig)
    ok = sorted({float(m) for m in (options or PROBE) if m * f <= STOP_OUT_CAP}, reverse=True)
    if not ok:
        raise PlanError("The stop on this setup is too wide for the multipliers Deriv offers on this index, so it cannot be "
                        "taken as a Deriv trade.")
    return ok


def plan_for(sig, risk: float, m: float) -> dict:
    """Stake, stop loss and take profit (USD) so that hitting the setup's stop loses `risk` and its target wins risk x RR."""
    f = stop_fraction(sig)
    e, s, t = float(sig["entry"]), float(sig["stop"]), float(sig["target"])
    stake = round(risk / (m * f), 2)
    if stake <= 0:
        raise PlanError("That amount is too small to trade.")
    sl = round(stake * m * f, 2)
    rr = abs(t - e) / abs(e - s)
    tp = round(sl * rr, 2)
    if not (0 < sl < stake) or tp <= 0:
        raise PlanError("This setup cannot be sized at that amount. Try a different amount.")
    if stake > MAX_STAKE:
        raise PlanError(f"That needs a stake of {stake:g} USD, above the {MAX_STAKE:g} USD maximum per trade. Risk a smaller amount.")
    return {"contract_type": "MULTUP" if int(sig["direction"]) > 0 else "MULTDOWN", "multiplier": m, "stake": stake,
            "stop_loss": sl, "take_profit": tp, "risk": sl}


def _n(v) -> str:
    return f"{float(v):.4f}"


def _mult(m):
    """200.0 is sent as 200: Deriv validates multipliers strictly."""
    m = float(m)
    return int(m) if m.is_integer() else m


def _payload(uid, sid, account_id, code, plan, max_price, exp) -> str:
    return "|".join(str(x) for x in ("dt", uid, sid, account_id, code, plan["contract_type"], _n(plan["multiplier"]),
                                     _n(plan["stake"]), _n(plan["stop_loss"]), _n(plan["take_profit"]), _n(max_price), exp))


def sign_plan(*a) -> str:
    return auth._sign(_payload(*a))


def check_sig(sig: str, *a) -> bool:
    return hmac.compare_digest(str(sig), sign_plan(*a))


# ---------------------------------------------------------------- Deriv's public market socket: price a plan, read the spot
async def _spot(ws, code: str):
    try:
        r = await deriv._request(ws, {"ticks_history": code, "count": 1, "end": "latest", "style": "ticks"}, expect="history")
        return float(r["history"]["prices"][-1])
    except Exception:
        return None


def _proposal_req(code: str, plan: dict) -> dict:
    return {"proposal": 1, "contract_type": plan["contract_type"], "underlying_symbol": code, "currency": "USD",
            "amount": plan["stake"], "basis": "stake", "multiplier": _mult(plan["multiplier"]),
            "limit_order": {"stop_loss": plan["stop_loss"], "take_profit": plan["take_profit"]}}


async def _quote_public(sig, risk: float):
    """Returns (plan, proposal dict, spot). Raises PlanError with a message the member can act on."""
    code = INSTRUMENTS[sig["instrument"]][0]
    async with websockets.connect(DERIV_WS_URL, open_timeout=15) as ws:
        options = []
        try:
            options = multiplier_options(await deriv._request(ws, {"contracts_for": code, "currency": "USD"},
                                                              expect="contracts_for"))
        except Exception:
            pass
        spot = await _spot(ws, code)
        last = "Deriv could not price this trade."
        for m in candidates(sig, options)[:6]:
            plan = plan_for(sig, risk, m)
            try:
                resp = await deriv._request(ws, _proposal_req(code, plan), expect="proposal")
            except RuntimeError as e:
                last = str(e).replace("Deriv API error: ", "")
                continue
            return plan, resp.get("proposal") or {}, spot
    raise PlanError(last)


def _fresh_signal(user: dict, sid: int):
    """The signal row, after checking everything that makes it tradable by this member right now."""
    from . import signal_feed as SF
    now = int(time.time())
    with closing(SF.db()) as con:
        sig = con.execute("SELECT * FROM signals WHERE id=?", (sid,)).fetchone()
        if not sig:
            raise HTTPException(404, "Signal not found")
        if market(sig["instrument"]) != "synthetic" or sig["instrument"] not in INSTRUMENTS:
            raise HTTPException(409, "Only synthetic indices can be taken as Deriv trades.")
        if not con.execute("SELECT 1 FROM signal_opens WHERE user_id=? AND signal_id=?", (user["id"], sid)).fetchone():
            raise HTTPException(403, "Open the setup first. That is what counts it towards your daily limit.")
    if sig["status"] != "open" or now > sig["expires_at"]:
        raise HTTPException(409, "This setup is no longer fresh enough to trade.")
    return dict(sig)


async def _json(request):
    return await auth._json(request)


def _pick_account(accts: list, account_id: str) -> dict:
    a = next((x for x in accts if x["id"] == account_id), None)
    if not a:
        raise HTTPException(422, "That Deriv account is not on your connection. Disconnect and connect again.")
    if a["status"] not in ("active", ""):
        raise HTTPException(409, "That Deriv account is not active.")
    if a["currency"] != "USD":
        raise HTTPException(409, "TradeLens trades USD accounts only for now.")
    return a


# ---------------------------------------------------------------- OAuth connect
async def start(request):
    user = auth.current_user(request)
    if not user:
        return RedirectResponse("/login.html", status_code=302)
    on, app_id = _enabled()
    if not on:
        return deriv_login._again("Trading with Deriv is not open yet.", 503)
    state, verifier = secrets.token_urlsafe(24), secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    now = int(time.time())
    with closing(db()) as con:
        con.execute("DELETE FROM deriv_trade_states WHERE created_at<?", (now - STATE_TTL,))
        con.execute("INSERT INTO deriv_trade_states(state,verifier,user_id,created_at) VALUES(?,?,?,?)",
                    (state, verifier, user["id"], now))
        con.commit()
    url = deriv_login.AUTH_URL + "?" + urlencode({
        "response_type": "code", "client_id": app_id, "redirect_uri": deriv_login._base(request) + "/auth/deriv/callback",
        "scope": "trade", "state": state, "code_challenge": challenge, "code_challenge_method": "S256"})
    resp = RedirectResponse(url, status_code=302)
    resp.set_cookie("dl_oauth", state, max_age=STATE_TTL, httponly=True, samesite="lax", secure=auth.SECURE, path="/auth/deriv")
    return resp


def owns_state(state: str) -> bool:
    """True when this OAuth state was started by 'Connect Deriv to trade' (so deriv_login hands the callback to us)."""
    if not state:
        return False
    with closing(db()) as con:
        return bool(con.execute("SELECT 1 FROM deriv_trade_states WHERE state=?", (state,)).fetchone())


async def callback(request):
    q = request.query_params
    state = q.get("state", "")
    with closing(db()) as con:
        r = con.execute("SELECT verifier, user_id, created_at FROM deriv_trade_states WHERE state=?", (state,)).fetchone()
        con.execute("DELETE FROM deriv_trade_states WHERE state=?", (state,))
        con.commit()
    user = auth.current_user(request)
    if q.get("error"):
        return RedirectResponse("/signals.html?deriv=cancelled", status_code=303)
    if not (r and q.get("code") and time.time() - r["created_at"] <= STATE_TTL and request.cookies.get("dl_oauth") == state
            and user and user["id"] == r["user_id"]):
        return deriv_login._again("This connection link has expired or was already used. Please start again from Signals.")
    on, app_id = _enabled()
    if not on:
        return deriv_login._again("Trading with Deriv is not open yet.", 503)
    try:
        async with httpx.AsyncClient(timeout=20) as cl:
            t = await cl.post(deriv_login.TOKEN_URL, headers={"accept": "application/json"},
                              data={"grant_type": "authorization_code", "client_id": app_id, "code": q["code"],
                                    "code_verifier": r["verifier"], "redirect_uri": deriv_login._base(request) + "/auth/deriv/callback"})
            t.raise_for_status()
            body = t.json()
        token = body.get("access_token")
        if not token:
            raise ValueError("no token")
        accts = await _accounts(user["id"], app_id, token)   # also proves the token works
        if not accts:
            return deriv_login._again("We could not find a Deriv trading account for this login. Create one on Deriv first.")
        _save_token(user["id"], token, body.get("expires_in"))
    except HTTPException as e:
        return deriv_login._again(str(e.detail), e.status_code)
    except Exception as e:
        print(f"[deriv-trade] connect failed: {type(e).__name__}", flush=True)
        return deriv_login._again("We could not connect your Deriv account just now. Please try again in a moment.", 502)
    resp = RedirectResponse("/signals.html?deriv=connected", status_code=303)
    resp.delete_cookie("dl_oauth", path="/auth/deriv")
    return resp


# ---------------------------------------------------------------- API
async def status(request):
    user = auth.require_user(request)
    on, _ = _enabled()
    out = {"enabled": on, "connected": False, "markup": MARKUP, "max_risk": MAX_RISK, "min_risk": MIN_RISK}
    if on:
        with closing(db()) as con:
            r = con.execute("SELECT expires_at FROM deriv_tokens WHERE user_id=?", (user["id"],)).fetchone()
        if r and r["expires_at"] > time.time():
            out.update(connected=True, expires_at=r["expires_at"])
    return JSONResponse(out)


async def accounts(request):
    user = auth.require_user(request)
    app_id, token = _session(user)
    accts = [a for a in await _accounts(user["id"], app_id, token) if a["currency"] == "USD"]
    return JSONResponse({"accounts": sorted(accts, key=lambda a: a["type"] != "demo")})


async def disconnect(request):
    user = auth.require_user(request)
    _drop_token(user["id"])
    return JSONResponse({"ok": True})


async def quote(request):
    """Preview only: prices the trade with Deriv and returns a signed, short-lived plan. Nothing is bought."""
    user = auth.require_user(request)
    d = await _json(request)
    try:
        sid, risk = int(d.get("signal_id")), float(d.get("risk"))
    except (TypeError, ValueError):
        raise HTTPException(422, "Enter the amount you are willing to lose as a number.")
    account_id = str(d.get("account_id", ""))
    if not (risk == risk and MIN_RISK <= risk <= MAX_RISK):
        raise HTTPException(422, f"Enter an amount between {MIN_RISK:g} and {MAX_RISK:g} USD.")
    if not ID_RE.match(account_id):
        raise HTTPException(422, "Choose a Deriv account.")
    app_id, token = _session(user)
    sig = _fresh_signal(user, sid)
    acct = _pick_account(await _accounts(user["id"], app_id, token), account_id)
    try:
        plan, prop, spot = await asyncio.wait_for(_quote_public(sig, risk), timeout=30)
    except PlanError as e:
        raise HTTPException(422, str(e))
    except (asyncio.TimeoutError, OSError, websockets.WebSocketException):
        raise HTTPException(502, "Deriv did not answer just now. Please try again in a moment.")
    if acct["balance"] is not None and plan["stake"] > acct["balance"]:
        raise HTTPException(422, f"This needs a stake of {plan['stake']:g} USD but that account has {acct['balance']:g} USD. "
                                 "Risk a smaller amount.")
    from . import signal_feed as SF
    sd = abs(sig["entry"] - sig["stop"])
    if spot is not None and abs(spot - sig["entry"]) > SF.CFG["exec_max_dev_r"] * sd:
        raise HTTPException(409, "The price has already moved too far from this setup's entry, so the trade is no longer "
                                 "attractive. Wait for the next setup.")
    ask = prop.get("ask_price")
    ask = float(ask) if isinstance(ask, (int, float)) else plan["stake"]
    max_price = round(max(ask, plan["stake"]) * PRICE_SLACK + 0.01, 2)
    exp = int(time.time()) + PLAN_TTL
    code = INSTRUMENTS[sig["instrument"]][0]
    return JSONResponse({
        "plan": plan, "account_id": acct["id"], "account_type": acct["type"], "max_price": max_price, "exp": exp,
        "sig": sign_plan(user["id"], sid, acct["id"], code, plan, max_price, exp),
        "name": INSTRUMENTS[sig["instrument"]][1], "direction": "BUY" if sig["direction"] > 0 else "SELL",
        "ask_price": ask, "longcode": prop.get("longcode"), "markup": MARKUP})


def _err_text(resp: dict):
    """Everything Deriv says about a refusal (message, code, details), or None when the answer is not an error."""
    e = resp.get("error")
    if not isinstance(e, dict):
        errs = resp.get("errors")
        e = errs[0] if isinstance(errs, list) and errs and isinstance(errs[0], dict) else None
    if e is None:
        return None
    parts = [str(e.get("message") or e.get("code") or "refused")]
    for k in ("details", "field", "fields"):
        if e.get(k):
            parts.append(f"{k}={json.dumps(e[k])[:200]}")
    return " | ".join(parts)


async def _otp_url(app_id: str, token: str, account_id: str) -> str:
    """A one-time authenticated WebSocket address for one Deriv account. Never logged."""
    async with httpx.AsyncClient(timeout=20) as cl:
        r = await cl.post(f"{ACCOUNTS_URL}/{account_id}/otp", headers=_headers(app_id, token))
    if r.status_code in (401, 403):
        raise PermissionError("expired")
    r.raise_for_status()
    url = (r.json().get("data") or {}).get("url", "")
    if not (isinstance(url, str) and url.startswith(WS_PREFIX)):
        raise RuntimeError("Deriv returned an unexpected connection address")
    return url


async def _buy_once(app_id: str, token: str, account_id: str, code: str, plan: dict, max_price: float, with_markup: bool):
    url = await _otp_url(app_id, token, account_id)
    params = {"contract_type": plan["contract_type"], "currency": "USD", "underlying_symbol": code, "amount": plan["stake"],
              "basis": "stake", "multiplier": _mult(plan["multiplier"]),
              "limit_order": {"stop_loss": plan["stop_loss"], "take_profit": plan["take_profit"]}}
    if with_markup:
        params["app_markup_percentage"] = MARKUP
    async with websockets.connect(url, open_timeout=15) as ws:      # the address holds a one-time code: never logged
        await ws.send(json.dumps({"buy": "1", "price": max_price, "parameters": params}))
        for _ in range(5):
            resp = json.loads(await ws.recv())
            err = _err_text(resp)
            if err:
                raise RuntimeError("Deriv API error: " + err)
            if isinstance(resp.get("buy"), dict):
                return resp["buy"]
    raise RuntimeError("Deriv sent no confirmation of the purchase")


# If Deriv refuses the markup field itself, trades still go through without it and we stop sending it until a restart.
_markup_in_request = {"on": True}


async def _buy(app_id: str, token: str, account_id: str, code: str, plan: dict, max_price: float):
    """Buys the contract. Returns (Deriv's buy answer, whether the markup was sent with it)."""
    if MARKUP > 0 and _markup_in_request["on"]:
        try:
            return await _buy_once(app_id, token, account_id, code, plan, max_price, True), True
        except RuntimeError as e:
            if "validation" not in str(e).lower():
                raise
            print(f"[deriv-trade] Deriv refused the request with app_markup_percentage in it ({str(e)[:200]}); retrying "
                  "without it. Set the markup on your app in developers.deriv.com instead.", flush=True)
            _markup_in_request["on"] = False
    return await _buy_once(app_id, token, account_id, code, plan, max_price, False), False


async def trade(request):
    """The member confirmed. Re-checks everything, then buys the contract on their Deriv account."""
    user = auth.require_user(request)
    d = await _json(request)
    plan = d.get("plan") if isinstance(d.get("plan"), dict) else {}
    try:
        sid, exp = int(d.get("signal_id")), int(d.get("exp"))
        plan = {"contract_type": str(plan["contract_type"]), "multiplier": float(plan["multiplier"]),
                "stake": float(plan["stake"]), "stop_loss": float(plan["stop_loss"]), "take_profit": float(plan["take_profit"])}
        max_price = float(d.get("max_price"))
    except (TypeError, ValueError, KeyError):
        raise HTTPException(422, "That quote is not valid. Preview the trade again.")
    account_id = str(d.get("account_id", ""))
    if not ID_RE.match(account_id):
        raise HTTPException(422, "Choose a Deriv account.")
    app_id, token = _session(user)
    sig = _fresh_signal(user, sid)
    code = INSTRUMENTS[sig["instrument"]][0]
    if not check_sig(d.get("sig", ""), user["id"], sid, account_id, code, plan, max_price, exp):
        raise HTTPException(422, "That quote is not valid. Preview the trade again.")
    if time.time() > exp:
        raise HTTPException(409, "That quote has expired because prices move. Preview the trade again.")
    if plan["contract_type"] != ("MULTUP" if sig["direction"] > 0 else "MULTDOWN") or plan["stake"] > MAX_STAKE:
        raise HTTPException(422, "That quote is not valid. Preview the trade again.")
    acct = _pick_account(await _accounts(user["id"], app_id, token), account_id)
    if acct["type"] == "real" and d.get("confirm_real") is not True:
        raise HTTPException(422, "Confirm that this is a real-money trade you are placing yourself.")
    now = int(time.time())
    with closing(db()) as con:
        con.execute("UPDATE deriv_trades SET status='failed', error='no answer' WHERE user_id=? AND status='sending' "
                    "AND created_at<?", (user["id"], now - 90))
        try:
            tid = con.insert("INSERT INTO deriv_trades(user_id,signal_id,account_id,account_type,symbol,contract_type,"
                             "multiplier,stake,stop_loss,take_profit,max_price,markup_pct,status,created_at) "
                             "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                             (user["id"], sid, account_id, acct["type"], code, plan["contract_type"], plan["multiplier"],
                              plan["stake"], plan["stop_loss"], plan["take_profit"], max_price, MARKUP, "sending", now))
            con.commit()
        except Exception as e:
            if dbx.is_duplicate(e):
                raise HTTPException(409, "You already took this setup as a Deriv trade.")
            raise

    def finish(status: str, **cols):
        with closing(db()) as c2:
            sets = ", ".join(f"{k}=?" for k in ("status", *cols))
            c2.execute(f"UPDATE deriv_trades SET {sets} WHERE id=?", (status, *cols.values(), tid))
            c2.commit()

    try:
        got, sent_markup = await asyncio.wait_for(_buy(app_id, token, account_id, code, plan, max_price), timeout=45)
    except PermissionError:
        _drop_token(user["id"])
        finish("failed", error="connection expired")
        raise HTTPException(409, "Your Deriv connection has expired. Tap Connect Deriv to trade again.")
    except RuntimeError as e:         # Deriv refused the order: its own message is safe and useful to show
        msg = str(e).replace("Deriv API error: ", "")[:400]
        print(f"[deriv-trade] order refused: {msg}", flush=True)
        finish("failed", error=msg)
        raise HTTPException(422, f"Deriv did not place the trade: {msg}")
    except Exception as e:
        # Do not keep the exception text: connection errors can carry the one-time socket address.
        print(f"[deriv-trade] buy failed: {type(e).__name__}", flush=True)
        finish("failed", error=type(e).__name__)
        raise HTTPException(502, "We could not reach Deriv to place the trade. Check your Deriv app before trying again, so "
                                 "you do not end up with two trades.")
    bp = got.get("buy_price")
    finish("placed", contract_id=str(got.get("contract_id") or ""), buy_price=float(bp) if isinstance(bp, (int, float)) else None,
           markup_pct=MARKUP if sent_markup else 0.0)
    return JSONResponse({"ok": True, "markup_sent": sent_markup, "contract_id": got.get("contract_id"), "buy_price": bp, "longcode": got.get("longcode"),
                         "account_type": acct["type"]})


async def my_trades(request):
    user = auth.require_user(request)
    with closing(db()) as con:
        rows = con.execute("SELECT id, signal_id, account_type, symbol, contract_type, multiplier, stake, stop_loss, "
                           "take_profit, buy_price, contract_id, status, created_at, result_status, profit FROM deriv_trades WHERE user_id=? "
                           "AND status IN ('placed','failed') ORDER BY id DESC LIMIT 15", (user["id"],)).fetchall()
    return JSONResponse({"trades": [dict(r) for r in rows]})


def _num_or_none(v):
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


async def check_trade(request):
    """Asks Deriv whether one of the member's contracts is still open, and what it made or lost. Read-only."""
    user = auth.require_user(request)
    tid = request.path_params["tid"]
    app_id, token = _session(user)
    with closing(db()) as con:
        r = con.execute("SELECT account_id, contract_id FROM deriv_trades WHERE id=? AND user_id=? AND status='placed'",
                        (tid, user["id"])).fetchone()
    try:
        cid = int(r["contract_id"]) if r else 0
    except (TypeError, ValueError):
        cid = 0
    if not cid:
        raise HTTPException(404, "Trade not found")
    try:
        url = await _otp_url(app_id, token, r["account_id"])
        async with websockets.connect(url, open_timeout=15) as ws:
            resp = await asyncio.wait_for(deriv._request(ws, {"proposal_open_contract": 1, "contract_id": cid},
                                                         expect="proposal_open_contract"), timeout=20)
    except PermissionError:
        _drop_token(user["id"])
        raise HTTPException(409, "Your Deriv connection has expired. Tap Connect Deriv to trade again, then check again.")
    except RuntimeError as e:
        raise HTTPException(422, "Deriv could not give the result: " + str(e).replace("Deriv API error: ", "")[:200])
    except (asyncio.TimeoutError, OSError, httpx.HTTPError, websockets.WebSocketException):
        raise HTTPException(502, "Deriv did not answer just now. Please try again in a moment.")
    c = resp.get("proposal_open_contract") or {}
    status = str(c.get("status") or "").lower()
    closed = bool(c.get("is_sold")) or status in ("won", "lost", "sold")
    profit = _num_or_none(c.get("profit"))
    if profit is None and _num_or_none(c.get("sell_price")) is not None and _num_or_none(c.get("buy_price")) is not None:
        profit = round(float(c["sell_price"]) - float(c["buy_price"]), 2)
    if closed and profit is not None:
        with closing(db()) as con:
            con.execute("UPDATE deriv_trades SET result_status=?, profit=?, checked_at=? WHERE id=? AND user_id=?",
                        (status or "closed", profit, int(time.time()), tid, user["id"]))
            con.commit()
    return JSONResponse({"closed": closed, "profit": profit, "status": status})


routes = [
    Route("/auth/deriv/trade/start", start, methods=["GET"]),
    Route("/api/deriv/status", status, methods=["GET"]),
    Route("/api/deriv/accounts", accounts, methods=["GET"]),
    Route("/api/deriv/disconnect", disconnect, methods=["POST"]),
    Route("/api/deriv/quote", quote, methods=["POST"]),
    Route("/api/deriv/trade", trade, methods=["POST"]),
    Route("/api/deriv/trades", my_trades, methods=["GET"]),
    Route("/api/deriv/trades/{tid:int}/check", check_trade, methods=["POST"]),
]
