"""Signal setups and their backtest.

Four rule-based setups are tested on three styles (Scalp on M5, Day on M15, Swing on H1) with a higher-timeframe
trend filter. Every trade is simulated the way it would happen live: the signal is read at a candle's close, the
entry is the NEXT candle's open, the stop is a multiple of ATR, and trades on a symbol never overlap. If the stop
and the target are both touched inside one candle, the stop is assumed to hit first (the cautious choice).
Costs are an assumed fraction of ATR per trade. The output is meant to tell us honestly which setups fire often
enough and which, if any, beat their costs, with the sample size always shown."""
import asyncio
import bisect
import json
import math
import os
import time
from contextlib import closing

from . import dbx, radar
from .config import INSTRUMENTS
from .indicators import ema, rsi

STYLES = {
    "scalp": {"tf": "M5", "ctx": "M15", "horizon": 24},
    "day": {"tf": "M15", "ctx": "H1", "horizon": 32},
    "swing": {"tf": "H1", "ctx": "H4", "horizon": 48},
}
PERIOD = {"M5": 300, "M15": 900, "H1": 3600, "H4": 14400}
SETUPS = ("ema_pullback", "squeeze_breakout", "range_breakout", "range_reversal")
RRS = (1.0, 1.5, 2.0)
BT_CFG = {"stop_atr": 1.5, "cost_atr": 0.05, "min_n": 60, "history": {"M5": 5000, "M15": 5000, "H1": 5000, "H4": 1300}}
try:
    radar._merge(BT_CFG, json.loads(os.environ.get("DRIVERLINE_SIGNAL_CONFIG", "{}")))
except ValueError:
    pass

SCHEMA = """
    CREATE TABLE IF NOT EXISTS backtest_runs(id {ID}, started_at BIGINT NOT NULL, finished_at BIGINT,
        status TEXT NOT NULL, params TEXT);
    CREATE TABLE IF NOT EXISTS backtest_results(run_id BIGINT NOT NULL, instrument TEXT NOT NULL, style TEXT NOT NULL,
        setup TEXT NOT NULL, rr DOUBLE PRECISION NOT NULL, n INTEGER NOT NULL, per_day DOUBLE PRECISION,
        hit_rate DOUBLE PRECISION, avg_r DOUBLE PRECISION, avg_r_h1 DOUBLE PRECISION, avg_r_h2 DOUBLE PRECISION,
        t_stat DOUBLE PRECISION, pf DOUBLE PRECISION, be_rate DOUBLE PRECISION, span_days DOUBLE PRECISION,
        max_loss_streak INTEGER);
    CREATE INDEX IF NOT EXISTS ix_bt_run ON backtest_results(run_id)"""


def _migrate(con):
    con.add_column("backtest_results", "sd_r", "DOUBLE PRECISION")


def db():
    return dbx.connect("signals", SCHEMA, _migrate)


# ---------------------------------------------------------------- indicator series
def build(candles: list, spiky: bool = False) -> dict:
    n = len(candles)
    o, h, l, c = ([x[k] for x in candles] for k in ("open", "high", "low", "close"))
    tr = [h[0] - l[0]] + [max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1])) for i in range(1, n)]
    if spiky:  # spikes must not blow up the ATR used for stops
        med = sorted(tr)[n // 2] or 1e-9
        tr = [min(t, 3 * med) for t in tr]
    pt = [0.0]
    for t in tr:
        pt.append(pt[-1] + t)
    atr = [(pt[i + 1] - pt[max(0, i - 13)]) / min(14, i + 1) for i in range(n)]
    pc, pc2 = [0.0], [0.0]
    for x in c:
        pc.append(pc[-1] + x)
        pc2.append(pc2[-1] + x * x)
    mid, up, lo, wid = [], [], [], []
    for i in range(n):
        a = max(0, i - 19)
        k = i + 1 - a
        m = (pc[i + 1] - pc[a]) / k
        sd = math.sqrt(max(0.0, (pc2[i + 1] - pc2[a]) / k - m * m))
        mid.append(m); up.append(m + 2 * sd); lo.append(m - 2 * sd); wid.append(4 * sd / m if m else 0.0)
    pd = [0.0]
    for i in range(1, n):
        pd.append(pd[-1] + abs(c[i] - c[i - 1]))
    er = [abs(c[i] - c[i - 30]) / (pd[i] - pd[i - 30]) if i >= 30 and pd[i] > pd[i - 30] else 0.0 for i in range(n)]
    rs = rsi(c)
    return {"n": n, "epoch": [x["epoch"] for x in candles], "o": o, "h": h, "l": l, "c": c, "e20": ema(c, 20),
            "e50": ema(c, 50), "atr": atr, "mid": mid, "up": up, "lo": lo, "wid": wid, "er": er,
            "rsi": [rs[i - 14] if 14 <= i and i - 14 < len(rs) else 50.0 for i in range(n)]}


def ctx_direction(F: dict, X: dict, p_sig: int, p_ctx: int) -> list:
    """Trend of the higher timeframe (EMA20 vs EMA50) using only candles that were already complete."""
    out, ep = [], X["epoch"]
    for t in F["epoch"]:
        j = bisect.bisect_right(ep, t + p_sig - p_ctx) - 1
        out.append(0 if j < 60 else (1 if X["e20"][j] > X["e50"][j] else -1))
    return out


# ---------------------------------------------------------------- the four setups: a list of (bar index, +1 buy / -1 sell)
def find_signals(setup: str, F: dict, ctx: list) -> list:
    n, out = F["n"], []
    o, h, l, c, e20, e50, atr = F["o"], F["h"], F["l"], F["c"], F["e20"], F["e50"], F["atr"]
    for i in range(120, n - 1):
        d = 0
        if setup == "ema_pullback":
            body_ok = (h[i] - l[i]) > 0 and abs(c[i] - o[i]) >= 0.4 * (h[i] - l[i])
            if ctx[i] > 0 and e20[i] > e50[i] > e50[i - 5] and body_ok and c[i] > o[i] and c[i] > e20[i] \
                    and any(l[k] <= e20[k] for k in (i - 2, i - 1, i)):
                d = 1
            elif ctx[i] < 0 and e20[i] < e50[i] < e50[i - 5] and body_ok and c[i] < o[i] and c[i] < e20[i] \
                    and any(h[k] >= e20[k] for k in (i - 2, i - 1, i)):
                d = -1
        elif setup == "squeeze_breakout":
            lim = sorted(F["wid"][i - 100:i])[19]
            squeezed = F["wid"][i - 1] <= lim or F["wid"][i - 2] <= lim
            big = (h[i] - l[i]) >= 1.1 * atr[i]
            if squeezed and big and c[i] > F["up"][i] and e50[i] > e50[i - 5]:
                d = 1
            elif squeezed and big and c[i] < F["lo"][i] and e50[i] < e50[i - 5]:
                d = -1
        elif setup == "range_breakout":
            if c[i] > max(h[i - 20:i]) and ctx[i] > 0 and e20[i] > e50[i]:
                d = 1
            elif c[i] < min(l[i - 20:i]) and ctx[i] < 0 and e20[i] < e50[i]:
                d = -1
        elif setup == "range_reversal":
            if F["er"][i] <= 0.35:
                if F["rsi"][i - 1] < 33 and l[i - 1] <= F["lo"][i - 1] and c[i] > o[i] and c[i] > c[i - 1]:
                    d = 1
                elif F["rsi"][i - 1] > 67 and h[i - 1] >= F["up"][i - 1] and c[i] < o[i] and c[i] < c[i - 1]:
                    d = -1
        if d:
            out.append((i, d))
    return out


# ---------------------------------------------------------------- trade simulation
def simulate(F: dict, signals: list, rr: float, horizon: int, cfg: dict = None) -> list:
    """Sequential, non-overlapping trades. Returns [(net R, hit target?)] in time order."""
    cfg = cfg or BT_CFG
    o, h, l, c, atr = F["o"], F["h"], F["l"], F["c"], F["atr"]
    cost_r = cfg["cost_atr"] / cfg["stop_atr"]
    trades, free_from = [], 0
    for i, d in signals:
        if i + 1 < free_from or i + 1 >= F["n"]:
            continue
        entry, sd = o[i + 1], cfg["stop_atr"] * atr[i]
        if sd <= 0:
            continue
        stop, target = entry - d * sd, entry + d * rr * sd
        end, res = min(F["n"] - 1, i + horizon), None
        for j in range(i + 1, end + 1):
            hit_stop = (l[j] <= stop) if d > 0 else (h[j] >= stop)
            hit_tgt = (h[j] >= target) if d > 0 else (l[j] <= target)
            if hit_stop:                       # cautious: a candle touching both counts as a loss
                res, free_from = (-1.0 - cost_r, False), j + 1
                break
            if hit_tgt:
                res, free_from = (rr - cost_r, True), j + 1
                break
        if res is None:                        # time ran out: close at the last candle
            res, free_from = (d * (c[end] - entry) / sd - cost_r, False), end + 1
        trades.append(res)
    return trades


def trade_stats(trades: list, span_days: float, rr: float, cfg: dict = None) -> dict | None:
    cfg = cfg or BT_CFG
    n = len(trades)
    if n < 5:
        return None
    r = [t[0] for t in trades]
    mean = sum(r) / n
    sd = math.sqrt(sum((x - mean) ** 2 for x in r) / (n - 1)) if n > 1 else 0.0
    half = n // 2
    wins, losses = sum(x for x in r if x > 0), -sum(x for x in r if x < 0)
    streak = best = 0
    for x in r:
        streak = streak + 1 if x < 0 else 0
        best = max(best, streak)
    cost_r = cfg["cost_atr"] / cfg["stop_atr"]
    return {"n": n, "per_day": n / span_days if span_days else None, "hit_rate": sum(1 for t in trades if t[1]) / n,
            "avg_r": mean, "avg_r_h1": sum(r[:half]) / half, "avg_r_h2": sum(r[half:]) / (n - half),
            "t_stat": mean / (sd / math.sqrt(n)) if sd > 0 else 0.0, "pf": wins / losses if losses else None,
            "be_rate": (1 + cost_r) / (1 + rr), "max_loss_streak": best, "sd_r": sd}


def backtest_symbol(data: dict, spiky: bool = False, cfg: dict = None) -> list:
    cfg = cfg or BT_CFG
    rows = []
    for style, sp in STYLES.items():
        if sp["tf"] not in data or sp["ctx"] not in data or len(data[sp["tf"]]) < 400:
            continue
        F, X = build(data[sp["tf"]], spiky), build(data[sp["ctx"]], spiky)
        ctx = ctx_direction(F, X, PERIOD[sp["tf"]], PERIOD[sp["ctx"]])
        span = (F["epoch"][-1] - F["epoch"][0]) / 86400
        for setup in SETUPS:
            sigs = find_signals(setup, F, ctx)
            for rr in RRS:
                st = trade_stats(simulate(F, sigs, rr, sp["horizon"], cfg), span, rr, cfg)
                if st:
                    rows.append({"style": style, "setup": setup, "rr": rr, "span_days": span, **st})
    return rows


# ---------------------------------------------------------------- running it inside the app
_state = {"running": False, "done": 0, "total": 0, "run_id": None, "current": None, "error": None}
_task = None


def start_backtest() -> bool:
    global _task
    if _state["running"]:
        return False
    _state.update(running=True, done=0, total=len(INSTRUMENTS), current=None, error=None)
    _task = asyncio.create_task(run_backtest())
    return True


async def run_backtest():
    from . import deriv
    _state.update(running=True, done=0, total=len(INSTRUMENTS), current=None, error=None)
    try:
        with closing(db()) as con:
            run_id = con.insert("INSERT INTO backtest_runs(started_at,status,params) VALUES(?,?,?)",
                                (int(time.time()), "running", json.dumps(BT_CFG)))
            con.commit()
        _state["run_id"] = run_id
        for key, (code, name) in INSTRUMENTS.items():
            _state["current"] = name
            try:
                d = (await deriv.fetch_multi([code], ("M5", "M15", "H1", "H4"), BT_CFG["history"])).get(code)
                if d:
                    rows = await asyncio.to_thread(backtest_symbol, d, radar.family(key) in ("spike", "jump"))
                    with closing(db()) as con:
                        for r in rows:
                            con.execute(
                                "INSERT INTO backtest_results(run_id,instrument,style,setup,rr,n,per_day,hit_rate,avg_r,"
                                "avg_r_h1,avg_r_h2,t_stat,pf,be_rate,span_days,max_loss_streak,sd_r) "
                                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                                (run_id, key, r["style"], r["setup"], r["rr"], r["n"], r["per_day"], r["hit_rate"],
                                 r["avg_r"], r["avg_r_h1"], r["avg_r_h2"], r["t_stat"], r["pf"], r["be_rate"],
                                 r["span_days"], r["max_loss_streak"], r["sd_r"]))
                        con.commit()
            except Exception as e:
                print(f"[backtest] {key} failed: {type(e).__name__}: {str(e)[:150]}", flush=True)
            _state["done"] += 1
            await asyncio.sleep(0.5)
        with closing(db()) as con:
            con.execute("UPDATE backtest_runs SET status='done', finished_at=? WHERE id=?", (int(time.time()), run_id))
            con.commit()
    except Exception as e:
        _state["error"] = f"{type(e).__name__}: {str(e)[:150]}"
        print(f"[backtest] failed: {_state['error']}", flush=True)
    finally:
        _state["running"] = False


def summary() -> dict:
    with closing(db()) as con:
        run = con.execute("SELECT id, started_at, finished_at, status FROM backtest_runs ORDER BY id DESC LIMIT 1").fetchone()
        rows = con.execute("SELECT * FROM backtest_results WHERE run_id=?", (run["id"],)).fetchall() if run else []
    out = {"state": dict(_state), "run": dict(run) if run else None, "pooled": [], "best": [], "rows": len(rows)}
    if not rows:
        return out
    groups, min_n = {}, BT_CFG["min_n"]
    for r in rows:
        groups.setdefault((r["style"], r["setup"], r["rr"]), []).append(r)
    for (style, setup, rr), g in groups.items():
        tot = sum(x["n"] for x in g)
        sds = [x["sd_r"] for x in g]
        t_pool = None
        if all(s is not None for s in sds):
            se = math.sqrt(sum(x["n"] * x["sd_r"] ** 2 for x in g)) / tot
            t_pool = (sum(x["avg_r"] * x["n"] for x in g) / tot) / se if se > 0 else None
        out["pooled"].append({
            "t_pooled": t_pool, "style": style, "setup": setup, "rr": rr, "symbols": len(g), "n": tot,
            "per_day_all": sum(x["per_day"] or 0 for x in g),
            "hit_rate": sum(x["hit_rate"] * x["n"] for x in g) / tot, "be_rate": g[0]["be_rate"],
            "avg_r": sum(x["avg_r"] * x["n"] for x in g) / tot,
            "stable_symbols": sum(1 for x in g if x["n"] >= min_n and x["avg_r_h1"] > 0 and x["avg_r_h2"] > 0)})
    out["pooled"].sort(key=lambda x: -x["avg_r"])
    big = [r for r in rows if r["n"] >= min_n]
    out["best"] = [{**{k: r[k] for k in ("instrument", "style", "setup", "rr", "n", "per_day", "hit_rate", "be_rate",
                                         "avg_r", "avg_r_h1", "avg_r_h2", "t_stat")},
                    "name": INSTRUMENTS.get(r["instrument"], ("", r["instrument"]))[1]}
                   for r in sorted(big, key=lambda r: -r["t_stat"])[:12]]
    out["share_t_over_2"] = round(sum(1 for r in big if r["t_stat"] > 2) / len(big), 3) if big else None
    out["tested_combinations"] = len(big)
    return out
