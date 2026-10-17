"""Phase 4a: a pre-registered candidate search, and a ONE-SHOT evaluation on the untouched test segment.

Procedure (fixed before any candidate is run):
  1. The candidates are the 16 combinations in GRID: fade the direction of the last completed 5-minute or 15-minute bar,
     enter 1 or 2 five-minute bars late, stop 1.5 or 3 ATR, target 1.0R or 1.5R. Every one of them is run on the
     development and validation segments with costs DOUBLED, and every one is logged.
  2. ADVANCE GATE: a candidate may go to the untouched test only if its 95% interval is above zero in BOTH development and
     validation (with the late entry and doubled costs) and validation has at least 150 trades. Among those that pass, the
     one with the best worst-case lower bound is selected. If none passes, nothing advances and the test stays untouched.
  3. The split is FROZEN at search time as epochs, so data added later can never move bars between segments.
  4. TEST (once): the selected candidate is evaluated on the untouched segment. It becomes DEMO VALIDATED only if all hold:
     at least 300 trades, the 95% interval above zero after the doubled costs, better than the random-direction baseline run
     with the same entry delay and costs, and a positive average in at least two of three time-thirds. Otherwise it stays
     RESEARCH ONLY. The test cannot be run a second time for the same search."""
import json

from . import features as FT
from . import research as RS
from . import store, validate as VAL

GRID = [{"k": k, "delay": d, "stop_atr": s, "rr": rr} for k in (1, 3) for d in (1, 2) for s in (1.5, 3.0) for rr in (1.0, 1.5)]
COST_MULT = 2
MIN_VAL_TRADES = 150
MIN_TEST_TRADES = 300
BAR = 300
RISK_DEFAULTS = {"max_vol_ratio": 3.0, "min_stop_points": 3.0, "max_signals_per_day": 24, "stale_seconds": 900,
                 "suspend_after_resolved": 30, "suspend_if_mean_r_below": -0.10, "max_loss_streak": 12}


def fade_signals(bars: list, lo: int, hi: int, k: int) -> list:
    """Fade the direction of the last COMPLETED k-bar block (price now against price k bars ago). A block completes on the
    5-minute bar whose index lines up with the epoch grid, so research and live pick exactly the same bars."""
    out = []
    for i in range(max(lo, k), hi):
        if (bars[i]["epoch"] // BAR + 1) % k:
            continue
        d = bars[i]["close"] - bars[i - k]["close"]
        if d:
            out.append((i, -1 if d > 0 else 1))
    return out


def run_candidate(bars, atr, cand, lo, hi, cost_mult=COST_MULT):
    return RS.simulate(bars, atr, fade_signals(bars, lo, hi, cand["k"]), cand["stop_atr"], cand["rr"], RS.HORIZON,
                       RS.COST_ATR * cost_mult, lo, hi, cand["delay"])


def _stat(trades, cand, seed):
    return RS.summarize(trades, cand["rr"], RS.COST_ATR * COST_MULT / cand["stop_atr"], seed)


def name_for(symbol: str, c: dict) -> str:
    return f"{symbol.lower()}_fade_k{c['k']}_d{c['delay']}_s{c['stop_atr']:g}_r{c['rr']:g}"


def frozen_split(bars: list) -> dict:
    b = RS.split_bounds(len(bars))
    return {"dev_end_epoch": bars[b["dev"][1]]["epoch"], "val_end_epoch": bars[b["val"][1]]["epoch"],
            "test_end_epoch": bars[-1]["epoch"] + BAR, "bars_sha": VAL.dataset_hash(bars)}


def search(bars: list, symbol: str, logger=None) -> dict:
    """Run the whole grid on development and validation, apply the advance gate, pick the candidate (if any)."""
    atr, _ = FT.atr_series(bars)
    bounds = RS.split_bounds(len(bars))
    rows = []
    for c in GRID:
        row = {**c, "name": name_for(symbol, c)}
        for seg in ("dev", "val"):
            lo, hi = bounds[seg]
            tr = run_candidate(bars, atr, c, lo, hi)
            m = _stat(tr, c, RS.seed_for(symbol, name_for(symbol, c), seg))
            row[seg] = {k: m.get(k) for k in ("n", "avg_r", "ci95", "hit", "breakeven_hit", "max_drawdown_r")}
            if logger:
                logger(row["name"], seg, c, row[seg])
        lo_d = (row["dev"].get("ci95") or [None])[0]
        lo_v = (row["val"].get("ci95") or [None])[0]
        row["worst_lower"] = None if lo_d is None or lo_v is None else min(lo_d, lo_v)
        row["passes_gate"] = bool(lo_d is not None and lo_v is not None and lo_d > 0 and lo_v > 0
                                  and row["val"]["n"] >= MIN_VAL_TRADES)
        rows.append(row)
    passing = sorted((r for r in rows if r["passes_gate"]), key=lambda r: -r["worst_lower"])
    selected = passing[0] if passing else None
    best = max((r for r in rows if r["worst_lower"] is not None), key=lambda r: r["worst_lower"], default=None)
    if selected:
        verdict = (f"{len(passing)} of {len(rows)} candidates passed the advance gate. Selected {selected['name']} "
                   f"(worst-case lower bound {selected['worst_lower']:+.3f}R). It may be evaluated once on the untouched test.")
    else:
        verdict = (f"None of the {len(rows)} candidates passed the advance gate (95% interval above zero in both development and "
                   f"validation with a late entry and doubled costs). Nothing advances and the untouched test stays untouched."
                   + (f" Closest: {best['name']} (worst-case lower bound {best['worst_lower']:+.3f}R)." if best else ""))
    return {"symbol": symbol, "bars": len(bars), "cost_multiple": COST_MULT, "grid_size": len(GRID), "candidates": rows,
            "selected": selected, "advance": selected is not None, "verdict": verdict, "frozen_split": frozen_split(bars),
            "test_untouched": True, "test_result": None, "gate": {"min_val_trades": MIN_VAL_TRADES}}


def definition_for(symbol: str, c: dict) -> dict:
    return {"rule": "fade_aggregated_bar", "symbol": symbol, "tf": BAR, "k": c["k"], "delay": c["delay"],
            "stop_atr": c["stop_atr"], "rr": c["rr"], "horizon": RS.HORIZON, "cost_atr": RS.COST_ATR * COST_MULT,
            "risk": dict(RISK_DEFAULTS)}


def evaluate_test(bars: list, search_result: dict, run_id: int) -> dict:
    """The one-shot evaluation. Raises if the search did not advance a candidate, if the data changed, or if the test
    for this search has already been used."""
    sel = search_result.get("selected")
    if not search_result.get("advance") or not sel:
        raise RuntimeError("No candidate advanced from the search, so there is nothing to test.")
    fs = search_result["frozen_split"]
    used = [b for b in bars if b["epoch"] < fs["test_end_epoch"]]
    if VAL.dataset_hash(used) != fs["bars_sha"]:
        raise RuntimeError("The stored candles differ from the ones the search used. The test was NOT run or used up.")
    symbol = search_result["symbol"]
    idx = {b["epoch"]: i for i, b in enumerate(used)}
    lo, hi = idx[fs["val_end_epoch"]], len(used)
    atr, _ = FT.atr_series(used)
    c = {k: sel[k] for k in ("k", "delay", "stop_atr", "rr")}
    trades = run_candidate(used, atr, c, lo, hi)
    seed = RS.seed_for(symbol, sel["name"], "test")
    m = _stat(trades, c, seed)
    third = len(trades) // 3
    thirds = [sum(t["r"] for t in part) / len(part) for part in (trades[:third], trades[third:2 * third], trades[2 * third:]) if part] if third else []
    rnd = RS.simulate(used, atr, RS.baseline_signals("random", used, lo, hi, seed), c["stop_atr"], c["rr"], RS.HORIZON,
                      RS.COST_ATR * COST_MULT, lo, hi, c["delay"])
    rnd_avg = sum(t["r"] for t in rnd) / len(rnd) if rnd else None
    why = []
    if m["n"] < MIN_TEST_TRADES:
        why.append(f"only {m['n']} trades (need {MIN_TEST_TRADES})")
    ci = m.get("ci95")
    if not ci or ci[0] <= 0:
        why.append("95% interval is not above zero after doubled costs")
    if rnd_avg is None or not ci or ci[0] <= rnd_avg:
        why.append("does not beat the random-direction baseline with the same delay and costs")
    if sum(1 for x in thirds if x > 0) < 2:
        why.append("not positive in at least two of three time-thirds")
    passed = not why
    result = {"candidate": sel["name"], "n": m["n"], "avg_r": m.get("avg_r"), "ci95": ci, "hit": m.get("hit"),
              "breakeven_hit": m.get("breakeven_hit"), "max_drawdown_r": m.get("max_drawdown_r"), "time_thirds": thirds,
              "random_baseline_avg_r": rnd_avg, "passed": passed, "why_not": why, "test_bars": hi - lo,
              "window": [fs["val_end_epoch"], fs["test_end_epoch"]]}
    # Only now is the one-shot lock taken: a failure above can never use the test up, and a result is only ever returned
    # to the caller if this call is the one that used it.
    if not store.unlock_test(run_id):
        raise RuntimeError("The untouched test for this search has already been used. It can never be run twice.")
    return result


def apply_outcome(model_id: int, result: dict) -> None:
    if result["passed"]:
        store.set_model_status(model_id, "demo_validated",
                               f"passed the untouched test: {result['n']} trades, average {result['avg_r']:+.3f}R, "
                               f"95% interval {result['ci95'][0]:+.3f} to {result['ci95'][1]:+.3f}")
    else:
        store.set_model_status(model_id, "research_only", "failed the untouched test: " + "; ".join(result["why_not"]))
