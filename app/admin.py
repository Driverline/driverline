"""Admin API used by static/admin.html (admin accounts only)."""
import re
import secrets
from contextlib import closing

from starlette.exceptions import HTTPException
from starlette.responses import JSONResponse
from starlette.routing import Route

from . import auth, mailer

CODE_RE = re.compile(r"^[A-Za-z0-9_-]{4,40}$")


async def overview(request):
    me = auth.require_admin(request)
    with closing(auth.db()) as con:
        users = [dict(r) for r in con.execute(
            "SELECT u.id,u.email,u.tier,u.blocked,u.created_at,COALESCE(g.n,0) AS used_today "
            "FROM users u LEFT JOIN usage g ON g.user_id=u.id AND g.day=? ORDER BY u.id", (auth._today(),))]
        invites = [dict(r) for r in con.execute("SELECT code,tier,uses_left FROM invites ORDER BY rowid DESC")]
    for u in users:
        u["limit"] = auth.LIMITS.get(u["tier"], 0)
    return JSONResponse({
        "me": me["email"], "my_id": me["id"], "users": users, "invites": invites,
        "mail_configured": mailer.configured(),
        "totals": {"users": len(users), "member": sum(u["tier"] == "member" for u in users),
                   "free": sum(u["tier"] == "free" for u in users),
                   "ai_today": sum(u["used_today"] for u in users)},
    })


def _target(request, me):
    uid = request.path_params["uid"]
    if uid == me["id"]:
        raise HTTPException(400, "You can't change your own account here (this prevents locking yourself out)")
    return uid


async def set_tier(request):
    me = auth.require_admin(request)
    uid, d = _target(request, me), await auth._json(request)
    if d.get("tier") not in ("free", "member", "admin"):
        raise HTTPException(422, "tier must be free, member or admin")
    with closing(auth.db()) as con:
        if not con.execute("UPDATE users SET tier=? WHERE id=?", (d["tier"], uid)).rowcount:
            raise HTTPException(404, "No such user")
        con.commit()
    return JSONResponse({"ok": True})


async def set_blocked(request):
    me = auth.require_admin(request)
    uid, d = _target(request, me), await auth._json(request)
    with closing(auth.db()) as con:
        if not con.execute("UPDATE users SET blocked=? WHERE id=?", (int(bool(d.get("blocked"))), uid)).rowcount:
            raise HTTPException(404, "No such user")
        con.commit()
    return JSONResponse({"ok": True})


async def reset_link(request):
    me = auth.require_admin(request)
    uid = request.path_params["uid"]
    with closing(auth.db()) as con:
        r = con.execute("SELECT id FROM users WHERE id=?", (uid,)).fetchone()
    if not r:
        raise HTTPException(404, "No such user")
    return JSONResponse({"link": auth.reset_link(request, auth.create_reset_token(uid)), "valid_minutes": 60})


async def create_invite(request):
    auth.require_admin(request)
    d = await auth._json(request)
    tier = d.get("tier", "free")
    try:
        uses = int(d.get("uses", 1))
    except (TypeError, ValueError):
        raise HTTPException(422, "uses must be a number")
    code = str(d.get("code") or "").strip() or secrets.token_urlsafe(6)
    if tier not in ("free", "member") or not 1 <= uses <= 1000 or not CODE_RE.match(code):
        raise HTTPException(422, "Tier must be free or member, uses 1 to 1000, code 4 to 40 letters, numbers, - or _")
    with closing(auth.db()) as con:
        con.execute("INSERT INTO invites(code,tier,uses_left) VALUES(?,?,?) "
                    "ON CONFLICT(code) DO UPDATE SET tier=excluded.tier, uses_left=excluded.uses_left",
                    (code, tier, uses))
        con.commit()
    return JSONResponse({"code": code, "tier": tier, "uses": uses})


async def delete_invite(request):
    auth.require_admin(request)
    with closing(auth.db()) as con:
        con.execute("DELETE FROM invites WHERE code=?", (request.path_params["code"],))
        con.commit()
    return JSONResponse({"ok": True})


routes = [
    Route("/api/admin/overview", overview, methods=["GET"]),
    Route("/api/admin/users/{uid:int}/tier", set_tier, methods=["POST"]),
    Route("/api/admin/users/{uid:int}/block", set_blocked, methods=["POST"]),
    Route("/api/admin/users/{uid:int}/reset-link", reset_link, methods=["POST"]),
    Route("/api/admin/invites", create_invite, methods=["POST"]),
    Route("/api/admin/invites/{code}", delete_invite, methods=["DELETE"]),
]
