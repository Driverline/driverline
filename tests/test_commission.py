"""Tests for referral commission: report reading, eligibility, balances and manual payouts (app/commission.py).
Run: python3 tests/test_commission.py   (own process, temp SQLite database, no network)."""
import json
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

from app import auth, commission, dbx, inbox, partner, prizes, rewards   # noqa: E402

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


def client(uid):
    c = TestClient(main.app)
    c.cookies.set(auth.COOKIE, auth.make_token(uid, "h"))
    return c


def wipe():
    for mod, names in ((commission, ("commission_rows", "commission_imports", "payout_details", "payout_requests")),
                       (rewards, ("referrals", "points_ledger", "points_balances")), (partner, ("partner_tags",)), (inbox, ("inbox_messages",))):
        with closing(mod.db()) as con:
            for n in names:
                con.execute(f"DELETE FROM {n}")
            con.commit()


def tag(uid, account, status="confirmed", display=None, uuid=None):
    with closing(partner.db()) as con:
        con.execute("INSERT INTO partner_tags(account_id,user_id,status,source,first_seen,display_id,client_uuid) VALUES(?,?,?,?,?,?,?)",
                    (account, uid, status, "test", 0, display, uuid))
        con.commit()


def refer(referrer, referred):
    with closing(rewards.db()) as con:
        con.execute("INSERT INTO referrals(referred_id,referrer_id,code,status,created_at) VALUES(?,?,?,'qualified',?)", (referred, referrer, "abcdefgh", 0))
        con.commit()


REPORT = "Client ID,Commission (USD),Country\nCR100,12.34,NG\nCR200,\"$1,000.00\",NG\nCR300,5.00,GH\nCR999,50.00,KE\nCR100,1.00,NG\n"


class Reading(unittest.TestCase):
    def test_amounts_become_whole_cents(self):
        for text, cents in (("12.34", 1234), ("$1,234.56", 123456), ("(2.50)", -250), ("-0.015", -2), ("0.005", 1), (" 7 ", 700), ("1e2", 10000)):
            self.assertEqual(commission.to_cents(text), cents, text)
        for bad in ("", "abc", "nan", "inf", "99999999999"):
            with self.assertRaises(ValueError):
                commission.to_cents(bad)

    def test_separator_is_detected_and_bad_files_are_refused(self):
        for sep in (",", ";", "\t", "|"):
            h, rows = commission.parse_report(f"Client{sep}Amount\nCR1{sep}1.50\nCR2{sep}2.00\n")
            self.assertEqual((h, len(rows)), (["Client", "Amount"], 2))
        for bad in ("", "   ", "OnlyOneColumn\n1\n", "A,A\n1,2\n", "A,B\n"):
            with self.assertRaises(Exception):
                commission.parse_report(bad)

    def test_an_unquoted_thousands_separator_is_refused_instead_of_misread(self):
        with self.assertRaises(Exception) as cm:
            commission.parse_report("Client ID,Commission,Country\nCR200,$1,000.00,NG\n")
        self.assertEqual(cm.exception.status_code, 422)
        h, rows = commission.parse_report('Client ID,Commission,Country\nCR200,"$1,000.00",NG\n')
        self.assertEqual(commission.to_cents(rows[0]["Commission"]), 100000)

    def test_columns_are_guessed_but_the_admin_still_chooses(self):
        a = commission.analyse(REPORT)
        self.assertTrue(a["needs_columns"])
        self.assertEqual(a["guess"], {"id_col": "Client ID", "amount_col": "Commission (USD)"})


class Eligibility(unittest.TestCase):
    def setUp(self):
        wipe()
        self.r1, self.r2 = mk_user(), mk_user()
        self.f1, self.f2, self.f3, self.f4 = mk_user(), mk_user(), mk_user(), mk_user()
        refer(self.r1, self.f1)
        refer(self.r2, self.f2)
        refer(self.r1, self.f3)
        tag(self.f1, "CR100")
        tag(self.f2, "CR200", display="DISP200")
        tag(self.f3, "CR300", status="provisional")      # only signed in through us: Deriv has not confirmed the tag
        tag(self.f4, "CR400")                            # confirmed, but nobody referred this member

    def go(self, text=REPORT):
        return commission.analyse(text, "Client ID", "Commission (USD)")

    def test_ten_percent_only_for_confirmed_referred_members_and_the_rest_is_counted(self):
        a = self.go()
        got = {s["client_id"]: (s["referrer_id"], s["commission_cents"], s["share_cents"]) for s in a["shared"]}
        self.assertEqual(got, {"CR100": (self.r1, 1334, 133), "CR200": (self.r2, 100000, 10000)})    # CR100's two rows add up: 13.34
        self.assertEqual(a["reasons"], {"not_ours": 1, "no_member": 0, "not_confirmed": 1, "no_referrer": 0})
        self.assertEqual(a["unmatched_sample"], ["CR999"])
        self.assertEqual(a["share_total_cents"], 10133)

    def test_confirmed_but_not_referred_and_untied_tags_earn_nothing(self):
        t = "Client ID,Commission (USD)\nCR400,10\n"
        self.assertEqual(self.go(t)["reasons"]["no_referrer"], 1)
        tag(None, "CR500")
        self.assertEqual(self.go("Client ID,Commission (USD)\nCR500,10\n")["reasons"]["no_member"], 1)

    def test_the_client_can_be_named_by_account_or_display_id_in_any_case(self):
        a = self.go("Client ID,Commission (USD)\ndisp200,10\n")
        self.assertEqual(len(a["shared"]), 1)

    def test_unreadable_rows_are_counted_and_negative_adjustments_reduce_the_share(self):
        a = self.go("Client ID,Commission (USD)\nCR100,abc\n,5\nCR100,10.00\nCR100,(4.00)\n")
        self.assertEqual(a["bad_rows"], 2)
        self.assertEqual(a["shared"][0]["commission_cents"], 600)

    def test_wrong_columns_are_refused(self):
        with self.assertRaises(Exception):
            commission.analyse(REPORT, "Nope", "Commission (USD)")
        with self.assertRaises(Exception):
            commission.analyse(REPORT, "Client ID", "Client ID")


class ImportAndBalances(unittest.TestCase):
    def setUp(self):
        wipe()
        self.admin = mk_user("admin")
        self.r1, self.f1 = mk_user(), mk_user()
        refer(self.r1, self.f1)
        tag(self.f1, "CR100")
        self.a = commission.analyse(REPORT, "Client ID", "Commission (USD)")

    def test_apply_stores_rows_and_balances_follow(self):
        commission.apply(self.a, "2026-10", self.admin, "oct.csv", False)
        self.assertEqual(commission.balances(self.r1), {"earned": 133, "paid": 0, "reserved": 0, "available": 133})

    def test_a_month_cannot_be_imported_twice_but_can_be_corrected(self):
        commission.apply(self.a, "2026-10", self.admin, "oct.csv", False)
        with self.assertRaises(Exception) as cm:
            commission.apply(self.a, "2026-10", self.admin, "oct.csv", False)
        self.assertEqual(cm.exception.status_code, 409)
        fixed = commission.analyse("Client ID,Commission (USD)\nCR100,100.00\n", "Client ID", "Commission (USD)")
        r = commission.apply(fixed, "2026-10", self.admin, "oct2.csv", True)
        self.assertTrue(r["replaced"])
        self.assertEqual(commission.balances(self.r1)["earned"], 1000)               # replaced, not added
        commission.apply(fixed, "2026-11", self.admin, "nov.csv", False)             # another month adds
        self.assertEqual(commission.balances(self.r1)["earned"], 2000)


class Api(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ["REWARDS_ON"] = "1"
        os.environ["REWARDS_COMMISSION_ON"] = "1"

    @classmethod
    def tearDownClass(cls):
        os.environ.pop("REWARDS_COMMISSION_ON", None)

    def setUp(self):
        wipe()
        self.admin, self.r1, self.f1 = mk_user("admin"), mk_user(), mk_user()
        refer(self.r1, self.f1)
        tag(self.f1, "CR100")
        self.ca, self.cu = client(self.admin), client(self.r1)
        body = {"csv": REPORT, "period": "2026-10", "id_col": "Client ID", "amount_col": "Commission (USD)"}
        self.assertEqual(self.ca.post("/api/admin/commission/apply", json={**body, "confirm": True}).status_code, 200)

    def test_only_admins_import_and_the_import_needs_a_month_and_a_confirmation(self):
        body = {"csv": REPORT, "period": "2026-10", "id_col": "Client ID", "amount_col": "Commission (USD)"}
        self.assertEqual(self.cu.post("/api/admin/commission/preview", json=body).status_code, 403)
        self.assertEqual(self.ca.post("/api/admin/commission/preview", json={**body, "period": "October"}).status_code, 422)
        self.assertEqual(self.ca.post("/api/admin/commission/apply", json={**body, "period": "2026-09"}).status_code, 422)      # no confirm
        p = self.ca.post("/api/admin/commission/preview", json={**body, "period": "2026-10"}).json()
        self.assertEqual((p["already_imported"]["rows"], p["share_total"]), (1, 1.33))
        self.assertEqual(self.ca.post("/api/admin/commission/preview", json={"csv": REPORT, "period": "2026-10"}).json()["needs_columns"], True)

    def test_the_member_sees_commission_only_when_it_is_switched_on(self):
        v = self.cu.get("/api/rewards").json()["commission"]
        self.assertEqual((v["on"], v["earned"], v["available"], v["can_request"]), (True, 1.33, 1.33, False))
        os.environ.pop("REWARDS_COMMISSION_ON")
        try:
            self.assertEqual(self.cu.get("/api/rewards").json()["commission"], {"on": False})
            self.assertEqual(self.cu.post("/api/rewards/payout-request", json={}).status_code, 404)
        finally:
            os.environ["REWARDS_COMMISSION_ON"] = "1"

    def bigger(self):
        big = "Client ID,Commission (USD)\nCR100,500.00\n"
        body = {"csv": big, "period": "2026-11", "id_col": "Client ID", "amount_col": "Commission (USD)", "confirm": True}
        self.assertEqual(self.ca.post("/api/admin/commission/apply", json=body).status_code, 200)        # +50.00

    def test_addresses_are_checked_and_the_member_only_sees_a_masked_one(self):
        for net, addr in (("trc20", "0x" + "a" * 40), ("bep20", "T" + "a" * 33), ("btc", "x"), ("trc20", "")):
            self.assertEqual(self.cu.post("/api/rewards/payout-details", json={"network": net, "address": addr}).status_code, 422, (net, addr))
        good = "T" + "a" * 33
        self.assertEqual(self.cu.post("/api/rewards/payout-details", json={"network": "trc20", "address": good}).status_code, 200)
        d = self.cu.get("/api/rewards").json()["commission"]["details"]
        self.assertEqual(d["address"], "Taaa...aaaa")
        self.assertNotIn(good, json.dumps(self.cu.get("/api/rewards").json()))

    def test_payout_flow_minimum_one_at_a_time_paid_and_rejected(self):
        self.cu.post("/api/rewards/payout-details", json={"network": "bep20", "address": "0x" + "b" * 40})
        self.assertEqual(self.cu.post("/api/rewards/payout-request", json={}).status_code, 409)            # $1.33 is under the $10 minimum
        self.bigger()
        r = self.cu.post("/api/rewards/payout-request", json={})
        self.assertEqual((r.status_code, r.json()["amount"]), (200, 51.33))
        self.assertEqual(self.cu.post("/api/rewards/payout-request", json={}).status_code, 409)            # one waiting at a time
        self.assertEqual(self.cu.post("/api/rewards/payout-details", json={"network": "bep20", "address": "0x" + "c" * 40}).status_code, 409)
        self.assertEqual(commission.balances(self.r1), {"earned": 5133, "paid": 0, "reserved": 5133, "available": 0})
        rid = r.json()["id"]
        adm = self.ca.get("/api/admin/commission").json()
        self.assertEqual(adm["requests"][0]["address"], "0x" + "b" * 40)                                   # the admin sees it in full to pay it
        self.assertEqual(self.cu.post("/api/admin/commission/payout", json={"id": rid, "action": "paid", "txid": "abcdef12"}).status_code, 403)
        self.assertEqual(self.ca.post("/api/admin/commission/payout", json={"id": rid, "action": "paid", "txid": "x"}).status_code, 422)
        self.assertEqual(self.ca.post("/api/admin/commission/payout", json={"id": rid, "action": "paid", "txid": "0xabc123def456"}).status_code, 200)
        self.assertEqual(self.ca.post("/api/admin/commission/payout", json={"id": rid, "action": "reject", "note": "late"}).status_code, 409)   # already decided
        self.assertEqual(commission.balances(self.r1), {"earned": 5133, "paid": 5133, "reserved": 0, "available": 0})
        with closing(inbox.db()) as con:
            self.assertIn("0xabc123def456", con.execute("SELECT body FROM inbox_messages WHERE user_id=?", (self.r1,)).fetchone()["body"])

    def test_a_rejected_request_returns_to_the_balance(self):
        self.cu.post("/api/rewards/payout-details", json={"network": "trc20", "address": "T" + "d" * 33})
        self.bigger()
        rid = self.cu.post("/api/rewards/payout-request", json={}).json()["id"]
        self.assertEqual(self.ca.post("/api/admin/commission/payout", json={"id": rid, "action": "reject", "note": ""}).status_code, 422)
        self.assertEqual(self.ca.post("/api/admin/commission/payout", json={"id": rid, "action": "reject", "note": "Address not accepted"}).status_code, 200)
        self.assertEqual(commission.balances(self.r1)["available"], 5133)
        self.assertEqual(self.cu.post("/api/rewards/payout-request", json={}).status_code, 200)             # can ask again

    def test_a_correction_that_lowers_earnings_below_what_was_paid_is_flagged_not_hidden(self):
        self.cu.post("/api/rewards/payout-details", json={"network": "trc20", "address": "T" + "e" * 33})
        self.bigger()
        rid = self.cu.post("/api/rewards/payout-request", json={}).json()["id"]
        self.ca.post("/api/admin/commission/payout", json={"id": rid, "action": "paid", "txid": "0xabc123def456"})
        small = "Client ID,Commission (USD)\nCR100,1.00\n"
        self.ca.post("/api/admin/commission/apply", json={"csv": small, "period": "2026-11", "id_col": "Client ID", "amount_col": "Commission (USD)", "confirm": True, "replace": True})
        row = [r for r in self.ca.get("/api/admin/commission").json()["referrers"]][0]
        self.assertTrue(row["overpaid"])
        self.assertEqual(self.cu.get("/api/rewards").json()["commission"]["available"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
