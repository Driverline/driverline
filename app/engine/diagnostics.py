"""Phase 3b: why does a baseline win, and would the win survive real conditions?

A structure test says a pattern exists. These diagnostics ask whether it is something a member could actually use:
  * outcome breakdown: how much of the average comes from targets, stops and timeouts (a hit rate below break-even with a
    positive average means the profit is coming from somewhere other than the target);
  * entry delay: the same trades entered 1, 2, 3 or 6 bars late (members act minutes after a bar closes);
  * harsher costs: 2x and 4x the assumed cost;
  * block-shuffled surrogates: shuffling in blocks of 6, 12 or 48 bars keeps the memory inside each block, so they show
    over what time scale the structure lives;
  * scales: how often direction reverses when price is sampled every 5 minutes, 10 minutes, ... 1 day;
  * ticks: what the structure looks like at tick level (Deriv only serves the last 24 hours of ticks).
The survival rule is fixed here in advance: a baseline is an EXECUTABLE CANDIDATE only if its 95% interval is above zero in
BOTH the development and validation segments (a) with the normal entry, (b) with the entry one bar late, and (c) with the
entry one bar late and costs doubled. Everything else is reported as not robust. The untouched test segment is never read."""
import math
import random

from . import features as FT
from . import research as RS
from . import structure as ST

STOP_ATR = 1.5
DELAYS = (0, 1, 2, 3, 6)
COST_MULTS = (1, 2, 4)
BLOCKS = (1, 6, 12, 48)
SCALES = (1, 2, 3, 6, 12, 24, 48, 144, 288)          # in M5 bars: 5 min, 10 min, 15 min, 30 min, 1 h, 2 h, 4 h, 12 h, 1 day
FOCUS = ("reversal_1", "momentum_1")
SURROGATES_DEFAULT = 40


def _run(bars, atr, bounds, base, seg, rr, delay=0, cost_mult=1, tag=""):
    lo, hi = bounds[seg]
    seed = RS.seed_for("diag", tag, base, seg, rr, delay)
    return RS.simulate(bars, atr, RS.baseline_signals(base, bars, lo, hi, seed), STOP_ATR, rr, RS.HORIZON,
                       RS.COST_ATR * cost_mult, lo, hi, delay)


def _summ(trades, rr, cost_mult, seed):
    return RS.summarize(trades, rr, RS.COST_ATR * cost_mult / STOP_ATR, seed)


def decomposition(bars, atr, bounds, tag):
    """Shares and average result by outcome type, for every baseline and target, development and validation."""
    out = []
    for stop_rr in (1.0, 1.5, 2.0):
        for base in RS.BASELINES:
            row = {"baseline": base, "rr": stop_rr}
            for seg in ("dev", "val"):
                tr = _run(bars, atr, bounds, base, seg, stop_rr, tag=tag)
                n = len(tr)
                d = {"n": n}
                if n:
                    d["avg_r"] = sum(t["r"] for t in tr) / n
                    d["avg_bars"] = sum(t["bars"] for t in tr) / n
                    for k in ("target", "stop", "timeout"):
                        xs = [t["r"] for t in tr if t["kind"] == k]
                        d[f"{k}_share"] = len(xs) / n
                        d[f"{k}_avg"] = sum(xs) / len(xs) if xs else None
                row[seg] = d
            out.append(row)
    return out


def delay_and_costs(bars, atr, bounds, tag):
    """For every baseline at the 1.5R target: the average result with the entry 0, 1, 2, 3 and 6 bars late, and with
    costs 1x, 2x and 4x (at 0 and 1 bars of delay)."""
    rr, rows = 1.5, []
    for base in RS.BASELINES:
        for delay in DELAYS:
            for cm in COST_MULTS:
                if cm != 1 and delay not in (0, 1):
                    continue
                row = {"baseline": base, "delay": delay, "cost_mult": cm}
                for seg in ("dev", "val"):
                    tr = _run(bars, atr, bounds, base, seg, rr, delay, cm, tag)
                    s = _summ(tr, rr, cm, RS.seed_for(tag, base, seg, delay, cm))
                    row[seg] = {k: s.get(k) for k in ("n", "avg_r", "ci95", "hit", "breakeven_hit")}
                rows.append(row)
    return rows


def surrogate_blocks(bars: list, block: int, rng: random.Random) -> list:
    """Shuffle the order of BLOCKS of consecutive bar shapes. block=1 destroys all memory; larger blocks keep the memory
    that lives inside a block."""
    shapes = [(b["close"] - b["open"], b["high"] - b["open"], b["low"] - b["open"]) for b in bars]
    blocks = [shapes[i:i + block] for i in range(0, len(shapes), block)]
    rng.shuffle(blocks)
    flat = [s for blk in blocks for s in blk]
    p, out = bars[0]["open"], []
    for b, (dc, dh, dl) in zip(bars, flat):
        out.append({"epoch": b["epoch"], "open": p, "high": p + dh, "low": p + dl, "close": p + dc})
        p += dc
    return out


def block_surrogates(bars, bounds, tag, n_surrogates, progress=None):
    """Real result of the focus baselines against block-shuffled copies, for each block size."""
    rr = 1.5
    use = bars[:bounds["val"][1]]
    atr_real, _ = FT.atr_series(use)
    real = {(b, s): (lambda tr: sum(t["r"] for t in tr) / len(tr) if tr else None)(_run(use, atr_real, bounds, b, s, rr, tag=tag))
            for b in FOCUS for s in ("dev", "val")}
    rows, done, total = [], 0, len(BLOCKS) * n_surrogates
    for B in BLOCKS:
        sur = {k: [] for k in real}
        for s in range(n_surrogates):
            sb = surrogate_blocks(use, B, random.Random(RS.seed_for(tag, "blk", B, s)))
            at, _ = FT.atr_series(sb)
            for b in FOCUS:
                for seg in ("dev", "val"):
                    tr = _run(sb, at, bounds, b, seg, rr, tag=(tag, B, s))
                    if tr:
                        sur[(b, seg)].append(sum(t["r"] for t in tr) / len(tr))
            done += 1
            if progress:
                progress(done, total)
        for (b, seg), val in real.items():
            xs = sur[(b, seg)]
            if val is None or len(xs) < 10:
                continue
            mu = sum(xs) / len(xs)
            sd = math.sqrt(sum((x - mu) ** 2 for x in xs) / (len(xs) - 1))
            rows.append({"block": B, "baseline": b, "segment": seg, "real": val, "surrogate_mean": mu, "surrogate_sd": sd,
                         "z": (val - mu) / sd if sd > 0 else 0.0})
    return rows


def scales(bars, bounds):
    """How often direction reverses when price is sampled every k bars (k x 5 minutes)."""
    rows = []
    for k in SCALES:
        row = {"bars": k, "minutes": 5 * k}
        for seg in ("dev", "val"):
            lo, hi = bounds[seg]
            closes = [b["close"] for b in bars[lo:hi]][::k]
            d = [b - a for a, b in zip(closes, closes[1:])]
            pairs = [(x, y) for x, y in zip(d, d[1:]) if x and y]
            if len(pairs) < 50:
                row[seg] = {"n": len(pairs), "p_reverse": None}
                continue
            rev = [abs(y) for x, y in pairs if (x > 0) != (y > 0)]
            cont = [abs(y) for x, y in pairs if (x > 0) == (y > 0)]
            rt = ST.runs_test([(v > 0) - (v < 0) for v in d])
            row[seg] = {"n": len(pairs), "p_reverse": len(rev) / len(pairs), "runs_z": rt["z"],
                        "mean_size_reverse": sum(rev) / len(rev) if rev else None,
                        "mean_size_continue": sum(cont) / len(cont) if cont else None}
        rows.append(row)
    return rows


def verdicts(dc_rows):
    """Apply the pre-registered survival rule to every baseline at the 1.5R target."""
    by = {(r["baseline"], r["delay"], r["cost_mult"]): r for r in dc_rows}
    out = []
    for base in RS.BASELINES:
        checks = {}
        for label, key in (("normal entry", (base, 0, 1)), ("entry one bar late", (base, 1, 1)),
                           ("one bar late, costs doubled", (base, 1, 2))):
            r = by.get(key)
            ok = bool(r) and all(r[s].get("ci95") and r[s]["ci95"][0] > 0 for s in ("dev", "val"))
            checks[label] = ok
        out.append({"baseline": base, "checks": checks, "executable_candidate": all(checks.values())})
    return out


def run_diagnostics(bars: list, symbol: str, n_surrogates: int = SURROGATES_DEFAULT, progress=None) -> dict:
    bounds = RS.split_bounds(len(bars))
    atr, _ = FT.atr_series(bars)
    tag = symbol
    dec = decomposition(bars, atr, bounds, tag)
    dc = delay_and_costs(bars, atr, bounds, tag)
    blk = block_surrogates(bars, bounds, tag, n_surrogates, progress)
    sc = scales(bars, bounds)
    vd = verdicts(dc)
    winners = [v["baseline"] for v in vd if v["executable_candidate"]]
    verdict = (f"Executable candidate(s) under the pre-registered rule: {', '.join(winners)}. They remain Research Only until the "
               f"untouched test and a forward demo agree." if winners else
               "No baseline survived the pre-registered rule (positive in both periods with a normal entry, one bar late, and "
               "one bar late with doubled costs). Nothing here is an executable edge.")
    return {"symbol": symbol, "family": ST.family_of(symbol), "bars": len(bars), "test_untouched": True,
            "bounds": {k: list(v) for k, v in bounds.items() if k != "test"}, "stop_atr": STOP_ATR, "decomposition": dec,
            "delay_costs": dc, "block_surrogates": blk, "scales": sc, "verdicts": vd, "verdict": verdict,
            "n_surrogates": n_surrogates}


# ---------------------------------------------------------------- tick level (Deriv serves only the last 24 hours)
def ticks_analysis(times: list, prices: list) -> dict:
    """Is the structure already there at tick level? Direction runs, autocorrelation, variance ratios, and how often
    direction reverses when the tick series is cut into bars of 10 s to 5 min."""
    d = [b - a for a, b in zip(prices, prices[1:])]
    out = {"ticks": len(prices), "hours": round((times[-1] - times[0]) / 3600, 1) if len(times) > 1 else 0}
    if len(d) < 2000:
        return {**out, "note": "too few ticks"}
    out["runs"] = ST.runs_test([(x > 0) - (x < 0) for x in d])
    lb = ST.ljung_box(d, 10)
    out["acf"] = {"lag1": lb["acf"][0], "lag2": lb["acf"][1], "lag3": lb["acf"][2], "ljung_box_p": lb["p"]}
    out["variance_ratio"] = {q: ST.variance_ratio(d, q) for q in (5, 30, 60, 300) if len(d) > 8 * q}
    sc = []
    for secs in (10, 30, 60, 120, 300, 600):
        by = {}
        for t, p in zip(times, prices):
            by[t - t % secs] = p                                  # the last price in each bar
        keys = sorted(by)
        cl = [by[k] for k in keys]
        dd = [b - a for a, b in zip(cl, cl[1:])]
        pairs = [(x, y) for x, y in zip(dd, dd[1:]) if x and y]
        if len(pairs) < 30:
            continue
        rev = sum(1 for x, y in pairs if (x > 0) != (y > 0))
        sc.append({"seconds": secs, "n": len(pairs), "p_reverse": rev / len(pairs),
                   "z": (rev / len(pairs) - 0.5) / (0.5 / math.sqrt(len(pairs)))})
    out["scales"] = sc
    return out
