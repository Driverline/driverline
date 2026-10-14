"""Admin-only research jobs and their API: probe Deriv's data, ingest and validate history, run the baselines.
Jobs run one at a time in the background. Nothing here is visible to members."""
import asyncio
import json
import re
import time
from contextlib import closing

import websockets
from starlette.exceptions import HTTPException
from starlette.responses import JSONResponse
from starlette.routing import Route

from .. import auth, deriv
from ..config import DERIV_WS_URL
from . import features as FT
from . import research as RS
from . import store, validate as VAL

TF = 300      # the stored timeframe: M5
# Symbols we expect to exist. The codes are NOT confirmed: the probe checks them against Deriv's live symbol list, and
# also finds anything on the list whose name contains "Step" or "Range Break".
EXPECTED = {"stpRNG": "Step Index 100", "stpRNG2": "Step Index 200", "stpRNG3": "Step Index 300",
            "stpRNG4": "Step Index 400", "stpRNG5": "Step Index 500", "RB100": "Range Break 100", "RB200": "Range Break 200"}
NAME_RE = re.compile(r"step|range\s*break", re.I)
SYMBOL_RE = re.compile(r"^[A-Za-z0-9_]{2,24}$")
_state = {"running": False, "job": None, "detail": "", "error": None, "started": None}


# ---------------------------------------------------------------- tick helpers (pure, tested)
def tick_sample_stats(times: list, prices: list) -> dict:
    n = len(prices)
    if n < 3:
        return {"n": n}
    gaps = sorted(b - a for a, b in zip(times, times[1:]))
    diffs = [round(abs(b - a), 8) for a, b in zip(prices, prices[1:])]
    flat = sum(1 for d in diffs if d == 0)
    counts = {}
    for d in diffs:
        counts[d] = counts.get(d, 0) + 1
    top = sorted(counts.items(), key=lambda kv: -kv[1])[:3]
    return {"n": n, "span_seconds": times[-1] - times[0], "median_gap_seconds": gaps[len(gaps) // 2],
            "flat_share": flat / len(diffs), "top_step_sizes": [{"size": k, "share": v / len(diffs)} for k, v in top]}


def bar_tick_stats(times: list, prices: list, bar: int = TF) -> dict:
    """Per-bar tick statistics: {bar start: {n, up, down, max_up, max_down, changes}}. The first and last bars of the
    sample are dropped because they are usually partial."""
    groups = {}
    for t, p in zip(times, prices):
        groups.setdefault(t - t % bar, []).append(p)
    keys = sorted(groups)
    out = {}
    for k in keys[1:-1]:
        ps = groups[k]
        up = down = max_up = max_down = ru = rd = changes = 0
        last_sign = 0
        for a, b in zip(ps, ps[1:]):
            s = (b > a) - (b < a)
            if s > 0:
                up += 1; ru += 1; rd = 0
            elif s < 0:
                down += 1; rd += 1; ru = 0
            else:
                ru = rd = 0
            max_up, max_down = max(max_up, ru), max(max_down, rd)
            if s and last_sign and s != last_sign:
                changes += 1
            if s:
                last_sign = s
        out[k] = {"n": len(ps), "up": up, "down": down, "max_up": max_up, "max_down": max_down, "changes": changes}
    return out


async def fetch_ticks(ws, symbol: str, start: int, end: int) -> tuple:
    """All ticks between start and end, oldest first, paged backwards from `end`."""
    got = {}
    cur = end
    while True:
        resp = await deriv._request(ws, {"ticks_history": symbol, "end": cur, "count": 5000, "style": "ticks"}, expect="history")
        h = resp["history"]
        times, prices = h.get("times") or [], h.get("prices") or []
        before = len(got)
        for t, p in zip(times, prices):
            if t >= start:
                got[int(t)] = float(p)
        if not times or len(got) == before or min(times) <= start:
            break
        cur = int(min(times)) - 1
        await asyncio.sleep(0.15)
    ts = sorted(got)
    return ts, [got[t] for t in ts]


# ---------------------------------------------------------------- jobs
async def _active(ws) -> dict:
    resp = await deriv._request(ws, {"active_symbols": "brief"}, expect="active_symbols")
    out = {}
    for s in resp["active_symbols"]:
        code = s.get("symbol") or s.get("underlying_symbol")
        if code:
            out[code] = s
    return out


async def probe_job():
    result = {"checked_at": int(time.time()), "symbols": {}}
    async with websockets.connect(DERIV_WS_URL, open_timeout=15, ping_interval=20) as ws:
        active = await _active(ws)
        targets = dict(EXPECTED)
        for code, s in active.items():
            nm = s.get("display_name") or s.get("symbol_name") or ""
            if NAME_RE.search(nm) and code not in targets:
                targets[code] = nm + " (found on Deriv's list)"
        for code, label in targets.items():
            s = active.get(code)
            rec = {"label": label, "expected_code": code in EXPECTED, "listed": s is not None,
                   "display": (s or {}).get("display_name") or (s or {}).get("symbol_name"), "market": (s or {}).get("market"),
                   "submarket": (s or {}).get("submarket"), "depth": {}, "ticks": None}
            _state["detail"] = f"probing {code}"
            if s is not None:
                for tf, gran, count in (("D1", 86400, 5000), ("H1", 3600, 8000), ("M5", 300, 30000)):
                    try:
                        c = sorted(await deriv._candles(ws, code, gran, count), key=lambda x: int(x["epoch"]))
                        rec["depth"][tf] = {"bars": len(c), "first": int(c[0]["epoch"]) if c else None,
                                            "last": int(c[-1]["epoch"]) if c else None,
                                            "days": round((int(c[-1]["epoch"]) - int(c[0]["epoch"])) / 86400, 1) if c else 0,
                                            "capped_by_request": len(c) >= count}
                    except Exception as e:
                        rec["depth"][tf] = {"error": str(e)[:120]}
                try:
                    r = await deriv._request(ws, {"ticks_history": code, "count": 5000, "end": "latest", "style": "ticks"}, expect="history")
                    rec["ticks"] = tick_sample_stats([int(t) for t in r["history"]["times"]], [float(p) for p in r["history"]["prices"]])
                except Exception as e:
                    rec["ticks"] = {"error": str(e)[:120]}
            result["symbols"][code] = rec
            await asyncio.sleep(0.3)
    rid = store.start_run("probe", params={"expected": list(EXPECTED)})
    store.finish_run(rid, result)


def _validate_and_register(symbol: str) -> dict:
    rows = store.load_candles(symbol, TF)
    report = VAL.validate_candles(rows, TF)
    sha = VAL.dataset_hash(rows)
    did = store.register_dataset(symbol, TF, rows, report, sha, report["status"])
    return {"dataset_id": did, "status": report["status"], "reasons": report["reasons"], "rows": len(rows)}


async def ingest_job(symbol: str, days: int, tick_days: int):
    async with websockets.connect(DERIV_WS_URL, open_timeout=15, ping_interval=20) as ws:
        if symbol not in await _active(ws):
            raise RuntimeError(f"{symbol} is not on Deriv's active symbol list. Run the probe to see the real codes.")
        _state["detail"] = f"downloading {symbol} candles"
        candles = sorted(await deriv._candles(ws, symbol, TF, days * 288), key=lambda x: int(x["epoch"]))
        rows = [{"epoch": int(c["epoch"]), "open": float(c["open"]), "high": float(c["high"]), "low": float(c["low"]),
                 "close": float(c["close"])} for c in candles]
        if rows and rows[-1]["epoch"] + TF > time.time():
            rows.pop()                                       # the newest candle is still forming
        await asyncio.to_thread(store.upsert_candles, symbol, TF, rows)
        if tick_days:
            _state["detail"] = f"downloading {symbol} ticks ({tick_days} days)"
            end = int(time.time())
            times, prices = await fetch_ticks(ws, symbol, end - tick_days * 86400, end)
            stats = await asyncio.to_thread(bar_tick_stats, times, prices)
            await asyncio.to_thread(store.update_tick_stats, symbol, TF, stats)
    _state["detail"] = f"validating {symbol}"
    res = await asyncio.to_thread(_validate_and_register, symbol)
    rid = store.start_run("ingest", symbol, res["dataset_id"], params={"days": days, "tick_days": tick_days})
    store.finish_run(rid, res)


async def validate_job(symbol: str):
    res = await asyncio.to_thread(_validate_and_register, symbol)
    rid = store.start_run("validate", symbol, res["dataset_id"])
    store.finish_run(rid, res)


def _baselines_sync(symbol: str):
    ds = store.latest_dataset(symbol, TF)
    if not ds:
        raise RuntimeError("There is no validated dataset for this symbol. Ingest it first.")
    if ds["status"] == "invalid":
        raise RuntimeError("The latest dataset failed validation, so it cannot be used. See the reasons and re-ingest.")
    bars = store.load_candles(symbol, TF)
    if VAL.dataset_hash(bars) != ds["sha256"]:
        raise RuntimeError("The stored candles changed since they were validated. Validate again first.")
    fh, ch = FT.feature_spec_hash(), store.file_hash(RS.__file__)
    for b in RS.BASELINES:
        store.register_model("baseline", f"baseline:{b}", 1, {"description": RS.BASELINE_DEFINITIONS[b], **{
            "specs": RS.SPECS, "horizon": RS.HORIZON, "cost_atr": RS.COST_ATR, "shares": RS.SHARES}}, fh, ch)
    run_id = store.start_run("baseline", symbol, ds["id"], split=RS.split_bounds(len(bars)), params=RS.config())
    try:
        res = RS.run_baselines(bars, f"{symbol}|{ds['sha256']}",
                               lambda label, seg, params, metrics: store.log_trial(run_id, label, seg, params, metrics))
        res["dataset"] = {"id": ds["id"], "sha256": ds["sha256"], "status": ds["status"]}
        res["feature_spec_hash"] = fh
        store.finish_run(run_id, res)
    except Exception:
        store.finish_run(run_id, {"error": "run failed"}, ok=False)
        raise


async def baselines_job(symbol: str):
    await asyncio.to_thread(_baselines_sync, symbol)


# ---------------------------------------------------------------- job runner and API
def _start(coro, job: str, detail: str) -> bool:
    if _state["running"]:
        coro.close()
        return False
    _state.update(running=True, job=job, detail=detail, error=None, started=int(time.time()))

    async def wrapper():
        try:
            await coro
        except Exception as e:
            _state["error"] = f"{type(e).__name__}: {str(e)[:200]}"
            print(f"[engine] {job} failed: {_state['error']}", flush=True)
        finally:
            _state["running"] = False
            _state["detail"] = ""

    asyncio.create_task(wrapper())
    return True


def _symbol(d: dict) -> str:
    s = str(d.get("symbol", "")).strip()
    if not SYMBOL_RE.match(s):
        raise HTTPException(422, "Enter a Deriv symbol code, for example stpRNG")
    return s


async def api_probe(request):
    auth.require_admin(request)
    return JSONResponse({"started": _start(probe_job(), "probe", "starting")})


async def api_ingest(request):
    auth.require_admin(request)
    d = await auth._json(request)
    sym = _symbol(d)
    try:
        days, tick_days = int(d.get("days", 180)), int(d.get("tick_days", 0))
    except (TypeError, ValueError):
        raise HTTPException(422, "days must be a whole number")
    if not 7 <= days <= 365 or not 0 <= tick_days <= 30:
        raise HTTPException(422, "Use 7 to 365 days of candles and 0 to 30 days of ticks")
    return JSONResponse({"started": _start(ingest_job(sym, days, tick_days), "ingest", sym)})


async def api_validate(request):
    auth.require_admin(request)
    sym = _symbol(await auth._json(request))
    return JSONResponse({"started": _start(validate_job(sym), "validate", sym)})


async def api_baselines(request):
    auth.require_admin(request)
    sym = _symbol(await auth._json(request))
    return JSONResponse({"started": _start(baselines_job(sym), "baselines", sym)})


def _condense(res: dict) -> list:
    """Short lines per baseline at the 1.5R target: sample, average result and interval for development and validation."""
    out = []
    for r in res.get("rows", []):
        if r["rr"] != 1.5:
            continue
        f = lambda m: "no trades" if not m.get("n") else (
            f"n={m['n']} avg {m['avg_r']:+.3f}R" + (f" [{m['ci95'][0]:+.3f}, {m['ci95'][1]:+.3f}]" if m.get("ci95") else ""))
        out.append({"baseline": r["baseline"], "dev": f(r["dev"]), "val": f(r["val"])})
    return out


async def api_status(request):
    auth.require_admin(request)

    def read():
        with closing(store.db()) as con:
            probe = con.execute("SELECT result, finished_at FROM research_runs WHERE kind='probe' AND status='done' "
                                "ORDER BY id DESC LIMIT 1").fetchone()
            ds = con.execute("SELECT d.* FROM research_datasets d JOIN (SELECT symbol, tf, MAX(id) mid FROM research_datasets "
                             "GROUP BY symbol, tf) m ON m.mid=d.id ORDER BY d.symbol").fetchall()
            runs = con.execute("SELECT id, symbol, result, finished_at FROM research_runs WHERE kind='baseline' "
                               "AND status='done' ORDER BY id DESC LIMIT 4").fetchall()
        return probe, ds, runs

    probe, ds, runs = await asyncio.to_thread(read)
    datasets = []
    for d in ds:
        rep = json.loads(d["report"])
        datasets.append({"id": d["id"], "symbol": d["symbol"], "status": d["status"], "rows": d["row_count"],
                         "first": d["first_epoch"], "last": d["last_epoch"], "reasons": rep.get("reasons", []),
                         "tick_stats_share": rep.get("tick_stats_share"), "sha": d["sha256"][:12]})
    out_runs = []
    for r in runs:
        res = json.loads(r["result"])
        out_runs.append({"id": r["id"], "symbol": r["symbol"], "finished_at": r["finished_at"], "notable": res.get("notable", []),
                         "lines": _condense(res), "bounds": res.get("bounds")})
    return JSONResponse({"state": dict(_state), "probe": json.loads(probe["result"]) if probe else None,
                         "datasets": datasets, "runs": out_runs})


routes = [
    Route("/api/admin/engine", api_status, methods=["GET"]),
    Route("/api/admin/engine/probe", api_probe, methods=["POST"]),
    Route("/api/admin/engine/ingest", api_ingest, methods=["POST"]),
    Route("/api/admin/engine/validate", api_validate, methods=["POST"]),
    Route("/api/admin/engine/baselines", api_baselines, methods=["POST"]),
]
