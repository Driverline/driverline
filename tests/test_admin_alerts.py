"""Tests for the admin alerts (app/admin_alerts.py), the hooks that raise them, and the paged chat (app/inbox.py member_view).
Run: python3 tests/test_admin_alerts.py   (own process, temp SQLite database, no network). The app's background jobs are not started."""
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

from app import auth, dbx                                          # noqa: E402

dbx.SQLITE_PATH = Path(tempfile.mkdtemp()) / "t.db"
from starlette.testclient import TestClient                        # noqa: E402
from app import admin_alerts, credits, inbox, main, push           # noqa: E402

_seq = [0]


def mk(tier="free"):
    _seq[0] += 1
    with closing(auth.db()) as con:
        uid = con.insert("INSERT INTO users(email,pw_hash,tier,created_at) VALUES(?,?,?,?)", (f"a{_seq[0]}@t.example", "h", tier, "2026-01-01T00:00:00"))
        con.commit()
    c = TestClient(main.app)
    c.cookies.set(auth.COOKIE, auth.make_token(uid, "h"))
    return uid, c


def alerts():
    with closing(admin_alerts.db()) as con:
        return [dict(r) for r in con.execute("SELECT * FROM admin_alerts ORDER BY id").fetchall()]


class Alerts(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.aid, cls.a = mk("admin")
        cls.mid, cls.m = mk("free")

    def setUp(self):
        with closing(admin_alerts.db()) as con:
            con.execute("DELETE FROM admin_alerts")
            con.commit()

    def test_an_alert_is_recorded_once_and_a_repeat_is_ignored(self):
        a = admin_alerts.notify("payment", "Payment", "x", self.mid, ref="R1", sender=lambda s, p: True, wait=True)
        self.assertIsNotNone(a)
        self.assertIsNone(admin_alerts.notify("payment", "Payment", "x", self.mid, ref="R1", sender=lambda s, p: True, wait=True))
        self.assertEqual(len(alerts()), 1)

    def test_it_never_raises(self):
        real = admin_alerts.db
        admin_alerts.db = lambda: (_ for _ in ()).throw(RuntimeError("down"))
        try:
            self.assertIsNone(admin_alerts.notify("payment", "t", "b", 1, ref="Z"))
        finally:
            admin_alerts.db = real

    def test_push_goes_only_to_admin_devices_and_dead_ones_are_removed(self):
        with closing(push.db()) as con:
            con.execute("DELETE FROM push_subs")
            for uid, ep in ((self.aid, "https://adm/live"), (self.aid, "https://adm/dead"), (self.mid, "https://member/x")):
                con.execute("INSERT INTO push_subs(user_id,endpoint,p256dh,auth,created_at) VALUES(?,?,?,?,?)", (uid, ep, "k", "a", int(time.time())))
            con.commit()
        got = []

        def sender(sub, payload):
            got.append(sub["endpoint"])
            return sub["endpoint"] != "https://adm/dead"
        admin_alerts.notify("mt5_request", "MT5 details sent", "b", self.mid, ref=1, sender=sender, wait=True)
        self.assertEqual(sorted(got), ["https://adm/dead", "https://adm/live"])
        with closing(push.db()) as con:
            left = [r["endpoint"] for r in con.execute("SELECT endpoint FROM push_subs").fetchall()]
        self.assertEqual(sorted(left), ["https://adm/live", "https://member/x"])

    def test_list_is_oldest_first_pages_and_unread_counts(self):
        for i in range(35):
            admin_alerts.notify("payment", f"P{i}", "b", self.mid, ref=f"r{i}", sender=lambda s, p: True, wait=True)
        d = self.a.get("/api/admin/alerts").json()
        self.assertEqual([x["title"] for x in d["alerts"]][-1], "P34")           # newest at the bottom
        self.assertEqual((len(d["alerts"]), d["has_more"], d["unread"]), (30, True, 35))
        older = self.a.get(f"/api/admin/alerts?before={d['alerts'][0]['id']}").json()
        self.assertEqual((len(older["alerts"]), older["has_more"]), (5, False))
        self.assertEqual(self.a.post("/api/admin/alerts/read", json={"all": True}).json()["unread"], 0)
        self.assertEqual(self.a.get("/api/admin/inbox/summary").json()["alerts"], 0)

    def test_members_cannot_read_or_clear_alerts(self):
        self.assertEqual(self.m.get("/api/admin/alerts").status_code, 403)
        self.assertEqual(self.m.post("/api/admin/alerts/read", json={"all": True}).status_code, 403)
        self.assertEqual(TestClient(main.app).get("/api/admin/alerts").status_code, 401)

    def test_summary_includes_alerts_without_changing_the_old_keys(self):
        admin_alerts.notify("partner", "t", "b", self.mid, ref="p", sender=lambda s, p: True, wait=True)
        s = self.a.get("/api/admin/inbox/summary").json()
        self.assertEqual((s["alerts"], "unread" in s, "conversations" in s), (1, True, True))


class Hooks(unittest.TestCase):
    """The real endpoints raise the alerts, and carry no secret."""
    @classmethod
    def setUpClass(cls):
        cls.aid, cls.a = mk("admin")
        cls.mid, cls.m = mk("free")
        with closing(admin_alerts.db()) as con:
            con.execute("DELETE FROM admin_alerts")
            con.commit()

    def test_a_manual_payment_claim_alerts_the_admins(self):
        with closing(credits.db()) as con:
            con.execute("INSERT OR REPLACE INTO billing_settings(key,value) VALUES('enabled','1')")
            con.execute("INSERT OR REPLACE INTO billing_settings(key,value) VALUES('pay_trc20','TXaddress')")
            con.commit()
        credits._cache["ts"] = 0
        o = self.m.post("/api/billing/order", json={"product": "copy", "method": "trc20", "accepted": True})
        self.assertEqual(o.status_code, 200, o.text)
        oid = o.json()["order"]["id"]
        self.assertEqual(self.m.post(f"/api/billing/order/{oid}/paid", json={"txid": "abcdef123456"}).status_code, 200)
        rows = [r for r in alerts() if r["kind"] == "payment"]
        self.assertEqual(len(rows), 1)
        self.assertIn("USDT sent", rows[0]["title"])
        self.assertNotIn("abcdef123456", rows[0]["body"] + rows[0]["title"])

    def test_mt5_details_partner_copy_and_auto_each_alert_without_secrets(self):
        from cryptography.fernet import Fernet
        from app import copy_trading, ea, partners
        os.environ["DRIVERLINE_VAULT_KEY"] = Fernet.generate_key().decode()
        uid, c = mk("free")
        with closing(credits.db()) as con:
            con.execute("INSERT OR REPLACE INTO billing_settings(key,value) VALUES('enabled','0')")
            con.commit()
        credits._cache["ts"] = 0
        r = c.post("/api/mt5req", json={"details": "Login: 55555\nPassword: TopSecret99\nServer: Deriv-Demo", "consent": True})
        self.assertEqual(r.status_code, 200, r.text)
        mt5 = [x for x in alerts() if x["kind"] == "mt5_request"][-1]
        self.assertIn("55555", mt5["body"])
        self.assertNotIn("TopSecret99", mt5["body"] + mt5["title"])
        # partner application: started, then submitted
        with closing(partners.db()) as con:
            partners._set(con, "mode", "public")
            partners._set(con, "master_link", "https://t.deriv.link?t=MASTER1")
            con.commit()
        ok = {"consent": True, "version": partners.CONSENT_VERSION}
        self.assertEqual(c.post("/api/partners/become/start", json={**ok, "path": "master_link"}).status_code, 200)
        self.assertEqual(c.post("/api/partners/become/submit", json={"deriv_partner_id": "DP-9999"}).status_code, 200)
        titles = [x["title"] for x in alerts() if x["kind"] == "partner"]
        self.assertEqual(sorted(titles), ["Partner application started", "Partner application submitted"])
        # an admin applying (a test) raises nothing
        n = len(alerts())
        self.assertEqual(self.a.post("/api/partners/become/start", json={**ok, "path": "master_link"}).status_code, 200)
        self.assertEqual(len(alerts()), n)

    def test_chat_pages(self):
        uid, c = mk("free")
        with closing(inbox.db()) as con:
            for i in range(95):
                con.insert("INSERT INTO inbox_messages(user_id,sender,body,created_at) VALUES(?,?,?,?)", (uid, "user" if i % 2 else "admin", f"m{i}", 1000 + i))
            for i in range(20):
                con.insert("INSERT INTO inbox_broadcasts(admin_id,title,body,created_at) VALUES(?,?,?,?)", (1, f"t{i}", f"b{i}", 1000 + i * 4))
            con.commit()
        d = c.get("/api/inbox").json()
        self.assertEqual((len(d["messages"]), d["messages"][-1]["body"], d["more_messages"]), (40, "m94", True))
        self.assertEqual((len(d["broadcasts"]), d["broadcasts"][-1]["body"], d["more_broadcasts"]), (15, "b19", True))
        self.assertEqual(d["messages"][0]["created_at"] < d["messages"][-1]["created_at"], True)       # oldest first
        o = c.get(f"/api/inbox?before_msg={d['messages'][0]['id']}&before_bc={d['broadcasts'][0]['id']}").json()
        self.assertEqual((len(o["messages"]), o["messages"][-1]["body"], o["more_messages"]), (40, "m54", True))
        self.assertEqual((len(o["broadcasts"]), o["more_broadcasts"]), (5, False))
        oo = c.get(f"/api/inbox?before_msg={o['messages'][0]['id']}&before_bc=0").json()
        self.assertEqual((len(oo["messages"]), oo["more_messages"], oo["broadcasts"]), (15, False, []))
        c.post("/api/inbox/send", json={"body": "new one"})
        n = c.get(f"/api/inbox?after_msg={d['messages'][-1]['id']}&after_bc={d['broadcasts'][-1]['id']}").json()
        self.assertEqual([m["body"] for m in n["messages"]], ["new one"])
        self.assertEqual(n["broadcasts"], [])

    def test_another_member_never_sees_this_chat(self):
        _, other = mk("free")
        with closing(inbox.db()) as con:
            con.insert("INSERT INTO inbox_messages(user_id,sender,body,created_at) VALUES(?,?,?,?)", (self.mid, "admin", "private", 5))
            con.commit()
        self.assertEqual([m["body"] for m in other.get("/api/inbox").json()["messages"]], [])


if __name__ == "__main__":
    unittest.main()
