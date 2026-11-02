"""Tests for in-app messages and broadcasts (app/inbox.py). Run: python3 tests/test_inbox.py   (own process, temp SQLite database, no network)."""
import os
import sys
import tempfile
import time
import types
import unittest
from contextlib import closing
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
try:
    import httpx  # noqa: F401
except ImportError:
    import httpx2 as httpx
    sys.modules["httpx"] = httpx
for _n in ("websockets", "pywebpush", "psycopg", "psycopg.rows"):
    try:
        __import__(_n)
    except ImportError:
        sys.modules[_n] = types.ModuleType(_n)
os.environ.update(DRIVERLINE_SECRET="s" * 40)
os.environ.pop("DATABASE_URL", None)

from app import auth, dbx, inbox                                  # noqa: E402
from starlette.applications import Starlette                      # noqa: E402
from starlette.exceptions import HTTPException                    # noqa: E402
from starlette.responses import JSONResponse                      # noqa: E402
from starlette.testclient import TestClient                       # noqa: E402

TMP = Path(tempfile.mkdtemp())
dbx.SQLITE_PATH = TMP / "t.db"


async def _err(request, exc):
    return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)


APP = Starlette(routes=[*inbox.routes], exception_handlers={HTTPException: _err})
_seq, _clients = [0], {}
ANON = TestClient(APP)
ANON.__enter__()


def mk_user(tier="free", created="2026-01-01T00:00:00", blocked=0):
    _seq[0] += 1
    with closing(auth.db()) as con:
        uid = con.insert("INSERT INTO users(email,pw_hash,tier,created_at,blocked) VALUES(?,?,?,?,?)",
                         (f"m{_seq[0]}@t.example", "h", tier, created, blocked))
        con.commit()
    return uid


def cl(uid):
    if uid not in _clients:
        c = TestClient(APP)
        c.__enter__()
        c.cookies.set(auth.COOKIE, auth.make_token(uid, "h"))
        _clients[uid] = c
    return _clients[uid]


def sql(text, *a):
    with closing(inbox.db()) as con:
        r = con.execute(text, a).fetchall()
        con.commit()
    return [dict(x) for x in r]


class Fresh(unittest.TestCase):
    """Every test starts with empty inbox tables, so broadcasts sent by one test are not unread in the next."""
    def setUp(self):
        for t in ("inbox_messages", "inbox_broadcasts", "inbox_broadcast_reads"):
            sql(f"DELETE FROM {t}")


class Access(Fresh):
    def test_signed_out_people_get_nothing(self):
        for m, p in (("get", "/api/inbox"), ("get", "/api/inbox/summary"), ("post", "/api/inbox/send"), ("post", "/api/inbox/read"),
                     ("get", "/api/admin/inbox"), ("post", "/api/admin/broadcast")):
            self.assertEqual(getattr(ANON, m)(p, **({"json": {"body": "x"}} if m == "post" else {})).status_code, 401, p)

    def test_members_cannot_use_the_admin_side(self):
        u = mk_user("member")
        c = cl(u)
        for m, p in (("get", "/api/admin/inbox"), ("get", "/api/admin/inbox/summary"), ("get", f"/api/admin/inbox/{u}"),
                     ("get", "/api/admin/broadcasts"), ("post", "/api/admin/inbox/send"), ("post", "/api/admin/broadcast"),
                     ("post", f"/api/admin/inbox/{u}/read")):
            r = getattr(c, m)(p, **({"json": {"body": "x", "user_id": u}} if m == "post" else {}))
            self.assertEqual(r.status_code, 403, p)

    def test_a_blocked_member_is_refused(self):
        u = mk_user(blocked=1)
        self.assertEqual(cl(u).get("/api/inbox").status_code, 401)


class Conversations(Fresh):
    def test_a_member_writes_the_admin_sees_it_answers_and_the_member_sees_the_answer(self):
        u, a = mk_user(), mk_user("admin")
        self.assertEqual(cl(u).post("/api/inbox/send", json={"body": "  Hello team, my signal is missing  "}).status_code, 200)
        s = cl(a).get("/api/admin/inbox/summary").json()
        self.assertEqual((s["unread"], s["conversations"]), (1, 1))
        lst = cl(a).get("/api/admin/inbox").json()["conversations"]
        row = next(c for c in lst if c["user_id"] == u)
        self.assertEqual((row["unread"], row["last"]["body"], row["last"]["sender"]), (1, "Hello team, my signal is missing", "user"))
        self.assertTrue(row["email"].endswith("@t.example"))
        t = cl(a).get(f"/api/admin/inbox/{u}").json()
        self.assertEqual([m["sender"] for m in t["messages"]], ["user"])
        self.assertEqual(cl(a).post(f"/api/admin/inbox/{u}/read").status_code, 200)
        self.assertEqual(next(c for c in cl(a).get("/api/admin/inbox").json()["conversations"] if c["user_id"] == u)["unread"], 0)
        r = cl(a).post("/api/admin/inbox/send", json={"user_id": u, "body": "Fixed, please check again"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(cl(u).get("/api/inbox/summary").json(), {"messages": 1, "broadcasts": 0, "unread": 1})
        v = cl(u).get("/api/inbox").json()
        self.assertEqual([(m["sender"], m["read"]) for m in v["messages"]], [("user", True), ("admin", False)])        # the admin opened the member's message; the answer is not read yet
        cl(u).post("/api/inbox/read")
        self.assertEqual(cl(u).get("/api/inbox/summary").json()["unread"], 0)
        self.assertTrue(all(m["read"] for m in cl(u).get("/api/inbox").json()["messages"] if m["sender"] == "admin"))

    def test_a_member_only_ever_sees_their_own_conversation(self):
        u, v, a = mk_user(), mk_user(), mk_user("admin")
        cl(u).post("/api/inbox/send", json={"body": "private from u"})
        cl(a).post("/api/admin/inbox/send", json={"user_id": u, "body": "reply to u"})
        self.assertEqual(cl(v).get("/api/inbox").json()["messages"], [])
        self.assertEqual(cl(v).get("/api/inbox/summary").json()["messages"], 0)

    def test_an_admin_can_start_a_conversation_by_email_and_unknown_members_are_refused(self):
        u, a = mk_user(), mk_user("admin")
        with closing(auth.db()) as con:
            email = con.execute("SELECT email FROM users WHERE id=?", (u,)).fetchone()["email"]
        r = cl(a).post("/api/admin/inbox/send", json={"email": email.upper(), "body": "Welcome aboard"})
        self.assertEqual((r.status_code, r.json()["user_id"]), (200, u))
        self.assertEqual(cl(u).get("/api/inbox/summary").json()["messages"], 1)
        self.assertEqual(cl(a).post("/api/admin/inbox/send", json={"email": "nobody@t.example", "body": "hi"}).status_code, 404)
        self.assertEqual(cl(a).post("/api/admin/inbox/send", json={"user_id": 999999, "body": "hi"}).status_code, 404)
        self.assertEqual(cl(a).get("/api/admin/inbox/999999").status_code, 404)

    def test_empty_and_oversized_messages_are_refused(self):
        u, a = mk_user(), mk_user("admin")
        for body in ("", "   ", None, 5, ["x"]):
            self.assertEqual(cl(u).post("/api/inbox/send", json={"body": body}).status_code, 422, repr(body))
        self.assertEqual(cl(u).post("/api/inbox/send", json={"body": "x" * (inbox.MEMBER_MAX + 1)}).status_code, 422)
        self.assertEqual(cl(u).post("/api/inbox/send", json={"body": "x" * inbox.MEMBER_MAX}).status_code, 200)
        self.assertEqual(cl(a).post("/api/admin/inbox/send", json={"user_id": u, "body": "x" * (inbox.ADMIN_MAX + 1)}).status_code, 422)
        self.assertEqual(cl(u).post("/api/inbox/send", content="not json", headers={"content-type": "application/json"}).status_code, 400)

    def test_flood_limits(self):
        u = mk_user()
        codes = [cl(u).post("/api/inbox/send", json={"body": f"m{i}"}).status_code for i in range(inbox.PER_MINUTE + 2)]
        self.assertEqual(codes, [200] * inbox.PER_MINUTE + [429, 429])
        sql("UPDATE inbox_messages SET created_at=created_at-3600 WHERE user_id=?", u)        # an hour later: the minute limit is gone
        self.assertEqual(cl(u).post("/api/inbox/send", json={"body": "again"}).status_code, 200)
        sql("UPDATE inbox_messages SET created_at=created_at-3600 WHERE user_id=?", u)
        n = sql("SELECT COUNT(*) AS c FROM inbox_messages WHERE user_id=? AND sender='user'", u)[0]["c"]
        for i in range(inbox.PER_DAY - n):
            sql("INSERT INTO inbox_messages(user_id,sender,body,created_at) VALUES(?,?,?,?)", u, "user", "fill", int(time.time()) - 3000)
        r = cl(u).post("/api/inbox/send", json={"body": "one too many"})
        self.assertEqual(r.status_code, 429)
        self.assertIn("today", r.json()["detail"])

    def test_text_is_stored_exactly_as_text_never_changed(self):
        u, a = mk_user(), mk_user("admin")
        evil = "<img src=x onerror=alert(1)> & \"quotes\" 'single'"
        cl(u).post("/api/inbox/send", json={"body": evil})
        self.assertEqual(cl(a).get(f"/api/admin/inbox/{u}").json()["messages"][0]["body"], evil)     # the pages show it with textContent

    def test_conversations_are_ordered_with_unread_first(self):
        a = mk_user("admin")
        old, new = mk_user(), mk_user()
        cl(old).post("/api/inbox/send", json={"body": "old"})
        cl(new).post("/api/inbox/send", json={"body": "new"})
        cl(a).post(f"/api/admin/inbox/{old}/read")
        ids = [c["user_id"] for c in cl(a).get("/api/admin/inbox").json()["conversations"]]
        self.assertLess(ids.index(new), ids.index(old))


class Broadcasts(Fresh):
    def test_a_broadcast_reaches_every_member_once_and_read_state_is_per_member(self):
        a, u, v = mk_user("admin"), mk_user(), mk_user("member")
        before = cl(a).get("/api/admin/broadcasts").json()["members"]
        r = cl(a).post("/api/admin/broadcast", json={"title": "Maintenance", "body": "We upgrade at 9pm"})
        self.assertEqual(r.status_code, 200)
        for m in (u, v):
            self.assertEqual(cl(m).get("/api/inbox/summary").json()["broadcasts"], 1)
        b = cl(u).get("/api/inbox").json()["broadcasts"][0]
        self.assertEqual((b["title"], b["body"], b["unread"]), ("Maintenance", "We upgrade at 9pm", True))
        cl(u).post("/api/inbox/read")
        self.assertEqual(cl(u).get("/api/inbox/summary").json()["broadcasts"], 0)
        self.assertFalse(cl(u).get("/api/inbox").json()["broadcasts"][0]["unread"])
        self.assertEqual(cl(v).get("/api/inbox/summary").json()["broadcasts"], 1)            # v has not opened it
        adm = cl(a).get("/api/admin/broadcasts").json()
        self.assertEqual(adm["members"], before)
        self.assertEqual(adm["broadcasts"][0]["seen"], 1)
        self.assertEqual(sql("SELECT COUNT(*) AS c FROM inbox_broadcasts WHERE body='We upgrade at 9pm'")[0]["c"], 1)   # stored once

    def test_only_broadcasts_after_signup_count_as_unread(self):
        a = mk_user("admin")
        cl(a).post("/api/admin/broadcast", json={"body": "from before the new member joined"})
        joined = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(time.time() + 5))
        u = mk_user(created=joined)
        self.assertEqual(cl(u).get("/api/inbox/summary").json()["broadcasts"], 0)
        shown = cl(u).get("/api/inbox").json()["broadcasts"]
        self.assertTrue(shown and not shown[0]["unread"])                                      # still readable in the list

    def test_a_double_click_does_not_send_twice_and_the_rules_hold(self):
        a = mk_user("admin")
        body = {"title": "t", "body": "double click test"}
        self.assertEqual(cl(a).post("/api/admin/broadcast", json=body).status_code, 200)
        self.assertEqual(cl(a).post("/api/admin/broadcast", json=body).status_code, 409)
        self.assertEqual(cl(a).post("/api/admin/broadcast", json={"title": "t", "body": ""}).status_code, 422)
        self.assertEqual(cl(a).post("/api/admin/broadcast", json={"title": "x" * 81, "body": "ok"}).status_code, 422)
        self.assertEqual(cl(a).post("/api/admin/broadcast", json={"body": "no title is fine"}).status_code, 200)

    def test_a_broadcast_is_not_a_private_message(self):
        a, u = mk_user("admin"), mk_user()
        cl(a).post("/api/admin/broadcast", json={"body": "to everyone, not a thread"})
        self.assertEqual(cl(u).get("/api/inbox").json()["messages"], [])
        self.assertEqual(sql("SELECT COUNT(*) AS c FROM inbox_messages WHERE user_id=?", u)[0]["c"], 0)      # nothing is copied into anyone's thread
        self.assertNotIn(u, [c["user_id"] for c in cl(a).get("/api/admin/inbox").json()["conversations"]])


if __name__ == "__main__":
    unittest.main(verbosity=2)
