"""Tests: every MT5 account starts protected (risk rules, auto-manage, equity protection) and can switch it off. Run: python3 tests/test_default_protection.py"""
import json
import sys
import unittest
from contextlib import closing
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_copy_trading as T                                          # noqa: E402
from test_copy_trading import add, cl, eareport, mk_user, q, ea        # noqa: E402
from app import inbox                                                  # noqa: E402


def bodies(uid):
    with closing(inbox.db()) as c:
        return [r["body"] for r in c.execute("SELECT body FROM inbox_messages WHERE user_id=? ORDER BY id", (uid,)).fetchall()]


class Defaults(unittest.TestCase):
    def test_a_new_account_has_the_protected_defaults(self):
        u = mk_user()
        a, k = add(u, "New")
        with closing(ea.db()) as con:
            self.assertEqual(ea.get_rules(con, a), {"max_risk_pct": 4.5, "daily_loss_pct": 10.0, "max_positions": 3})
            m = ea.get_manage(con, a)
        self.assertEqual((m["be_on"], m["be_trigger"], m["be_offset"]), (True, 1.0, 0.1))
        self.assertEqual((m["tsl_on"], m["tsl_start"], m["tsl_dist"]), (True, 1.5, 1.0))
        self.assertEqual((m["ttp_on"], m["ttp_near"], m["ttp_step"]), (True, 0.3, 1.0))
        self.assertEqual((m["prot_mode"], m["prot_risk_pct"], m["prot_cap_pct"], m["prot_grace"], m["prot_be_pct"]), ("stop", 4.5, 4.5, 45, 1.0))

    def test_the_member_is_told_once_when_the_account_first_connects(self):
        u = mk_user()
        a, k = add(u, "Fresh")
        eareport(k, "FR1", [], 100.0)
        eareport(k, "FR1", [], 100.0)
        msgs = [m for m in bodies(u) if "protected by default" in m]
        self.assertEqual(len(msgs), 1)
        for word in ("4.5%", "10%", "breakeven", "Equity protection", "switch off"):
            self.assertIn(word, msgs[0])

    def test_members_can_still_change_and_switch_things_off(self):
        u = mk_user()
        a, k = add(u, "Mine")
        c = cl(u)
        self.assertEqual(c.post("/api/ea/rules", json={"account": a, "max_risk_pct": 2, "daily_loss_pct": 5, "max_positions": 2}).status_code, 200)
        self.assertEqual(c.post("/api/ea/manage", json={"account": a, "be_on": False, "tsl_on": False, "ttp_on": False}).status_code, 200)
        with closing(ea.db()) as con:
            self.assertEqual(ea.get_rules(con, a)["max_risk_pct"], 2)
            m = ea.get_manage(con, a)
        self.assertEqual((m["be_on"], m["tsl_on"], m["ttp_on"]), (False, False, False))
        r = c.post("/api/ea/manage", json={"account": a, "prot_mode": "off"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["prot_mode"], "stop")                           # switching equity protection off still waits 24 hours (existing safeguard)

    def test_old_untouched_accounts_move_to_the_new_defaults_and_customised_ones_do_not(self):
        untouched, custom, rulesonly = mk_user(), mk_user(), mk_user()
        ua, _ = add(untouched, "U")
        ca, _ = add(custom, "C")
        ra, _ = add(rulesonly, "R")
        old_manage = json.dumps(ea.OLD_MANAGE_DEFAULT)
        for aid in (ua, ca, ra):
            q("DELETE FROM ea_acct_rules WHERE account_id=?", aid)
            q("DELETE FROM ea_acct_manage WHERE account_id=?", aid)
        q("INSERT INTO ea_acct_rules(account_id,max_risk_pct,daily_loss_pct,max_positions) VALUES(?,1.0,3.0,3)", ua)
        q("INSERT INTO ea_acct_manage(account_id,settings) VALUES(?,?)", ua, old_manage)
        q("INSERT INTO ea_acct_rules(account_id,max_risk_pct,daily_loss_pct,max_positions) VALUES(?,10,20,3)", ca)                  # the member's own numbers
        q("INSERT INTO ea_acct_manage(account_id,settings) VALUES(?,?)", ca, json.dumps({**ea.OLD_MANAGE_DEFAULT, "be_on": True, "tsl_start": 2.0}))
        q("INSERT INTO ea_acct_rules(account_id,max_risk_pct,daily_loss_pct,max_positions) VALUES(?,1.0,3.0,3)", ra)
        q("DELETE FROM ea_meta WHERE k='defaults_protected_2026_11'")
        with closing(ea.db()) as con:
            n = ea.apply_default_protection(con)
            self.assertEqual(n, 2)
            self.assertEqual(ea.get_rules(con, ua), {"max_risk_pct": 4.5, "daily_loss_pct": 10.0, "max_positions": 3})
            self.assertTrue(ea.get_manage(con, ua)["tsl_on"] and ea.get_manage(con, ua)["prot_mode"] == "stop")
            self.assertEqual(ea.get_rules(con, ca)["max_risk_pct"], 10)
            mc = ea.get_manage(con, ca)
            self.assertEqual((mc["be_on"], mc["tsl_start"], mc["tsl_on"], mc["prot_mode"]), (True, 2.0, False, "off"))                  # untouched by the change
            self.assertEqual(ea.get_rules(con, ra)["max_risk_pct"], 4.5)
            self.assertEqual(ea.apply_default_protection(con), 0)                                                                       # only once
        self.assertEqual(len([m for m in bodies(untouched) if "protected by default" in m]), 1)
        self.assertEqual([m for m in bodies(custom) if "protected by default" in m], [])

    def test_copy_members_get_the_higher_default_limit(self):
        u = mk_user()
        T.SUBS(u)
        a, k = add(u, "Copy")
        eareport(k, "CP1", [], 100.0)
        with closing(ea.db()) as con:
            self.assertEqual(T.CT.effective_limit(con, a), 4.5)


if __name__ == "__main__":
    unittest.main()
