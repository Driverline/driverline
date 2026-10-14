"""Phase 3: is there ANY structure to exploit? Pre-registered tests for Step Indices and Range Break Indices.

Everything here is decided before the data is looked at:
  * the list of hypotheses (HYPOTHESES), their direction and their horizons, are fixed in this file and registered in the
    model registry before each run, so a run cannot quietly add or drop a test;
  * every test is run on the development and the validation segment separately, and every p-value is adjusted for the
    number of tests (Holm) within its segment;
  * a pattern counts as REPLICATED only if it is significant after adjustment in BOTH segments, in the same direction;
  * the untouched test segment is never read here.
The surrogate test shuffles the order of the bars' shapes (keeping each bar's own open/high/low/close relationships) and
reruns the baselines, so an apparent edge that comes from the way stops and targets are defined, rather than from the
order of prices, shows up in the surrogates too and is not mistaken for structure."""
import math
import random
from collections import deque

from . import features as FT
from . import research as RS

LAGS = tuple(range(1, 11))
VR_Q = (2, 4, 8, 16, 32)
CHANNEL = 48
EVENT_H = {"bar_reversal": (1, 3, 6, 12), "breakout_continuation": (6, 12, 24), "failed_breakout": (6, 12, 24),
           "boundary_reversal": (6, 12, 24)}
SURROGATES_DEFAULT = 80
ALPHA = 0.05
MIN_EVENTS = 30

HYPOTHESES = {
    "direction_runs": "Direction of consecutive M5 closes is independent (runs test)",
    "return_autocorr": "M5 price changes are uncorrelated at lags 1 to 10",
    "variance_ratio": "Variance grows in proportion to time (variance ratio at 2, 4, 8, 16, 32 bars)",
    "volatility_clustering": "Absolute price changes are uncorrelated at lags 1 to 10",
    "hour_of_day": "Average price change does not depend on the hour of the day (24 hours)",
    "surrogate_baselines": "Each baseline's result is no different from the same baseline on order-shuffled bars",
    "bar_reversal": "After an up bar the next bars fall, after a down bar they rise (fade the last bar), 1/3/6/12 bars ahead",
    "breakout_continuation": f"After a close beyond the prior {CHANNEL}-bar range, price continues in that direction (6/12/24 bars)",
    "failed_breakout": f"A breakout that closes back inside the range within 3 bars reverses (6/12/24 bars)",
    "boundary_reversal": f"A touch of the prior {CHANNEL}-bar high or low without a close beyond it reverses (6/12/24 bars)",
}
FAMILY_TESTS = {"step": ("direction_runs", "return_autocorr", "variance_ratio", "volatility_clustering", "hour_of_day",
                         "surrogate_baselines", "bar_reversal"),
                "range_break": tuple(HYPOTHESES)}


# ---------------------------------------------------------------- statistics (pure, tested)
def p_from_z(z: float) -> float:
    return math.erfc(abs(z) / math.sqrt(2))


def chi2_sf(x: float, k: int) -> float:
    """Chance that a chi-square variable with k degrees of freedom exceeds x."""
    if x <= 0:
        return 1.0
    a, xx = k / 2.0, x / 2.0
    if xx < a + 1:
        ap, s = a, 1.0 / a
        d = s
        for _ in range(1000):
            ap += 1
            d *= xx / ap
            s += d
            if abs(d) < abs(s) * 1e-13:
                break
        return max(0.0, 1.0 - s * math.exp(-xx + a * math.log(xx) - math.lgamma(a)))
    b = xx + 1 - a
    c, d = 1e300, 1.0 / b
    h = d
    for i in range(1, 1000):
        an = -i * (i - a)
        b += 2
        d = an * d + b
        d = d if abs(d) > 1e-300 else 1e-300
        c = b + an / c
        c = c if abs(c) > 1e-300 else 1e-300
        d = 1.0 / d
        delta = d * c
        h *= delta
        if abs(delta - 1) < 1e-13:
            break
    return min(1.0, math.exp(-xx + a * math.log(xx) - math.lgamma(a)) * h)


def holm(ps: list) -> list:
    """Holm-adjusted p-values (family-wise error control), in the original order."""
    m = len(ps)
    order = sorted(range(m), key=lambda i: ps[i])
    out, running = [1.0] * m, 0.0
    for rank, i in enumerate(order):
        running = max(running, min(1.0, (m - rank) * ps[i]))
        out[i] = running
    return out


def runs_test(signs: list) -> dict:
    sg = [s for s in signs if s]
    n1, n2 = sum(1 for s in sg if s > 0), sum(1 for s in sg if s < 0)
    n = n1 + n2
    if n1 < 10 or n2 < 10:
        return {"n": n, "z": None, "p": None}
    runs = 1 + sum(1 for a, b in zip(sg, sg[1:]) if a != b)
    mu = 2 * n1 * n2 / n + 1
    var = (mu - 1) * (mu - 2) / (n - 1)
    z = (runs - mu) / math.sqrt(var)
    return {"n": n, "z": z, "p": p_from_z(z), "runs": runs, "expected_runs": mu}


def autocorr(x: list, lag: int) -> float:
    n = len(x)
    mu = sum(x) / n
    den = sum((v - mu) ** 2 for v in x)
    if den == 0 or lag >= n:
        return 0.0
    return sum((x[t] - mu) * (x[t - lag] - mu) for t in range(lag, n)) / den


def ljung_box(x: list, m: int) -> dict:
    n = len(x)
    rs = [autocorr(x, k) for k in range(1, m + 1)]
    q = n * (n + 2) * sum(r * r / (n - k) for k, r in enumerate(rs, 1))
    return {"q": q, "p": chi2_sf(q, m), "acf": rs, "n": n}


def variance_ratio(r: list, q: int) -> dict:
    """Lo-MacKinlay variance ratio with the heteroskedasticity-robust z statistic."""
    m = len(r)
    mu = sum(r) / m
    d = [v - mu for v in r]
    s2a = sum(v * v for v in d) / (m - 1)
    if s2a == 0 or m < 4 * q:
        return {"vr": None, "z": None, "p": None}
    pre = [0.0]
    for v in r:
        pre.append(pre[-1] + v)
    s2c = sum((pre[t] - pre[t - q] - q * mu) ** 2 for t in range(q, m + 1)) / (q * (m - q + 1) * (1 - q / m))
    vr = s2c / s2a
    den = sum(v * v for v in d) ** 2
    theta = 0.0
    for j in range(1, q):
        delta = sum(d[t] ** 2 * d[t - j] ** 2 for t in range(j, m)) / den * m
        theta += (2 * (q - j) / q) ** 2 * delta
    z = (vr - 1) / math.sqrt(theta / m) if theta > 0 else 0.0
    return {"vr": vr, "z": z, "p": p_from_z(z)}


def hour_effect(epochs: list, r: list) -> list:
    """For each hour of the day: the t statistic of its average price change against zero."""
    groups = {}
    for e, v in zip(epochs, r):
        groups.setdefault((e // 3600) % 24, []).append(v)
    out = []
    for h in range(24):
        g = groups.get(h, [])
        n = len(g)
        if n < 30:
            out.append({"hour": h, "n": n, "t": None, "p": None})
            continue
        mu = sum(g) / n
        sd = math.sqrt(sum((v - mu) ** 2 for v in g) / (n - 1))
        t = mu / (sd / math.sqrt(n)) if sd > 0 else 0.0
        out.append({"hour": h, "n": n, "t": t, "p": p_from_z(t), "mean": mu})
    return out


# ---------------------------------------------------------------- surrogate data
def surrogate_bars(bars: list, rng: random.Random) -> list:
    """The same bars with their ORDER shuffled: every bar keeps its own shape (open to close, high and low), but the
    sequence, and so any memory between bars, is destroyed."""
    shapes = [(b["close"] - b["open"], b["high"] - b["open"], b["low"] - b["open"]) for b in bars]
    rng.shuffle(shapes)
    p, out = bars[0]["open"], []
    for b, (dc, dh, dl) in zip(bars, shapes):
        out.append({"epoch": b["epoch"], "open": p, "high": p + dh, "low": p + dl, "close": p + dc})
        p += dc
    return out


def _baseline_avgs(bars: list, bounds: dict, stop_atr: float, rr: float, seed_tag) -> dict:
    atr, _ = FT.atr_series(bars)
    out = {}
    for seg in ("dev", "val"):
        lo, hi = bounds[seg]
        for base in RS.BASELINES:
            seed = RS.seed_for(seed_tag, base, seg)
            tr = RS.simulate(bars, atr, RS.baseline_signals(base, bars, lo, hi, seed), stop_atr, rr, RS.HORIZON, RS.COST_ATR, lo, hi)
            out[(base, seg)] = sum(t["r"] for t in tr) / len(tr) if tr else None
    return out


def surrogate_test(bars: list, n_surrogates: int, seed: str, stop_atr: float = 1.5, rr: float = 1.5, progress=None) -> list:
    """For each baseline and segment: the real average result, the surrogate mean and spread, and a z-based p-value."""
    bounds = RS.split_bounds(len(bars))
    bars = bars[:bounds["val"][1]]       # the untouched test segment is not even used to build surrogates
    real = _baseline_avgs(bars, bounds, stop_atr, rr, ("real", seed))
    sur = {k: [] for k in real}
    for s in range(n_surrogates):
        rng = random.Random(RS.seed_for(seed, "surrogate", s))
        for k, v in _baseline_avgs(surrogate_bars(bars, rng), bounds, stop_atr, rr, ("s", seed, s)).items():
            if v is not None:
                sur[k].append(v)
        if progress:
            progress(s + 1, n_surrogates)
    rows = []
    for (base, seg), val in real.items():
        xs = sur[(base, seg)]
        if val is None or len(xs) < 10:
            rows.append({"baseline": base, "segment": seg, "real": val, "p": None})
            continue
        mu = sum(xs) / len(xs)
        sd = math.sqrt(sum((x - mu) ** 2 for x in xs) / (len(xs) - 1))
        z = (val - mu) / sd if sd > 0 else 0.0
        rows.append({"baseline": base, "segment": seg, "real": val, "surrogate_mean": mu, "surrogate_sd": sd, "z": z,
                     "p": p_from_z(z), "n_surrogates": len(xs)})
    return rows


# ---------------------------------------------------------------- event studies
def channel_flags(bars: list, n: int = CHANNEL):
    """For each bar: (broke_up, broke_down, touched_high, touched_low) against the PRIOR n bars' high and low."""
    hi, lo = deque(), deque()          # monotonic deques of (index, value)
    out = [None] * len(bars)
    for i, b in enumerate(bars):
        if i >= n:
            ph, pl = bars[hi[0][0]]["high"], bars[lo[0][0]]["low"]
            up, dn = b["close"] > ph, b["close"] < pl
            out[i] = (up, dn, (b["high"] >= ph and not up), (b["low"] <= pl and not dn))
        while hi and hi[-1][1] <= b["high"]:
            hi.pop()
        hi.append((i, b["high"]))
        while hi[0][0] <= i - n:
            hi.popleft()
        while lo and lo[-1][1] >= b["low"]:
            lo.pop()
        lo.append((i, b["low"]))
        while lo[0][0] <= i - n:
            lo.popleft()
    return out


def events_for(name: str, bars: list, flags: list) -> list:
    """(index, predicted direction) pairs, with the direction fixed by the hypothesis in advance."""
    ev = []
    for i in range(1, len(bars)):
        if name == "bar_reversal":
            d = bars[i]["close"] - bars[i - 1]["close"]
            if d:
                ev.append((i, -1 if d > 0 else 1))
            continue
        f = flags[i]
        if f is None:
            continue
        up, dn, th, tl = f
        if name == "breakout_continuation":
            if up:
                ev.append((i, 1))
            elif dn:
                ev.append((i, -1))
        elif name == "boundary_reversal":
            if th:
                ev.append((i, -1))
            elif tl:
                ev.append((i, 1))
        elif name == "failed_breakout":
            # a breakout within the last 3 bars, and this bar closes back inside the range that was broken
            for back in (1, 2, 3):
                j = i - back
                if j >= 1 and flags[j] and (flags[j][0] or flags[j][1]):
                    ph = max(b["high"] for b in bars[max(0, j - CHANNEL):j])
                    pl = min(b["low"] for b in bars[max(0, j - CHANNEL):j])
                    if flags[j][0] and bars[i]["close"] <= ph:
                        ev.append((i, -1))
                        break
                    if flags[j][1] and bars[i]["close"] >= pl:
                        ev.append((i, 1))
                        break
    return ev


def event_study(bars: list, atr: list, events: list, h: int, lo: int, hi: int, seed: int) -> dict:
    """Average forward move in ATR units, in the hypothesised direction, over events at least h bars apart. Positive
    means the hypothesis was right."""
    vals, last = [], -10 ** 9
    for i, d in events:
        if i < lo or i + h >= hi or i < last + h:
            continue
        a = atr[i]
        if not a:
            continue
        vals.append(d * (bars[i + h]["close"] - bars[i]["close"]) / a)
        last = i
    n = len(vals)
    if n < MIN_EVENTS:
        return {"n": n, "mean": None, "t": None, "p": None}
    mu = sum(vals) / n
    sd = math.sqrt(sum((v - mu) ** 2 for v in vals) / (n - 1))
    t = mu / (sd / math.sqrt(n)) if sd > 0 else 0.0
    return {"n": n, "mean": mu, "t": t, "p": p_from_z(t), "ci95": RS.block_bootstrap_ci(vals, seed, reps=500)}


# ---------------------------------------------------------------- the whole study
def family_of(symbol: str) -> str:
    return "range_break" if symbol.upper().startswith("RB") else "step"


def run_structure(bars: list, symbol: str, n_surrogates: int = SURROGATES_DEFAULT, logger=None, progress=None) -> dict:
    """Runs every pre-registered test for the symbol's family on the development and validation segments only."""
    fam = family_of(symbol)
    names = FAMILY_TESTS[fam]
    bounds = RS.split_bounds(len(bars))
    atr, _ = FT.atr_series(bars)
    flags = channel_flags(bars)
    tests = []

    def add(hyp, name, seg, stat, p, n, effect=None, detail=None):
        tests.append({"hypothesis": hyp, "name": name, "segment": seg, "stat": stat, "p": p, "n": n, "effect": effect,
                      "detail": detail})

    for seg in ("dev", "val"):
        lo, hi = bounds[seg]
        closes = [b["close"] for b in bars[lo:hi]]
        rets = [b - a for a, b in zip(closes, closes[1:])]
        eps = [b["epoch"] for b in bars[lo + 1:hi]]
        if "direction_runs" in names:
            r = runs_test([(x > 0) - (x < 0) for x in rets])
            add("direction_runs", "runs test", seg, r["z"], r["p"], r["n"], effect=None if r["z"] is None else -r["z"],
                detail={"runs": r.get("runs"), "expected": r.get("expected_runs")})
        if "return_autocorr" in names:
            for k in LAGS:
                rk = autocorr(rets, k)
                z = rk * math.sqrt(len(rets))
                add("return_autocorr", f"lag {k}", seg, rk, p_from_z(z), len(rets), effect=rk)
        if "variance_ratio" in names:
            for q in VR_Q:
                v = variance_ratio(rets, q)
                add("variance_ratio", f"q={q}", seg, v["vr"], v["p"], len(rets), effect=None if v["vr"] is None else v["vr"] - 1)
        if "volatility_clustering" in names:
            ab = [abs(x) for x in rets]
            for k in LAGS:
                rk = autocorr(ab, k)
                add("volatility_clustering", f"lag {k}", seg, rk, p_from_z(rk * math.sqrt(len(ab))), len(ab), effect=rk)
        if "hour_of_day" in names:
            for h in hour_effect(eps, rets):
                add("hour_of_day", f"hour {h['hour']:02d} UTC", seg, h.get("t"), h["p"], h["n"], effect=h.get("mean"))
        for name in ("bar_reversal", "breakout_continuation", "failed_breakout", "boundary_reversal"):
            if name not in names:
                continue
            ev = events_for(name, bars, flags)
            for h in EVENT_H[name]:
                e = event_study(bars, atr, ev, h, lo, hi, RS.seed_for(symbol, name, h, seg))
                add(name, f"{h} bars ahead", seg, e["mean"], e["p"], e["n"], effect=e["mean"],
                    detail={"t": e["t"], "ci95": e.get("ci95")})
    if "surrogate_baselines" in names:
        for r in surrogate_test(bars, n_surrogates, symbol, progress=progress):
            add("surrogate_baselines", r["baseline"], r["segment"], r["real"], r["p"], r.get("n_surrogates"),
                effect=None if r["real"] is None else r["real"] - (r.get("surrogate_mean") or 0.0),
                detail={"surrogate_mean": r.get("surrogate_mean"), "surrogate_sd": r.get("surrogate_sd"), "z": r.get("z")})
    # multiplicity: Holm within each segment, over every test that could be run
    for seg in ("dev", "val"):
        idx = [i for i, t in enumerate(tests) if t["segment"] == seg]
        adj = holm([tests[i]["p"] if tests[i]["p"] is not None else 1.0 for i in idx])
        for i, a in zip(idx, adj):
            tests[i]["p_adj"] = a
    by = {}
    for t in tests:
        by.setdefault((t["hypothesis"], t["name"]), {})[t["segment"]] = t
    replicated = []
    for (hyp, name), segs in by.items():
        d, v = segs.get("dev"), segs.get("val")
        if d and v and d["p_adj"] < ALPHA and v["p_adj"] < ALPHA and d["effect"] is not None and v["effect"] is not None \
                and (d["effect"] > 0) == (v["effect"] > 0):
            replicated.append({"hypothesis": hyp, "name": name, "dev": d["stat"], "val": v["stat"], "dev_p_adj": d["p_adj"],
                               "val_p_adj": v["p_adj"], "dev_n": d["n"], "val_n": v["n"], "effect_dev": d["effect"],
                               "effect_val": v["effect"]})
    replicated.sort(key=lambda r: max(r["dev_p_adj"], r["val_p_adj"]))
    if logger:
        for t in tests:
            logger(f"{t['hypothesis']}|{t['name']}", t["segment"], {"hypothesis": t["hypothesis"], "name": t["name"]},
                   {k: t[k] for k in ("stat", "p", "p_adj", "n", "effect")})
    n_per_seg = len(tests) // 2
    verdict = (f"{len(replicated)} pattern(s) replicated in development and validation after adjusting for {n_per_seg} tests per segment."
               if replicated else
               f"No pattern replicated: nothing was significant in both development and validation after adjusting for "
               f"{n_per_seg} tests per segment.")
    return {"symbol": symbol, "family": fam, "bars": len(bars), "bounds": {k: list(v) for k, v in bounds.items() if k != "test"},
            "test_untouched": True, "tests_per_segment": n_per_seg, "alpha": ALPHA, "replicated": replicated, "verdict": verdict,
            "tests": tests, "n_surrogates": n_surrogates if "surrogate_baselines" in names else 0}
