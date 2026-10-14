"""Reproducible research core: outcome definitions, chronological splits, baselines, statistics and calibration.

Outcome definition (fixed before any testing): the signal is read at a bar's close, the entry is the NEXT bar's open,
the stop is stop_atr x ATR14 away, the target is rr x the stop distance, and the trade ends at the first touch
(stop first if both are touched in one bar) or at the horizon (closed at that bar's close). Costs are a flat fraction
of ATR. Trades on a symbol never overlap. A trade must finish inside its own segment, so no outcome ever peeks across
a split boundary.

Splits are chronological: the first 60% of bars is development, the next 20% validation, the last 20% is the untouched
test. run_baselines() evaluates development and validation only; the test segment is never touched here."""
import hashlib
import json
import math
import random

from . import features as FT

SPECS = ((1.5, 1.0), (1.5, 1.5), (1.5, 2.0))     # (stop in ATR, target in R)
HORIZON = 24                                     # bars (M5: two hours)
COST_ATR = 0.05
SHARES = (0.6, 0.2, 0.2)
BASELINES = ("always_long", "always_short", "random", "momentum_1", "reversal_1")
BASELINE_DEFINITIONS = {
    "always_long": "Buy at every opportunity (measures drift and costs)",
    "always_short": "Sell at every opportunity (measures drift and costs)",
    "random": "Random direction at every opportunity (seeded)",
    "momentum_1": "Trade in the direction of the last closed bar",
    "reversal_1": "Trade against the direction of the last closed bar",
}


def config() -> dict:
    return {"specs": SPECS, "horizon": HORIZON, "cost_atr": COST_ATR, "shares": SHARES, "baselines": BASELINES}


def split_bounds(n: int, shares=SHARES) -> dict:
    a = int(n * shares[0])
    b = int(n * (shares[0] + shares[1]))
    return {"dev": (0, a), "val": (a, b), "test": (b, n)}


def baseline_signals(name: str, bars: list, lo: int, hi: int, seed: int) -> list:
    rng = random.Random(seed)
    out = []
    for i in range(max(lo, 1), hi):
        if name == "always_long":
            out.append((i, 1))
        elif name == "always_short":
            out.append((i, -1))
        elif name == "random":
            out.append((i, rng.choice((1, -1))))
        else:
            d = bars[i]["close"] - bars[i - 1]["close"]
            if d != 0:
                s = 1 if d > 0 else -1
                out.append((i, s if name == "momentum_1" else -s))
    return out


def simulate(bars: list, atr: list, signals: list, stop_atr: float, rr: float, horizon: int, cost_atr: float,
             lo: int, hi: int) -> list:
    cost = cost_atr / stop_atr
    out, free_from = [], lo
    for i, d in signals:
        if i + 1 < free_from or i + 1 >= hi or i < lo:
            continue
        a = atr[i]
        if a is None or a <= 0:
            continue
        entry, sd = bars[i + 1]["open"], stop_atr * a
        stop, target = entry - d * sd, entry + d * rr * sd
        end, res = min(hi - 1, i + horizon), None
        for j in range(i + 1, end + 1):
            b = bars[j]
            hit_stop = (b["low"] <= stop) if d > 0 else (b["high"] >= stop)
            hit_tgt = (b["high"] >= target) if d > 0 else (b["low"] <= target)
            if hit_stop:
                res = (-1.0 - cost, False, j)
                break
            if hit_tgt:
                res = (rr - cost, True, j)
                break
        if res is None:
            res = (d * (bars[end]["close"] - entry) / sd - cost, False, end)
        out.append({"i": i, "d": d, "r": res[0], "hit": res[1], "exit": res[2]})
        free_from = res[2] + 1
    return out


# ---------------------------------------------------------------- statistics
def block_bootstrap_ci(rs: list, seed: int, reps: int = 1000):
    """95% interval for the mean, resampling blocks so that trade-to-trade dependence is kept. None if under 30 trades."""
    n = len(rs)
    if n < 30:
        return None
    b = max(2, int(round(n ** (1 / 3))))
    rng, means = random.Random(seed), []
    nblocks = -(-n // b)
    for _ in range(reps):
        tot = cnt = 0
        for _ in range(nblocks):
            s = rng.randrange(0, n - b + 1)
            tot += sum(rs[s:s + b])
            cnt += b
        means.append(tot / cnt)
    means.sort()
    return [means[int(0.025 * reps)], means[int(0.975 * reps) - 1]]


def summarize(trades: list, rr: float, cost_atr_r: float, seed: int) -> dict:
    n = len(trades)
    if n == 0:
        return {"n": 0}
    rs = [t["r"] for t in trades]
    mean = sum(rs) / n
    sd = math.sqrt(sum((x - mean) ** 2 for x in rs) / (n - 1)) if n > 1 else 0.0
    eq = pk = dd = 0.0
    streak = best = 0
    for x in rs:
        eq += x
        pk = max(pk, eq)
        dd = max(dd, pk - eq)
        streak = streak + 1 if x < 0 else 0
        best = max(best, streak)
    return {"n": n, "avg_r": mean, "sd_r": sd, "ci95": block_bootstrap_ci(rs, seed), "hit": sum(1 for t in trades if t["hit"]) / n,
            "breakeven_hit": (1 + cost_atr_r) / (1 + rr), "total_r": sum(rs), "max_drawdown_r": dd, "longest_losing_streak": best}


def percentile_cuts(values: list):
    v = sorted(values)
    return (v[len(v) // 3], v[2 * len(v) // 3]) if len(v) >= 30 else None


def regime_breakdown(trades: list, vol_ratio: list, cuts) -> dict:
    """Average result by volatility regime (low, mid, high; cut points come from the development segment) and by thirds
    of the segment's time."""
    out = {"volatility": None, "time_thirds": []}
    if cuts and trades:
        groups = {"low": [], "mid": [], "high": []}
        for t in trades:
            v = vol_ratio[t["i"]]
            groups["low" if v < cuts[0] else "mid" if v < cuts[1] else "high"].append(t["r"])
        out["volatility"] = {k: {"n": len(v), "avg_r": (sum(v) / len(v)) if v else None} for k, v in groups.items()}
    k = len(trades) // 3
    for part in (trades[:k], trades[k:2 * k], trades[2 * k:]) if k else ():
        out["time_thirds"].append({"n": len(part), "avg_r": sum(t["r"] for t in part) / len(part)})
    return out


def calibration(probs: list, outcomes: list, bins: int = 10) -> dict:
    """Brier score, log loss and a reliability table. probs are predicted chances of the target being hit first."""
    n = len(probs)
    if n == 0:
        return {"n": 0}
    eps = 1e-9
    brier = sum((p - y) ** 2 for p, y in zip(probs, outcomes)) / n
    ll = -sum(y * math.log(max(p, eps)) + (1 - y) * math.log(max(1 - p, eps)) for p, y in zip(probs, outcomes)) / n
    table = []
    for b in range(bins):
        lo, hi = b / bins, (b + 1) / bins
        idx = [k for k, p in enumerate(probs) if lo <= p < hi or (b == bins - 1 and p == 1.0)]
        if idx:
            table.append({"from": lo, "to": hi, "n": len(idx), "mean_predicted": sum(probs[k] for k in idx) / len(idx),
                          "observed": sum(outcomes[k] for k in idx) / len(idx)})
    return {"n": n, "brier": brier, "log_loss": ll, "reliability": table}


def seed_for(*parts) -> int:
    return int(hashlib.sha256("|".join(str(p) for p in parts).encode()).hexdigest()[:12], 16)


# ---------------------------------------------------------------- the baseline study
def run_baselines(bars: list, key: str, logger=None) -> dict:
    """Every baseline x spec on the development and validation segments. `logger(label, segment, params, metrics)` is
    called for every one of them. Returns the full result and a short list of anything that stands out."""
    n = len(bars)
    atr, vr = FT.atr_series(bars)
    bounds = split_bounds(n)
    result = {"bars": n, "bounds": {k: list(v) for k, v in bounds.items() if k != "test"},
              "test_untouched": True, "config": config(), "rows": [], "notable": []}
    for stop_atr, rr in SPECS:
        cost_r = COST_ATR / stop_atr
        for base in BASELINES:
            row = {"baseline": base, "stop_atr": stop_atr, "rr": rr}
            dev_cuts = None
            for seg in ("dev", "val"):
                lo, hi = bounds[seg]
                seed = seed_for(key, base, stop_atr, rr, seg)
                trades = simulate(bars, atr, baseline_signals(base, bars, lo, hi, seed), stop_atr, rr, HORIZON, COST_ATR, lo, hi)
                m = summarize(trades, rr, cost_r, seed)
                if seg == "dev":
                    dev_cuts = percentile_cuts([vr[t["i"]] for t in trades])
                m["regimes"] = regime_breakdown(trades, vr, dev_cuts)
                if seg == "val" and trades:
                    p = row["dev"].get("hit", 0.0) if row.get("dev", {}).get("n") else 0.0
                    m["calibration_base_rate"] = calibration([p] * len(trades), [1 if t["hit"] else 0 for t in trades])
                row[seg] = m
                if logger:
                    logger(f"{base}|stop{stop_atr}|rr{rr}", seg, {"baseline": base, "stop_atr": stop_atr, "rr": rr,
                                                                  "horizon": HORIZON, "cost_atr": COST_ATR}, m)
            result["rows"].append(row)
            d, v = row["dev"], row["val"]
            if d.get("ci95") and v.get("ci95") and ((d["ci95"][0] > 0 and v["ci95"][0] > 0) or (d["ci95"][1] < 0 and v["ci95"][1] < 0)):
                result["notable"].append(f"{base} stop{stop_atr} rr{rr}: interval excludes zero in both development "
                                         f"({d['avg_r']:+.3f}R) and validation ({v['avg_r']:+.3f}R)")
    return result
