"""Smart Analysis Timing ("Opportunity Radar").

Ranks the supported Synthetic Indices by how worthwhile it is to spend ONE analysis credit on them right now.
It never calls Claude and never consumes a credit. The score is not a win probability.

Opportunity Score (0-100) = weighted blend of whichever of these are available:
  ready  current setup readiness, from the same candle indicators the app already computes
  time   how this hour compares with the symbol's other hours, ONLY if a statistically meaningful
         time-of-day effect is found in its candle history (otherwise skipped and reported honestly)
  hist   how often Claude actually found a setup (TRADE) in similar conditions, from the analyses
         this app has logged; its weight ramps up as data accumulates
Missing components are left out and the rest are re-weighted, so nothing is invented while data is thin.
"""
import asyncio
import bisect
import copy
import json
import math
import os
import time
from contextlib import closing
from datetime import datetime, timedelta, timezone

from . import auth, dbx
from .config import INSTRUMENTS
from .indicators import classify_structure, ema, macd, range_stats, rsi, swing_points

DEFAULT_CONFIG = {
    "weights": {"ready": 0.55, "time": 0.20, "hist": 0.25},
    "readiness_weights": {"trend": 0.35, "efficiency": 0.20, "volatility": 0.20, "momentum": 0.10, "range": 0.15},
    "tf_blend": {"M15": 0.6, "H1": 0.4},
    "actionable_threshold": 62,      # combined readiness counted as "actionable" in the candle-history proxy
    "min_recommend": 65,             # below this nothing is recommended
    "labels": {"high": 80, "worth": 65, "mixed": 50},
    "time_effect": {"min_days": 20, "alpha": 0.01, "min_eta2": 0.02, "favourable_pct": 0.7},
    "hist": {"min_n": 30, "ramp_n": 60, "prior_n": 20},
    "spike_penalty": 0.25, "spike_mult": 4.0,
    "scan_seconds": 300, "history_refresh_hours": 24, "history_m15_bars": 5000, "history_h1_bars": 1300,
    "preferred_min_trades": 3, "preferred_rank_bonus": 1.5,
}


def _merge(base: dict, extra: dict) -> dict:
    for k, v in extra.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _merge(base[k], v)
        else:
            base[k] = v
    return base


def load_config() -> dict:
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    try:
        return _merge(cfg, json.loads(os.environ.get("DRIVERLINE_RADAR_CONFIG", "{}")))
    except ValueError:
        return cfg


CFG = load_config()

SCHEMA = """
    CREATE TABLE IF NOT EXISTS analyses(id {ID}, created_at BIGINT NOT NULL, instrument TEXT NOT NULL,
        hour_utc INTEGER NOT NULL, status TEXT NOT NULL, direction TEXT, setup_type TEXT, no_trade_reason TEXT,
        confidence DOUBLE PRECISION, readiness DOUBLE PRECISION, regime TEXT, features TEXT);
    CREATE INDEX IF NOT EXISTS ix_analyses_inst ON analyses(instrument, created_at);
    CREATE TABLE IF NOT EXISTS radar_hist(instrument TEXT PRIMARY KEY, stats TEXT NOT NULL, updated_at BIGINT NOT NULL)"""


def db():
    return dbx.connect("radar", SCHEMA)


def family(key: str) -> str:
    if key.startswith("step"):
        return "step"
    if key.startswith(("boom", "crash")):
        return "spike"
    if key.startswith("jump"):
        return "jump"
    return "volatility"


# ---------------------------------------------------------------- per-bar features (reuses indicators.py)
def series_features(candles: list, spiky: bool = False, cfg: dict = None) -> list:
    """Indicator state at every bar, built from the same primitives analyze_timeframe uses."""
    cfg = cfg or CFG
    n = len(candles)
    if n < 60:
        return []
    closes = [c["close"] for c in candles]
    e20, e50, e200 = ema(closes, 20), ema(closes, 50), ema(closes, 200)
    _, _, mh = macd(closes)
    rs = rsi(closes)
    tr = [candles[0]["high"] - candles[0]["low"]]
    for p, c in zip(candles, candles[1:]):
        tr.append(max(c["high"] - c["low"], abs(c["high"] - p["close"]), abs(c["low"] - p["close"])))
    med = sorted(tr)[n // 2] or 1e-9
    trc = [min(t, 3 * med) for t in tr] if spiky else tr   # spikes must not distort the volatility baseline
    pt = [0.0]
    for t in trc:
        pt.append(pt[-1] + t)
    atr = [(pt[i + 1] - pt[max(0, i - 13)]) / min(14, i + 1) for i in range(n)]
    pa = [0.0]
    for a in atr:
        pa.append(pa[-1] + a)
    highs, lows = swing_points(candles)
    hp = lp = 0
    hc, lc = [], []
    last_spike, out = None, []
    for i in range(n):
        while hp < len(highs) and highs[hp][0] + 3 <= i:
            hc.append(highs[hp]); hp += 1
        while lp < len(lows) and lows[lp][0] + 3 <= i:
            lc.append(lows[lp]); lp += 1
        if tr[i] > cfg["spike_mult"] * med:
            last_spike = i
        mean100 = (pa[i + 1] - pa[max(0, i - 99)]) / min(100, i + 1)
        rg = range_stats(candles[i - 29:i + 1]) if i >= 29 else None
        out.append({
            "epoch": candles[i]["epoch"], "structure": classify_structure(hc, lc),
            "ema_stack": "bullish" if e20[i] > e50[i] > e200[i] else "bearish" if e20[i] < e50[i] < e200[i] else "mixed",
            "price_vs_ema50": "above" if closes[i] > e50[i] else "below",
            "rsi": rs[i - 14] if i >= 14 and i - 14 < len(rs) else None,
            "macd_hist": mh[i], "macd_prev": mh[i - 1] if i else mh[i],
            "er": rg["efficiency"] if rg else 0.0, "consolidating": bool(rg and rg["is_consolidating"]),
            "vol_ratio": (atr[i] / mean100) if mean100 > 0 else None,
            "spike_recent": last_spike is not None and i - last_spike <= 2,
        })
    return out


def _vol_score(r):
    if r is None:
        return None
    if r < 0.6:
        return 0.1
    if r < 0.9:
        return 0.1 + (r - 0.6) / 0.3 * 0.5
    if r < 1.6:
        return 0.6 + (r - 0.9) / 0.7 * 0.4
    if r < 2.5:
        return 1.0 - (r - 1.6) / 0.9 * 0.4
    return 0.4


def tf_readiness(f: dict, cfg: dict = None) -> float:
    """0-1: how clean and usable the picture looks on one timeframe (not a win probability)."""
    cfg = cfg or CFG
    st = f["structure"]
    direction = 1 if st.startswith("bullish") else -1 if st.startswith("bearish") else 0
    s_struct = 1.0 if direction else (0.4 if st.startswith("mixed") else 0.1)
    stack = {"bullish": 1, "bearish": -1}.get(f["ema_stack"], 0)
    s_stack = 1.0 if (direction and stack == direction) else (0.0 if (direction and stack == -direction) else 0.5)
    rsi_v = f.get("rsi")
    rising = f["macd_hist"] > f["macd_prev"]
    if rsi_v is not None and (rsi_v < 25 or rsi_v > 75):
        s_mom = 0.4
    elif direction == 0:
        s_mom = 0.5
    else:
        s_mom = 1.0 if (rising and direction == 1) or (not rising and direction == -1) else 0.6
    parts = {"trend": 0.5 * s_struct + 0.5 * s_stack, "efficiency": min(1.0, f["er"] / 0.5),
             "volatility": _vol_score(f.get("vol_ratio")), "momentum": s_mom,
             "range": 0.2 if f["consolidating"] else 1.0}
    w = cfg["readiness_weights"]
    tot = sum(w[k] for k, v in parts.items() if v is not None)
    score = sum(w[k] * v for k, v in parts.items() if v is not None) / tot
    if f.get("spike_recent"):
        score *= 1 - cfg["spike_penalty"]
    return score


def combined_readiness(f15: dict, f1h: dict = None, cfg: dict = None) -> float:
    """0-100, M15 and H1 blended with the configured weights."""
    cfg = cfg or CFG
    b, num, den = cfg["tf_blend"], 0.0, 0.0
    for tf, f in (("M15", f15), ("H1", f1h)):
        if f is not None:
            num += b[tf] * tf_readiness(f, cfg)
            den += b[tf]
    return 100.0 * num / den if den else 0.0


def regime_label(f15: dict, f1h: dict) -> str:
    def d(f):
        s = f["structure"]
        return 1 if s.startswith("bullish") else -1 if s.startswith("bearish") else 0
    if f15["consolidating"]:
        return "range"
    if d(f15) and d(f15) == d(f1h):
        vr = f15.get("vol_ratio") or 1.0
        return "trend_expanding" if vr >= 1.2 else "trend_quiet" if vr <= 0.8 else "trend_normal"
    return "mixed"


# ---------------------------------------------------------------- statistics (no SciPy needed)
def _betacf(a, b, x):
    qab, qap, qam, c = a + b, a + 1, a - 1, 1.0
    d = 1 - qab * x / qap
    d = 1e-300 if abs(d) < 1e-300 else d
    d = 1 / d
    h = d
    for m in range(1, 201):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1 + aa * d; d = 1e-300 if abs(d) < 1e-300 else d
        c = 1 + aa / c; c = 1e-300 if abs(c) < 1e-300 else c
        d = 1 / d; h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1 + aa * d; d = 1e-300 if abs(d) < 1e-300 else d
        c = 1 + aa / c; c = 1e-300 if abs(c) < 1e-300 else c
        d = 1 / d; de = d * c; h *= de
        if abs(de - 1) < 3e-12:
            break
    return h


def _betainc(a, b, x):
    if x <= 0:
        return 0.0
    if x >= 1:
        return 1.0
    bt = math.exp(math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b) + a * math.log(x) + b * math.log(1 - x))
    if x < (a + 1) / (a + b + 2):
        return bt * _betacf(a, b, x) / a
    return 1 - bt * _betacf(b, a, 1 - x) / b


def f_sf(F: float, d1: int, d2: int) -> float:
    """P(F' >= F) for an F distribution with (d1, d2) degrees of freedom."""
    return _betainc(d2 / 2, d1 / 2, d2 / (d2 + d1 * F)) if F > 0 else 1.0


def time_of_day_test(obs_by_hour: dict, cfg: dict = None) -> dict:
    """One-way ANOVA of (day, hour) mean readiness across the 24 hours. Each hour-group holds one value
    per DAY, so the observations are roughly independent (consecutive candles are not)."""
    cfg = cfg or CFG
    groups = {h: v for h, v in obs_by_hour.items() if len(v) >= 5}
    days = max((len(v) for v in groups.values()), default=0)
    if len(groups) < 12 or days < cfg["time_effect"]["min_days"]:
        return {"tested": False, "significant": False, "reason": "Still building historical data", "days": days}
    allv = [x for v in groups.values() for x in v]
    N, k = len(allv), len(groups)
    grand = sum(allv) / N
    ssb = sum(len(v) * (sum(v) / len(v) - grand) ** 2 for v in groups.values())
    ssw = sum((x - sum(v) / len(v)) ** 2 for v in groups.values() for x in v)
    if ssw <= 0 or N <= k:
        return {"tested": False, "significant": False, "reason": "Not enough variation", "days": days}
    F = (ssb / (k - 1)) / (ssw / (N - k))
    p, eta2 = f_sf(F, k - 1, N - k), ssb / (ssb + ssw)
    te = cfg["time_effect"]
    return {"tested": True, "significant": bool(p < te["alpha"] and eta2 >= te["min_eta2"]),
            "p": round(p, 6), "eta2": round(eta2, 4), "F": round(F, 2), "days": days}


def history_stats(m15: list, h1: list, spiky: bool = False, cfg: dict = None) -> dict:
    """Candle-history behaviour by hour of day (UTC), using the same readiness function as the live score."""
    cfg = cfg or CFG
    f15, f1h = series_features(m15, spiky, cfg), series_features(h1, spiky, cfg)
    if len(f15) < 400 or len(f1h) < 200:
        return {"ok": False, "reason": "Not enough candles"}
    h1_epochs = [f["epoch"] for f in f1h]
    per_day_hour: dict = {}
    bars = actionable = 0
    hours = {h: {"sum": 0.0, "act": 0, "n": 0} for h in range(24)}
    for i in range(200, len(f15)):
        j = bisect.bisect_right(h1_epochs, f15[i]["epoch"] - 3600) - 1   # last COMPLETED H1 bar
        if j < 150:
            continue
        r = combined_readiness(f15[i], f1h[j], cfg)
        t = datetime.fromtimestamp(f15[i]["epoch"], timezone.utc)
        key = (t.date(), t.hour)
        per_day_hour.setdefault(key, []).append(r)
        hh = hours[t.hour]
        hh["sum"] += r; hh["n"] += 1
        a = r >= cfg["actionable_threshold"]
        hh["act"] += a; actionable += a; bars += 1
    if not bars:
        return {"ok": False, "reason": "Not enough aligned candles"}
    obs = {h: [] for h in range(24)}
    for (d, h), v in per_day_hour.items():
        if len(v) >= 3:
            obs[h].append(sum(v) / len(v))
    test = time_of_day_test(obs, cfg)
    hours_out = {}
    for h in range(24):
        hh = hours[h]
        hours_out[str(h)] = {"n_days": len(obs[h]), "mean": round(hh["sum"] / hh["n"], 2) if hh["n"] else None,
                             "rate": round(hh["act"] / hh["n"], 4) if hh["n"] else None}
    ranked = sorted((h for h in range(24) if hours_out[str(h)]["mean"] is not None),
                    key=lambda h: hours_out[str(h)]["mean"], reverse=True)
    test["best_hours"], test["worst_hours"] = ranked[:3], ranked[-3:]
    span = (m15[-1]["epoch"] - m15[0]["epoch"]) / 86400
    return {"ok": True, "span_days": round(span, 1), "bars": bars, "base_rate": round(actionable / bars, 4),
            "hours": hours_out, "time_effect": test, "updated_at": int(time.time())}


def hour_percentile(stats: dict, hour: int):
    means = {int(h): v["mean"] for h, v in stats.get("hours", {}).items() if v.get("mean") is not None and v.get("n_days", 0) >= 5}
    if hour not in means or len(means) < 12:
        return None
    return sum(1 for v in means.values() if v <= means[hour]) / len(means)


def minutes_to_favourable(stats: dict, now: datetime, cfg: dict = None):
    cfg = cfg or CFG
    if not (stats.get("ok") and stats.get("time_effect", {}).get("significant")):
        return None
    for add in range(1, 25):
        start = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=add)
        p = hour_percentile(stats, start.hour)
        if p is not None and p >= cfg["time_effect"]["favourable_pct"]:
            return int((start - now).total_seconds() // 60)
    return None


# ---------------------------------------------------------------- stored data
_hist: dict = {}
_hist_loaded = False
_scan: dict = {"ts": 0.0, "items": {}}
_actual_cache: dict = {"ts": 0.0, "data": None}
_scan_lock = None


def _load_hist():
    global _hist_loaded
    if _hist_loaded:
        return
    with closing(db()) as con:
        for r in con.execute("SELECT instrument, stats FROM radar_hist").fetchall():
            _hist[r["instrument"]] = json.loads(r["stats"])
    _hist_loaded = True


def _save_hist(key: str, stats: dict):
    _hist[key] = stats
    with closing(db()) as con:
        con.execute("INSERT INTO radar_hist(instrument,stats,updated_at) VALUES(?,?,?) "
                    "ON CONFLICT(instrument) DO UPDATE SET stats=excluded.stats, updated_at=excluded.updated_at",
                    (key, json.dumps(stats), int(time.time())))
        con.commit()


def record_analysis(key: str, card: dict):
    """Log one real Claude analysis (called after a fresh generation). Never raises."""
    try:
        item = _scan["items"].get(key)
        fresh = item and time.time() - item["ts"] < 900
        with closing(db()) as con:
            con.insert(
                "INSERT INTO analyses(created_at,instrument,hour_utc,status,direction,setup_type,no_trade_reason,"
                "confidence,readiness,regime,features) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (int(time.time()), key, datetime.now(timezone.utc).hour, str(card.get("status")),
                 card.get("direction"), card.get("setup_type"), card.get("no_trade_reason"),
                 float(card["confidence"]) if isinstance(card.get("confidence"), (int, float)) else None,
                 item["readiness"] if fresh else None, item["regime"] if fresh else None,
                 json.dumps({"m15": item["f15"], "h1": item["f1h"]}) if fresh else None))
            con.commit()
        _actual_cache["ts"] = 0.0
    except Exception as e:
        print(f"[radar] could not log analysis: {e}", flush=True)


def _actual() -> dict:
    """Claude's real results: {instrument: [n, trades]}, {(instrument, regime): [n, trades]}, global [n, trades]."""
    if _actual_cache["data"] is not None and time.time() - _actual_cache["ts"] < 300:
        return _actual_cache["data"]
    inst, reg, g = {}, {}, [0, 0]
    with closing(db()) as con:
        rows = con.execute("SELECT instrument, regime, status FROM analyses WHERE created_at>? ORDER BY id DESC LIMIT 50000",
                           (int(time.time()) - 120 * 86400,)).fetchall()
    for r in rows:
        t = 1 if r["status"] == "TRADE" else 0
        for d, k in ((inst, r["instrument"]), (reg, (r["instrument"], r["regime"]))):
            e = d.setdefault(k, [0, 0]); e[0] += 1; e[1] += t
        g[0] += 1; g[1] += t
    _actual_cache.update(ts=time.time(), data={"inst": inst, "reg": reg, "global": g})
    return _actual_cache["data"]


# ---------------------------------------------------------------- scanning (Deriv data only, no Claude)
async def refresh_scan():
    from . import deriv
    data = await deriv.fetch_multi([v[0] for v in INSTRUMENTS.values()], ("M15", "H1"), 300)
    items, now = {}, time.time()
    for key, (code, name) in INSTRUMENTS.items():
        d = data.get(code)
        if not d or len(d.get("M15", [])) < 100 or len(d.get("H1", [])) < 100:
            continue
        spiky = family(key) in ("spike", "jump")
        a, b = series_features(d["M15"], spiky), series_features(d["H1"], spiky)
        if not a or not b:
            continue
        f15, f1h = a[-1], b[-1]
        items[key] = {"ts": now, "f15": f15, "f1h": f1h, "readiness": round(combined_readiness(f15, f1h), 2),
                      "regime": regime_label(f15, f1h), "price": d["M15"][-1]["close"]}
    if items:
        _scan.update(ts=now, items=items)


async def ensure_fresh(max_age: float = None):
    global _scan_lock
    if _scan_lock is None:
        _scan_lock = asyncio.Lock()
    max_age = max_age or CFG["scan_seconds"] * 1.5
    if time.time() - _scan["ts"] < max_age:
        return
    async with _scan_lock:
        if time.time() - _scan["ts"] >= max_age:
            await asyncio.wait_for(refresh_scan(), timeout=40)


async def refresh_history(force: bool = False):
    from . import deriv
    _load_hist()
    for key, (code, name) in INSTRUMENTS.items():
        old = _hist.get(key)
        if not force and old and old.get("span_days", 0) >= 30 and time.time() - old.get("updated_at", 0) < CFG["history_refresh_hours"] * 3600:
            continue
        try:
            d = (await deriv.fetch_multi([code], ("M15", "H1"),
                                         {"M15": CFG["history_m15_bars"], "H1": CFG["history_h1_bars"]})).get(code)
            if d:
                stats = await asyncio.to_thread(history_stats, d["M15"], d["H1"], family(key) in ("spike", "jump"))
                _save_hist(key, stats)
        except Exception as e:
            print(f"[radar] history for {key} failed: {e}", flush=True)
        await asyncio.sleep(1)


async def background_loops():
    async def scans():
        await asyncio.sleep(5)
        while True:
            try:
                await refresh_scan()
            except Exception as e:
                print(f"[radar] scan failed: {e}", flush=True)
            await asyncio.sleep(CFG["scan_seconds"])

    async def histories():
        await asyncio.sleep(30)
        while True:
            try:
                await refresh_history()
            except Exception as e:
                print(f"[radar] history refresh failed: {e}", flush=True)
            await asyncio.sleep(6 * 3600)

    return [asyncio.create_task(scans()), asyncio.create_task(histories())]


# ---------------------------------------------------------------- scoring
def _label(score: float) -> str:
    L = CFG["labels"]
    return ("High analysis opportunity" if score >= L["high"] else "Worth analysing" if score >= L["worth"]
            else "Mixed conditions" if score >= L["mixed"] else "Lower analysis opportunity")


def _condition(f15: dict, f1h: dict) -> str:
    def d(f):
        s = f["structure"]
        return 1 if s.startswith("bullish") else -1 if s.startswith("bearish") else 0
    if f15["consolidating"]:
        base = "Ranging / compression"
    elif d(f15) and d(f15) == d(f1h):
        base = "Aligned trend structure"
    elif d(f15) or d(f1h):
        base = "Developing trend structure"
    else:
        base = "Mixed conditions"
    vr = f15.get("vol_ratio")
    if vr is not None and vr >= 1.3:
        base += " + volatility expansion"
    elif vr is not None and vr <= 0.7:
        base += " + low volatility"
    return base + (" (recent spike)" if f15.get("spike_recent") else "")


def score_instrument(key: str, now: datetime) -> dict | None:
    item = _scan["items"].get(key)
    if not item:
        return None
    f15, f1h = item["f15"], item["f1h"]
    st = _hist.get(key) if _hist.get(key, {}).get("ok") else None
    act = _actual()
    comps, reasons, notes = {}, [], []
    comps["ready"] = (item["readiness"] / 100.0, CFG["weights"]["ready"])

    # time-of-day: only when the candle history shows a meaningful effect
    time_note = "Still building historical data for this symbol."
    if st:
        te = st["time_effect"]
        if te.get("significant"):
            p = hour_percentile(st, now.hour)
            if p is not None:
                comps["time"] = (p, CFG["weights"]["time"])
                if p >= CFG["time_effect"]["favourable_pct"]:
                    reasons.append("Historically favourable period for this symbol")
                    time_note = "This hour is historically more favourable for this symbol."
                else:
                    time_note = "This hour is not one of the historically favourable periods for this symbol."
        elif te.get("tested"):
            time_note = "No meaningful historical time advantage detected."
        else:
            time_note = "Still building historical data for this symbol."

    # Claude's real results in similar conditions
    n_i, t_i = act["inst"].get(key, [0, 0])
    gn, gt = act["global"]
    h = CFG["hist"]
    if n_i >= h["min_n"] and gn >= h["min_n"] and gt > 0:
        g_rate = gt / gn
        inst_rate = (t_i + h["prior_n"] * g_rate) / (n_i + h["prior_n"])
        n_r, t_r = act["reg"].get((key, item["regime"]), [0, 0])
        reg_rate = (t_r + h["prior_n"] * inst_rate) / (n_r + h["prior_n"])
        comps["hist"] = (max(0.0, min(1.0, 0.5 * reg_rate / g_rate)), CFG["weights"]["hist"] * min(1.0, n_i / h["ramp_n"]))
        if reg_rate >= 1.25 * g_rate:
            reasons.append("Lower historical No-Trade rate in similar conditions")
        elif reg_rate <= 0.75 * g_rate:
            reasons.append("Higher historical No-Trade rate in similar conditions")
        notes.append(f"Based on {n_i} past analyses for this symbol.")
    else:
        notes.append(f"Still building analysis history for this symbol ({n_i}/{h['min_n']}).")

    tot = sum(w for _, w in comps.values())
    score = 100.0 * sum(c * w for c, w in comps.values()) / tot if tot else 0.0

    vr = f15.get("vol_ratio")
    if vr is not None and vr >= 1.2:
        reasons.insert(0, "Current volatility is above its recent range")
    elif vr is not None and vr <= 0.75:
        reasons.insert(0, "Volatility is below its recent range")
    sd = lambda f: 1 if f["structure"].startswith("bullish") else -1 if f["structure"].startswith("bearish") else 0
    if f15["consolidating"]:
        reasons.append("Price is ranging with no confirmed breakout")
    elif sd(f15) and sd(f15) == sd(f1h):
        reasons.append("Market structure is relatively clear on both M15 and H1")
    elif sd(f15) or sd(f1h):
        reasons.append("Structure is clear on only one timeframe")
    else:
        reasons.append("Timeframes are mixed")
    if (f15.get("rsi") or 50) < 25 or (f15.get("rsi") or 50) > 75:
        reasons.append("Momentum looks stretched (RSI at an extreme)")
    if f15.get("spike_recent"):
        reasons.append("A spike occurred recently; conditions may still be settling")
    return {"key": key, "name": INSTRUMENTS[key][1], "score": round(score), "label": _label(score),
            "condition": _condition(f15, f1h), "reasons": reasons[:4], "time_note": time_note, "notes": notes,
            "regime": item["regime"], "price": item["price"], "parts": {k: round(c, 3) for k, (c, w) in comps.items()}}


def _preferred(uid: int) -> list:
    from . import journal
    try:
        with closing(journal.db()) as con:
            rows = con.execute("SELECT instrument, COUNT(*) AS c FROM trades WHERE user_id=? GROUP BY instrument "
                               "ORDER BY c DESC LIMIT 3", (uid,)).fetchall()
        return [r["instrument"] for r in rows if r["c"] >= CFG["preferred_min_trades"] and r["instrument"] in INSTRUMENTS]
    except Exception:
        return []


def _fmt_minutes(m: int) -> str:
    return f"approximately {round(m / 5) * 5 or 5} minutes" if m < 120 else f"approximately {round(m / 60)} hours"


async def get_radar(user: dict, fresh_fn=lambda k: False) -> dict:
    _load_hist()
    try:
        await ensure_fresh()
    except Exception as e:
        print(f"[radar] refresh failed: {e}", flush=True)
    if not _scan["items"] or time.time() - _scan["ts"] > 1800:
        return {"status": "unavailable", "message": "Smart Analysis Timing is not available right now. You can still analyse manually."}
    now = datetime.now(timezone.utc)
    pref = _preferred(user["id"])
    items = [s for k in INSTRUMENTS if (s := score_instrument(k, now))]
    for s in items:
        s["preferred"] = s["key"] in pref
        s["cached"] = bool(fresh_fn(s["key"]))
    items.sort(key=lambda s: -(s["score"] + (CFG["preferred_rank_bonus"] if s["preferred"] else 0)))
    used, limit = auth.quota(user)
    built = sum(1 for k in INSTRUMENTS if _hist.get(k, {}).get("ok"))
    out = {"status": "ok", "generated_at": int(_scan["ts"]), "credits": {"used": used, "limit": limit},
           "preferred": pref, "notes": [] if built == len(INSTRUMENTS) else
           [f"Still building historical data for {len(INSTRUMENTS) - built} of {len(INSTRUMENTS)} symbols."]}
    top = items[0] if items else None
    if top and top["score"] >= CFG["min_recommend"]:
        out["best"], out["others"] = top, items[1:6]
        if pref and max((s["score"] for s in items if s["preferred"]), default=0) < CFG["min_recommend"]:
            out["preferred_note"] = "Your usual symbols (" + ", ".join(INSTRUMENTS[k][1] for k in pref) + ") currently show weaker conditions."
    else:
        wait = {"title": "No Strong Analysis Opportunity Right Now",
                "message": "Your analysis credits are limited, and current conditions don't justify spending one yet.",
                "next_window": None}
        best_m = None
        for k in INSTRUMENTS:
            st = _hist.get(k)
            if st and st.get("ok") and st["time_effect"].get("significant"):
                m = minutes_to_favourable(st, now)
                if m is not None and (best_m is None or m < best_m[0]):
                    best_m = (m, k)
        if best_m:
            wait["next_window"] = {"key": best_m[1], "name": INSTRUMENTS[best_m[1]][1], "text": _fmt_minutes(best_m[0]),
                                   "note": "A historical expectation only, not a prediction."}
        out["wait"], out["others"] = wait, items[:6]
        if pref:
            out["preferred_note"] = "Your usual symbols (" + ", ".join(INSTRUMENTS[k][1] for k in pref) + ") are not showing strong conditions."
    return out


def admin_summary() -> dict:
    _load_hist()
    act = _actual()
    rows = []
    for key, (code, name) in INSTRUMENTS.items():
        st = _hist.get(key) if _hist.get(key, {}).get("ok") else None
        n, t = act["inst"].get(key, [0, 0])
        it = _scan["items"].get(key)
        rows.append({"key": key, "name": name,
                     "history": ({"span_days": st["span_days"], "base_rate": st["base_rate"], "time_effect": st["time_effect"]} if st else None),
                     "analyses": {"n": n, "trade": t, "rate": round(t / n, 3) if n else None},
                     "now": {"readiness": it["readiness"], "regime": it["regime"]} if it else None})
    return {"rows": rows, "global": {"n": act["global"][0], "trade": act["global"][1]},
            "scan_age": int(time.time() - _scan["ts"]) if _scan["ts"] else None, "config": CFG}
