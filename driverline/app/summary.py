"""Rule-based summary of the computed facts. Works without Claude; not a trade signal."""

_W = {"D1": 3, "H4": 2, "H1": 1}


def _line(f: dict) -> str:
    parts = [f"{f['structure']}", f"price {f['price_vs_ema50']} EMA50, EMAs {f['ema_stack']}"]
    rsi = f.get("rsi")
    if rsi is not None:
        parts.append(f"RSI {rsi} ({'oversold' if rsi < 30 else 'overbought' if rsi > 70 else 'neutral'})")
    parts.append("MACD histogram " + ("rising" if f["macd_hist"] > f["macd_hist_prev"] else "falling"))
    r = f.get("range")
    if r and r.get("is_consolidating"):
        parts.append(f"ranging {r['low']} to {r['high']}")
    return "; ".join(parts)


def summarize_facts(facts: dict) -> dict:
    score = 0
    for tf, w in _W.items():
        f = facts.get(tf)
        if not f:
            continue
        score += w * ((f["ema_stack"] == "bullish") - (f["ema_stack"] == "bearish"))
        score += w * (f["structure"].startswith("bullish") - f["structure"].startswith("bearish"))
    bias = "Bullish" if score >= 4 else "Bearish" if score <= -4 else "Mixed / unclear"
    levels = {tf: {"swing_high": facts[tf]["last_swing_high"], "swing_low": facts[tf]["last_swing_low"],
                   "price": facts[tf]["price"]} for tf in ("H4", "H1", "M15") if tf in facts}
    return {"bias": bias, "timeframes": {tf: _line(f) for tf, f in facts.items()}, "levels": levels}
