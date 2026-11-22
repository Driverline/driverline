"""Tests for connect.get_entitlements: each plan, free grants, expiry, members/admins, billing on/off. Run: python3 tests/test_connect_entitlements.py"""
import os
import sys
import tempfile
import time
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.update(DRIVERLINE_SECRET="s" * 40)
os.environ.pop("DATABASE_URL", None)

from app import auth, credits, dbx                                 # noqa: E402

dbx.SQLITE_PATH = Path(tempfile.mkdtemp()) / "t.db"
from app import connect                                            # noqa: E402

_n = [0]


def mk(tier="free"):
    _n[0] += 1
    with closing(auth.db()) as con:
        uid = con.insert("INSERT INTO users(email,pw_hash,tier,created_at) VALUES(?,?,?,?)", (f"e{_n[0]}@t.example", "h", tier, "2026-01-01T00:00:00"))
        con.commit()
    with closing(auth.db()) as con:
        return dict(con.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone())


def give(uid, plan, days=10):
    now = int(time.time())
    with closing(credits.db()) as con:
        con.execute("INSERT INTO billing_subs(user_id,plan,starts_at,ends_at) VALUES(?,?,?,?)", (uid, plan, now - 60, now + days * 86400))
        con.commit()
    return now + days * 86400


def ents(u, billing=True):
    with patch.object(credits, "billing_on", return_value=billing):
        return connect.get_entitlements(u)


class EntitlementTests(unittest.TestCase):
    def test_free_account_has_nothing_paid(self):
        e = ents(mk())
        self.assertEqual(e["flags"], dict(notifications=False, mt5=False, copytrading=False, autotrading=False))
        self.assertFalse(e["grandfathered"])
        self.assertEqual(e["signals"]["kind"], "credits")
        self.assertEqual(e["plans"], [])
        self.assertTrue(all(v is None for v in e["until"].values()))

    def test_every_catalog_plan_matches_its_flags(self):
        for key, spec in credits.CATALOG.items():
            if not spec.get("days"):
                continue
            u = mk()
            end = give(u["id"], key)
            e = ents(u)
            self.assertEqual(e["flags"]["notifications"], bool(spec.get("notify")), key)
            self.assertEqual(e["flags"]["mt5"], bool(spec.get("mt5")), key)
            self.assertEqual(e["flags"]["autotrading"], bool(spec.get("mt5")), key)
            if spec.get("copy"):
                self.assertTrue(e["flags"]["copytrading"], key)
                self.assertEqual(e["until"]["copytrading"], end, key)
            if spec.get("mt5"):
                self.assertEqual(e["until"]["mt5"], end, key)
            self.assertEqual(e["plans"][0]["plan"], key)

    def test_expired_plan_gives_nothing(self):
        u = mk()
        give(u["id"], "pro", days=-2)
        e = ents(u)
        self.assertFalse(any(e["flags"].values()))
        self.assertEqual(e["plans"], [])

    def test_free_grants_count(self):
        for g in ("founder_mt5", "ref_mt5"):
            u = mk()
            end = give(u["id"], g)
            e = ents(u)
            self.assertTrue(e["flags"]["mt5"] and e["flags"]["notifications"], g)
            self.assertFalse(e["flags"]["copytrading"], g)
            self.assertEqual(e["until"]["mt5"], end)

    def test_members_and_admins_keep_old_access_but_copytrading_is_never_free(self):
        for tier in ("member", "admin"):
            e = ents(mk(tier))
            self.assertTrue(e["grandfathered"])
            self.assertTrue(e["flags"]["mt5"] and e["flags"]["notifications"] and e["flags"]["autotrading"])
            self.assertFalse(e["flags"]["copytrading"])
            self.assertIsNone(e["until"]["mt5"])
            self.assertEqual(e["signals"]["kind"], "limit")

    def test_billing_off_plans_do_not_count_for_free_accounts(self):
        u = mk()
        give(u["id"], "pro")
        e = ents(u, billing=False)
        self.assertFalse(any(e["flags"].values()))
        self.assertFalse(e["billing_on"])
        self.assertEqual(e["signals"]["kind"], "analyses")

    def test_welcome_signals_for_new_free_account(self):
        self.assertEqual(ents(mk())["signals"]["balance"], 3)

    def test_analysis_quota_shape(self):
        a = ents(mk())["analysis"]
        self.assertEqual(set(a), {"used", "limit"})
        self.assertEqual(ents(mk("member"))["analysis"]["limit"], auth.LIMITS["member"])

    def test_nothing_secret_in_the_answer(self):
        text = str(ents(mk())).lower()
        for bad in ("pw_hash", "password", "email"):
            self.assertNotIn(bad, text)


if __name__ == "__main__":
    unittest.main()
