"""Reference model of the EA's equity-protection decision rules (DriverlineCompanion.mq5, ProtectPositions), for Volatility 25
(1 lot = 1.0 money per price unit). It is a MODEL, not the EA itself: it pins the behaviour that went wrong in the first demo trial
(two trades with no stop got a correct protective stop and were then closed) and the rules that replace it. Keep it in step with the EA.
Run: python3 tests/test_protect_logic.py"""
import math
import unittest

TICK = 0.001
BALANCE, VOL, OPEN = 1159.04, 0.5, 2832.882


def protective_distance(vol, target):
    return math.floor((target / vol) / TICK) * TICK          # rounded DOWN so the risk never exceeds the target


def cycle_v154(balance, equity, open_, sl, vol, price, tp_near):
    """EA 1.54: size = min(balance, equity), exact cap, the stop check also looked at the take-profit, and any failure closed the trade."""
    size = min(balance, equity)
    if sl is not None and vol * (open_ - sl) <= size * 4.5 / 100 + 1e-7:
        return "ok", sl
    new = round(open_ - protective_distance(vol, size * 4.5 / 100), 3)
    placeable = (sl is None or new > sl) and price - new > 0 and not tp_near
    if placeable:
        return ("set" if sl is None else "tightened"), new
    return "closed", sl


def cycle_v155(balance, open_, sl, vol, price, tp_near):
    """EA 1.55: size = balance, 5% tolerance, only the stop is checked, an existing stop is never a reason to close."""
    size = balance
    if sl is not None and vol * (open_ - sl) <= size * 4.5 / 100 * 1.05 + 1e-7:
        return "ok", sl
    new = round(open_ - protective_distance(vol, size * 4.5 / 100), 3)
    if sl is not None and new <= sl:
        return "ok", sl
    if price - new > 0:
        return ("set" if sl is None else "tightened"), new
    return ("closed" if sl is None else "failed"), sl         # a trade that has its own stop is left open and the member is told


def run154(prices, tp_near_from):
    sl, out = None, []
    for i, price in enumerate(prices):
        what, sl = cycle_v154(BALANCE, BALANCE + VOL * (price - OPEN), OPEN, sl, VOL, price, i >= tp_near_from)
        out.append(what)
        if what == "closed":
            break
    return out


def run155(prices, tp_near_from):
    sl, out = None, []
    for i, price in enumerate(prices):
        what, sl = cycle_v155(BALANCE, OPEN, sl, VOL, price, i >= tp_near_from)
        out.append(what)
        if what in ("closed", "failed"):
            break
    return out


class Model(unittest.TestCase):
    DRIFT = (2832.50, 2832.30, 2832.10, 2831.90, 2831.70)           # price drifts a little against the trade, far from the stop

    def test_old_rules_closed_a_healthy_trade_once_the_take_profit_check_failed(self):
        self.assertEqual(run154(self.DRIFT, tp_near_from=2), ["set", "tightened", "closed"])

    def test_old_rules_re_tightened_the_stop_every_time_equity_moved(self):
        self.assertEqual(run154(self.DRIFT, tp_near_from=99)[:3], ["set", "tightened", "tightened"])

    def test_new_rules_set_one_stop_and_leave_the_trade_alone(self):
        self.assertEqual(run155(self.DRIFT, tp_near_from=99), ["set"] + ["ok"] * 4)
        self.assertEqual(run155(self.DRIFT, tp_near_from=2), ["set"] + ["ok"] * 4)       # a take-profit near the price no longer matters

    def test_the_protective_stop_risks_no_more_than_the_target(self):
        d = protective_distance(VOL, BALANCE * 4.5 / 100)
        self.assertLessEqual(VOL * d, BALANCE * 0.045)
        self.assertGreater(VOL * d, BALANCE * 0.045 * 0.999)

    def test_a_really_oversized_stop_is_still_tightened(self):
        what, sl = cycle_v155(BALANCE, OPEN, OPEN - 500, VOL, OPEN - 1, False)       # stop 500 away: risks 250 = 21%
        self.assertEqual(what, "tightened")
        self.assertGreater(sl, OPEN - 500)

    def test_a_stop_within_the_limit_is_left_alone(self):
        self.assertEqual(cycle_v155(BALANCE, OPEN, OPEN - 100, VOL, OPEN - 1, False)[0], "ok")   # risks 50 = 4.3%

    def test_no_stop_and_none_can_be_placed_means_close(self):
        self.assertEqual(cycle_v155(BALANCE, OPEN, None, VOL, OPEN - 200, False)[0], "closed")

    def test_a_trade_with_its_own_big_stop_that_cannot_be_tightened_is_not_closed(self):
        self.assertEqual(cycle_v155(BALANCE, OPEN, OPEN - 500, VOL, OPEN - 300, False)[0], "failed")


if __name__ == "__main__":
    unittest.main(verbosity=1)
