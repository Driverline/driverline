"""Deriv Options (Rise/Fall and Accumulators) from TradeLens: manual trades the member confirms, and AutoTrading sessions.

This is an ADDITIVE feature. It does not change deriv_trade.py (Multipliers): it only reads that module's connection helpers
(the member's short-lived encrypted token, account list, one-time socket address). The rules live in options_logic.py.

Switches
  DERIV_OPTIONS=1            turns the feature on (default off: no tab, every route answers 404)
  opt_meta 'mode'            off | testers (default) | public      who sees the page and may place confirmed trades
  opt_meta 'auto'            off | testers (default)              who may run AutoTrading sessions
Real-money rules
  * a trade on a REAL account needs the member's own confirmation (manual) or a session started with the real-money box ticked
  * a real account is only traded when the strategy has a stored backtest whose lower bound still beats break-even at the payout
    Deriv quotes right now. Demo accounts can run any strategy (that is what demo is for).
AutoTrading works while the member's Deriv connection lives (about an hour); it then stops and asks them to connect again.
"""
import asyncio
import hmac
import json
import os
import time
from contextlib import closing
from datetime import datetime, timezone

import websockets
from starlette.exceptions import HTTPException
from starlette.responses import JSONResponse
from starlette.routing import Route

from . import auth, dbx, deriv
from . import deriv_trade as DT
from . import options_logic as L
from .config import DERIV_WS_URL, INSTRUMENTS

SCHEMA = """
    CREATE TABLE IF NOT EXISTS opt_meta(k TEXT PRIMARY KEY, v TEXT);
    CREATE TABLE IF NOT EXISTS opt_settings(user_id INTEGER PRIMARY KEY, cfg TEXT NOT NULL, auto_on INTEGER NOT NULL DEFAULT 0,
        account_id TEXT, account_type TEXT, session_started BIGINT, session_ends BIGINT, reset_at BIGINT, last_note TEXT,
        updated_at BIGINT NOT NULL);
    CREATE TABLE IF NOT EXISTS opt_trades(id {ID}, user_id INTEGER NOT NULL, account_id TEXT NOT NULL, account_type TEXT NOT NULL,
        symbol TEXT NOT NULL, strategy TEXT NOT NULL, contract_type TEXT NOT NULL, duration INTEGER, stake DOUBLE PRECISION NOT NULL,
        ask_price DOUBLE PRECISION, payout DOUBLE PRECISION, buy_price DOUBLE PRECISION, contract_id TEXT, step INTEGER NOT NULL DEFAULT 0,
        source TEXT NOT NULL, status TEXT NOT NULL, error TEXT, result_status TEXT, profit DOUBLE PRECISION, created_at BIGINT NOT NULL,
        closed_at BIGINT);
    CREATE INDEX IF NOT EXISTS ix_opt_trades_user ON opt_trades(user_id, id);
    CREATE TABLE IF NOT EXISTS opt_validation(strategy TEXT NOT NULL, symbol TEXT NOT NULL, duration INTEGER NOT NULL, n INTEGER NOT NULL,
        wins INTEGER NOT NULL, hit DOUBLE PRECISION NOT NULL, ratio DOUBLE PRECISION NOT NULL, breakeven DOUBLE PRECISION NOT NULL,
        lower DOUBLE PRECISION NOT NULL, ev DOUBLE PRECISION NOT NULL, edge INTEGER NOT NULL, bars INTEGER NOT NULL, updated_at BIGINT NOT NULL,
        PRIMARY KEY(strategy, symbol, duration));
    CREATE TABLE IF NOT EXISTS opt_events(id {ID}, user_id INTEGER NOT NULL, kind TEXT NOT NULL, detail TEXT, created_at BIGINT NOT NULL);
    CREATE INDEX IF NOT EXISTS ix_opt_events_user ON opt_events(user_id, id)"""

PLAN_TTL = 60
LOOP_SECONDS = 20
CANDLE_BARS = 400            # M1 candles read for a live signal
BACKTEST_BARS = 5000         # M1 candles read for a backtest (Deriv's per-request ceiling)
PAYOUT_RATIO_ASSUMED = float(os.environ.get("DERIV_OPT_PAYOUT_RATIO", "0.85"))     # only for the offline backtest
_locks: dict = {}


def on() -> bool:
    return os.environ.get("DERIV_OPTIONS", "0") == "1"


def db():
    auth.db().close()
    return dbx.connect("dopt", SCHEMA)


def _now() -> int:
    return int(time.time())


def _day_start(now: int) -> int:
    return now - now % 86400


# ---------------------------------------------------------------- who may use it
def _meta(con, k, default):
    r = con.execute("SELECT v FROM opt_meta WHERE k=?", (k,)).fetchone()
    return r["v"] if r else default


def _testers(con) -> list:
    try:
        return [int(x) for x in json.loads(_meta(con, "testers", "[]"))]
    except (ValueError, TypeError):
        return []


def access(user: dict) -> dict:
    """{'page': may open the page and confirm trades, 'auto': may run AutoTrading sessions}."""
    if not on():
        return {"page": False, "auto": False}
    with closing(db()) as con:
        mode, auto, ids = _meta(con, "mode", "testers"), _meta(con, "auto", "testers"), _testers(con)
    staff = user["tier"] == "admin" or user["id"] in ids
    page = mode == "public" or (mode == "testers" and staff)
    return {"page": page, "auto": page and auto == "testers" and staff}


def _need_page(user):
    if not access(user)["page"]:
        raise HTTPException(404, "Not found")


# ---------------------------------------------------------------- settings storage
def _load_cfg(con, uid: int) -> dict:
    r = con.execute("SELECT * FROM opt_settings WHERE user_id=?", (uid,)).fetchone()
    cfg = dict(L.DEFAULTS)
    if r:
        try:
            cfg.update(json.loads(r["cfg"]))
        except ValueError:
            pass
    return cfg, (dict(r) if r else None)


def _save_row(con, uid: int, **cols):
    now = _now()
    if not con.execute("SELECT 1 FROM opt_settings WHERE user_id=?", (uid,)).fetchone():
        con.execute("INSERT INTO opt_settings(user_id,cfg,updated_at) VALUES(?,?,?)", (uid, json.dumps(L.DEFAULTS), now))
    cols["updated_at"] = now
    con.execute("UPDATE opt_settings SET " + ", ".join(f"{k}=?" for k in cols) + " WHERE user_id=?", (*cols.values(), uid))
    con.commit()


def _event(con, uid: int, kind: str, detail: str):
    con.execute("INSERT INTO opt_events(user_id,kind,detail,created_at) VALUES(?,?,?,?)", (uid, kind, str(detail)[:300], _now()))
    con.execute("DELETE FROM opt_events WHERE user_id=? AND id NOT IN (SELECT id FROM opt_events WHERE user_id=? ORDER BY id DESC LIMIT 50)",
                (uid, uid))
    con.commit()


def _note(uid: int, text: str):
    """Remember why AutoTrading is not trading, and write an event only when the reason changes (the loop runs every 20 s)."""
    with closing(db()) as con:
        r = con.execute("SELECT last_note FROM opt_settings WHERE user_id=?", (uid,)).fetchone()
        if r and r["last_note"] != text:
            con.execute("UPDATE opt_settings SET last_note=? WHERE user_id=?", (text, uid))
            _event(con, uid, "note", text)


# ---------------------------------------------------------------- stats the risk rules need
def _stats(con, uid: int, account_id: str, cfg: dict, row, now: int) -> dict:
    ds = _day_start(now)
    t = con.execute("SELECT COALESCE(SUM(profit),0) AS p, COUNT(*) AS n FROM opt_trades WHERE user_id=? AND account_id=? AND status='placed' "
                    "AND created_at>=?", (uid, account_id, ds)).fetchone()
    pnl = con.execute("SELECT COALESCE(SUM(profit),0) AS p FROM opt_trades WHERE user_id=? AND account_id=? AND status='placed' "
                      "AND result_status IS NOT NULL AND created_at>=?", (uid, account_id, ds)).fetchone()["p"]
    opens = con.execute("SELECT symbol, stake FROM opt_trades WHERE user_id=? AND account_id=? AND status IN ('placed','sending') "
                        "AND result_status IS NULL", (uid, account_id)).fetchall()
    reset = (row or {}).get("reset_at") or 0
    last = con.execute("SELECT profit, step, closed_at FROM opt_trades WHERE user_id=? AND account_id=? AND status='placed' AND "
                       "result_status IS NOT NULL AND closed_at>? ORDER BY id DESC LIMIT 30", (uid, account_id, reset)).fetchall()
    consec = 0
    for r in last:
        if r["profit"] is not None and r["profit"] < 0:
            consec += 1
        else:
            break
    session = None
    if row and row.get("auto_on") and row.get("session_started"):
        session = con.execute("SELECT COALESCE(SUM(profit),0) AS p FROM opt_trades WHERE user_id=? AND account_id=? AND source='auto' "
                              "AND status='placed' AND created_at>=?", (uid, account_id, row["session_started"])).fetchone()["p"]
    return {"today_pnl": float(pnl), "open_stake": float(sum(o["stake"] for o in opens)), "today_n": int(t["n"]), "open_n": len(opens),
            "open_symbols": [o["symbol"] for o in opens], "consec_losses": consec,
            "last_close": last[0]["closed_at"] if last else None, "session_pnl": session,
            "last_closed": dict(last[0]) if last else None}


# ---------------------------------------------------------------- talking to Deriv
class PlanError(Exception):
    pass


def _contract(cfg: dict, direction: int, stake: float) -> dict:
    if L.STRATEGIES[cfg["strategy"]]["kind"] == "accu":
        return {"contract_type": "ACCU", "amount": stake, "growth_rate": cfg["growth_rate"],
                "limit_order": {"take_profit": min(cfg["accu_tp"], DT.MAX_STAKE)}}
    return {"contract_type": "CALL" if direction > 0 else "PUT", "amount": stake, "duration": cfg["duration"], "duration_unit": "m"}


async def _proposal(code: str, contract: dict):
    req = {"proposal": 1, "currency": "USD", "underlying_symbol": code, "basis": "stake", **contract}
    try:
        async with websockets.connect(DERIV_WS_URL, open_timeout=15) as ws:
            r = await deriv._request(ws, req, expect="proposal")
    except RuntimeError as e:
        raise PlanError(str(e).replace("Deriv API error: ", "")[:300])
    p = r.get("proposal") or {}
    ask, pay = p.get("ask_price"), p.get("payout")
    return {"ask": float(ask) if isinstance(ask, (int, float)) else contract["amount"],
            "payout": float(pay) if isinstance(pay, (int, float)) else None, "longcode": p.get("longcode")}


async def _closes(code: str, bars: int) -> list:
    async with websockets.connect(DERIV_WS_URL, open_timeout=15, ping_interval=20) as ws:
        candles = await deriv._candles(ws, code, 60, bars)
    return [float(c["close"]) for c in candles]


async def _buy(app_id, token, account_id, code, contract: dict, max_price: float) -> dict:
    url = await DT._otp_url(app_id, token, account_id)
    params = {"currency": "USD", "underlying_symbol": code, "basis": "stake", **contract}
    if DT.MARKUP > 0 and DT._markup_in_request["on"]:
        params["app_markup_percentage"] = DT.MARKUP
    for attempt in (0, 1):
        async with websockets.connect(url if attempt == 0 else await DT._otp_url(app_id, token, account_id), open_timeout=15) as ws:
            await ws.send(json.dumps({"buy": "1", "price": max_price, "parameters": params}))
            for _ in range(5):
                resp = json.loads(await ws.recv())
                err = DT._err_text(resp)
                if err:
                    if attempt == 0 and "app_markup_percentage" in params and "validation" in err.lower():
                        DT._markup_in_request["on"] = False
                        params.pop("app_markup_percentage")
                        break
                    raise RuntimeError("Deriv API error: " + err)
                if isinstance(resp.get("buy"), dict):
                    return resp["buy"]
            else:
                raise RuntimeError("Deriv sent no confirmation of the purchase")
    raise RuntimeError("Deriv refused the purchase")


async def _check_contract(app_id, token, account_id, cid: int):
    url = await DT._otp_url(app_id, token, account_id)
    async with websockets.connect(url, open_timeout=15) as ws:
        resp = await asyncio.wait_for(deriv._request(ws, {"proposal_open_contract": 1, "contract_id": cid},
                                                     expect="proposal_open_contract"), timeout=20)
    c = resp.get("proposal_open_contract") or {}
    status = str(c.get("status") or "").lower()
    closed = bool(c.get("is_sold")) or status in ("won", "lost", "sold")
    profit = DT._num_or_none(c.get("profit"))
    if profit is None and DT._num_or_none(c.get("sell_price")) is not None and DT._num_or_none(c.get("buy_price")) is not None:
        profit = round(float(c["sell_price"]) - float(c["buy_price"]), 2)
    return closed and profit is not None, profit, status


async def _settle_open(uid: int, app_id: str, token: str) -> None:
    """Asks Deriv about every open contract of this member and stores the results (read-only on Deriv's side)."""
    with closing(db()) as con:
        rows = con.execute("SELECT id, account_id, contract_id FROM opt_trades WHERE user_id=? AND status='placed' AND result_status IS NULL "
                           "ORDER BY id LIMIT 10", (uid,)).fetchall()
    for r in rows:
        try:
            cid = int(r["contract_id"])
        except (TypeError, ValueError):
            continue
        try:
            closed, profit, status = await _check_contract(app_id, token, r["account_id"], cid)
        except PermissionError:
            raise
        except Exception as e:
            print(f"[deriv-options] settle failed: {type(e).__name__}", flush=True)
            continue
        if closed:
            with closing(db()) as con:
                con.execute("UPDATE opt_trades SET result_status=?, profit=?, closed_at=? WHERE id=?", (status or "closed", profit, _now(), r["id"]))
                con.commit()


# ---------------------------------------------------------------- placing one trade (shared by manual and auto)
def _validation(con, strategy, code_key, duration):
    r = con.execute("SELECT * FROM opt_validation WHERE strategy=? AND symbol=? AND duration=?", (strategy, code_key, duration)).fetchone()
    return dict(r) if r else None


def _edge_gate(cfg: dict, symbol_key: str, acct_type: str, quote: dict):
    """None when the trade may go ahead, else the reason. Real accounts always need a proven edge; demo only when asked."""
    needs = acct_type == "real" or cfg["require_edge"]
    if not needs:
        return None
    if not L.STRATEGIES[cfg["strategy"]]["backtest"]:
        return "This strategy has no backtest yet, so it runs on demo accounts only." if acct_type == "real" else \
            "This strategy has no backtest, so it cannot pass the edge check. Switch the edge check off to use it on demo."
    with closing(db()) as con:
        v = _validation(con, cfg["strategy"], symbol_key, cfg["duration"])
    ratio = L.ratio_from_quote(quote["ask"], quote["payout"])
    if not L.edge_ok(v, ratio):
        have = f"best tested hit rate {v['lower'] * 100:.1f}% on {v['n']} trades" if v and v["n"] else "no backtest stored"
        need = f"{L.breakeven(ratio) * 100:.1f}%" if ratio > 0 else "unknown (no payout quoted)"
        return f"No proven edge for this strategy on this symbol: {have}; break-even at today's payout is {need}."
    return None


def _sig(uid, account_id, code, contract, max_price, exp) -> str:
    return auth._sign("|".join(str(x) for x in ("opt", uid, account_id, code, json.dumps(contract, sort_keys=True), max_price, exp)))


async def _place(user: dict, app_id: str, token: str, acct: dict, cfg: dict, symbol_key: str, contract: dict, max_price: float,
                 step: int, source: str, quote: dict) -> dict:
    code = INSTRUMENTS[symbol_key][0]
    uid, now = user["id"], _now()
    with closing(db()) as con:
        con.execute("UPDATE opt_trades SET status='failed', error='no answer' WHERE user_id=? AND status='sending' AND created_at<?", (uid, now - 90))
        if con.execute("SELECT 1 FROM opt_trades WHERE user_id=? AND status='sending'", (uid,)).fetchone():
            raise HTTPException(409, "A trade is being placed. Wait a moment.")
        tid = con.insert("INSERT INTO opt_trades(user_id,account_id,account_type,symbol,strategy,contract_type,duration,stake,ask_price,payout,"
                         "step,source,status,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                         (uid, acct["id"], acct["type"], symbol_key, cfg["strategy"], contract["contract_type"], contract.get("duration"),
                          contract["amount"], quote["ask"], quote["payout"], step, source, "sending", now))
        con.commit()

    def finish(status, **cols):
        with closing(db()) as c2:
            c2.execute("UPDATE opt_trades SET " + ", ".join(f"{k}=?" for k in ("status", *cols)) + " WHERE id=?", (status, *cols.values(), tid))
            c2.commit()

    try:
        got = await asyncio.wait_for(_buy(app_id, token, acct["id"], code, contract, max_price), timeout=45)
    except PermissionError:
        DT._drop_token(uid)
        finish("failed", error="connection expired")
        raise HTTPException(409, "Your Deriv connection has expired. Tap Connect Deriv to trade again.")
    except RuntimeError as e:
        msg = str(e).replace("Deriv API error: ", "")[:400]
        finish("failed", error=msg)
        raise HTTPException(422, f"Deriv did not place the trade: {msg}")
    except Exception as e:
        print(f"[deriv-options] buy failed: {type(e).__name__}", flush=True)     # never the exception text: it can hold a one-time address
        finish("failed", error=type(e).__name__)
        raise HTTPException(502, "We could not reach Deriv to place the trade. Check your Deriv app before trying again.")
    bp = got.get("buy_price")
    finish("placed", contract_id=str(got.get("contract_id") or ""), buy_price=float(bp) if isinstance(bp, (int, float)) else None)
    return {"id": tid, "contract_id": got.get("contract_id"), "buy_price": bp, "longcode": got.get("longcode")}


async def start(request):
    """Same Deriv connection as the Signals page; a cookie only tells the callback to bring the member back to Options."""
    resp = await DT.start(request)
    try:
        resp.set_cookie("dl_next", "options", max_age=DT.STATE_TTL, httponly=True, samesite="lax", secure=auth.SECURE, path="/auth/deriv")
    except AttributeError:
        pass
    return resp


# ---------------------------------------------------------------- manual: ideas, quote, trade
async def ideas(request):
    user = auth.require_user(request)
    _need_page(user)
    key = request.query_params.get("symbol", "")
    if key not in L.SYMBOLS:
        raise HTTPException(422, "Choose a symbol from the list.")
    try:
        closes = await asyncio.wait_for(_closes(INSTRUMENTS[key][0], CANDLE_BARS), timeout=30)
    except Exception:
        raise HTTPException(502, "Deriv did not answer just now. Please try again in a moment.")
    out = []
    for s, meta in L.STRATEGIES.items():
        d = L.latest_signal(s, closes)
        out.append({"strategy": s, "label": meta["label"], "kind": meta["kind"], "signal": d,
                    "side": None if not d else ("Accumulator" if meta["kind"] == "accu" else "Rise" if d > 0 else "Fall")})
    return JSONResponse({"symbol": key, "name": INSTRUMENTS[key][1], "ideas": out, "last": closes[-1] if closes else None})


def _body_cfg(user_cfg: dict, d: dict) -> dict:
    cfg = dict(user_cfg)
    s = str(d.get("strategy", cfg["strategy"]))
    if s not in L.STRATEGIES:
        raise HTTPException(422, "Choose a strategy from the list.")
    cfg["strategy"] = s
    return cfg


async def _prepare(user, d, for_trade: bool):
    """Shared by quote and trade: returns (cfg, symbol_key, direction, app_id, token, acct, stats, row)."""
    from . import partner
    partner.require_tagged(user)
    key = str(d.get("symbol", ""))
    if key not in L.SYMBOLS:
        raise HTTPException(422, "Choose a symbol from the list.")
    try:
        direction = int(d.get("direction", 0))
    except (TypeError, ValueError):
        direction = 0
    with closing(db()) as con:
        cfg, row = _load_cfg(con, user["id"])
    cfg = _body_cfg(cfg, d)
    if L.STRATEGIES[cfg["strategy"]]["kind"] == "updown" and direction not in (1, -1):
        raise HTTPException(422, "Choose Rise or Fall.")
    account_id = str(d.get("account_id", ""))
    if not DT.ID_RE.match(account_id):
        raise HTTPException(422, "Choose a Deriv account.")
    app_id, token = DT._session(user)
    acct = DT._pick_account(await DT._accounts(user["id"], app_id, token), account_id)
    if acct["type"] == "real" and not L.STRATEGIES[cfg["strategy"]]["backtest"]:
        raise HTTPException(409, "This strategy is for demo accounts only until it has a backtest.")
    with closing(db()) as con:
        st = _stats(con, user["id"], account_id, cfg, row, _now())
    why = L.risk_block(cfg, st, _now())
    if why:
        raise HTTPException(409, why)
    return cfg, key, direction, app_id, token, acct, st


async def quote(request):
    """Preview only: prices the trade with Deriv and returns a signed, short-lived plan. Nothing is bought."""
    user = auth.require_user(request)
    _need_page(user)
    d = await auth._json(request)
    cfg, key, direction, app_id, token, acct, st = await _prepare(user, d, False)
    step = L.next_step(cfg, st["last_closed"])
    raw = d.get("stake")
    try:
        stake = L.stake_for(cfg, acct["balance"], step) if raw in (None, "") else round(float(raw), 2)
    except (TypeError, ValueError):
        raise HTTPException(422, "Enter the stake as a number.")
    if not (L.MIN_STAKE <= stake <= min(cfg["max_stake"], DT.MAX_STAKE)):
        raise HTTPException(422, f"Enter a stake between {L.MIN_STAKE:g} and {min(cfg['max_stake'], DT.MAX_STAKE):g} USD (your largest stake).")
    if acct["balance"] is not None and stake > acct["balance"]:
        raise HTTPException(422, "That stake is more than the account balance.")
    contract = _contract(cfg, direction, stake)
    try:
        q = await asyncio.wait_for(_proposal(INSTRUMENTS[key][0], contract), timeout=30)
    except PlanError as e:
        raise HTTPException(422, str(e))
    except (asyncio.TimeoutError, OSError, websockets.WebSocketException):
        raise HTTPException(502, "Deriv did not answer just now. Please try again in a moment.")
    blocked = _edge_gate(cfg, key, acct["type"], q)
    ratio = L.ratio_from_quote(q["ask"], q["payout"])
    max_price = round(max(q["ask"], stake) * DT.PRICE_SLACK + 0.01, 2)
    exp = _now() + PLAN_TTL
    return JSONResponse({"contract": contract, "symbol": key, "name": INSTRUMENTS[key][1], "account_id": acct["id"],
                         "account_type": acct["type"], "strategy": cfg["strategy"], "step": step, "stake": stake, "ask_price": q["ask"],
                         "payout": q["payout"], "breakeven": round(L.breakeven(ratio), 4) if ratio > 0 else None, "longcode": q["longcode"],
                         "max_price": max_price, "exp": exp, "blocked": blocked, "markup": DT.MARKUP,
                         "sig": _sig(user["id"], acct["id"], INSTRUMENTS[key][0], contract, max_price, exp)})


async def trade(request):
    """The member confirmed. Re-checks the signature, the risk rules and the edge rule, then buys."""
    user = auth.require_user(request)
    _need_page(user)
    d = await auth._json(request)
    contract = d.get("contract") if isinstance(d.get("contract"), dict) else {}
    try:
        exp, max_price = int(d.get("exp")), float(d.get("max_price"))
        contract = json.loads(json.dumps(contract))
        stake = float(contract["amount"])
    except (TypeError, ValueError, KeyError):
        raise HTTPException(422, "That quote is not valid. Preview the trade again.")
    cfg, key, direction, app_id, token, acct, st = await _prepare(user, d, True)
    code = INSTRUMENTS[key][0]
    if not hmac.compare_digest(str(d.get("sig", "")), _sig(user["id"], acct["id"], code, contract, max_price, exp)):
        raise HTTPException(422, "That quote is not valid. Preview the trade again.")
    if _now() > exp:
        raise HTTPException(409, "That quote has expired because prices move. Preview the trade again.")
    if not (L.MIN_STAKE <= stake <= min(cfg["max_stake"], DT.MAX_STAKE)) or st["open_stake"] + stake > cfg["daily_loss"] + st["today_pnl"]:
        raise HTTPException(409, "This trade is outside your stake or daily loss limits.")
    if acct["type"] == "real" and d.get("confirm_real") is not True:
        raise HTTPException(422, "Confirm that this is a real-money trade you are placing yourself.")
    try:
        q = await asyncio.wait_for(_proposal(code, contract), timeout=30)
    except PlanError as e:
        raise HTTPException(422, str(e))
    except (asyncio.TimeoutError, OSError, websockets.WebSocketException):
        raise HTTPException(502, "Deriv did not answer just now. Please try again in a moment.")
    blocked = _edge_gate(cfg, key, acct["type"], q)
    if blocked:
        raise HTTPException(409, blocked)
    step = L.next_step(cfg, st["last_closed"])
    got = await _place(user, app_id, token, acct, cfg, key, contract, max_price, step, "manual", q)
    return JSONResponse({"ok": True, **got, "account_type": acct["type"]})


# ---------------------------------------------------------------- settings and sessions
async def me(request):
    user = auth.current_user(request)
    acc = access(user) if user else {"page": False, "auto": False}
    if not acc["page"]:
        return JSONResponse({"available": False})
    now = _now()
    with closing(db()) as con:
        cfg, row = _load_cfg(con, user["id"])
        trades = con.execute("SELECT id, account_type, symbol, strategy, contract_type, duration, stake, payout, step, source, status, error, "
                             "result_status, profit, created_at FROM opt_trades WHERE user_id=? AND status IN ('placed','failed') "
                             "ORDER BY id DESC LIMIT 15", (user["id"],)).fetchall()
        events = con.execute("SELECT kind, detail, created_at FROM opt_events WHERE user_id=? ORDER BY id DESC LIMIT 10", (user["id"],)).fetchall()
        val = con.execute("SELECT * FROM opt_validation ORDER BY strategy, symbol, duration").fetchall()
        st = _stats(con, user["id"], (row or {}).get("account_id") or "-", cfg, row, now)
        admin = None
        if user["tier"] == "admin":
            admin = {"mode": _meta(con, "mode", "testers"), "auto": _meta(con, "auto", "testers")}
            ids = _testers(con)
    if admin is not None:
        with closing(auth.db()) as ac:
            admin["testers"] = [r["email"] for r in (ac.execute("SELECT email FROM users WHERE id=?", (i,)).fetchone() for i in ids) if r]
    return JSONResponse({
        "available": True, "auto_available": acc["auto"], "settings": cfg, "defaults": L.DEFAULTS, "admin": admin,
        "session": {"on": bool(row and row["auto_on"]), "ends": (row or {}).get("session_ends"), "account_id": (row or {}).get("account_id"),
                    "account_type": (row or {}).get("account_type"), "note": (row or {}).get("last_note"), "pnl": st["session_pnl"]},
        "today": {k: st[k] for k in ("today_pnl", "today_n", "open_n", "open_stake", "consec_losses")},
        "strategies": [{"key": k, **v} for k, v in L.STRATEGIES.items()],
        "symbols": [{"key": k, "name": INSTRUMENTS[k][1]} for k in L.SYMBOLS], "durations": list(L.DURATIONS), "growth_rates": list(L.GROWTH_RATES),
        "caps": {"max_stake": DT.MAX_STAKE, "mg_mult": [L.MG_MIN_MULT, L.MG_MAX_MULT], "mg_steps": L.MG_MAX_STEPS, "min_n": L.MIN_N,
                 "cooldown_mins": L.COOLDOWN_SECS // 60, "chain_pct": L.CHAIN_MAX_PCT, "risk_pct": L.MAX_RISK_PCT},
        "validation": [dict(r) for r in val], "trades": [dict(r) for r in trades], "events": [dict(r) for r in events], "markup": DT.MARKUP})


async def save_settings(request):
    user = auth.require_user(request)
    _need_page(user)
    d = await auth._json(request)
    bal = None
    acct_id = str(d.get("account_id", ""))
    if acct_id and DT.ID_RE.match(acct_id):
        try:
            app_id, token = DT._session(user)
            bal = DT._pick_account(await DT._accounts(user["id"], app_id, token), acct_id).get("balance")
        except HTTPException:
            bal = None
    try:
        cfg = L.clean_settings(d, bal, DT.MAX_STAKE)
    except ValueError as e:
        raise HTTPException(422, str(e))
    with closing(db()) as con:
        _save_row(con, user["id"], cfg=json.dumps(cfg))
    return JSONResponse({"ok": True, "settings": cfg})


async def session(request):
    """Start or stop an AutoTrading session. A session lives no longer than the member's Deriv connection."""
    user = auth.require_user(request)
    _need_page(user)
    d = await auth._json(request)
    uid = user["id"]
    if d.get("action") == "stop":
        with closing(db()) as con:
            _save_row(con, uid, auto_on=0, last_note="Stopped by you.")
            _event(con, uid, "stop", "AutoTrading stopped by you.")
        return JSONResponse({"ok": True})
    if d.get("action") != "start":
        raise HTTPException(422, "Unknown action.")
    if not access(user)["auto"]:
        raise HTTPException(403, "AutoTrading for options is not available on your account yet.")
    from . import partner
    partner.require_tagged(user)
    account_id = str(d.get("account_id", ""))
    if not DT.ID_RE.match(account_id):
        raise HTTPException(422, "Choose a Deriv account.")
    app_id, token = DT._session(user)
    acct = DT._pick_account(await DT._accounts(uid, app_id, token), account_id)
    with closing(db()) as con:
        cfg, row = _load_cfg(con, uid)
    try:
        cfg = L.clean_settings(cfg, acct["balance"], DT.MAX_STAKE)      # settings are checked again against today's balance
    except ValueError as e:
        raise HTTPException(422, "Fix your settings first: " + str(e))
    if not cfg["symbols"]:
        raise HTTPException(422, "Choose at least one symbol.")
    if acct["type"] == "real":
        if d.get("confirm_real") is not True:
            raise HTTPException(422, "Tick the box that confirms you are letting AutoTrading place real-money trades.")
        if not L.STRATEGIES[cfg["strategy"]]["backtest"]:
            raise HTTPException(409, "This strategy runs on demo accounts only.")
    with closing(DT.db()) as dc:
        t = dc.execute("SELECT expires_at FROM deriv_tokens WHERE user_id=?", (uid,)).fetchone()
    ends = int(t["expires_at"]) if t else _now() + 3000
    with closing(db()) as con:
        _save_row(con, uid, auto_on=1, account_id=account_id, account_type=acct["type"], session_started=_now(), session_ends=ends,
                  reset_at=_now(), last_note="Session started. Waiting for a setup.")
        _event(con, uid, "start", f"AutoTrading started on a {acct['type']} account ({cfg['strategy']}).")
    return JSONResponse({"ok": True, "ends": ends})


# ---------------------------------------------------------------- trades list / check
async def my_trades(request):
    user = auth.require_user(request)
    _need_page(user)
    with closing(db()) as con:
        rows = con.execute("SELECT id, account_type, symbol, strategy, contract_type, duration, stake, payout, step, source, status, error, "
                           "result_status, profit, created_at FROM opt_trades WHERE user_id=? AND status IN ('placed','failed') "
                           "ORDER BY id DESC LIMIT 30", (user["id"],)).fetchall()
    return JSONResponse({"trades": [dict(r) for r in rows]})


async def check_trades(request):
    """Settle the member's open options contracts now (read-only on Deriv's side)."""
    user = auth.require_user(request)
    _need_page(user)
    app_id, token = DT._session(user)
    try:
        await _settle_open(user["id"], app_id, token)
    except PermissionError:
        DT._drop_token(user["id"])
        raise HTTPException(409, "Your Deriv connection has expired. Tap Connect Deriv, then check again.")
    return await my_trades(request)


# ---------------------------------------------------------------- AutoTrading loop
async def _auto_user(uid: int, cache: dict):
    lock = _locks.setdefault(uid, asyncio.Lock())
    if lock.locked():
        return
    async with lock:
        with closing(auth.db()) as ac:
            u = ac.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
        user = dict(u) if u and not u["blocked"] else None
        with closing(db()) as con:
            cfg, row = _load_cfg(con, uid)
        if not user or not row or not row["auto_on"]:
            return

        def stop(why, kind="stop"):
            with closing(db()) as c2:
                _save_row(c2, uid, auto_on=0, last_note=why)
                _event(c2, uid, kind, why)

        if not access(user)["auto"]:
            return stop("AutoTrading was switched off for your account.")
        token = DT._load_token(uid)
        if not token or _now() >= (row["session_ends"] or 0):
            return stop("Your Deriv connection ended, so AutoTrading stopped. Connect Deriv and start it again.")
        on_, app_id = DT._enabled()
        if not on_:
            return
        try:
            await _settle_open(uid, app_id, token)
        except PermissionError:
            DT._drop_token(uid)
            return stop("Your Deriv connection expired, so AutoTrading stopped.")
        now = _now()
        with closing(db()) as con:
            row = con.execute("SELECT * FROM opt_settings WHERE user_id=?", (uid,)).fetchone()
            row = dict(row)
            st = _stats(con, uid, row["account_id"], cfg, row, now)
        end = L.session_end_reason(cfg, st["session_pnl"])
        if end == "target":
            return stop(f"Session target reached ({st['session_pnl']:+.2f} USD). AutoTrading stopped.", "target")
        if end == "stop":
            return stop(f"Session stop loss reached ({st['session_pnl']:+.2f} USD). AutoTrading stopped.", "stop_loss")
        if st["consec_losses"] >= cfg["cooldown_losses"] and st["last_close"] and now - st["last_close"] >= L.COOLDOWN_SECS:
            with closing(db()) as con:
                _save_row(con, uid, reset_at=now)         # the pause is over: the losing run starts again from zero
            st["consec_losses"] = 0
        why = L.risk_block(cfg, st, now)
        if why:
            return _note(uid, why)
        accts = await DT._accounts(uid, app_id, token)
        acct = next((a for a in accts if a["id"] == row["account_id"]), None)
        if not acct or acct["status"] not in ("active", ""):
            return stop("The chosen Deriv account is not available, so AutoTrading stopped.")
        kind = L.STRATEGIES[cfg["strategy"]]["kind"]
        for key in cfg["symbols"]:
            if key in st["open_symbols"] or key not in L.SYMBOLS:
                continue
            code = INSTRUMENTS[key][0]
            if code not in cache:
                try:
                    cache[code] = await asyncio.wait_for(_closes(code, CANDLE_BARS), timeout=30)
                except Exception:
                    cache[code] = []
            sig = L.latest_signal(cfg["strategy"], cache[code])
            if not sig:
                continue
            step = L.next_step(cfg, st["last_closed"])
            stake = L.stake_for(cfg, acct["balance"], step)
            if acct["balance"] is not None and stake > acct["balance"]:
                return stop("The balance is too small for the next stake, so AutoTrading stopped.")
            if st["open_stake"] + stake > cfg["daily_loss"] + st["today_pnl"]:
                return _note(uid, "The next stake would take you past your daily loss limit, so it was not placed.")
            contract = _contract(cfg, sig, stake)
            try:
                q = await asyncio.wait_for(_proposal(code, contract), timeout=30)
            except PlanError as e:
                _note(uid, f"Deriv could not price {INSTRUMENTS[key][1]}: {e}")
                continue
            except Exception:
                continue
            blocked = _edge_gate(cfg, key, acct["type"], q)
            if blocked:
                _note(uid, f"Setup on {INSTRUMENTS[key][1]} skipped. {blocked}")
                continue
            max_price = round(max(q["ask"], stake) * DT.PRICE_SLACK + 0.01, 2)
            try:
                await _place(user, app_id, token, acct, cfg, key, contract, max_price, step, "auto", q)
            except HTTPException as e:
                _note(uid, f"Trade not placed: {e.detail}")
                if e.status_code == 409 and "expired" in str(e.detail):
                    return stop("Your Deriv connection expired, so AutoTrading stopped.")
                return
            with closing(db()) as con:
                _event(con, uid, "trade", f"{'Accumulator' if kind == 'accu' else 'Rise' if sig > 0 else 'Fall'} {INSTRUMENTS[key][1]} "
                                          f"{stake:g} USD (step {step + 1}).")
                _save_row(con, uid, last_note="Trade placed.")
            return                                      # one new trade per member per cycle


async def auto_loop():
    while True:
        try:
            if on():
                with closing(db()) as con:
                    ids = [r["user_id"] for r in con.execute("SELECT user_id FROM opt_settings WHERE auto_on=1").fetchall()]
                cache: dict = {}
                for uid in ids:
                    try:
                        await asyncio.wait_for(_auto_user(uid, cache), timeout=90)
                    except Exception as e:
                        print(f"[deriv-options] auto step failed: {type(e).__name__}", flush=True)
        except Exception as e:
            print(f"[deriv-options] loop: {type(e).__name__}", flush=True)
        await asyncio.sleep(LOOP_SECONDS)


def enabled() -> bool:
    return on() and os.environ.get("DERIV_OPTIONS_AUTO", "1") != "0"


# ---------------------------------------------------------------- admin: who may use it, and the backtest
async def admin_set(request):
    auth.require_admin(request)
    d = await auth._json(request)
    mode, auto = str(d.get("mode", "testers")), str(d.get("auto", "testers"))
    if mode not in ("off", "testers", "public") or auto not in ("off", "testers"):
        raise HTTPException(422, "mode: off, testers or public. auto: off or testers (there is no public AutoTrading).")
    raw = d.get("testers") or []
    if not isinstance(raw, list) or len(raw) > 50:
        raise HTTPException(422, "testers must be a list of up to 50 e-mail addresses")
    ids, missing = [], []
    with closing(auth.db()) as ac:
        for e in raw:
            r = ac.execute("SELECT id FROM users WHERE lower(email)=?", (str(e).strip().lower(),)).fetchone()
            (ids if r else missing).append(r["id"] if r else str(e))
    with closing(db()) as con:
        for k, v in (("mode", mode), ("auto", auto), ("testers", json.dumps(sorted(set(ids))))):
            if con.execute("SELECT 1 FROM opt_meta WHERE k=?", (k,)).fetchone():
                con.execute("UPDATE opt_meta SET v=? WHERE k=?", (v, k))
            else:
                con.execute("INSERT INTO opt_meta(k,v) VALUES(?,?)", (k, v))
        con.commit()
    return JSONResponse({"ok": True, "unknown_emails": missing})


async def admin_backtest(request):
    """Reads Deriv's recent M1 history and stores a measured hit rate per strategy, symbol and duration. Admin only."""
    auth.require_admin(request)
    d = await auth._json(request)
    syms = [s for s in (d.get("symbols") or list(L.SYMBOLS)) if s in L.SYMBOLS][:10]
    durs = [x for x in (d.get("durations") or [1, 3, 5]) if x in L.DURATIONS][:4]
    out = []
    for key in syms:
        try:
            closes = await asyncio.wait_for(_closes(INSTRUMENTS[key][0], BACKTEST_BARS), timeout=120)
        except Exception:
            out.append({"symbol": key, "error": "no history"})
            continue
        for s, meta in L.STRATEGIES.items():
            if not meta["backtest"]:
                continue
            for dur in durs:
                r = L.backtest(s, closes, dur, PAYOUT_RATIO_ASSUMED)
                with closing(db()) as con:
                    con.execute("DELETE FROM opt_validation WHERE strategy=? AND symbol=? AND duration=?", (s, key, dur))
                    con.execute("INSERT INTO opt_validation(strategy,symbol,duration,n,wins,hit,ratio,breakeven,lower,ev,edge,bars,updated_at) "
                                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                                (s, key, dur, r["n"], r["wins"], r["hit"], r["ratio"], r["breakeven"], r["lower"], r["ev"], int(r["edge"]),
                                 len(closes), _now()))
                    con.commit()
                out.append({"symbol": key, "strategy": s, "duration": dur, **r})
    return JSONResponse({"results": out, "bars": BACKTEST_BARS, "assumed_payout_ratio": PAYOUT_RATIO_ASSUMED})


routes = [
    Route("/auth/deriv/options/start", start, methods=["GET"]),
    Route("/api/options/me", me, methods=["GET"]),
    Route("/api/options/settings", save_settings, methods=["POST"]),
    Route("/api/options/session", session, methods=["POST"]),
    Route("/api/options/ideas", ideas, methods=["GET"]),
    Route("/api/options/quote", quote, methods=["POST"]),
    Route("/api/options/trade", trade, methods=["POST"]),
    Route("/api/options/trades", my_trades, methods=["GET"]),
    Route("/api/options/trades/check", check_trades, methods=["POST"]),
    Route("/api/options/admin", admin_set, methods=["POST"]),
    Route("/api/options/admin/backtest", admin_backtest, methods=["POST"]),
]
