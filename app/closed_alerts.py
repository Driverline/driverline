"""Notifications for trades that have closed: take profit, stop loss, closed by hand (and a few other ways).

How it works: the MT5 companion EA already reports each fully closed position to TradeLens. When one is imported into the journal
for the first time, ea.report hands it to notify_closed(). The EA's newer version also says WHY the position closed (it reads
MT5's own deal reason). For older EA versions the reason is judged from where the trade ended against its stop and target.

Safeguards: only trades that closed in the last 30 minutes alert (so reconnecting an old terminal never floods a phone),
at most 3 separate alerts per report (more are combined into one summary), nothing for blocked members, members without
notifications, members who switched them off (a checkbox in Notifications), or inside their own quiet hours. NOTIFY_CLOSED=0 switches these alerts off.
"""
import os
import time
from contextlib import closing
from datetime import datetime, timezone

from . import auth, credits, push

RECENT_SECS = 1800
MAX_SEPARATE = 3
LABELS = {"tp": "Take profit hit", "sl": "Stop loss hit", "manual": "Closed manually", "expert": "Closed from TradeLens",
          "so": "Stopped out by the broker", "other": "Trade closed"}


def classify(t: dict) -> str:
    """tp, sl, manual, expert, so or other. Trusts the EA's own reason; otherwise judges by the closing price."""
    r = str(t.get("reason") or "").lower()
    if r in LABELS and r != "other":
        return r
    try:
        direction, ex, en = t["direction"], float(t["exit"]), float(t["entry"])
        sl, tp = float(t.get("sl") or 0), float(t.get("tp") or 0)
    except (KeyError, TypeError, ValueError):
        return "other"
    tol = lambda level: max(abs(en - level) * 0.03, abs(en) * 0.00002)
    if direction == "BUY":
        if tp and ex >= tp - tol(tp):
            return "tp"
        if sl and ex <= sl + tol(sl):
            return "sl"
    elif direction == "SELL":
        if tp and ex <= tp + tol(tp):
            return "tp"
        if sl and ex >= sl - tol(sl):
            return "sl"
    return "other"


def _money(x: float, currency) -> str:
    return f"{'+' if x >= 0 else '-'}{abs(x):.2f}" + (f" {str(currency)[:4]}" if currency else "")


def messages(trades: list, currency=None) -> list:
    trades = sorted(trades, key=lambda t: t.get("closed_at") or 0)
    if len(trades) > MAX_SEPARATE:
        net = sum(float(t.get("profit") or 0) for t in trades)
        wins = sum(1 for t in trades if float(t.get("profit") or 0) > 0)
        return [{"title": f"{len(trades)} trades closed",
                 "body": f"Net result {_money(net, currency)}: {wins} up, {len(trades) - wins} down or flat. Review them in your journal.",
                 "url": "/journal.html", "tag": f"closed-batch-{int(trades[-1].get('closed_at') or 0)}"}]
    out = []
    for t in trades:
        kind = classify(t)
        pnl, r = float(t.get("profit") or 0), t.get("r_multiple")
        name = t.get("symbol") or "Trade"
        rtxt = f" ({'+' if r >= 0 else '-'}{abs(r):.1f}R)" if isinstance(r, (int, float)) else ""
        out.append({"title": f"{LABELS[kind]}: {name}",
                    "body": f"{t.get('direction')} {float(t.get('lots') or 0):g} lots closed at {float(t.get('exit') or 0):g}. "
                            f"Result {_money(pnl, currency)}{rtxt}. Review it in your journal.",
                    "url": "/journal.html", "tag": f"closed-{t.get('id')}"})
    return out


def notify_closed(uid: int, trades: list, currency=None, sender=None) -> int:
    """Push the closed-trade alerts to this member's devices. Returns how many pushes were delivered."""
    if os.environ.get("NOTIFY_CLOSED", "1") == "0":
        return 0
    now = time.time()
    fresh = [t for t in trades if now - float(t.get("closed_at") or 0) <= RECENT_SECS]
    if not fresh:
        return 0
    from . import signal_feed as F
    sender = sender or push._send
    with closing(auth.db()) as con:
        u = con.execute("SELECT tier, blocked FROM users WHERE id=?", (uid,)).fetchone()
    if not u or u["blocked"] or not credits.entitled({"id": uid, "tier": u["tier"]}, "notify"):
        return 0
    with closing(F.db()) as con:
        prefs = F._prefs(con, uid)
    if prefs.get("notify_closed") == 0:              # the member switched closed-trade alerts off in their notification settings
        return 0
    if push._quiet(prefs, datetime.now(timezone.utc)):
        return 0
    with closing(push.db()) as con:
        subs = [dict(r) for r in con.execute("SELECT * FROM push_subs WHERE user_id=?", (uid,)).fetchall()]
    sent, gone = 0, []
    for body in messages(fresh, currency):
        for sub in subs:
            if sub["id"] in gone:
                continue
            try:
                if sender(sub, body):
                    sent += 1
                else:
                    gone.append(sub["id"])
            except Exception as e:
                print(f"[push] closed-trade alert failed: {type(e).__name__}", flush=True)
    if gone:
        with closing(push.db()) as con:
            for i in gone:
                con.execute("DELETE FROM push_subs WHERE id=?", (i,))
            con.commit()
    return sent
