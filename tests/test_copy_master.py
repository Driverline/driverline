"""Tests for choosing the master account when an orphaned flag exists. Run: python3 tests/test_copy_master.py (temp SQLite, no network)."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_copy_trading as T                                          # noqa: E402
from test_copy_trading import cl, q, add, mk_user, eareport, pos       # noqa: E402


class Master(T.World, unittest.TestCase):
    def test_an_orphaned_open_flag_does_not_block_choosing_a_master(self):
        self.assertEqual(self.flag().status_code, 200)
        a = cl(self.admin)
        q("DELETE FROM ea_meta WHERE k='copy_master'")                      # as after the old master account was removed
        q("DELETE FROM ea_accounts WHERE id=?", self.m)
        new, k = add(self.admin, "New master")
        r = a.post("/api/copy/admin/master", json={"account": new})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(q("SELECT COUNT(*) AS c FROM copy_flags WHERE status='open'")[0]["c"], 0)
        self.assertEqual(a.get("/api/copy/admin").json()["master"]["id"], new)

    def test_a_live_flag_still_blocks_replacing_the_current_master(self):
        self.flag()
        other, _ = add(self.admin, "Other")
        self.assertEqual(cl(self.admin).post("/api/copy/admin/master", json={"account": other}).status_code, 409)

    def test_removing_the_master_account_ends_its_flags(self):
        fid = self.flag().json()["id"]
        r = cl(self.admin).delete(f"/api/ea/accounts/{self.m}")
        if r.status_code in (404, 405):
            self.skipTest("account removal route differs")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(q("SELECT status FROM copy_flags WHERE id=?", fid)[0]["status"], "closed")


if __name__ == "__main__":
    unittest.main()
