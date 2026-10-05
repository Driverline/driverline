"""Accounts, invite codes, signed-cookie sessions and daily AI quotas (stdlib only)."""
import asyncio
import hashlib
import hmac
import os
import re
import secrets
import time
from contextlib import closing
from pathlib import Path

from starlette.exceptions import HTTPException
from starlette.responses import JSONResponse
from starlette.routing import Route

from . import dbx, mailer
LIMITS = {
    "free": int(os.environ.get("DRIVERLINE_FREE_LIMIT", 3)),
    "member": int(os.environ.get("DRIVERLINE_MEMBER_LIMIT", 30)),
    "admin": 1000,
}
COOKIE, MAX_AGE = "dl_session", 30 * 86400
SECURE = os.environ.get("DRIVERLINE_SECURE") == "1"  # set to 1 when served over HTTPS
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


AUTH_SCHEMA = """
    CREATE TABLE IF NOT EXISTS users(id {ID}, email TEXT UNIQUE NOT NULL,
        pw_hash TEXT NOT NULL, tier TEXT NOT NULL DEFAULT 'free', blocked INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS invites(code TEXT PRIMARY KEY, tier TEXT NOT NULL, uses_left INTEGER NOT NULL);
    CREATE TABLE IF NOT EXISTS usage(user_id INTEGER, day TEXT, n INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY(user_id, day));
    CREATE TABLE IF NOT EXISTS resets(token_hash TEXT PRIMARY KEY, user_id INTEGER NOT NULL,
        expires_at BIGINT NOT NULL)"""


def db():
    return dbx.connect("auth", AUTH_SCHEMA)


def _now():
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())


def _today():
    return time.strftime("%Y-%m-%d", time.gmtime())


def _secret() -> bytes:
    s = os.environ.get("DRIVERLINE_SECRET")
    if s:
        return s.encode()
    f = dbx.SQLITE_PATH.parent / "secret.key"
    f.parent.mkdir(parents=True, exist_ok=True)
    if dbx.using_postgres():
        print("[warning] Set DRIVERLINE_SECRET: without it, logins reset whenever the server restarts.", flush=True)
    if not f.exists():
        f.write_text(secrets.token_hex(32))
        try:
            f.chmod(0o600)
        except OSError:
            pass
    return f.read_text().strip().encode()


def hash_pw(pw: str) -> str:
    salt = os.urandom(16)
    h = hashlib.scrypt(pw.encode(), salt=salt, n=2 ** 14, r=8, p=1, dklen=32)
    return salt.hex() + "$" + h.hex()


def check_pw(pw: str, stored: str) -> bool:
    try:
        salt, h = stored.split("$")
        calc = hashlib.scrypt(pw.encode(), salt=bytes.fromhex(salt), n=2 ** 14, r=8, p=1, dklen=32)
        return hmac.compare_digest(calc.hex(), h)
    except Exception:
        return False


def _sign(payload: str) -> str:
    return hmac.new(_secret(), payload.encode(), hashlib.sha256).hexdigest()


def _tag(pw_hash: str) -> str:
    return hashlib.sha256(pw_hash.encode()).hexdigest()[:10]


def make_token(uid: int, pw_hash: str) -> str:
    p = f"{uid}:{int(time.time()) + MAX_AGE}:{_tag(pw_hash)}"
    return p + ":" + _sign(p)


def read_token(token: str):
    try:
        uid, exp, tag, sig = token.split(":")
        if hmac.compare_digest(sig, _sign(f"{uid}:{exp}:{tag}")) and int(exp) > time.time():
            return int(uid), tag
    except Exception:
        pass
    return None


def current_user(request):
    t = request.cookies.get(COOKIE)
    parsed = read_token(t) if t else None
    if not parsed:
        return None
    uid, tag = parsed
    with closing(db()) as con:
        r = con.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    if not r or r["blocked"] or _tag(r["pw_hash"]) != tag:
        return None
    return dict(r)


def require_user(request) -> dict:
    u = current_user(request)
    if not u:
        raise HTTPException(401, "Please sign in")
    return u


def require_admin(request) -> dict:
    u = require_user(request)
    if u["tier"] != "admin":
        raise HTTPException(403, "Admin only")
    return u


def quota(user: dict):
    with closing(db()) as con:
        r = con.execute("SELECT n FROM usage WHERE user_id=? AND day=?", (user["id"], _today())).fetchone()
    return (r["n"] if r else 0), LIMITS.get(user["tier"], 0)


def has_quota(user: dict) -> bool:
    used, limit = quota(user)
    return used < limit


def consume_quota(user: dict):
    with closing(db()) as con:
        con.execute("INSERT INTO usage(user_id,day,n) VALUES(?,?,1) "
                    "ON CONFLICT(user_id,day) DO UPDATE SET n=usage.n+1", (user["id"], _today()))
        con.commit()


def bootstrap_admin():
    """Creates the first admin from DRIVERLINE_ADMIN_EMAIL / DRIVERLINE_ADMIN_PASSWORD if that account is missing.
    It never overwrites an existing account. Remove the password variable after your first login."""
    email = os.environ.get("DRIVERLINE_ADMIN_EMAIL", "").strip().lower()
    pw = os.environ.get("DRIVERLINE_ADMIN_PASSWORD", "")
    if not email or len(pw) < 8:
        return
    with closing(db()) as con:
        if con.execute("SELECT 1 FROM users WHERE email=?", (email,)).fetchone():
            return
        con.execute("INSERT INTO users(email,pw_hash,tier,created_at) VALUES(?,?,'admin',?)", (email, hash_pw(pw), _now()))
        con.commit()


async def _json(request):
    try:
        d = await request.json()
    except Exception:
        raise HTTPException(400, "Invalid JSON body")
    if not isinstance(d, dict):
        raise HTTPException(400, "JSON object expected")
    return d


def _session(resp, uid, pw_hash):
    resp.set_cookie(COOKIE, make_token(uid, pw_hash), max_age=MAX_AGE, httponly=True,
                    samesite="lax", secure=SECURE, path="/")
    return resp


async def signup(request):
    d = await _json(request)
    email = str(d.get("email", "")).strip().lower()
    pw, code = str(d.get("password", "")), str(d.get("invite", "")).strip()
    if not EMAIL_RE.match(email) or len(email) > 120:
        raise HTTPException(422, "Enter a valid email")
    if not 8 <= len(pw) <= 200:
        raise HTTPException(422, "Password must be 8 to 200 characters")
    ph = hash_pw(pw)
    with closing(db()) as con:
        inv = con.execute("SELECT * FROM invites WHERE code=? AND uses_left>0", (code,)).fetchone()
        if not inv:
            raise HTTPException(403, "Invalid or used invite code")
        try:
            uid = con.insert("INSERT INTO users(email,pw_hash,tier,created_at) VALUES(?,?,?,?)",
                             (email, ph, inv["tier"], _now()))
        except Exception as e:
            if dbx.is_duplicate(e):
                raise HTTPException(409, "Email already registered")
            raise
        con.execute("UPDATE invites SET uses_left=uses_left-1 WHERE code=?", (code,))
        con.commit()
    return _session(JSONResponse({"ok": True}), uid, ph)


_fails: dict[str, list[float]] = {}


def _locked(email: str) -> bool:
    now = time.time()
    _fails[email] = [t for t in _fails.get(email, []) if now - t < 900]
    return len(_fails[email]) >= 5


async def login(request):
    d = await _json(request)
    email, pw = str(d.get("email", "")).strip().lower(), str(d.get("password", ""))
    if _locked(email):
        raise HTTPException(429, "Too many attempts. Try again in 15 minutes.")
    with closing(db()) as con:
        r = con.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
    if not (r and check_pw(pw, r["pw_hash"])):
        _fails.setdefault(email, []).append(time.time())
        raise HTTPException(401, "Invalid email or password")
    if r["blocked"]:
        raise HTTPException(403, "This account is disabled")
    _fails.pop(email, None)
    return _session(JSONResponse({"ok": True}), r["id"], r["pw_hash"])


async def logout(request):
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(COOKIE, path="/")
    return resp


async def me(request):
    u = require_user(request)
    used, limit = quota(u)
    return JSONResponse({"email": u["email"], "tier": u["tier"], "used": used, "limit": limit})


RESET_TTL = 3600
_forgot: dict[str, list[float]] = {}
_bg: set = set()


def create_reset_token(user_id: int) -> str:
    raw = secrets.token_urlsafe(32)
    now = int(time.time())
    with closing(db()) as con:
        con.execute("DELETE FROM resets WHERE user_id=? OR expires_at<?", (user_id, now))
        con.execute("INSERT INTO resets VALUES(?,?,?)",
                    (hashlib.sha256(raw.encode()).hexdigest(), user_id, now + RESET_TTL))
        con.commit()
    return raw


def reset_link(request, raw: str) -> str:
    base = os.environ.get("DRIVERLINE_BASE_URL", "").rstrip("/") or str(request.base_url).rstrip("/")
    return f"{base}/reset.html#token={raw}"


async def forgot(request):
    d = await _json(request)
    email = str(d.get("email", "")).strip().lower()
    generic = JSONResponse({"ok": True, "message": "If that email has an account, a reset link is on its way."})
    now = time.time()
    hist = [t for t in _forgot.get(email, []) if now - t < 3600]
    _forgot[email] = hist
    if len(hist) >= 3:  # silently ignore repeats so nobody can flood an inbox
        return generic
    _forgot[email].append(now)
    with closing(db()) as con:
        r = con.execute("SELECT id, blocked FROM users WHERE email=?", (email,)).fetchone()
    if r and not r["blocked"]:
        link = reset_link(request, create_reset_token(r["id"]))
        task = asyncio.create_task(mailer.send_reset(email, link))  # background: same speed for every email
        _bg.add(task)
        task.add_done_callback(_bg.discard)
    return generic


async def reset(request):
    d = await _json(request)
    raw, pw = str(d.get("token", "")), str(d.get("password", ""))
    if not 8 <= len(pw) <= 200:
        raise HTTPException(422, "Password must be 8 to 200 characters")
    th = hashlib.sha256(raw.encode()).hexdigest()
    with closing(db()) as con:
        r = con.execute("SELECT user_id FROM resets WHERE token_hash=? AND expires_at>?",
                        (th, int(time.time()))).fetchone()
        if not r:
            raise HTTPException(400, "This reset link is invalid or has expired")
        con.execute("UPDATE users SET pw_hash=? WHERE id=?", (hash_pw(pw), r["user_id"]))
        con.execute("DELETE FROM resets WHERE user_id=?", (r["user_id"],))
        con.commit()
    return JSONResponse({"ok": True})


routes = [
    Route("/api/auth/forgot", forgot, methods=["POST"]),
    Route("/api/auth/reset", reset, methods=["POST"]),
    Route("/api/auth/signup", signup, methods=["POST"]),
    Route("/api/auth/login", login, methods=["POST"]),
    Route("/api/auth/logout", logout, methods=["POST"]),
    Route("/api/auth/me", me, methods=["GET"]),
]
