"""MT5 connection requests: the member sends their MT5 login details, an administrator connects the account on TradeLens' own VPS.

TradeLens does not hand out the Companion EA, so members do not create keys. Instead:
  member  -> POST /api/mt5req  {details: "Login: ...\\nPassword: ...\\nServer: ...", consent: true}
  admin   -> sees the request, reveals the credentials (audited), provisions the account (creates the connection key shown to the admin
             once), sets up MT5 + EA on the VPS with it
  system  -> when that EA first reports, the request becomes 'connected' and the stored password is DELETED.

Handling of the password (the sensitive part):
- Encrypted at rest with Fernet using DRIVERLINE_VAULT_KEY (set only in the server environment). Without that key nothing is stored: the
  request is refused (fail closed). The password is never logged, never returned to the member, and never put in a list response.
- Only an administrator can reveal it, one request at a time; every reveal is written to mt5_request_log.
- It is wiped automatically when the account connects, when the member cancels, when the request is rejected, and after STALE_DAYS.
"""
import os
import re
import secrets
import time
from contextlib import closing

from starlette.exceptions import HTTPException
from starlette.responses import JSONResponse
from starlette.routing import Route

from . import auth, credits, dbx, ea

STALE_DAYS = 14
DAILY_LIMIT = 5
ACTIVE = ("new", "in_progress")
_LABELS = {"login": "login", "password": "password", "pass": "password", "server": "server"}


# ---------------------------------------------------------------- the vault
def _fernet():
    key = os.environ.get("DRIVERLINE_VAULT_KEY", "").strip()
    if not key:
        return None
    try:
        from cryptography.fernet import Fernet
        return Fernet(key.encode())
    except Exception:
        return None


def vault_ready() -> bool:
    return _fernet() is not None


def _enc(text: str) -> str:
    f = _fernet()
    if not f:
        raise HTTPException(503, "Sending MT5 details is not available right now. Please contact TradeLens support.")
    return f.encrypt(text.encode()).decode()


def _dec(token: str) -> str:
    f = _fernet()
    if not f:
        raise HTTPException(503, "The vault key is not configured on the server")
    try:
        return f.decrypt(token.encode()).decode()
    except Exception:
        raise HTTPException(500, "Could not decrypt this password (wrong vault key?)")


# ---------------------------------------------------------------- parsing
def parse_details(text) -> tuple:
    """'Login: 123\\nPassword: x\\nServer: y' -> (login, password, server). Labels are case-insensitive; ':' or '=' both work; extra
    blank lines and spaces are fine. Raises HTTPException(422) with a message the member can act on."""
    text = str(text or "")
    if len(text) > 600:
        raise HTTPException(422, "That is too long. Paste just the three lines: Login, Password and Server")
    found = {}
    for line in text.replace("\r", "\n").split("\n"):
        m = re.match(r"^\s*([A-Za-z]+)\s*[:=]\s*(.*?)\s*$", line)
        if m and m.group(1).lower() in _LABELS and m.group(2):
            found.setdefault(_LABELS[m.group(1).lower()], m.group(2))
    fmt = "Use this format:\nLogin: 12345678\nPassword: your password\nServer: your broker server"
    for k in ("login", "password", "server"):
        if k not in found:
            raise HTTPException(422, f"The {k} is missing. {fmt}")
    login, pw, server = found["login"], found["password"], found["server"]
    if not re.fullmatch(r"\d{3,12}", login):
        raise HTTPException(422, "The login should be your MT5 account number (digits only)")
    if not 4 <= len(pw) <= 64 or any(ord(c) < 32 or ord(c) == 127 for c in pw):
        raise HTTPException(422, "The password should be 4 to 64 characters")
    if not re.fullmatch(r"[A-Za-z0-9 ._\-]{2,60}", server):
        raise HTTPException(422, "The server name looks wrong. Copy it exactly as MT5 shows it, for example DerivSVG-Server-03")
    return login, pw, server


# ---------------------------------------------------------------- helpers
def _now() -> int:
    return int(time.time())


def _sweep(con):
    """Passwords of requests nobody completed are deleted after STALE_DAYS."""
    cutoff = _now() - STALE_DAYS * 86400
    con.execute("UPDATE mt5_requests SET pw_enc=NULL, pw_wiped_at=?, status='expired', note='Not completed in time. Please send your details again.', "
                "updated_at=? WHERE status IN ('new','in_progress') AND created_at<?", (_now(), _now(), cutoff))
    con.commit()


def _wipe(con, rid, status, note=None):
    con.execute("UPDATE mt5_requests SET pw_enc=NULL, pw_wiped_at=?, status=?, note=COALESCE(?, note), updated_at=? WHERE id=?",
                (_now(), status, note, _now(), rid))


def _log(con, rid, admin_id, action):
    con.execute("INSERT INTO mt5_request_log(request_id,admin_id,action,created_at) VALUES(?,?,?,?)", (rid, admin_id, action, _now()))


def _pub(r) -> dict:
    return {"id": r["id"], "label": r["label"], "login": r["login"], "server": r["server"], "status": r["status"], "note": r["note"] or "",
            "created_at": r["created_at"], "account_id": r["account_id"]}


def on_report(con, aid: int):
    """An account's EA reported. If it belongs to an in-progress request, the connection worked: delete the stored password."""
    r = con.execute("SELECT id FROM mt5_requests WHERE account_id=? AND status='in_progress'", (aid,)).fetchone()
    if r:
        _wipe(con, r["id"], "connected", "")
        _log(con, r["id"], None, "connected: password deleted")
        con.commit()


# ---------------------------------------------------------------- member
async def create(request):
    u = auth.require_user(request)
    if not credits.mt5_allowed(u):
        raise HTTPException(403, "The MT5 connection is part of Signals Pro. Buy it in the Billing tab.")
    d = await auth._json(request)
    if d.get("consent") is not True:
        raise HTTPException(422, "Please tick the box to confirm you agree to share these details with TradeLens support")
    login, pw, server = parse_details(d.get("details"))
    label = ea._label(d.get("label")) or f"Account {login}"
    enc = _enc(pw)                                   # refused (503) when the vault key is not configured
    with closing(ea.db()) as con:
        _sweep(con)
        ea._lock(con, u["id"])
        used = len(ea.user_accounts(con, u["id"])) + con.execute(
            "SELECT COUNT(*) AS c FROM mt5_requests WHERE user_id=? AND status IN ('new','in_progress') AND account_id IS NULL", (u["id"],)).fetchone()["c"]
        if used >= ea.MAX_ACCOUNTS:
            raise HTTPException(409, f"You can connect up to {ea.MAX_ACCOUNTS} MT5 accounts. Remove one first.")
        if con.execute("SELECT 1 FROM mt5_requests WHERE user_id=? AND login=? AND status IN ('new','in_progress')", (u["id"], login)).fetchone():
            raise HTTPException(409, "You already sent this account. TradeLens support will set it up soon.")
        if con.execute("SELECT 1 FROM ea_accounts WHERE user_id=? AND login=?", (u["id"], login)).fetchone():
            raise HTTPException(409, "This MT5 account is already connected")
        if con.execute("SELECT COUNT(*) AS c FROM mt5_requests WHERE user_id=? AND created_at>?", (u["id"], _now() - 86400)).fetchone()["c"] >= DAILY_LIMIT:
            raise HTTPException(429, "Too many requests today. Please try again tomorrow.")
        rid = con.insert("INSERT INTO mt5_requests(user_id,label,login,server,pw_enc,status,created_at,updated_at) VALUES(?,?,?,?,?,'new',?,?)",
                         (u["id"], label, login, server, enc, _now(), _now()))
        con.commit()
    return JSONResponse({"id": rid, "status": "new"}, headers={"Cache-Control": "no-store"})


async def mine(request):
    u = auth.require_user(request)
    with closing(ea.db()) as con:
        _sweep(con)
        rows = con.execute("SELECT * FROM mt5_requests WHERE user_id=? AND status IN ('new','in_progress','rejected','expired') ORDER BY id DESC LIMIT 10",
                           (u["id"],)).fetchall()
    return JSONResponse({"requests": [_pub(r) for r in rows], "vault_ready": vault_ready()})


async def cancel(request):
    u = auth.require_user(request)
    with closing(ea.db()) as con:
        r = con.execute("SELECT * FROM mt5_requests WHERE id=? AND user_id=?", (request.path_params["rid"], u["id"])).fetchone()
        if not r:
            raise HTTPException(404, "Request not found")
        if r["status"] not in ACTIVE:
            raise HTTPException(409, "This request is already finished")
        _wipe(con, r["id"], "cancelled", "Cancelled by the member")
        _log(con, r["id"], None, "cancelled by member: password deleted")
        con.commit()
    return JSONResponse({"ok": True})


# ---------------------------------------------------------------- admin
async def admin_list(request):
    auth.require_admin(request)
    status = request.query_params.get("status", "open")
    with closing(ea.db()) as con:
        _sweep(con)
        q = ("SELECT r.*, u.email FROM mt5_requests r JOIN users u ON u.id=r.user_id "
             + ("WHERE r.status IN ('new','in_progress') " if status == "open" else "") + "ORDER BY r.id DESC LIMIT 100")
        rows = con.execute(q).fetchall()
        out = []
        for r in rows:
            acc = con.execute("SELECT last_seen FROM ea_accounts WHERE id=?", (r["account_id"],)).fetchone() if r["account_id"] else None
            out.append({**_pub(r), "email": r["email"], "has_password": bool(r["pw_enc"]), "user_id": r["user_id"],
                        "ea_connected": bool(acc and acc["last_seen"] and _now() - acc["last_seen"] < 120)})
    return JSONResponse({"requests": out, "vault_ready": vault_ready()}, headers={"Cache-Control": "no-store"})


async def admin_reveal(request):
    u = auth.require_admin(request)
    with closing(ea.db()) as con:
        r = con.execute("SELECT * FROM mt5_requests WHERE id=?", (request.path_params["rid"],)).fetchone()
        if not r:
            raise HTTPException(404, "Request not found")
        if not r["pw_enc"]:
            raise HTTPException(409, "The password has been deleted (the request is finished). Ask the member to send the details again.")
        pw = _dec(r["pw_enc"])
        _log(con, r["id"], u["id"], "revealed")
        con.commit()
    return JSONResponse({"login": r["login"], "password": pw, "server": r["server"]}, headers={"Cache-Control": "no-store"})


async def admin_provision(request):
    """Creates the member's MT5 account and its connection key (shown ONCE, to the admin, to put into the EA on the VPS)."""
    u = auth.require_admin(request)
    with closing(ea.db()) as con:
        r = con.execute("SELECT * FROM mt5_requests WHERE id=?", (request.path_params["rid"],)).fetchone()
        if not r:
            raise HTTPException(404, "Request not found")
        if r["status"] not in ACTIVE:
            raise HTTPException(409, "This request is already finished")
        if r["account_id"]:
            raise HTTPException(409, "An account was already created for this request. Use 'New key' to replace its key.")
        ea._lock(con, r["user_id"])
        rows = ea.user_accounts(con, r["user_id"])
        if len(rows) >= ea.MAX_ACCOUNTS:
            raise HTTPException(409, f"This member already has {ea.MAX_ACCOUNTS} accounts")
        label = r["label"]
        if any(x["label"].lower() == label.lower() for x in rows):
            label = f"{label} {r['login']}"[:30]
        try:
            aid, raw = ea._new_key(con, r["user_id"], label)
        except Exception as e:
            con.raw.rollback()
            if dbx.is_duplicate(e):
                raise HTTPException(409, "The member already has an account with that name. Rename it first.")
            raise
        con.execute("UPDATE mt5_requests SET account_id=?, status='in_progress', updated_at=? WHERE id=?", (aid, _now(), r["id"]))
        _log(con, r["id"], u["id"], "provisioned")
        con.commit()
    return JSONResponse({"account_id": aid, "key": raw}, headers={"Cache-Control": "no-store"})


async def admin_newkey(request):
    u = auth.require_admin(request)
    with closing(ea.db()) as con:
        r = con.execute("SELECT * FROM mt5_requests WHERE id=?", (request.path_params["rid"],)).fetchone()
        if not r or not r["account_id"]:
            raise HTTPException(404, "No account was created for this request yet")
        raw = "dlk_" + secrets.token_urlsafe(24)
        con.execute("UPDATE ea_accounts SET key_hash=?, created_at=?, last_seen=NULL, login=NULL WHERE id=?", (ea._hash(raw), _now(), r["account_id"]))
        _log(con, r["id"], u["id"], "new key")
        con.commit()
    return JSONResponse({"key": raw}, headers={"Cache-Control": "no-store"})


async def admin_set(request):
    """reject (note shown to the member) / wipe (delete the stored password now) / progress (mark as being worked on)."""
    u = auth.require_admin(request)
    d = await auth._json(request)
    action = d.get("action")
    with closing(ea.db()) as con:
        r = con.execute("SELECT * FROM mt5_requests WHERE id=?", (request.path_params["rid"],)).fetchone()
        if not r:
            raise HTTPException(404, "Request not found")
        if action == "reject":
            note = str(d.get("note") or "").strip()[:300]
            if not note:
                raise HTTPException(422, "Write a short reason. The member will see it")
            if r["status"] not in ACTIVE:
                raise HTTPException(409, "This request is already finished")
            _wipe(con, r["id"], "rejected", note)
        elif action == "wipe":
            _wipe(con, r["id"], r["status"])
        elif action == "progress":
            if r["status"] != "new":
                raise HTTPException(409, "Only a new request can be marked in progress")
            con.execute("UPDATE mt5_requests SET status='in_progress', updated_at=? WHERE id=?", (_now(), r["id"]))
        else:
            raise HTTPException(422, "action must be reject, wipe or progress")
        _log(con, r["id"], u["id"], action + (": password deleted" if action in ("reject", "wipe") else ""))
        con.commit()
    return JSONResponse({"ok": True})


routes = [
    Route("/api/mt5req", create, methods=["POST"]),
    Route("/api/mt5req/me", mine, methods=["GET"]),
    Route("/api/mt5req/{rid:int}/cancel", cancel, methods=["POST"]),
    Route("/api/mt5req/admin", admin_list, methods=["GET"]),
    Route("/api/mt5req/admin/{rid:int}/reveal", admin_reveal, methods=["POST"]),
    Route("/api/mt5req/admin/{rid:int}/provision", admin_provision, methods=["POST"]),
    Route("/api/mt5req/admin/{rid:int}/key", admin_newkey, methods=["POST"]),
    Route("/api/mt5req/admin/{rid:int}/set", admin_set, methods=["POST"]),
]
