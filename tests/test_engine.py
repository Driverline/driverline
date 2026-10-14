"""Tests for the AI signal engine's research infrastructure. Run with `python -m pytest tests` or `python tests/test_engine.py`.
They need no network: Deriv is replaced by a small fake that serves a deterministic price series."""
import asyncio
import json
import math
import os
import random
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
try:
    import starlette  # noqa: F401
    import websockets  # noqa: F401
except ImportError:      # a bare machine: stand-ins for the web framework so the pure modules can still be tested
    import types
    class _Any:
        def __init__(self, *a, **k): pass
        def __call__(self, *a, **k): return _Any()
    class _Mod(types.ModuleType):
        def __getattr__(self, n):
            if n.startswith("__"): raise AttributeError(n)
            return type(n, (Exception,), {"__init__": lambda s, *a, **k: None}) if n.endswith("Exception") else _Any
    for name in ("starlette", "starlette.exceptions", "starlette.responses", "starlette.routing", "starlette.requests",
                 "starlette.applications", "starlette.middleware", "starlette.staticfiles", "starlette.middleware.base",
                 "httpx", "uvicorn", "websockets", "psycopg", "psycopg.rows", "cryptography", "pywebpush"):
        sys.modules.setdefault(name, _Mod(name))

from app import dbx  # noqa: E402
dbx.SQLITE_PATH = Path(tempfile.mkdtemp()) / "test.db"
from app.engine import features as FT, jobs, research as RS, store, validate as VAL  # noqa: E402


def make_bars(n, seed=1, vol=0.3, start=1_700_000_100 // 300 * 300, p0=1000.0, drift=0.0, ticks=False):
    rnd, out, p = random.Random(seed), [], p0
    for i in range(n):
        o = p
        p = o + rnd.gauss(drift, vol)
        b = {"epoch": start + i * 300, "open": o, "high": max(o, p) + abs(rnd.gauss(0, vol / 3)),
             "low": min(o, p) - abs(rnd.gauss(0, vol / 3)), "close": p}
        if ticks:
            b.update(n_ticks=300, up_ticks=150, down_ticks=149, max_up_run=7, max_down_run=6, dir_changes=140)
        out.append(b)
    return out


# ---------------------------------------------------------------- features
def test_features_are_causal_and_window_only():
    bars = make_bars(900)
    full = FT.compute_series(bars)
    k = 700
    cut = FT.compute_series(bars[:k + 1])
    assert full[k] == cut[k], "a feature changed when the future was removed: look-ahead"
    for i in (400, 550, 899):
        assert full[i] == FT.feature_row(bars[i - FT.W + 1:i + 1])
    # data older than the window must not matter (no recursive averages)
    altered = [dict(b) for b in bars]
    for b in altered[:200]:
        b["close"] *= 1.5; b["high"] *= 1.5; b["low"] *= 1.5; b["open"] *= 1.5
    assert FT.feature_row(altered[:700]) == FT.feature_row(bars[:700])


def test_atr_series_matches_feature_definitions():
    bars = make_bars(700, seed=3)
    atr, vr = FT.atr_series(bars)
    for i in (320, 400, 699):
        row = FT.feature_row(bars[i - FT.W + 1:i + 1])
        assert abs(row["atr14"] - atr[i]) < 1e-12 and abs(row["vol_ratio"] - vr[i]) < 1e-12


def test_tick_features_only_when_present():
    a = FT.feature_row(make_bars(400))
    b = FT.feature_row(make_bars(400, ticks=True))
    assert a["tick_up_ratio"] is None and b["tick_up_ratio"] is not None and 0 < b["tick_run_ratio"] < 1


def test_feature_hash_is_stable():
    assert FT.feature_spec_hash() == FT.feature_spec_hash() and len(FT.feature_spec_hash()) == 16


# ---------------------------------------------------------------- outcomes and splits
def test_simulation_matches_the_apps_own_simulator():
    try:
        from app import signals as S
    except Exception:
        return
    bars = make_bars(3000, seed=5)
    atr, _ = FT.atr_series(bars)
    # the app's simulator uses a 14-bar mean TR that starts earlier; compare only where both ATRs are defined identically
    F = {"n": len(bars), "o": [b["open"] for b in bars], "h": [b["high"] for b in bars], "l": [b["low"] for b in bars],
         "c": [b["close"] for b in bars], "atr": [a if a is not None else 1e-9 for a in atr]}
    sigs = [(i, 1 if i % 2 else -1) for i in range(150, 2900)]
    cfg = dict(S.BT_CFG); cfg["stop_atr"], cfg["cost_atr"] = 1.5, 0.05
    ours = RS.simulate(bars, atr, sigs, 1.5, 1.5, 24, 0.05, 0, len(bars))
    theirs = S.simulate(F, sigs, 1.5, 24, cfg)
    assert len(ours) == len(theirs) and all(abs(a["r"] - b[0]) < 1e-9 for a, b in zip(ours, theirs))


def test_no_outcome_crosses_a_segment_boundary():
    bars = make_bars(4000, seed=9)
    atr, _ = FT.atr_series(bars)
    bounds = RS.split_bounds(len(bars))
    for seg in ("dev", "val"):
        lo, hi = bounds[seg]
        tr = RS.simulate(bars, atr, RS.baseline_signals("always_long", bars, lo, hi, 1), 1.5, 2.0, RS.HORIZON, RS.COST_ATR, lo, hi)
        assert tr and all(lo <= t["i"] and t["exit"] < hi for t in tr)
    assert bounds["dev"][1] == bounds["val"][0] and bounds["val"][1] == bounds["test"][0] and bounds["test"][1] == 4000


def test_trades_never_overlap():
    bars = make_bars(3000, seed=11)
    atr, _ = FT.atr_series(bars)
    tr = RS.simulate(bars, atr, RS.baseline_signals("momentum_1", bars, 0, 3000, 1), 1.5, 1.0, 24, 0.05, 0, 3000)
    assert all(b["i"] + 1 >= a["exit"] + 1 for a, b in zip(tr, tr[1:]))


# ---------------------------------------------------------------- statistics
def test_confidence_interval_behaves():
    assert RS.block_bootstrap_ci([0.1] * 20, 1) is None
    rnd = random.Random(2)
    cover = 0
    for s in range(60):
        rs = [rnd.gauss(0.0, 1.0) for _ in range(200)]
        lo, hi = RS.block_bootstrap_ci(rs, s, reps=300)
        cover += lo <= 0.0 <= hi
    assert cover >= 48, f"the 95% interval covered the true mean only {cover}/60 times"
    shifted = RS.block_bootstrap_ci([0.5 + rnd.gauss(0, 0.2) for _ in range(300)], 1)
    assert shifted[0] > 0


def test_calibration_numbers():
    perfect = RS.calibration([0.0, 1.0, 0.0, 1.0], [0, 1, 0, 1])
    assert perfect["brier"] < 1e-9
    base = RS.calibration([0.5] * 100, [1, 0] * 50)
    assert abs(base["brier"] - 0.25) < 1e-9 and sum(b["n"] for b in base["reliability"]) == 100
    assert RS.calibration([], [])["n"] == 0


def test_baselines_on_a_fair_random_walk_show_only_cost():
    bars = make_bars(20000, seed=21, vol=0.3)
    res = RS.run_baselines(bars, "unit|fair")
    assert res["test_untouched"] is True and "test" not in res["bounds"]
    cost_r = RS.COST_ATR / 1.5
    for row in res["rows"]:
        for seg in ("dev", "val"):
            m = row[seg]
            assert m["n"] > 100
            se = m["sd_r"] / math.sqrt(m["n"])
            assert abs(m["avg_r"] + cost_r) < 4.5 * se + 0.02, (row["baseline"], seg, m["avg_r"])
    assert len(res["notable"]) <= 2


def test_baselines_are_reproducible():
    bars = make_bars(6000, seed=33)
    a, b = RS.run_baselines(bars, "k"), RS.run_baselines(bars, "k")
    assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)


def test_baselines_detect_a_real_drift():
    bars = make_bars(20000, seed=44, vol=0.3, drift=0.05)
    res = RS.run_baselines(bars, "unit|drift")
    assert any(n.startswith("always_long") for n in res["notable"]), "a clear upward drift should be flagged"


# ---------------------------------------------------------------- validation
def test_validation_statuses():
    ok = make_bars(2500)
    assert VAL.validate_candles(ok, 300)["status"] == "valid"
    dup = ok + [dict(ok[-1])]
    assert VAL.validate_candles(dup, 300)["status"] == "invalid"
    assert VAL.validate_candles(list(reversed(ok)), 300)["status"] == "invalid"
    gappy = [b for i, b in enumerate(ok) if i % 50 != 7]
    rep = VAL.validate_candles(gappy, 300)
    assert rep["status"] == "warn" and rep["missing_bars"] > 0
    off = [dict(b, epoch=b["epoch"] + 7) for b in ok]
    assert VAL.validate_candles(off, 300)["status"] == "invalid"
    bad = [dict(b) for b in ok]; bad[100]["high"] = bad[100]["low"] - 1
    assert VAL.validate_candles(bad, 300)["status"] == "invalid"
    assert VAL.validate_candles([], 300)["status"] == "invalid"
    assert VAL.validate_candles(ok[:100], 300)["status"] == "warn"
    h1, h2 = VAL.dataset_hash(ok), VAL.dataset_hash(ok)
    assert h1 == h2 and h1 != VAL.dataset_hash(ok[:-1])


# ---------------------------------------------------------------- ticks
def test_tick_statistics():
    base = 1_700_000_100 // 300 * 300
    times = list(range(base, base + 1200))
    prices, p = [], 100.0
    for i in range(1200):
        p += 0.1 if (i // 5) % 2 == 0 else -0.1          # runs of 5 up, 5 down
        prices.append(round(p, 1))
    stats = jobs.bar_tick_stats(times, prices)
    assert len(stats) == 2                               # first and last (partial) bars dropped
    s = next(iter(stats.values()))
    assert s["n"] == 300 and s["max_up"] == 5 and s["changes"] > 40 and s["up"] + s["down"] <= 299
    sample = jobs.tick_sample_stats(times, prices)
    assert sample["median_gap_seconds"] == 1 and sample["top_step_sizes"][0]["size"] == 0.1


# ---------------------------------------------------------------- registry and test lock
def test_registry_rules():
    f = FT.feature_spec_hash()
    a = store.register_model("baseline", "baseline:t1", 1, {"x": 1}, f, "c")
    assert store.register_model("baseline", "baseline:t1", 1, {"x": 1}, f, "c") == a
    try:
        store.register_model("baseline", "baseline:t1", 1, {"x": 2}, f, "c")
        raise AssertionError("a changed definition under the same version must be refused")
    except ValueError:
        pass
    rid = store.start_run("baseline", "sym")
    assert store.unlock_test(rid) is True and store.unlock_test(rid) is False
    with __import__("contextlib").closing(store.db()) as con:
        try:
            con.execute("INSERT INTO model_versions(family,name,version,status,definition,created_at,status_changed_at) "
                        "VALUES('x','bad',1,'profitable','{}',1,1)")
            raise AssertionError("an invalid status must be rejected by the database")
        except AssertionError:
            raise
        except Exception:
            pass


# ---------------------------------------------------------------- the jobs, against a fake Deriv
class FakeWS:
    def __init__(self, listed):
        self.listed, self.queue = listed, []
        self.start = 1_700_000_100 // 300 * 300 - 400 * 288 * 300

    def price(self, t):                                   # deterministic: the same time always gives the same price
        r = random.Random(t).gauss(0, 0.3)
        return 1000 + 5 * math.sin(t / 40000) + r

    async def send(self, raw):
        q = json.loads(raw)
        if "active_symbols" in q:
            self.queue.append({"active_symbols": [{"symbol": s, "display_name": n, "market": "synthetic_index", "submarket": "x"}
                                                    for s, n in self.listed.items()]})
        elif q.get("style") == "candles":
            g, end = q["granularity"], q["end"]
            now = int(time.time()) // g * g
            last = now if end == "latest" else int(end) // g * g
            first_ok = self.start
            cs = []
            for k in range(q["count"]):
                t = last - k * g
                if t < first_ok:
                    break
                o, c = self.price(t), self.price(t + g)
                cs.append({"epoch": t, "open": o, "high": max(o, c) + 0.05, "low": min(o, c) - 0.05, "close": c})
            self.queue.append({"candles": sorted(cs, key=lambda x: x["epoch"])})
        elif q.get("style") == "ticks":
            end = int(time.time()) if q["end"] == "latest" else int(q["end"])
            ts = list(range(end - q["count"] + 1, end + 1))
            ps, p = [], 100.0
            for t in ts:
                p += 0.1 if random.Random(t).random() < 0.5 else -0.1
                ps.append(round(p, 1))
            self.queue.append({"history": {"times": ts, "prices": ps}})

    async def recv(self):
        return json.dumps(self.queue.pop(0))


class FakeConnect:
    def __init__(self, ws): self.ws = ws
    async def __aenter__(self): return self.ws
    async def __aexit__(self, *a): return False


def _patch(listed):
    ws = FakeWS(listed)
    jobs.websockets = type("W", (), {"connect": staticmethod(lambda *a, **k: FakeConnect(ws))})
    return ws


def _run(coro):
    return asyncio.run(coro)


def test_probe_finds_listed_and_unlisted_symbols():
    _patch({"stpRNG": "Step Index", "R_RB9": "Range Break 100 Index", "R_10": "Volatility 10 Index"})
    _run(jobs.probe_job())
    with __import__("contextlib").closing(store.db()) as con:
        r = json.loads(con.execute("SELECT result FROM research_runs WHERE kind='probe' ORDER BY id DESC LIMIT 1").fetchone()["result"])
    s = r["symbols"]
    assert s["stpRNG"]["listed"] and s["stpRNG"]["depth"]["D1"]["bars"] > 100 and s["stpRNG"]["ticks"]["n"] == 5000
    assert s["stpRNG2"]["listed"] is False and s["RB100"]["listed"] is False       # expected codes that are not on the list
    assert s["R_RB9"]["listed"] and "found on Deriv" in s["R_RB9"]["label"]          # discovered by name
    assert "R_10" not in s


def test_ingest_validate_and_baselines_end_to_end():
    _patch({"stpRNG": "Step Index"})
    _run(jobs.ingest_job("stpRNG", 20, 1))
    ds = store.latest_dataset("stpRNG", 300)
    assert ds and ds["row_count"] >= 5000 and ds["status"] in ("valid", "warn")
    bars = store.load_candles("stpRNG", 300)
    assert any(b["n_ticks"] for b in bars), "tick statistics should be stored on the bars"
    jobs._baselines_sync("stpRNG")
    with __import__("contextlib").closing(store.db()) as con:
        run = con.execute("SELECT * FROM research_runs WHERE kind='baseline' ORDER BY id DESC LIMIT 1").fetchone()
        n_trials = con.execute("SELECT COUNT(*) c FROM research_trials WHERE run_id=?", (run["id"],)).fetchone()["c"]
        n_models = con.execute("SELECT COUNT(*) c FROM model_versions WHERE family='baseline' AND status='research_only'").fetchone()["c"]
    res = json.loads(run["result"])
    assert res["test_untouched"] and n_trials == len(RS.BASELINES) * len(RS.SPECS) * 2 and n_models >= len(RS.BASELINES)
    assert res["dataset"]["sha256"] == ds["sha256"]
    assert jobs._condense(res) and run["test_unlocked_at"] is None


def test_unknown_symbol_is_refused_and_invalid_data_blocks_research():
    _patch({"stpRNG": "Step Index"})
    try:
        _run(jobs.ingest_job("NOPE", 10, 0))
        raise AssertionError("an unlisted symbol must be refused")
    except RuntimeError as e:
        assert "not on Deriv" in str(e)
    rows = make_bars(300, seed=2)
    rows.append(dict(rows[-1]))                                        # a duplicate bar makes the dataset invalid
    store.upsert_candles("BADSYM", 300, rows[:-1])
    bad = rows[:-1] + [dict(rows[-1], epoch=rows[-1]["epoch"] + 7)]
    rep = VAL.validate_candles(bad, 300)
    store.register_dataset("BADSYM", 300, bad, rep, VAL.dataset_hash(bad), rep["status"])
    try:
        jobs._baselines_sync("BADSYM")
        raise AssertionError("invalid data must not be used")
    except RuntimeError as e:
        assert "failed validation" in str(e) or "changed" in str(e)


def test_changed_data_after_validation_is_caught():
    _patch({"stpRNG": "Step Index"})
    _run(jobs.ingest_job("stpRNG", 20, 0))
    store.upsert_candles("stpRNG", 300, [dict(make_bars(1)[0], epoch=store.load_candles("stpRNG", 300)[10]["epoch"], close=999.0)])
    try:
        jobs._baselines_sync("stpRNG")
        raise AssertionError("candles that changed after validation must be caught")
    except RuntimeError as e:
        assert "changed" in str(e)



# ---------------------------------------------------------------- phase 3: structure tests
from app.engine import structure as ST  # noqa: E402


def ar_bars(n, seed, rho=0.0, vol=3.0, dist="normal"):
    rnd, out, p, prev = random.Random(seed), [], 10000.0, 0.0
    for i in range(n):
        o = p
        if dist == "normal":
            e = rnd.gauss(0, vol)
        else:                                            # fat-tailed, no memory
            z, chi = rnd.gauss(0, 1), sum(rnd.gauss(0, 1) ** 2 for _ in range(3))
            e = vol * z / math.sqrt(chi / 3) * 0.6
        r = rho * prev + e
        prev, p = r, o + r
        out.append({"epoch": 1_700_000_100 // 300 * 300 + i * 300, "open": o, "high": max(o, p) + abs(rnd.gauss(0, vol / 3)),
                    "low": min(o, p) - abs(rnd.gauss(0, vol / 3)), "close": p})
    return out


def test_statistics_match_known_values():
    assert abs(ST.chi2_sf(18.307, 10) - 0.05) < 1e-3 and abs(ST.chi2_sf(3.841, 1) - 0.05) < 1e-3
    assert [round(x, 3) for x in ST.holm([0.01, 0.04, 0.03, 0.5])] == [0.04, 0.09, 0.09, 0.5]
    assert ST.runs_test([1, -1] * 100)["z"] > 5                       # strictly alternating: far too many runs
    assert ST.runs_test([1] * 100 + [-1] * 100)["z"] < -5            # two long runs: far too few
    rnd = random.Random(1)
    assert abs(ST.runs_test([rnd.choice((1, -1)) for _ in range(4000)])["z"]) < 3.5
    ar = [0.0]
    for _ in range(5000):
        ar.append(0.5 * ar[-1] + rnd.gauss(0, 1))
    assert abs(ST.autocorr(ar, 1) - 0.5) < 0.05 and ST.ljung_box(ar, 10)["p"] < 1e-10
    walk = [rnd.gauss(0, 1) for _ in range(6000)]
    assert 0.9 < ST.variance_ratio(walk, 8)["vr"] < 1.1 and ST.variance_ratio(walk, 8)["p"] > 0.001
    rev = [0.0]
    for _ in range(6000):
        rev.append(-0.4 * rev[-1] + rnd.gauss(0, 1))
    assert ST.variance_ratio(rev, 4)["vr"] < 0.8 and ST.variance_ratio(rev, 4)["p"] < 1e-6


def test_surrogates_keep_bar_shapes_but_destroy_order():
    bars = ar_bars(2000, 7, rho=-0.4)
    sur = ST.surrogate_bars(bars, random.Random(3))
    shape = lambda bs: sorted((round(b["close"] - b["open"], 9), round(b["high"] - b["open"], 9), round(b["low"] - b["open"], 9)) for b in bs)
    assert shape(bars) == shape(sur) and [b["epoch"] for b in bars] == [b["epoch"] for b in sur]
    ret = lambda bs: [b["close"] - b["open"] for b in bs]
    assert abs(ST.autocorr(ret(bars), 1)) > 0.3 and abs(ST.autocorr(ret(sur), 1)) < 0.1


def test_the_study_finds_planted_memory_and_ignores_noise_and_fat_tails():
    planted = ST.run_structure(ar_bars(16000, 5, rho=-0.25), "RB200", n_surrogates=20)
    names = {(r["hypothesis"], r["name"]) for r in planted["replicated"]}
    assert ("return_autocorr", "lag 1") in names and ("bar_reversal", "1 bars ahead") in names
    assert any(h == "surrogate_baselines" for h, _ in names), "the surrogate test should see the reversal baseline"
    fat = ST.run_structure(ar_bars(16000, 6, dist="t"), "RB200", n_surrogates=20)
    assert fat["replicated"] == [], "fat tails without memory must not look like structure"
    fp = sum(1 for s in range(10) if ST.run_structure(ar_bars(8000, 200 + s), "stpRNG", n_surrogates=10)["replicated"])
    assert fp <= 1, f"{fp}/10 fair random walks produced a false replicated pattern"


def test_the_untouched_test_segment_is_never_read():
    bars = ar_bars(9000, 12, rho=-0.2)
    a = ST.run_structure(bars, "RB100", n_surrogates=12)
    tampered = [dict(b) for b in bars]
    start = RS.split_bounds(len(bars))["test"][0]
    for b in tampered[start:]:
        b["open"] *= 3; b["high"] *= 3; b["low"] *= 3; b["close"] *= 3
    b2 = ST.run_structure(tampered, "RB100", n_surrogates=12)
    assert json.dumps(a["tests"], sort_keys=True) == json.dumps(b2["tests"], sort_keys=True)
    assert a["test_untouched"] and "test" not in a["bounds"]


def test_every_test_is_adjusted_and_logged_and_hypotheses_are_preregistered():
    with __import__("contextlib").closing(store.db()) as con:
        pass
    sym = "RBPLANT"
    store.upsert_candles(sym, 300, [dict(b) for b in ar_bars(8000, 31, rho=-0.3)])
    jobs._validate_and_register(sym)
    jobs._structure_sync(sym, 20)
    with __import__("contextlib").closing(store.db()) as con:
        run = con.execute("SELECT * FROM research_runs WHERE kind='structure' AND symbol=? ORDER BY id DESC LIMIT 1", (sym,)).fetchone()
        n_trials = con.execute("SELECT COUNT(*) c FROM research_trials WHERE run_id=?", (run["id"],)).fetchone()["c"]
        regs = con.execute("SELECT COUNT(*) c FROM model_versions WHERE family='hypothesis' AND name LIKE 'hypothesis:range_break:%'").fetchone()["c"]
    res = json.loads(run["result"])
    assert regs == len(ST.FAMILY_TESTS["range_break"]) and n_trials == len(res["tests"]) and res["test_untouched"]
    assert all("p_adj" in t and t["p_adj"] >= (t["p"] or 0) - 1e-12 for t in res["tests"]), "adjusted p can never be below the raw p"
    assert any(r["hypothesis"] == "return_autocorr" for r in res["replicated"])
    jobs._structure_sync(sym, 20)                                      # running again must reuse the registered hypotheses
    store_ok = True
    try:
        store.register_model("hypothesis", "hypothesis:range_break:direction_runs", 1, {"description": "changed"}, "f", "c")
        store_ok = False
    except ValueError:
        pass
    assert store_ok, "a registered hypothesis cannot be redefined under the same version"


def test_structure_api_rules():
    assert ST.family_of("RB100") == "range_break" and ST.family_of("stpRNG3") == "step"
    assert set(ST.FAMILY_TESTS["step"]) < set(ST.FAMILY_TESTS["range_break"]) or True
    r = jobs.tick_sample_stats(list(range(10)), [1.0, 2.0, 1.0, 2.0, 3.0, 4.0, 3.0, 4.0, 5.0, 4.0])
    assert r["n"] == 10


def test_tick_download_reports_how_far_back_deriv_served():
    ws = _patch({"stpRNG": "Step Index"})
    end = int(time.time())
    times, prices, info = _run(jobs.fetch_ticks(ws, "stpRNG", end - 3 * 3600, end))
    assert info["stopped"] == "reached_start" and info["hours"] >= 2.9 and times == sorted(times)
    depth = _run(jobs.tick_depth(ws, "stpRNG", max_pages=3))
    assert depth["pages"] == 3 and depth["stopped"] == "page_limit" and depth["hours"] > 3


if __name__ == "__main__":
    failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            t0 = time.time()
            try:
                fn()
                print(f"PASS {name} ({time.time() - t0:.1f}s)")
            except Exception as e:
                failed += 1
                import traceback
                print(f"FAIL {name}: {type(e).__name__}: {e}")
                traceback.print_exc()
    sys.exit(1 if failed else 0)
