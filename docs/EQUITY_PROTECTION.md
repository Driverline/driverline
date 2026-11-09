# Equity protection (trades with no stop loss, or an oversized one)

Status: stage 1 (server, app screen, event log) LIVE. Stage 2 (MT5 companion EA 1.54 with `InpAllowProtect`, and the Open-trade / pending-order pre-check) written, NOT yet compiled or trialled: needs a Deriv demo account trial before any member uses it.

## Problem
Auto-manage (breakeven, trailing) skips a trade with no stop loss, because there is no 1R to measure from. A stop that risks most of the
account is just as dangerous. Both blow accounts.

## What the member chooses (per MT5 account, default OFF)
| Setting | Default | Range |
|---|---|---|
| `prot_mode` | off | off, stop (recommended), close, breakeven |
| `prot_risk_pct` what a protective stop risks, % of account | 4.5 (so small accounts can trade Volatility 10, 25 and 50 at the minimum lot) | 0.25 to 10 |
| `prot_cap_pct` a stop riskier than this is "unreasonable", % of account | 4.5 (same as the protective risk, so a trade risking 5.0% on a $10 account is refused) | 0.5 to 25 (not below risk) |
| `prot_grace` seconds to wait after the trade opened | 45 | 10 to 300 |
| `prot_be_pct` profit (% of account) that triggers breakeven in breakeven mode | 1 | 0.25 to 10 |

Modes: **stop** sets a stop that risks `prot_risk_pct` (or tightens an oversized one); **close** closes the trade; **breakeven** moves the stop
to entry once profit reaches `prot_be_pct` (does NOT limit the loss, labelled as such). A trade is "in scope" when it has no stop, or its
stop risks more than `prot_cap_pct`. Trades switched off with the per-trade Auto button are never touched.

## Commitment device
Stricter settings apply at once. Anything that protects LESS (mode switched down or off, bigger risk, cap, wait or breakeven threshold) is
queued for 24 hours (`prot_pending`, per field). Saving the current value again cancels a queued change. While protection is off, numbers apply at once.

## Server (stage 1, built)
- Settings live in the existing `ea_acct_manage` JSON (additive, merged over defaults, no migration for existing rows).
- New table `ea_protect_log(account_id, ticket, action, ...)`, unique per account, ticket and action. Created by the app at start; RLS is enabled by `dbx`.
- The EA reports what it did in `protect_events` inside the normal `/api/ea/report` call. Each new event writes one inbox message. Text is built on the
  server from a fixed list of actions; free text from the EA is stripped and cut.
- Account flag `protect_allowed` (reported by the EA) drives `can_protect` in `/api/ea/status`; the screen is disabled until the EA allows it.

## EA algorithm (stage 2, to build and test on a Deriv demo account first)
New input `InpAllowProtect = false` (separate from `InpAllowManage`, so nobody gets auto-close by accident). Runs on each timer cycle, in its own
function, independent of the breakeven/trailing switches.
For each open position that is not excluded and older than `prot_grace`:
1. Account size = the BALANCE (not equity: equity moves with the trade's own floating loss, which made an earlier version chase its own stop).
2. Risk at the stop = `-OrderCalcProfit(type, symbol, volume, open, sl)` as a % of account size. No stop = unlimited.
3. In scope if no stop, or risk % > `prot_cap_pct` plus a 5% tolerance for rounding.
4. stop mode: protective distance = distance whose loss equals `prot_risk_pct` of account size (scale a probe distance through OrderCalcProfit,
   round to tick size). New stop = open -/+ distance. Place it only if it is tighter than any existing stop (never widen), is on the safe side of the
   current price and respects the broker's stop and freeze levels (`StopsAllowed`). If the existing stop is already at or inside the protective level, do nothing (never close for that). Only if a protective stop truly cannot be placed (no stop and too close to the price, past it, or too small to price) close the trade and say why.
5. After a stop is set or tightened, overwrite the remembered 1R (`TL_R_<ticket>`) with the new distance, so breakeven and trailing work from it.
6. close mode: close at market. breakeven mode: when profit >= `prot_be_pct` of account size, move the stop to entry (only if tighter).
7. Retry a failed modify at most 3 times, then report `failed`. Remember handled tickets (`TL_P_<ticket>`) so each action is taken and reported once.
8. Report every action in `protect_events` (`stop_set`, `stop_tightened`, `closed_no_stop`, `closed_big_stop`, `closed_mode`, `breakeven`, `failed`).

## Risks and wording
- Limits risk, does not guarantee it: gaps and slippage can pass a stop. Education and risk analysis only.
- Closing is a new kind of action for the EA: opt-in, separate input, logged, and told to the member.
- Deriv synthetic indices can have large minimum stop distances; a protective stop may not be placeable on a small account with big lots, which is why stop mode closes in that case.
- The EA cannot be unit-tested here (no MT5). Server logic is tested in `tests/test_protection.py`. The EA needs a demo-account trial for: no stop, oversized stop, stop too close, trade already past the protective level, grace period, excluded trade, hedged positions, restart of the EA (stored GlobalVariables).

## Rollout
1. Stage 1 (this change): safe to deploy, nothing acts until the EA reports `protect_allowed`.
2. Stage 2: EA change, demo trial, then a small group of live members, then everyone.
3. Terms/privacy wording check before enabling for members.

## Default risk of 4.5% (decided by the owner)
The protective-stop risk defaults to 4.5% of the account, not 2%, so small accounts can trade the Volatility indices at their minimum lot:
a $10 account allows $0.45, enough for Volatility 50 (minimum about $0.25, 2.5%) but not Volatility 25 (about $0.50, 5%). Members therefore see
that some symbols are not tradeable with a small balance. The "unreasonable stop" limit also defaults to 4.5%, so the smallest Volatility 25 trade on a $10 account (5.0%) is refused.

## Pre-check (built with stage 2)
`ea.protect_precheck()` runs before the app queues an Open-trade (`signal_feed.execute_signal`) or a pending order (`pending_orders.place`). In stop or
close mode, and only when the EA reports `protect_allowed`, a trade whose stop would risk more than `prot_cap_pct` of the account (balance; empty lot size = the symbol's minimum lot) is refused with a 409 and a reason: the lot size that would fit, or the account size needed. It never blocks
when the contract specs are unknown. Trades opened by hand in MT5 are not pre-checked: the EA handles them after the waiting time.

## Stage 2 trial checklist (demo account, `InpAllowProtect = true`)
1. No stop, stop mode: protective stop appears after the waiting time; inbox message; breakeven/trailing then use the new 1R.
2. Oversized stop, stop mode: stop moves closer, never wider.
3. Protective level inside the broker's minimum distance, or already passed by price: trade closes, message says why.
4. close mode closes; breakeven mode moves to entry only after the profit threshold and only if tighter.
5. A trade switched off with the Auto button is untouched. A trade younger than the waiting time is untouched.
6. Restart the EA and MT5: no repeated actions (GlobalVariables TL_PT_/TL_PD_/TL_R_).
7. Hedged or several positions on one symbol, a symbol with a large minimum stop distance (Volatility indices, Boom/Crash), a JPY or cross pair.
8. Manually widening a protective stop: it is tightened again.
9. Connection lost: the last settings keep working; weakening a setting in the app only reaches the EA after the 24-hour wait.

## Bug found in the first demo trial (EA 1.54, fixed in 1.55)
Two demo trades with no stop were given a correct protective stop and then closed within minutes ("risked more than your limit and a protective stop could not be placed"),
although nothing was wrong with the trades. Two causes in EA 1.54 are established by the reference model in `tests/test_protect_logic.py`; whether they are the whole story is
confirmed only by repeating the trial with 1.55, which records the numbers behind every action in `ea_protect_log.detail`:
1. The account size was min(balance, equity). Equity moves with the trade's own floating loss, so the target moved every cycle and the stop was re-tightened again and again.
2. "Can a stop be placed?" (`StopsAllowed`) also checked the TAKE-PROFIT against the price. A take-profit near the price made the answer "no", and every "no" closed the trade.
Rounding alone cannot cause a close (an oversize stop can always be tightened by a tick); that was ruled out in the model.
1.55: size = balance; 5% tolerance on "too big"; only the stop is checked; a stop already at or inside the protective level means do nothing; a trade that has its own
oversized stop and cannot be tightened is LEFT OPEN and reported ("failed" event with the reason); only a trade with no stop at all and no way to place one is closed.
Every event now carries the numbers (risk, balance, price, open, stop, new stop, broker minimum) and closure messages show the reason.
