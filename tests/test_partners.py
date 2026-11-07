"""Tests for the TradeLens partner referral programme (app/partners.py).
Run: python3 tests/test_partners.py   (own process, temp SQLite database, no network)."""
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
os.environ.update(DRIVERLINE_SECRET="s" * 40, REWARDS_ON="1")
os.environ.pop("DATABASE_URL", None)

from app import auth, credits, dbx, deriv_login, partners, rewards   # noqa: E402

dbx.SQLITE_PATH = Path(tempfile.mkdtemp()) / "t.db"
from starlette.testclient import TestClient   # noqa: E402
from app import main                          # noqa: E402

_seq = [0]
MASTER = "https://t.deriv.link?t=MASTERLINK1"
LINK_A = "https://t.deriv.link?t=PARTNERAAA1"
LINK_B = "https://t.deriv.link?t=PARTNERBBB2"
HOUSE = "https://t.deriv.link?t=HOUSELINK01"
OK = {"consent": True, "version": partners.CONSENT_VERSION}


def mk_user(tier="free"):
    _seq[0] += 1
    with closing(auth.db()) as con:
        uid = con.insert("INSERT INTO users(email,pw_hash,tier,created_at,blocked) VALUES(?,?,?,?,0)", (f"u{_seq[0]}@t.example", "h", tier, "2026-01-01T00:00:00"))
        con.commit()
    return uid


def email(uid):
    with closing(auth.db()) as con:
        return con.execute("SELECT email FROM users WHERE id=?", (uid,)).fetchone()["email"]


def client(uid, **cookies):
    c = TestClient(main.app)
    c.cookies.set(auth.COOKIE, auth.make_token(uid, "h"))
    for k, v in cookies.items():
        c.cookies.set(k, v)
    return c


def reset(mode="public"):
    partners._rl.clear()
    with closing(partners.db()) as con:
        for t in ("tl_partners", "tl_partner_referrals", "tl_partner_events", "tl_partner_consents", "tl_partner_verifications", "tl_partner_audit", "tl_partner_settings"):
            con.execute(f"DELETE FROM {t}")
        partners._set(con, "master_link", MASTER)
        partners._set(con, "approval_record", "Deriv Partner Management, ref TEST-1, 2026-11-01, no conditions")
        partners._set(con, "mode", mode)
        con.commit()


def make_partner(link=LINK_A, approve=True, verify=True, admin=None):
    """A member who applied, was approved by the admin and had their link mapped and verified."""
    before = partners.mode()
    with closing(partners.db()) as con:                 # open the programme while setting up, then put the mode back
        partners._set(con, "mode", "public")
        con.commit()
    try:
        return _make_partner(link, approve, verify, admin)
    finally:
        with closing(partners.db()) as con:
            partners._set(con, "mode", before)
            con.commit()


def _make_partner(link, approve, verify, admin):
    uid = mk_user()
    c = client(uid)
    assert c.post("/api/partners/become/start", json={**OK, "path": "master_link"}).status_code == 200
    ad = admin or client(mk_user("admin"))
    with closing(partners.db()) as con:
        row = partners._by_user(con, uid)
    if approve:
        r = ad.post(f"/api/admin/partners/{row['id']}/status", json={"action": "approve", "deriv_partner_id": f"DP{uid}", "source": "Deriv Partners Hub", "verified_on": "2026-11-05"})
        assert r.status_code == 200, r.text
    if link:
        assert ad.post(f"/api/admin/partners/{row['id']}/link", json={"deriv_referral_link": link}).status_code == 200
        if verify:
            assert ad.post(f"/api/admin/partners/{row['id']}/verify-link", json={"source": "Deriv Partners Hub", "verified_on": "2026-11-05"}).status_code == 200
    return uid, row["id"], row["ref_code"], ad


def signup_with(code, mode_cookie=True):
    """A brand-new member who arrived through a referral code (cookie kept by track.js)."""
    new = mk_user()

    class Req:
        cookies = {partners.COOKIE: code} if mode_cookie else {}
    partners.on_signup(new, Req())
    return new


def attributed_to(uid):
    with closing(partners.db()) as con:
        r = con.execute("SELECT partner_id FROM tl_partner_referrals WHERE referred_id=?", (uid,)).fetchone()
    return r["partner_id"] if r else None


def events(kind):
    with closing(partners.db()) as con:
        return con.execute("SELECT COUNT(*) AS n FROM tl_partner_events WHERE kind=?", (kind,)).fetchone()["n"]


class Attribution(unittest.TestCase):
    def setUp(self):
        reset()

    def test_a_valid_code_attributes_the_new_member_to_the_partner(self):
        _, pid, code, _ = make_partner()
        new = signup_with(code)
        self.assertEqual(attributed_to(new), pid)
        self.assertEqual(events("signup_attributed"), 1)

    def test_an_invalid_or_malformed_code_is_rejected_and_logged_never_stored(self):
        make_partner()
        for bad in ("NOPE123", "TLZZZZZZ", "'; DROP TABLE users;--", "x" * 40, "a b"):
            self.assertIsNone(attributed_to(signup_with(bad)))
        self.assertGreaterEqual(events("code_rejected"), 2)

    def test_a_code_of_a_partner_who_is_not_approved_is_not_accepted(self):
        uid, pid, code, ad = make_partner(approve=False, link=None)
        with closing(partners.db()) as con:
            code = partners._by_user(con, uid)["ref_code"]
        self.assertIsNone(attributed_to(signup_with(code)))
        uid2, pid2, code2, ad2 = make_partner(link=LINK_B)
        ad2.post(f"/api/admin/partners/{pid2}/status", json={"action": "suspend", "note": "checking"})
        self.assertIsNone(attributed_to(signup_with(code2)))

    def test_self_referral_is_prevented(self):
        uid, pid, code, _ = make_partner()

        class Req:
            cookies = {partners.COOKIE: code}
        partners.on_signup(uid, Req())
        self.assertIsNone(attributed_to(uid))
        self.assertEqual(events("self_referral_blocked"), 1)

    def test_the_first_valid_referral_wins_and_a_later_code_cannot_overwrite_it(self):
        _, p1, c1, ad = make_partner(LINK_A)
        _, p2, c2, _ = make_partner(LINK_B, admin=ad)
        new = signup_with(c1)

        class Req:
            cookies = {partners.COOKIE: c2}
        partners.on_signup(new, Req())
        partners.on_signup(new, Req())
        self.assertEqual(attributed_to(new), p1)
        with closing(partners.db()) as con:
            self.assertEqual(con.execute("SELECT COUNT(*) AS n FROM tl_partner_referrals WHERE referred_id=?", (new,)).fetchone()["n"], 1)
        self.assertEqual(events("duplicate_ignored"), 2)

    def test_nothing_is_recorded_while_the_programme_is_off(self):
        _, _, code, _ = make_partner()
        reset("off")
        with closing(partners.db()) as con:
            partners._set(con, "mode", "off")
            con.commit()
        self.assertIsNone(attributed_to(signup_with(code)))

    def test_the_public_code_check_says_only_yes_or_no(self):
        _, _, code, _ = make_partner()
        c = TestClient(main.app)
        self.assertEqual(c.get(f"/api/public/partner-ref?code={code}").json(), {"active": True, "valid": True})
        self.assertEqual(c.get("/api/public/partner-ref?code=NOPE12345").json(), {"active": True, "valid": False})
        reset("off")
        self.assertEqual(c.get(f"/api/public/partner-ref?code={code}").json(), {"active": False, "valid": False})


class TraderJourney(unittest.TestCase):
    def setUp(self):
        reset()

    def go(self, uid, **extra):
        return client(uid).post("/api/partners/trader/handoff", json={**OK, "new_account": True, **extra})

    def test_a_referred_member_is_routed_through_their_partners_verified_link(self):
        _, _, code, _ = make_partner(LINK_A)
        new = signup_with(code)
        r = self.go(new)
        self.assertEqual((r.status_code, r.json()["redirect"]), (200, LINK_A))
        self.assertEqual(events("trader_handoff"), 1)
        with closing(partners.db()) as con:
            self.assertEqual(con.execute("SELECT COUNT(*) AS n FROM tl_partner_consents WHERE user_id=? AND kind='trader_handoff'", (new,)).fetchone()["n"], 1)

    def test_two_partners_two_different_links(self):
        _, _, ca, ad = make_partner(LINK_A)
        _, _, cb, _ = make_partner(LINK_B, admin=ad)
        self.assertEqual(self.go(signup_with(ca)).json()["redirect"], LINK_A)
        self.assertEqual(self.go(signup_with(cb)).json()["redirect"], LINK_B)

    def test_no_consent_or_no_new_account_confirmation_means_no_redirect(self):
        _, _, code, _ = make_partner()
        new = signup_with(code)
        c = client(new)
        self.assertEqual(c.post("/api/partners/trader/handoff", json={"new_account": True}).status_code, 422)
        self.assertEqual(c.post("/api/partners/trader/handoff", json={"consent": True, "version": "old", "new_account": True}).status_code, 422)
        self.assertEqual(c.post("/api/partners/trader/handoff", json={**OK}).status_code, 422)
        self.assertEqual(events("trader_handoff"), 0)

    def test_an_unverified_link_blocks_the_handoff_and_the_master_link_is_never_substituted(self):
        _, pid, code, ad = make_partner(LINK_A, verify=False)
        new = signup_with(code)
        r = self.go(new)
        self.assertEqual(r.status_code, 409)
        self.assertNotIn(MASTER, r.text)
        self.assertNotIn("redirect", r.json())
        self.assertEqual(events("trader_handoff_blocked"), 1)

    def test_a_partner_suspended_after_the_signup_blocks_the_handoff(self):
        _, pid, code, ad = make_partner(LINK_A)
        new = signup_with(code)
        ad.post(f"/api/admin/partners/{pid}/status", json={"action": "suspend", "note": "under review"})
        r = self.go(new)
        self.assertEqual(r.status_code, 409)
        self.assertNotIn(LINK_A, r.text)

    def test_changing_a_partners_link_resets_verification_until_it_is_checked_again(self):
        _, pid, code, ad = make_partner(LINK_A)
        new = signup_with(code)
        ad.post(f"/api/admin/partners/{pid}/link", json={"deriv_referral_link": LINK_B})
        self.assertEqual(self.go(new).status_code, 409)
        ad.post(f"/api/admin/partners/{pid}/verify-link", json={"source": "Deriv Partners Hub", "verified_on": "2026-11-06"})
        self.assertEqual(self.go(new).json()["redirect"], LINK_B)

    def test_a_member_nobody_introduced_uses_the_house_link_only_once_it_is_approved_and_verified(self):
        owner = mk_user("admin")
        ad = client(owner)
        member = mk_user()
        self.assertEqual(self.go(member).status_code, 409)             # no house record: no hand-off, and not the master link
        self.assertEqual(ad.post("/api/admin/partners/house", json={"deriv_referral_link": HOUSE, "deriv_partner_id": "OWNER1"}).status_code, 200)
        self.assertEqual(self.go(member).status_code, 409)             # created but not approved
        with closing(partners.db()) as con:
            hid = partners._house(con)["id"]
        self.assertEqual(ad.post(f"/api/admin/partners/{hid}/status", json={"action": "approve", "source": "Deriv Partners Hub", "verified_on": "2026-11-05"}).status_code, 200)
        self.assertEqual(self.go(member).status_code, 409)             # approved, link not verified
        ad.post(f"/api/admin/partners/{hid}/verify-link", json={"source": "Deriv Partners Hub", "verified_on": "2026-11-05"})
        self.assertEqual(self.go(member).json()["redirect"], HOUSE)
        self.assertEqual(ad.post("/api/admin/partners/house", json={"deriv_referral_link": HOUSE}).status_code, 409)

    def test_existing_deriv_accounts_are_never_touched(self):
        _, _, code, _ = make_partner()
        new = signup_with(code)
        with closing(deriv_login.db()) as con:
            con.execute("INSERT INTO deriv_accounts(account_id,user_id,created_at,linked_at) VALUES('CR777',?,0,0)", (new,))
            con.commit()
        self.go(new)
        with closing(deriv_login.db()) as con:
            rows = [dict(r) for r in con.execute("SELECT account_id, user_id FROM deriv_accounts WHERE user_id=?", (new,)).fetchall()]
        self.assertEqual(rows, [{"account_id": "CR777", "user_id": new}])
        import ast
        tree = ast.parse(Path(partners.__file__).read_text())
        imported = {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names} | \
                   {n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
        self.assertFalse(imported & {"httpx", "requests", "urllib.request", "http.client", "socket", "websockets", "aiohttp"}, imported)   # no outbound call to Deriv at all
        sql = " ".join(n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str)).upper()
        for word in ("UPDATE DERIV_ACCOUNTS", "DELETE FROM DERIV_ACCOUNTS", "INSERT INTO DERIV_ACCOUNTS", "DERIV_TRADES"):
            self.assertNotIn(word, sql, word)


class PartnerJourney(unittest.TestCase):
    def setUp(self):
        reset()

    def test_become_a_partner_goes_through_the_master_link_and_waits_for_approval(self):
        uid = mk_user()
        c = client(uid)
        self.assertEqual(c.post("/api/partners/become/start", json={"path": "master_link"}).status_code, 422)          # consent needed
        r = c.post("/api/partners/become/start", json={**OK, "path": "master_link"})
        self.assertEqual((r.status_code, r.json()["redirect"], r.json()["status"]), (200, MASTER, "application_initiated"))
        self.assertEqual(events("master_handoff"), 1)
        c.post("/api/partners/become/submit", json={"deriv_partner_id": "DP-1234"})
        me = c.get("/api/partners/me").json()["partner"]
        self.assertEqual((me["status"], me["referral_url"], me["can_refer_traders"]), ("awaiting_deriv_approval", None, False))

    def test_the_master_link_is_not_used_for_traders_and_is_not_hard_coded(self):
        src = (Path(partners.__file__).read_text() + Path(partners.__file__).with_name("rewards.py").read_text())
        self.assertNotIn("GPPM4YUD4KBQ", src)
        self.assertNotIn("SQJFVSUTWWXT", src)
        _, _, code, _ = make_partner()
        r = client(signup_with(code)).post("/api/partners/trader/handoff", json={**OK, "new_account": True})
        self.assertNotEqual(r.json()["redirect"], MASTER)

    def test_without_a_configured_master_link_the_application_cannot_start_that_way(self):
        with closing(partners.db()) as con:
            partners._set(con, "master_link", "")
            con.commit()
        r = client(mk_user()).post("/api/partners/become/start", json={**OK, "path": "master_link"})
        self.assertEqual(r.status_code, 409)

    def test_an_already_tagged_client_skips_the_master_link_and_still_waits_for_verification(self):
        uid = mk_user()
        r = client(uid).post("/api/partners/become/start", json={**OK, "path": "already_tagged"})
        self.assertEqual((r.status_code, r.json()["redirect"], r.json()["status"]), (200, None, "awaiting_deriv_approval"))

    def test_an_unapproved_partner_cannot_operate_as_an_active_sub_partner(self):
        uid = mk_user()
        c = client(uid)
        c.post("/api/partners/become/start", json={**OK, "path": "master_link"})
        with closing(partners.db()) as con:
            code = partners._by_user(con, uid)["ref_code"]
        self.assertIsNone(attributed_to(signup_with(code)))             # code is dead until approval
        d = c.get("/api/partners/dashboard").json()
        self.assertIsNone(d["partner"]["referral_url"])
        self.assertFalse(d["partner"]["can_refer_traders"])

    def test_the_admin_must_record_the_verification_source_date_and_deriv_id_to_approve(self):
        uid = mk_user()
        client(uid).post("/api/partners/become/start", json={**OK, "path": "master_link"})
        ad = client(mk_user("admin"))
        with closing(partners.db()) as con:
            pid = partners._by_user(con, uid)["id"]
        url = f"/api/admin/partners/{pid}/status"
        self.assertEqual(ad.post(url, json={"action": "approve"}).status_code, 422)
        self.assertEqual(ad.post(url, json={"action": "approve", "source": "Hub", "verified_on": "05/11/2026", "deriv_partner_id": "DP1"}).status_code, 422)
        self.assertEqual(ad.post(url, json={"action": "approve", "source": "Deriv Partners Hub", "verified_on": "2026-11-05"}).status_code, 422)   # no partner id
        self.assertEqual(ad.post(url, json={"action": "approve", "source": "Deriv Partners Hub", "verified_on": "2026-11-05", "deriv_partner_id": "DP1"}).status_code, 200)

    def test_status_changes_follow_the_allowed_paths_and_each_is_audited(self):
        uid, pid, _, ad = make_partner()
        url = f"/api/admin/partners/{pid}/status"
        self.assertEqual(ad.post(url, json={"action": "suspend"}).status_code, 422)                        # reason needed
        self.assertEqual(ad.post(url, json={"action": "suspend", "note": "complaint"}).status_code, 200)
        self.assertEqual(ad.post(url, json={"action": "suspend", "note": "again"}).status_code, 409)
        self.assertEqual(ad.post(url, json={"action": "activate", "note": "resolved"}).status_code, 200)
        actions = [a["action"] for a in ad.get(f"/api/admin/partners/{pid}").json()["audit"]]
        for a in ("approve", "set_link", "verify_link", "suspend", "activate"):
            self.assertIn(a, actions)

    def test_links_must_be_official_https_deriv_addresses(self):
        uid, pid, _, ad = make_partner()
        for bad in ("http://t.deriv.link?t=X", "https://evil.example/?t=X", "https://t.deriv.link.evil.com/x", "https://user:pw@t.deriv.link/x",
                    "javascript:alert(1)", "https://t.deriv.link:8443/x", "https://t.deriv.link/ x", ""):
            self.assertEqual(ad.post(f"/api/admin/partners/{pid}/link", json={"deriv_referral_link": bad}).status_code, 422, bad)

    def test_two_partners_cannot_share_one_deriv_link(self):
        _, p1, _, ad = make_partner(LINK_A)
        uid2, p2, _, _ = make_partner(LINK_B, admin=ad)
        self.assertEqual(ad.post(f"/api/admin/partners/{p2}/link", json={"deriv_referral_link": LINK_A}).status_code, 409)


class Dashboard(unittest.TestCase):
    def setUp(self):
        reset()

    def test_counts_are_separated_and_unverifiable_figures_are_not_invented(self):
        uid, pid, code, ad = make_partner()
        a, b, c = signup_with(code), signup_with(code), signup_with(code)
        with closing(auth.db()) as con:
            con.execute("INSERT INTO usage(user_id,day,n) VALUES(?,?,2)", (a, "2026-11-02"))
            con.commit()
        client(a).post("/api/partners/trader/handoff", json={**OK, "new_account": True})
        client(a).post("/api/partners/trader/handoff", json={**OK, "new_account": True})        # same member twice counts once
        d = client(uid).get("/api/partners/dashboard").json()
        self.assertEqual(d["counts"], {"tradelens_registrations": 3, "completed_onboarding": 1, "deriv_handoffs_started": 1,
                                       "deriv_account_registrations": None, "deriv_confirmed_tagging": 0})
        ad.post(f"/api/admin/partners/{pid}/verify-client", json={"client": "CR12345", "source": "Active Trader Breakdown", "verified_on": "2026-11-05"})
        self.assertEqual(client(uid).get("/api/partners/dashboard").json()["counts"]["deriv_confirmed_tagging"], 1)
        text = str(d).lower()
        for banned in ("balance", "earning", "withdraw", "payout", "estimate"):
            self.assertNotIn(banned, text.replace("does not calculate, estimate", ""), banned)
        self.assertIn("deriv manages all official partner commissions", text)
        self.assertEqual(d["partner"]["referral_url"], f"http://testserver/login.html?ref={code}")


class Access(unittest.TestCase):
    def setUp(self):
        reset("off")

    def test_off_hides_everything_from_members_and_the_legacy_link(self):
        m = client(mk_user())
        for path in ("/api/partners/me", "/api/partners/dashboard"):
            self.assertEqual(m.get(path).status_code, 404, path)
        self.assertEqual(m.post("/api/partners/become/start", json={**OK, "path": "master_link"}).status_code, 404)
        self.assertEqual(m.post("/api/partners/trader/handoff", json={**OK, "new_account": True}).status_code, 404)
        self.assertFalse(m.get("/api/partners/access").json()["access"])
        self.assertFalse(partners.legacy_link_allowed())
        self.assertEqual(deriv_login._cfg()["ib"], "")                  # the old universal link is not shown or used

    def test_review_mode_opens_the_screens_to_admins_and_listed_reviewers_only(self):
        admin = client(mk_user("admin"))
        rv = mk_user()
        other = client(mk_user())
        admin.post("/api/admin/partners/settings", json={"mode": "review", "review_emails": email(rv)})
        self.assertTrue(client(rv).get("/api/partners/access").json()["access"])
        self.assertTrue(admin.get("/api/partners/access").json()["access"])
        self.assertFalse(other.get("/api/partners/access").json()["access"])
        self.assertEqual(other.get("/api/partners/me").status_code, 404)
        self.assertFalse(partners.legacy_link_allowed())

    def test_a_reviewer_can_read_the_admin_view_with_masked_emails_and_change_nothing(self):
        admin = client(mk_user("admin"))
        uid, pid, code, _ = make_partner(admin=admin)
        rv = mk_user()
        admin.post("/api/admin/partners/settings", json={"mode": "review", "review_emails": email(rv)})
        r = client(rv)
        self.assertTrue(r.get("/api/admin/partners/whoami").json()["read_only"])
        lst = r.get("/api/admin/partners").json()["partners"]
        self.assertEqual(len(lst), 1)
        self.assertNotIn(email(uid), str(lst))
        self.assertNotIn(email(uid), r.get(f"/api/admin/partners/{pid}").text)
        self.assertEqual(r.get("/api/admin/partners/settings").status_code, 200)
        self.assertNotIn("review_emails", r.get("/api/admin/partners/settings").json())
        for method, path, body in (("post", f"/api/admin/partners/{pid}/status", {"action": "suspend", "note": "x y z"}),
                                   ("post", f"/api/admin/partners/{pid}/link", {"deriv_referral_link": LINK_B}),
                                   ("post", f"/api/admin/partners/{pid}/verify-link", {"source": "abc", "verified_on": "2026-11-05"}),
                                   ("post", f"/api/admin/partners/{pid}/verify-client", {"client": "abc", "source": "abc", "verified_on": "2026-11-05"}),
                                   ("post", "/api/admin/partners/settings", {"mode": "public"}),
                                   ("post", "/api/admin/partners/house", {"deriv_referral_link": HOUSE})):
            self.assertIn(getattr(r, method)(path, json=body).status_code, (401, 403), path)

    def test_public_mode_needs_the_recorded_written_approval(self):
        admin = client(mk_user("admin"))
        with closing(partners.db()) as con:
            partners._set(con, "approval_record", "")
            con.commit()
        self.assertEqual(admin.post("/api/admin/partners/settings", json={"mode": "public"}).status_code, 409)
        admin.post("/api/admin/partners/settings", json={"approval_record": "Deriv Compliance email 2026-12-01, conditions: none"})
        self.assertEqual(admin.post("/api/admin/partners/settings", json={"mode": "public"}).status_code, 200)
        self.assertTrue(client(mk_user()).get("/api/partners/access").json()["access"])
        self.assertTrue(partners.legacy_link_allowed())

    def test_ordinary_users_cannot_change_any_approval_status_or_link(self):
        uid, pid, code, ad = make_partner()
        m = client(uid)                                      # the partner themself
        for path, body in ((f"/api/admin/partners/{pid}/status", {"action": "approve", "source": "abc", "verified_on": "2026-11-05", "deriv_partner_id": "DP1"}),
                           (f"/api/admin/partners/{pid}/link", {"deriv_referral_link": LINK_B}),
                           (f"/api/admin/partners/{pid}/verify-link", {"source": "abc", "verified_on": "2026-11-05"}),
                           ("/api/admin/partners/settings", {"master_link": LINK_B}), ("/api/admin/partners/house", {"deriv_referral_link": HOUSE})):
            self.assertIn(m.post(path, json=body).status_code, (401, 403, 404), path)
        self.assertIn(TestClient(main.app).get("/api/admin/partners").status_code, (401, 403, 404))
        with closing(partners.db()) as con:
            self.assertEqual(partners._by_user(con, uid)["deriv_referral_link"], LINK_A)

    def test_settings_changes_are_audited_and_reviewers_are_validated(self):
        admin = client(mk_user("admin"))
        self.assertEqual(admin.post("/api/admin/partners/settings", json={"review_emails": "not-an-email"}).status_code, 422)
        self.assertEqual(admin.post("/api/admin/partners/settings", json={"master_link": "https://evil.example/x"}).status_code, 422)
        self.assertEqual(admin.post("/api/admin/partners/settings", json={"master_link": MASTER}).status_code, 200)
        with closing(partners.db()) as con:
            self.assertGreaterEqual(con.execute("SELECT COUNT(*) AS n FROM tl_partner_audit WHERE action='settings'").fetchone()["n"], 1)

    def test_search_finds_partners_by_email_partner_id_or_code(self):
        admin = client(mk_user("admin"))
        uid, pid, code, _ = make_partner(admin=admin)
        for q in (email(uid), f"TLP{pid:04d}", code.lower()):
            self.assertEqual(len(admin.get("/api/admin/partners", params={"q": q}).json()["partners"]), 1, q)
        self.assertEqual(admin.get("/api/admin/partners", params={"q": "nobody"}).json()["partners"], [])


class PrivacyAndConsent(unittest.TestCase):
    def setUp(self):
        reset("public")

    def test_the_privacy_notice_is_public_and_linked_from_login_terms_and_consent_screens(self):
        c = TestClient(main.app)
        r = c.get("/privacy.html")
        self.assertEqual(r.status_code, 200)
        for word in ("Anthropic", "NOWPayments", "Deriv", "consent", "Cookies", "not owned, operated or endorsed by Deriv"):
            self.assertIn(word, r.text)
        for page in ("/login.html", "/terms.html", "/partner.html"):
            self.assertIn("/privacy.html", c.get(page).text, page)

    def test_the_consent_notices_point_to_the_privacy_notice_and_use_the_new_version(self):
        self.assertEqual(partners.CONSENT_VERSION, "2026-11-v2")
        self.assertTrue(any("/privacy.html" in t for t in partners.TRADER_DISCLOSURE))
        self.assertTrue(any("/privacy.html" in t for t in partners.PARTNER_DISCLOSURE))
        m = client(mk_user()).get("/api/partners/me").json()
        self.assertEqual(m["disclosures"]["version"], "2026-11-v2")

    def test_consent_given_to_the_old_notice_version_is_refused(self):
        r = client(mk_user()).post("/api/partners/become/start", json={"consent": True, "version": "2026-11-v1", "path": "master_link"})
        self.assertEqual(r.status_code, 422)


class ExistingFeaturesUntouched(unittest.TestCase):
    def setUp(self):
        reset("off")

    def test_signup_and_invite_codes_work_with_the_programme_off(self):
        with closing(auth.db()) as con:
            con.execute("INSERT INTO invites(code,tier,uses_left) VALUES('KEEP1','free',2) ON CONFLICT(code) DO NOTHING")
            con.commit()
        c = TestClient(main.app)
        r = c.post("/api/auth/signup", json={"email": "new.member@t.example", "password": "a-long-password-1", "invite": "KEEP1"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(c.post("/api/auth/signup", json={"email": "x.member@t.example", "password": "a-long-password-1", "invite": "WRONG"}).status_code // 100, 4)
        with closing(auth.db()) as con:
            self.assertEqual(con.execute("SELECT uses_left FROM invites WHERE code='KEEP1'").fetchone()["uses_left"], 1)

    def test_a_signup_with_a_partner_cookie_attaches_the_partner_once_when_the_programme_is_on(self):
        reset("public")
        _, pid, code, ad = make_partner()
        with closing(auth.db()) as con:
            con.execute("INSERT INTO invites(code,tier,uses_left) VALUES('KEEP2','free',5) ON CONFLICT(code) DO NOTHING")
            con.commit()
        c = TestClient(main.app)
        c.cookies.set(partners.COOKIE, code)
        self.assertEqual(c.post("/api/auth/signup", json={"email": "ref.member@t.example", "password": "a-long-password-1", "invite": "KEEP2"}).status_code, 200)
        with closing(auth.db()) as con:
            uid = con.execute("SELECT id FROM users WHERE email='ref.member@t.example'").fetchone()["id"]
        self.assertEqual(attributed_to(uid), pid)

    def test_a_failure_in_the_partner_module_never_breaks_signup(self):
        orig = partners.on_signup
        partners.on_signup = lambda *a, **k: 1 / 0
        try:
            c = TestClient(main.app)
            with closing(auth.db()) as con:
                con.execute("INSERT INTO invites(code,tier,uses_left) VALUES('KEEP3','free',5) ON CONFLICT(code) DO NOTHING")
                con.commit()
            self.assertEqual(c.post("/api/auth/signup", json={"email": "safe.member@t.example", "password": "a-long-password-1", "invite": "KEEP3"}).status_code, 200)
        finally:
            partners.on_signup = orig


if __name__ == "__main__":
    unittest.main(verbosity=1)
