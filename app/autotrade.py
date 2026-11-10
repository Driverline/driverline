"""AutoTrading: TradeLens places the trades from published signals by itself, inside limits the trader sets.

The trader picks, per MT5 account, which Signal types (Scalp, Day, Swing, H4 gold) and which symbols may be traded, how big each trade is,
and the most that may be open / opened per day. Nothing here changes how a trade is placed or protected: an auto trade is an ordinary
'open' command for the member's OWN EA, with the signal's stop and target attached, and it goes through exactly the same checks as the
"Open trade" button (AllowExecute, the member's own daily loss limit and position cap, equity-protection pre-check). Auto manage and
equity protection then look after it like any other trade.

Pull model, like copy trading (copy_trading.py):
  1. on_new_signals(): after each scan, one 'waiting' job per matching account (unique per signal and account).
  2. dispatch(): called from the EA poll. Turns at most ONE waiting job of that account into an 'open' command, or skips it with a reason.
     A job is never placed late (JOB_TTL), never when the signal is no longer open, and never twice.
  3. on_report(): follows the command (sent -> open -> closed / failed) and finds the trade's ticket.

Account size: which symbols may be traded depends on the account's live USD equity (TIER_SYMBOLS below). The tier is worked out on
the server from what the member's own EA last reported, never from the browser, and is re-checked before every trade. Gold is H4 only
from tier 2 to tier 6 (micro contract in tiers 2-5, standard contract in tier 6). Falling into a lower tier never closes a trade.

Access: private for now. ea_meta 'auto_mode' is 'testers' (admin plus the listed testers, the default) or 'off' (nobody).
There is deliberately no public mode yet (see docs/AUTOTRADING.md).
"""
import json
import time
from contextlib import closing
from datetime import datetime, timezone

from starlette.exceptions import HTTPException
from starlette.responses import JSONResponse
from starlette.routing import Route

from . import auth, credits, ea, journal
from .config import INSTRUMENTS, match_key

JOB_TTL = 120            # seconds a job may wait for the EA to collect it; after that it is skipped
TICKET_WAIT = 60         # seconds to look for the new position before giving up on finding its ticket
MODES = ("fixed", "risk")
RATINGS = ("Low", "Medium", "High")
STYLE_ORDER = ("scalp", "day", "swing", "h4")
STYLE_LABEL = {"scalp": "Scalp", "day": "Day", "swing": "Swing", "h4": "H4 gold"}
DEFAULTS = {"enabled": 0, "styles": [], "instruments": [], "mode": "risk", "fixed_lots": None, "risk_pct": 1.0, "max_risk_pct": 2.0,
            "max_open": 3, "max_per_day": 5, "max_rating": "Medium", "loss_limit": 3}
LIMITS = {"max_open": (1, 10), "max_per_day": (1, 50), "loss_limit": (2, 10)}
LOOSEN_DELAY = 24 * 3600     # a setting that lets AutoTrading take MORE risk only applies this long after it was asked for
STALE = 120                  # seconds: equity older than this is not trusted for a new trade
LOSS_EPS = 0.001             # a trade must lose more than 0.1% of the balance to count as a loss (breakeven exits are neutral)

# ---------------------------------------------------------------- account size -> symbols
_T1 = ("v10", "v50")
_T2 = _T1 + ("v25", "xauusd")
_T3 = _T2 + ("v10_1s", "v25_1s", "v50_1s")
_T4 = _T3 + ("v75", "v100", "v75_1s", "v100_1s", "step")
_T5 = _T4 + ("boom300", "crash300")
_T6 = _T5 + ("boom500", "boom1000", "crash500", "crash1000")
TIER_SYMBOLS = {1: _T1, 2: _T2, 3: _T3, 4: _T4, 5: _T5, 6: _T6, 7: None}          # None = every supported symbol
TIER_BOUNDS = ((26, 1), (60, 2), (160, 3), (600, 4), (1500, 5), (10500, 6))         # equity below the bound -> that tier
TIER_FROM = {1: 0, 2: 26, 3: 60, 4: 160, 5: 600, 6: 1500, 7: 10500}
GOLD_H4_TIERS = (2, 3, 4, 5, 6)
LOCKED_TEXT = "Not available for your current account equity. Increase your account equity to unlock this symbol category."


def tier_of(equity: float) -> int:
    """$25 and below (anything under $26) is tier 1, $26 to $59.99 tier 2, ... $10,500 and above tier 7."""
    for bound, t in TIER_BOUNDS:
        if equity < bound:
            return t
    return 7


def eligible_keys(tier) -> set:
    if tier is None:
        return set()
    sym = TIER_SYMBOLS[tier]
    return set(INSTRUMENTS) if sym is None else set(sym)


def is_micro(symbol: str) -> bool:
    return "micro" in str(symbol).lower()


def trade_allowed(tier, key: str, style: str) -> str:
    """'' when a trade of this instrument and signal type is allowed at this tier, else the reason."""
    if tier is None:
        return "Your account equity could not be verified"
    if key not in eligible_keys(tier):
        return LOCKED_TEXT
    if key == "xauusd" and tier in GOLD_H4_TIERS and style != "h4":
        return "Gold is traded on the H4 timeframe only at your account size"
    return ""


def gold_label(tier) -> str:
    return "XAUUSDmicro (H4 only)" if tier in (2, 3, 4, 5) else "XAUUSD (H4 only)" if tier == 6 else "XAUUSD"


def equity_status(con, aid: int, now: int = None) -> dict:
    """What the member's own EA last reported for this account, judged for AutoTrading. ok=False means no NEW auto trade may be started."""
    now = now or _now()
    out = {"ok": False, "why": "", "equity": None, "tier": None, "currency": None, "age": None}
    a = con.execute("SELECT last_seen FROM ea_accounts WHERE id=?", (aid,)).fetchone()
    r = con.execute("SELECT state, updated_at FROM ea_acct_state WHERE account_id=?", (aid,)).fetchone()
    if not a or not r:
        out["why"] = "No data from your MT5 yet"
        return out
    try:
        acc = json.loads(r["state"]).get("account") or {}
    except ValueError:
        out["why"] = "Your MT5 data could not be read"
        return out
    out["currency"] = str(acc.get("currency") or "").upper()
    eq = _f(acc.get("equity"), float("nan"))
    out["age"] = now - int(r["updated_at"] or 0)
    if eq == eq and eq > 0 and eq != float("inf"):
        out["equity"] = eq
    if not (a["last_seen"] and now - a["last_seen"] < STALE) or out["age"] > STALE:
        out["why"] = "Your MT5 is not connected right now, so your account equity cannot be verified"
    elif out["equity"] is None:
        out["why"] = "Your account equity was not reported by MT5"
    elif out["currency"] != "USD":
        out["why"] = f"AutoTrading is for USD accounts for now (this account is in {out['currency'] or 'an unknown currency'})"
    else:
        out["ok"], out["tier"] = True, tier_of(out["equity"])
    return out


def _now() -> int:
    return int(time.time())


def _f(v, d=0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return d


def _day_start(now: int) -> int:
    return now - now % 86400


# ---------------------------------------------------------------- who may use it
def mode(con) -> str:
    r = con.execute("SELECT v FROM ea_meta WHERE k='auto_mode'").fetchone()
    return r["v"] if r and r["v"] in ("off", "testers") else "testers"


def testers(con) -> list:
    r = con.execute("SELECT v FROM ea_meta WHERE k='auto_testers'").fetchone()
    try:
        return [int(x) for x in json.loads(r["v"])] if r and r["v"] else []
    except (ValueError, TypeError):
        return []


def _user(uid: int):
    with closing(auth.db()) as con:
        r = con.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    return dict(r) if r and not r["blocked"] else None


def allowed(con, user: dict) -> str:
    """'' when this member may use AutoTrading, otherwise the reason (shown to nobody but the admin)."""
    from . import signal_feed
    if mode(con) == "off":
        return "AutoTrading is switched off"
    if user["tier"] != "admin" and user["id"] not in testers(con):
        return "AutoTrading is not available yet"
    if not (signal_feed.paid(user) or credits.entitled(user, "mt5")):
        return "AutoTrading is for paid members with MT5 connected"
    return ""


# ---------------------------------------------------------------- events and messages
def _event(con, aid: int, uid: int, kind: str, detail: str):
    """Audit log of eligibility decisions, tier changes, rejected attempts and automatic pauses (shown to admins)."""
    con.execute("INSERT INTO auto_events(account_id,user_id,kind,detail,created_at) VALUES(?,?,?,?,?)", (aid, uid, kind, str(detail)[:300], _now()))
    print(f"[auto] account {aid} {kind}: {str(detail)[:160]}", flush=True)


def _send_inbox(msgs: list):
    """Messages are sent only after the caller committed its own write (SQLite allows one writer)."""
    if not msgs:
        return
    from . import inbox
    try:
        with closing(inbox.db()) as ic:
            for uid, body in msgs:
                ic.insert("INSERT INTO inbox_messages(user_id,sender,admin_id,body,created_at) VALUES(?,?,?,?,?)", (uid, "admin", 0, "AutoTrading: " + body[:900], _now()))
            ic.commit()
    except Exception as e:
        print(f"[auto] inbox message failed: {type(e).__name__}", flush=True)


# ---------------------------------------------------------------- settings
SETS = ("styles", "instruments")
LOOSE_FIELDS = SETS + ("mode", "max_rating", "max_open", "max_per_day", "risk_pct", "max_risk_pct", "fixed_lots", "loss_limit")


def _row_settings(r) -> dict:
    s = dict(DEFAULTS)
    s.update({"paused_reason": None, "streak_since": 0, "pending": {}, "tier": None, "equity": None, "equity_at": None})
    if r:
        for k in ("enabled", "mode", "fixed_lots", "risk_pct", "max_risk_pct", "max_open", "max_per_day", "max_rating", "loss_limit",
                  "paused_reason", "streak_since", "tier", "equity", "equity_at"):
            if k in r.keys() and r[k] is not None:
                s[k] = r[k]
        for k in SETS:
            try:
                s[k] = [str(x) for x in json.loads(r[k] or "[]")]
            except ValueError:
                s[k] = []
        try:
            s["pending"] = json.loads(r["pending"] or "{}") if "pending" in r.keys() else {}
        except ValueError:
            s["pending"] = {}
    s["enabled"] = int(bool(s["enabled"]))
    return s


def _looser(f, cur, want) -> bool:
    if f in SETS:
        return bool(set(want) - set(cur))
    if f == "max_rating":
        return RATINGS.index(want) > RATINGS.index(cur)
    if f == "mode":
        return want != cur
    return _f(want) > _f(cur)


def _same(f, a, b) -> bool:
    return sorted(a) == sorted(b) if f in SETS else a == b


def merge(cur: dict, want: dict, old_pending: dict, now: int) -> tuple:
    """(settings that apply now, settings waiting for the delay). Anything that makes AutoTrading take more risk waits 24 hours;
    anything that makes it safer applies at once."""
    eff, pend = dict(want), {}
    for f in LOOSE_FIELDS:
        if _looser(f, cur[f], want[f]):
            eff[f] = [x for x in want[f] if x in cur[f]] if f in SETS else cur[f]
            prev = old_pending.get(f)
            at = prev["at"] if prev and _same(f, prev["v"], want[f]) else now + LOOSEN_DELAY
            pend[f] = {"v": want[f], "at": at}
    return eff, pend


def _write_cols(con, aid, s: dict):
    con.execute("UPDATE auto_settings SET styles=?, instruments=?, mode=?, fixed_lots=?, risk_pct=?, max_risk_pct=?, max_open=?, max_per_day=?, "
                "max_rating=?, loss_limit=?, pending=?, updated_at=? WHERE account_id=?",
                (json.dumps(s["styles"]), json.dumps(s["instruments"]), s["mode"], s["fixed_lots"], s["risk_pct"], s["max_risk_pct"], s["max_open"],
                 s["max_per_day"], s["max_rating"], s["loss_limit"], json.dumps(s["pending"]), _now(), aid))


def get_settings(con, aid: int) -> dict:
    """The settings in force. A delayed loosening that has matured is applied (and saved) here, so every reader sees the same thing."""
    s = _row_settings(con.execute("SELECT * FROM auto_settings WHERE account_id=?", (aid,)).fetchone())
    now, due = _now(), {f: p for f, p in s["pending"].items() if p.get("at", 0) <= _now()}
    if due:
        for f, p in due.items():
            s[f] = p["v"]
            del s["pending"][f]
        _write_cols(con, aid, s)
        con.commit()
    return s


def form_values(s: dict) -> dict:
    """What the form shows: the settings in force, with waiting changes laid over them, so saving again does not cancel a waiting change."""
    f = {k: s[k] for k in ("styles", "instruments", "mode", "fixed_lots", "risk_pct", "max_risk_pct", "max_open", "max_per_day", "max_rating", "loss_limit", "enabled")}
    for k, p in s["pending"].items():
        f[k] = p["v"]
    return f


def catalog() -> dict:
    from . import signal_feed as F
    have = {k[1] for k in F.allowed_combos()}
    if F.CFG.get("validated_enabled"):
        have |= {k[1] for k in F.VALIDATED}
    styles = [{"key": k, "label": STYLE_LABEL[k]} for k in STYLE_ORDER if k in have]
    paused = set(F.CFG.get("paused_instruments") or [])
    ins = [{"key": k, "name": v[1], "paused": k in paused} for k, v in INSTRUMENTS.items()]
    return {"styles": styles, "instruments": ins}


def eligibility(con, aid: int) -> dict:
    """What the member's account may trade right now, for the page and for the admin view."""
    st = equity_status(con, aid)
    keys = eligible_keys(st["tier"])
    return {"ok": st["ok"], "why": st["why"], "equity": st["equity"], "tier": st["tier"], "currency": st["currency"], "keys": sorted(keys),
            "gold": gold_label(st["tier"]) if st["tier"] else None, "locked": LOCKED_TEXT}


def protection_view(con, aid: int) -> dict:
    """Auto manage and Equity protection settings next to their recommended (default) values. AutoTrading never changes them."""
    m = ea.get_manage(con, aid)
    keys = [k for k in ea.MANAGE_DEFAULT if k not in ("excluded", "prot_pending")]
    changed = [{"key": k, "value": m.get(k), "recommended": ea.MANAGE_DEFAULT[k]} for k in keys if m.get(k) != ea.MANAGE_DEFAULT[k]]
    return {"is_default": not changed, "changed": changed, "prot_mode": m.get("prot_mode")}


# ---------------------------------------------------------------- 1. new signals -> waiting jobs
def on_new_signals(ids: list) -> int:
    """Called after each scan. One waiting job per enabled account whose choices match the signal. Returns how many were created."""
    from . import signal_feed as F
    made, now = 0, _now()
    if not ids:
        return 0
    with closing(F.db()) as fc:
        sigs = [dict(r) for sid in ids for r in fc.execute("SELECT * FROM signals WHERE id=?", (sid,)).fetchall()]
    with closing(ea.db()) as con:
        rows = [dict(r) for r in con.execute("SELECT account_id, user_id FROM auto_settings WHERE enabled=1").fetchall()]
        ok_user: dict = {}
        for r in rows:
            s = get_settings(con, r["account_id"])
            uid = r["user_id"]
            if not s["enabled"] or s["paused_reason"]:
                continue
            if uid not in ok_user:
                u = _user(uid)
                ok_user[uid] = bool(u and not allowed(con, u))
            if not ok_user[uid]:
                continue
            for sig in sigs:
                if sig["style"] not in s["styles"] or sig["instrument"] not in s["instruments"]:
                    continue
                if s["tier"] is not None and trade_allowed(s["tier"], sig["instrument"], sig["style"]):
                    continue                                                  # the last known tier does not allow it; dispatch re-checks live
                if RATINGS.index(sig["risk"] if sig["risk"] in RATINGS else "High") > RATINGS.index(s["max_rating"]):
                    continue
                if con.execute("SELECT 1 FROM auto_jobs WHERE signal_id=? AND account_id=?", (sig["id"], r["account_id"])).fetchone():
                    continue
                con.execute("INSERT INTO auto_jobs(signal_id,account_id,user_id,status,created_at,updated_at,symbol,dir) "
                            "VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(signal_id, account_id) DO NOTHING",
                            (sig["id"], r["account_id"], r["user_id"], "waiting", now, now, sig["instrument"],
                             "BUY" if sig["direction"] > 0 else "SELL"))
                made += 1
        con.commit()
    return made


# ---------------------------------------------------------------- 2. the EA asked for work
def _state(con, aid):
    r = con.execute("SELECT state, specs FROM ea_acct_state WHERE account_id=?", (aid,)).fetchone()
    if not r:
        return None, []
    return json.loads(r["state"]), [x for x in json.loads(r["specs"] or "[]") if isinstance(x, dict)]


def _tickets(st) -> set:
    return {int(_f(p.get("ticket"))) for p in (st or {}).get("positions", [])}


def _set(con, jid, **kw):
    kw["updated_at"] = _now()
    con.execute(f"UPDATE auto_jobs SET {','.join(f'{k}=?' for k in kw)} WHERE id=?", (*kw.values(), jid))


def _pick_symbol(specs: list, key: str, tier) -> tuple:
    """(spec, reason). Gold must be the micro contract in tiers 2-5 and the standard contract in tier 6; tier 7 takes either."""
    name = INSTRUMENTS.get(key, ("", key))[1]
    want = journal._norm(name)
    exact = [x for x in specs if journal._norm(str(x.get("symbol", ""))) == want]
    cands = exact + [x for x in specs if match_key(str(x.get("symbol", ""))) == key and x not in exact]        # exact name first, then broker variants
    if key == "xauusd" and tier in GOLD_H4_TIERS:
        need_micro = tier != 6
        cands = [x for x in cands if is_micro(x.get("symbol", "")) == need_micro]
        if not cands:
            return None, ("XAUUSDmicro was not found in your MT5 Market Watch" if need_micro else "XAUUSD (the standard contract) was not found in your MT5 Market Watch")
    return (cands[0], "") if cands else (None, f"{name} was not found in your MT5 Market Watch")


def dispatch(con, aid: int):
    """Called from poll(): turn at most ONE waiting auto job of this account into an open command (or skip it, saying why)."""
    from . import copy_trading, signal_feed as F
    now = _now()
    if con.execute("SELECT 1 FROM ea_commands WHERE account_id=? AND status IN ('pending','sent') AND created_at>?",
                   (aid, now - ea.CMD_TTL)).fetchone():
        return
    job = con.execute("SELECT * FROM auto_jobs WHERE account_id=? AND status='waiting' ORDER BY id LIMIT 1", (aid,)).fetchone()
    if not job:
        return
    job = dict(job)

    def skip(why, kind=None):
        _set(con, job["id"], status="skipped", note=why)
        if kind:
            _event(con, aid, job["user_id"], kind, f"signal {job['signal_id']}: {why}")
        con.commit()

    if now - job["created_at"] > JOB_TTL:
        return skip("Your EA was not connected in time, so this trade was skipped (auto trades are never placed late)")
    acct = con.execute("SELECT * FROM ea_accounts WHERE id=?", (aid,)).fetchone()
    s = get_settings(con, aid)
    user = _user(job["user_id"])
    if not acct or not s["enabled"]:
        return skip("AutoTrading is off for this account")
    if s["paused_reason"]:
        return skip("AutoTrading is paused until you resume it")
    if not user or allowed(con, user):
        return skip("AutoTrading is not available on your account")
    with closing(F.db()) as fc:
        sig = fc.execute("SELECT * FROM signals WHERE id=?", (job["signal_id"],)).fetchone()
    if not sig or sig["status"] != "open" or now > sig["expires_at"]:
        return skip("The setup was no longer fresh enough to trade")
    if sig["style"] not in s["styles"] or sig["instrument"] not in s["instruments"]:
        return skip("You changed your AutoTrading choices before this trade was placed")
    es = equity_status(con, aid, now)                                         # live check, from the EA's own report, before EVERY trade
    if not es["ok"]:
        return skip(es["why"], "rejected_equity")
    why = trade_allowed(es["tier"], sig["instrument"], sig["style"])
    if why:
        return skip(why, "rejected_tier")
    st, specs = _state(con, aid)
    acc = st["account"]
    if not acc.get("execute_allowed"):
        return skip("Trading from the app is not enabled on your account (AllowExecute is off)")
    rules = ea.get_rules(con, aid)
    if ea.risk_check(st, rules)["status"] == "STOP":
        return skip(f"You reached your own daily loss limit of {rules['daily_loss_pct']:g}%")
    if len(st["positions"]) >= rules["max_positions"]:
        return skip(f"You already have {len(st['positions'])} open positions, your own maximum")
    live = con.execute("SELECT COUNT(*) AS c FROM auto_jobs WHERE account_id=? AND status IN ('sent','open')", (aid,)).fetchone()["c"]
    if live >= s["max_open"]:
        return skip(f"{live} auto trades are already open, your AutoTrading maximum")
    today = con.execute("SELECT COUNT(*) AS c FROM auto_jobs WHERE account_id=? AND placed_at>=?", (aid, _day_start(now))).fetchone()["c"]
    if today >= s["max_per_day"]:
        return skip(f"{today} auto trades were already placed today, your AutoTrading daily maximum")
    name = INSTRUMENTS.get(sig["instrument"], ("", sig["instrument"]))[1]
    spec, why = _pick_symbol(specs, sig["instrument"], es["tier"])
    if not spec:
        return skip(why)
    if any(p.get("symbol") == spec["symbol"] for p in st["positions"]):
        return skip(f"You already have an open {spec['symbol']} trade, so AutoTrading did not add another")
    sd = abs(sig["entry"] - sig["stop"])
    cap = min([c for c in (_f(s["max_risk_pct"]), _f(rules.get("max_risk_pct"))) if c > 0] or [0])
    lots, pct, why = copy_trading.size(spec, _f(acc.get("balance")), sd, s["mode"], s["fixed_lots"], s["risk_pct"], cap)
    if lots is None:
        _set(con, job["id"], status="skipped", note=why, risk_pct=pct)
        con.commit()
        return
    blocked = ea.protect_precheck(acc, ea.get_manage(con, aid), spec, lots, sd, name)
    if blocked:
        return skip(blocked)
    payload = {"symbol": spec["symbol"], "dir": "BUY" if sig["direction"] > 0 else "SELL", "lots": lots, "sl_dist": sd,
               "tp_dist": abs(sig["target"] - sig["entry"]), "ref": sig["entry"], "maxdev": F.CFG["exec_max_dev_r"] * sd,
               "signal_id": sig["id"], "comment": f"TLA-{sig['id']}", "auto_job": job["id"]}
    cid = con.insert("INSERT INTO ea_commands(user_id,account_id,kind,ticket,created_at,status,payload) VALUES(?,?,?,?,?,'pending',?)",
                     (job["user_id"], aid, "open", None, now, json.dumps(payload)))
    _set(con, job["id"], status="sent", command_id=cid, lots=lots, risk_pct=pct, placed_at=now, symbol=spec["symbol"],
         dir=payload["dir"], before=json.dumps(sorted(_tickets(st))))
    con.commit()


# ---------------------------------------------------------------- 3. the EA reported
def _cmd_msg(con, cid) -> str:
    r = con.execute("SELECT result FROM ea_commands WHERE id=?", (cid,)).fetchone()
    try:
        return str(json.loads(r["result"]).get("message") or "") if r and r["result"] else ""
    except ValueError:
        return ""


def _money(v) -> str:
    return f"${v:,.2f}"


def _sync_equity(con, aid: int, uid: int, msgs: list):
    """Keep the stored tier current, log changes and tell the member. Existing trades are never touched."""
    row = con.execute("SELECT * FROM auto_settings WHERE account_id=?", (aid,)).fetchone()
    if not row:
        return
    es = equity_status(con, aid)
    now, old = _now(), row["tier"]
    if es["ok"]:
        if es["tier"] != old:
            con.execute("UPDATE auto_settings SET tier=?, equity=?, equity_at=? WHERE account_id=?", (es["tier"], es["equity"], now, aid))
            if old is not None:
                _event(con, aid, uid, "tier_change", f"equity {_money(es['equity'])}: tier {old} -> {es['tier']}")
                chosen = set(_row_settings(row)["instruments"])
                recent = con.execute("SELECT 1 FROM auto_events WHERE account_id=? AND kind='tier_msg' AND created_at>?", (aid, now - 3600)).fetchone()
                if chosen and not recent:
                    cut = sorted(chosen - eligible_keys(es["tier"]))
                    if es["tier"] < old:
                        body = (f"Your account equity is now {_money(es['equity'])} (tier {es['tier']}, was tier {old}). "
                                + (("New auto trades are paused on: " + ", ".join(INSTRUMENTS[k][1] for k in cut if k in INSTRUMENTS) + ". ") if cut else "")
                                + "Trades already open are not closed and keep being managed.")
                    else:
                        body = (f"Your account equity is now {_money(es['equity'])} (tier {es['tier']}, was tier {old}). More symbols can now be chosen "
                                "in the AutoTrading tab. None were switched on for you.")
                    _event(con, aid, uid, "tier_msg", body)
                    msgs.append((uid, body))
        elif not row["equity_at"] or row["equity_at"] < now - 30:
            con.execute("UPDATE auto_settings SET equity=?, equity_at=? WHERE account_id=?", (es["equity"], now, aid))


def on_report(con, aid: int, state: dict):
    now, have, msgs = _now(), _tickets(state), []
    uid = None
    for j in con.execute("SELECT * FROM auto_jobs WHERE account_id=? AND status IN ('sent','open')", (aid,)).fetchall():
        j = dict(j)
        uid = j["user_id"]
        if j["status"] == "sent":
            c = con.execute("SELECT status FROM ea_commands WHERE id=?", (j["command_id"],)).fetchone()
            if not c or c["status"] in ("failed", "expired"):
                _set(con, j["id"], status="failed", note=_cmd_msg(con, j["command_id"]) or "Your EA did not place the trade")
            elif c["status"] == "done":
                _set(con, j["id"], status="open")
                j["status"], j["updated_at"] = "open", now
                msgs.append((j["user_id"], f"placed a {j['dir']} {INSTRUMENTS.get(j['symbol'], ('', j['symbol']))[1] if j['symbol'] in INSTRUMENTS else j['symbol']} "
                                           f"trade, {j['lots']:g} lot" + (f", risking about {j['risk_pct']:g}% of your balance" if j["risk_pct"] is not None else "")
                                           + f" (signal #{j['signal_id']}). It has a stop loss and a target, and Auto manage and Equity protection apply."))
        if j["status"] == "open":
            if j["ticket"] is None:
                before = set(json.loads(j["before"] or "[]"))
                cand = [p for p in state.get("positions", []) if int(_f(p.get("ticket"))) not in before and p.get("symbol") == j["symbol"]
                        and str(p.get("type")).upper() == j["dir"]]
                if len(cand) > 1:
                    cand = [p for p in cand if abs(_f(p.get("volume")) - _f(j["lots"])) < 1e-6] or cand
                if len(cand) == 1:
                    _set(con, j["id"], ticket=int(_f(cand[0].get("ticket"))))
                elif not cand and now - j["updated_at"] > TICKET_WAIT:
                    _set(con, j["id"], status="closed")                      # opened and already closed (stop or target hit)
            elif j["ticket"] not in have:
                _set(con, j["id"], status="closed")
    if uid is None:
        r = con.execute("SELECT user_id FROM auto_settings WHERE account_id=?", (aid,)).fetchone()
        uid = r["user_id"] if r else None
    if uid is not None:
        _sync_equity(con, aid, uid, msgs)
    con.commit()
    _send_inbox(msgs)


def on_closed(aid: int, closed: list, offset: int = 0):
    """Called from the report with the positions MT5 says have closed. Records each auto trade's result, and pauses AutoTrading
    for the account after loss_limit losses in a row (breakeven exits are neutral; a win ends the run)."""
    if not closed:
        return
    msgs = []
    with closing(ea.db()) as con:
        jobs = [dict(r) for r in con.execute("SELECT * FROM auto_jobs WHERE account_id=? AND profit IS NULL AND status IN ('sent','open','closed') "
                                              "AND placed_at>?", (aid, _now() - 30 * 86400)).fetchall()]
        if not jobs:
            return
        for r in closed[:100]:
            try:
                pid, profit = int(r["id"]), float(r["profit"])
                opened, closed_at = int(r["opened"]) - offset, int(r["closed"]) - offset
            except (KeyError, TypeError, ValueError):
                continue
            if profit != profit:
                continue
            j = next((x for x in jobs if x["ticket"] == pid), None)
            if not j:
                c = [x for x in jobs if x["ticket"] is None and x["symbol"] == r.get("symbol") and x["dir"] == str(r.get("type")).upper()
                     and abs(opened - (x["placed_at"] or 0)) < 300]
                j = c[0] if len(c) == 1 else None
            if j:
                con.execute("UPDATE auto_jobs SET profit=?, closed_at=?, ticket=COALESCE(ticket,?), status='closed', updated_at=? WHERE id=? AND profit IS NULL",
                            (profit, closed_at, pid, _now(), j["id"]))
                jobs.remove(j)
        con.commit()
        row = con.execute("SELECT * FROM auto_settings WHERE account_id=?", (aid,)).fetchone()
        if row and row["enabled"]:
            s = get_settings(con, aid)
            st, _ = _state(con, aid)
            eps = LOSS_EPS * _f(((st or {}).get("account") or {}).get("balance"))
            run = 0
            for x in con.execute("SELECT profit FROM auto_jobs WHERE account_id=? AND profit IS NOT NULL AND closed_at>? "
                                 "ORDER BY closed_at DESC, id DESC LIMIT 20", (aid, int(s["streak_since"] or 0))).fetchall():
                if x["profit"] < -eps:
                    run += 1
                elif x["profit"] > eps:
                    break
            if run >= int(s["loss_limit"]):
                con.execute("UPDATE auto_settings SET enabled=0, paused_reason='losses', updated_at=? WHERE account_id=?", (_now(), aid))
                con.execute("UPDATE auto_jobs SET status='skipped', note='AutoTrading paused after losses in a row', updated_at=? "
                            "WHERE account_id=? AND status='waiting'", (_now(), aid))
                _event(con, aid, row["user_id"], "loss_pause", f"{run} losing auto trades in a row (limit {s['loss_limit']})")
                msgs.append((row["user_id"], f"paused itself after {run} losing auto trades in a row (your limit is {s['loss_limit']}). Open trades stay open and "
                                             "keep their protection. Open the AutoTrading tab and tick resume when you are ready."))
        con.commit()
    _send_inbox(msgs)


# ---------------------------------------------------------------- member API
def _job_out(j: dict) -> dict:
    sig_name = INSTRUMENTS[j["symbol"]][1] if j.get("symbol") in INSTRUMENTS else j.get("symbol")
    return {"id": j["id"], "signal_id": j["signal_id"], "account": j["account_id"], "status": j["status"], "note": j["note"] or "",
            "symbol": sig_name, "dir": j["dir"], "lots": j["lots"], "risk_pct": j["risk_pct"], "profit": j.get("profit"), "at": j["created_at"]}


async def me_get(request):
    u = auth.require_user(request)
    with closing(ea.db()) as con:
        why = allowed(con, u)
        if why:
            return JSONResponse({"available": False})
        accts = []
        for a in ea.user_accounts(con, u["id"]):
            s = get_settings(con, a["id"])
            old = con.execute("SELECT ack_at FROM auto_settings WHERE account_id=?", (a["id"],)).fetchone()
            accts.append({"id": a["id"], "label": a["label"], "settings": {k: s[k] for k in form_values(s)}, "form": form_values(s),
                          "pending": s["pending"], "paused_reason": s["paused_reason"], "acknowledged": bool(old and old["ack_at"]),
                          "own_limit_pct": ea.get_rules(con, a["id"])["max_risk_pct"], "eligibility": eligibility(con, a["id"]),
                          "protection": protection_view(con, a["id"])})
        jobs = [_job_out(dict(r)) for r in con.execute("SELECT * FROM auto_jobs WHERE user_id=? ORDER BY id DESC LIMIT 30", (u["id"],)).fetchall()]
    return JSONResponse({"available": True, "accounts": accts, "catalog": catalog(), "recent": jobs, "defaults": DEFAULTS, "delay_hours": LOOSEN_DELAY // 3600,
                         "tiers": {str(k): v for k, v in TIER_FROM.items()}, "ratings": list(RATINGS), "now": _now()})


def _num(d, k, lo, hi):
    v = d.get(k)
    if v in (None, ""):
        return None
    try:
        v = float(v)
    except (TypeError, ValueError):
        raise HTTPException(422, f"{k} must be a number")
    if not lo <= v <= hi or v != v:
        raise HTTPException(422, f"{k} must be between {lo:g} and {hi:g}")
    return v


def _int(d, k):
    lo, hi = LIMITS[k]
    try:
        v = int(d.get(k, DEFAULTS[k]))
    except (TypeError, ValueError):
        raise HTTPException(422, f"{k} must be a whole number")
    if not lo <= v <= hi:
        raise HTTPException(422, f"{k} must be between {lo} and {hi}")
    return v


async def me_settings(request):
    u = auth.require_user(request)
    d = await auth._json(request)
    cat = catalog()
    styles = d.get("styles") or []
    ins = d.get("instruments") or []
    if not isinstance(styles, list) or not isinstance(ins, list):
        raise HTTPException(422, "Choose signal types and symbols from the lists")
    styles, ins = list(dict.fromkeys(map(str, styles))), list(dict.fromkeys(map(str, ins)))
    if any(x not in {c["key"] for c in cat["styles"]} for x in styles):
        raise HTTPException(422, "One of those signal types is not available")
    if any(x not in {c["key"] for c in cat["instruments"]} for x in ins):
        raise HTTPException(422, "One of those symbols is not available")
    md = d.get("mode", "risk")
    if md not in MODES:
        raise HTTPException(422, "mode must be fixed or risk")
    rating = d.get("max_rating", DEFAULTS["max_rating"])
    if rating not in RATINGS:
        raise HTTPException(422, "Choose Low, Medium or High")
    fixed, risk, cap = _num(d, "fixed_lots", 0.0001, 1000), _num(d, "risk_pct", 0.05, 10), _num(d, "max_risk_pct", 0.05, 10)
    if md == "risk" and risk is None:
        raise HTTPException(422, "risk_pct is required for percentage risk")
    want = {"styles": styles, "instruments": ins, "mode": md, "fixed_lots": fixed, "risk_pct": risk if risk is not None else DEFAULTS["risk_pct"],
            "max_risk_pct": cap if cap is not None else DEFAULTS["max_risk_pct"], "max_open": _int(d, "max_open"), "max_per_day": _int(d, "max_per_day"),
            "max_rating": rating, "loss_limit": _int(d, "loss_limit")}
    enabled = bool(d.get("enabled"))
    if enabled and not (styles and ins):
        raise HTTPException(422, "Choose at least one signal type and one symbol before switching AutoTrading on")
    now = _now()
    with closing(ea.db()) as con:
        why = allowed(con, u)
        if why:
            raise HTTPException(403, why)
        a = ea.resolve(con, u["id"], ea._acct_param(request, d), explicit=True)
        if enabled or ins:                                    # the symbol rules are enforced here too: the browser is never trusted
            es = equity_status(con, a["id"], now)
            if not es["ok"]:
                if enabled:
                    raise HTTPException(409, es["why"] + ". AutoTrading can be switched on when your account equity can be verified.")
            else:
                bad = [k for k in ins if k not in eligible_keys(es["tier"])]
                if bad:
                    _event(con, a["id"], u["id"], "rejected_setting", "tried to choose " + ", ".join(bad) + f" at tier {es['tier']}")
                    con.commit()
                    raise HTTPException(422, ", ".join(INSTRUMENTS[k][1] for k in bad) + ": " + LOCKED_TEXT)
        row = con.execute("SELECT * FROM auto_settings WHERE account_id=?", (a["id"],)).fetchone()
        ack = row["ack_at"] if row and row["ack_at"] else None
        cur = get_settings(con, a["id"])
        if enabled and not ack:
            if d.get("acknowledge") is not True:
                raise HTTPException(422, "Please tick the box to confirm you understand AutoTrading places real trades for you")
            ack = now
        if enabled and cur["paused_reason"]:
            if d.get("resume") is not True:
                raise HTTPException(409, "AutoTrading paused itself after losses in a row. Tick 'resume' to confirm you want it back on.")
        if row and ack and row["ack_at"]:                      # set up before: anything that takes more risk waits 24 hours
            eff, pend = merge(cur, want, cur["pending"], now)
        else:                                                   # first set-up: nothing to loosen yet
            eff, pend = want, {}
        resume = enabled and bool(cur["paused_reason"])
        con.execute("INSERT INTO auto_settings(account_id,user_id,enabled,styles,instruments,mode,fixed_lots,risk_pct,max_risk_pct,max_open,max_per_day,"
                    "max_rating,loss_limit,pending,ack_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(account_id) DO NOTHING",
                    (a["id"], u["id"], 0, "[]", "[]", "risk", None, 1.0, 2.0, 3, 5, "Medium", 3, "{}", ack, now))
        eff["pending"] = pend
        _write_cols(con, a["id"], eff)
        con.execute("UPDATE auto_settings SET enabled=?, ack_at=?, paused_reason=?, streak_since=? WHERE account_id=?",
                    (int(enabled), ack, None if resume or (enabled and not cur["paused_reason"]) else cur["paused_reason"],
                     now if resume else int(cur["streak_since"] or 0), a["id"]))
        if resume:
            _event(con, a["id"], u["id"], "resume", "member resumed AutoTrading after a loss pause")
        if not enabled:                                       # pausing cancels anything still waiting
            con.execute("UPDATE auto_jobs SET status='skipped', note='You paused AutoTrading', updated_at=? WHERE account_id=? AND status='waiting'",
                        (now, a["id"]))
        con.commit()
        out = get_settings(con, a["id"])
    return JSONResponse({"ok": True, "account": a["id"], "settings": {k: out[k] for k in form_values(out)}, "pending": out["pending"],
                         "delay_hours": LOOSEN_DELAY // 3600})


# ---------------------------------------------------------------- admin
async def admin_get(request):
    auth.require_admin(request)
    now = _now()
    with closing(ea.db()) as con:
        ids = testers(con)
        m = mode(con)
        rows = [dict(r) for r in con.execute("SELECT s.account_id, s.user_id, s.enabled, s.instruments, s.styles, s.paused_reason, s.tier, s.equity, s.equity_at, "
                                             "a.label FROM auto_settings s JOIN ea_accounts a ON a.id=s.account_id ORDER BY s.enabled DESC, s.account_id")]
        accts = []
        for r in rows[:100]:
            es = equity_status(con, r["account_id"], now)
            try:
                chosen = json.loads(r["instruments"] or "[]")
            except ValueError:
                chosen = []
            accts.append({"account": r["account_id"], "user_id": r["user_id"], "label": r["label"], "enabled": bool(r["enabled"]), "paused": r["paused_reason"],
                          "equity": es["equity"], "tier": es["tier"], "stored_tier": r["tier"], "status": "ok" if es["ok"] else es["why"],
                          "last_sync": r["equity_at"], "symbols": [INSTRUMENTS[k][1] if k in INSTRUMENTS else k for k in chosen],
                          "not_allowed_now": [INSTRUMENTS[k][1] for k in chosen if k in INSTRUMENTS and es["ok"] and k not in eligible_keys(es["tier"])]})
        events = [dict(r) for r in con.execute("SELECT account_id, kind, detail, created_at FROM auto_events ORDER BY id DESC LIMIT 40").fetchall()]
        on = con.execute("SELECT COUNT(*) AS c FROM auto_settings WHERE enabled=1").fetchone()["c"]
        recent = con.execute("SELECT status, COUNT(*) AS c FROM auto_jobs WHERE created_at>? GROUP BY status", (now - 7 * 86400,)).fetchall()
    with closing(auth.db()) as ac:
        emails = [r["email"] for r in ac.execute(f"SELECT email FROM users WHERE id IN ({','.join('?' * len(ids))})", ids).fetchall()] if ids else []
        who = {r["id"]: r["email"] for r in ac.execute(f"SELECT id, email FROM users WHERE id IN ({','.join('?' * len({x['user_id'] for x in accts}))})",
                                                       list({x["user_id"] for x in accts})).fetchall()} if accts else {}
    for x in accts:
        x["email"] = who.get(x["user_id"], "")
        del x["user_id"]
    return JSONResponse({"mode": m, "testers": emails, "accounts_on": on, "jobs_7d": {r["status"]: r["c"] for r in recent}, "accounts": accts, "events": events})


async def admin_set(request):
    auth.require_admin(request)
    d = await auth._json(request)
    m = d.get("mode")
    if m not in ("off", "testers"):
        raise HTTPException(422, "mode must be off or testers. There is no public mode yet.")
    raw = d.get("testers") or []
    if not isinstance(raw, list) or len(raw) > 50:
        raise HTTPException(422, "testers must be a list of e-mail addresses (up to 50)")
    ids, missing = [], []
    with closing(auth.db()) as ac:
        for e in raw:
            r = ac.execute("SELECT id FROM users WHERE lower(email)=?", (str(e).strip().lower(),)).fetchone()
            (ids if r else missing).append(r["id"] if r else str(e))
    if missing:
        raise HTTPException(422, "No member with: " + ", ".join(missing[:5]))
    with closing(ea.db()) as con:
        for k, v in (("auto_mode", m), ("auto_testers", json.dumps(sorted(set(ids))))):
            con.execute("INSERT INTO ea_meta(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, v))
        if m == "off":
            con.execute("UPDATE auto_jobs SET status='skipped', note='AutoTrading was switched off', updated_at=? WHERE status='waiting'", (_now(),))
        con.commit()
    return await admin_get(request)


routes = [
    Route("/api/auto/me", me_get, methods=["GET"]),
    Route("/api/auto/settings", me_settings, methods=["POST"]),
    Route("/api/auto/admin", admin_get, methods=["GET"]),
    Route("/api/auto/admin", admin_set, methods=["POST"]),
]
