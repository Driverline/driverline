"""Continue with Deriv: proves a person has a real Deriv account, then hands them a one-time TradeLens invite code.

What it does, step by step:
  1. The person taps "Continue with Deriv". We send them to Deriv's own OAuth page (authorization code with PKCE).
  2. Deriv sends them back with a one-time code. We swap it for an access token, read the list of their Deriv account
     IDs once, and THROW THE TOKEN AWAY. Nothing about the token is stored or logged, and this sign-up step never calls
     any trading or payment endpoint (trading from a signal is a separate, optional step: see deriv_trade.py).
  3. If none of those Deriv accounts is linked to a TradeLens user yet, we issue one single-use invite code (a person
     who comes back before using it gets the same code, not a new one). The invite is remembered against their Deriv
     account IDs.
  4. When that invite code is used to sign up, the Deriv accounts become linked to the new TradeLens user and the
     one-time welcome signals are granted. A Deriv account can only ever be linked to one TradeLens user, so it cannot
     be used again to farm welcome signals. Anyone who hits "already linked" is sent to support.
Everything is OFF until the owner switches it on in Admin. Existing members and the existing invite system are untouched."""
import base64
import hashlib
import html
import re
import secrets
import time
from contextlib import closing
from urllib.parse import urlencode

import httpx
from starlette.exceptions import HTTPException
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse
from starlette.routing import Route

from . import auth, credits, dbx

AUTH_URL = "https://auth.deriv.com/oauth2/auth"
TOKEN_URL = "https://auth.deriv.com/oauth2/token"
ACCOUNTS_URL = "https://api.derivws.com/trading/v1/options/accounts"
SCOPE = "trade"            # Deriv requires it to read the account list; we never use it for anything else
STATE_TTL = 600            # seconds a sign-in attempt stays valid
SUPPORT_TG, SUPPORT_MAIL = "https://t.me/TradeLens_Crew", "tradelens_crew@outlook.com"
ID_RE = re.compile(r"^[A-Za-z0-9_-]{3,40}$")
ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"

SCHEMA = """
    CREATE TABLE IF NOT EXISTS deriv_states(state TEXT PRIMARY KEY, verifier TEXT NOT NULL, created_at BIGINT NOT NULL);
    CREATE TABLE IF NOT EXISTS deriv_accounts(account_id TEXT PRIMARY KEY, user_id INTEGER, invite_code TEXT,
        created_at BIGINT NOT NULL, linked_at BIGINT);
    CREATE INDEX IF NOT EXISTS ix_deriv_user ON deriv_accounts(user_id);
    CREATE INDEX IF NOT EXISTS ix_deriv_invite ON deriv_accounts(invite_code)"""


def db():
    auth.db().close()
    return dbx.connect("deriv", SCHEMA)


def _cfg() -> dict:
    with closing(credits.db()) as con:
        s = credits.settings(con)
    on = s.get("deriv_enabled") == "1" and bool(s.get("deriv_app_id"))
    return {"on": on, "app_id": s.get("deriv_app_id", ""), "ib": s.get("deriv_ib_link", ""),
            "cashier": s.get("deriv_cashier_url", "")}


def _base(request) -> str:
    import os
    return os.environ.get("DRIVERLINE_BASE_URL", "").rstrip("/") or str(request.base_url).rstrip("/")


# ---------------------------------------------------------------- small pages the person lands on
def _page(title: str, inner: str, status: int = 200):
    doc = ("<!DOCTYPE html><html lang='en'><head><meta charset='utf-8'><title>TradeLens</title>"
           "<meta name='viewport' content='width=device-width, initial-scale=1'>"
           "<meta name='referrer' content='no-referrer'><link rel='stylesheet' href='/theme.css'></head><body>"
           "<header><a class='brand' href='/'><span><b>Trade<i>Lens</i></b><small>by Driverline</small></span></a></header>"
           f"<main><div class='card'><h2>{html.escape(title)}</h2>{inner}</div></main></body></html>")
    return HTMLResponse(doc, status_code=status, headers={"cache-control": "no-store"})


def _support_html() -> str:
    return (f"<p>Please contact TradeLens support: <a style='color:var(--gold2)' href='{SUPPORT_TG}'>Telegram</a> "
            f"or <a style='color:var(--gold2)' href='mailto:{SUPPORT_MAIL}'>{SUPPORT_MAIL}</a>.</p>")


def _again(msg: str, status: int = 400):
    return _page("Deriv sign-in", f"<p>{html.escape(msg)}</p><p><a style='color:var(--gold2)' href='/login.html'>Back to sign in</a></p>", status)


# ---------------------------------------------------------------- OAuth
_starts: dict = {}


def _rate_ok(ip: str) -> bool:
    now = time.time()
    _starts[ip] = [t for t in _starts.get(ip, []) if now - t < 60]
    if len(_starts[ip]) >= 12:
        return False
    _starts[ip].append(now)
    return True


async def start(request):
    c = _cfg()
    if not c["on"]:
        return _again("Deriv sign-in is not open yet.", 503)
    ip = request.client.host if request.client else "?"
    if not _rate_ok(ip):
        return _again("Too many attempts. Please wait a minute and try again.", 429)
    state, verifier = secrets.token_urlsafe(24), secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    now = int(time.time())
    with closing(db()) as con:
        con.execute("DELETE FROM deriv_states WHERE created_at<?", (now - STATE_TTL,))
        con.execute("INSERT INTO deriv_states(state,verifier,created_at) VALUES(?,?,?)", (state, verifier, now))
        con.commit()
    url = AUTH_URL + "?" + urlencode({"response_type": "code", "client_id": c["app_id"],
                                      "redirect_uri": _base(request) + "/auth/deriv/callback", "scope": SCOPE,
                                      "state": state, "code_challenge": challenge, "code_challenge_method": "S256"})
    resp = RedirectResponse(url, status_code=302)
    resp.set_cookie("dl_oauth", state, max_age=STATE_TTL, httponly=True, samesite="lax", secure=auth.SECURE,
                    path="/auth/deriv")
    return resp


def _take_state(state: str):
    """The PKCE verifier for this state, once. None if unknown, used or expired."""
    if not state:
        return None
    with closing(db()) as con:
        r = con.execute("SELECT verifier, created_at FROM deriv_states WHERE state=?", (state,)).fetchone()
        con.execute("DELETE FROM deriv_states WHERE state=?", (state,))
        con.commit()
    return r["verifier"] if r and time.time() - r["created_at"] <= STATE_TTL else None


def account_ids(data) -> list:
    """Every Deriv account ID found in the response, whatever shape Deriv wraps the list in."""
    out = []

    def walk(x, depth=0):
        if depth > 5:
            return
        if isinstance(x, dict):
            for k in ("account_id", "loginid", "id"):
                v = x.get(k)
                if isinstance(v, str) and ID_RE.match(v) and v not in out:
                    out.append(v)
            for v in x.values():
                if isinstance(v, (dict, list)):
                    walk(v, depth + 1)
        elif isinstance(x, list):
            for v in x:
                walk(v, depth + 1)

    walk(data)
    return out[:30]


async def _read_accounts(app_id: str, code: str, verifier: str, redirect: str) -> list:
    async with httpx.AsyncClient(timeout=20) as cl:
        t = await cl.post(TOKEN_URL, data={"grant_type": "authorization_code", "client_id": app_id, "code": code,
                                           "code_verifier": verifier, "redirect_uri": redirect},
                          headers={"accept": "application/json"})
        t.raise_for_status()
        token = t.json().get("access_token")
        if not token:
            raise ValueError("no token")
        a = await cl.get(ACCOUNTS_URL, headers={"Authorization": f"Bearer {token}", "Deriv-App-ID": app_id,
                                                "accept": "application/json"})
        token = None                                   # never kept, never logged
        a.raise_for_status()
        return account_ids(a.json())


def _new_invite(con) -> str:
    code = "DV-" + "".join(secrets.choice(ALPHABET) for _ in range(8))
    with closing(auth.db()) as a:
        a.execute("INSERT INTO invites(code,tier,uses_left) VALUES(?,?,?)", (code, "free", 1))
        a.commit()
    return code


def _invite_open(code: str) -> bool:
    with closing(auth.db()) as a:
        return bool(a.execute("SELECT 1 FROM invites WHERE code=? AND uses_left>0", (code,)).fetchone())


async def callback(request):
    from . import deriv_trade
    state = request.query_params.get("state", "")
    if deriv_trade.owns_state(state):          # started from "Connect Deriv to trade" on a signal, not from sign-up
        return await deriv_trade.callback(request)
    c = _cfg()
    q = request.query_params
    verifier = _take_state(state)
    if not c["on"]:
        return _again("Deriv sign-in is not open yet.", 503)
    if q.get("error"):
        return _again("Deriv sign-in was cancelled, so nothing was changed. You can try again any time.")
    if not (verifier and q.get("code") and request.cookies.get("dl_oauth") == state):
        return _again("This sign-in link has expired or was already used. Please start again.")
    try:
        ids = await _read_accounts(c["app_id"], q["code"], verifier, _base(request) + "/auth/deriv/callback")
    except Exception as e:
        print(f"[deriv] sign-in failed: {type(e).__name__}", flush=True)
        return _again("We could not verify your Deriv account just now. Please try again in a moment.", 502)
    if not ids:
        return _page("No Deriv account found",
                     "<p>We could not find a Deriv trading account for this login. Create one first, then come back.</p>"
                     + (f"<p><a style='color:var(--gold2)' href='{html.escape(c['ib'])}' target='_blank' rel='noopener'>"
                        "Create a Deriv account</a></p>" if c["ib"] else ""))
    ph = ",".join("?" * len(ids))
    now = int(time.time())
    with closing(db()) as con:
        rows = con.execute(f"SELECT account_id, user_id, invite_code FROM deriv_accounts WHERE account_id IN ({ph})",
                           ids).fetchall()
        if any(r["user_id"] for r in rows):
            print("[deriv] sign-in refused: account already linked", flush=True)
            return _page("This Deriv account is already linked",
                         "<p>This Deriv account is already linked to a TradeLens account.</p>"
                         "<p>If it is yours, sign in with your TradeLens email and password. If you did not create that "
                         "account, or you cannot get in, we will help you.</p>" + _support_html())
        code = next((r["invite_code"] for r in rows if r["invite_code"] and _invite_open(r["invite_code"])), None)
        if not code:
            code = _new_invite(con)
        for i in ids:
            con.execute("INSERT INTO deriv_accounts(account_id,invite_code,created_at) VALUES(?,?,?) "
                        "ON CONFLICT(account_id) DO UPDATE SET invite_code=excluded.invite_code", (i, code, now))
        con.commit()
    safe = html.escape(code)
    return _page("Your Deriv account is verified",
                 "<p>Here is your one-time invite code:</p>"
                 f"<div class='big' id='code' style='letter-spacing:.08em'>{safe}</div>"
                 f"<div class='btns'><a href='/login.html#invite={safe}' style='flex:1;text-align:center;padding:13px;"
                 "border-radius:12px;background:linear-gradient(135deg,var(--gold2),var(--gold));color:#0a0a0a;"
                 "text-decoration:none;font-weight:700'>Continue to create your account</a></div>"
                 "<p class='mut'>New members get free welcome signals when they sign up with this code. "
                 "This sign-in step only reads your Deriv account ID once to check it is real. It does not keep your Deriv "
                 "access token and does not place trades. TradeLens never moves your money.</p>")


def on_signup(uid: int, code: str):
    """Called after someone signs up with an invite code. Links the Deriv accounts behind that code to the new user
    and gives the one-time welcome signals."""
    if not code.startswith("DV-"):
        return
    with closing(db()) as con:
        cur = con.execute("UPDATE deriv_accounts SET user_id=?, linked_at=? WHERE invite_code=? AND user_id IS NULL",
                          (uid, int(time.time()), code))
        linked = cur.rowcount
        con.commit()
    if linked:
        credits.ensure_welcome(uid)


# ---------------------------------------------------------------- what the sign-in page needs to know
async def public_config(request):
    c = _cfg()
    return JSONResponse({"enabled": c["on"], "ib_link": c["ib"] if c["on"] else "",
                         "cashier_url": c["cashier"] if c["on"] else ""})


# ---------------------------------------------------------------- admin: help someone who contacted support
async def admin_lookup(request):
    auth.require_admin(request)
    aid = request.query_params.get("account_id", "").strip()
    if not ID_RE.match(aid):
        raise HTTPException(422, "Enter a Deriv account ID")
    with closing(db()) as con:
        r = con.execute("SELECT user_id, invite_code, linked_at FROM deriv_accounts WHERE account_id=?", (aid,)).fetchone()
    if not r:
        return JSONResponse({"found": False})
    email = None
    if r["user_id"]:
        with closing(auth.db()) as a:
            u = a.execute("SELECT email FROM users WHERE id=?", (r["user_id"],)).fetchone()
        email = u["email"] if u else None
    return JSONResponse({"found": True, "linked": bool(r["user_id"]), "email": email, "invite_open": bool(
        r["invite_code"] and _invite_open(r["invite_code"])), "linked_at": r["linked_at"]})


async def admin_unlink(request):
    """Frees a Deriv account so its owner can verify again. The welcome signals already given stay as they are."""
    u = auth.require_admin(request)
    d = await auth._json(request)
    aid = str(d.get("account_id", "")).strip()
    if not ID_RE.match(aid):
        raise HTTPException(422, "Enter a Deriv account ID")
    with closing(db()) as con:
        cur = con.execute("DELETE FROM deriv_accounts WHERE account_id=?", (aid,))
        con.commit()
    print(f"[deriv] account unlinked by admin {u['id']}", flush=True)
    return JSONResponse({"ok": True, "removed": cur.rowcount})


routes = [
    Route("/auth/deriv/start", start, methods=["GET"]),
    Route("/auth/deriv/callback", callback, methods=["GET"]),
    Route("/api/public/deriv", public_config, methods=["GET"]),
    Route("/api/admin/deriv/lookup", admin_lookup, methods=["GET"]),
    Route("/api/admin/deriv/unlink", admin_unlink, methods=["POST"]),
]
