"""Tests for referrals, points and levels (app/rewards.py).
Run: python3 tests/test_rewards.py   (own process, temp SQLite database, no network, no broker)."""
import json
import os
import sys
import tempfile
import time
import types
import unittest
from contextlib import closing
from datetime import datetime, timezone
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

from app import auth, credits, dbx, deriv_login, deriv_trade, ea, journal, rewards   # noqa: E402

dbx.SQLITE_PATH = Path(tempfile.mkdtemp()) / "t.db"
from starlette.testclient import TestClient   # noqa: E402
from app import main                          # noqa: E402

_seq = [0]


def mk_user(tier="free"):
    _seq[0] += 1
    with closing(auth.db()) as con:
        uid = con.insert("INSERT INTO users(email,pw_hash,tier,created_at,blocked) VALUES(?,?,?,?,0)",
                         (f"member{_seq[0]}@t.example", "h", tier, "2026-01-01T00:00:00"))
        con.commit()
    return uid


def wipe():
    for mod, names in ((rewards, ("ref_codes", "referrals", "points_ledger", "points_balances", "rewards_meta")),
                       (journal, ("trades",)), (ea, ("ea_accounts", "ea_acct_state", "copy_members", "copy_subs")),
                       (credits, ("billing_orders", "billing_subs")), (deriv_login, ("deriv_accounts",)),
                       (deriv_trade, ("deriv_trades",)), (auth, ("usage",))):
        with closing(mod.db()) as con:
            for n in names:
                con.execute(f"DELETE FROM {n}")
            con.commit()
    rewards._last.clear()


def pts(uid):
    return rewards.balances(uid)["lifetime"]


def kinds(uid):
    with closing(rewards.db()) as con:
        return [(r["kind"], r["delta"]) for r in con.execute("SELECT kind, delta FROM points_ledger WHERE user_id=? ORDER BY id", (uid,)).fetchall()]


def begin(ago=3600):
    """Rewards 'started' an hour ago, so activity from now on counts."""
    with closing(rewards.db()) as con:
        con.execute("INSERT INTO rewards_meta(key,value) VALUES('start',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(int(time.time()) - ago),))
        con.commit()


def mk_account(uid, login, demo, label=None):
    with closing(ea.db()) as con:
        aid = con.insert("INSERT INTO ea_accounts(user_id,label,key_hash,created_at,last_seen,login) VALUES(?,?,?,?,?,?)",
                         (uid, label or f"A{login}", f"kh{uid}{login}", 0, 0, login))
        con.execute("INSERT INTO ea_acct_state(account_id,state,specs,updated_at) VALUES(?,?,?,?)",
                    (aid, json.dumps({"account": {"login": login, "demo": demo}}), None, 0))
        con.commit()
    return aid


def mk_trade(uid, login, n, closed=None):
    closed = closed or datetime.now(timezone.utc)
    with closing(journal.db()) as con:
        con.execute("INSERT INTO trades(user_id,instrument,direction,entry,status,opened_at,closed_at,mt5_id) VALUES(?,?,?,?,'closed',?,?,?)",
                    (uid, "v25", "BUY", 1.0, closed.isoformat(timespec="seconds"), closed.isoformat(timespec="seconds"), f"{login}:{n}"))
        con.commit()


def mk_order(uid, usd, status="approved", provider_status=None, product="vault"):  # noqa
    with closing(credits.db()) as con:
        oid = con.insert("INSERT INTO billing_orders(user_id,product,method,amount_usd,currency,ref,status,created_at,provider_status) VALUES(?,?,?,?,?,?,?,?,?)",
                         (uid, product, "manual", usd, "USD", f"ref{time.time_ns()}", status, int(time.time()), provider_status))
        con.commit()
    return oid


def link_deriv(uid, acct):
    with closing(deriv_login.db()) as con:
        con.execute("INSERT INTO deriv_accounts(account_id,user_id,created_at,linked_at) VALUES(?,?,?,?)", (acct, uid, 0, 0))
        con.commit()


def refer(referrer, referred):
    with closing(rewards.db()) as con:
        con.execute("INSERT INTO referrals(referred_id,referrer_id,code,status,created_at) VALUES(?,?,?,'pending',?)",
                    (referred, referrer, "abcdefgh", int(time.time())))
        con.commit()


class FakeReq:
    def __init__(self, cookies=None):
        self.cookies = cookies or {}


class Base(unittest.TestCase):
    def setUp(self):
        os.environ["REWARDS_ON"] = "1"
        wipe()
        begin()


class Levels(unittest.TestCase):
    def test_levels_follow_lifetime_points(self):
        for p, name in ((0, "Bronze"), (999, "Bronze"), (1000, "Silver"), (4999, "Silver"), (5000, "Gold"),
                        (20000, "Diamond"), (59999, "Diamond"), (60000, "Platinum"), (10 ** 6, "Platinum")):
            self.assertEqual(rewards.level_for(p)["name"], name, p)
        self.assertEqual(rewards.level_for(500)["percent"], 50)
        self.assertIsNone(rewards.level_for(70000)["next"])

    def test_names_are_masked(self):
        self.assertEqual(rewards.mask("jonathan@x.com"), "jo***")


class Ledger(Base):
    def test_an_award_is_idempotent_and_moves_both_balances(self):
        u = mk_user()
        with closing(rewards.db()) as con:
            self.assertTrue(rewards.award(con, u, "daily", "2026-11-05", 2, "x"))
            self.assertFalse(rewards.award(con, u, "daily", "2026-11-05", 2, "x"))
            self.assertTrue(rewards.award(con, u, "daily", "2026-11-06", 2, "x"))
        self.assertEqual(rewards.balances(u), {"spendable": 4, "lifetime": 4})

    def test_off_means_nothing_happens(self):
        os.environ["REWARDS_ON"] = "0"
        u = mk_user()
        rewards.touch(u, force=True)
        rewards.on_signup(u, FakeReq({"dl_rc": "abcdefgh"}))
        self.assertEqual(pts(u), 0)
        c = TestClient(main.app)
        c.cookies.set(auth.COOKIE, auth.make_token(u, "h"))
        self.assertEqual(c.get("/api/rewards").json(), {"enabled": False})
        self.assertFalse(c.get("/api/auth/me").json()["rewards"])


class Referral(Base):
    def test_every_member_has_one_stable_unique_code(self):
        a, b = mk_user(), mk_user()
        ca = rewards.code_for(a)
        self.assertEqual(ca, rewards.code_for(a))
        self.assertNotEqual(ca, rewards.code_for(b))
        self.assertRegex(ca, r"^[a-z0-9]{8}$")

    def test_signup_attaches_the_referrer_once_and_never_changes_it(self):
        a, b, new = mk_user(), mk_user(), mk_user()
        ca, cb = rewards.code_for(a), rewards.code_for(b)
        rewards.on_signup(new, FakeReq({"dl_rc": ca}))
        rewards.on_signup(new, FakeReq({"dl_rc": cb}))          # a later link does not take over
        with closing(rewards.db()) as con:
            self.assertEqual(con.execute("SELECT referrer_id FROM referrals WHERE referred_id=?", (new,)).fetchone()["referrer_id"], a)

    def test_self_referral_and_bad_codes_are_ignored(self):
        a, new = mk_user(), mk_user()
        rewards.on_signup(a, FakeReq({"dl_rc": rewards.code_for(a)}))
        rewards.on_signup(new, FakeReq({"dl_rc": "nope"}))
        rewards.on_signup(new, FakeReq({"dl_rc": "zzzzzzzz"}))
        rewards.on_signup(new, FakeReq({"dl_rc": "'; drop table users;--"}))
        with closing(rewards.db()) as con:
            self.assertEqual(con.execute("SELECT COUNT(*) AS n FROM referrals").fetchone()["n"], 0)

    def test_the_referrer_is_paid_only_when_the_friend_links_deriv(self):
        a, f = mk_user(), mk_user()
        refer(a, f)
        rewards.sweep_user(f)
        self.assertEqual(pts(a), 0)                             # joined only: nothing yet
        link_deriv(f, "CR1")
        rewards.sweep_user(f)
        rewards.sweep_user(f)                                   # again: still once
        self.assertEqual(kinds(a), [("ref_qualify", 50)])

    def test_purchases_by_a_referral_pay_the_referrer_and_a_refund_takes_it_back(self):
        a, f = mk_user(), mk_user()
        refer(a, f)
        link_deriv(f, "CR2")
        o1 = mk_order(f, 100)
        rewards.sweep_user(f)
        self.assertEqual(pts(f), 1000)                          # 10 points per USD
        self.assertEqual(sorted(kinds(a)), [("ref_first_buy", 100), ("ref_qualify", 50)])
        o2 = mk_order(f, 50)
        rewards.sweep_user(f)
        self.assertEqual(pts(f), 1500)
        self.assertEqual(pts(a), 50 + 100 + 100)                # later purchase: 2 per USD
        with closing(credits.db()) as con:
            con.execute("UPDATE billing_orders SET provider_status='refunded' WHERE id=?", (o2,))
            con.commit()
        rewards.sweep_user(f)
        rewards.sweep_user(f)
        self.assertEqual(pts(f), 1000)
        self.assertEqual(pts(a), 150)

    def test_a_referral_who_never_linked_deriv_earns_the_referrer_nothing_from_purchases(self):
        a, f = mk_user(), mk_user()
        refer(a, f)
        mk_order(f, 100)
        rewards.sweep_user(f)
        self.assertEqual(pts(a), 0)
        self.assertEqual(pts(f), 1000)


class Earning(Base):
    def test_daily_visit_once_a_day_and_the_streak_bonuses(self):
        u = mk_user()
        rewards.touch(u, force=True)
        rewards.touch(u, force=True)
        self.assertEqual(kinds(u), [("daily", 2)])
        with closing(rewards.db()) as con:                      # six earlier days in a row: today makes seven
            d = rewards._day()
            for _ in range(6):
                d = rewards._prev(d)
                rewards.award(con, u, "daily", d, 2, "x", d)
        rewards.touch(u, force=True)
        self.assertIn(("streak7", 10), kinds(u))
        rewards.touch(u, force=True)
        self.assertEqual([k for k in kinds(u) if k[0] == "streak7"], [("streak7", 10)])
        self.assertNotIn(("streak30", 50), kinds(u))

    def test_analyses_one_point_each_up_to_five_a_day_and_not_before_the_start(self):
        u = mk_user()
        with closing(auth.db()) as con:
            con.execute("INSERT INTO usage(user_id,day,n) VALUES(?,?,?)", (u, rewards._day(), 8))
            con.execute("INSERT INTO usage(user_id,day,n) VALUES(?,?,?)", (u, "2020-01-01", 3))
            con.commit()
        rewards.sweep_user(u)
        rewards.sweep_user(u)
        self.assertEqual(pts(u), 5)

    def test_live_trades_three_demo_one_and_five_a_day(self):
        u = mk_user()
        mk_account(u, "111", False)
        mk_account(u, "222", True)
        mk_trade(u, "111", 1)
        mk_trade(u, "222", 2)
        rewards.sweep_user(u)
        self.assertEqual(pts(u), 100 + 3 + 1)                   # two MT5 connections (50 each), one live and one demo trade
        for n in range(3, 12):
            mk_trade(u, "111", n)
        rewards.sweep_user(u)
        rewards.sweep_user(u)
        with closing(rewards.db()) as con:
            self.assertEqual(con.execute("SELECT COUNT(*) AS n FROM points_ledger WHERE user_id=? AND kind='trade'", (u,)).fetchone()["n"], 5)

    def test_an_account_of_unknown_type_earns_the_demo_rate_and_old_trades_earn_nothing(self):
        u = mk_user()
        mk_trade(u, "999", 1)                                   # no such account: unknown
        mk_trade(u, "999", 2, closed=datetime(2020, 1, 1, tzinfo=timezone.utc))
        rewards.sweep_user(u)
        self.assertEqual(pts(u), 1)

    def test_deriv_app_trades_count_by_account_type(self):
        u = mk_user()
        with closing(deriv_trade.db()) as con:
            for i, t in enumerate(("real", "demo")):
                con.execute("INSERT INTO deriv_trades(user_id,signal_id,account_id,account_type,symbol,contract_type,status,created_at) "
                            "VALUES(?,?,?,?,?,?,'placed',?)", (u, i, "A", t, "R_25", "MULTUP", int(time.time())))
            con.commit()
        rewards.sweep_user(u)
        self.assertEqual(pts(u), 4)

    def test_purchase_points_and_a_refund_reverses_them(self):
        u = mk_user()
        oid = mk_order(u, 15)
        mk_order(u, 40, status="pending")                       # unpaid: nothing
        rewards.sweep_user(u)
        self.assertEqual(pts(u), 150)
        with closing(credits.db()) as con:
            con.execute("UPDATE billing_orders SET provider_status='refunded' WHERE id=?", (oid,))
            con.commit()
        rewards.sweep_user(u)
        rewards.sweep_user(u)
        self.assertEqual(pts(u), 0)

    def test_mt5_connection_fifty_points_for_two_accounts_and_never_twice_for_one_login(self):
        u, other = mk_user(), mk_user()
        for lg in ("1", "2", "3"):
            mk_account(u, lg, False)
        mk_account(other, "1", False, label="dup")
        rewards.sweep_user(u)
        rewards.sweep_user(other)
        self.assertEqual(pts(u), 100)
        self.assertEqual(pts(other), 0)

    def test_copy_trading_first_activation_and_the_monthly_points(self):
        u = mk_user()
        mk_account(u, "5", False)
        with closing(ea.db()) as con:
            con.execute("INSERT INTO copy_members(account_id,user_id,enabled,updated_at) VALUES(1,?,1,0)", (u,))
            con.commit()
        rewards.sweep_user(u)
        self.assertEqual(sorted(k for k, _ in kinds(u)), ["copy_first", "mt5_conn"])      # no active plan: no monthly points
        with closing(credits.db()) as con:
            con.execute("INSERT INTO billing_subs(user_id,plan,starts_at,ends_at) VALUES(?,?,?,?)", (u, "copy", 0, int(time.time()) + 86400))
            con.commit()
        rewards.sweep_user(u)
        rewards.sweep_user(u)
        self.assertEqual(pts(u), 50 + 100 + 20)


class Api(Base):
    @classmethod
    def setUpClass(cls):
        os.environ["REWARDS_ON"] = "1"
        wipe()
        begin()
        cls.admin, cls.u, cls.v = mk_user("admin"), mk_user(), mk_user()
        cls.ca, cls.cu = TestClient(main.app), TestClient(main.app)
        cls.ca.cookies.set(auth.COOKIE, auth.make_token(cls.admin, "h"))
        cls.cu.cookies.set(auth.COOKIE, auth.make_token(cls.u, "h"))

    def test_member_view_has_a_link_level_board_and_masks_other_members(self):
        mk_order(self.v, 20)
        rewards.sweep_user(self.v)
        d = self.cu.get("/api/rewards").json()
        self.assertTrue(d["enabled"])
        self.assertTrue(d["link"].endswith("/login.html?rc=" + d["code"]))
        self.assertEqual(d["level"]["name"], "Bronze")
        names = [x["name"] for x in d["all"]["top"]]
        self.assertIn("me***", names)                           # member emails start with "me"
        self.assertNotIn("@", json.dumps(d["all"]))
        self.assertEqual(d["month"]["my_rank"], 2)              # v earned more than u (200 vs a daily 2)

    def test_me_reports_that_rewards_are_on(self):
        self.assertTrue(self.cu.get("/api/auth/me").json()["rewards"])

    def test_admin_only_and_adjust_needs_a_reason_and_cannot_go_below_zero(self):
        self.assertEqual(self.cu.get("/api/admin/rewards").status_code, 403)
        self.assertEqual(self.cu.post("/api/admin/rewards/adjust", json={}).status_code, 403)
        email = f"member{self.u}@t.example"
        r = self.ca.post("/api/admin/rewards/adjust", json={"email": email, "points": 500, "reason": "Contest prize"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.ca.post("/api/admin/rewards/adjust", json={"email": email, "points": 5, "reason": ""}).status_code, 422)
        self.assertEqual(self.ca.post("/api/admin/rewards/adjust", json={"email": email, "points": -999999, "reason": "too big"}).status_code, 422)
        self.assertEqual(self.ca.post("/api/admin/rewards/adjust", json={"email": email, "points": -100000, "reason": "too much"}).status_code, 409)
        self.assertEqual(self.ca.get("/api/admin/rewards").status_code, 200)


class Bonus(Base):
    """Phase 2: free signals and free MT5 + Copy trading for the referrer. Needs REWARDS_BONUS_ON=1 as well."""
    def setUp(self):
        super().setUp()
        os.environ["REWARDS_BONUS_ON"] = "1"
        self.a = mk_user()

    def tearDown(self):
        os.environ.pop("REWARDS_BONUS_ON", None)

    def friend(self, qualified=True):
        f = mk_user()
        refer(self.a, f)
        if qualified:
            link_deriv(f, f"CR{f}")
            rewards.sweep_user(f)
        return f

    def signals(self, uid=None):
        with closing(credits.db()) as con:
            r = con.execute("SELECT COALESCE(SUM(delta),0) AS s FROM credit_ledger WHERE user_id=? AND kind='ref_signals'", (uid or self.a,)).fetchone()
        return r["s"]

    def buy(self, f, product):
        mk_order(f, credits.CATALOG[product]["usd"], product=product)
        rewards.sweep_user(f)

    def free_rows(self):
        with closing(credits.db()) as con:
            return [dict(r) for r in con.execute("SELECT * FROM billing_subs WHERE user_id=? AND plan='ref_mt5' ORDER BY id", (self.a,)).fetchall()]

    def copy_end(self):
        with closing(ea.db()) as con:
            r = con.execute("SELECT ends_at FROM copy_subs WHERE user_id=?", (self.a,)).fetchone()
        return r["ends_at"] if r else 0

    def test_off_by_default_even_with_rewards_on(self):
        os.environ.pop("REWARDS_BONUS_ON")
        f = self.friend()
        self.buy(f, "vault")
        self.assertEqual((self.signals(), self.free_rows()), (0, []))

    def test_five_percent_of_the_signals_of_a_vault_or_vault_lite(self):
        f = self.friend()
        self.buy(f, "vault")
        self.buy(f, "vaultlite")
        rewards.sweep_referrer(self.a)
        rewards.sweep_referrer(self.a)
        self.assertEqual(self.signals(), 275 + 125)
        with closing(credits.db()) as con:
            self.assertEqual(con.execute("SELECT COUNT(*) AS n FROM credit_ledger WHERE user_id=? AND kind='ref_signals'", (self.a,)).fetchone()["n"], 2)

    def test_other_plans_give_no_signals(self):
        f = self.friend()
        for p in ("pro", "copy", "plus", "topup", "boost"):
            self.buy(f, p)
        self.assertEqual(self.signals(), 0)

    def test_a_friend_who_has_not_linked_deriv_earns_nothing(self):
        f = self.friend(qualified=False)
        self.buy(f, "vault")
        self.assertEqual((self.signals(), self.free_rows()), (0, []))
        link_deriv(f, "LATE1")                              # linking later counts what they already bought
        rewards.sweep_user(f)
        self.assertEqual(self.signals(), 275)

    def test_a_refunded_or_unpaid_order_does_not_count(self):
        f = self.friend()
        mk_order(f, 100, product="vault", provider_status="refunded")
        mk_order(f, 100, product="vault", status="pending")
        rewards.sweep_user(f)
        self.assertEqual(self.signals(), 0)

    def test_every_third_buying_friend_earns_a_free_30_day_connection_with_copy_trading(self):
        f1, f2, f3 = self.friend(), self.friend(), self.friend()
        self.buy(f1, "copy")
        self.buy(f2, "pro")
        self.assertEqual(self.free_rows(), [])
        self.buy(f3, "vaultlite")
        rows = self.free_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["ends_at"] - rows[0]["starts_at"], 30 * 86400)
        self.assertEqual(self.copy_end(), rows[0]["ends_at"])
        rewards.sweep_referrer(self.a)
        rewards.sweep_referrer(self.a)
        self.assertEqual(len(self.free_rows()), 1)           # repeating never gives another

    def test_the_same_friend_buying_twice_counts_once_and_signal_plans_alone_do_not_count(self):
        f1, f2, f3 = self.friend(), self.friend(), self.friend()
        self.buy(f1, "copy")
        self.buy(f1, "copy")
        self.buy(f2, "copy")
        self.buy(f3, "boost")
        self.buy(f3, "plus")
        self.assertEqual(self.free_rows(), [])

    def test_six_friends_stack_two_rewards_back_to_back(self):
        for _ in range(6):
            self.buy(self.friend(), "copy")
        rows = self.free_rows()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[1]["starts_at"], rows[0]["ends_at"])
        self.assertEqual(self.copy_end(), rows[1]["ends_at"])

    def test_the_reward_gives_the_mt5_connection_and_never_shortens_a_longer_copy_grant(self):
        long_end = int(time.time()) + 200 * 86400
        with closing(ea.db()) as con:
            con.execute("INSERT INTO copy_subs(user_id,ends_at,note,admin_id,updated_at) VALUES(?,?,?,?,?)", (self.a, long_end, "gift", None, 0))
            con.commit()
        for _ in range(3):
            self.buy(self.friend(), "pro")
        self.assertEqual(self.copy_end(), long_end)
        os.environ["BILLING_ON"] = "1"
        old = credits.billing_on
        credits.billing_on = lambda: True
        try:
            u = {"id": self.a, "tier": "free"}
            self.assertTrue(credits.entitled(u, "mt5"))
            self.assertTrue(credits.entitled(u, "notify"))
        finally:
            credits.billing_on = old

    def test_the_billing_page_still_loads_with_the_reward_plan_active(self):
        for _ in range(3):
            self.buy(self.friend(), "copy")
        c = TestClient(main.app)
        c.cookies.set(auth.COOKIE, auth.make_token(self.a, "h"))
        r = c.get("/api/billing")
        self.assertEqual(r.status_code, 200)
        self.assertIn("ref_mt5", json.dumps(r.json()))

    def test_the_rewards_page_shows_progress(self):
        f1, f2 = self.friend(), self.friend()
        self.buy(f1, "copy")
        self.buy(f2, "vault")
        c = TestClient(main.app)
        c.cookies.set(auth.COOKIE, auth.make_token(self.a, "h"))
        b = c.get("/api/rewards").json()["bonus"]
        self.assertEqual((b["on"], b["buyers"], b["free_earned"], b["next_in"], b["signals_earned"]), (True, 2, 0, 1, 275))


if __name__ == "__main__":
    unittest.main(verbosity=2)
