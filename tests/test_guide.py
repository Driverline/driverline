"""The in-app guided tour: the page is served, /api/auth/me tells the Home page when the member joined, and Home links to the guide.
Run: python3 tests/test_guide.py"""
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


class Guide(unittest.TestCase):
    def test_the_tour_page_is_served_and_complete(self):
        r = TestClient(main.app).get("/guide.html")
        self.assertEqual(r.status_code, 200)
        for needle in ("Back to Home", "Start the tour", "data:image/webp;base64,"):
            self.assertIn(needle, r.text)
        for feature in ("Analysis", "Signals", "Alerts", "Risk", "Journal", "MT5", "Billing", "Messages"):
            self.assertIn("name:'" + feature + "'", r.text)

    def test_home_links_to_the_guide_and_holds_the_prompt(self):
        t = TestClient(main.app).get("/").text
        self.assertIn('href="/guide.html"', t)
        self.assertIn('id="tourbar"', t)

    def test_me_reports_when_the_member_joined(self):
        with closing(auth.db()) as con:
            uid = con.insert("INSERT INTO users(email,pw_hash,tier,created_at) VALUES(?,?,?,?)", ("g@t.example", "h", "free", "2026-11-01T08:00:00"))
            con.commit()
        c = TestClient(main.app)
        c.cookies.set(auth.COOKIE, auth.make_token(uid, "h"))
        d = c.get("/api/auth/me").json()
        self.assertEqual((d["email"], d["joined"]), ("g@t.example", "2026-11-01"))
        self.assertIn("tier", d)


if __name__ == "__main__":
    unittest.main()
