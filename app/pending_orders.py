"""Pending orders for NO TRADE cards: "set it now, let MT5 fire it if the picture changes while I'm away".

Two halves live here.

1. build_pending() turns the numbers a NO TRADE analysis already computed (ceiling, floor, average, recent range, ATR) into up to six
   ready-to-place orders: buy limit, sell limit, buy stop, sell stop, buy stop limit and sell stop limit. No AI wording and no invented
   prices: every price is a level already drawn on the "What to wait for" chart, plus a buffer measured in ATR. Orders that do not make
   sense right now (wrong side of price, too little reward for the risk, spike risk ...) are returned in `unavailable` with the reason,
   so a beginner can see WHY an order type is not offered.

2. place() / the signed token. Each proposal carries a token signed with the server secret. The browser sends back only that token plus
   a lot size and an expiry; it can never change a price. place() then runs the same safety checks as "Open trade in MT5" (paid member,
   EA connected, the member's own daily-loss and position limits) plus pending-order checks, and queues ONE command for the member's own
   EA. The EA places a real broker-side pending order, so MT5 itself fires it when the price condition is met, even if the app, the
   phone or the EA is offline by then.

Lot size works like "Open trade in MT5": type one, or leave it empty for the symbol's smallest lot.
Nothing here ever trades by itself: the member taps "Set pending order" and confirms, and the EA must have AllowPending switched on.
The picture and the orders only describe what WOULD happen if price reaches a level. They never predict.
"""
import base64
import hashlib
import hmac
import json
import os
import time
from contextlib import closing

from starlette.exceptions import HTTPException
from starlette.responses import JSONResponse
from starlette.routing import Route

from .config import INSTRUMENTS, market, match_key
from . import watch_chart as WC

ORDER_TYPES = ("BUY_LIMIT", "SELL_LIMIT", "BUY_STOP", "SELL_STOP", "BUY_STOP_LIMIT", "SELL_STOP_LIMIT")
LABELS = {"BUY_LIMIT": "Buy limit", "SELL_LIMIT": "Sell limit", "BUY_STOP": "Buy stop", "SELL_STOP": "Sell stop",
          "BUY_STOP_LIMIT": "Buy stop limit", "SELL_STOP_LIMIT": "Sell stop limit"}

VIEW = "H1"                    # the same candles and levels as the default "What to wait for" picture
MIN_RR = 1.5                   # below this reward:risk an order is not offered (the AI card uses the same bar)
STOP_ATR = 1.0                 # stop loss sits this many ATR beyond the level the order is built on
TRIGGER_ATR = 0.25             # stop orders trigger a little beyond the line, so a one-tick poke does not fill them
PAD_ATR = 0.1                  # take profit stops this short of the opposite line, where price has to turn
MIN_AWAY_ATR = 0.2             # an order closer than this to the current price is too close to be useful
MAX_BREAKOUT_R = 3.0           # a breakout target is the range height, but never more than this many times the risk
MAX_LIMIT_R = 4.0              # a limit-order target is the opposite line, but never more than this many times the risk
HOURS = (4, 8, 12, 24)
DEFAULT_HOURS = 12
MAX_HOURS = 48
MAX_ORDERS = 10                # pending orders this app will keep open for one member at once
TOKEN_TTL = max(300, int(os.environ.get("DRIVERLINE_PENDING_TTL_MIN", "180")) * 60)

NOTE = ("A pending order waits at a price you choose. If price reaches it, MT5 opens the trade by itself, with the stop loss and take profit "
        "attached. If price never gets there, nothing happens. This is a plan made from today's analysis, not a prediction.")
WHY_NOT = {
    "spike_risk": "Sudden spikes can jump straight past a pending order's price and past its stop loss, so TradeLens does not offer pending "
                  "orders while spike risk is the reason for NO TRADE. Wait for calm candles and analyze again.",
}
GLOSSARY = [
    {"term": "Limit order", "text": "Waits for a BETTER price than now: a buy limit sits below price (buy a dip), a sell limit sits above price (sell a rise)."},
    {"term": "Stop order", "text": "Waits for price to MOVE to a level, then trades in that direction: a buy stop sits above price, a sell stop sits below. "
                                  "It fires the moment price touches the level, not when a candle closes."},
    {"term": "Stop limit order", "text": "Two steps. Price first touches the trigger level (the stop), and only then does a limit order appear at a better price "
                                         "just behind it. You get in on the retest, not on the first rush."},
    {"term": "Expiry", "text": "How long the order may wait. An order that has not filled by then is cancelled, so an old plan never fires days later."},
]


# ------------------------------------------------------------------------------------------------ numbers
def _r(x):
    x = float(x)
    return round(x, 2 if abs(x) >= 1000 else 3 if abs(x) >= 50 else 5)


def _atr(candles: list, n: int = 14) -> float:
    c = candles[-(n + 1):]
    trs = [max(b["high"] - b["low"], abs(b["high"] - a["close"]), abs(b["low"] - a["close"])) for a, b in zip(c[:-1], c[1:])]
    return sum(trs) / len(trs) if trs else 0.0


def _s(x) -> str:
    """A price as plain text for the EA (no exponent, no trailing zeros)."""
    return f"{float(x):.8f}".rstrip("0").rstrip(".")


# ------------------------------------------------------------------------------------------------ signed tokens
def _key() -> bytes:
    from . import auth
    return b"tradelens-pending|" + auth._secret()


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def make_token(p: dict) -> str:
    body = _b64(json.dumps(p, separators=(",", ":"), sort_keys=True).encode())
    return body + "." + _b64(hmac.new(_key(), body.encode(), hashlib.sha256).digest())


def read_token(tok, now=None) -> dict:
    """The proposal inside a token, or ValueError with a message the member can read."""
    try:
        body, sig = str(tok).split(".")
        good = hmac.compare_digest(_unb64(sig), hmac.new(_key(), body.encode(), hashlib.sha256).digest())
        p = json.loads(_unb64(body))
    except Exception:
        raise ValueError("This order could not be read. Analyze again and try once more.")
    if not good or not isinstance(p, dict):
        raise ValueError("This order could not be verified. Analyze again and try once more.")
    if (now or time.time()) - p.get("i", 0) > TOKEN_TTL:
        raise ValueError("This analysis is too old to place an order from. Analyze again to get fresh levels.")
    if p.get("t") not in ORDER_TYPES or not all(isinstance(p.get(k), (int, float)) for k in ("p", "s", "x", "r", "m")):
        raise ValueError("This order is incomplete. Analyze again and try once more.")
    return p


# ------------------------------------------------------------------------------------------------ the six orders
def _ctx(facts: dict, card: dict, candles: dict):
    series, f = candles.get(VIEW) or [], facts.get(VIEW)
    if not f or len(series) < max(WC.MIN_BARS, 16):
        return None
    bars = series[-WC.BARS:]
    price = float(f["price"])
    ceiling, floor, zone = WC._levels(f, price, bars)
    atr = _atr(series)
    if not atr > 0:
        return None
    return {"P": price, "atr": atr, "C": ceiling, "F": floor, "zone": zone, "ema": f.get("ema50"),
            "dir": WC._direction(f), "reason": card.get("no_trade_reason") or "other"}


def _one(kind: str, c: dict):
    """(order dict without text, None) when the order makes sense now, else (None, why not in plain words)."""
    P, atr, C, F, ema, reason = c["P"], c["atr"], c["C"], c["F"], c["ema"], c["reason"]
    buy = kind.startswith("BUY")
    stretched = reason == "overextended" and ema
    away = MIN_AWAY_ATR * atr
    risk_gap = STOP_ATR * atr

    if kind.endswith("_LIMIT") and not kind.endswith("STOP_LIMIT"):
        if stretched and ((buy and P < ema) or (not buy and P > ema)):
            return None, "Price is stretched " + ("below" if buy else "above") + " its average, so the only sensible pending order goes the other way (back toward the average)."
        cands = []
        if buy:
            if F and F < P - away:
                cands.append((F, "floor"))
            if ema and ema < P - away and (c["dir"] == "up" or stretched):
                cands.append((ema, "average"))
            if not cands:
                return None, "There is no support level below the current price to buy from."
            level, name = max(cands)
            far = C
            if not far or far <= level:
                return None, "There is no ceiling above to aim for."
        else:
            if C and C > P + away:
                cands.append((C, "ceiling"))
            if ema and ema > P + away and (c["dir"] == "down" or stretched):
                cands.append((ema, "average"))
            if not cands:
                return None, "There is no resistance level above the current price to sell from."
            level, name = min(cands)
            far = F
            if not far or far >= level:
                return None, "There is no floor below to aim for."
        sl = level - risk_gap if buy else level + risk_gap
        reach = min(abs(far - level) - PAD_ATR * atr, MAX_LIMIT_R * risk_gap)    # a far-away line is not a realistic target
        tp = level + reach if buy else level - reach
        if (buy and tp <= P + away) or (not buy and tp >= P - away):     # price is already at or past the target: nothing left to wait for
            return None, "Price is already at or past the realistic target for this order, so there is no room left to wait for."
        return {"price": level, "limit": None, "entry": level, "sl": sl, "tp": tp, "name": name}, None

    if stretched:
        return None, "Price is stretched far from its average. A breakout order here would chase it, so only a pullback order is offered."
    line, other = (C, F) if buy else (F, C)
    word = "ceiling" if buy else "floor"
    if not line or not other:
        return None, f"There is no {word} {'above' if buy else 'below'} the current price, or no range to measure a target from."
    if (buy and line <= P + away) or (not buy and line >= P - away):
        return None, f"The {word} is too close to the current price for a pending order."
    trigger = line + TRIGGER_ATR * atr if buy else line - TRIGGER_ATR * atr
    height = abs(C - F)
    if kind.endswith("STOP_LIMIT"):
        entry = line                                    # the retest of the line that was just broken
        risk = risk_gap
    else:
        entry = trigger
        risk = TRIGGER_ATR * atr + risk_gap
    sl = line - risk_gap if buy else line + risk_gap
    reward = min(height, MAX_BREAKOUT_R * risk)
    tp = entry + reward if buy else entry - reward
    return {"price": trigger, "limit": entry if kind.endswith("STOP_LIMIT") else None, "entry": entry, "sl": sl, "tp": tp,
            "name": word}, None


def _words(kind: str, o: dict, c: dict) -> dict:
    buy = kind.startswith("BUY")
    f = WC.fmt
    pr, sl, tp, ent = f(o["price"]), f(o["sl"]), f(o["tp"]), f(o["entry"])
    side = "BUY" if buy else "SELL"
    if kind.endswith("STOP_LIMIT"):
        what = (f"Waits for price to {'rise' if buy else 'fall'} to {pr} (the trigger, just {'above' if buy else 'below'} the {o['name']}). "
                f"Only then does it set a {side} limit at {ent}, so you get in if price comes back to test the {o['name']} it just broke.")
        miss = "If price breaks out and never comes back to test it, the order does not fill. That is the price of getting a better entry."
    elif kind.endswith("_LIMIT"):
        what = (f"Waits for price to {'fall' if buy else 'rise'} back to {ent} (the {o['name']}) and then {side}s there, "
                f"a better price than now. Stop loss {sl}, take profit {tp}.")
        miss = f"If price runs to the target without ever touching {ent}, the order is not filled and your EA cancels it."
    else:
        what = (f"Waits for price to {'rise through' if buy else 'fall through'} the {o['name']}. It {side}s the moment price touches {pr}. "
                f"Stop loss {sl}, take profit {tp}.")
        miss = "A stop order fires on touch, not on a candle close, so a quick poke through the line can fill it and then reverse."
    return {"what": what, "miss": miss}


def build_pending(facts: dict, card: dict, candles: dict, key: str):
    """The pending-order options for a NO TRADE card, or None (TRADE cards, or not enough candles to build levels)."""
    if not card or str(card.get("status")).upper() != "NO TRADE":
        return None
    c = _ctx(facts, card, candles)
    if not c:
        return None
    reason = c["reason"]
    out = {"tf": VIEW, "price": c["P"], "hours": list(HOURS), "default_hours": DEFAULT_HOURS, "note": NOTE, "glossary": GLOSSARY,
           "options": [], "unavailable": [], "warnings": [], "enabled": True}
    if reason in WHY_NOT:
        out["enabled"] = False
        out["why_not"] = WHY_NOT[reason]
        return out
    if reason == "mixed_timeframes":
        out["warnings"].append("The bigger and smaller timeframes disagree right now. Pending orders are a plan for AFTER they line up, so use a small lot size.")
    mk = market(key)
    if mk != "synthetic":
        out["warnings"].append("News and the weekend open can gap price past a pending order. It may then fill at a worse price than shown.")
    elif key.startswith(("boom", "crash", "jump")):
        out["warnings"].append("This index spikes suddenly. A spike can skip over a pending order's price and its stop loss, so the fill can be worse than shown.")
    now = int(time.time())
    for kind in ORDER_TYPES:
        o, why = _one(kind, c)
        if o:
            risk = abs(o["entry"] - o["sl"])
            rr = abs(o["tp"] - o["entry"]) / risk if risk else 0
            wrong = (o["tp"] <= o["entry"]) if kind.startswith("BUY") else (o["tp"] >= o["entry"])
            if wrong or not risk:
                o, why = None, "The levels do not leave a sensible target."
            elif rr < MIN_RR:
                o, why = None, f"Only {rr:.1f}x reward for the risk here. TradeLens offers an order only when the reward is at least {MIN_RR:g}x the risk."
        if not o:
            out["unavailable"].append({"type": kind, "label": LABELS[kind], "reason": why})
            continue
        o = {k: (_r(v) if isinstance(v, float) else v) for k, v in o.items()}
        risk = abs(o["entry"] - o["sl"])
        tok = make_token({"v": 1, "k": key, "t": kind, "p": o["price"], "l": o["limit"] or 0, "s": o["sl"], "x": o["tp"],
                          "r": _r(c["P"]), "m": _r(max(4 * c["atr"], 3 * risk)), "i": now})
        words = _words(kind, o, c)
        buy = kind.startswith("BUY")
        lv = [{"price": o["sl"], "label": "Stop loss", "kind": "sl"}, {"price": o["tp"], "label": "Take profit", "kind": "tp"}]
        lv.insert(0, {"price": o["price"], "label": "Trigger" if o["limit"] else "Order", "kind": "entry"})
        if o["limit"]:
            lv.insert(1, {"price": o["limit"], "label": "Limit", "kind": "entry"})
        out["options"].append({"type": kind, "label": LABELS[kind], "direction": "BUY" if buy else "SELL", "price": o["price"],
                               "limit": o["limit"], "entry": o["entry"], "sl": o["sl"], "tp": o["tp"], "risk": _r(risk),
                               "rr": round(abs(o["tp"] - o["entry"]) / risk, 2), "what": words["what"], "miss": words["miss"],
                               "levels": lv, "token": tok})
    if not out["options"]:
        out["note"] = ("No pending order has a good enough reward for its risk right now, which agrees with the NO TRADE result. "
                       "The list below says why each type is not offered.")
    return out


# ------------------------------------------------------------------------------------------------ placing one
def _risk_pct(spec: dict, lots: float, dist: float, balance: float):
    try:
        unit = (spec["tick_value"] / spec["tick_size"]) if spec.get("tick_size", 0) > 0 and spec.get("tick_value", 0) > 0 \
            else spec.get("contract_size", 0)
        l = lots if lots > 0 else spec.get("vol_min", 0)
        return (l * dist * unit / balance * 100) if unit > 0 and l > 0 and balance > 0 else None
    except (TypeError, KeyError, ZeroDivisionError):
        return None


async def place(request):
    """The member tapped "Set pending order". Queues ONE pending-order request for THEIR OWN EA."""
    from . import auth, credits, ea, journal
    from .signal_feed import paid
    user = auth.require_user(request)
    if not (paid(user) or credits.entitled(user, "mt5")):
        raise HTTPException(403, "Pending orders from the app are for paid members with MT5 connected")
    d = await auth._json(request)
    try:
        p = read_token(d.get("token"))
    except ValueError as e:
        raise HTTPException(409, str(e))
    raw = d.get("lots")
    try:
        lots = 0.0 if raw in (None, "", 0, "0") else float(raw)
        hours = DEFAULT_HOURS if d.get("hours") in (None, "") else float(d.get("hours"))
    except (TypeError, ValueError):
        raise HTTPException(422, "Enter the lot size and the hours as numbers")
    if not (0 <= lots <= 1000) or lots != lots:
        raise HTTPException(422, "That lot size is not valid")
    if not (1 <= hours <= MAX_HOURS):
        raise HTTPException(422, f"Choose an expiry between 1 and {MAX_HOURS} hours")
    kind, key = p["t"], p.get("k")
    if key not in INSTRUMENTS:
        raise HTTPException(409, "This order is for an instrument that is not available any more. Analyze again.")
    tokid = hashlib.sha256(str(d.get("token")).encode()).hexdigest()[:8]
    cmt = "TLP-" + tokid
    now = int(time.time())
    entry = p["l"] if p.get("l") else p["p"]
    with closing(ea.db()) as ec:
        acct, s = ea.load(ec, user["id"], d.get("account"))          # which MT5 account: required when the member has more than one
        if not (acct["last_seen"] and now - acct["last_seen"] < 120 and s):
            raise HTTPException(409, "Your MT5 is not connected right now. Pending orders are placed through your own MT5 connection.")
        state = json.loads(s["state"])
        acc, orders = state.get("account") or {}, [o for o in (state.get("orders") or []) if isinstance(o, dict)]
        if not acc.get("pending_allowed"):
            raise HTTPException(409, "Pending orders are not enabled on your account yet (AllowPending is off). Ask TradeLens support to enable it.")
        rules = ea.get_rules(ec, acct["id"])
        if ea.risk_check(state, rules)["status"] == "STOP":
            raise HTTPException(409, f"You reached your own daily loss limit of {rules['daily_loss_pct']:g}%. New orders are paused for today.")
        if len(state.get("positions") or []) + len(orders) >= rules["max_positions"]:
            raise HTTPException(409, f"You already have {len(state.get('positions') or [])} open positions and {len(orders)} pending orders, "
                                     f"which reaches your own maximum of {rules['max_positions']}.")
        if len(orders) >= MAX_ORDERS:
            raise HTTPException(409, f"You already have {len(orders)} pending orders. Cancel one first.")
        if any(str(o.get("comment")) == cmt for o in orders):
            raise HTTPException(409, "This pending order is already waiting in your MT5.")
        name = INSTRUMENTS[key][1]
        want = journal._norm(name)
        specs = [x for x in json.loads(s["specs"] or "[]") if isinstance(x, dict)]
        spec = (next((x for x in specs if journal._norm(x.get("symbol", "")) == want), None)
                or next((x for x in specs if match_key(x.get("symbol", "")) == key), None))
        if not spec:
            raise HTTPException(409, f"{name} was not found in your MT5 Market Watch. Add it there first.")
        symbol = spec["symbol"]
        pct = _risk_pct(spec, lots, abs(entry - p["s"]), float(acc.get("balance") or 0))
        # Like "Open trade in MT5": an empty lot size means the symbol's smallest lot, and the member is WARNED (in the app) when the
        # risk is above their own rule, never blocked. The risk is returned so the app can say so.
        for r in ec.execute("SELECT payload FROM ea_commands WHERE account_id=? AND kind='pending_order' AND created_at>? "
                            "AND status IN ('pending','sent','done')", (acct["id"], now - 90)).fetchall():
            if r["payload"] and json.loads(r["payload"]).get("comment") == cmt:
                raise HTTPException(409, "You just sent this order. Check the list of pending orders before sending it again.")
        cid = ea.queue_open(ec, acct, {
            "symbol": symbol, "ptype": kind, "lots": lots, "price": p["p"], "limit": p.get("l") or 0, "sl": p["s"], "tp": p["x"],
            "exp_min": int(hours * 60), "ref": p["r"], "maxdev": p["m"], "comment": cmt}, kind="pending_order")
    return JSONResponse({"command_id": cid, "symbol": symbol, "comment": cmt, "account": acct["id"], "account_label": acct["label"],
                         "risk_pct": round(pct, 2) if pct is not None else None,
                         "over_rule": bool(pct is not None and pct > rules["max_risk_pct"] + 1e-9)})


routes = [Route("/api/pending/place", place, methods=["POST"])]
