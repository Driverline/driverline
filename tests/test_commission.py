"""Tests that commission sharing is retired (app/commission.py): history stays readable, nothing new can be created.
Run: python3 tests/test_commission.py   (own process, temp SQLite database, no network)."""
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

from app import auth, commission, dbx   # noqa: E402

dbx.SQLITE_PATH = Path(tempfile.mkdtemp()) / "t.db"
from starlette.testclient import TestClient   # noqa: E402
from app import main                          # noqa: E402

_seq = [0]


def mk_user(tier="free"):
    _seq[0] += 1
    with closing(auth.db()) as con:
        uid = con.insert("INSERT INTO users(email,pw_hash,tier,created_at,blocked) VALUES(?,?,?,?,0)", (f"c{_seq[0]}@t.example", "h", tier, "2026-01-01T00:00:00"))
        con.commit()
    return uid


def client(uid):
    c = TestClient(main.app)
    c.cookies.set(auth.COOKIE, auth.make_token(uid, "h"))
    return c


INSERTS = {
    "commission_rows": "INSERT INTO commission_rows(period,client_id,referred_id,referrer_id,commission_cents,pct,share_cents,created_at) VALUES('2026-10','c',1,2,100,10,10,0)",
    "commission_imports": "INSERT INTO commission_imports(period,rows_total,rows_shared,commission_cents,share_cents,created_at) VALUES('2026-10',1,1,100,10,0)",
    "payout_details": "INSERT INTO payout_details(user_id,network,address,updated_at) VALUES(1,'trc20','T123',0)",
    "payout_requests": "INSERT INTO payout_requests(user_id,amount_cents,network,address,created_at) VALUES(1,1000,'trc20','T123',0)",
}


class Retired(unittest.TestCase):
    def test_no_new_commission_row_import_payout_request_or_address_can_be_created(self):
        for table, sql in INSERTS.items():
            with closing(commission.db()) as con:
                with self.assertRaises(Exception, msg=table) as cm:
                    con.execute(sql)
                self.assertIn("retired", str(cm.exception).lower())
                con.raw.rollback()
                self.assertEqual(con.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"], 0)

    def test_history_is_read_only_and_empty_when_nothing_was_ever_recorded(self):
        h = commission.history()
        self.assertTrue(h["retired"])
        self.assertEqual((h["rows"], h["shared_cents"], h["imports"], h["payout_requests"]), (0, 0, [], []))

    def test_old_records_stay_readable_if_they_exist(self):
        with closing(commission.db()) as con:       # simulate a record written before retirement: drop the guard, insert, restore it
            con.execute("DROP TRIGGER trg_commission_rows_retired")
            con.execute(INSERTS["commission_rows"])
            commission._block_new_rows(con)          # put the guard back (it is safe to run again)
            con.commit()
        self.assertEqual(commission.history()["rows"], 1)
        with closing(commission.db()) as con:
            with self.assertRaises(Exception):
                con.execute(INSERTS["commission_rows"].replace("'c'", "'d'"))

    def test_only_a_read_only_admin_route_remains_and_members_cannot_use_it(self):
        self.assertEqual([(r.path, sorted(r.methods - {"HEAD"})) for r in commission.routes], [("/api/admin/commission", ["GET"])])
        member, admin = client(mk_user()), client(mk_user("admin"))
        self.assertIn(member.get("/api/admin/commission").status_code, (401, 403))
        self.assertEqual(admin.get("/api/admin/commission").status_code, 200)
        for path in ("/api/commission", "/api/commission/payout", "/api/admin/commission/import", "/api/admin/commission/payout"):
            self.assertIn(admin.post(path, json={}).status_code, (404, 405), path)


if __name__ == "__main__":
    unittest.main(verbosity=1)
