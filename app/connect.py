"""TradeLens Connect: the link between a Telegram user and a TradeLens account, and the funnel events around it.

Steps 1 and 3 of the Phase 1 build: the two tables and the Connect token. Nothing here is wired into app/main.py yet, so the live
app is unchanged. Later steps add the entitlements and the /api/connect/v1 routes to this module (the initData check is telegram_initdata.py).

  * telegram_links: one row per link. At most ONE active (not revoked) row per Telegram ID and per TradeLens user, enforced by
    the database itself. Unlinking sets revoked_at and keeps the row for audit. The Telegram numeric ID is the identity, never
    the username (it can change). No phone number, contacts or message content is ever stored.
  * connect_events: the funnel events (EVENTS below). props is small JSON text and never holds personal data or chat content.

Same pattern as credits.py and wallet.py: the app creates these tables itself on first use; on Postgres dbx also switches row-level
security on, so Supabase's public REST API cannot read them. Mirrored by migrations/2026_11_connect.sql."""
import base64
import hashlib
import hmac
import json
import os
import time
from contextlib import closing

from starlette.exceptions import HTTPException
from starlette.responses import JSONResponse
from starlette.routing import Route

from . import auth, dbx, telegram_initdata

SCHEMA = """
    CREATE TABLE IF NOT EXISTS telegram_links(id {ID}, user_id INTEGER NOT NULL, telegram_id BIGINT NOT NULL,
        telegram_username TEXT, source TEXT NOT NULL, linked_at BIGINT NOT NULL, last_seen_at BIGINT NOT NULL,
        revoked_at BIGINT);
    CREATE UNIQUE INDEX IF NOT EXISTS ux_tglink_telegram ON telegram_links(telegram_id) WHERE revoked_at IS NULL;
    CREATE UNIQUE INDEX IF NOT EXISTS ux_tglink_user ON telegram_links(user_id) WHERE revoked_at IS NULL;
    CREATE TABLE IF NOT EXISTS connect_events(id {ID}, telegram_id BIGINT, user_id INTEGER, event TEXT NOT NULL,
        props TEXT, created_at BIGINT NOT NULL);
    CREATE INDEX IF NOT EXISTS ix_cevents_event ON connect_events(event, created_at);
    CREATE TABLE IF NOT EXISTS connect_alert_sent(user_id INTEGER NOT NULL, signal_id INTEGER NOT NULL, day TEXT NOT NULL,
        sent_at BIGINT NOT NULL, PRIMARY KEY(user_id, signal_id));
    CREATE INDEX IF NOT EXISTS ix_calert_day ON connect_alert_sent(user_id, day)"""

# The only event names /events (a later step) will accept. Anything else is rejected so the table stays clean.
EVENTS = ("announce_sent", "bot_start", "miniapp_opened", "link_started", "link_completed", "link_conflict",
          "signal_viewed", "upgrade_tapped", "paid_action_tapped", "unlink")
LINK_SOURCES = ("bot", "mini_app", "connect_app")


def db():
    auth.db().close()                      # makes sure the users table exists first, as credits.py does
    return dbx.connect("connect", SCHEMA)


# ---------------------------------------------------------------- the Connect token
# A Bearer token for the Mini App and, later, the Android client. It is NOT the website session: that is a cookie signed with
# DRIVERLINE_SECRET and cannot be read here, and this token cannot be read by auth.py. Even if both secrets were equal, the "tlc1"
# prefix and the "connect" audience keep them apart. The token carries no plan, balance or flag: those are read fresh on every call.
AUDIENCE = "connect"
TOKEN_PREFIX = "tlc1"
TOKEN_TTL = 900            # 15 minutes. The Mini App asks for a new one each time it opens.
MIN_SECRET = 32


def controls_on() -> bool:
    """The app's safe-direction controls (connect_controls.py) are off until CONNECT_CONTROLS=1 as well."""
    return enabled() and os.environ.get("CONNECT_CONTROLS") == "1"


def enabled() -> bool:
    """Connect is off until CONNECT_SECRET (a NEW random value, at least 32 characters) is set in Render, the same way tg_bot.py
    stays off without its webhook secret. Never reuse DRIVERLINE_SECRET here."""
    return len(os.environ.get("CONNECT_SECRET", "")) >= MIN_SECRET


def _secret() -> bytes:
    if not enabled():
        raise RuntimeError("CONNECT_SECRET is missing or shorter than 32 characters")
    return os.environ["CONNECT_SECRET"].encode()


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _sig(body: str) -> str:
    return hmac.new(_secret(), body.encode(), hashlib.sha256).hexdigest()


def issue_token(user: dict, telegram_id: int, now=None) -> str:
    """A token for this member and this Telegram account. "pt" is the short tag of the password hash that auth.py also uses, so a
    password reset ends every Connect token at once. Raises if CONNECT_SECRET is not set."""
    t = int(time.time() if now is None else now)
    claims = {"aud": AUDIENCE, "uid": int(user["id"]), "tg": int(telegram_id), "pt": auth._tag(user["pw_hash"]),
              "iat": t, "exp": t + TOKEN_TTL}
    body = TOKEN_PREFIX + "." + _b64(json.dumps(claims, sort_keys=True, separators=(",", ":")).encode())
    return body + "." + _sig(body)


def read_token(token, now=None):
    """The token's claims, or None for anything wrong: bad shape, bad signature, wrong audience, expired, or no secret set."""
    try:
        prefix, payload, sig = token.split(".")
        if prefix != TOKEN_PREFIX or not hmac.compare_digest(sig, _sig(prefix + "." + payload)):
            return None
        c = json.loads(_unb64(payload))
        t = int(time.time() if now is None else now)
        if c.get("aud") != AUDIENCE or type(c.get("pt")) is not str or any(type(c.get(k)) is not int for k in ("uid", "tg", "iat", "exp")):
            return None
        if c["exp"] <= t or c["iat"] > t + 60:
            return None
        return c
    except Exception:
        return None


def bearer(request) -> str:
    scheme, _, tok = (request.headers.get("authorization") or "").partition(" ")
    return tok.strip() if scheme.lower() == "bearer" else ""


def require_connect_user(request):
    """(user, link) for a valid Connect token, otherwise a 401. The member and the active link are read from the database on EVERY
    call, so blocking an account, unlinking, linking another Telegram account or a password reset takes effect at once, not after
    15 minutes. Every failure gives the same message, so a caller learns nothing about which check failed."""
    fail = HTTPException(401, "Please open TradeLens Connect again")
    c = read_token(bearer(request))
    if not c:
        raise fail
    with closing(auth.db()) as con:
        u = con.execute("SELECT * FROM users WHERE id=?", (c["uid"],)).fetchone()
    if not u or u["blocked"] or auth._tag(u["pw_hash"]) != c["pt"]:
        raise fail
    with closing(db()) as con:
        link = con.execute("SELECT * FROM telegram_links WHERE user_id=? AND telegram_id=? AND revoked_at IS NULL",
                           (c["uid"], c["tg"])).fetchone()
    if not link:
        raise fail
    return dict(u), dict(link)


# ---------------------------------------------------------------- what this member may use
# Read-only and built ONLY from the functions the website itself uses (credits, copy_plan, signal_feed, auth), so Connect can never
# disagree with the app about who has what. Nothing here writes, except that signal_feed.budget() grants the 3 welcome signals to a
# free account the first time it is asked, exactly as the website does when that member opens the signals page.
FLAGS = ("notifications", "mt5", "copytrading", "autotrading")


def _plan_expiry(user: dict, feature: str) -> int:
    """Latest end (epoch seconds) of an active paid plan or free grant that includes this feature; 0 when none does."""
    from . import credits
    best = 0
    for p in credits.active_plans(user["id"]):
        spec = credits.CATALOG.get(p["plan"]) or credits.GRANTS.get(p["plan"]) or {}
        if spec.get(feature):
            best = max(best, int(p["ends_at"]))
    return best


def get_entitlements(user: dict) -> dict:
    """The answer behind /me and /entitlements. "until" is epoch seconds, null for a flag that has no end date (members and admins,
    who keep what they had before billing, or a flag that is off)."""
    from . import copy_plan, credits, signal_feed
    gf = credits.grandfathered(user)
    notify = bool(credits.entitled(user, "notify"))
    mt5 = bool(credits.entitled(user, "mt5"))
    cp = copy_plan.state(user)
    auto = bool(signal_feed.paid(user) or mt5)          # AutoTrading follows the MT5 entitlement, as autotrade.py decides it

    def until(on: bool, feature: str):
        if not on or gf:
            return None
        return _plan_expiry(user, feature) or None

    with closing(signal_feed.db()) as con:
        b = signal_feed.budget(con, user)
    used, limit = auth.quota(user)
    billing = bool(credits.billing_on())
    return {
        "tier": user["tier"],
        "grandfathered": gf,
        "billing_on": billing,
        "flags": {"notifications": notify, "mt5": mt5, "copytrading": bool(cp["copy"]), "autotrading": auto},
        "until": {"notifications": until(notify, "notify"), "mt5": until(mt5, "mt5"),
                  "copytrading": (int(cp["until"]) or None) if cp["copy"] else None,
                  "autotrading": until(auto, "mt5")},
        "signals": {k: b.get(k) for k in ("kind", "balance", "used", "limit", "paused") if k in b},
        "analysis": {"used": int(used), "limit": int(limit)},
        "plans": [{"plan": p["plan"], "label": (credits.CATALOG.get(p["plan"]) or credits.GRANTS.get(p["plan"]) or {}).get("label", p["plan"]),
                   "until": int(p["ends_at"])} for p in credits.active_plans(user["id"])],
    }


# ---------------------------------------------------------------- the /api/connect/v1 routes
# Six routes. All are off (404) until CONNECT_SECRET is set. The Mini App page sends Telegram's signed initData; verifying it needs the
# bot's token, which is read from CONNECT_BOT_TOKEN (a SEPARATE setting from anything else, so a test bot can be used first).
# Linking takes the member's email and password IN THE REQUEST BODY (not the website cookie), checked with the same function and the
# same lock-out counter as the website login, so Connect adds no new way to guess a password.
NOT_FOUND = JSONResponse({"detail": "Not found"}, status_code=404)
_link_tries: dict = {}              # telegram_id -> attempt times, so one Telegram account cannot hammer /link
LINK_MAX, LINK_WINDOW = 5, 900


def _bot_token() -> str:
    return os.environ.get("CONNECT_BOT_TOKEN", "")


def _now() -> int:
    return int(time.time())


async def _body(request) -> dict:
    try:
        d = await request.json()
    except Exception:
        raise HTTPException(400, "Bad request")
    if not isinstance(d, dict):
        raise HTTPException(400, "Bad request")
    return d


def _tg_identity(d: dict) -> dict:
    """Who Telegram says is calling, from the signed initData. Same 401 for every failure."""
    try:
        return telegram_initdata.verify_init_data(str(d.get("init_data", "")), _bot_token())
    except Exception:
        raise HTTPException(401, "Please open TradeLens Connect again")


def record(event: str, telegram_id=None, user_id=None, props=None):
    """One funnel event. Never raises: a problem here must not break the feature. props must never hold personal data or chat text."""
    if event not in EVENTS:
        return
    try:
        with closing(db()) as con:
            con.execute("INSERT INTO connect_events(telegram_id,user_id,event,props,created_at) VALUES(?,?,?,?,?)",
                        (telegram_id, user_id, event, json.dumps(props, separators=(",", ":")) if props else None, _now()))
            con.commit()
    except Exception as e:
        print(f"[connect] could not record an event: {type(e).__name__}", flush=True)


def _active_link(telegram_id: int):
    with closing(db()) as con:
        r = con.execute("SELECT * FROM telegram_links WHERE telegram_id=? AND revoked_at IS NULL", (telegram_id,)).fetchone()
    return dict(r) if r else None


def _user(uid: int):
    with closing(auth.db()) as con:
        r = con.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    return dict(r) if r else None


def _mask(email: str) -> str:
    name, _, dom = (email or "").partition("@")
    return (name[:2] + "***@" + dom) if dom else ""


def _me(user: dict) -> dict:
    return {"email": _mask(user["email"]), "tier": user["tier"], "joined": str(user.get("created_at") or "")[:10],
            "entitlements": get_entitlements(user)}


def link_status(telegram_id: int):
    """For the bot's /status: None when this Telegram account is not linked, else (user, link)."""
    lk = _active_link(telegram_id)
    u = _user(lk["user_id"]) if lk else None
    return (u, lk) if u and not u["blocked"] else None


def unlink_telegram(telegram_id: int) -> bool:
    """Ends the active link of this Telegram account. True when there was one. Used by /unlink here and in the bot."""
    with closing(db()) as con:
        cur = con.execute("UPDATE telegram_links SET revoked_at=? WHERE telegram_id=? AND revoked_at IS NULL", (_now(), telegram_id))
        con.commit()
        n = cur.rowcount
    if n:
        record("unlink", telegram_id=telegram_id)
    return bool(n)


async def session(request):
    """POST {init_data}: opens the Mini App. Linked -> a Connect token and the member's summary. Not linked -> {"linked": false}."""
    if not enabled():
        return NOT_FOUND
    ident = _tg_identity(await _body(request))
    tg = ident["telegram_id"]
    lk = _active_link(tg)
    u = _user(lk["user_id"]) if lk else None
    record("miniapp_opened", telegram_id=tg, user_id=u["id"] if u else None, props={"linked": bool(u)})
    if not u or u["blocked"]:
        return JSONResponse({"linked": False})
    with closing(db()) as con:
        con.execute("UPDATE telegram_links SET last_seen_at=?, telegram_username=? WHERE id=?", (_now(), ident.get("username"), lk["id"]))
        con.commit()
    return JSONResponse({"linked": True, "token": issue_token(u, tg), "expires_in": TOKEN_TTL, "me": _me(u)})


async def link(request):
    """POST {init_data, email, password}: links this Telegram account to the TradeLens account that owns the email and password."""
    if not enabled():
        return NOT_FOUND
    d = await _body(request)
    ident = _tg_identity(d)
    tg = ident["telegram_id"]
    now = time.time()
    tries = [t for t in _link_tries.get(tg, []) if now - t < LINK_WINDOW]
    if len(tries) >= LINK_MAX:
        raise HTTPException(429, "Too many attempts. Try again in 15 minutes.")
    _link_tries[tg] = tries + [now]
    if len(_link_tries) > 5000:
        _link_tries.pop(next(iter(_link_tries)), None)
    email, pw = str(d.get("email", "")).strip().lower(), str(d.get("password", ""))
    if auth._locked(email):
        raise HTTPException(429, "Too many attempts. Try again in 15 minutes.")
    record("link_started", telegram_id=tg)
    with closing(auth.db()) as con:
        r = con.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
    if not (r and auth.check_pw(pw, r["pw_hash"])):
        auth._fails.setdefault(email, []).append(time.time())
        raise HTTPException(401, "Invalid email or password")
    if r["blocked"]:
        raise HTTPException(403, "This account is disabled")
    auth._fails.pop(email, None)
    u = dict(r)
    try:
        with closing(db()) as con:
            con.insert("INSERT INTO telegram_links(user_id,telegram_id,telegram_username,source,linked_at,last_seen_at) VALUES(?,?,?,?,?,?)",
                       (u["id"], tg, ident.get("username"), "mini_app", _now(), _now()))
            con.commit()
    except Exception as e:
        if not dbx.is_duplicate(e):
            raise
        record("link_conflict", telegram_id=tg, user_id=u["id"])
        raise HTTPException(409, "This Telegram account or this TradeLens account is already linked. Unlink it first, then try again.")
    record("link_completed", telegram_id=tg, user_id=u["id"], props={"source": "mini_app"})
    _link_tries.pop(tg, None)
    return JSONResponse({"linked": True, "token": issue_token(u, tg), "expires_in": TOKEN_TTL, "me": _me(u)})


async def unlink(request):
    """POST with the Bearer token: ends the link. The token stops working at once."""
    if not enabled():
        return NOT_FOUND
    u, lk = require_connect_user(request)
    unlink_telegram(lk["telegram_id"])
    return JSONResponse({"linked": False})


async def me(request):
    if not enabled():
        return NOT_FOUND
    u, _ = require_connect_user(request)
    return JSONResponse(_me(u))


async def entitlements(request):
    if not enabled():
        return NOT_FOUND
    u, _ = require_connect_user(request)
    return JSONResponse(get_entitlements(u))


async def signals(request):
    """GET with the Bearer token: the same signal list the website shows, read-only. It never opens or executes a signal, so it
    spends nothing from the member's balance; opening stays in the existing signals flow."""
    if not enabled():
        return NOT_FOUND
    u, _ = require_connect_user(request)
    from . import signal_feed as F
    now = int(time.time())
    with closing(F.db()) as con:
        rows = con.execute("SELECT * FROM signals WHERE created_at>? ORDER BY id DESC LIMIT 60",
                           (now - F.CFG["view_hours"] * 3600,)).fetchall()
        opened = {r["signal_id"] for r in con.execute("SELECT signal_id FROM signal_opens WHERE user_id=?", (u["id"],)).fetchall()}
        b = F.budget(con, u)
    return JSONResponse({"signals": [F._public(dict(r), r["id"] in opened, now) for r in rows], "budget": b, "ts": now})


async def mt5(request):
    """GET with the Bearer token: what the website's MT5 page shows, read-only (same ea.py functions and rules as /api/ea/status).
    It cannot change rules, close a position or send any command."""
    if not enabled():
        return NOT_FOUND
    u, _ = require_connect_user(request)
    from . import credits, ea
    now = time.time()
    with closing(ea.db()) as con:
        rows = ea.user_accounts(con, u["id"])
        out = {"mt5_allowed": credits.mt5_can_report(u), "accounts": [ea._summary(con, a, now) for a in rows]}
        if not rows:
            if request.query_params.get("account") not in (None, ""):
                raise HTTPException(404, "That MT5 account was not found")
            return JSONResponse({**out, "has_key": False, "connected": False, "account_id": None, "controls": controls_on()})
        a = ea.resolve(con, u["id"], request.query_params.get("account"))
        aid = a["id"]
        s = con.execute("SELECT state FROM ea_acct_state WHERE account_id=?", (aid,)).fetchone()
        rules, mg = ea.get_rules(con, aid), ea.get_manage(con, aid)
    seen = a["last_seen"]
    out.update(account_id=aid, label=a["label"], has_key=True, connected=bool(seen and now - seen < 120),
               last_seen_ago=int(now - seen) if seen else None, rules=rules, manage=mg)
    if s:
        st = json.loads(s["state"])
        out.update(account=st["account"], today=st["today"], positions=st["positions"], check=ea.risk_check(st, rules),
                   orders=st.get("orders") or [])
    out["controls"] = controls_on()
    return JSONResponse(out)


async def auto(request):
    """GET with the Bearer token: what the website's AutoTrading page shows (eligibility, status, protection, recent jobs),
    read-only. It cannot switch AutoTrading on or off, change a setting or start a trade."""
    if not enabled():
        return NOT_FOUND
    u, _ = require_connect_user(request)
    from . import autotrade as A, ea
    with closing(ea.db()) as con:
        if A.allowed(con, u):
            return JSONResponse({"available": False})
        accts = []
        for a in ea.user_accounts(con, u["id"]):
            st = A.get_settings(con, a["id"])
            accts.append({"id": a["id"], "label": a["label"], "enabled": bool(st["enabled"]), "settings": {k: st[k] for k in A.form_values(st)},
                          "pending": st["pending"], "paused_reason": st["paused_reason"],
                          "eligibility": A.eligibility(con, a["id"]), "protection": A.protection_view(con, a["id"])})
        jobs = [A._job_out(dict(r)) for r in con.execute("SELECT * FROM auto_jobs WHERE user_id=? ORDER BY id DESC LIMIT 30", (u["id"],)).fetchall()]
    return JSONResponse({"available": True, "accounts": accts, "recent": jobs, "now": A._now(), "controls": controls_on()})


async def events(request):
    """POST {event, props?} with the Bearer token. Only the names in EVENTS are accepted; props is kept only if small."""
    if not enabled():
        return NOT_FOUND
    u, lk = require_connect_user(request)
    d = await _body(request)
    name = d.get("event")
    if name not in EVENTS:
        raise HTTPException(400, "Unknown event")
    props = d.get("props")
    if not isinstance(props, dict) or len(json.dumps(props)) > 300:
        props = None
    record(name, telegram_id=lk["telegram_id"], user_id=u["id"], props=props)
    return JSONResponse({"ok": True})


routes = [Route("/api/connect/v1/session", session, methods=["POST"]),
          Route("/api/connect/v1/link", link, methods=["POST"]),
          Route("/api/connect/v1/unlink", unlink, methods=["POST"]),
          Route("/api/connect/v1/me", me, methods=["GET"]),
          Route("/api/connect/v1/entitlements", entitlements, methods=["GET"]),
          Route("/api/connect/v1/signals", signals, methods=["GET"]),
          Route("/api/connect/v1/mt5", mt5, methods=["GET"]),
          Route("/api/connect/v1/auto", auto, methods=["GET"]),
          Route("/api/connect/v1/events", events, methods=["POST"])]
