"""The real application serves the messaging and perk endpoints, with the right permissions (app/main.py wiring).
Run: python3 tests/test_app_wiring.py   (own process, temp SQLite database, no network). The background jobs of the app are not started."""
import os
import sys
import tempfile
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

from app import auth, dbx                                          # noqa: E402

dbx.SQLITE_PATH = Path(tempfile.mkdtemp()) / "t.db"
from starlette.testclient import TestClient                        # noqa: E402
from app import main                                               # noqa: E402

_seq = [0]


def mk(tier):
    _seq[0] += 1
    with closing(auth.db()) as con:
        uid = con.insert("INSERT INTO users(email,pw_hash,tier,created_at) VALUES(?,?,?,?)", (f"w{_seq[0]}@t.example", "h", tier, "2026-01-01T00:00:00"))
        con.commit()
    c = TestClient(main.app)           # no "with": the lifespan (background jobs) is not started
    c.cookies.set(auth.COOKIE, auth.make_token(uid, "h"))
    return uid, c


class Wiring(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mid, cls.m = mk("free")
        cls.aid, cls.a = mk("admin")
        cls.oid, cls.o = mk("free")
        cls.anon = TestClient(main.app)

    def test_conversation_and_broadcast_end_to_end(self):
        self.assertEqual(self.m.post("/api/inbox/send", json={"body": "hello <b>x</b>"}).status_code, 200)
        self.assertGreaterEqual(self.a.get("/api/admin/inbox/summary").json()["unread"], 1)
        self.assertEqual(self.a.post("/api/admin/inbox/send", json={"user_id": self.mid, "body": "how can we help?"}).status_code, 200)
        v = self.m.get("/api/inbox").json()
        self.assertEqual((v["unread"], v["messages"][-1]["sender"], v["messages"][0]["body"]), (1, "admin", "hello <b>x</b>"))
        self.assertEqual(self.o.get("/api/inbox").json()["messages"], [])                 # another member sees none of it
        self.assertEqual(self.a.post("/api/admin/broadcast", json={"title": "Update", "body": "New perks"}).status_code, 200)
        self.assertEqual(self.o.get("/api/inbox/summary").json()["broadcasts"], 1)
        self.m.post("/api/inbox/read")
        self.assertEqual(self.m.get("/api/inbox/summary").json()["unread"], 0)

    def test_permissions(self):
        self.assertEqual(self.anon.get("/api/inbox").status_code, 401)
        self.assertEqual(self.m.get("/api/admin/inbox").status_code, 403)
        self.assertEqual(self.m.post("/api/admin/broadcast", json={"body": "x"}).status_code, 403)
        self.assertEqual(self.m.post("/api/admin/inbox/send", json={"user_id": self.oid, "body": "x"}).status_code, 403)
        self.assertEqual(self.m.get("/api/admin/perks").status_code, 403)

    def test_pages_and_nav(self):
        for p in ("/inbox.html", "/admin-messages.html", "/billing.html"):
            self.assertEqual(self.anon.get(p).status_code, 200, p)
        js = self.anon.get("/auth.js").text
        self.assertIn("'Messages','/inbox.html'", js)
        self.assertIn("/admin-messages.html", js)

    def test_billing_and_admin_perks_show_the_new_numbers(self):
        b = self.m.get("/api/billing").json()
        self.assertEqual(b["welcome"], 3)
        d = self.a.get("/api/admin/perks").json()
        self.assertEqual((d["slots"], d["min_balance"], d["signals"]), (3, 100.0, 5))


if __name__ == "__main__":
    unittest.main(verbosity=2)
