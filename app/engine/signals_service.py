"""Phase 4b: the signal service, in SHADOW mode (Module B decisions and Module C risk controls).

What it does, for every model that has passed the untouched test (status demo_validated or approved):
  * reads the latest 5-minute candles, drops the one still forming, and decides with the SAME functions the research used
    (candidates.fade_signals' alignment rule, features.atr_series for volatility);
  * returns one of four decisions:
        BUY / SELL   a complete, valid setup with levels, an entry window and an expiry
        WAIT         the setup is incomplete (for example the 15-minute bar has not closed yet). Not stored.
        NO_TRADE     a mandatory criterion failed: stale data, a gap in the data, volatility spike, stop too tight, invalid
                     target, daily cap reached, model suspended, no direction. Stored as a risk event, once per bar.
  * stores every BUY/SELL in ai_signals with the model version, the evidence status at that moment, a reproducible feature
    reference (hashes, the exact candle window and inputs) and visibility 'admin'. Members never see them in this phase;
  * tracks each open signal to its real outcome with the same simulator used in research, then checks the suspension rules:
    the model is suspended automatically after a run of losses or a weak result, and a suspended model produces no signals;
  * never sizes a position, never raises risk after a loss, and never places a trade.
Confidence is not computed (there is no calibrated probability model yet), so it is stored as empty and never shown.
Evidence status is kept separate and copied from the model."""
import asyncio
import json
import math
import os
import time
from contextlib import closing

from .. import deriv
from . import candidates as CA
from . import features as FT
from . import research as RS
from . import store

BAR = 300
LIVE_STATUSES = ("demo_validated", "approved")
RULE = "fade_aggregated_bar"
MIN_BARS = 150
_state = {"last_scan": None, "last_error": None}


def _risk(defn: dict) -> dict:
    return {**CA.RISK_DEFAULTS, **(defn.get("risk") or {})}


def _midnight(now: int) -> int:
    return now - now % 86400


# ---------------------------------------------------------------- the decision (pure, tested)
def decide(model: dict, bars: list, now: int, signals_today: int = 0) -> dict:
    """One decision for one model on the latest candles. `bars` may include the candle that is still forming."""
    d = model["definition"] if isinstance(model["definition"], dict) else json.loads(model["definition"])
    risk, k, delay = _risk(d), int(d["k"]), int(d["delay"])
    if model["status"] == "suspended":
        return {"decision": "NO_TRADE", "kind": "model_suspended", "reason": model.get("status_reason") or "model is suspended"}
    if model["status"] not in LIVE_STATUSES:
        return {"decision": "NO_TRADE", "kind": "not_validated", "reason": "only validated models produce signals"}
    done = [b for b in bars if b["epoch"] + BAR <= now]
    if len(done) < MIN_BARS:
        return {"decision": "NO_TRADE", "kind": "insufficient_data", "reason": f"only {len(done)} completed candles"}
    i = len(done) - 1
    last = done[i]
    base = {"bar_epoch": last["epoch"]}
    if now - (last["epoch"] + BAR) > risk["stale_seconds"]:
        return {**base, "decision": "NO_TRADE", "kind": "stale_data",
                "reason": f"newest completed candle is {(now - last['epoch'] - BAR) // 60} minutes old"}
    if any(done[j]["epoch"] - done[j - 1]["epoch"] != BAR for j in range(i - k + 1, i + 1)):
        return {**base, "decision": "NO_TRADE", "kind": "data_gap", "reason": "missing candles inside the signal bar"}
    if (last["epoch"] // BAR + 1) % k:
        left = k - ((last["epoch"] // BAR + 1) % k)
        return {**base, "decision": "WAIT", "kind": "incomplete_bar",
                "reason": f"the {k * 5}-minute bar completes in about {left * 5} minutes"}
    move = last["close"] - done[i - k]["close"]
    if move == 0:
        return {**base, "decision": "NO_TRADE", "kind": "no_direction", "reason": "the last bar did not move"}
    atr, vr = FT.atr_series(done)
    if atr[i] is None:
        return {**base, "decision": "NO_TRADE", "kind": "no_volatility_estimate", "reason": "not enough history to scale risk"}
    if vr[i] > risk["max_vol_ratio"]:
        return {**base, "decision": "NO_TRADE", "kind": "volatility_spike",
                "reason": f"volatility is {vr[i]:.1f} times its recent average (limit {risk['max_vol_ratio']})"}
    sd = d["stop_atr"] * atr[i]
    if sd < risk["min_stop_points"]:
        return {**base, "decision": "NO_TRADE", "kind": "stop_too_tight", "reason": f"stop distance {sd:.1f} is below the minimum"}
    if not 0.5 <= d["rr"] <= 3.0:
        return {**base, "decision": "NO_TRADE", "kind": "invalid_rr", "reason": "target multiple outside 0.5 to 3"}
    if signals_today >= risk["max_signals_per_day"]:
        return {**base, "decision": "NO_TRADE", "kind": "daily_cap", "reason": f"{signals_today} signals already today"}
    direction = -1 if move > 0 else 1
    entry = last["close"]
    planned = last["epoch"] + BAR * (1 + delay)
    return {**base, "decision": "BUY" if direction > 0 else "SELL", "kind": None, "reason": "setup complete",
            "direction": direction, "entry_ref": entry, "atr": atr[i], "stop": entry - direction * sd,
            "target": entry + direction * d["rr"] * sd, "rr": d["rr"], "planned_entry_epoch": planned,
            "expires_at": planned + BAR, "i": i,
            "rationale": {"rule": RULE, "plain": f"The last {k * 5}-minute bar moved {'up' if move > 0 else 'down'} "
                                                  f"{abs(move):.1f} points, so the setup fades it. Enter at the open of the "
                                                  f"candle at {planned}, {delay} candle(s) after the signal bar closed.",
                          "bar_change": move, "k": k, "delay": delay, "atr": atr[i], "vol_ratio": vr[i],
                          "stop_atr": d["stop_atr"], "stop_distance": sd},
            "feature_ref": {"feature_spec_hash": FT.feature_spec_hash(), "code_hash": store.file_hash(CA.__file__),
                            "window_first_epoch": done[max(0, i - 120)]["epoch"], "window_last_epoch": last["epoch"],
                            "inputs": {"close": entry, "close_k_ago": done[i - k]["close"], "atr14": atr[i]}}}


def outcome_for(signal: dict, defn: dict, bars: list, now: int):
    """The real outcome of an open signal from the candles after it, using the research simulator. None if unresolved."""
    done = [b for b in bars if b["epoch"] + BAR <= now]
    idx = {b["epoch"]: n for n, b in enumerate(done)}
    i = idx.get(signal["bar_epoch"])
    if i is None:
        return "missing"
    atr, _ = FT.atr_series(done)
    d = signal["decision"] == "BUY" and 1 or -1
    tr = RS.simulate(done, atr, [(i, d)], defn["stop_atr"], defn["rr"], defn["horizon"], defn["cost_atr"], i, len(done), defn["delay"])
    if not tr:
        return None
    t = tr[0]
    if t["kind"] == "timeout" and t["exit"] < i + defn["delay"] + defn["horizon"]:
        return None                      # not enough candles yet to call it a timeout
    return t


# ---------------------------------------------------------------- persistence and the scan
def _record_event(con, model_id, symbol, bar_epoch, kind, detail):
    con.execute("INSERT INTO risk_events(model_version_id,symbol,bar_epoch,kind,detail,created_at) VALUES(?,?,?,?,?,?) "
                "ON CONFLICT(model_version_id, symbol, bar_epoch, kind) DO NOTHING",
                (model_id, symbol, int(bar_epoch), kind, str(detail)[:300], store.now()))


def check_suspension(con, model: dict) -> str:
    """Suspend automatically after a run of losses or a clearly negative forward record. Returns the reason or ''."""
    d = json.loads(model["definition"]) if isinstance(model["definition"], str) else model["definition"]
    risk = _risk(d)
    n = risk["suspend_after_resolved"]
    rows = con.execute("SELECT result_r FROM ai_signals WHERE model_version_id=? AND result_r IS NOT NULL ORDER BY id DESC LIMIT ?",
                       (model["id"], n)).fetchall()
    rs = [r["result_r"] for r in rows]
    reason = ""
    streak = 0
    for x in rs:
        if x < 0:
            streak += 1
        else:
            break
    if streak >= risk["max_loss_streak"]:
        reason = f"{streak} losing signals in a row (limit {risk['max_loss_streak']})"
    elif len(rs) >= n:
        mean = sum(rs) / len(rs)
        ci = RS.block_bootstrap_ci(rs, model["id"], reps=500)
        if mean < risk["suspend_if_mean_r_below"] and ci and ci[1] < 0:
            reason = f"last {n} signals average {mean:+.3f}R with a 95% interval entirely below zero"
    return reason


def scan_model(model: dict, bars: list, now: int) -> dict:
    """Decide, store, track outcomes, and apply the suspension rules for one model."""
    defn = json.loads(model["definition"]) if isinstance(model["definition"], str) else model["definition"]
    model = {**model, "definition": defn}
    symbol = defn["symbol"]
    out = {"model": model["name"], "model_id": model["id"], "resolved": 0}
    with closing(store.db()) as con:
        today = con.execute("SELECT COUNT(*) AS c FROM ai_signals WHERE model_version_id=? AND created_at>=?",
                            (model["id"], _midnight(now))).fetchone()["c"]
        dec = decide(model, bars, now, today)
        out.update({k: dec.get(k) for k in ("decision", "kind", "reason")})
        if dec["decision"] in ("BUY", "SELL"):
            cur = con.execute(
                "INSERT INTO ai_signals(model_version_id,symbol,tf,bar_epoch,decision,entry_ref,atr,stop,target,rr,planned_entry_epoch,"
                "expires_at,rationale,confidence,evidence_status,feature_ref,visibility,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(model_version_id, symbol, bar_epoch) DO NOTHING",
                (model["id"], symbol, BAR, dec["bar_epoch"], dec["decision"], dec["entry_ref"], dec["atr"], dec["stop"],
                 dec["target"], dec["rr"], dec["planned_entry_epoch"], dec["expires_at"], json.dumps(dec["rationale"]), None,
                 model["status"], json.dumps(dec["feature_ref"]), "admin", now))
            if cur.rowcount != 1:
                out["decision"], out["kind"], out["reason"] = "NO_TRADE", "duplicate", "this bar already produced a signal"
                _record_event(con, model["id"], symbol, dec["bar_epoch"], "duplicate", "signal for this bar already exists")
            else:
                out["created"] = True
        elif dec["decision"] == "NO_TRADE" and dec.get("bar_epoch") is not None:
            _record_event(con, model["id"], symbol, dec["bar_epoch"], dec["kind"], dec["reason"])
        elif dec["decision"] == "NO_TRADE":
            _record_event(con, model["id"], symbol, now - now % BAR, dec["kind"], dec["reason"])
        for s in con.execute("SELECT * FROM ai_signals WHERE model_version_id=? AND status='open'", (model["id"],)).fetchall():
            res = outcome_for(dict(s), defn, bars, now)
            if res == "missing":
                if now - s["created_at"] > 86400:
                    con.execute("UPDATE ai_signals SET status='timeout', closed_at=? WHERE id=?", (now, s["id"]))
                    _record_event(con, model["id"], symbol, s["bar_epoch"], "unresolvable_signal", "candles for this signal are gone")
            elif res:
                con.execute("UPDATE ai_signals SET status=?, result_r=?, closed_at=? WHERE id=?", (res["kind"], res["r"], now, s["id"]))
                out["resolved"] += 1
        con.commit()
        if model["status"] in LIVE_STATUSES:
            reason = check_suspension(con, model)
            if reason:
                store.set_model_status(model["id"], "suspended", "automatic: " + reason)
                _record_event(con, model["id"], symbol, now - now % BAR, "auto_suspended", reason)
                con.commit()
                out["suspended"] = reason
    return out


def _models() -> list:
    with closing(store.db()) as con:
        rows = con.execute("SELECT * FROM model_versions WHERE status IN ('demo_validated','approved','suspended')").fetchall()
    out = []
    for r in rows:
        try:
            if json.loads(r["definition"]).get("rule") == RULE:
                out.append(dict(r))
        except ValueError:
            continue
    return out


async def scan_once(now: int = None, data: dict = None) -> list:
    """One pass over every live model. `data` ({symbol: [candles]}) is for tests; normally candles come from Deriv."""
    now = now or int(time.time())
    results = []
    models = await asyncio.to_thread(_models)
    for sym in sorted({json.loads(m["definition"])["symbol"] for m in models}):
        bars, err = None, None
        if data is not None:
            bars = data.get(sym)
        else:
            try:
                got = await deriv.fetch_multi([sym], ("M5",), 400)
                bars = [{"epoch": int(c["epoch"]), "open": float(c["open"]), "high": float(c["high"]), "low": float(c["low"]),
                         "close": float(c["close"])} for c in got[sym]["M5"]]
            except Exception as e:
                err = f"{type(e).__name__}: {str(e)[:100]}"
        for m in [m for m in models if json.loads(m["definition"])["symbol"] == sym]:
            if not bars:
                def note(m=m, err=err):
                    with closing(store.db()) as con:
                        _record_event(con, m["id"], sym, now - now % 900, "data_unavailable", err or "no candles")
                        con.commit()
                await asyncio.to_thread(note)
                results.append({"model": m["name"], "decision": "NO_TRADE", "kind": "data_unavailable", "reason": err or "no candles"})
                continue
            results.append(await asyncio.to_thread(scan_model, m, bars, now))
    _state["last_scan"] = now
    return results


async def engine_loop():
    """Background loop, started only when DRIVERLINE_ENGINE_SHADOW=1. Never raises."""
    await asyncio.sleep(45)
    while True:
        try:
            await scan_once()
            _state["last_error"] = None
        except Exception as e:
            _state["last_error"] = f"{type(e).__name__}: {str(e)[:100]}"
            print(f"[engine] shadow scan failed: {_state['last_error']}", flush=True)
        await asyncio.sleep(60)


def enabled() -> bool:
    return os.environ.get("DRIVERLINE_ENGINE_SHADOW", "0") == "1"


def summary() -> dict:
    """For the Admin page: models, signal counts and forward results, recent signals and risk events."""
    with closing(store.db()) as con:
        models = []
        for m in con.execute("SELECT id, name, status, status_reason, definition FROM model_versions WHERE definition LIKE ? ORDER BY id",
                             (f'%"{RULE}"%',)).fetchall():
            rs = [r["result_r"] for r in con.execute("SELECT result_r FROM ai_signals WHERE model_version_id=? AND result_r IS NOT NULL",
                                                      (m["id"],)).fetchall()]
            opn = con.execute("SELECT COUNT(*) AS c FROM ai_signals WHERE model_version_id=? AND status='open'", (m["id"],)).fetchone()["c"]
            first = con.execute("SELECT MIN(created_at) AS t FROM ai_signals WHERE model_version_id=?", (m["id"],)).fetchone()["t"]
            models.append({"id": m["id"], "name": m["name"], "status": m["status"], "status_reason": m["status_reason"],
                           "open": opn, "resolved": len(rs), "avg_r": (sum(rs) / len(rs)) if rs else None,
                           "ci95": RS.block_bootstrap_ci(rs, m["id"], reps=500), "first_signal": first})
        sigs = [dict(r) for r in con.execute("SELECT s.id, s.symbol, s.decision, s.bar_epoch, s.entry_ref, s.stop, s.target, s.status, "
                                             "s.result_r, s.evidence_status, s.created_at, m.name FROM ai_signals s JOIN model_versions m "
                                             "ON m.id=s.model_version_id ORDER BY s.id DESC LIMIT 12").fetchall()]
        ev = [dict(r) for r in con.execute("SELECT e.kind, e.detail, e.symbol, e.created_at, m.name FROM risk_events e JOIN model_versions m "
                                           "ON m.id=e.model_version_id ORDER BY e.id DESC LIMIT 10").fetchall()]
    return {"enabled": enabled(), "last_scan": _state["last_scan"], "last_error": _state["last_error"], "models": models,
            "signals": sigs, "events": ev}
