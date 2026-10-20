"""A picture for NO TRADE cards: what the gray "Next:" line is saying, drawn.

Built only from numbers the analysis already computed (swing levels, ranges, moving averages, trend direction) and the candles it
downloaded. No AI wording and no invented prices: every level on the picture is one of those numbers, and the "If ... then ..."
lines are filled in from them. The picture only describes what WOULD change the situation. It never predicts.

build_watch() returns None for TRADE cards, so BUY and SELL cards stay exactly as they are.
"""
import statistics

BARS = 80
MIN_BARS = 30
VIEWS = ("H1", "M15")          # hourly first, 15-minute as an alternative view

TITLES = {
    "consolidation": "Price is stuck in a box",
    "mixed_timeframes": "The big picture and the small picture disagree",
    "unclear_structure": "No clear pattern yet",
    "overextended": "Price has moved far from its average",
    "poor_risk_reward": "There is too little room for a good trade",
    "spike_risk": "Sudden spikes can jump past a stop",
    "other": "Not a clear trade yet",
}
GLOSSARY = [
    {"term": "Ceiling", "text": "A price area where rises have recently stopped. Price has struggled to get above it."},
    {"term": "Floor", "text": "A price area where falls have recently stopped. Price has held above it so far."},
    {"term": "Closes above / below", "text": "A candle closes when its time period ends. Ignore brief pokes through a line; wait for the candle to finish beyond it."},
    {"term": "Average (EMA50)", "text": "A smooth line of recent prices. When price is far from it, price is stretched and often rests or pulls back."},
    {"term": "Wait zone", "text": "The box price has been bouncing inside. Inside it, there is no edge, so staying out is the plan."},
]
NOTE = "This shows what would change the picture. It is not a prediction, and nothing here is a signal until a BUY or SELL card appears."


def fmt(x) -> str:
    x = float(x)
    d = 2 if abs(x) >= 1000 else 3 if abs(x) >= 50 else 4 if abs(x) >= 1 else 5
    return f"{x:,.{d}f}"


def _direction(f: dict) -> str:
    ups = (f.get("ema_stack") == "bullish") + str(f.get("structure", "")).startswith("bullish")
    downs = (f.get("ema_stack") == "bearish") + str(f.get("structure", "")).startswith("bearish")
    return "up" if ups > downs else "down" if downs > ups else "mixed"


def _spikes(candles: list) -> list:
    rng = [c["high"] - c["low"] for c in candles]
    med = statistics.median(rng) if rng else 0
    return [i for i, r in enumerate(rng) if med > 0 and r > 3.5 * med][-6:]


def _levels(f: dict, price: float, candles: list):
    """(ceiling, floor, zone): the nearest level above price, the nearest below, and the box when price is ranging."""
    rng = f.get("range") or {}
    zone = None
    ups, downs = [], []
    if rng.get("is_consolidating") and rng.get("high") and rng.get("low") and rng["high"] > rng["low"]:
        zone = {"low": rng["low"], "high": rng["high"], "label": "Wait zone"}
        ups.append(rng["high"])
        downs.append(rng["low"])
    for lv in (f.get("last_swing_high"), rng.get("high")):
        if lv and lv > price:
            ups.append(lv)
    for lv in (f.get("last_swing_low"), rng.get("low")):
        if lv and lv < price:
            downs.append(lv)
    recent = candles[-40:]
    if not ups or min(ups) <= price:
        hi = max(c["high"] for c in recent)
        ups = [hi] if hi > price else []
    if not downs or max(downs) >= price:
        lo = min(c["low"] for c in recent)
        downs = [lo] if lo < price else []
    if zone:
        return zone["high"], zone["low"], zone
    return (min(ups) if ups else None), (max(downs) if downs else None), None


def _scenarios(reason: str, tf_name: str, price: float, ceiling, floor, f: dict) -> list:
    out = []
    buy = ({"kind": "buy", "text": f"If a candle closes above {fmt(ceiling)}, look for a BUY setup."} if ceiling else None)
    sell = ({"kind": "sell", "text": f"If a candle closes below {fmt(floor)}, look for a SELL setup."} if floor else None)
    ema = f.get("ema50")
    if reason == "mixed_timeframes":
        out.append({"kind": "wait", "text": "Wait until the bigger timeframes (D1, H4) and the smaller ones (H1, M15) point the same way."})
    elif reason == "overextended" and ema:
        if price > ema:
            out.append({"kind": "buy", "text": f"If price pulls back toward the average (about {fmt(ema)}) and holds, look for a BUY setup."})
        else:
            out.append({"kind": "sell", "text": f"If price bounces back up toward the average (about {fmt(ema)}) and fails, look for a SELL setup."})
        out.append({"kind": "wait", "text": "Chasing price now would mean a far stop. Waiting costs nothing."})
        return out
    elif reason == "poor_risk_reward" and ceiling and floor:
        near_top = (ceiling - price) < (price - floor)
        out.append({"kind": "wait", "text": ("Price is close to the ceiling, so a BUY here has little room to run before it."
                                             if near_top else
                                             "Price is close to the floor, so a SELL here has little room to run before it.")})
    elif reason == "spike_risk":
        out.append({"kind": "wait", "text": "Wait for several calm candles without sudden spikes, then analyze again. Spikes can skip over a stop."})
        return out
    elif reason == "consolidation":
        out.append({"kind": "wait", "text": "Inside the box there is no edge. Wait for price to leave it."})
    for s in (buy, sell):
        if s:
            out.append(s)
    out.append({"kind": "wait", "text": "Until then, staying out is the plan."})
    return out


def _view(tf: str, facts: dict, candles: dict, reason: str):
    series, f = candles.get(tf) or [], facts.get(tf)
    if not f or len(series) < MIN_BARS:
        return None
    bars = series[-BARS:]
    price = float(f["price"])
    ceiling, floor, zone = _levels(f, price, bars)
    levels, caption = [], None
    if reason == "overextended" and f.get("ema50"):          # only the average matters here: keep the picture simple
        levels.append({"price": f["ema50"], "label": "Average", "kind": "ema"})
        caption = f"The purple line is the average price. Price is far {'above' if price > f['ema50'] else 'below'} it."
    elif reason == "spike_risk":                              # the spikes are the message: no other lines
        caption = "Circled candles are sudden spikes. A stop can be skipped over when this happens."
    else:
        if ceiling:
            levels.append({"price": ceiling, "label": "Ceiling", "kind": "ceiling"})
        if floor:
            levels.append({"price": floor, "label": "Floor", "kind": "floor"})
    return {"tf": tf, "price": price, "caption": caption,
            "candles": [[c["epoch"], c["open"], c["high"], c["low"], c["close"]] for c in bars],
            "levels": levels, "zone": zone, "spikes": _spikes(bars) if reason == "spike_risk" else [],
            "scenarios": _scenarios(reason, tf, price, ceiling, floor, f)}


def build_watch(facts: dict, card: dict, candles: dict):
    """The picture for a NO TRADE card, or None (TRADE cards, or not enough candles)."""
    if not card or str(card.get("status")).upper() != "NO TRADE":
        return None
    reason = card.get("no_trade_reason") if card.get("no_trade_reason") in TITLES else "other"
    views = {tf: v for tf in VIEWS if (v := _view(tf, facts, candles, reason))}
    if not views:
        return None
    chips = [{"tf": tf, "dir": _direction(facts[tf])} for tf in ("D1", "H4", "H1", "M15") if tf in facts] \
        if reason == "mixed_timeframes" else []
    return {"reason": reason, "title": TITLES[reason], "default": "H1" if "H1" in views else next(iter(views)),
            "views": views, "chips": chips, "glossary": GLOSSARY, "note": NOTE}
