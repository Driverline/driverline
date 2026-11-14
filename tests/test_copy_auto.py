"""Tests for automatic copying of every master trade (copy_trading.auto_*). Run: python3 tests/test_copy_auto.py (temp SQLite, no network)."""
import os
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_copy_trading as T                                          # noqa: E402  (its helpers; its own tests are not re-run here)
from test_copy_trading import CT, cl, eareport, pos, q, ea              # noqa: E402


def auto(w, on=True):
    return cl(w.admin).post("/api/copy/admin/auto", json={"enabled": on})


def late(w, mins=5):
    """Pretend the master's last report was `mins` minutes ago (an offline gap)."""
    q("UPDATE ea_accounts SET last_seen=? WHERE id=?", int(time.time()) - mins * 60, w.m)


class AutoCopy(T.World, unittest.TestCase):
    def setUp(self):
        q("DELETE FROM copy_auto_seen")
        q("DELETE FROM ea_meta WHERE k IN ('copy_auto','copy_auto_since')")
        q("DELETE FROM copy_flags WHERE source='auto'")
        super().setUp()

    def flags(self):
        return q("SELECT * FROM copy_flags WHERE source='auto' ORDER BY id")

    def test_off_by_default_nothing_is_copied(self):
        eareport(self.km, "9000", [pos(900), pos(901)])
        self.assertEqual(self.flags(), [])
        self.assertFalse(cl(self.admin).get("/api/copy/admin").json()["auto"]["enabled"])

    def test_switching_on_leaves_trades_already_open_alone_and_copies_new_ones_at_once(self):
        r = auto(self)
        self.assertEqual(r.status_code, 200, r.text)
        eareport(self.km, "9000", [pos(900)])                              # open before: never copied
        self.assertEqual(self.flags(), [])
        eareport(self.km, "9000", [pos(900), pos(901, typ="SELL", sl=1.1010, tp=1.0970)])
        f = self.flags()
        self.assertEqual([(x["master_ticket"], x["dir"]) for x in f], [(901, "SELL")])
        jobs = self.jobs(f[0]["id"])
        self.assertEqual({j["account_id"] for j in jobs}, {self.f1["a"], self.f2["a"]})       # every subscribed follower with copying on
        eareport(self.km, "9000", [pos(900), pos(901, typ="SELL", sl=1.1010, tp=1.0970)])
        self.assertEqual(len(self.flags()), 1)                                                 # reports repeat: still one flag

    def test_followers_who_are_off_or_unsubscribed_get_nothing(self):
        off = self.follower("off", enabled=False)
        ns = self.follower("ns", sub=False, enabled=False)
        auto(self)
        eareport(self.km, "9000", [pos(900), pos(902)])
        got = {j["account_id"] for j in self.jobs(self.flags()[0]["id"])}
        self.assertEqual(got, {self.f1["a"], self.f2["a"]})
        self.assertNotIn(off["a"], got)
        self.assertNotIn(ns["a"], got)

    def test_the_copy_closes_when_the_master_trade_closes(self):
        auto(self)
        eareport(self.km, "9000", [pos(900), pos(903)])
        fid = self.flags()[0]["id"]
        eareport(self.km, "9000", [pos(900)])
        self.assertEqual(q("SELECT status FROM copy_flags WHERE id=?", fid)[0]["status"], "closed")

    def test_a_trade_without_stop_and_take_profit_waits_then_is_skipped_with_an_alert(self):
        auto(self)
        eareport(self.km, "9000", [pos(900), pos(904, sl=0, tp=0)])
        self.assertEqual(self.flags(), [])
        self.assertEqual(q("SELECT action FROM copy_auto_seen WHERE ticket=904")[0]["action"], "waiting")
        eareport(self.km, "9000", [pos(900), pos(904)])                    # SL/TP set within the wait: copied
        self.assertEqual([x["master_ticket"] for x in self.flags()], [904])
        eareport(self.km, "9000", [pos(900), pos(904), pos(905, sl=0)])
        q("UPDATE copy_auto_seen SET first_seen=first_seen-100 WHERE ticket=905")
        eareport(self.km, "9000", [pos(900), pos(904), pos(905, sl=0)])
        self.assertEqual(q("SELECT action FROM copy_auto_seen WHERE ticket=905")[0]["action"], "skipped")
        self.assertEqual(len(self.flags()), 1)
        from app import admin_alerts
        with __import__("contextlib").closing(admin_alerts.db()) as con:
            self.assertTrue(con.execute("SELECT 1 FROM admin_alerts WHERE kind='copy_skip' AND ref=?", (f"{self.m}:905",)).fetchone())

    def test_trades_found_after_an_offline_gap_are_never_copied(self):
        auto(self)
        late(self)
        eareport(self.km, "9000", [pos(900), pos(906)])
        self.assertEqual(self.flags(), [])
        self.assertEqual(q("SELECT action FROM copy_auto_seen WHERE ticket=906")[0]["action"], "baseline")
        eareport(self.km, "9000", [pos(900), pos(906), pos(907)])         # back to normal: the next new one is copied
        self.assertEqual([x["master_ticket"] for x in self.flags()], [907])

    def test_wrong_side_stop_is_skipped_not_copied(self):
        auto(self)
        eareport(self.km, "9000", [pos(900), pos(908, sl=1.2)])
        self.assertEqual(self.flags(), [])
        self.assertEqual(q("SELECT action FROM copy_auto_seen WHERE ticket=908")[0]["action"], "skipped")

    def test_daily_brake(self):
        auto(self)
        real = CT.AUTO_MAX_PER_DAY
        CT.AUTO_MAX_PER_DAY = 2
        try:
            eareport(self.km, "9000", [pos(900), pos(910), pos(911), pos(912)])
        finally:
            CT.AUTO_MAX_PER_DAY = real
        self.assertEqual(len(self.flags()), 2)
        self.assertEqual(q("SELECT action FROM copy_auto_seen WHERE ticket=912")[0]["action"], "skipped")

    def test_a_manual_flag_still_works_and_is_not_doubled(self):
        auto(self)
        eareport(self.km, "9000", [pos(900), pos(913)])
        self.assertEqual(self.flag(913).status_code, 409)                  # already flagged by the system
        self.assertEqual(len(self.flags()), 1)
        auto(self, False)
        eareport(self.km, "9000", [pos(900), pos(913), pos(914)])
        self.assertEqual(len(self.flags()), 1)                             # off again: new trades are not copied
        self.assertEqual(self.flag(914).status_code, 200)                  # but flagging by hand still works

    def test_only_admins_can_switch_it_and_it_needs_a_connected_master(self):
        member = self.follower("m1")["u"]
        self.assertEqual(cl(member).post("/api/copy/admin/auto", json={"enabled": True}).status_code, 403)
        self.assertEqual(cl(self.admin).post("/api/copy/admin/auto", json={"enabled": "yes"}).status_code, 422)
        late(self, 10)
        self.assertEqual(auto(self).status_code, 409)
        self.assertFalse(cl(self.admin).get("/api/copy/admin").json()["auto"]["enabled"])

    def test_a_failure_in_automatic_copying_never_stops_the_report(self):
        auto(self)
        real = CT.create_flag
        CT.create_flag = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
        try:
            r = eareport(self.km, "9000", [pos(900), pos(915)])
        finally:
            CT.create_flag = real
        self.assertEqual(r.status_code, 200, r.text)


if __name__ == "__main__":
    unittest.main(argv=[sys.argv[0], "AutoCopy"])
