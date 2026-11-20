"""Tests for the pure Deriv-options rules (app/options_logic.py). Run: python3 tests/test_options_logic.py (no network, no database)."""
import random
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app import options_logic as L  # noqa: E402

OK = dict(strategy="trend", symbols=["v100"], duration=3, growth_rate=0.01, accu_tp=1, stake_mode="fixed", stake=1, risk_pct=1,
          max_stake=10, session_tp=5, session_sl=5, daily_loss=10, max_per_day=20, max_open=1, cooldown_losses=4,
          martingale=False, mg_mult=2, mg_steps=3, ack_martingale=False)


def walk(n, seed=1):
    r, p, out = random.Random(seed), 1000.0, []
    for _ in range(n):
        p += r.gauss(0, 1)
        out.append(p)
    return out


class Logic(unittest.TestCase):
    def test_breakeven_and_wilson(self):
        self.assertAlmostEqual(L.breakeven(0.85), 1 / 1.85)
        self.assertLess(L.wilson_lower(60, 100), 0.6)
        self.assertEqual(L.wilson_lower(0, 0), 0.0)
        self.assertAlmostEqual(L.ratio_from_quote(1, 1.85), 0.85)
        self.assertEqual(L.ratio_from_quote(1, 0.9), 0.0)

    def test_signals_use_only_the_past(self):
        c = walk(600)
        for s in ("trend", "rsi", "calm"):
            full = L.signal_series(s, c)
            self.assertEqual(full[:400], L.signal_series(s, c[:400])[:400])

    def test_random_walk_has_no_edge(self):
        c = walk(20000, 7)
        for s in ("trend", "rsi"):
            r = L.backtest(s, c, 3, 0.85)
            self.assertGreater(r["n"], L.MIN_N)
            self.assertFalse(r["edge"], r)

    def test_edge_requires_proof(self):
        self.assertFalse(L.edge_ok(None, 0.85))
        self.assertFalse(L.edge_ok({"n": 50, "lower": 0.9}, 0.85))
        self.assertTrue(L.edge_ok({"n": 500, "lower": 0.60}, 0.85))
        self.assertFalse(L.edge_ok({"n": 500, "lower": 0.60}, 0.40))   # payout fell: break-even rose above the proven rate
        with self.assertRaises(ValueError):
            L.backtest("calm", [1.0] * 500, 3, 0.85)

    def test_stakes_and_martingale(self):
        cfg = L.clean_settings({**OK, "martingale": True, "ack_martingale": True, "stake": 1}, balance=500)
        self.assertEqual([L.stake_for(cfg, 500, k) for k in range(3)], [1.0, 2.0, 4.0])
        loss = lambda step: {"profit": -1.0, "step": step}
        self.assertEqual(L.next_step(cfg, None), 0)
        self.assertEqual(L.next_step(cfg, loss(0)), 1)
        self.assertEqual(L.next_step(cfg, loss(1)), 2)
        self.assertEqual(L.next_step(cfg, loss(2)), 0)            # chain used up: back to the base stake
        self.assertEqual(L.next_step(cfg, {"profit": 0.8, "step": 2}), 0)
        plain = L.clean_settings(OK, balance=500)
        self.assertEqual(L.next_step(plain, loss(0)), 0)
        self.assertEqual(L.stake_for(plain, 500, 0), 1.0)

    def test_pct_stake_is_clamped(self):
        cfg = L.clean_settings({**OK, "stake_mode": "pct", "risk_pct": 2, "max_stake": 5}, balance=1000)
        self.assertEqual(L.base_stake(cfg, 1000), 5.0)
        self.assertEqual(L.base_stake(cfg, 10), L.MIN_STAKE)

    def test_settings_refuse_dangerous_chains(self):
        mg = {**OK, "martingale": True, "ack_martingale": True}
        with self.assertRaises(ValueError):
            L.clean_settings({**mg, "ack_martingale": False}, 500)
        with self.assertRaises(ValueError):
            L.clean_settings({**mg, "mg_steps": 5, "stake": 1, "max_stake": 10, "daily_loss": 10}, 500)   # 1+2+4+8+16 > 10
        with self.assertRaises(ValueError):
            L.clean_settings({**mg, "max_open": 2}, 500)
        with self.assertRaises(ValueError):
            L.clean_settings({**mg, "mg_mult": 3, "mg_steps": 4, "stake": 2, "max_stake": 20, "daily_loss": 200}, 100)  # >20% of balance
        with self.assertRaises(ValueError):
            L.clean_settings({**OK, "daily_loss": 80}, 100)
        with self.assertRaises(ValueError):
            L.clean_settings({**OK, "symbols": ["xauusd"]}, 100)
        with self.assertRaises(ValueError):
            L.clean_settings({**OK, "stake": "abc"}, 100)
        with self.assertRaises(ValueError):
            L.clean_settings({**OK, "mg_mult": 10, "martingale": True, "ack_martingale": True}, 500)

    def test_risk_block(self):
        cfg = L.clean_settings(OK, 500)
        st = dict(today_pnl=0, open_stake=0, today_n=0, open_n=0, open_symbols=[], consec_losses=0, last_close=None, session_pnl=None)
        self.assertIsNone(L.risk_block(cfg, st, 1000))
        self.assertIn("Daily loss", L.risk_block(cfg, {**st, "today_pnl": -9, "open_stake": 1}, 1000))   # open stake counts
        self.assertIn("trade cap", L.risk_block(cfg, {**st, "today_n": 20}, 1000))
        self.assertIn("open trades", L.risk_block(cfg, {**st, "open_n": 1}, 1000))
        self.assertIn("Cooling", L.risk_block(cfg, {**st, "consec_losses": 4, "last_close": 900}, 1000))
        self.assertIsNone(L.risk_block(cfg, {**st, "consec_losses": 4, "last_close": 1000 - L.COOLDOWN_SECS - 1}, 1000))

    def test_session_end(self):
        cfg = L.clean_settings(OK, 500)
        self.assertIsNone(L.session_end_reason(cfg, None))
        self.assertIsNone(L.session_end_reason(cfg, 1.0))
        self.assertEqual(L.session_end_reason(cfg, 5.0), "target")
        self.assertEqual(L.session_end_reason(cfg, -5.0), "stop")


if __name__ == "__main__":
    unittest.main()
