"""Tests for Telegram signal alerts (app/connect_alerts.py). Run: python3 tests/test_connect_alerts.py"""
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
from app import connect, connect_alerts                            # noqa: E402
from app import signal_feed as F                                   # noqa: E402

_n = [0]


def member(styles="swing,day", linked=True, tier="member", blocked=0):
    _n[0] += 1
    with closing(auth.db()) as con:
        uid = con.insert("INSERT INTO users(email,pw_hash,tier,created_at,blocked) VALUES(?,?,?,?,?)",
                         (f"a{_n[0]}@t.example", auth.hash_pw("x" * 12), tier, "2026-01-01T00:00:00", blocked))
        con.commit()
    with closing(F.db()) as con:
        con.execute("INSERT INTO user_prefs(user_id,notify_styles) VALUES(?,?)", (uid, styles))
        con.commit()
    tg = 900000 + _n[0]
    if linked:
        with closing(connect.db()) as con:
            con.execute("INSERT INTO telegram_links(user_id,telegram_id,source,linked_at,last_seen_at) VALUES(?,?,?,?,?)",
                        (uid, tg, "test", int(time.time()), int(time.time())))
            con.commit()
    return uid, tg


def signal(style="day"):
    _n[0] += 1
    with closing(F.db()) as con:
        sid = con.insert("INSERT INTO signals(created_at,bar_epoch,instrument,style,setup,direction,entry,stop,target,rr,risk,expires_at) "
                         "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                         (int(time.time()), _n[0], "XAUUSD", style, "trend", 1, 1.0, 0.9, 1.3, 3.0, "low", int(time.time()) + 3600))
        con.commit()
    return sid


class Alerts(unittest.TestCase):
    def run_alerts(self, sid):
        out = []
        n = connect_alerts.notify_new([sid], sender=lambda chat, text: out.append((chat, text)) or True)
        return n, out

    def test_a_linked_member_with_alerts_on_gets_one_message_and_only_once(self):
        _, tg = member()
        sid = signal()
        n, out = self.run_alerts(sid)
        self.assertIn(tg, [c for c, _ in out])
        self.assertNotIn("1.3", " ".join(t for _, t in out), "levels must never be sent")
        n2, out2 = self.run_alerts(sid)
        self.assertNotIn(tg, [c for c, _ in out2])

    def test_unlinked_members_style_off_and_blocked_are_skipped(self):
        _, tg_unlinked = member(linked=False)
        _, tg_style = member(styles="swing")
        _, tg_blocked = member(blocked=1)
        _, out = self.run_alerts(signal(style="day"))
        sent = [c for c, _ in out]
        for tg in (tg_style, tg_blocked):
            self.assertNotIn(tg, sent)
        self.assertEqual(len([1 for c in sent if c == tg_unlinked]), 0)

    def test_a_member_already_alerted_by_web_push_is_not_alerted_again(self):
        uid, tg = member()
        sid = signal()
        with closing(F.db()) as con:
            con.execute("INSERT INTO signal_notifs(user_id,signal_id,day) VALUES(?,?,?)", (uid, sid, F._today()))
            con.commit()
        _, out = self.run_alerts(sid)
        self.assertNotIn(tg, [c for c, _ in out])

    def test_a_failed_send_is_retried_next_time_and_never_counted(self):
        uid, tg = member()
        sid = signal()
        connect_alerts.notify_new([sid], sender=lambda c, t: False)
        with closing(F.db()) as con:
            self.assertIsNone(con.execute("SELECT 1 FROM signal_notifs WHERE user_id=? AND signal_id=?", (uid, sid)).fetchone())

    def test_also_mode_alerts_even_after_web_push_and_never_twice_or_touches_web_push(self):
        uid, tg = member()
        sid = signal()
        with closing(F.db()) as con:
            con.execute("INSERT INTO signal_notifs(user_id,signal_id,day) VALUES(?,?,?)", (uid, sid, F._today()))
            con.commit()
        os.environ["CONNECT_ALERTS_ALSO"] = "1"
        try:
            _, out = self.run_alerts(sid)
            self.assertIn(tg, [c for c, _ in out])
            _, again = self.run_alerts(sid)
            self.assertNotIn(tg, [c for c, _ in again])
            with closing(F.db()) as con:
                n = con.execute("SELECT COUNT(*) AS c FROM signal_notifs WHERE user_id=?", (uid,)).fetchone()["c"]
            self.assertEqual(n, 1, "web push records must not be written in also-mode")
            uid2, tg2 = member()
            sid2 = signal()
            connect_alerts.notify_new([sid2], sender=lambda c, t: False)
            with closing(connect.db()) as con:
                self.assertIsNone(con.execute("SELECT 1 FROM connect_alert_sent WHERE user_id=?", (uid2,)).fetchone())
        finally:
            os.environ.pop("CONNECT_ALERTS_ALSO", None)

    def test_it_is_off_by_default(self):
        for k in ("CONNECT_ALERTS", "CONNECT_SECRET", "CONNECT_BOT_TOKEN"):
            os.environ.pop(k, None)
        self.assertFalse(connect_alerts.enabled())
        os.environ.update(CONNECT_ALERTS="1", CONNECT_SECRET="c" * 40, CONNECT_BOT_TOKEN="1:t")
        self.assertTrue(connect_alerts.enabled())
        os.environ["CONNECT_ALERTS"] = "0"
        self.assertFalse(connect_alerts.enabled())


if __name__ == "__main__":
    unittest.main()
