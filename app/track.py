"""Growth tracking: which ad or link brought which member, and how far each one got.

What is stored (nothing here identifies a person beyond the account they later create):
  * visits       a daily COUNT per source/medium/campaign/content (no IP address, no cookie id, no per-visitor row)
  * attribution  one row per new member: the campaign that brought them (last paid click within 30 days, else how they arrived)
  * funnel_events  one row per member per milestone we cannot work out from other tables (connected Deriv for trading)
The browser script static/track.js reads utm_source, utm_medium, utm_campaign, utm_content, utm_term and ref from the link,
keeps them for 30 days, counts the visit once per browser session, and leaves a cookie so the server can attribute the signup
(including signups that go through Continue with Deriv). Funnel stages are read from the tables the app already keeps.
"""
import json
import re
import time
from contextlib import closing
from datetime import datetime, timedelta, timezone
from urllib.parse import unquote

from starlette.exceptions import HTTPException
from starlette.responses import JSONResponse
from starlette.routing import Route

from . import auth, dbx

COOKIE = "dl_attr"
MAX_KEYS_PER_DAY = 400            # distinct campaign combinations counted per day (stops junk links filling the table)
SCHEMA = """
    CREATE TABLE IF NOT EXISTS attribution(user_id INTEGER PRIMARY KEY, source TEXT NOT NULL, medium TEXT NOT NULL,
        campaign TEXT NOT NULL, content TEXT NOT NULL, term TEXT, referrer TEXT, landing TEXT, created_at BIGINT NOT NULL);
    CREATE TABLE IF NOT EXISTS visits(day TEXT NOT NULL, source TEXT NOT NULL, medium TEXT NOT NULL, campaign TEXT NOT NULL,
        content TEXT NOT NULL, n INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(day, source, medium, campaign, content));
    CREATE TABLE IF NOT EXISTS funnel_events(user_id INTEGER NOT NULL, event TEXT NOT NULL, created_at BIGINT NOT NULL,
        PRIMARY KEY(user_id, event))"""
EVENTS = {"deriv_connected"}
_BAD = re.compile(r"[^a-z0-9_.+\- ]")
_hits: dict[str, list[float]] = {}


def db():
    auth.db().close()
    return dbx.connect("track", SCHEMA)


def _clean(v, n=60) -> str:
    return _BAD.sub("", str(v or "").strip().lower())[:n].strip()


def normalize(raw) -> dict:
    """Whatever the browser sent, reduced to a safe, small, always-complete record."""
    raw = raw if isinstance(raw, dict) else {}
    referrer = _clean(raw.get("referrer"))
    source = _clean(raw.get("source")) or _clean(raw.get("ref")) or referrer or "direct"
    return {"source": source, "medium": _clean(raw.get("medium")) or "none", "campaign": _clean(raw.get("campaign")) or "none",
            "content": _clean(raw.get("content")) or "none", "term": _clean(raw.get("term")) or None,
            "referrer": referrer or None, "landing": _clean(raw.get("landing"), 80) or None}


def from_request(request) -> dict:
    try:
        return normalize(json.loads(unquote(request.cookies.get(COOKIE, "") or "null")))
    except Exception:
        return normalize({})


def record_signup(uid: int, request):
    a = from_request(request)
    with closing(db()) as con:
        con.execute("INSERT INTO attribution(user_id,source,medium,campaign,content,term,referrer,landing,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(user_id) DO NOTHING",
                    (uid, a["source"], a["medium"], a["campaign"], a["content"], a["term"], a["referrer"], a["landing"],
                     int(time.time())))
        con.commit()


def record_event(uid: int, event: str):
    if event not in EVENTS:
        return
    with closing(db()) as con:
        con.execute("INSERT INTO funnel_events(user_id,event,created_at) VALUES(?,?,?) ON CONFLICT(user_id,event) DO NOTHING",
                    (uid, event, int(time.time())))
        con.commit()


def _ip(request) -> str:
    fwd = request.headers.get("x-forwarded-for", "")
    return (fwd.split(",")[0].strip() or (request.client.host if request.client else "?"))[:60]


def _rate_ok(ip: str) -> bool:
    now = time.time()
    h = [t for t in _hits.get(ip, []) if now - t < 600]
    if len(h) >= 40:
        _hits[ip] = h
        return False
    h.append(now)
    _hits[ip] = h
    if len(_hits) > 5000:
        _hits.clear()
    return True


async def visit(request):
    """Counts one visit for a campaign. Public, tiny, and never fails the page."""
    if not _rate_ok(_ip(request)):
        return JSONResponse({"ok": True})
    try:
        a = normalize(await request.json())
    except Exception:
        a = normalize({})
    day = auth._today()
    with closing(db()) as con:
        key = (day, a["source"], a["medium"], a["campaign"], a["content"])
        known = con.execute("SELECT 1 FROM visits WHERE day=? AND source=? AND medium=? AND campaign=? AND content=?", key).fetchone()
        if known or con.execute("SELECT COUNT(*) AS c FROM visits WHERE day=?", (day,)).fetchone()["c"] < MAX_KEYS_PER_DAY:
            con.execute("INSERT INTO visits(day,source,medium,campaign,content,n) VALUES(?,?,?,?,?,1) "
                        "ON CONFLICT(day,source,medium,campaign,content) DO UPDATE SET n=visits.n+1", key)
            con.commit()
    return JSONResponse({"ok": True})


def _user_ids(db_factory, sql: str) -> set:
    try:
        with closing(db_factory()) as con:
            return {r["user_id"] for r in con.execute(sql).fetchall() if r["user_id"] is not None}
    except Exception as e:
        print(f"[track] could not read a funnel stage: {type(e).__name__}", flush=True)
        return set()


async def funnel(request):
    """Admin: per campaign, how many visited, signed up, and reached each next step."""
    auth.require_admin(request)
    try:
        days = max(1, min(365, int(request.query_params.get("days", 30))))
    except ValueError:
        days = 30
    since = int(time.time()) - days * 86400
    since_day = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
    from . import deriv_login, deriv_trade, signal_feed
    opened = _user_ids(signal_feed.db, "SELECT DISTINCT user_id FROM signal_opens")
    traded = _user_ids(deriv_trade.db, "SELECT DISTINCT user_id FROM deriv_trades WHERE status='placed'")
    verified = _user_ids(deriv_login.db, "SELECT user_id FROM deriv_accounts WHERE user_id IS NOT NULL")
    with closing(db()) as con:
        vis = con.execute("SELECT source,medium,campaign,content,SUM(n) AS n FROM visits WHERE day>=? "
                          "GROUP BY source,medium,campaign,content", (since_day,)).fetchall()
        attr = con.execute("SELECT user_id,source,medium,campaign,content FROM attribution WHERE created_at>=?", (since,)).fetchall()
        connected = {r["user_id"] for r in con.execute("SELECT user_id FROM funnel_events WHERE event='deriv_connected'").fetchall()}
        tracked_total = con.execute("SELECT COUNT(*) AS c FROM attribution").fetchone()["c"]
    with closing(auth.db()) as con:
        users_total = con.execute("SELECT COUNT(*) AS c FROM users").fetchone()["c"]
    rows: dict = {}

    def row(k):
        return rows.setdefault(k, {"source": k[0], "medium": k[1], "campaign": k[2], "content": k[3], "visits": 0,
                                   "signups": 0, "verified": 0, "opened": 0, "connected": 0, "traded": 0})
    for v in vis:
        row((v["source"], v["medium"], v["campaign"], v["content"]))["visits"] += int(v["n"] or 0)
    for a in attr:
        r = row((a["source"], a["medium"], a["campaign"], a["content"]))
        u = a["user_id"]
        r["signups"] += 1
        r["verified"] += u in verified
        r["opened"] += u in opened
        r["connected"] += u in connected
        r["traded"] += u in traded
    out = sorted(rows.values(), key=lambda r: (-r["signups"], -r["visits"]))
    tot = {k: sum(r[k] for r in out) for k in ("visits", "signups", "verified", "opened", "connected", "traded")}
    return JSONResponse({"days": days, "rows": out, "totals": tot, "untracked_users": max(0, users_total - tracked_total)})


routes = [
    Route("/api/track/visit", visit, methods=["POST"]),
    Route("/api/admin/funnel", funnel, methods=["GET"]),
]
