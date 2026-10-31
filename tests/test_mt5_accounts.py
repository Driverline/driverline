"""Tests for several MT5 accounts per member (app/ea.py, plus the trade and pending-order routes that act on one account).
Run: python3 tests/test_mt5_accounts.py   (own process, temp SQLite database, no network, no broker)."""
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

from app import auth, credits, dbx, ea, pending_orders, signal_feed     # noqa: E402
from starlette.applications import Starlette                           # noqa: E402
from starlette.exceptions import HTTPException                         # noqa: E402
from starlette.responses import JSONResponse                           # noqa: E402
from starlette.testclient import TestClient                            # noqa: E402

TMP = Path(tempfile.mkdtemp())
dbx.SQLITE_PATH = TMP / "t.db"


async def _err(request, exc):
    return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)


APP = Starlette(routes=[*ea.routes, *signal_feed.routes, *pending_orders.routes, *credits.routes], exception_handlers={HTTPException: _err})
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


def eareport(key, login="1001", demo=True, positions=0, balance=1000.0, **flags):
    ea._last_report.clear()
    acc = {"login": login, "server": "Deriv-Demo" if demo else "Deriv-Live", "currency": "USD", "balance": balance, "equity": balance, "demo": demo,
           "remote_close": True, "manage_allowed": True, "execute_allowed": True, "pending_allowed": True, **flags}
    body = {"account": acc, "today": {"closed_pnl": 0}, "positions": [{"ticket": 500 + i, "symbol": "EURUSDm", "type": "BUY", "volume": 0.1, "profit": 0, "sl": 1, "risk": 1}
                                                                 for i in range(positions)], "orders": [], "specs": SPECS}
    return ANON.post("/api/ea/report", json=body, headers={"x-driverline-key": key})


def eapoll(key):
    ea._last_poll.clear()
    return ANON.post("/api/ea/poll", json={}, headers={"x-driverline-key": key})


class Two:
    """A member with a Demo and a Live account, both connected."""
    def setUp(self):
        self.u = mk_user()
        self.demo, self.kd = add(self.u, "Demo")
        self.live, self.kl = add(self.u, "Live")
        assert eareport(self.kd, "1001", True, positions=1).status_code == 200
        assert eareport(self.kl, "2002", False, positions=2, balance=5000.0).status_code == 200


class AccountTests(unittest.TestCase):
    def test_create_list_limit_names_and_removal(self):
        u = mk_user()
        a, ka = add(u, "Demo")
        b, kb = add(u, "Live")
        c, kc = add(u, "  Swap  Free \x00")
        self.assertEqual(len({ka, kb, kc}), 3)
        r = cl(u).post("/api/ea/accounts", json={"label": "Fourth"})
        self.assertEqual(r.status_code, 409)
        self.assertIn("up to 3", r.json()["detail"])
        self.assertEqual(cl(u).post("/api/ea/accounts", json={"label": "live"}).status_code, 409)             # no duplicate names, any case
        accs = cl(u).get("/api/ea/accounts").json()
        self.assertEqual([x["label"] for x in accs["accounts"]], ["Demo", "Live", "Swap Free"])
        self.assertEqual(accs["max"], 3)
        self.assertEqual(cl(u).post(f"/api/ea/accounts/{c}/rename", json={"label": "Live"}).status_code, 409)
        self.assertEqual(cl(u).post(f"/api/ea/accounts/{c}/rename", json={"label": "Swap-free"}).status_code, 200)
        self.assertEqual(cl(u).post("/api/ea/accounts", json={"label": ""}).status_code, 422)
        self.assertEqual(cl(u).post("/api/ea/accounts", json={}).status_code, 422)
        self.assertEqual(cl(u).delete(f"/api/ea/accounts/{b}").status_code, 200)
        self.assertEqual(len(cl(u).get("/api/ea/accounts").json()["accounts"]), 2)
        self.assertEqual(cl(u).post("/api/ea/accounts", json={"label": "Live again"}).status_code, 200)       # a slot is free again

    def test_keys_are_hashed_unique_and_shown_once(self):
        u = mk_user()
        a, k = add(u, "Demo")
        self.assertTrue(k.startswith("dlk_"))
        row = q("SELECT key_hash FROM ea_accounts WHERE id=?", a)[0]
        self.assertNotEqual(row["key_hash"], k)
        self.assertNotIn(k, json.dumps(cl(u).get("/api/ea/accounts").json()) + json.dumps(cl(u).get("/api/ea/status").json()))

    def test_replacing_a_key_stops_the_old_one_and_keeps_the_data(self):
        u = mk_user()
        a, k = add(u, "Demo")
        eareport(k, "1", True, positions=1)
        new = cl(u).post(f"/api/ea/accounts/{a}/key").json()["key"]
        self.assertEqual(eareport(k).status_code, 401)
        self.assertEqual(eareport(new, "1", True).status_code, 200)
        self.assertEqual(q("SELECT COUNT(*) AS c FROM ea_acct_state WHERE account_id=?", a)[0]["c"], 1)

    def test_removing_an_account_disconnects_it_and_deletes_only_its_data(self):
        u = mk_user()
        a, ka = add(u, "Demo")
        b, kb = add(u, "Live")
        eareport(ka, "1")
        eareport(kb, "2", False)
        cl(u).post("/api/ea/command", json={"kind": "close_all", "account": a})
        cl(u).delete(f"/api/ea/accounts/{a}")
        self.assertEqual(eareport(ka).status_code, 401)
        self.assertEqual(eapoll(ka).status_code, 401)
        for t in ("ea_acct_state", "ea_acct_rules", "ea_acct_manage"):
            self.assertEqual(q(f"SELECT * FROM {t} WHERE account_id=?", a), [], t)
        self.assertEqual(q("SELECT * FROM ea_commands WHERE account_id=?", a), [])
        self.assertEqual(eareport(kb, "2", False).status_code, 200)
        self.assertEqual(cl(u).get(f"/api/ea/status?account={b}").json()["account"]["login"], "2")

    def test_a_new_account_starts_with_the_members_existing_rules_and_auto_manage(self):
        u = mk_user()
        a, _ = add(u, "Demo")
        cl(u).post("/api/ea/rules", json={"account": a, "max_risk_pct": 0.5, "daily_loss_pct": 2, "max_positions": 2})
        cl(u).post("/api/ea/manage", json={"account": a, "be_on": True})
        b, _ = add(u, "Live")
        self.assertEqual(cl(u).get(f"/api/ea/status?account={b}").json()["rules"], {"max_risk_pct": 0.5, "daily_loss_pct": 2.0, "max_positions": 2})
        self.assertTrue(cl(u).get(f"/api/ea/status?account={b}").json()["manage"]["be_on"])

    def test_entitlement_still_decides_who_may_connect(self):
        u = mk_user("free")
        credits.billing_on = lambda: True
        try:
            self.assertEqual(cl(u).post("/api/ea/accounts", json={"label": "Demo"}).status_code, 403)
            self.assertEqual(cl(u).post("/api/ea/key").status_code, 403)
        finally:
            credits.billing_on = lambda: False.__class__(False)


class SeparationTests(Two, unittest.TestCase):
    def test_each_account_has_its_own_live_data(self):
        d = cl(self.u).get(f"/api/ea/status?account={self.demo}").json()
        l = cl(self.u).get(f"/api/ea/status?account={self.live}").json()
        self.assertEqual((d["account"]["login"], d["account"]["demo"], len(d["positions"]), d["label"]), ("1001", True, 1, "Demo"))
        self.assertEqual((l["account"]["login"], l["account"]["demo"], len(l["positions"]), l["label"]), ("2002", False, 2, "Live"))
        self.assertEqual(l["account"]["balance"], 5000.0)
        self.assertEqual([a["label"] for a in d["accounts"]], ["Demo", "Live"])
        self.assertTrue(all(a["connected"] for a in d["accounts"]))

    def test_a_plain_status_read_returns_the_connected_account_and_never_mixes_them(self):
        s = cl(self.u).get("/api/ea/status").json()
        self.assertIn(s["account_id"], (self.demo, self.live))
        self.assertEqual(len(s["accounts"]), 2)
        q("UPDATE ea_accounts SET last_seen=? WHERE id=?", int(time.time()) - 900, self.demo)
        self.assertEqual(cl(self.u).get("/api/ea/status").json()["account_id"], self.live)

    def test_rules_and_auto_manage_are_per_account(self):
        cl(self.u).post("/api/ea/rules", json={"account": self.live, "max_risk_pct": 0.3, "daily_loss_pct": 1, "max_positions": 1})
        cl(self.u).post("/api/ea/manage", json={"account": self.live, "tsl_on": True, "tsl_start": 2, "tsl_dist": 1})
        d = cl(self.u).get(f"/api/ea/status?account={self.demo}").json()
        l = cl(self.u).get(f"/api/ea/status?account={self.live}").json()
        self.assertEqual((d["rules"]["max_risk_pct"], d["manage"]["tsl_on"]), (1.0, False))
        self.assertEqual((l["rules"]["max_risk_pct"], l["manage"]["tsl_on"], l["rules"]["max_positions"]), (0.3, True, 1))
        self.assertEqual(l["check"]["status"], "WARN")                                  # 2 positions over the Live account's own maximum of 1
        self.assertEqual(d["check"]["status"], "OK")

    def test_each_ea_only_sees_its_own_commands_and_settings(self):
        cl(self.u).post("/api/ea/manage", json={"account": self.live, "be_on": True})
        r = cl(self.u).post("/api/ea/command", json={"kind": "close_all", "account": self.live})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(eapoll(self.kd).json()["cmd"], "")                              # the Demo EA is not told to close anything
        p = eapoll(self.kl).json()
        self.assertEqual((p["cmd"], p["be_on"]), ("close_all", "1"))
        self.assertEqual(eapoll(self.kd).json()["be_on"], "0")
        cid = p["id"]
        ANON.post("/api/ea/result", json={"id": cid, "ok": True, "closed": 2}, headers={"x-driverline-key": self.kd})     # the wrong EA answers
        self.assertEqual(q("SELECT status FROM ea_commands WHERE id=?", cid)[0]["status"], "sent")
        ANON.post("/api/ea/result", json={"id": cid, "ok": True, "closed": 2}, headers={"x-driverline-key": self.kl})
        self.assertEqual(q("SELECT status FROM ea_commands WHERE id=?", cid)[0]["status"], "done")

    def test_a_close_in_progress_on_one_account_does_not_block_the_other(self):
        self.assertEqual(cl(self.u).post("/api/ea/command", json={"kind": "close_all", "account": self.live}).status_code, 200)
        self.assertEqual(cl(self.u).post("/api/ea/command", json={"kind": "close_all", "account": self.live}).status_code, 409)
        self.assertEqual(cl(self.u).post("/api/ea/command", json={"kind": "close", "ticket": 500, "account": self.demo}).status_code, 200)

    def test_a_ticket_belongs_to_its_own_account(self):
        self.assertEqual(cl(self.u).post("/api/ea/command", json={"kind": "close", "ticket": 501, "account": self.demo}).status_code, 404)   # only Live has 501
        self.assertEqual(cl(self.u).post("/api/ea/command", json={"kind": "close", "ticket": 501, "account": self.live}).status_code, 200)

    def test_two_terminals_on_one_key_are_refused_instead_of_mixing_data(self):
        r = eareport(self.kd, "9999", True)
        self.assertEqual(r.status_code, 409)
        self.assertIn("separate key", r.json()["detail"])
        self.assertEqual(cl(self.u).get(f"/api/ea/status?account={self.demo}").json()["account"]["login"], "1001")
        q("UPDATE ea_accounts SET last_seen=? WHERE id=?", int(time.time()) - 600, self.demo)                 # the old terminal is long gone
        self.assertEqual(eareport(self.kd, "9999", True).status_code, 200)
        self.assertEqual(cl(self.u).get(f"/api/ea/status?account={self.demo}").json()["account"]["login"], "9999")

    def test_specs_are_per_account(self):
        q("UPDATE ea_acct_state SET specs=? WHERE account_id=?", json.dumps([{"symbol": "XAUUSD.sf"}]), self.live)
        self.assertEqual([x["symbol"] for x in cl(self.u).get(f"/api/ea/specs?account={self.live}").json()], ["XAUUSD.sf"])
        self.assertEqual([x["symbol"] for x in cl(self.u).get(f"/api/ea/specs?account={self.demo}").json()], ["EURUSDm"])


class ExplicitAccountTests(Two, unittest.TestCase):
    """With more than one account, nothing that acts on an account may guess which one."""
    def test_actions_without_an_account_are_refused_and_change_nothing(self):
        c = cl(self.u)
        for method, path, body in (("post", "/api/ea/command", {"kind": "close_all"}), ("post", "/api/ea/rules", {"max_risk_pct": 1, "daily_loss_pct": 3, "max_positions": 3}),
                                   ("post", "/api/ea/manage", {"be_on": True}), ("post", "/api/ea/manage/exclude", {"ticket": 500, "excluded": True}),
                                   ("get", "/api/ea/manage", None), ("post", "/api/ea/key", None)):
            r = getattr(c, method)(path, **({"json": body} if body is not None else {}))
            self.assertEqual(r.status_code, 422, (path, r.text))
            self.assertIn("more than one MT5 account", r.json()["detail"])
        self.assertEqual(q("SELECT * FROM ea_commands WHERE user_id=?", self.u), [])
        self.assertEqual(c.delete("/api/ea/key").status_code, 422)
        self.assertEqual(len(c.get("/api/ea/accounts").json()["accounts"]), 2)

    def test_pending_order_and_one_click_trade_need_an_account_and_go_to_exactly_that_one(self):
        tok = pending_orders.make_token({"t": "BUY_LIMIT", "k": "eurusd", "p": 1.1, "s": 1.09, "x": 1.13, "r": 1.1, "m": 0.001, "l": 0, "i": time.time()})
        r = cl(self.u).post("/api/pending/place", json={"token": tok, "lots": 0.1, "hours": 8})
        self.assertEqual(r.status_code, 422, r.text)
        self.assertEqual(q("SELECT * FROM ea_commands WHERE user_id=?", self.u), [])
        r = cl(self.u).post("/api/pending/place", json={"token": tok, "lots": 0.1, "hours": 8, "account": self.live})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual((r.json()["account"], r.json()["account_label"]), (self.live, "Live"))
        cmds = q("SELECT account_id,kind FROM ea_commands WHERE user_id=?", self.u)
        self.assertEqual([(c["account_id"], c["kind"]) for c in cmds], [(self.live, "pending_order")])
        self.assertEqual(eapoll(self.kd).json()["cmd"], "")
        self.assertEqual(eapoll(self.kl).json()["cmd"], "pending_order")

    def test_execute_signal_needs_an_account_and_the_same_signal_can_go_to_each_account_once(self):
        now = int(time.time())
        with closing(signal_feed.db()) as con:
            sid = con.insert("INSERT INTO signals(created_at,bar_epoch,instrument,style,setup,direction,entry,stop,target,rr,risk,status,expires_at) "
                             "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", (now, now, "eurusd", "day", "ema_pullback", 1, 1.1, 1.09, 1.12, 2.0, "normal", "open", now + 3600))
            con.execute("INSERT INTO signal_opens(user_id,signal_id,day,created_at,counted) VALUES(?,?,?,?,1)", (self.u, sid, "2026-10-30", now))
            con.commit()
        url = f"/api/signals/{sid}/execute"
        r = cl(self.u).post(url, json={"lots": 0.1})
        self.assertEqual(r.status_code, 422, r.text)
        ok = cl(self.u).post(url, json={"lots": 0.1, "account": self.demo})
        self.assertEqual(ok.status_code, 200, ok.text)
        self.assertEqual(ok.json()["account_label"], "Demo")
        self.assertEqual(cl(self.u).post(url, json={"lots": 0.1, "account": self.demo}).status_code, 409)       # not twice on Demo
        eapoll(self.kd)                                                                                           # the Demo EA collects it
        again = cl(self.u).post(url, json={"lots": 0.1, "account": self.live})
        self.assertEqual(again.status_code, 200, again.text)                                                      # but Live is a different account
        self.assertEqual(sorted(c["account_id"] for c in q("SELECT account_id FROM ea_commands WHERE kind='open'")[-2:]), sorted([self.demo, self.live]))

    def test_a_single_account_member_needs_to_say_nothing(self):
        u = mk_user()
        a, k = add(u, "Main")
        eareport(k, "7", True, positions=1)
        c = cl(u)
        self.assertEqual(c.post("/api/ea/command", json={"kind": "close_all"}).status_code, 200)
        self.assertEqual(c.post("/api/ea/rules", json={"max_risk_pct": 1, "daily_loss_pct": 3, "max_positions": 3}).status_code, 200)
        self.assertEqual(c.get("/api/ea/status").json()["account_id"], a)


class IsolationTests(Two, unittest.TestCase):
    def test_another_member_can_never_reach_these_accounts(self):
        x = mk_user()
        c = cl(x)
        for method, path, body in (("get", f"/api/ea/status?account={self.live}", None), ("get", f"/api/ea/specs?account={self.live}", None),
                                   ("post", "/api/ea/command", {"kind": "close_all", "account": self.live}),
                                   ("post", "/api/ea/rules", {"account": self.live, "max_risk_pct": 1, "daily_loss_pct": 3, "max_positions": 3}),
                                   ("post", "/api/ea/manage", {"account": self.live, "be_on": True}),
                                   ("get", f"/api/ea/manage?account={self.live}", None),
                                   ("post", f"/api/ea/accounts/{self.live}/key", None), ("post", f"/api/ea/accounts/{self.live}/rename", {"label": "x"}),
                                   ("delete", f"/api/ea/accounts/{self.live}", None)):
            r = getattr(c, method)(path, **({"json": body} if body is not None else {}))
            self.assertEqual(r.status_code, 404, (path, r.status_code, r.text))
        self.assertEqual(c.get("/api/ea/accounts").json()["accounts"], [])
        self.assertEqual(cl(self.u).get(f"/api/ea/status?account={self.live}").json()["account"]["login"], "2002")      # untouched
        self.assertEqual(ANON.get("/api/ea/accounts").status_code, 401)
        self.assertEqual(ANON.post("/api/ea/accounts", json={"label": "x"}).status_code, 401)

    def test_a_bad_account_value_is_rejected(self):
        for bad in ("abc", "1.5", "-1", "999999"):
            r = cl(self.u).get(f"/api/ea/status?account={bad}")
            self.assertIn(r.status_code, (404, 422), bad)

    def test_a_blocked_member_cannot_report(self):
        q("UPDATE users SET blocked=1 WHERE id=?", self.u)
        try:
            self.assertEqual(eareport(self.kd).status_code, 401)
        finally:
            q("UPDATE users SET blocked=0 WHERE id=?", self.u)


class MigrationTests(unittest.TestCase):
    def _again(self):
        dbx._ready.discard(("ea", str(dbx.SQLITE_PATH)))      # the next connection runs the migration like a freshly started server

    def test_an_existing_connection_becomes_the_first_account_and_keeps_working(self):
        u = mk_user()
        key = "dlk_legacy_key_for_test_0123456789"
        now = int(time.time())
        state = {"account": {"login": "555", "server": "Deriv-Live", "demo": False, "balance": 321.0, "currency": "USD", "remote_close": True},
                 "today": {}, "positions": [], "orders": []}
        with closing(ea.db()) as con:
            con.execute("DELETE FROM ea_meta WHERE k='accounts_backfilled'")
            con.execute("INSERT INTO ea_keys(user_id,key_hash,created_at,last_seen) VALUES(?,?,?,?)", (u, ea._hash(key), now, now))
            con.execute("INSERT INTO ea_state(user_id,state,specs,updated_at) VALUES(?,?,?,?)", (u, json.dumps(state), json.dumps(SPECS), now))
            con.execute("INSERT INTO ea_rules(user_id,max_risk_pct,daily_loss_pct,max_positions) VALUES(?,?,?,?)", (u, 0.7, 2.5, 4))
            con.execute("INSERT INTO ea_manage(user_id,settings) VALUES(?,?)", (u, json.dumps({"be_on": True, "be_trigger": 1.2})))
            con.commit()
        self._again()
        s = cl(u).get("/api/ea/status").json()
        self.assertEqual((s["label"], s["account"]["login"], s["account"]["balance"], s["connected"]), ("Main", "555", 321.0, True))
        self.assertEqual((s["rules"]["max_risk_pct"], s["rules"]["max_positions"], s["manage"]["be_on"], s["manage"]["be_trigger"]), (0.7, 4, True, 1.2))
        self.assertEqual(eareport(key, "555", False).status_code, 200)               # the EA keeps using the SAME key, untouched
        self.assertEqual(eapoll(key).status_code, 200)
        self.assertEqual(len(q("SELECT * FROM ea_accounts WHERE user_id=?", u)), 1)
        self.assertEqual(len(q("SELECT * FROM ea_keys WHERE user_id=?", u)), 1)       # the old rows are kept as they were (rollback safety)

    def test_running_it_again_never_duplicates_or_resurrects_a_removed_account(self):
        u = mk_user()
        key = "dlk_legacy_key_two_0123456789abcdef"
        with closing(ea.db()) as con:
            con.execute("DELETE FROM ea_meta WHERE k='accounts_backfilled'")
            con.execute("INSERT INTO ea_keys(user_id,key_hash,created_at,last_seen) VALUES(?,?,?,?)", (u, ea._hash(key), int(time.time()), None))
            con.commit()
        self._again()
        (acc,) = q("SELECT id FROM ea_accounts WHERE user_id=?", u)
        self.assertEqual(cl(u).delete(f"/api/ea/accounts/{acc['id']}").status_code, 200)
        for _ in range(2):
            self._again()
            ea.db().close()
        self.assertEqual(q("SELECT * FROM ea_accounts WHERE user_id=?", u), [])
        self.assertEqual(eareport(key).status_code, 401)                              # the removed key stays dead
        self.assertEqual(q("SELECT COUNT(*) AS c FROM ea_meta WHERE k='accounts_backfilled'")[0]["c"], 1)

    def test_the_old_single_account_calls_still_work_for_a_cached_page(self):
        u = mk_user()
        self.assertFalse(cl(u).get("/api/ea/status").json()["has_key"])
        r = cl(u).post("/api/ea/key")
        self.assertEqual(r.status_code, 200)
        k1 = r.json()["key"]
        self.assertTrue(cl(u).get("/api/ea/status").json()["has_key"])
        k2 = cl(u).post("/api/ea/key").json()["key"]                                  # "create or replace"
        self.assertNotEqual(k1, k2)
        self.assertEqual(eareport(k1).status_code, 401)
        self.assertEqual(eareport(k2).status_code, 200)
        self.assertEqual(len(q("SELECT * FROM ea_accounts WHERE user_id=?", u)), 1)
        self.assertEqual(cl(u).delete("/api/ea/key").status_code, 200)
        self.assertFalse(cl(u).get("/api/ea/status").json()["has_key"])

    def test_the_migration_file_is_additive_and_the_new_tables_exist(self):
        text = (ROOT / "migrations" / "2026_10_mt5_accounts.sql").read_text()
        body = "\n".join(l for l in text.splitlines() if not l.strip().startswith("--")).upper()
        for word in ("DROP ", "DELETE ", "TRUNCATE", "UPDATE ", "RENAME"):
            self.assertNotIn(word, body, word)
        for t in ("ea_accounts", "ea_acct_state", "ea_acct_rules", "ea_acct_manage", "ea_meta"):
            self.assertIn(t.upper(), body)
            self.assertEqual(q(f"SELECT COUNT(*) AS c FROM {t}")[0]["c"] >= 0, True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
