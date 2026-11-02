"""Tests for the daily Telegram recap (app/recap.py) and the close reason kept by the journal.
Run: python3 tests/test_recap.py   (own process, temp SQLite database, a fake Telegram, no network)."""
import asyncio
import contextlib
import io
import json
import os
import sys
import tempfile
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

from app import auth, dbx, journal, recap                       # noqa: E402

dbx.SQLITE_PATH = Path(tempfile.mkdtemp()) / "t.db"
from starlette.testclient import TestClient                      # noqa: E402
from app import main                                              # noqa: E402

TOKEN = "123456:SECRET-TOKEN-VALUE"
os.environ.update(TELEGRAM_BOT_TOKEN=TOKEN, TELEGRAM_RECAP_CHAT="@tl_channel", DAILY_RECAP="1", RECAP_HOUR="21", RECAP_TZ_OFFSET="1")
os.environ.pop("RECAP_USER_IDS", None)

_seq = [0]


def utc(y, mo, d, h=0, mi=0):
    return datetime(y, mo, d, h, mi, tzinfo=timezone.utc)


def epoch(*a):
    return int(utc(*a).timestamp())


def mk_user(tier="admin", blocked=0):
    _seq[0] += 1
    with closing(auth.db()) as con:
        uid = con.insert("INSERT INTO users(email,pw_hash,tier,created_at,blocked) VALUES(?,?,?,?,?)",
                         (f"r{_seq[0]}@t.example", "h", tier, "2026-01-01T00:00:00", blocked))
        con.commit()
    return uid


def mk_trade(uid, instrument="v25", direction="BUY", entry=100.0, exit_price=101.0, stop=None, target=None, pnl=1.0, r=None,
             reason=None, closed=utc(2026, 11, 2, 10, 0)):
    with closing(journal.db()) as con:
        con.execute("INSERT INTO trades(user_id,instrument,direction,entry,stop,target,lots,exit_price,pnl,r_multiple,status,opened_at,closed_at,reason) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,'closed',?,?,?)",
                    (uid, instrument, direction, entry, stop, target, 1.0, exit_price, pnl, r,
                     closed.isoformat(timespec="seconds"), closed.isoformat(timespec="seconds"), reason))
        con.commit()


def wipe():
    for name, mod in (("trades", journal), ("recap_log", recap)):
        with closing(mod.db()) as con:
            con.execute(f"DELETE FROM {name}")
            con.commit()


class FakeTelegram:
    def __init__(self, fail=None):
        self.requests, self.fail = [], fail

    def __call__(self, request):
        self.requests.append(request)
        if self.fail == "network":
            raise httpx.ConnectError("boom " + str(request.url))
        if self.fail:
            return httpx.Response(self.fail, json={"ok": False, "description": "Bad Request: chat not found"})
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 700 + len(self.requests)}})

    def bodies(self):
        return [json.loads(r.content) for r in self.requests]


def plug(fake):
    recap._TRANSPORT = httpx.MockTransport(fake)


class Outcomes(unittest.TestCase):
    def t(self, **k):
        base = dict(direction="BUY", entry=100.0, exit_price=101.0, stop=None, target=None, pnl=1.0, r_multiple=None, reason=None)
        base.update(k)
        return base

    def test_the_ea_reason_decides(self):
        self.assertEqual(recap.outcome(self.t(reason="tp")), "tp")
        self.assertEqual(recap.outcome(self.t(reason="sl", exit_price=99.0, pnl=-1.0)), "sl")
        self.assertEqual(recap.outcome(self.t(reason="so", exit_price=90.0, pnl=-9.0)), "so")
        self.assertEqual(recap.outcome(self.t(reason="manual")), "manual")
        self.assertEqual(recap.outcome(self.t(reason="expert")), "manual")

    def test_a_stop_hit_in_profit_is_a_trailing_stop_not_a_stop_loss(self):
        self.assertEqual(recap.outcome(self.t(reason="sl", exit_price=103.0, pnl=3.0)), "trail")

    def test_breakeven_means_closed_at_the_entry_price(self):
        self.assertEqual(recap.outcome(self.t(reason="sl", exit_price=100.0, pnl=0.0)), "be")
        self.assertEqual(recap.outcome(self.t(reason="sl", entry=2889.194, exit_price=2889.2, stop=2889.194, pnl=0.0)), "be")
        self.assertEqual(recap.outcome(self.t(reason=None, exit_price=100.005, pnl=0.01)), "be")           # within 0.01%
        self.assertNotEqual(recap.outcome(self.t(reason="sl", exit_price=99.5, pnl=-0.5)), "be")

    def test_old_trades_without_a_reason_are_judged_only_by_saved_levels(self):
        buy = self.t(reason=None, exit_price=110.0, stop=95.0, target=110.0, pnl=10.0)
        self.assertEqual(recap.outcome(buy), "tp")
        self.assertEqual(recap.outcome(self.t(reason=None, exit_price=95.0, stop=95.0, target=110.0, pnl=-5.0)), "sl")
        self.assertEqual(recap.outcome(self.t(direction="SELL", reason=None, entry=100.0, exit_price=90.0, stop=105.0, target=90.0, pnl=10.0)), "tp")
        # no stop and no target saved: never guessed as TP or SL
        self.assertEqual(recap.outcome(self.t(reason=None, exit_price=105.0, pnl=5.0)), "closed")

    def test_labels_never_show_money_and_show_r_only_when_reliable(self):
        d = recap.describe(self.t(instrument="v25", reason="tp", exit_price=110.0, stop=95.0, target=110.0, pnl=987.65, r_multiple=2.0))
        self.assertEqual((d["name"], d["side"], d["r"]), ("Volatility 25", "Buy", 2.0))
        self.assertIsNone(recap.describe(self.t(reason="sl", exit_price=103.0, pnl=3.0, r_multiple=1.0))["r"])      # trailed stop: R misleading
        self.assertIsNone(recap.describe(self.t(reason="manual", pnl=5.0))["r"])                                   # no stop saved: no R
        self.assertEqual(recap.describe(self.t(instrument="not_a_key"))["name"], "not_a_key")


class Messages(unittest.TestCase):
    def test_tiers(self):
        self.assertEqual(recap.tier(0, 0, 0), "none")
        self.assertEqual(recap.tier(3, 1, 0), "strong")
        self.assertEqual(recap.tier(1, 0, 0), "green")                # one win is a good day, not a "strong" one
        self.assertEqual(recap.tier(2, 2, 0), "even")                 # wins and losses balanced
        self.assertEqual(recap.tier(2, 2, 1), "even")
        self.assertEqual(recap.tier(0, 0, 2), "flat")                 # only breakeven exits: the message may talk about breakeven
        self.assertEqual(recap.tier(1, 2, 0), "red")
        self.assertEqual(recap.tier(0, 3, 0), "heavy")
        self.assertEqual(recap.tier(1, 4, 1), "heavy")

    def test_only_a_flat_day_talks_about_breakeven_exits(self):
        for tier_name, lines in recap.COACH.items():
            if tier_name != "flat":
                self.assertFalse(any("breakeven" in x.lower() for x in lines), tier_name)

    def test_the_line_is_stable_for_a_day_and_rotates_between_days(self):
        self.assertEqual(recap.coach(3, 0, 0, "2026-11-02"), recap.coach(3, 0, 0, "2026-11-02"))
        self.assertGreater(len({recap.coach(3, 0, 0, f"2026-11-{d:02d}") for d in range(1, 10)}), 1)

    def test_no_message_promises_profit_or_tells_anyone_to_trade(self):
        bad = ("guarantee", "will profit", "buy now", "sell now", "double your", "revenge trade now")
        for lines in recap.COACH.values():
            for x in lines:
                self.assertFalse(any(b in x.lower() for b in bad), x)

    def test_the_post_lists_each_trade_and_the_summary(self):
        trades = [dict(instrument="v25", direction="BUY", entry=100.0, exit_price=110.0, stop=95.0, target=110.0, pnl=500.0, r_multiple=2.0, reason="tp"),
                  dict(instrument="v50", direction="SELL", entry=100.0, exit_price=105.0, stop=105.0, target=90.0, pnl=-250.0, r_multiple=-1.0, reason="sl"),
                  dict(instrument="v100", direction="BUY", entry=100.0, exit_price=100.0, stop=None, target=None, pnl=0.0, r_multiple=None, reason="sl")]
        text = recap.build("2026-11-02", trades)
        self.assertIn("Mon 02 Nov 2026", text)
        self.assertIn("1. Volatility 25 · Buy · ✅ Take profit +2.0R", text)
        self.assertIn("2. Volatility 50 · Sell · ❌ Stop loss -1.0R", text)
        self.assertIn("3. Volatility 100 · Buy · ➖ Breakeven", text)
        self.assertIn("Closed today: 3 · 1 in profit · 1 at a loss · 1 breakeven/flat", text)
        self.assertIn("Net result: +1.0R", text)
        self.assertIn(recap.FOOTER, text)
        for money in ("500", "250", "$"):
            self.assertNotIn(money, text)

    def test_net_result_is_left_out_when_any_r_is_unreliable(self):
        trades = [dict(instrument="v25", direction="BUY", entry=100.0, exit_price=110.0, stop=95.0, target=110.0, pnl=5.0, r_multiple=2.0, reason="tp"),
                  dict(instrument="v25", direction="BUY", entry=100.0, exit_price=101.0, stop=None, target=None, pnl=1.0, r_multiple=None, reason=None)]
        self.assertNotIn("Net result", recap.build("2026-11-02", trades))

    def test_quiet_day(self):
        text = recap.build("2026-11-02", [])
        self.assertIn("No trades closed today.", text)
        self.assertIn(recap.COACH["none"][0] if recap.coach(0, 0, 0, "2026-11-02") == recap.COACH["none"][0] else recap.COACH["none"][1], text)

    def test_a_long_day_is_capped_and_fits_in_one_telegram_message(self):
        one = dict(instrument="v100_1s", direction="BUY", entry=100.0, exit_price=110.0, stop=95.0, target=110.0, pnl=5.0, r_multiple=2.0, reason="tp")
        text = recap.build("2026-11-02", [one] * 80)
        self.assertLessEqual(len(text), 4000)
        self.assertIn(f"...and {80 - recap.MAX_ROWS} more", text)
        self.assertIn("Closed today: 80", text)


class Day(unittest.TestCase):
    def setUp(self):
        wipe()
        os.environ.pop("RECAP_USER_IDS", None)

    def test_a_day_runs_midnight_to_midnight_lagos_time(self):
        a = mk_user()
        mk_trade(a, closed=utc(2026, 11, 1, 22, 59))     # 23:59 on 1 Nov in Lagos
        mk_trade(a, closed=utc(2026, 11, 1, 23, 0))      # 00:00 on 2 Nov
        mk_trade(a, closed=utc(2026, 11, 2, 22, 59))     # 23:59 on 2 Nov
        mk_trade(a, closed=utc(2026, 11, 2, 23, 0))      # 00:00 on 3 Nov
        self.assertEqual(len(recap.trades_for_day("2026-11-02")), 2)
        self.assertEqual(len(recap.trades_for_day("2026-11-01")), 1)
        self.assertEqual(recap.local_day(epoch(2026, 11, 2, 22, 30)), "2026-11-02")
        self.assertEqual(recap.local_day(epoch(2026, 11, 2, 23, 30)), "2026-11-03")

    def test_only_admin_trades_blocked_admins_and_members_are_left_out(self):
        a, member, blocked = mk_user(), mk_user("member"), mk_user("admin", blocked=1)
        for u in (a, member, blocked):
            mk_trade(u)
        self.assertEqual([t["user_id"] for t in recap.trades_for_day("2026-11-02")], [a])

    def test_recap_user_ids_narrows_it_to_the_chosen_accounts(self):
        a, b = mk_user(), mk_user()
        mk_trade(a)
        mk_trade(b)
        os.environ["RECAP_USER_IDS"] = str(b)
        self.assertEqual([t["user_id"] for t in recap.trades_for_day("2026-11-02")], [b])

    def test_trades_come_oldest_first(self):
        a = mk_user()
        mk_trade(a, instrument="v50", closed=utc(2026, 11, 2, 15))
        mk_trade(a, instrument="v10", closed=utc(2026, 11, 2, 8))
        self.assertEqual([t["instrument"] for t in recap.trades_for_day("2026-11-02")], ["v10", "v50"])


class JournalKeepsTheReason(unittest.TestCase):
    def test_a_closed_position_is_stored_with_why_it_closed(self):
        u = mk_user()
        row = {"symbol": "Volatility 25", "type": "BUY", "open": 100.0, "close": 110.0, "volume": 1, "profit": 10.0, "sl": 95.0, "tp": 110.0,
               "opened": 1_700_000_000, "closed": 1_700_003_600, "id": 424242, "reason": "tp"}
        self.assertEqual(journal.import_mt5(u, "9001", [row]), 1)
        self.assertEqual(journal.import_mt5(u, "9001", [row]), 0)                       # safe to repeat
        old = dict(row, id=424243, reason="")
        journal.import_mt5(u, "9001", [old])
        with closing(journal.db()) as con:
            got = {r["mt5_id"]: r["reason"] for r in con.execute("SELECT mt5_id, reason FROM trades WHERE user_id=?", (u,)).fetchall()}
        self.assertEqual(got, {"9001:424242": "tp", "9001:424243": None})


class Telegram(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        wipe()

    async def test_the_post_goes_to_the_channel_with_the_token_only_in_the_url(self):
        fake = FakeTelegram()
        plug(fake)
        mid = await recap.send_telegram("hello")
        self.assertEqual(mid, "701")
        req = fake.requests[0]
        self.assertEqual(str(req.url), f"https://api.telegram.org/bot{TOKEN}/sendMessage")
        body = fake.bodies()[0]
        self.assertEqual((body["chat_id"], body["text"]), ("@tl_channel", "hello"))
        self.assertNotIn(TOKEN, json.dumps(body))

    async def test_a_refusal_and_a_network_error_never_leak_the_token(self):
        for fail in (400, "network"):
            plug(FakeTelegram(fail))
            with self.assertRaises(recap.RecapError) as cm:
                await recap.send_telegram("x")
            self.assertNotIn(TOKEN, str(cm.exception))
            self.assertNotIn("123456", str(cm.exception))
        plug(FakeTelegram(400))
        with self.assertRaises(recap.RecapError) as cm:
            await recap.send_telegram("x")
        self.assertIn("chat not found", str(cm.exception))


class Schedule(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        wipe()
        os.environ.update(DAILY_RECAP="1")
        os.environ.pop("RECAP_USER_IDS", None)
        self.admin = mk_user()
        mk_trade(self.admin, instrument="v25", reason="tp", exit_price=110.0, stop=95.0, target=110.0, pnl=10.0, r=2.0, closed=utc(2026, 11, 2, 9))
        self.fake = FakeTelegram()
        plug(self.fake)

    def log(self):
        with closing(recap.db()) as con:
            return {r["day"]: dict(r) for r in con.execute("SELECT * FROM recap_log").fetchall()}

    async def test_nothing_is_posted_unless_switched_on_and_configured(self):
        for var, val in (("DAILY_RECAP", "0"), ("TELEGRAM_BOT_TOKEN", ""), ("TELEGRAM_RECAP_CHAT", "")):
            old = os.environ[var]
            os.environ[var] = val
            try:
                self.assertFalse(recap.enabled(), var)
                self.assertEqual(await recap.tick(epoch(2026, 11, 2, 21, 30)), [])
            finally:
                os.environ[var] = old
        self.assertEqual(self.fake.requests, [])

    async def test_it_waits_for_the_hour_then_posts_once(self):
        self.assertEqual(await recap.tick(epoch(2026, 11, 2, 19, 30)), [])          # 20:30 in Lagos: too early
        self.assertEqual(self.fake.requests, [])
        self.assertEqual(await recap.tick(epoch(2026, 11, 2, 20, 0)), ["2026-11-02"])     # 21:00 in Lagos
        for minute in (1, 2, 30, 400):                                              # the loop looks again every minute: no repeats
            self.assertEqual(await recap.tick(epoch(2026, 11, 2, 20, 0) + minute * 60), [])
        self.assertEqual(len(self.fake.requests), 1)
        self.assertIn("Volatility 25", self.fake.bodies()[0]["text"])
        self.assertEqual(self.log()["2026-11-02"]["status"], "sent")

    async def test_a_restart_or_a_second_copy_of_the_app_cannot_post_twice(self):
        now = epoch(2026, 11, 2, 21, 0)
        results = await asyncio.gather(*(recap.post_day("2026-11-02", now) for _ in range(5)))
        self.assertEqual(sum(1 for r in results if r["sent"]), 1)
        self.assertEqual(len(self.fake.requests), 1)

    async def test_a_failed_post_is_retried_after_ten_minutes_and_then_gives_up(self):
        plug(FakeTelegram(400))
        base = epoch(2026, 11, 2, 21, 0)
        self.assertEqual(await recap.tick(base), [])
        self.assertEqual(self.log()["2026-11-02"]["status"], "failed")
        self.assertEqual(await recap.tick(base + 120), [])                         # too soon to retry
        ok = FakeTelegram()
        plug(ok)
        self.assertEqual(len(ok.requests), 0)
        self.assertEqual(await recap.tick(base + 700), ["2026-11-02"])             # after 10 minutes it works
        self.assertEqual(self.log()["2026-11-02"]["status"], "sent")
        # a day that keeps failing stops after MAX_ATTEMPTS
        plug(FakeTelegram(400))
        t = base + 86400
        mk_trade(self.admin, closed=utc(2026, 11, 3, 9))
        for i in range(recap.MAX_ATTEMPTS + 3):
            await recap.tick(t + i * 700)
        row = self.log()["2026-11-03"]
        self.assertEqual((row["status"], row["attempts"]), ("failed", recap.MAX_ATTEMPTS))

    async def test_a_claim_that_never_finished_is_taken_over_after_fifteen_minutes(self):
        base = epoch(2026, 11, 2, 21, 0)
        with closing(recap.db()) as con:
            con.execute("INSERT INTO recap_log(day,status,claimed_at) VALUES(?,?,?)", ("2026-11-02", "sending", base - 60))
            con.commit()
        self.assertEqual(await recap.tick(base), [])                              # someone is busy with it right now
        self.assertEqual(self.fake.requests, [])
        self.assertEqual(await recap.tick(base + 1000), ["2026-11-02"])           # that one died: this one posts

    async def test_a_missed_day_is_posted_late_but_only_once_the_recap_has_worked_before(self):
        morning = epoch(2026, 11, 3, 6, 0)                                        # 07:00 on 3 Nov, nothing was ever posted
        self.assertEqual(await recap.tick(morning), [])                           # switching it on never posts an old day
        await recap.tick(epoch(2026, 11, 2, 21, 0))                               # 2 Nov posts normally
        self.assertEqual(len(self.fake.requests), 1)
        mk_trade(self.admin, instrument="v50", closed=utc(2026, 11, 3, 12))
        # the service slept through 3 Nov's evening and wakes at 07:00 on 5 Nov: only yesterday (4 Nov) is made up
        self.assertEqual(await recap.tick(epoch(2026, 11, 5, 6, 0)), ["2026-11-04"])
        self.assertNotIn("2026-11-03", self.log())
        # a service that slept through the evening of 3 Nov and wakes at 07:00 on 4 Nov makes up 3 Nov
        self.assertEqual(await recap.tick(epoch(2026, 11, 4, 6, 0)), ["2026-11-03"])
        self.assertIn("Volatility 50", self.fake.bodies()[-1]["text"])

    async def test_the_token_is_never_printed_even_when_posting_fails(self):
        plug(FakeTelegram("network"))
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            await recap.tick(epoch(2026, 11, 2, 21, 0))
        self.assertIn("not posted", buf.getvalue())
        self.assertNotIn("SECRET-TOKEN", buf.getvalue())
        self.assertNotIn("123456", buf.getvalue())
        self.assertNotIn("SECRET-TOKEN", json.dumps(self.log()))


class AdminApi(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        wipe()
        cls.aid = mk_user()
        cls.mid = mk_user("member")
        mk_trade(cls.aid, closed=utc(2026, 11, 2, 9))
        cls.a, cls.m = TestClient(main.app), TestClient(main.app)
        cls.a.cookies.set(auth.COOKIE, auth.make_token(cls.aid, "h"))
        cls.m.cookies.set(auth.COOKIE, auth.make_token(cls.mid, "h"))

    def setUp(self):
        self.fake = FakeTelegram()
        plug(self.fake)
        with closing(recap.db()) as con:
            con.execute("DELETE FROM recap_log")
            con.commit()
        self._day = recap.local_day
        recap.local_day = lambda ts=None: "2026-11-02"       # the trade above closed on 2 Nov

    def tearDown(self):
        recap.local_day = self._day

    def test_only_admins(self):
        self.assertEqual(self.m.get("/api/admin/recap").status_code, 403)
        self.assertEqual(self.m.post("/api/admin/recap/send", json={}).status_code, 403)
        self.assertEqual(TestClient(main.app).get("/api/admin/recap").status_code, 401)

    def test_the_view_shows_a_preview_and_never_the_token(self):
        r = self.a.get("/api/admin/recap")
        self.assertEqual(r.status_code, 200)
        d = r.json()
        self.assertEqual((d["settings"]["token_set"], d["settings"]["chat_set"], d["settings"]["scheduled"]), (True, True, True))
        self.assertIn("TradeLens Daily Recap", d["preview"])
        self.assertNotIn("SECRET-TOKEN", r.text)
        self.assertNotIn("123456", r.text)
        self.assertEqual(self.fake.requests, [])                # looking never posts

    def test_post_now_posts_once_and_a_repeat_needs_confirming(self):
        r = self.a.post("/api/admin/recap/send", json={})
        self.assertEqual((r.status_code, r.json()["sent"]), (200, True))
        self.assertEqual(len(self.fake.requests), 1)
        self.assertEqual(self.a.post("/api/admin/recap/send", json={}).status_code, 409)
        self.assertEqual(len(self.fake.requests), 1)
        self.assertEqual(self.a.post("/api/admin/recap/send", json={"again": True}).status_code, 200)
        self.assertEqual(len(self.fake.requests), 2)

    def test_post_now_explains_a_missing_setup_and_a_telegram_refusal(self):
        old = os.environ["TELEGRAM_RECAP_CHAT"]
        os.environ["TELEGRAM_RECAP_CHAT"] = ""
        try:
            r = self.a.post("/api/admin/recap/send", json={})
            self.assertEqual(r.status_code, 409)
            self.assertIn("TELEGRAM_RECAP_CHAT", r.json()["detail"])
        finally:
            os.environ["TELEGRAM_RECAP_CHAT"] = old
        plug(FakeTelegram(400))
        r = self.a.post("/api/admin/recap/send", json={})
        self.assertEqual(r.status_code, 502)
        self.assertIn("chat not found", r.json()["detail"])
        self.assertNotIn("SECRET-TOKEN", r.text)

    def test_post_now_works_before_the_schedule_is_switched_on(self):
        os.environ["DAILY_RECAP"] = "0"
        try:
            self.assertFalse(recap.enabled())
            self.assertEqual(self.a.post("/api/admin/recap/send", json={}).status_code, 200)
        finally:
            os.environ["DAILY_RECAP"] = "1"


if __name__ == "__main__":
    unittest.main(verbosity=2)
