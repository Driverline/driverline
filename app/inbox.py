"""In-app messages between members and the TradeLens team, and broadcasts from the team to every member.

  * One conversation per member with "the team". The member writes in the Messages tab; any admin can read and answer it in
    Admin > Messages. An admin can also start a conversation with a member.
  * A broadcast is one announcement written by an admin and shown to every member in their Messages tab. It is stored once, not
    copied per member, and each member only has a "seen up to" marker.
  * Plain text only. The pages show it as text, never as HTML.
  * Limits protect the admins from floods: length of a message, messages per minute and per day from one member.
Everything is additive: it creates its own tables and touches nothing else. Mirrored by migrations/2026_11_inbox.sql."""
import calendar
import time
from contextlib import closing

from starlette.exceptions import HTTPException
from starlette.responses import JSONResponse
from starlette.routing import Route

from . import auth, dbx

SCHEMA = """
    CREATE TABLE IF NOT EXISTS inbox_messages(id {ID}, user_id INTEGER NOT NULL, sender TEXT NOT NULL, admin_id INTEGER,
        body TEXT NOT NULL, created_at BIGINT NOT NULL, read_at BIGINT);
    CREATE INDEX IF NOT EXISTS ix_inbox_user ON inbox_messages(user_id, id);
    CREATE INDEX IF NOT EXISTS ix_inbox_unread ON inbox_messages(sender, read_at);
    CREATE TABLE IF NOT EXISTS inbox_broadcasts(id {ID}, admin_id INTEGER NOT NULL, title TEXT, body TEXT NOT NULL, created_at BIGINT NOT NULL);
    CREATE TABLE IF NOT EXISTS inbox_broadcast_reads(user_id INTEGER PRIMARY KEY, last_id BIGINT NOT NULL DEFAULT 0, updated_at BIGINT NOT NULL)"""

MEMBER_MAX = 1000          # characters in one message from a member
ADMIN_MAX = 2000           # characters in one message or broadcast from an admin
TITLE_MAX = 80
PER_MINUTE = 5             # messages one member may send in a minute
PER_DAY = 30               # ... and in 24 hours
THREAD_SHOWN = 200         # messages shown in one conversation
BROADCASTS_SHOWN = 30
INBOX_SHOWN = 100          # conversations listed to the admin


def db():
    auth.db().close()
    return dbx.connect("inbox", SCHEMA)


def _now() -> int:
    return int(time.time())


def _joined(user: dict) -> int:
    """When the member signed up (epoch seconds). Broadcasts sent before that are not counted as unread."""
    try:
        return calendar.timegm(time.strptime(str(user.get("created_at")), "%Y-%m-%dT%H:%M:%S"))
    except (ValueError, TypeError):
        return 0


def _text(d: dict, key: str, limit: int, label: str, required: bool = True) -> str:
    v = d.get(key)
    v = v.strip() if isinstance(v, str) else ""
    if required and not v:
        raise HTTPException(422, f"Write a {label} first")
    if len(v) > limit:
        raise HTTPException(422, f"That {label} is too long (at most {limit} characters)")
    return v


def _msg(r) -> dict:
    return {"id": r["id"], "sender": r["sender"], "body": r["body"], "created_at": r["created_at"], "read": r["read_at"] is not None}


def _bc(r, unread=False) -> dict:
    return {"id": r["id"], "title": r["title"] or "", "body": r["body"], "created_at": r["created_at"], "unread": unread}


# ---------------------------------------------------------------- member side
def _counts(con, user: dict) -> dict:
    msgs = con.execute("SELECT COUNT(*) AS c FROM inbox_messages WHERE user_id=? AND sender='admin' AND read_at IS NULL",
                       (user["id"],)).fetchone()["c"]
    seen = con.execute("SELECT last_id FROM inbox_broadcast_reads WHERE user_id=?", (user["id"],)).fetchone()
    bc = con.execute("SELECT COUNT(*) AS c FROM inbox_broadcasts WHERE id>? AND created_at>=?",
                     (seen["last_id"] if seen else 0, _joined(user))).fetchone()["c"]
    return {"messages": msgs, "broadcasts": bc, "unread": msgs + bc}


async def member_summary(request):
    user = auth.require_user(request)
    with closing(db()) as con:
        return JSONResponse(_counts(con, user))


async def member_view(request):
    user = auth.require_user(request)
    with closing(db()) as con:
        c = _counts(con, user)
        seen = con.execute("SELECT last_id FROM inbox_broadcast_reads WHERE user_id=?", (user["id"],)).fetchone()
        last, joined = (seen["last_id"] if seen else 0), _joined(user)
        rows = con.execute("SELECT * FROM (SELECT * FROM inbox_messages WHERE user_id=? ORDER BY id DESC LIMIT ?) t ORDER BY id",
                           (user["id"], THREAD_SHOWN)).fetchall()
        bcs = con.execute("SELECT * FROM inbox_broadcasts ORDER BY id DESC LIMIT ?", (BROADCASTS_SHOWN,)).fetchall()
    return JSONResponse({**c, "messages": [_msg(r) for r in rows],
                         "broadcasts": [_bc(r, r["id"] > last and r["created_at"] >= joined) for r in bcs]})


async def member_read(request):
    """The member has opened the tab: everything shown is now read."""
    user = auth.require_user(request)
    now = _now()
    with closing(db()) as con:
        con.execute("UPDATE inbox_messages SET read_at=? WHERE user_id=? AND sender='admin' AND read_at IS NULL", (now, user["id"]))
        top = con.execute("SELECT COALESCE(MAX(id),0) AS m FROM inbox_broadcasts").fetchone()["m"]
        con.execute("INSERT INTO inbox_broadcast_reads(user_id,last_id,updated_at) VALUES(?,?,?) ON CONFLICT(user_id) DO UPDATE SET "
                    "last_id=excluded.last_id, updated_at=excluded.updated_at", (user["id"], top, now))
        con.commit()
    return JSONResponse({"ok": True})


async def member_send(request):
    user = auth.require_user(request)
    body = _text(await auth._json(request), "body", MEMBER_MAX, "message")
    now = _now()
    with closing(db()) as con:
        recent = con.execute("SELECT COUNT(*) AS c FROM inbox_messages WHERE user_id=? AND sender='user' AND created_at>?",
                             (user["id"], now - 60)).fetchone()["c"]
        today = con.execute("SELECT COUNT(*) AS c FROM inbox_messages WHERE user_id=? AND sender='user' AND created_at>?",
                            (user["id"], now - 86400)).fetchone()["c"]
        if recent >= PER_MINUTE:
            raise HTTPException(429, "You are sending messages too fast. Wait a minute and try again.")
        if today >= PER_DAY:
            raise HTTPException(429, "You have reached today's message limit. We will answer the ones you already sent.")
        mid = con.insert("INSERT INTO inbox_messages(user_id,sender,body,created_at) VALUES(?,?,?,?)", (user["id"], "user", body, now))
        con.commit()
    return JSONResponse({"ok": True, "id": mid})


# ---------------------------------------------------------------- admin side
def _people(ids) -> dict:
    ids = sorted({int(i) for i in ids})
    if not ids:
        return {}
    with closing(auth.db()) as con:
        rows = con.execute(f"SELECT id, email, tier FROM users WHERE id IN ({','.join('?' * len(ids))})", ids).fetchall()
    return {r["id"]: {"email": r["email"], "tier": r["tier"]} for r in rows}


async def admin_summary(request):
    auth.require_admin(request)
    with closing(db()) as con:
        n = con.execute("SELECT COUNT(*) AS c, COUNT(DISTINCT user_id) AS u FROM inbox_messages WHERE sender='user' AND read_at IS NULL").fetchone()
    return JSONResponse({"unread": n["c"], "conversations": n["u"]})


async def admin_list(request):
    auth.require_admin(request)
    with closing(db()) as con:
        rows = con.execute("SELECT user_id, MAX(id) AS last_id, COUNT(*) AS n, "
                           "SUM(CASE WHEN sender='user' AND read_at IS NULL THEN 1 ELSE 0 END) AS unread "
                           "FROM inbox_messages GROUP BY user_id ORDER BY unread DESC, last_id DESC LIMIT ?", (INBOX_SHOWN,)).fetchall()
        last = {}
        if rows:
            ids = [r["last_id"] for r in rows]
            for m in con.execute(f"SELECT id, sender, body, created_at FROM inbox_messages WHERE id IN ({','.join('?' * len(ids))})", ids).fetchall():
                last[m["id"]] = m
    who = _people(r["user_id"] for r in rows)
    out = []
    for r in rows:
        m = last.get(r["last_id"])
        out.append({"user_id": r["user_id"], "email": (who.get(r["user_id"]) or {}).get("email", "?"),
                    "tier": (who.get(r["user_id"]) or {}).get("tier"), "unread": int(r["unread"] or 0), "count": r["n"],
                    "last": {"sender": m["sender"], "body": m["body"][:140], "created_at": m["created_at"]} if m else None})
    return JSONResponse({"conversations": out})


async def admin_thread(request):
    auth.require_admin(request)
    uid = request.path_params["uid"]
    who = _people([uid]).get(uid)
    if not who:
        raise HTTPException(404, "No such member")
    with closing(db()) as con:
        rows = con.execute("SELECT * FROM (SELECT * FROM inbox_messages WHERE user_id=? ORDER BY id DESC LIMIT ?) t ORDER BY id",
                           (uid, THREAD_SHOWN)).fetchall()
    return JSONResponse({"user": {"id": uid, **who}, "messages": [_msg(r) for r in rows]})


async def admin_read(request):
    auth.require_admin(request)
    uid = request.path_params["uid"]
    with closing(db()) as con:
        con.execute("UPDATE inbox_messages SET read_at=? WHERE user_id=? AND sender='user' AND read_at IS NULL", (_now(), uid))
        con.commit()
    return JSONResponse({"ok": True})


async def admin_send(request):
    """Answer a member, or start a conversation with one (by user_id or email)."""
    admin = auth.require_admin(request)
    d = await auth._json(request)
    body = _text(d, "body", ADMIN_MAX, "message")
    uid = d.get("user_id")
    with closing(auth.db()) as con:
        if isinstance(uid, int) and not isinstance(uid, bool):
            r = con.execute("SELECT id FROM users WHERE id=?", (uid,)).fetchone()
        else:
            email = str(d.get("email", "")).strip().lower()
            r = con.execute("SELECT id FROM users WHERE lower(email)=?", (email,)).fetchone() if email else None
    if not r:
        raise HTTPException(404, "No member with that email")
    with closing(db()) as con:
        mid = con.insert("INSERT INTO inbox_messages(user_id,sender,admin_id,body,created_at) VALUES(?,?,?,?,?)",
                         (r["id"], "admin", admin["id"], body, _now()))
        # answering a conversation means the admin has read what the member wrote
        con.execute("UPDATE inbox_messages SET read_at=? WHERE user_id=? AND sender='user' AND read_at IS NULL", (_now(), r["id"]))
        con.commit()
    return JSONResponse({"ok": True, "id": mid, "user_id": r["id"]})


async def admin_broadcasts(request):
    auth.require_admin(request)
    with closing(db()) as con:
        rows = con.execute("SELECT * FROM inbox_broadcasts ORDER BY id DESC LIMIT ?", (BROADCASTS_SHOWN,)).fetchall()
        seen = {r["id"]: con.execute("SELECT COUNT(*) AS c FROM inbox_broadcast_reads WHERE last_id>=?", (r["id"],)).fetchone()["c"]
                for r in rows}
    with closing(auth.db()) as con:
        total = con.execute("SELECT COUNT(*) AS c FROM users WHERE blocked=0").fetchone()["c"]
    return JSONResponse({"members": total, "broadcasts": [{**_bc(r), "seen": seen[r["id"]]} for r in rows]})


async def admin_broadcast(request):
    admin = auth.require_admin(request)
    d = await auth._json(request)
    title = _text(d, "title", TITLE_MAX, "title", required=False)
    body = _text(d, "body", ADMIN_MAX, "broadcast")
    now = _now()
    with closing(db()) as con:
        if con.execute("SELECT 1 FROM inbox_broadcasts WHERE body=? AND COALESCE(title,'')=? AND created_at>?",
                       (body, title, now - 60)).fetchone():
            raise HTTPException(409, "That exact broadcast was just sent")     # a double click
        bid = con.insert("INSERT INTO inbox_broadcasts(admin_id,title,body,created_at) VALUES(?,?,?,?)", (admin["id"], title, body, now))
        con.commit()
    print(f"[inbox] broadcast {bid} sent by admin {admin['id']}", flush=True)
    return JSONResponse({"ok": True, "id": bid})


routes = [
    Route("/api/inbox/summary", member_summary, methods=["GET"]),
    Route("/api/inbox", member_view, methods=["GET"]),
    Route("/api/inbox/read", member_read, methods=["POST"]),
    Route("/api/inbox/send", member_send, methods=["POST"]),
    Route("/api/admin/inbox/summary", admin_summary, methods=["GET"]),
    Route("/api/admin/inbox", admin_list, methods=["GET"]),
    Route("/api/admin/inbox/send", admin_send, methods=["POST"]),
    Route("/api/admin/inbox/{uid:int}", admin_thread, methods=["GET"]),
    Route("/api/admin/inbox/{uid:int}/read", admin_read, methods=["POST"]),
    Route("/api/admin/broadcasts", admin_broadcasts, methods=["GET"]),
    Route("/api/admin/broadcast", admin_broadcast, methods=["POST"]),
]
