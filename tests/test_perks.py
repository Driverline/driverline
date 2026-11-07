"""Tests for the sign-up signals and partner / founding-member perks (app/credits.py, app/perks.py).
Run: python3 tests/test_perks.py   (own process, temp SQLite database, no network, no broker)."""
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
os.environ["PARTNER_PERKS_ON"] = "1"      # new partner perks are off by default (Nov 2026); these tests exercise the mechanism itself

from app import auth, credits, dbx, perks                       # noqa: E402

TMP = Path(tempfile.mkdtemp())
dbx.SQLITE_PATH = TMP / "t.db"
_seq = [0]
PARTNERS = set()
perks._confirmed = lambda uid: uid in PARTNERS
credits.billing_on = lambda: True


def mk_user(tier="free"):
    _seq[0] += 1
    with closing(auth.db()) as con:
        uid = con.insert("INSERT INTO users(email,pw_hash,tier,created_at) VALUES(?,?,?,?)", (f"p{_seq[0]}@t.example", "h", tier, "2026-01-01T00:00:00"))
        con.commit()
    return uid


def ledger(uid):
    with closing(credits.db()) as con:
        return [(r["kind"], r["delta"]) for r in con.execute("SELECT kind, delta FROM credit_ledger WHERE user_id=? ORDER BY id", (uid,)).fetchall()]


def perk(uid):
    with closing(perks.db()) as con:
        r = con.execute("SELECT * FROM partner_perks WHERE user_id=?", (uid,)).fetchone()
    return dict(r) if r else None


def plans(uid):
    return sorted(p["plan"] for p in credits.active_plans(uid))


def report(uid, login, balance=150.0, server="Deriv-Server", demo=False, currency="USD"):
    perks.verify_founder(uid, {"login": login, "server": server, "balance": balance, "demo": demo, "currency": currency})


def partner(tier="free", welcome_first=True):
    """A free member who is confirmed as a partner member. By default they already received the welcome signals at sign-up."""
    u = mk_user(tier)
    if welcome_first:
        credits.ensure_welcome(u)
    PARTNERS.add(u)
    return u


class SignupSignals(unittest.TestCase):
    def test_welcome_is_three(self):
        self.assertEqual(credits.WELCOME, 3)
        u = mk_user()
        credits.ensure_welcome(u)
        credits.ensure_welcome(u)                                     # safe to repeat
        self.assertEqual(ledger(u), [("welcome", 3)])
        self.assertEqual(credits.balance(u), 3)

    def test_a_partner_member_ends_with_five_in_total_not_eight(self):
        u = partner()
        self.assertEqual(credits.balance(u), 3)                       # the welcome signals come first, at sign-up
        self.assertTrue(perks.sync(u))
        self.assertTrue(perks.sync(u))                                # and the sync runs every few minutes: nothing doubles
        self.assertEqual(credits.balance(u), 5)
        self.assertEqual(ledger(u), [("welcome", 3), ("partner_welcome", 2)])

    def test_a_partner_confirmed_before_any_welcome_gets_five_and_no_welcome(self):
        u = partner(welcome_first=False)
        perks.sync(u)
        credits.ensure_welcome(u)                                     # arrives afterwards: must not add 3 on top
        credits.ensure_welcome(u)
        self.assertEqual(ledger(u), [("partner_welcome", 5)])
        self.assertEqual(credits.balance(u), 5)

    def test_a_member_who_already_used_their_welcome_signals_still_totals_five(self):
        u = partner()
        self.assertTrue(credits.spend(u, "signal:1"))
        self.assertTrue(credits.spend(u, "signal:2"))
        perks.sync(u)
        self.assertEqual(credits.balance(u), 3)                       # 5 given, 2 spent
        self.assertEqual(sum(d for k, d in ledger(u) if k != "spend"), 5)

    def test_old_members_keep_what_they_have_nothing_is_taken_back(self):
        u = mk_user()
        with closing(credits.db()) as con:                            # a member from before this change: welcome 5 and partner 5
            credits._credit(con, u, 5, "welcome", "welcome", "Welcome signals")
            credits._credit(con, u, 5, "partner_welcome", "partner", "Partner member bonus signals")
            con.commit()
        PARTNERS.add(u)
        perks.sync(u)
        self.assertEqual(credits.balance(u), 10)
        self.assertEqual(len(ledger(u)), 2)

    def test_an_old_welcome_of_five_gets_no_extra_when_confirmed_later(self):
        u = mk_user()
        with closing(credits.db()) as con:
            credits._credit(con, u, 5, "welcome", "welcome", "Welcome signals")
            con.commit()
        PARTNERS.add(u)
        perks.sync(u)
        self.assertEqual(credits.balance(u), 5)
        self.assertEqual(ledger(u), [("welcome", 5)])

    def test_not_a_partner_member_gets_no_partner_bonus(self):
        u = mk_user()
        credits.ensure_welcome(u)
        self.assertFalse(perks.sync(u))
        self.assertEqual(credits.balance(u), 3)

    def test_paid_members_are_untouched(self):
        u = mk_user("member")
        PARTNERS.add(u)
        self.assertFalse(perks.sync(u))
        self.assertEqual(credits.balance(u), 0)

    def test_partner_members_no_longer_get_free_notifications(self):
        u = partner()
        perks.sync(u)
        self.assertEqual(plans(u), [])
        self.assertFalse(credits.entitled({"id": u, "tier": "free"}, "notify"))
        self.assertFalse(credits.entitled({"id": u, "tier": "free"}, "mt5"))

    def test_partner_pro_price_is_unchanged(self):
        u = partner()
        self.assertEqual(perks.price_for({"id": u, "tier": "free"}, "pro", 30.0), 15.0)


class FoundingMembers(unittest.TestCase):
    def setUp(self):
        with closing(perks.db()) as con:                             # every test starts with the three places free
            con.execute("DELETE FROM partner_perks")
            con.commit()
        with closing(credits.db()) as con:
            con.execute("DELETE FROM billing_subs")
            con.commit()

    def synced(self, n=1):
        us = [partner() for _ in range(n)]
        for u in us:
            perks.sync(u)
        return us

    def test_three_places_and_the_minimum_is_one_hundred(self):
        self.assertEqual((perks.FOUNDER_SLOTS, perks.FOUNDER_MIN_BALANCE), (3, 100.0))

    def test_a_confirmed_partner_waits_with_no_rank_and_can_connect_to_be_read(self):
        (u,) = self.synced()
        p = perk(u)
        self.assertEqual((p["founder_status"], p["founder_rank"]), ("pending", None))
        self.assertTrue(perks.founder_pending(u))
        self.assertTrue(credits.mt5_can_report({"id": u, "tier": "free"}))
        self.assertFalse(credits.entitled({"id": u, "tier": "free"}, "mt5"))     # connecting to report is not the free MT5

    def test_funding_unlocks_mt5_and_notifications_for_thirty_days(self):
        (u,) = self.synced()
        report(u, "1001", 100.0)                                     # exactly $100 counts
        p = perk(u)
        self.assertEqual((p["founder_status"], p["founder_rank"], p["founder_login"]), ("verified", 1, "1001"))
        self.assertEqual(plans(u), ["founder_mt5"])
        user = {"id": u, "tier": "free"}
        self.assertTrue(credits.entitled(user, "mt5"))
        self.assertTrue(credits.entitled(user, "notify"))
        with closing(credits.db()) as con:
            s = con.execute("SELECT starts_at, ends_at FROM billing_subs WHERE user_id=? AND plan='founder_mt5'", (u,)).fetchone()
        self.assertEqual(s["ends_at"] - s["starts_at"], 30 * 86400)

    def test_under_one_hundred_demo_wrong_server_or_wrong_currency_do_not_count(self):
        us = self.synced(4)
        report(us[0], "2001", 99.99)
        report(us[1], "2002", 500, demo=True)
        report(us[2], "2003", 500, server="ICMarkets-Live")
        report(us[3], "2004", 500, currency="EUR")
        for u in us:
            self.assertEqual(perk(u)["founder_status"], "pending")
            self.assertEqual(plans(u), [])
        self.assertEqual(perk(us[0])["last_balance"], 99.99)

    def test_only_the_first_three_to_fund_get_it_not_the_first_three_to_sign_up(self):
        us = self.synced(5)
        for u, login in ((us[4], "3001"), (us[3], "3002"), (us[1], "3003")):    # funding order, not sign-up order
            report(u, login, 250)
        self.assertEqual([perk(u)["founder_rank"] for u in (us[4], us[3], us[1])], [1, 2, 3])
        for u in (us[0], us[2]):
            self.assertEqual(plans(u), [])
        report(us[0], "3004", 5000)                                  # the fourth to fund: too late
        self.assertEqual((perk(us[0])["founder_status"], perk(us[0])["founder_rank"]), ("closed", None))
        self.assertEqual(plans(us[0]), [])
        self.assertFalse(credits.entitled({"id": us[0], "tier": "free"}, "mt5"))

    def test_once_full_nobody_else_may_connect_to_be_read(self):
        us = self.synced(4)
        for i in range(3):
            report(us[i], f"400{i}", 300)
        self.assertFalse(perks.founder_pending(us[3]))
        self.assertFalse(credits.mt5_can_report({"id": us[3], "tier": "free"}))
        late = partner()
        perks.sync(late)                                             # a partner confirmed after the places are gone
        self.assertEqual(perk(late)["founder_status"], "closed")
        self.assertFalse(credits.mt5_can_report({"id": late, "tier": "free"}))

    def test_late_partners_still_get_their_five_signals_and_the_pro_price(self):
        us = self.synced(3)
        for i, u in enumerate(us):
            report(u, f"500{i}", 300)
        late = partner()
        perks.sync(late)
        self.assertEqual(credits.balance(late), 5)
        self.assertEqual(perks.price_for({"id": late, "tier": "free"}, "pro", 30.0), 15.0)

    def test_one_funded_mt5_login_cannot_verify_two_members(self):
        a, b = self.synced(2)
        report(a, "6001", 300)
        report(b, "6001", 300)                                       # the same funded account on a second TradeLens member
        self.assertEqual(perk(a)["founder_status"], "verified")
        self.assertEqual(perk(b)["founder_status"], "pending")
        self.assertEqual(plans(b), [])

    def test_verifying_twice_does_not_extend_or_duplicate(self):
        (u,) = self.synced()
        report(u, "7001", 300)
        report(u, "7001", 900)
        with closing(credits.db()) as con:
            n = con.execute("SELECT COUNT(*) AS c FROM billing_subs WHERE user_id=? AND plan='founder_mt5'", (u,)).fetchone()["c"]
        self.assertEqual((n, perk(u)["founder_balance"]), (1, 300.0))

    def test_ranks_are_unique_and_within_the_three_places(self):
        us = self.synced(3)
        for i, u in enumerate(us):
            report(u, f"800{i}", 150)
        self.assertEqual(sorted(perk(u)["founder_rank"] for u in us), [1, 2, 3])

    def test_the_member_view_shows_the_places_left(self):
        a, b = self.synced(2)
        v = perks.member_view(a)
        self.assertEqual((v["partner"], v["signals"], v["founder"]["slots"], v["founder"]["slots_left"]), (True, 5, 3, 3))
        report(b, "9001", 200)
        self.assertEqual(perks.member_view(a)["founder"]["slots_left"], 2)
        self.assertEqual(perks.member_view(b)["founder"]["status"], "verified")

    def test_the_old_pending_ranks_are_cleared_by_the_migration_and_verified_ones_kept(self):
        a, b = partner(), partner()
        with closing(perks.db()) as con:
            con.execute("INSERT INTO partner_perks(user_id,granted_at,founder_rank,founder_status) VALUES(?,?,?,?)", (a, 1, 1, "pending"))
            con.execute("INSERT INTO partner_perks(user_id,granted_at,founder_rank,founder_status) VALUES(?,?,?,?)", (b, 1, 2, "verified"))
            con.commit()
            perks._migrate(con)
            con.commit()
        self.assertEqual((perk(a)["founder_rank"], perk(b)["founder_rank"]), (None, 2))
        report(a, "1111", 150)                                       # a waiting member can still claim a place
        self.assertEqual(perk(a)["founder_status"], "verified")
        self.assertEqual(perk(a)["founder_rank"], 1)

    def test_a_legacy_partner_notify_plan_still_works_until_it_ends(self):
        u = mk_user()
        with closing(credits.db()) as con:
            con.execute("INSERT INTO billing_subs(user_id,plan,starts_at,ends_at) VALUES(?,?,?,?)", (u, "partner_notify", int(time.time()) - 60, int(time.time()) + 86400))
            con.commit()
        self.assertTrue(credits.entitled({"id": u, "tier": "free"}, "notify"))
        self.assertFalse(credits.entitled({"id": u, "tier": "free"}, "mt5"))


class ClosedByDefault(unittest.TestCase):
    """Nov 2026: every benefit tied to Deriv tagging is closed unless PARTNER_PERKS_ON=1 (the rest of this file turns it on to test the mechanism)."""
    def setUp(self):
        self._old = os.environ.pop("PARTNER_PERKS_ON", None)

    def tearDown(self):
        if self._old is not None:
            os.environ["PARTNER_PERKS_ON"] = self._old

    def test_the_flag_is_off_when_unset_or_not_exactly_one(self):
        self.assertFalse(perks.grants_on())
        os.environ["PARTNER_PERKS_ON"] = "true"
        self.assertFalse(perks.grants_on())
        os.environ["PARTNER_PERKS_ON"] = "1"
        self.assertTrue(perks.grants_on())

    def test_a_confirmed_member_pays_the_normal_pro_price(self):
        u = partner()
        self.assertEqual(perks.price_for({"id": u, "tier": "free"}, "pro", 30.0), 30.0)

    def test_a_confirmed_member_gets_no_bonus_signals_and_no_founding_place(self):
        u = partner()
        before = ledger(u)
        self.assertFalse(perks.sync(u))
        self.assertEqual(ledger(u), before)
        self.assertEqual(perk(u), None)
        self.assertFalse(perks.founder_pending(u))

    def test_copy_trading_mode_of_signals_plus_needs_the_flag(self):
        from app import copy_plan
        u = partner()
        self.assertFalse(copy_plan.ib_linked(u))
        os.environ["PARTNER_PERKS_ON"] = "1"
        self.assertTrue(copy_plan.ib_linked(u))

    def test_the_billing_catalog_no_longer_advertises_the_deriv_linked_copy_mode(self):
        from starlette.testclient import TestClient
        from app import main
        u = mk_user()
        c = TestClient(main.app)
        c.cookies.set(auth.COOKIE, auth.make_token(u, "h"))
        cat = c.get("/api/billing").json()["catalog"]
        plus = [p for p in cat if p["key"] == "plus"]
        self.assertEqual(len(plus), 1)
        self.assertFalse(plus[0]["copy_ib"])
        os.environ["PARTNER_PERKS_ON"] = "1"
        self.assertTrue([p for p in c.get("/api/billing").json()["catalog"] if p["key"] == "plus"][0]["copy_ib"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
