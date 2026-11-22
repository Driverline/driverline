"""Tests for the TradeLens Connect tables (app/connect.py): one active link per Telegram ID and per member, unlinking and relinking,
and that creating them touches nothing else. Run: python3 tests/test_connect_schema.py   (own process, temp SQLite database, no network)."""
import os
import sys
import tempfile
import time
import unittest
from contextlib import closing
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.update(DRIVERLINE_SECRET="s" * 40)
os.environ.pop("DATABASE_URL", None)

from app import auth, dbx                                          # noqa: E402

dbx.SQLITE_PATH = Path(tempfile.mkdtemp()) / "t.db"
from app import connect                                            # noqa: E402

_seq = [0]


def mk():
    _seq[0] += 1
    with closing(auth.db()) as con:
        uid = con.insert("INSERT INTO users(email,pw_hash,tier,created_at) VALUES(?,?,?,?)",
                         (f"c{_seq[0]}@t.example", "h", "free", "2026-01-01T00:00:00"))
        con.commit()
    return uid


def link(con, uid, tg, source="mini_app"):
    now = int(time.time())
    return con.insert("INSERT INTO telegram_links(user_id,telegram_id,telegram_username,source,linked_at,last_seen_at) VALUES(?,?,?,?,?,?)",
                      (uid, tg, "someone", source, now, now))


class ConnectSchemaTests(unittest.TestCase):
    def test_tables_are_created_and_creation_is_repeatable(self):
        for _ in range(3):
            with closing(connect.db()) as con:
                names = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        self.assertIn("telegram_links", names)
        self.assertIn("connect_events", names)
        self.assertIn("users", names)

    def test_one_active_link_per_telegram_id(self):
        a, b = mk(), mk()
        with closing(connect.db()) as con:
            link(con, a, 1001)
            con.commit()
            with self.assertRaises(Exception) as cm:
                link(con, b, 1001)
            self.assertTrue(dbx.is_duplicate(cm.exception))

    def test_one_active_link_per_member(self):
        a = mk()
        with closing(connect.db()) as con:
            link(con, a, 2001)
            con.commit()
            with self.assertRaises(Exception) as cm:
                link(con, a, 2002)
            self.assertTrue(dbx.is_duplicate(cm.exception))

    def test_unlink_keeps_the_row_and_allows_a_new_link(self):
        a, b = mk(), mk()
        with closing(connect.db()) as con:
            old = link(con, a, 3001)
            con.commit()
            con.execute("UPDATE telegram_links SET revoked_at=? WHERE id=?", (int(time.time()), old))
            con.commit()
            link(con, a, 3002)             # the same member links another Telegram account
            link(con, b, 3001)             # the freed Telegram account links to someone else
            con.commit()
            rows = con.execute("SELECT COUNT(*) AS c FROM telegram_links WHERE user_id=? OR telegram_id=?", (a, 3001)).fetchone()["c"]
            active = con.execute("SELECT COUNT(*) AS c FROM telegram_links WHERE revoked_at IS NULL AND (user_id=? OR telegram_id=?)",
                                 (a, 3001)).fetchone()["c"]
        self.assertEqual(rows, 3)           # the revoked row is still there for audit
        self.assertEqual(active, 2)

    def test_telegram_id_holds_a_real_telegram_sized_number(self):
        a = mk()
        big = 7_000_000_000                 # Telegram IDs already pass 32 bits
        with closing(connect.db()) as con:
            link(con, a, big)
            con.commit()
            got = con.execute("SELECT telegram_id FROM telegram_links WHERE user_id=?", (a,)).fetchone()["telegram_id"]
        self.assertEqual(got, big)

    def test_events_can_be_written_before_anyone_is_identified(self):
        with closing(connect.db()) as con:
            con.insert("INSERT INTO connect_events(telegram_id,user_id,event,props,created_at) VALUES(?,?,?,?,?)",
                       (None, None, "miniapp_opened", '{"linked":false}', int(time.time())))
            con.commit()
            n = con.execute("SELECT COUNT(*) AS c FROM connect_events WHERE event='miniapp_opened'").fetchone()["c"]
        self.assertEqual(n, 1)

    def test_event_names_match_the_spec(self):
        self.assertEqual(len(connect.EVENTS), 10)
        self.assertEqual(len(set(connect.EVENTS)), 10)
        for core in ("announce_sent", "miniapp_opened", "link_completed", "upgrade_tapped"):
            self.assertIn(core, connect.EVENTS)

    def test_existing_tables_are_untouched(self):
        with closing(auth.db()) as con:
            before = [r[1] for r in con.raw.execute("PRAGMA table_info(users)")]
        connect.db().close()
        with closing(auth.db()) as con:
            after = [r[1] for r in con.raw.execute("PRAGMA table_info(users)")]
        self.assertEqual(before, after)

    def test_sql_file_matches_the_module(self):
        sql = (ROOT / "migrations" / "2026_11_connect.sql").read_text()
        for needle in ("telegram_links", "connect_events", "ux_tglink_telegram", "ux_tglink_user", "ix_cevents_event",
                       "ENABLE ROW LEVEL SECURITY"):
            self.assertIn(needle, sql)
        for needle in ("telegram_id BIGINT NOT NULL", "revoked_at BIGINT", "WHERE revoked_at IS NULL"):
            self.assertIn(needle, sql)
            self.assertIn(needle, connect.SCHEMA)


if __name__ == "__main__":
    unittest.main()
