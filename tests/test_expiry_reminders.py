"""Tests for the plan-expiry reminders (app/expiry_reminders.py). Run: python3 tests/test_expiry_reminders.py (temp SQLite, no network)."""
import sys
import time
import unittest
from contextlib import closing
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_copy_trading as T                                          # noqa: E402
from test_copy_trading import mk_user, q, cl, ea                       # noqa: E402
from app import auth, credits, expiry_reminders as X, inbox, push      # noqa: E402

DAY = 86400
NOW = int(time.time())


def sub(uid, plan, ends):
    with closing(credits.db()) as c:
        c.insert("INSERT INTO billing_subs(user_id,plan,starts_at,ends_at,order_id) VALUES(?,?,?,?,?)", (uid, plan, ends - 30 * DAY, ends, 0))
        c.commit()


def msgs(uid):
    with closing(inbox.db()) as c:
        return [r["body"] for r in c.execute("SELECT body FROM inbox_messages WHERE user_id=? ORDER BY id", (uid,)).fetchall()]


def push_sub(uid):
    with closing(push.db()) as c:
        c.insert("INSERT INTO push_subs(user_id,endpoint,p256dh,auth,created_at) VALUES(?,?,?,?,?)", (uid, f"https://e/{uid}", "k", "a", NOW))
        c.commit()


class Expiry(unittest.TestCase):
    def setUp(self):
        credits.billing_on = lambda: True
        self.sent = []
        self.sender = lambda s, p: self.sent.append((s["user_id"], p)) or True
        self.u = mk_user("free")

    def run_at(self, t=None):
        real = credits._now
        credits._now = lambda: t or NOW                         # the plan checks inside the job must see the same pretend time
        try:
            return X.run_once(t or NOW, self.sender)
        finally:
            credits._now = real

    def test_three_day_one_day_and_ended_each_once(self):
        push_sub(self.u)
        sub(self.u, "pro", NOW + 2 * DAY + 3600)
        self.run_at()
        self.run_at()                                                                       # a repeat sends nothing more
        self.assertEqual(len(msgs(self.u)), 1)
        self.assertIn("ends on", msgs(self.u)[0])
        self.assertEqual(len(self.sent), 1)
        self.run_at(NOW + 2 * DAY)                                                           # now inside the last day
        self.assertIn("ends tomorrow", msgs(self.u)[1])
        self.run_at(NOW + 2 * DAY + 600)
        self.assertEqual(len(msgs(self.u)), 2)
        self.run_at(NOW + 2 * DAY + 7200)                                                    # it has ended
        last = msgs(self.u)[2]
        self.assertIn("has ended", last)
        self.assertIn("MT5 connection and AutoTrading", last)
        self.assertEqual(len(msgs(self.u)), 3)

    def test_one_message_lists_every_service_that_is_due(self):
        sub(self.u, "pro", NOW + 2 * DAY)                                                    # notifications + MT5 + copy trading
        self.run_at()
        self.assertEqual(len(msgs(self.u)), 1)
        for name in ("Signal notifications", "MT5 connection and AutoTrading", "Copy trading"):
            self.assertIn(name, msgs(self.u)[0])

    def test_renewing_moves_the_end_so_no_ended_notice_and_a_new_cycle_starts(self):
        sub(self.u, "pro", NOW + 2 * DAY)
        self.run_at()
        sub(self.u, "pro", NOW + 32 * DAY)                                                   # renewed: stacks on top
        self.run_at(NOW + 2 * DAY + 100)
        self.assertEqual(len(msgs(self.u)), 1)
        self.run_at(NOW + 30 * DAY + 1000)                                                   # the new end gets its own reminder
        self.assertEqual(len(msgs(self.u)), 2)

    def test_only_the_service_that_stops_is_mentioned(self):
        sub(self.u, "pro", NOW + 40 * DAY)
        sub(self.u, "copy", NOW + 2 * DAY)                                                   # but Pro still provides copy trading until day 40
        self.run_at()
        self.assertEqual(msgs(self.u), [])

    def test_copy_trading_grants_count(self):
        q("INSERT INTO copy_subs(user_id,ends_at,note,updated_at) VALUES(?,?,?,?)", self.u, NOW + DAY // 2, "t", NOW)
        self.run_at()
        self.assertEqual(len(msgs(self.u)), 1)
        self.assertIn("Copy trading", msgs(self.u)[0])

    def test_nothing_for_members_blocked_admins_old_lapses_or_when_billing_is_off(self):
        member = mk_user("member")
        sub(member, "pro", NOW + DAY)                                                        # a member keeps notifications and MT5 anyway; copy is separate
        blocked = mk_user("free")
        sub(blocked, "pro", NOW + DAY)
        with closing(auth.db()) as c:
            c.execute("UPDATE users SET blocked=1 WHERE id=?", (blocked,))
            c.commit()
        old = mk_user("free")
        sub(old, "pro", NOW - 30 * DAY)                                                      # lapsed a month ago: never announced
        admin = mk_user("admin")
        sub(admin, "pro", NOW + DAY)
        self.run_at()
        self.assertEqual(msgs(blocked) + msgs(old) + msgs(admin), [])
        self.assertTrue(all("Copy trading" in m and "Signal notifications" not in m for m in msgs(member)))
        credits.billing_on = lambda: False
        again = mk_user("free")
        sub(again, "pro", NOW + DAY)
        self.run_at()
        self.assertEqual(msgs(again), [])

    def test_admin_list(self):
        sub(self.u, "pro", NOW + 2 * DAY)
        admin = mk_user("admin")
        r = cl(admin).get("/api/admin/expiring")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIn(self.u, [m["user_id"] for m in r.json()["members"]])
        self.assertEqual(cl(self.u).get("/api/admin/expiring").status_code, 403)


if __name__ == "__main__":
    T.APP.routes.extend(X.routes)
    unittest.main()
