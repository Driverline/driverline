"""Data validation for stored candles. A dataset that fails is marked invalid and cannot be used for research runs."""
import hashlib
import math


def dataset_hash(rows: list) -> str:
    h = hashlib.sha256()
    for r in rows:
        h.update(f"{r['epoch']},{r['open']:.10g},{r['high']:.10g},{r['low']:.10g},{r['close']:.10g}\n".encode())
    return h.hexdigest()


def validate_candles(rows: list, tf: int) -> dict:
    """Returns a report with a status of valid, warn or invalid, and the reasons.
    invalid: unsorted or duplicate times, times not on the timeframe grid, inconsistent or non-positive prices.
    warn: more than 0.2% of bars missing, extreme bars, or fewer than 2000 bars."""
    n = len(rows)
    rep = {"rows": n, "tf": tf, "first": rows[0]["epoch"] if n else None, "last": rows[-1]["epoch"] if n else None,
           "duplicates": 0, "unsorted": 0, "misaligned": 0, "missing_bars": 0, "largest_gap_bars": 0,
           "gap_share": 0.0, "ohlc_inconsistent": 0, "bad_prices": 0, "flat_bars": 0, "extreme_bars": 0,
           "tick_stats_share": 0.0}
    why_invalid, why_warn = [], []
    if n == 0:
        return {**rep, "status": "invalid", "reasons": ["no rows"]}
    prev, ranges, tick_rows = None, [], 0
    for r in rows:
        e, o, h, l, c = r["epoch"], r["open"], r["high"], r["low"], r["close"]
        if prev is not None:
            if e == prev:
                rep["duplicates"] += 1
            elif e < prev:
                rep["unsorted"] += 1
            else:
                gap = (e - prev) // tf - 1
                if gap > 0:
                    rep["missing_bars"] += gap
                    rep["largest_gap_bars"] = max(rep["largest_gap_bars"], gap)
        prev = e
        if e % tf:
            rep["misaligned"] += 1
        if not all(isinstance(x, (int, float)) and math.isfinite(x) and x > 0 for x in (o, h, l, c)):
            rep["bad_prices"] += 1
            continue
        if h < max(o, c) - 1e-12 or l > min(o, c) + 1e-12 or h < l:
            rep["ohlc_inconsistent"] += 1
        if h == l:
            rep["flat_bars"] += 1
        ranges.append(h - l)
        if r.get("n_ticks"):
            tick_rows += 1
    rep["gap_share"] = rep["missing_bars"] / (n + rep["missing_bars"])
    rep["tick_stats_share"] = tick_rows / n
    pos = sorted(x for x in ranges if x > 0)
    if pos:
        med = pos[len(pos) // 2]
        rep["extreme_bars"] = sum(1 for x in ranges if x > 25 * med)
    for k, text in (("duplicates", "duplicate times"), ("unsorted", "times out of order"),
                    ("misaligned", "times not on the timeframe grid"), ("ohlc_inconsistent", "inconsistent high/low"),
                    ("bad_prices", "missing or non-positive prices")):
        if rep[k]:
            why_invalid.append(f"{rep[k]} {text}")
    if rep["gap_share"] > 0.002:
        why_warn.append(f"{rep['gap_share'] * 100:.2f}% of bars are missing")
    if rep["extreme_bars"]:
        why_warn.append(f"{rep['extreme_bars']} extreme bars (range above 25x the median)")
    if n < 2000:
        why_warn.append(f"only {n} bars")
    status = "invalid" if why_invalid else ("warn" if why_warn else "valid")
    return {**rep, "status": status, "reasons": why_invalid + why_warn}
