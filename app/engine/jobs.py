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
from . import candidates as CA, diagnostics as DG, signals_service as SS, store, structure as ST, validate as VAL

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
    """All ticks between start and end, oldest first, paged backwards from `end`. Returns (times, prices, info), where
    info says how far back Deriv actually served and why the download stopped."""
    got, cur, pages, reason = {}, end, 0, "reached_start"
    while True:
        try:
            resp = await deriv._request(ws, {"ticks_history": symbol, "end": cur, "count": 5000, "style": "ticks"}, expect="history")
        except Exception as e:
            reason = f"error: {str(e)[:80]}"
            break
        pages += 1
        h = resp["history"]
        times, prices = h.get("times") or [], h.get("prices") or []
        before = len(got)
        for t, p in zip(times, prices):
            if t >= start:
                got[int(t)] = float(p)
        if not times:
            reason = "deriv_returned_no_older_ticks"
            break
        if min(times) <= start:
            break
        if len(got) == before:
            reason = "no_progress"
            break
        cur = int(min(times)) - 1
        await asyncio.sleep(0.15)
    ts = sorted(got)
    info = {"pages": pages, "ticks": len(ts), "oldest": ts[0] if ts else None, "newest": ts[-1] if ts else None,
            "hours": round((ts[-1] - ts[0]) / 3600, 1) if len(ts) > 1 else 0, "stopped": reason}
    return ts, [got[t] for t in ts], info


async def tick_depth(ws, symbol: str, max_pages: int = 40) -> dict:
    """How far back Deriv serves ticks for this symbol (pages of 5,000)."""
    cur, oldest, newest, pages, total, reason = "latest", None, None, 0, 0, "page_limit"
    while pages < max_pages:
        try:
            r = await deriv._request(ws, {"ticks_history": symbol, "end": cur, "count": 5000, "style": "ticks"}, expect="history")
        except Exception as e:
            reason = f"error: {str(e)[:80]}"
            break
        times = r["history"].get("times") or []
        if not times:
            reason = "no_more_data"
            break
        pages += 1
        total += len(times)
        lo, hi = int(min(times)), int(max(times))
        newest = hi if newest is None else newest
        if oldest is not None and lo >= oldest:
            reason = "no_progress"
            break
        oldest, cur = lo, lo - 1
        await asyncio.sleep(0.1)
    return {"pages": pages, "ticks": total, "hours": round((newest - oldest) / 3600, 1) if oldest and newest else 0,
            "stopped": reason}


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
                try:
                    rec["tick_depth"] = await tick_depth(ws, code)
                except Exception as e:
                    rec["tick_depth"] = {"error": str(e)[:120]}
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
            times, prices, tick_info = await fetch_ticks(ws, symbol, end - tick_days * 86400, end)
            stats = await asyncio.to_thread(bar_tick_stats, times, prices)
            await asyncio.to_thread(store.update_tick_stats, symbol, TF, stats)
    _state["detail"] = f"validating {symbol}"
    res = await asyncio.to_thread(_validate_and_register, symbol)
    if tick_days:
        res["ticks"] = tick_info
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


def _structure_sync(symbol: str, n_surrogates: int):
    ds = store.latest_dataset(symbol, TF)
    if not ds:
        raise RuntimeError("There is no validated dataset for this symbol. Ingest it first.")
    if ds["status"] == "invalid":
        raise RuntimeError("The latest dataset failed validation, so it cannot be used.")
    bars = store.load_candles(symbol, TF)
    if VAL.dataset_hash(bars) != ds["sha256"]:
        raise RuntimeError("The stored candles changed since they were validated. Validate again first.")
    fam = ST.family_of(symbol)
    fh, ch = FT.feature_spec_hash(), store.file_hash(ST.__file__)
    for hyp in ST.FAMILY_TESTS[fam]:        # pre-registration: the hypotheses are on record before any test runs
        store.register_model("hypothesis", f"hypothesis:{fam}:{hyp}", 1, {
            "description": ST.HYPOTHESES[hyp], "family": fam, "alpha": ST.ALPHA, "segments": ["dev", "val"],
            "horizons": ST.EVENT_H.get(hyp), "multiplicity": "Holm within each segment",
            "replication_rule": "significant after adjustment in development AND validation, same direction"}, fh, ch)
    bounds = RS.split_bounds(len(bars))
    run_id = store.start_run("structure", symbol, ds["id"], split={k: list(v) for k, v in bounds.items() if k != "test"},
                             params={"n_surrogates": n_surrogates, "family": fam, "dataset_sha": ds["sha256"]})
    try:
        res = ST.run_structure(bars, symbol, n_surrogates,
                               lambda label, seg, params, metrics: store.log_trial(run_id, label, seg, params, metrics),
                               lambda i, n: _state.update(detail=f"surrogate {i} of {n}"))
        res["dataset"] = {"id": ds["id"], "sha256": ds["sha256"], "status": ds["status"]}
        store.finish_run(run_id, res)
    except Exception:
        store.finish_run(run_id, {"error": "run failed"}, ok=False)
        raise


async def structure_job(symbol: str, n_surrogates: int):
    await asyncio.to_thread(_structure_sync, symbol, n_surrogates)


def _diagnostics_sync(symbol: str, n_surrogates: int):
    ds = store.latest_dataset(symbol, TF)
    if not ds:
        raise RuntimeError("There is no validated dataset for this symbol. Ingest it first.")
    if ds["status"] == "invalid":
        raise RuntimeError("The latest dataset failed validation, so it cannot be used.")
    bars = store.load_candles(symbol, TF)
    if VAL.dataset_hash(bars) != ds["sha256"]:
        raise RuntimeError("The stored candles changed since they were validated. Validate again first.")
    bounds = RS.split_bounds(len(bars))
    run_id = store.start_run("diagnostics", symbol, ds["id"], split={k: list(v) for k, v in bounds.items() if k != "test"},
                             params={"n_surrogates": n_surrogates, "dataset_sha": ds["sha256"], "stop_atr": DG.STOP_ATR,
                                     "delays": DG.DELAYS, "cost_multiples": DG.COST_MULTS, "blocks": DG.BLOCKS,
                                     "rule": "executable candidate only if the 95% interval is above zero in development and "
                                             "validation with a normal entry, one bar late, and one bar late with doubled costs"})
    try:
        res = DG.run_diagnostics(bars, symbol, n_surrogates, lambda i, n: _state.update(detail=f"block surrogate {i} of {n}"))
        res["dataset"] = {"id": ds["id"], "sha256": ds["sha256"], "status": ds["status"]}
        for r in res["delay_costs"]:
            store.log_trial(run_id, f"{r['baseline']}|delay{r['delay']}|cost{r['cost_mult']}x", "dev+val",
                            {k: r[k] for k in ("baseline", "delay", "cost_mult")}, {"dev": r["dev"], "val": r["val"]})
        store.finish_run(run_id, res)
    except Exception:
        store.finish_run(run_id, {"error": "run failed"}, ok=False)
        raise


async def diagnostics_job(symbol: str, n_surrogates: int):
    await asyncio.to_thread(_diagnostics_sync, symbol, n_surrogates)


async def tick_structure_job(symbol: str):
    async with websockets.connect(DERIV_WS_URL, open_timeout=15, ping_interval=20) as ws:
        if symbol not in await _active(ws):
            raise RuntimeError(f"{symbol} is not on Deriv's active symbol list.")
        _state["detail"] = f"downloading {symbol} ticks (last 24 hours)"
        end = int(time.time())
        times, prices, info = await fetch_ticks(ws, symbol, end - 86400, end)
    _state["detail"] = "analysing ticks"
    res = await asyncio.to_thread(DG.ticks_analysis, times, prices)
    res.update(symbol=symbol, download=info)
    rid = store.start_run("ticks", symbol, params={"window_hours": 24})
    store.finish_run(rid, res)


def _candidates_sync(symbol: str):
    ds = store.latest_dataset(symbol, TF)
    if not ds:
        raise RuntimeError("There is no validated dataset for this symbol. Ingest it first.")
    if ds["status"] == "invalid":
        raise RuntimeError("The latest dataset failed validation, so it cannot be used.")
    bars = store.load_candles(symbol, TF)
    if VAL.dataset_hash(bars) != ds["sha256"]:
        raise RuntimeError("The stored candles changed since they were validated. Validate again first.")
    run_id = store.start_run("candidates", symbol, ds["id"], split={k: list(v) for k, v in RS.split_bounds(len(bars)).items() if k != "test"},
                             params={"grid": CA.GRID, "cost_multiple": CA.COST_MULT, "dataset_sha": ds["sha256"],
                                     "advance_gate": "95% interval above zero in development AND validation, late entry, doubled costs, "
                                                     f"at least {CA.MIN_VAL_TRADES} validation trades",
                                     "test_rule": f"one shot; at least {CA.MIN_TEST_TRADES} trades, interval above zero after doubled "
                                                  "costs, beats the random baseline with the same delay and costs, positive in two of "
                                                  "three time-thirds"})
    try:
        res = CA.search(bars, symbol, lambda label, seg, params, metrics: store.log_trial(run_id, label, seg, params, metrics))
        res["dataset"] = {"id": ds["id"], "sha256": ds["sha256"], "status": ds["status"]}
        if res["advance"]:
            sel = res["selected"]
            res["model_id"] = store.register_model("range_break", f"candidate:{sel['name']}", 1, CA.definition_for(symbol, sel),
                                                   FT.feature_spec_hash(), store.file_hash(CA.__file__))
        store.finish_run(run_id, res)
    except Exception:
        store.finish_run(run_id, {"error": "run failed"}, ok=False)
        raise


async def candidates_job(symbol: str):
    await asyncio.to_thread(_candidates_sync, symbol)


def _candidate_test_sync(run_id: int):
    run = store.get_run(run_id)
    if not run or run["kind"] != "candidates" or run["status"] != "done":
        raise RuntimeError("That candidate search does not exist or has not finished.")
    res = json.loads(run["result"])
    if res.get("test_result") is not None or run["test_unlocked_at"]:
        raise RuntimeError("The untouched test for this search has already been used. It can never be run twice.")
    bars = store.load_candles(run["symbol"], TF)
    result = CA.evaluate_test(bars, res, run_id)           # takes the one-shot lock only after it has a result
    res["test_result"] = result
    store.set_run_result(run_id, res)
    CA.apply_outcome(res["model_id"], result)
    rid = store.start_run("candidate_test", run["symbol"], run["dataset_id"], model_version_id=res["model_id"], params={"search_run": run_id})
    store.finish_run(rid, result)


async def candidate_test_job(run_id: int):
    await asyncio.to_thread(_candidate_test_sync, run_id)


def condense_candidates(res: dict) -> dict:
    rows = sorted([r for r in res.get("candidates", []) if r.get("worst_lower") is not None], key=lambda r: -r["worst_lower"])[:6]
    f = lambda m: "no trades" if not m.get("n") else f"n={m['n']} {m['avg_r']:+.3f} [{m['ci95'][0]:+.3f}, {m['ci95'][1]:+.3f}]" if m.get("ci95") else f"n={m['n']}"
    return {"verdict": res.get("verdict"), "advance": res.get("advance"), "selected": (res.get("selected") or {}).get("name"),
            "model_id": res.get("model_id"), "grid_size": res.get("grid_size"), "test_result": res.get("test_result"),
            "top": [{"name": r["name"], "dev": f(r["dev"]), "val": f(r["val"]), "passes": r["passes_gate"]} for r in rows]}


def condense_diag(res: dict) -> dict:
    """What the Admin page shows: the verdict, delay lines, outcome breakdown, block z-scores and scales."""
    f = lambda m: "-" if not m or m.get("avg_r") is None else (
        f"{m['avg_r']:+.3f}" + ("*" if m.get("ci95") and m["ci95"][0] > 0 else ""))
    delay = []
    for b in DG.FOCUS:
        for r in res.get("delay_costs", []):
            if r["baseline"] == b and r["cost_mult"] == 1:
                delay.append({"baseline": b, "delay": r["delay"], "dev": f(r["dev"]), "val": f(r["val"])})
    dec = []
    for r in res.get("decomposition", []):
        if r["rr"] == 1.5 and r["baseline"] in DG.FOCUS:
            g = lambda m, k: "-" if m.get(f"{k}_share") is None else f"{m[f'{k}_share'] * 100:.0f}% ({'-' if m[f'{k}_avg'] is None else format(m[f'{k}_avg'], '+.2f')})"
            dec.append({"baseline": r["baseline"], "dev": {k: g(r["dev"], k) for k in ("target", "stop", "timeout")} | {"avg_bars": r["dev"].get("avg_bars")},
                        "val": {k: g(r["val"], k) for k in ("target", "stop", "timeout")} | {"avg_bars": r["val"].get("avg_bars")}})
    blk = [{"block": r["block"], "baseline": r["baseline"], "segment": r["segment"], "z": r["z"]} for r in res.get("block_surrogates", [])]
    sc = []
    for r in res.get("scales", []):
        sc.append({"minutes": r["minutes"], "dev": r["dev"].get("p_reverse"), "val": r["val"].get("p_reverse")})
    return {"verdict": res.get("verdict"), "executable": [v["baseline"] for v in res.get("verdicts", []) if v["executable_candidate"]],
            "checks": res.get("verdicts"), "delay": delay, "decomposition": dec, "blocks": blk, "scales": sc,
            "n_surrogates": res.get("n_surrogates")}


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


async def api_structure(request):
    auth.require_admin(request)
    d = await auth._json(request)
    sym = _symbol(d)
    try:
        n = int(d.get("n_surrogates", ST.SURROGATES_DEFAULT))
    except (TypeError, ValueError):
        raise HTTPException(422, "n_surrogates must be a whole number")
    if not 20 <= n <= 200:
        raise HTTPException(422, "Use 20 to 200 surrogates")
    return JSONResponse({"started": _start(structure_job(sym, n), "structure", sym)})


async def api_diagnostics(request):
    auth.require_admin(request)
    d = await auth._json(request)
    sym = _symbol(d)
    try:
        n = int(d.get("n_surrogates", DG.SURROGATES_DEFAULT))
    except (TypeError, ValueError):
        raise HTTPException(422, "n_surrogates must be a whole number")
    if not 20 <= n <= 100:
        raise HTTPException(422, "Use 20 to 100 surrogates per block size")
    return JSONResponse({"started": _start(diagnostics_job(sym, n), "diagnostics", sym)})


async def api_ticks(request):
    auth.require_admin(request)
    sym = _symbol(await auth._json(request))
    return JSONResponse({"started": _start(tick_structure_job(sym), "ticks", sym)})


async def api_candidates(request):
    auth.require_admin(request)
    sym = _symbol(await auth._json(request))
    return JSONResponse({"started": _start(candidates_job(sym), "candidates", sym)})


async def api_candidate_test(request):
    auth.require_admin(request)
    d = await auth._json(request)
    try:
        run_id = int(d.get("run_id"))
    except (TypeError, ValueError):
        raise HTTPException(422, "run_id must be a number")
    if d.get("confirm") is not True:
        raise HTTPException(422, "The untouched test can only be used once. Confirm that you want to use it now.")
    return JSONResponse({"started": _start(candidate_test_job(run_id), "candidate_test", str(run_id))})


async def api_shadow_scan(request):
    auth.require_admin(request)
    if _state["running"]:
        return JSONResponse({"started": False})
    res = await SS.scan_once()
    return JSONResponse({"started": True, "results": res})


async def api_model(request):
    u = auth.require_admin(request)
    d = await auth._json(request)
    try:
        mid = int(d.get("model_id"))
    except (TypeError, ValueError):
        raise HTTPException(422, "model_id must be a number")
    action, reason = d.get("action"), str(d.get("reason", "")).strip()[:200]
    m = store.get_model(mid)
    if not m:
        raise HTTPException(404, "No such model")
    if action == "suspend":
        store.set_model_status(mid, "suspended", f"manual by admin {u['id']}: {reason or 'no reason given'}")
    elif action == "resume":
        if m["status"] != "suspended":
            raise HTTPException(409, "Only a suspended model can be resumed")
        if not reason:
            raise HTTPException(422, "Say why it is safe to resume")
        # back to the status it earned: it passed the untouched test, so demo_validated (never approved by resuming)
        store.set_model_status(mid, "demo_validated", f"resumed by admin {u['id']}: {reason}")
    else:
        raise HTTPException(422, "action must be suspend or resume")
    return JSONResponse({"ok": True})


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
            structure = con.execute("SELECT id, symbol, result, finished_at FROM research_runs WHERE kind='structure' "
                                    "AND status='done' ORDER BY id DESC LIMIT 4").fetchall()
            diag = con.execute("SELECT id, symbol, result, finished_at FROM research_runs WHERE kind='diagnostics' "
                               "AND status='done' ORDER BY id DESC LIMIT 3").fetchall()
            tickruns = con.execute("SELECT id, symbol, result, finished_at FROM research_runs WHERE kind='ticks' "
                                   "AND status='done' ORDER BY id DESC LIMIT 3").fetchall()
            cands = con.execute("SELECT id, symbol, result, finished_at, test_unlocked_at FROM research_runs WHERE kind='candidates' "
                                "AND status='done' ORDER BY id DESC LIMIT 2").fetchall()
        return probe, ds, runs, structure, diag, tickruns, cands

    probe, ds, runs, structure, diag, tickruns, cands = await asyncio.to_thread(read)
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
    out_struct = []
    for r in structure:
        res = json.loads(r["result"])
        out_struct.append({"id": r["id"], "symbol": r["symbol"], "family": res.get("family"), "finished_at": r["finished_at"],
                           "verdict": res.get("verdict"), "tests_per_segment": res.get("tests_per_segment"),
                           "n_surrogates": res.get("n_surrogates"),
                           "replicated": [{k: x[k] for k in ("hypothesis", "name", "dev", "val", "dev_p_adj", "val_p_adj")}
                                          for x in res.get("replicated", [])[:10]]})
    out_diag = [{"id": r["id"], "symbol": r["symbol"], "finished_at": r["finished_at"], **condense_diag(json.loads(r["result"]))}
                for r in diag]
    out_ticks = []
    for r in tickruns:
        res = json.loads(r["result"])
        out_ticks.append({"id": r["id"], "symbol": r["symbol"], "finished_at": r["finished_at"], "ticks": res.get("ticks"),
                          "hours": res.get("hours"), "runs_z": (res.get("runs") or {}).get("z"), "acf": res.get("acf"),
                          "variance_ratio": {k: v.get("vr") for k, v in (res.get("variance_ratio") or {}).items()},
                          "scales": res.get("scales"), "note": res.get("note")})
    return JSONResponse({"state": dict(_state), "probe": json.loads(probe["result"]) if probe else None,
                         "datasets": datasets, "runs": out_runs, "structure": out_struct, "diagnostics": out_diag,
                         "tick_runs": out_ticks,
                         "candidates": [{"id": r["id"], "symbol": r["symbol"], "finished_at": r["finished_at"],
                                         "test_used": bool(r["test_unlocked_at"]), **condense_candidates(json.loads(r["result"]))} for r in cands],
                         "shadow": await asyncio.to_thread(SS.summary)})


routes = [
    Route("/api/admin/engine", api_status, methods=["GET"]),
    Route("/api/admin/engine/probe", api_probe, methods=["POST"]),
    Route("/api/admin/engine/ingest", api_ingest, methods=["POST"]),
    Route("/api/admin/engine/validate", api_validate, methods=["POST"]),
    Route("/api/admin/engine/baselines", api_baselines, methods=["POST"]),
    Route("/api/admin/engine/structure", api_structure, methods=["POST"]),
    Route("/api/admin/engine/diagnostics", api_diagnostics, methods=["POST"]),
    Route("/api/admin/engine/ticks", api_ticks, methods=["POST"]),
    Route("/api/admin/engine/candidates", api_candidates, methods=["POST"]),
    Route("/api/admin/engine/candidates/test", api_candidate_test, methods=["POST"]),
    Route("/api/admin/engine/shadow/scan", api_shadow_scan, methods=["POST"]),
    Route("/api/admin/engine/model", api_model, methods=["POST"]),
]
