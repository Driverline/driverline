"""Tests for automatic copy-trading set-up and proportional ("match the team's risk") sizing. Run: python3 tests/test_copy_mirror.py (temp SQLite, no network)."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_copy_trading as T                                          # noqa: E402
from test_copy_trading import CT, SUBS, add, cl, eapoll, eareport, mk_user, pos, q   # noqa: E402


class Mirror(T.World, unittest.TestCase):
    def setUp(self):
        super().setUp()
        eareport(self.km, "9000", [pos(901, volume=0.4)], 1000.0)        # master $1000, 0.40 lot, 0.0010 stop: $40 = 4% of the account

    def member(self, tag, balance, cap=None, mode="mirror"):
        f = self.follower(tag, enabled=False, balance=balance)
        body = {"enabled": True, "mode": mode, "risk_pct": 0.5}
        if cap:
            body["max_risk_pct"] = cap
        self.assertEqual(cl(f["u"]).post("/api/copy/me/settings", json=body).status_code, 200)
        return f

    def test_size_scales_with_the_account_and_keeps_the_same_percentage(self):
        spec = T.SPEC
        for bal, lots in ((100, 0.04), (1000, 0.4), (10000, 4.0)):
            got, pct, why = CT.size(spec, bal, 0.0010, "mirror", None, None, 5.0, 4.0)
            self.assertAlmostEqual(got, lots, 6, (bal, why))
            self.assertAlmostEqual(pct, 4.0, 3)

    def test_the_followers_cap_gates_a_trade_that_risks_more_than_their_limit(self):
        lots, pct, why = CT.size(T.SPEC, 10000, 0.0010, "mirror", None, None, 1.0, 4.0)
        self.assertIsNone(lots)
        self.assertIn("1%", why)

    def test_a_balance_too_small_to_copy_in_proportion_is_skipped_never_oversized(self):
        lots, _, why = CT.size(T.SPEC, 10, 0.0010, "mirror", None, None, 5.0, 4.0)     # 4% of $10 = $0.40, smallest lot risks $1
        self.assertIsNone(lots)
        self.assertIn("too small", why)
        self.assertIsNone(CT.size(T.SPEC, 1000, 0.0010, "mirror", None, None, 5.0, None)[0])

    def test_the_flag_records_the_masters_risk_and_followers_are_sized_from_it(self):
        big = self.member("big", 10000.0, cap=5)
        mid = self.member("mid", 1000.0, cap=5)
        capped = self.member("capped", 10000.0)                                        # default 1% limit: must not take a 4% trade
        fid = self.flag(901).json()["id"]
        f = q("SELECT master_lots, master_risk_pct FROM copy_flags WHERE id=?", fid)[0]
        self.assertAlmostEqual(f["master_lots"], 0.4, 6)
        self.assertAlmostEqual(f["master_risk_pct"], 4.0, 3)
        self.assertEqual(float(eapoll(big["k"]).json()["lots"]), 4.0)
        self.assertEqual(float(eapoll(mid["k"]).json()["lots"]), 0.4)
        self.assertEqual(eapoll(capped["k"]).json()["cmd"], "")
        j = self.job(fid, capped)
        self.assertEqual(j["status"], "skipped")
        self.assertEqual(q("SELECT COUNT(*) AS c FROM ea_commands WHERE account_id=? AND kind='open'", capped["a"])[0]["c"], 0)


class AutoSetUp(T.World, unittest.TestCase):
    def paid(self, balance=500.0):
        u = mk_user()
        SUBS(u)
        a, k = add(u, "Mine")
        return u, a, k, balance

    def test_a_paid_member_starts_copying_when_their_account_first_reports(self):
        u, a, k, bal = self.paid()
        self.assertEqual(q("SELECT * FROM copy_members WHERE account_id=?", a), [])
        eareport(k, "LP1", [], bal)
        m = q("SELECT * FROM copy_members WHERE account_id=?", a)
        self.assertEqual((m[0]["enabled"], m[0]["mode"], m[0]["how"]), (1, "mirror", "auto"))
        msg = q("SELECT body FROM inbox_messages WHERE user_id=?", u)
        self.assertEqual(len(msg), 1)
        self.assertIn("Copy trading is now on", msg[0]["body"])
        eareport(k, "LP1", [], bal)                                                     # reports again: no second message
        self.assertEqual(len(q("SELECT 1 FROM inbox_messages WHERE user_id=?", u)), 1)

    def test_never_overrides_a_member_who_switched_it_off_and_never_enrols_the_unpaid_or_the_master(self):
        u, a, k, bal = self.paid()
        eareport(k, "LP2", [], bal)
        cl(u).post("/api/copy/me/settings", json={"enabled": False, "mode": "mirror", "account": a})
        eareport(k, "LP2", [], bal)
        self.assertEqual(q("SELECT enabled FROM copy_members WHERE account_id=?", a)[0]["enabled"], 0)
        un = mk_user()
        ua, uk = add(un, "Unpaid")
        eareport(uk, "LP3", [], 500.0)
        self.assertEqual(q("SELECT * FROM copy_members WHERE account_id=?", ua), [])
        eareport(self.km, "9000", [], 1000.0)
        self.assertEqual(q("SELECT * FROM copy_members WHERE account_id=?", self.m), [])

    def test_the_admin_board_and_set_up_button(self):
        u, a, k, bal = self.paid()
        d = cl(self.admin).get("/api/copy/admin").json()
        mine = next(x for x in d["setup"] if x["user_id"] == u)
        self.assertEqual(mine["accounts"][0]["state"], "not_set_up")
        r = cl(self.admin).post("/api/copy/admin/member", json={"user_id": u, "message": "Set up for you."})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["set_up"], 1)
        self.assertEqual(q("SELECT how FROM copy_members WHERE account_id=?", a)[0]["how"], "admin")
        self.assertEqual(len(q("SELECT 1 FROM inbox_messages WHERE user_id=?", u)), 2)
        self.assertEqual(cl(u).post("/api/copy/admin/member", json={"user_id": u}).status_code, 403)
        nomt5 = mk_user()
        SUBS(nomt5)
        self.assertEqual(cl(self.admin).post("/api/copy/admin/member", json={"user_id": nomt5}).status_code, 409)

    def test_granting_days_sets_up_an_already_connected_account(self):
        u = mk_user()
        a, k = add(u, "Early")
        eareport(k, "LP4", [], 500.0)
        self.assertEqual(q("SELECT * FROM copy_members WHERE account_id=?", a), [])
        r = cl(self.admin).post("/api/copy/admin/subscription", json={"user_id": u, "days": 30})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(q("SELECT enabled, how FROM copy_members WHERE account_id=?", a)[0]["enabled"], 1)


if __name__ == "__main__":
    unittest.main()
