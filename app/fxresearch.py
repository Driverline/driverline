"""Research harness for forex and gold: do any strategies built for these markets beat their trading costs?

Nothing here publishes a signal. It tests a SMALL, fixed list of strategies (chosen in advance, never tuned to the
results) on years of history, with realistic spread costs, and reports which, if any, pass a tough set of checks.
Only a strategy that passes is a candidate to be wired into the live feed, and the owner decides that.

How every trade is simulated: the signal is read at a candle's close, the entry is the NEXT candle's open, the stop
is a multiple of ATR, trades on a symbol never overlap, and if the stop and the target are both touched inside one
candle the stop counts first. A candle that opens beyond the stop (a weekend gap) fills at that open, not at the stop.
The cost of a trade is the assumed spread divided by the stop distance, so tight stops cost more R.

Checks a (strategy, target) must pass, for forex (7 majors pooled) or for gold on its own:
  1. enough trades, average result above zero after costs, and a t-statistic of at least 2.0 for forex and 2.5 for
     gold (gold is a single symbol with fewer trades, so a lucky result is far more likely and the bar is higher)
  2. positive in the first 60% of the history AND clearly positive in the last 40% (t of at least 1; that later part
     was never used to choose anything)
  3. still positive when the spread is 50% wider
  4. forex only: positive on at least 4 of the 7 pairs
The number of combinations tested is shown, because some will look good by chance alone."""
import asyncio
import json
import math
import time
from contextlib import closing

from . import dbx
from . import signals as S
from .config import INSTRUMENTS, REAL_MARKETS, market, spread as spread_of
from .indicators import ema

PERIOD = {**S.PERIOD, "D1": 86400}
SCHEMA = """
    CREATE TABLE IF NOT EXISTS fx_research(id {ID}, started_at BIGINT NOT NULL, finished_at BIGINT,
        status TEXT NOT NULL, result TEXT)"""

CFG = {"min_t": 2.0, "min_t_gold": 2.5, "min_t_test": 1.0, "min_trades_forex": 150, "min_trades_gold": 150, "stress": 1.5, "train_share": 0.6,
       "min_pairs_positive": 4, "history": {"D1": 2500, "H4": 6000, "H1": 12000}, "gold_m15": 20000}

# name: (timeframe, context timeframe or None, stop in ATR, horizon in candles, targets in R, markets it runs on)
STRATEGIES = {
    "trend_pullback_h4": ("H4", "D1", 2.0, 30, (1.5, 2.0, 3.0), ("forex", "metal")),
    "donchian_h4": ("H4", "D1", 2.0, 60, (2.0, 3.0), ("forex", "metal")),
    "donchian_d1": ("D1", None, 2.5, 40, (2.0, 3.0), ("forex", "metal")),
    "d1_trend_pullback": ("D1", None, 2.5, 40, (2.0, 3.0), ("forex", "metal")),
    "london_breakout": ("H1", None, 1.5, 12, (1.0, 1.5, 2.0), ("forex", "metal")),
    "asian_reversion": ("H1", None, 1.5, 8, (1.0, 1.5), ("forex", "metal")),
    "gold_day_reversal": ("M15", "H1", 1.5, 32, (1.5, 2.0), ("metal",)),
}
LABELS = {"trend_pullback_h4": "H4 trend pullback", "donchian_h4": "H4 55-bar breakout",
          "donchian_d1": "D1 55-bar breakout", "d1_trend_pullback": "D1 trend pullback",
          "london_breakout": "London breakout of the Asian range", "asian_reversion": "Asian-session range reversal",
          "gold_day_reversal": "Gold day range reversal (retest)"}


# ---------------------------------------------------------------- the strategies: lists of (bar index, +1 buy / -1 sell)
def sig_trend_pullback(F, ctx):
    o, h, l, c, e20, e50 = F["o"], F["h"], F["l"], F["c"], F["e20"], F["e50"]
    out = []
    for i in range(210, F["n"] - 1):
        if ctx[i] > 0 and e20[i] > e50[i] > e50[i - 6] and c[i] > e20[i] and c[i] > o[i] and min(l[i - 2:i + 1]) <= e20[i]:
            out.append((i, 1))
        elif ctx[i] < 0 and e20[i] < e50[i] < e50[i - 6] and c[i] < e20[i] and c[i] < o[i] and max(h[i - 2:i + 1]) >= e20[i]:
            out.append((i, -1))
    return out


def sig_donchian(F, ctx, n=55):
    h, l, c = F["h"], F["l"], F["c"]
    out = []
    for i in range(210, F["n"] - 1):
        if c[i] > max(h[i - n:i]) and (ctx is None or ctx[i] > 0) and (ctx is not None or c[i] > F["e200"][i]):
            out.append((i, 1))
        elif c[i] < min(l[i - n:i]) and (ctx is None or ctx[i] < 0) and (ctx is not None or c[i] < F["e200"][i]):
            out.append((i, -1))
    return out


def sig_d1_pullback(F, ctx):
    o, h, l, c, e20, e50, e200 = F["o"], F["h"], F["l"], F["c"], F["e20"], F["e50"], F["e200"]
    out = []
    for i in range(210, F["n"] - 1):
        if e20[i] > e50[i] > e200[i] and c[i] > e20[i] and c[i] > o[i] and min(l[i - 2:i + 1]) <= e20[i]:
            out.append((i, 1))
        elif e20[i] < e50[i] < e200[i] and c[i] < e20[i] and c[i] < o[i] and max(h[i - 2:i + 1]) >= e20[i]:
            out.append((i, -1))
    return out


def sig_london(F, ctx):
    """First close beyond the Asian range (00:00 to 06:59 UTC) during 07:00 to 10:59 UTC, once per day."""
    ep, h, l, c, atr = F["epoch"], F["h"], F["l"], F["c"], F["atr"]
    rng, done, out = {}, set(), []
    for i in range(F["n"] - 1):
        day, hour = ep[i] // 86400, (ep[i] // 3600) % 24
        if hour <= 6:
            hi, lo, k = rng.get(day, (-1e18, 1e18, 0))
            rng[day] = (max(hi, h[i]), min(lo, l[i]), k + 1)
        elif hour <= 10 and day not in done and i >= 120:
            hi, lo, k = rng.get(day, (0, 0, 0))
            if k < 6 or not 0.5 * atr[i] <= hi - lo <= 6 * atr[i]:
                continue
            if c[i] > hi:
                out.append((i, 1)); done.add(day)
            elif c[i] < lo:
                out.append((i, -1)); done.add(day)
    return out


def sig_asian_reversion(F, ctx):
    ep, o, h, l, c = F["epoch"], F["o"], F["h"], F["l"], F["c"]
    out = []
    for i in range(120, F["n"] - 1):
        if not 1 <= (ep[i] // 3600) % 24 <= 6 or F["er"][i] > 0.35:
            continue
        if l[i] <= F["lo"][i] and c[i] > F["lo"][i] and c[i] > o[i]:
            out.append((i, 1))
        elif h[i] >= F["up"][i] and c[i] < F["up"][i] and c[i] < o[i]:
            out.append((i, -1))
    return out


def sig_gold_reversal(F, ctx):
    return S.find_signals("range_reversal", F, ctx)


DETECT = {"trend_pullback_h4": sig_trend_pullback, "donchian_h4": lambda F, c: sig_donchian(F, c),
          "donchian_d1": lambda F, c: sig_donchian(F, None), "d1_trend_pullback": sig_d1_pullback,
          "london_breakout": sig_london, "asian_reversion": sig_asian_reversion, "gold_day_reversal": sig_gold_reversal}


# ---------------------------------------------------------------- simulation with times, spread costs and gap fills
def sim(F: dict, sigs: list, rr: float, horizon: int, stop_atr: float, spread: float) -> list:
    """[(entry epoch, net R, hit target?)] for sequential, non-overlapping trades."""
    o, h, l, c, atr, ep, n = F["o"], F["h"], F["l"], F["c"], F["atr"], F["epoch"], F["n"]
    out, free_from = [], 0
    for i, d in sigs:
        if i + 1 < free_from or i + 1 >= n:
            continue
        entry, sd = o[i + 1], stop_atr * atr[i]
        if sd <= 0:
            continue
        cost = spread / sd
        stop, target = entry - d * sd, entry + d * rr * sd
        end, res = min(n - 1, i + horizon), None
        for j in range(i + 1, end + 1):
            if (d > 0 and o[j] <= stop) or (d < 0 and o[j] >= stop):      # gapped through the stop: filled at the open
                res, free_from = (d * (o[j] - entry) / sd - cost, False), j + 1
                break
            hit_stop = (l[j] <= stop) if d > 0 else (h[j] >= stop)
            hit_tgt = (h[j] >= target) if d > 0 else (l[j] <= target)
            if hit_stop:
                res, free_from = (-1.0 - cost, False), j + 1
                break
            if hit_tgt:
                res, free_from = (rr - cost, True), j + 1
                break
        if res is None:
            res, free_from = (d * (c[end] - entry) / sd - cost, False), end + 1
        out.append((ep[i + 1], res[0], res[1]))
    return out


def _stats(rs: list) -> dict:
    n = len(rs)
    if n == 0:
        return {"n": 0, "avg": None, "t": None}
    mean = sum(rs) / n
    sd = math.sqrt(sum((x - mean) ** 2 for x in rs) / (n - 1)) if n > 1 else 0.0
    return {"n": n, "avg": mean, "t": (mean / (sd / math.sqrt(n))) if sd > 0 else 0.0}


def _prepare(candles: dict, tf: str) -> dict:
    F = S.build(candles[tf], False)
    F["e200"] = ema(F["c"], 200)
    return F


def research_symbol(key: str, data: dict) -> dict:
    """{(strategy, rr): {"trades": [...], "stress": [...], "span": (first, last)}} for one symbol."""
    mk, sp = market(key), spread_of(key)
    out = {}
    for name, (tf, ctf, stop_atr, horizon, rrs, markets) in STRATEGIES.items():
        if mk not in markets or tf not in data or len(data[tf]) < 400:
            continue
        if ctf and (ctf not in data or len(data[ctf]) < 100):
            continue
        F = _prepare(data, tf)
        ctx = None
        if ctf:
            X = _prepare(data, ctf)
            ctx = S.ctx_direction(F, X, PERIOD[tf], PERIOD[ctf])
        sigs = DETECT[name](F, ctx)
        span = (F["epoch"][0], F["epoch"][-1])
        for rr in rrs:
            out[(name, rr)] = {"trades": sim(F, sigs, rr, horizon, stop_atr, sp),
                               "stress": sim(F, sigs, rr, horizon, stop_atr, sp * CFG["stress"]), "span": span}
    return out


def evaluate(per_symbol: dict) -> list:
    """per_symbol: {key: research_symbol(...)}. Returns one row per (strategy, target, group) with the verdict."""
    rows = []
    combos = {(name, rr) for res in per_symbol.values() for (name, rr) in res}
    for name, rr in sorted(combos):
        for group in ("forex", "metal"):
            keys = [k for k in per_symbol if market(k) == group and (name, rr) in per_symbol[k]]
            if not keys:
                continue
            allr, train, test, stress, pos_pairs, pairs = [], [], [], [], 0, 0
            hits = 0
            for k in keys:
                rec = per_symbol[k][(name, rr)]
                a, b = rec["span"]
                cut = a + CFG["train_share"] * (b - a)
                rs = [t[1] for t in rec["trades"]]
                allr += rs
                hits += sum(1 for t in rec["trades"] if t[2])
                train += [t[1] for t in rec["trades"] if t[0] < cut]
                test += [t[1] for t in rec["trades"] if t[0] >= cut]
                stress += [t[1] for t in rec["stress"]]
                if len(rs) >= 15:
                    pairs += 1
                    pos_pairs += (sum(rs) / len(rs)) > 0
            tot, tr, te, st = _stats(allr), _stats(train), _stats(test), _stats(stress)
            need = CFG["min_trades_forex"] if group == "forex" else CFG["min_trades_gold"]
            why = []
            if tot["n"] < need:
                why.append(f"only {tot['n']} trades (need {need})")
            if not tot["n"] or tot["avg"] <= 0:
                why.append("average result is not above zero after costs")
            elif tot["t"] < (CFG["min_t"] if group == "forex" else CFG["min_t_gold"]):
                why.append(f"t={tot['t']:.1f} is below {CFG['min_t'] if group == 'forex' else CFG['min_t_gold']}")
            if not tr["n"] or tr["avg"] <= 0:
                why.append("not positive in the first 60% of the history")
            if not te["n"] or te["avg"] <= 0:
                why.append("not positive in the last 40% (the untouched part)")
            elif te["t"] < CFG["min_t_test"]:
                why.append(f"the last 40% is too weak (t={te['t']:.1f}, need {CFG['min_t_test']})")
            if not st["n"] or st["avg"] <= 0:
                why.append("turns negative if spreads are 50% wider")
            if group == "forex" and pos_pairs < CFG["min_pairs_positive"]:
                why.append(f"positive on only {pos_pairs} of {pairs} pairs")
            f = lambda x: None if x["avg"] is None else round(x["avg"], 3)
            rows.append({"strategy": name, "label": LABELS[name], "rr": rr, "group": group, "symbols": len(keys),
                         "n": tot["n"], "avg_r": f(tot), "t": None if tot["t"] is None else round(tot["t"], 2),
                         "hit": round(hits / tot["n"], 3) if tot["n"] else None,
                         "train_avg": f(tr), "train_n": tr["n"], "test_avg": f(te), "test_n": te["n"],
                         "stress_avg": f(st), "pairs_positive": pos_pairs, "pairs": pairs,
                         "passed": not why, "why_not": why})
    rows.sort(key=lambda r: (not r["passed"], -(r["avg_r"] if r["avg_r"] is not None else -9)))
    return rows


# ---------------------------------------------------------------- running it inside the app (admin only)
_state = {"running": False, "done": 0, "total": 0, "current": None, "error": None}
_task = None


def start() -> bool:
    global _task
    if _state["running"]:
        return False
    _state.update(running=True, done=0, total=len(REAL_MARKETS), current=None, error=None)
    _task = asyncio.create_task(run())
    return True


async def run():
    from . import deriv
    _state.update(running=True, done=0, total=len(REAL_MARKETS), current=None, error=None)
    run_id = None
    try:
        with closing(dbx.connect("fxresearch", SCHEMA)) as con:
            run_id = con.insert("INSERT INTO fx_research(started_at,status) VALUES(?,?)", (int(time.time()), "running"))
            con.commit()
        per_symbol, history = {}, {}
        for key, (code, name) in REAL_MARKETS.items():
            _state["current"] = name
            try:
                counts = dict(CFG["history"])
                tfs = ("D1", "H4", "H1")
                if key == "xauusd":
                    tfs, counts["M15"] = ("D1", "H4", "H1", "M15"), CFG["gold_m15"]
                d = (await deriv.fetch_multi([code], tfs, counts)).get(code)
                if d:
                    history[key] = {tf: {"bars": len(c), "days": round((c[-1]["epoch"] - c[0]["epoch"]) / 86400)} for tf, c in d.items() if c}
                    per_symbol[key] = await asyncio.to_thread(research_symbol, key, d)
            except Exception as e:
                print(f"[research] {key} failed: {type(e).__name__}: {str(e)[:150]}", flush=True)
            _state["done"] += 1
            await asyncio.sleep(0.5)
        rows = evaluate(per_symbol)
        result = {"rows": rows, "tested": len(rows), "passed": sum(1 for r in rows if r["passed"]), "history": history,
                  "config": {k: v for k, v in CFG.items() if k != "history"},
                  "spreads": {k: spread_of(k) for k in REAL_MARKETS}}
        with closing(dbx.connect("fxresearch", SCHEMA)) as con:
            con.execute("UPDATE fx_research SET status='done', finished_at=?, result=? WHERE id=?",
                        (int(time.time()), json.dumps(result), run_id))
            con.commit()
    except Exception as e:
        _state["error"] = f"{type(e).__name__}: {str(e)[:150]}"
        print(f"[research] failed: {_state['error']}", flush=True)
        if run_id:
            try:
                with closing(dbx.connect("fxresearch", SCHEMA)) as con:
                    con.execute("UPDATE fx_research SET status='failed', finished_at=? WHERE id=?", (int(time.time()), run_id))
                    con.commit()
            except Exception:
                pass
    finally:
        _state["running"] = False


def summary() -> dict:
    with closing(dbx.connect("fxresearch", SCHEMA)) as con:
        r = con.execute("SELECT id, started_at, finished_at, status, result FROM fx_research WHERE status='done' "
                        "ORDER BY id DESC LIMIT 1").fetchone()
    out = {"state": dict(_state), "run": None, "result": None}
    if r:
        out["run"] = {"id": r["id"], "started_at": r["started_at"], "finished_at": r["finished_at"]}
        out["result"] = json.loads(r["result"]) if r["result"] else None
    return out
