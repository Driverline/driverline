# Deriv options (Rise/Fall and Accumulators)

Additive feature. Multipliers (`deriv_trade.py`) are untouched; this module only reuses its connection helpers.

## Switch on
1. Set `DERIV_OPTIONS=1` on Render and redeploy. Nothing is visible until then.
2. Sign in as admin, open the new **Options** tile, and in the Admin card choose who sees it (default: admin + testers) and who may run AutoTrading (admin + testers only; there is no public AutoTrading).
3. Press **Run backtest on Deriv history**. Real-money trades stay blocked until a strategy has a stored result whose lower bound beats break-even at the live payout.
4. Test on a demo account first.

## Files
`app/options_logic.py` (rules, no I/O) · `app/deriv_options.py` (storage, Deriv calls, routes, AutoTrading loop) · `static/options.html` · `migrations/2026_11_deriv_options.sql` · tests `tests/test_options_logic.py`, `tests/test_deriv_options.py`.
Existing files touched (additive lines only): `app/main.py` (import, routes, loop), `app/deriv_trade.py` (callback returns to Options when the member connected from there), `static/auth.js` (Options tab), `static/index.html` (tile icon and caption).

## Rules enforced on the server
Daily loss limit (open trades counted at full stake), daily trade cap, open-trade cap, pause after a losing run, session stop loss and target, largest stake, one trade placed at a time. Martingale is opt-in with an acknowledgement; its chain (multiplier 1.5-3, up to 5 steps) must fit inside the largest stake, the daily loss limit and 20% of the balance, and it runs one trade at a time. After the last step the chain restarts at the base stake.

## Limits to know
AutoTrading runs only while the member's Deriv connection lives (about an hour). Contract parameters (Rise/Fall duration, Accumulator growth rate and take profit) follow Deriv's documented fields but have not been exercised against the live API from the build environment: run demo first and read any Deriv refusal shown in the log.
