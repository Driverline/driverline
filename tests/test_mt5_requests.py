"""Tests for MT5 connection requests (app/mt5_requests.py). Run: python3 tests/test_mt5_requests.py   (own process, temp SQLite database, no network, no broker)."""
import json
import os
import sys
import tempfile
import time
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
import types
for _n in ("websockets", "pywebpush", "psycopg", "psycopg.rows"):
    try:
        __import__(_n)
    except ImportError:
        sys.modules[_n] = types.ModuleType(_n)
os.environ.update(DRIVERLINE_SECRET="s" * 40)
os.environ.pop("DATABASE_URL", None)
os.environ.pop("DRIVERLINE_MEMBER_KEYS", None)
from cryptography.fernet import Fernet
VAULT = Fernet.generate_key().decode()
os.environ["DRIVERLINE_VAULT_KEY"] = VAULT

from app import auth, credits, dbx, ea, mt5_requests as MR, pending_orders, signal_feed     # noqa: E402
from starlette.applications import Starlette                           # noqa: E402
from starlette.exceptions import HTTPException                         # noqa: E402
from starlette.responses import JSONResponse                           # noqa: E402
from starlette.testclient import TestClient                            # noqa: E402

TMP = Path(tempfile.mkdtemp())
dbx.SQLITE_PATH = TMP / "t.db"


async def _err(request, exc):
    return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)


APP = Starlette(routes=[*ea.routes, *MR.routes], exception_handlers={HTTPException: _err})
_seq, _clients = [0], {}
ANON = TestClient(APP)
ANON.__enter__()
SPECS = [{"symbol": "EURUSDm", "tick_size": 0.00001, "tick_value": 1.0, "vol_min": 0.01, "vol_step": 0.01, "vol_max": 100, "contract_size": 100000}]


def mk_user(tier="member"):
    _seq[0] += 1
    with closing(auth.db()) as con:
        uid = con.insert("INSERT INTO users(email,pw_hash,tier,created_at) VALUES(?,?,?,?)", (f"a{_seq[0]}@t.example", "h", tier, "2026-01-01T00:00:00"))
        con.commit()
    return uid


def cl(uid):
    if uid not in _clients:
        c = TestClient(APP)
        c.__enter__()
        c.cookies.set(auth.COOKIE, auth.make_token(uid, "h"))
        _clients[uid] = c
    return _clients[uid]


def q(sqltext, *a):
    with closing(ea.db()) as con:
        r = con.execute(sqltext, a).fetchall()
        con.commit()
    return [dict(x) for x in r]


def add(uid, label):
    r = cl(uid).post("/api/ea/accounts", json={"label": label})
    assert r.status_code == 200, r.text
    return r.json()["id"], r.json()["key"]


def eareport(key, login="1", positions=(), balance=1000.0, demo=True, **flags):
    ea._last_report.clear()
    acc = {"login": login, "server": "Deriv-Demo", "currency": "USD", "balance": balance, "equity": balance, "demo": demo,
           "remote_close": True, "manage_allowed": True, "execute_allowed": True, "pending_allowed": True, **flags}
    body = {"account": acc, "today": {"closed_pnl": 0}, "positions": list(positions), "orders": [], "specs": SPECS}
    return ANON.post("/api/ea/report", json=body, headers={"x-driverline-key": key})


def pos(ticket, typ="BUY", symbol="EURUSDm", volume=0.1, op=1.1000, sl=1.0990, tp=1.1030):
    return {"ticket": ticket, "symbol": symbol, "type": typ, "volume": volume, "open": op, "sl": sl, "tp": tp, "profit": 0, "risk": 1}


def eapoll(key):
    ea._last_poll.clear()
    return ANON.post("/api/ea/poll", json={}, headers={"x-driverline-key": key})




PW = "SmartTrader@3438"
DET = lambda login="140374097", pw=PW, server="DerivSVG-Server-03": f"Login: {login}\n\nPassword: {pw}\n\nServer: {server}\n"


def submit(u, details=None, consent=True, **kw):
    return cl(u).post("/api/mt5req", json={"details": details if details is not None else DET(), "consent": consent, **kw})


class ParseTests(unittest.TestCase):
    def test_the_requested_format_and_sloppy_variants(self):
        self.assertEqual(MR.parse_details(DET()), ("140374097", PW, "DerivSVG-Server-03"))
        self.assertEqual(MR.parse_details("login=123456\nPASSWORD :  a b c d \nserver: Deriv-Demo"), ("123456", "a b c d", "Deriv-Demo"))
        self.assertEqual(MR.parse_details("Login: 1234\r\nPassword: pa:ss=word\r\nServer: X Y"), ("1234", "pa:ss=word", "X Y"))     # ':' inside the password survives

    def test_bad_input_is_rejected_with_a_helpful_message(self):
        from starlette.exceptions import HTTPException as H
        for bad in ("", "Login: 123456\nPassword: abcd", "Password: abcd\nServer: Deriv-Demo", "Login: 12ab\nPassword: abcd\nServer: Deriv-Demo",
                    "Login: 123456\nPassword: abc\nServer: Deriv-Demo", "Login: 123456\nPassword: abcd\nServer: bad<script>", "Login: 123456\nPassword: " + "x" * 65 + "\nServer: Deriv-Demo",
                    "Login: 123456\nPassword: abcd\nServer: Deriv-Demo\n" + "x" * 700):
            with self.assertRaises(H, msg=bad[:30]) as c:
                MR.parse_details(bad)
            self.assertEqual(c.exception.status_code, 422)


class MemberFlowTests(unittest.TestCase):
    def test_submit_stores_the_password_encrypted_and_never_returns_it(self):
        u = mk_user()
        r = submit(u)
        self.assertEqual(r.status_code, 200, r.text)
        row = q("SELECT * FROM mt5_requests WHERE id=?", r.json()["id"])[0]
        self.assertEqual((row["login"], row["server"], row["status"]), ("140374097", "DerivSVG-Server-03", "new"))
        self.assertNotIn(PW, row["pw_enc"])
        self.assertNotIn(PW, json.dumps(row))
        self.assertEqual(Fernet(VAULT.encode()).decrypt(row["pw_enc"].encode()).decode(), PW)
        mine = cl(u).get("/api/mt5req/me")
        self.assertNotIn(PW, mine.text)
        self.assertNotIn("pw_enc", mine.text)
        self.assertEqual(mine.json()["requests"][0]["status"], "new")
        self.assertNotIn(PW, r.text)

    def test_consent_is_required_and_the_vault_key_is_required(self):
        u = mk_user()
        self.assertEqual(submit(u, consent=False).status_code, 422)
        self.assertEqual(cl(u).post("/api/mt5req", json={"details": DET()}).status_code, 422)
        os.environ["DRIVERLINE_VAULT_KEY"] = ""
        try:
            r = submit(u)
            self.assertEqual(r.status_code, 503)
            self.assertEqual(q("SELECT COUNT(*) AS c FROM mt5_requests WHERE user_id=?", u)[0]["c"], 0)       # nothing was stored in the clear
        finally:
            os.environ["DRIVERLINE_VAULT_KEY"] = VAULT

    def test_limits_duplicates_and_entitlement(self):
        u = mk_user()
        self.assertEqual(submit(u).status_code, 200)
        self.assertEqual(submit(u).status_code, 409)                                          # same login twice
        self.assertEqual(submit(u, DET("111111")).status_code, 200)
        self.assertEqual(submit(u, DET("222222")).status_code, 200)
        r = submit(u, DET("333333"))
        self.assertEqual(r.status_code, 409)                                                  # 3 accounts/requests is the limit
        self.assertIn("up to 3", r.json()["detail"])
        f = mk_user("free")
        credits.billing_on = lambda: True
        try:
            self.assertEqual(submit(f).status_code, 403)
        finally:
            credits.billing_on = lambda: False
        self.assertEqual(ANON.post("/api/mt5req", json={"details": DET(), "consent": True}).status_code, 401)

    def test_cancelling_deletes_the_password_and_only_the_owner_can(self):
        u, x = mk_user(), mk_user()
        rid = submit(u).json()["id"]
        self.assertEqual(cl(x).post(f"/api/mt5req/{rid}/cancel").status_code, 404)
        self.assertIsNotNone(q("SELECT pw_enc FROM mt5_requests WHERE id=?", rid)[0]["pw_enc"])
        self.assertEqual(cl(u).post(f"/api/mt5req/{rid}/cancel").status_code, 200)
        row = q("SELECT * FROM mt5_requests WHERE id=?", rid)[0]
        self.assertEqual((row["pw_enc"], row["status"]), (None, "cancelled"))
        self.assertEqual(cl(u).post(f"/api/mt5req/{rid}/cancel").status_code, 409)

    def test_members_cannot_make_their_own_keys_by_default(self):
        u = mk_user()
        self.assertEqual(cl(u).post("/api/ea/accounts", json={"label": "Mine"}).status_code, 403)
        self.assertEqual(cl(u).post("/api/ea/key").status_code, 403)
        self.assertFalse(cl(u).get("/api/ea/status").json()["self_serve"])
        a = mk_user("admin")
        self.assertEqual(cl(a).post("/api/ea/accounts", json={"label": "Mine"}).status_code, 200)
        self.assertTrue(cl(a).get("/api/ea/status").json()["self_serve"])

    def test_old_requests_are_wiped(self):
        u = mk_user()
        rid = submit(u).json()["id"]
        q("UPDATE mt5_requests SET created_at=? WHERE id=?", int(time.time()) - (MR.STALE_DAYS + 1) * 86400, rid)
        cl(u).get("/api/mt5req/me")
        row = q("SELECT * FROM mt5_requests WHERE id=?", rid)[0]
        self.assertEqual((row["pw_enc"], row["status"]), (None, "expired"))


class AdminFlowTests(unittest.TestCase):
    def test_only_an_admin_can_list_reveal_provision_or_change(self):
        u, adm = mk_user(), mk_user("admin")
        rid = submit(u).json()["id"]
        for method, path, body in (("get", "/api/mt5req/admin", None), ("post", f"/api/mt5req/admin/{rid}/reveal", None),
                                   ("post", f"/api/mt5req/admin/{rid}/provision", None), ("post", f"/api/mt5req/admin/{rid}/key", None),
                                   ("post", f"/api/mt5req/admin/{rid}/set", {"action": "wipe"})):
            r = getattr(cl(u), method)(path, **({"json": body} if body is not None else {}))
            self.assertEqual(r.status_code, 403, path)
            self.assertIn(getattr(ANON, method)(path, **({"json": body} if body is not None else {})).status_code, (401, 403))
        lst = cl(adm).get("/api/mt5req/admin").json()
        self.assertNotIn(PW, json.dumps(lst))                                                 # the list never carries a password
        self.assertTrue(any(r["id"] == rid and r["has_password"] for r in lst["requests"]))

    def test_reveal_is_audited_and_gives_exactly_what_the_member_sent(self):
        u, adm = mk_user(), mk_user("admin")
        rid = submit(u).json()["id"]
        r = cl(adm).post(f"/api/mt5req/admin/{rid}/reveal")
        self.assertEqual(r.json(), {"login": "140374097", "password": PW, "server": "DerivSVG-Server-03"})
        self.assertIn("no-store", r.headers["cache-control"])
        log = q("SELECT * FROM mt5_request_log WHERE request_id=?", rid)
        self.assertEqual([(l["action"], l["admin_id"]) for l in log], [("revealed", adm)])

    def test_provision_connect_and_the_password_is_deleted_when_the_ea_reports(self):
        u, adm = mk_user(), mk_user("admin")
        rid = submit(u, label="Live").json()["id"]
        r = cl(adm).post(f"/api/mt5req/admin/{rid}/provision")
        self.assertEqual(r.status_code, 200, r.text)
        key, aid = r.json()["key"], r.json()["account_id"]
        self.assertEqual(cl(adm).post(f"/api/mt5req/admin/{rid}/provision").status_code, 409)           # once only
        self.assertEqual(q("SELECT status FROM mt5_requests WHERE id=?", rid)[0]["status"], "in_progress")
        self.assertEqual(q("SELECT user_id,label FROM ea_accounts WHERE id=?", aid), [{"user_id": u, "label": "Live"}])
        self.assertIsNotNone(q("SELECT pw_enc FROM mt5_requests WHERE id=?", rid)[0]["pw_enc"])           # still needed until the EA connects
        self.assertEqual(eareport(key, "140374097", [], 100.0).status_code, 200)
        row = q("SELECT * FROM mt5_requests WHERE id=?", rid)[0]
        self.assertEqual((row["status"], row["pw_enc"]), ("connected", None))
        self.assertEqual(cl(adm).post(f"/api/mt5req/admin/{rid}/reveal").status_code, 409)               # gone for good
        self.assertEqual(cl(u).get("/api/ea/accounts").json()["accounts"][0]["login"], "140374097")
        self.assertEqual(cl(u).get("/api/mt5req/me").json()["requests"], [])                              # no longer an open request
        self.assertTrue(any(l["action"].startswith("connected") for l in q("SELECT action FROM mt5_request_log WHERE request_id=?", rid)))

    def test_reject_needs_a_reason_wipes_the_password_and_the_member_sees_the_reason(self):
        u, adm = mk_user(), mk_user("admin")
        rid = submit(u).json()["id"]
        self.assertEqual(cl(adm).post(f"/api/mt5req/admin/{rid}/set", json={"action": "reject"}).status_code, 422)
        self.assertEqual(cl(adm).post(f"/api/mt5req/admin/{rid}/set", json={"action": "reject", "note": "Wrong server name"}).status_code, 200)
        row = q("SELECT * FROM mt5_requests WHERE id=?", rid)[0]
        self.assertEqual((row["status"], row["pw_enc"]), ("rejected", None))
        mine = cl(u).get("/api/mt5req/me").json()["requests"][0]
        self.assertEqual((mine["status"], mine["note"]), ("rejected", "Wrong server name"))
        self.assertEqual(submit(u).status_code, 200)                                                     # the member can send it again

    def test_wipe_progress_and_new_key(self):
        u, adm = mk_user(), mk_user("admin")
        rid = submit(u).json()["id"]
        self.assertEqual(cl(adm).post(f"/api/mt5req/admin/{rid}/key").status_code, 404)                  # no account yet
        self.assertEqual(cl(adm).post(f"/api/mt5req/admin/{rid}/set", json={"action": "progress"}).status_code, 200)
        self.assertEqual(cl(adm).post(f"/api/mt5req/admin/{rid}/set", json={"action": "progress"}).status_code, 409)
        k1 = cl(adm).post(f"/api/mt5req/admin/{rid}/provision").json()["key"]
        k2 = cl(adm).post(f"/api/mt5req/admin/{rid}/key").json()["key"]
        self.assertEqual(eareport(k1, "1", []).status_code, 401)
        self.assertEqual(cl(adm).post(f"/api/mt5req/admin/{rid}/set", json={"action": "wipe"}).status_code, 200)
        self.assertIsNone(q("SELECT pw_enc FROM mt5_requests WHERE id=?", rid)[0]["pw_enc"])
        self.assertEqual(cl(adm).post(f"/api/mt5req/admin/{rid}/set", json={"action": "nope"}).status_code, 422)
        self.assertEqual(eareport(k2, "140374097", []).status_code, 200)


class StorageTests(unittest.TestCase):
    def test_the_password_is_nowhere_in_the_database_file_in_clear(self):
        u = mk_user()
        secret = "UniqueSecret#9172xQ"
        self.assertEqual(submit(u, DET("555555", secret)).status_code, 200)
        raw = (Path(dbx.SQLITE_PATH)).read_bytes()
        self.assertNotIn(secret.encode(), raw)


if __name__ == "__main__":
    unittest.main(verbosity=2)
