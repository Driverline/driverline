"""Tests for the admin's per-trade limit offer (member must accept). Run: python3 tests/test_copy_offer.py (temp SQLite, no network)."""
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_copy_trading as T                                          # noqa: E402
from test_copy_trading import CT, cl, eapoll, eareport, pos, q, mk_user   # noqa: E402


class Offer(T.World, unittest.TestCase):
    def setUp(self):
        super().setUp()
        eareport(self.km, "9000", [pos(901, volume=0.4)], 1000.0)          # 4% trade
        self.f = self.follower("o", balance=10000.0, mode="mirror")

    def offer(self, pct=4.5, **kw):
        return cl(self.admin).post("/api/copy/admin/limit-offer", json={"account_id": self.f["a"], "pct": pct, "message": "Please accept", **kw})

    def limit(self):
        return q("SELECT max_risk_pct FROM copy_members WHERE account_id=?", self.f["a"])[0]["max_risk_pct"]

    def test_an_offer_changes_nothing_until_the_member_accepts(self):
        r = self.offer()
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIsNone(self.limit())
        self.assertEqual(eapoll(self.f["k"]).json()["cmd"], "")
        msg = q("SELECT body FROM inbox_messages WHERE user_id=? ORDER BY id DESC", self.f["u"])[0]["body"]
        self.assertIn("4.5%", msg)
        me = cl(self.f["u"]).get("/api/copy/me").json()["accounts"][0]
        self.assertEqual((me["offer"]["pct"], me["offer"]["from_pct"]), (4.5, 1.0))
        fid = self.flag(901).json()["id"]                                     # still gated at the old 1% limit
        self.assertEqual(eapoll(self.f["k"]).json()["cmd"], "")
        self.assertEqual(self.job(fid, self.f)["status"], "skipped")

    def test_accepting_sets_the_limit_and_the_next_trade_is_copied_in_proportion(self):
        oid = self.offer().json()["id"]
        r = cl(self.f["u"]).post("/api/copy/me/limit-offer", json={"id": oid, "accept": True})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self.limit(), 4.5)
        self.assertEqual(q("SELECT status FROM copy_limit_offers WHERE id=?", oid)[0]["status"], "accepted")
        self.flag(901)
        self.assertEqual(float(eapoll(self.f["k"]).json()["lots"]), 4.0)
        self.assertEqual(cl(self.f["u"]).post("/api/copy/me/limit-offer", json={"id": oid, "accept": True}).status_code, 409)   # only once

    def test_declining_keeps_the_limit(self):
        oid = self.offer().json()["id"]
        r = cl(self.f["u"]).post("/api/copy/me/limit-offer", json={"id": oid, "accept": False})
        self.assertEqual(r.json()["accepted"], False)
        self.assertIsNone(self.limit())

    def test_only_the_owner_can_answer_and_only_admin_can_offer(self):
        oid = self.offer().json()["id"]
        other = mk_user()
        self.assertEqual(cl(other).post("/api/copy/me/limit-offer", json={"id": oid, "accept": True}).status_code, 409)
        self.assertEqual(cl(self.f["u"]).post("/api/copy/admin/limit-offer", json={"account_id": self.f["a"], "pct": 5}).status_code, 403)
        self.assertIsNone(self.limit())

    def test_validation_new_offer_replaces_old_cancel_and_expiry(self):
        self.assertEqual(self.offer(0.5).status_code, 422)                    # not higher than the current 1%
        self.assertEqual(self.offer(11).status_code, 422)
        first = self.offer(3).json()["id"]
        second = self.offer(4.5).json()["id"]
        self.assertEqual(q("SELECT status FROM copy_limit_offers WHERE id=?", first)[0]["status"], "cancelled")
        self.assertEqual(cl(self.f["u"]).post("/api/copy/me/limit-offer", json={"id": first, "accept": True}).status_code, 409)
        board = next(x for x in cl(self.admin).get("/api/copy/admin").json()["setup"] if x["user_id"] == self.f["u"])
        self.assertEqual(board["accounts"][0]["offer"]["pct"], 4.5)
        self.assertEqual(cl(self.admin).post("/api/copy/admin/limit-offer/cancel", json={"id": second}).status_code, 200)
        third = self.offer(4.5).json()["id"]
        q("UPDATE copy_limit_offers SET created_at=? WHERE id=?", int(time.time()) - 8 * 86400, third)
        self.assertEqual(cl(self.f["u"]).post("/api/copy/me/limit-offer", json={"id": third, "accept": True}).status_code, 409)
        self.assertIsNone(self.limit())

    def test_no_offer_for_an_account_without_copy_set_up(self):
        u = mk_user()
        a, _ = T.add(u, "none")
        self.assertEqual(cl(self.admin).post("/api/copy/admin/limit-offer", json={"account_id": a, "pct": 4.5}).status_code, 404)


if __name__ == "__main__":
    unittest.main()
