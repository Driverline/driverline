"""Daily Telegram recap: one post a day in the TradeLens channel with the trades that closed that day, how each ended, and a short
discipline or encouragement line that depends on how the day went.

What it covers: the trades the Companion EA imported into the journal from your LIVE MT5 account(s) only. A demo account, an account whose type
is unknown, and trades typed into the journal by hand are never included: this channel is public. To name the accounts yourself, set
RECAP_MT5_LOGINS (comma-separated MT5 logins); that list then replaces the live-only rule. Admin users: all admins, or only RECAP_USER_IDS.
A trade counts on the day (in RECAP_TZ_OFFSET time, default Lagos UTC+1) it CLOSED. Open positions are not in the journal, so they are not listed.
With no account selected nothing is posted (a recap with nothing behind it would mislead).

Each line: instrument, Buy/Sell, an emoji for the RESULT (green tick profit, red cross loss, minus breakeven: the same whatever way the trade
closed, so the day can be read at a glance), how it ended, and the result in R when it is reliable. Money is never shown (same rule as the EA's posts).
  Take profit / Stop loss / Trailing stop in profit / Breakeven / Closed manually / Stopped out by the broker / Closed in profit or at a loss.
  The reason comes from MT5 through the EA and is kept on the trade from now on (journal.trades.reason). Older trades have none: they are
  judged from their stop and target when those were saved, otherwise they are only called "closed in profit" or "closed at a loss",
  never "take profit" by guess. Breakeven means the trade closed at its entry price (within 0.01%).

How it is switched on (all settings live in Render, none in the code):
  TELEGRAM_BOT_TOKEN      the bot token (the same bot as the EA's). A secret: never logged, never shown, never sent to a browser.
  TELEGRAM_RECAP_CHAT     the channel, like @yourchannel or -100123456789. The bot must be able to post there.
  DAILY_RECAP=1           switches the schedule on. Without it nothing is ever posted automatically (the admin can still post by hand).
  RECAP_HOUR              local hour to post after (default 21). RECAP_TZ_OFFSET hours from UTC (default 1). RECAP_USER_IDS optional.
  RECAP_MT5_LOGINS        optional: exactly these MT5 logins feed the recap (default: every live account).

Safeguards: one post per day, recorded in recap_log before sending, so a restart or a second copy of the app cannot post it twice.
A failed send is retried every 10 minutes (up to 6 times). If the service was asleep at the posting time, the missed day is posted late,
but only for yesterday and only when a recap was posted on an earlier day, so switching it on never posts an old day.
"""
import asyncio
import json
import time
from contextlib import closing
from datetime import datetime, timedelta, timezone
import os

import httpx
from starlette.exceptions import HTTPException
from starlette.responses import JSONResponse
from starlette.routing import Route

from . import auth, dbx
from .config import INSTRUMENTS

SCHEMA = """
    CREATE TABLE IF NOT EXISTS recap_log(day TEXT PRIMARY KEY, status TEXT NOT NULL, claimed_at BIGINT NOT NULL, sent_at BIGINT,
        message_id TEXT, trades INTEGER NOT NULL DEFAULT 0, attempts INTEGER NOT NULL DEFAULT 0, error TEXT)"""

BE_PCT = 0.0001            # closed within 0.01% of its entry price = breakeven
MAX_ROWS = 25              # trades listed in one post (Telegram allows 4096 characters)
STALE_SENDING = 900        # a claim that never finished is taken over after 15 minutes
RETRY_AFTER = 600
MAX_ATTEMPTS = 6
FOOTER = "Educational only, not financial advice. Use your own lot size and risk."
_TRANSPORT = None          # tests plug a fake Telegram in here (httpx.MockTransport)
_warned = [0.0]


class NoAccount(Exception):
    """No MT5 account is selected for the recap."""


class RecapError(Exception):
    """Carries a message that is safe to show: it never contains the bot token."""


# ---------------------------------------------------------------- settings
def _env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


def _token() -> str:
    return _env("TELEGRAM_BOT_TOKEN")


def _chat() -> str:
    return _env("TELEGRAM_RECAP_CHAT")


def _int_env(name: str, default: int, lo: int, hi: int) -> int:
    try:
        return min(hi, max(lo, int(_env(name, str(default)))))
    except ValueError:
        return default


def hour() -> int:
    return _int_env("RECAP_HOUR", 21, 0, 23)


def tz_offset() -> int:
    return _int_env("RECAP_TZ_OFFSET", 1, -12, 14)


def can_post() -> bool:
    return bool(_token() and _chat())


def enabled() -> bool:
    """The schedule runs only when it was switched on AND the bot token and channel are set."""
    return _env("DAILY_RECAP") == "1" and can_post()


def status() -> dict:
    return {"switched_on": _env("DAILY_RECAP") == "1", "token_set": bool(_token()), "chat_set": bool(_chat()),
            "hour": hour(), "tz_offset": tz_offset(), "scheduled": enabled(), "can_post": can_post()}


def db():
    auth.db().close()
    return dbx.connect("recap", SCHEMA)


# ---------------------------------------------------------------- days
def local_day(ts: float = None) -> str:
    """The calendar day (YYYY-MM-DD) in the recap's own time zone."""
    t = datetime.fromtimestamp(time.time() if ts is None else ts, timezone.utc) + timedelta(hours=tz_offset())
    return t.strftime("%Y-%m-%d")


def _bounds(day: str):
    start = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=timezone.utc) - timedelta(hours=tz_offset())
    return start, start + timedelta(days=1)


def _recap_users() -> list:
    raw = _env("RECAP_USER_IDS")
    if raw:
        return sorted({int(x) for x in raw.replace(" ", "").split(",") if x.isdigit()})
    with closing(auth.db()) as con:
        return [r["id"] for r in con.execute("SELECT id FROM users WHERE tier='admin' AND blocked=0").fetchall()]


def _login_of(t: dict) -> str:
    """The MT5 login a journal trade came from (its mt5_id is "login:position"); '' for a trade typed in by hand."""
    return str(t.get("mt5_id") or "").split(":", 1)[0].strip() if t.get("mt5_id") else ""


def accounts() -> list:
    """The MT5 accounts of the recap users, and whether the EA last reported each one as a LIVE account. Anything that is not clearly
    live (demo, never reported, unreadable) counts as NOT live: a demo account's trades must never reach the public channel."""
    from . import ea
    ids = _recap_users()
    if not ids:
        return []
    with closing(ea.db()) as con:
        rows = con.execute(f"SELECT a.id, a.user_id, a.label, a.login, s.state FROM ea_accounts a LEFT JOIN ea_acct_state s ON s.account_id=a.id "
                           f"WHERE a.user_id IN ({','.join('?' * len(ids))}) ORDER BY a.id", ids).fetchall()
    out = []
    for r in rows:
        live = False
        try:
            acc = (json.loads(r["state"]) or {}).get("account") or {}
            live = acc.get("demo") is not None and not acc["demo"]
        except (TypeError, ValueError, AttributeError):
            pass
        out.append({"id": r["id"], "label": r["label"], "login": str(r["login"] or "").strip(), "live": live})
    return out


def included_logins() -> set:
    """Whose trades the recap may show: the logins listed in RECAP_MT5_LOGINS, or by default every LIVE account."""
    raw = _env("RECAP_MT5_LOGINS")
    if raw:
        return {x.strip() for x in raw.split(",") if x.strip()}
    return {a["login"] for a in accounts() if a["live"] and a["login"]}


def _rows_for_day(day: str) -> list:
    """Every EA-imported trade of the recap users that closed on that day, from any account, oldest first."""
    from . import journal
    ids = _recap_users()
    if not ids:
        return []
    start, end = _bounds(day)
    with closing(journal.db()) as con:
        rows = con.execute(f"SELECT * FROM trades WHERE status='closed' AND mt5_id IS NOT NULL AND closed_at>=? AND closed_at<? "
                           f"AND user_id IN ({','.join('?' * len(ids))}) ORDER BY closed_at, id",
                           (start.isoformat(timespec="seconds"), end.isoformat(timespec="seconds"), *ids)).fetchall()
    return [dict(r) for r in rows]


def trades_for_day(day: str) -> list:
    """The trades that closed that day on the accounts the recap may show, oldest first."""
    logins = included_logins()
    return [t for t in _rows_for_day(day) if _login_of(t) in logins] if logins else []


def _tail(login: str) -> str:
    return "\u2026" + login[-3:] if login else "?"


def account_overview(day: str) -> list:
    """For the admin: every account that has trades, with whether it feeds the recap and how many trades it has that day."""
    logins = included_logins()
    rows = _rows_for_day(day)
    seen, out = set(), []
    for a in accounts():
        seen.add(a["login"])
        out.append({"label": a["label"], "login": _tail(a["login"]), "live": a["live"], "included": a["login"] in logins,
                    "trades": sum(1 for t in rows if _login_of(t) == a["login"])})
    for lg in sorted({_login_of(t) for t in rows} - seen):               # an account that is no longer listed but still has trades
        out.append({"label": "(account no longer listed)", "login": _tail(lg), "live": False, "included": lg in logins,
                    "trades": sum(1 for t in rows if _login_of(t) == lg)})
    return out


# ---------------------------------------------------------------- how a trade ended
def _num(v):
    try:
        x = float(v)
        return x if x == x and abs(x) != float("inf") else None
    except (TypeError, ValueError):
        return None


def outcome(t: dict) -> str:
    """tp, sl, trail (stop hit in profit), be, so, manual or closed (reason unknown)."""
    entry, ex, pnl = _num(t.get("entry")), _num(t.get("exit_price")), _num(t.get("pnl")) or 0.0
    reason = str(t.get("reason") or "").lower()
    if entry and ex is not None and abs(ex - entry) <= abs(entry) * BE_PCT:
        return "be"
    if reason == "tp":
        return "tp"
    if reason == "sl":
        return "trail" if pnl > 0 else "sl"
    if reason == "so":
        return "so"
    if reason in ("manual", "expert"):
        return "manual"
    if not reason:                                  # older trade: only what its saved stop and target prove
        from . import closed_alerts
        c = closed_alerts.classify({"direction": t.get("direction"), "entry": entry, "exit": ex,
                                    "sl": t.get("stop"), "tp": t.get("target")})
        if c == "tp":
            return "tp"
        if c == "sl":
            return "trail" if pnl > 0 else "sl"
    return "closed"


def _sign(t: dict, code: str) -> int:
    if code == "be":
        return 0
    v = _num(t.get("pnl"))
    if v is None:
        v = _num(t.get("r_multiple")) or 0.0
    return (v > 0) - (v < 0)


EMOJI = {1: "✅", -1: "❌", 0: "➖"}      # profit / loss / breakeven or flat: every line starts with its RESULT, however the trade closed


def describe(t: dict) -> dict:
    code = outcome(t)
    sign = _sign(t, code)
    label = {"tp": "Take profit", "sl": "Stop loss", "trail": "Trailing stop, in profit", "be": "Breakeven",
             "so": "Stopped out by the broker", "manual": "Closed manually",
             "closed": "Closed " + ("in profit" if sign > 0 else "at a loss" if sign < 0 else "flat")}[code]
    if code == "manual":
        label += " in profit" if sign > 0 else " at a loss" if sign < 0 else ""
    r = _num(t.get("r_multiple"))
    reliable = code in ("tp", "sl", "manual", "closed") and r is not None   # a trailed or moved stop makes R misleading
    key = t.get("instrument")
    name = INSTRUMENTS[key][1] if key in INSTRUMENTS else str(key or "Trade")
    return {"name": name, "side": "Buy" if t.get("direction") == "BUY" else "Sell", "code": code, "sign": sign,
            "emoji": EMOJI[sign], "label": label, "r": r if reliable else None, "reliable": reliable or code == "be"}


# ---------------------------------------------------------------- the day's message
COACH = {
    "none": ["No trades closed today. Sitting out is a decision too: patience is part of discipline. Wait for setups that meet your rules.",
             "A quiet day. Not trading when there is nothing to trade is a skill. Use the time to review your journal."],
    "strong": ["A strong, disciplined day. Keep doing what got you here: same rules, same risk, no extra trades because you feel confident.",
               "A good day comes from following the plan, not from luck. Do not raise your risk tomorrow because of today.",
               "Well done. Note what you did right so you can repeat it, then switch off and rest."],
    "green": ["A positive day. Stay consistent: the edge is in repeating the process, not in one good day.",
              "Good work staying patient. Review your entries tonight and keep the same risk tomorrow."],
    "even": ["An even day: the wins and the losses balanced out. What counts is that you followed your rules. Review each trade and keep your risk the same.",
             "Wins and losses cancelled out today. That is normal. Stay patient, protect your capital and wait for clean setups."],
    "flat": ["A flat day is fine. Protecting your capital is a win, and breakeven exits mean your risk management is working.",
             "Not every day pays. You managed risk and stayed in the game. Reset and wait for clean setups."],
    "red": ["A tough day, and that is part of trading. Do not chase losses or increase your size to win it back. Review each trade, then rest.",
            "Losses happen to every trader. What matters is following your rules. Check whether each loss was a good setup that did not work, or a rule you bent."],
    "heavy": ["A hard day. Close the charts and step away. No revenge trades. Review your journal tomorrow with a clear head, and trade smaller until you are back in rhythm.",
              "Heavy losses call for a pause, not a bigger bet. Protect your capital, rest, and come back to your plan tomorrow."],
}


def tier(wins: int, losses: int, flat: int) -> str:
    n = wins + losses + flat
    if n == 0:
        return "none"
    if (wins == 0 and losses >= 3) or losses - wins >= 3:
        return "heavy"
    if losses > wins:
        return "red"
    if wins > losses:
        return "strong" if wins >= 2 and wins / (wins + losses) >= 0.65 else "green"
    return "even" if wins else "flat"


def coach(wins: int, losses: int, flat: int, day: str) -> str:
    pool = COACH[tier(wins, losses, flat)]
    return pool[datetime.strptime(day, "%Y-%m-%d").toordinal() % len(pool)]      # rotates day by day, same text for the same day


def build(day: str, trades: list) -> str:
    rows = [describe(t) for t in trades]
    wins = sum(1 for d in rows if d["sign"] > 0)
    losses = sum(1 for d in rows if d["sign"] < 0)
    flat = len(rows) - wins - losses
    pretty = datetime.strptime(day, "%Y-%m-%d").strftime("%a %d %b %Y")
    out = [f"\U0001F4CA TradeLens Daily Recap\n{pretty}", ""]
    for i, d in enumerate(rows[:MAX_ROWS], 1):
        out.append(f"{i}. {d['name']} · {d['side']} · {d['emoji']} {d['label']}" +(f" {d['r']:+.1f}R" if d["r"] is not None else ""))
    if len(rows) > MAX_ROWS:
        out.append(f"...and {len(rows) - MAX_ROWS} more")
    if rows:
        out += ["", f"Closed today: {len(rows)} · {wins} in profit · {losses} at a loss · {flat} breakeven/flat"]
        if all(d["reliable"] for d in rows) and any(d["r"] is not None for d in rows):
            out.append(f"Net result: {sum(d['r'] or 0.0 for d in rows):+.1f}R")
    else:
        out.append("No trades closed today.")
    out += ["", "\U0001F4AC " + coach(wins, losses, flat, day), "", FOOTER]
    return "\n".join(out)[:4000]


# ---------------------------------------------------------------- the log: one post per day
def _claim(day: str, now: int, force: bool = False, again: bool = False) -> bool:
    """Reserves the day before anything is sent. False means someone else has it, it is already posted, or it gave up."""
    with closing(db()) as con:
        try:
            con.execute("INSERT INTO recap_log(day,status,claimed_at) VALUES(?,?,?)", (day, "sending", now))
            con.commit()
            return True
        except Exception as e:
            if not dbx.is_duplicate(e):
                raise
            con.raw.rollback()
        if force:                                   # an admin pressed the button: allowed again only when they confirmed a repeat
            cur = con.execute("UPDATE recap_log SET status='sending', claimed_at=? WHERE day=? AND (status<>'sent' OR ?=1)",
                              (now, day, 1 if again else 0))
        else:
            cur = con.execute("UPDATE recap_log SET status='sending', claimed_at=? WHERE day=? AND attempts<? AND "
                              "((status='sending' AND claimed_at<?) OR (status='failed' AND claimed_at<?))",
                              (now, day, MAX_ATTEMPTS, now - STALE_SENDING, now - RETRY_AFTER))
        con.commit()
        return cur.rowcount == 1


def _finish(day: str, now: int, ok: bool, message_id: str = None, n: int = 0, error: str = None):
    with closing(db()) as con:
        if ok:
            con.execute("UPDATE recap_log SET status='sent', sent_at=?, message_id=?, trades=?, error=NULL WHERE day=?",
                        (now, message_id, n, day))
        else:
            con.execute("UPDATE recap_log SET status='failed', claimed_at=?, attempts=attempts+1, error=? WHERE day=?",
                        (now, (error or "failed")[:200], day))
        con.commit()


def _worked_before(day: str) -> bool:
    """True when a recap was posted on some day BEFORE `day`: proof the recap was already running, so a missed `day` is worth making up."""
    with closing(db()) as con:
        return bool(con.execute("SELECT 1 FROM recap_log WHERE status='sent' AND day<? LIMIT 1", (day,)).fetchone())


def _unsent(day: str) -> bool:
    """True when that day has no post yet: no row, or one that failed or never finished (the claim rules decide when to retry)."""
    with closing(db()) as con:
        r = con.execute("SELECT status FROM recap_log WHERE day=?", (day,)).fetchone()
    return not r or r["status"] != "sent"


# ---------------------------------------------------------------- Telegram
async def send_telegram(text: str) -> str:
    """Posts to the channel and returns the message id. Errors carry Telegram's own description, never the URL (it holds the token)."""
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(15.0, connect=8.0), transport=_TRANSPORT) as cl:
            r = await cl.post(f"https://api.telegram.org/bot{_token()}/sendMessage",
                              json={"chat_id": _chat(), "text": text, "disable_web_page_preview": True})
    except httpx.HTTPError as e:
        raise RecapError(f"Could not reach Telegram ({type(e).__name__})")
    try:
        d = r.json()
    except ValueError:
        d = {}
    if r.status_code != 200 or not d.get("ok"):
        raise RecapError("Telegram refused the post: " + str(d.get("description") or f"HTTP {r.status_code}")[:120])
    return str((d.get("result") or {}).get("message_id") or "")


async def post_day(day: str, now: int = None, force: bool = False, again: bool = False) -> dict:
    now = int(now or time.time())
    if not await asyncio.to_thread(included_logins):
        raise NoAccount("No MT5 account is selected for the recap (no LIVE account found). Set RECAP_MT5_LOGINS in Render to the account(s) you post from")
    trades = await asyncio.to_thread(trades_for_day, day)
    text = build(day, trades)
    if not await asyncio.to_thread(_claim, day, now, force, again):
        return {"sent": False, "day": day, "why": "already posted or being posted"}
    try:
        mid = await send_telegram(text)
    except RecapError as e:
        await asyncio.to_thread(_finish, day, now, False, None, 0, str(e))
        raise
    except Exception as e:                           # never leave a day stuck as "sending"
        await asyncio.to_thread(_finish, day, now, False, None, 0, type(e).__name__)
        raise RecapError(f"Unexpected error ({type(e).__name__})")
    await asyncio.to_thread(_finish, day, now, True, mid, len(trades))
    return {"sent": True, "day": day, "trades": len(trades), "message_id": mid}


async def tick(now: float = None) -> list:
    """One look at the clock. Posts today's recap once the hour has come, and yesterday's if it was missed (see the module notes)."""
    if not enabled():
        return []
    now = time.time() if now is None else now
    if not await asyncio.to_thread(included_logins):               # a recap with no account behind it would be misleading: say nothing, warn rarely
        if now - _warned[0] > 3600:
            _warned[0] = now
            print("[recap] no live MT5 account selected: nothing posted (set RECAP_MT5_LOGINS)", flush=True)
        return []
    today = local_day(now)
    local = datetime.fromtimestamp(now, timezone.utc) + timedelta(hours=tz_offset())
    due = []
    yesterday = (local - timedelta(days=1)).strftime("%Y-%m-%d")
    if await asyncio.to_thread(_worked_before, yesterday) and await asyncio.to_thread(_unsent, yesterday):
        due.append(yesterday)
    if local.hour >= hour() and await asyncio.to_thread(_unsent, today):
        due.append(today)
    done = []
    for day in due:
        try:
            r = await post_day(day, int(now))
            if r["sent"]:
                done.append(day)
        except RecapError as e:
            print(f"[recap] {day} not posted: {e}", flush=True)
    return done


async def loop():
    await asyncio.sleep(45)
    while True:
        try:
            await tick()
        except Exception as e:
            print(f"[recap] tick failed: {type(e).__name__}", flush=True)
        await asyncio.sleep(60)


# ---------------------------------------------------------------- admin
async def admin_view(request):
    auth.require_admin(request)
    day = local_day()
    trades = await asyncio.to_thread(trades_for_day, day)
    accts = await asyncio.to_thread(account_overview, day)
    with closing(db()) as con:
        log = [dict(r) for r in con.execute("SELECT day,status,sent_at,trades,attempts,error FROM recap_log ORDER BY day DESC LIMIT 7").fetchall()]
    return JSONResponse({"settings": status(), "day": day, "count": len(trades), "preview": build(day, trades), "log": log,
                         "accounts": accts, "explicit": bool(_env("RECAP_MT5_LOGINS"))})


async def admin_send(request):
    auth.require_admin(request)
    if not can_post():
        raise HTTPException(409, "Set TELEGRAM_BOT_TOKEN and TELEGRAM_RECAP_CHAT in Render first")
    d = await auth._json(request)
    try:
        r = await post_day(local_day(), force=True, again=d.get("again") is True)
    except NoAccount as e:
        raise HTTPException(409, str(e))
    except RecapError as e:
        raise HTTPException(502, str(e))
    if not r["sent"]:
        raise HTTPException(409, "Today's recap is already posted or being posted. Confirm to post it again.")
    return JSONResponse({"ok": True, **r})


routes = [
    Route("/api/admin/recap", admin_view, methods=["GET"]),
    Route("/api/admin/recap/send", admin_send, methods=["POST"]),
]
