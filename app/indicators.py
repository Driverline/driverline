"""Pure-Python indicator + structure engine. The LLM only sees these computed facts."""


def ema(values: list[float], period: int) -> list[float]:
    if not values:
        return []
    k = 2 / (period + 1)
    out = [values[0]]
    for v in values[1:]:
        out.append(v * k + out[-1] * (1 - k))
    return out


def macd(closes: list[float], fast=12, slow=26, signal=9):
    ef, es = ema(closes, fast), ema(closes, slow)
    line = [a - b for a, b in zip(ef, es)]
    sig = ema(line, signal)
    hist = [a - b for a, b in zip(line, sig)]
    return line, sig, hist


def rsi(closes: list[float], period: int = 14) -> list[float]:
    if len(closes) <= period:
        return []
    gains, losses = [], []
    for i in range(1, len(closes)):
        d = closes[i] - closes[i - 1]
        gains.append(max(d, 0))
        losses.append(max(-d, 0))
    ag = sum(gains[:period]) / period
    al = sum(losses[:period]) / period
    out = []
    for i in range(period, len(gains) + 1):
        if i > period:
            ag = (ag * (period - 1) + gains[i - 1]) / period
            al = (al * (period - 1) + losses[i - 1]) / period
        out.append(100.0 if al == 0 else 100 - 100 / (1 + ag / al))
    return out


def swing_points(candles: list[dict], left: int = 3, right: int = 3):
    highs, lows = [], []
    for i in range(left, len(candles) - right):
        h, l = candles[i]["high"], candles[i]["low"]
        if all(h > candles[j]["high"] for j in range(i - left, i + right + 1) if j != i):
            highs.append((i, h))
        if all(l < candles[j]["low"] for j in range(i - left, i + right + 1) if j != i):
            lows.append((i, l))
    return highs, lows


def classify_structure(highs, lows) -> str:
    if len(highs) < 2 or len(lows) < 2:
        return "unclear"
    hh = highs[-1][1] > highs[-2][1]
    hl = lows[-1][1] > lows[-2][1]
    if hh and hl:
        return "bullish (higher highs, higher lows)"
    if not hh and not hl:
        return "bearish (lower highs, lower lows)"
    return "mixed / transitioning"


def detect_range(candles: list[dict], lookback: int = 30, er_max: float = 0.3):
    """Consolidation = choppy path (low efficiency ratio) AND price away from the range edges."""
    recent = candles[-lookback:]
    if len(recent) < lookback:
        return None
    hi = max(c["high"] for c in recent)
    lo = min(c["low"] for c in recent)
    trs = [max(c["high"] - c["low"], abs(c["high"] - p["close"]), abs(c["low"] - p["close"]))
           for p, c in zip(recent[:-1], recent[1:])]
    atr = sum(trs) / len(trs) if trs else 0
    closes = [c["close"] for c in recent]
    path = sum(abs(y - x) for x, y in zip(closes, closes[1:]))
    er = abs(closes[-1] - closes[0]) / path if path else 0.0
    pos = (closes[-1] - lo) / (hi - lo) if hi > lo else 0.5
    return {
        "high": round(hi, 4), "low": round(lo, 4), "atr": round(atr, 4),
        "width_atr": round((hi - lo) / atr, 2) if atr else None,
        "efficiency": round(er, 2),
        "is_consolidating": bool(er <= er_max and 0.15 <= pos <= 0.85),
    }


def analyze_timeframe(candles: list[dict]) -> dict:
    closes = [c["close"] for c in candles]
    price = closes[-1]
    e20, e50, e200 = ema(closes, 20)[-1], ema(closes, 50)[-1], ema(closes, 200)[-1]
    _, _, hist = macd(closes)
    r = rsi(closes)
    highs, lows = swing_points(candles)
    last_high = highs[-1][1] if highs else None
    last_low = lows[-1][1] if lows else None
    return {
        "price": round(price, 4),
        "ema20": round(e20, 4), "ema50": round(e50, 4), "ema200": round(e200, 4),
        "price_vs_ema50": "above" if price > e50 else "below",
        "ema_stack": ("bullish" if e20 > e50 > e200 else
                      "bearish" if e20 < e50 < e200 else "mixed"),
        "macd_hist": round(hist[-1], 4), "macd_hist_prev": round(hist[-2], 4),
        "rsi": round(r[-1], 1) if r else None,
        "structure": classify_structure(highs, lows),
        "last_swing_high": round(last_high, 4) if last_high else None,
        "last_swing_low": round(last_low, 4) if last_low else None,
        "dist_to_swing_high": round(last_high - price, 4) if last_high else None,
        "dist_to_swing_low": round(price - last_low, 4) if last_low else None,
        "range": detect_range(candles),
    }


def analyze_all(candles_by_tf: dict[str, list[dict]]) -> dict:
    return {tf: analyze_timeframe(c) for tf, c in candles_by_tf.items() if len(c) >= 60}
