"""The feature definitions, used by research AND by live inference so they can never drift apart.

Every feature is computed from a FIXED trailing window of W bars that ends at the bar that just closed. Nothing looks
ahead, and nothing depends on data older than the window (no recursive averages), so computing a feature on the full
history and computing it live on the latest W bars give exactly the same number. feature_spec_hash() changes whenever
the definitions or this file change, and every model version records the hash it was built with."""
import hashlib
import json
import math
import os

W = 300
SPEC = {"version": 1, "window": W, "atr": 14, "atr_long": 100, "er": 30, "sma": 20, "slope_lag": 5, "range": 48, "bb": 20,
        "run_cap": 20}
NAMES = ("atr14", "ret1", "body", "up", "run", "er30", "slope20", "pos48", "dist_hi48", "dist_lo48", "since_hi48",
         "since_lo48", "vol_ratio", "bbw20", "tick_up_ratio", "tick_run_ratio", "tick_change_rate")


def feature_spec_hash() -> str:
    with open(os.path.abspath(__file__), "rb") as f:
        code = hashlib.sha256(f.read()).hexdigest()
    return hashlib.sha256((json.dumps(SPEC, sort_keys=True) + code).encode()).hexdigest()[:16]


def _tr(win: list, k: int) -> float:
    h, l, pc = win[k]["high"], win[k]["low"], win[k - 1]["close"]
    return max(h - l, abs(h - pc), abs(l - pc))


def feature_row(win: list):
    """The features for the LAST bar of `win` (needs at least W bars; only the last W are used). None if not enough
    data or no volatility to scale by."""
    if len(win) < W:
        return None
    win = win[-W:]
    n = W
    trs = [_tr(win, k) for k in range(n - 100, n)]
    atr, atr_long = sum(trs[-14:]) / 14, sum(trs) / 100
    if atr <= 0 or atr_long <= 0:
        return None
    c = [b["close"] for b in win]
    last = win[-1]
    run, s = 0, 0
    for k in range(n - 1, 0, -1):
        d = c[k] - c[k - 1]
        sg = (d > 0) - (d < 0)
        if sg == 0 or (run and sg != s) or run >= SPEC["run_cap"]:
            break
        if run == 0:
            s = sg
        run += 1
    path = sum(abs(c[k] - c[k - 1]) for k in range(n - 30, n))
    sma_now, sma_then = sum(c[-20:]) / 20, sum(c[-25:-5]) / 20
    hi48, lo48 = [b["high"] for b in win[-48:]], [b["low"] for b in win[-48:]]
    rng = max(hi48) - min(lo48)
    mean20 = sum(c[-20:]) / 20
    sd20 = math.sqrt(sum((x - mean20) ** 2 for x in c[-20:]) / 20)
    row = {
        "atr14": atr, "ret1": (c[-1] - c[-2]) / atr, "body": (c[-1] - last["open"]) / atr, "up": 1.0 if c[-1] > last["open"] else 0.0,
        "run": float(run * s), "er30": abs(c[-1] - c[-31]) / path if path > 0 else 0.0, "slope20": (sma_now - sma_then) / atr,
        "pos48": (c[-1] - min(lo48)) / rng if rng > 0 else 0.5,
        "dist_hi48": (max(b["high"] for b in win[-49:-1]) - c[-1]) / atr, "dist_lo48": (c[-1] - min(b["low"] for b in win[-49:-1])) / atr,
        "since_hi48": float(47 - max(range(48), key=lambda j: (hi48[j], j))),
        "since_lo48": float(47 - max(range(48), key=lambda j: (-lo48[j], j))),
        "vol_ratio": atr / atr_long, "bbw20": 4 * sd20 / atr,
        "tick_up_ratio": None, "tick_run_ratio": None, "tick_change_rate": None,
    }
    nt = last.get("n_ticks")
    if nt:
        ud = (last.get("up_ticks") or 0) + (last.get("down_ticks") or 0)
        row["tick_up_ratio"] = (last["up_ticks"] or 0) / ud if ud else None
        row["tick_run_ratio"] = max(last.get("max_up_run") or 0, last.get("max_down_run") or 0) / nt
        row["tick_change_rate"] = (last.get("dir_changes") or 0) / nt
    return row


def compute_series(bars: list) -> list:
    """feature_row() for every bar, None where there is not enough history yet. Same function as live inference."""
    return [feature_row(bars[i - W + 1:i + 1]) if i >= W - 1 else None for i in range(len(bars))]


def atr_series(bars: list):
    """(atr14, vol_ratio) per bar, computed from prefix sums. Identical in value to feature_row()'s atr14 and vol_ratio
    (checked by the tests); used where the full feature set is not needed."""
    n = len(bars)
    tr = [0.0] * n
    for k in range(1, n):
        tr[k] = max(bars[k]["high"] - bars[k]["low"], abs(bars[k]["high"] - bars[k - 1]["close"]),
                    abs(bars[k]["low"] - bars[k - 1]["close"]))
    pre = [0.0]
    for x in tr:
        pre.append(pre[-1] + x)
    atr, vr = [None] * n, [None] * n
    for i in range(100, n):
        a, al = (pre[i + 1] - pre[i - 13]) / 14, (pre[i + 1] - pre[i - 99]) / 100
        if a > 0 and al > 0:
            atr[i], vr[i] = a, a / al
    return atr, vr
