# AutoTrading (private test)

TradeLens places trades from published signals on a member's own MT5, limited to the Signal types and Symbols they enable.

## Status
Stage 1, private. Visible only to admins and testers listed in Admin > Copy trading > "AutoTrading access". Paid members only (same entitlement as the Open-trade button). There is no public mode in the code. Before any public launch: Deriv review closed, legal check (an automated service acting on members' accounts), a demo-account trial.

## How it works
1. `signal_loop` publishes new signals, then calls `autotrade.on_new_signals`: one `waiting` job per enabled account whose styles, symbols and max risk rating match. Unique per (signal, account).
2. The EA's poll calls `autotrade.dispatch` (after copy dispatch). It turns at most ONE waiting job into an ordinary `open` command (comment `TLA-<signal id>`, stop and target attached), or skips it with a reason shown to the member.
3. `autotrade.on_report` follows the command: sent, open (ticket found), closed, or failed.

A job is skipped (never queued late) when: older than 120 s, signal no longer open or expired, member switched it off or changed choices, MT5 not connected, AllowExecute off, own daily loss limit reached, own max positions reached, max auto trades open reached (default 3), max per UTC day reached (default 5), the symbol already has an open trade, symbol not in Market Watch, lot would risk more than the cap (never shrunk, skipped), or the equity-protection pre-check would refuse it.

Auto manage and Equity protection are untouched and apply to auto trades like any other trade.

## Settings (per MT5 account, `auto_settings`)
enabled, styles, instruments (nothing is pre-selected), sizing (risk % of balance, default 1%, or fixed lot), cap per trade (default 2%, and never above the member's own limit), max open (1-10, default 3), max per day (1-50, default 5), highest risk rating (default Medium). First enabling needs a tick-box acknowledgement (`ack_at`). Pausing cancels waiting jobs; open trades stay open and protected.

## API
- `GET /api/auto/me` (`available:false` for anyone not allowed), `POST /api/auto/settings`
- `GET/POST /api/auto/admin` (mode `testers` or `off`, tester e-mails)

## Files
`app/autotrade.py`, tables in `app/ea.py` SCHEMA (`auto_settings`, `auto_jobs`), hooks in `ea.poll`, `ea.report`, `signal_feed.signal_loop`, `static/auto.html`, tab in `static/auth.js`, tile in `static/index.html`, admin card in `static/admin-copy.html`, `tests/test_autotrade.py`.

## Account size and symbol eligibility (round 2)
The tier comes from the live equity in the member's own EA report (never from the browser) and is checked again before EVERY trade (`equity_status`, `trade_allowed`). Equity below $26 is tier 1 (the brief's $25 / $26 gap is closed this way), $26-59.99 tier 2, $60-159.99 tier 3, $160-599.99 tier 4, $600-1,499.99 tier 5, $1,500-10,499.99 tier 6, $10,500+ tier 7 (every supported symbol).

| Tier | Adds |
|---|---|
| 1 | Volatility 10, 50 |
| 2 | Volatility 25, gold (H4 only, micro contract) |
| 3 | Volatility 10/25/50 (1s) |
| 4 | Volatility 75, 100, 75 (1s), 100 (1s), Step Index |
| 5 | Boom 300, Crash 300 |
| 6 | Boom/Crash 500 and 1000; gold becomes the standard XAUUSD (H4 only) |
| 7 | All supported symbols, existing timeframe rules |

Enforcement is on the server in three places: the settings API (a locked symbol is refused with 422 and logged as `rejected_setting`), job creation, and dispatch (`rejected_tier` / `rejected_equity` events). Gold must be the `H4` signal type in tiers 2-6, and the broker symbol must be the micro contract in tiers 2-5 and the standard contract in tier 6. No new trade starts when the account is not connected, the equity report is older than 120 s, equity is missing, or the account currency is not USD (non-USD accounts are refused for now). A fall into a lower tier never closes or changes an open trade; the member gets an inbox message (at most one per hour) and the symbols that are no longer allowed are skipped with the reason. A rise unlocks symbols for selection only; nothing is switched on.

## Defaults
Auto manage and Equity protection keep their existing defaults (`ea.MANAGE_DEFAULT`) and AutoTrading never writes them (tested). The page shows "Using the recommended settings" or lists what differs, and the MT5 page asks for confirmation before saving non-recommended Equity protection settings while AutoTrading is on.

## 24-hour delay on loosening
After the first set-up, anything that lets AutoTrading take more risk (added symbols or types, higher max open, per day, risk %, cap, fixed lot, loss limit or rating, switching sizing mode) waits 24 hours (`LOOSEN_DELAY`, `merge`). Lowering or removing applies at once and cancels a waiting increase. The form shows waiting values, so saving again does not restart the clock. Switching AutoTrading on or off is not delayed.

## Loss pause
`on_closed` records each closed auto trade's profit from the EA's closed-trade report. After `loss_limit` (default 3, range 2-10) losing auto trades in a row, AutoTrading switches itself off for that account until the member ticks Resume. A win ends the run; a trade that loses less than 0.1% of the balance (breakeven exit) is neutral. Resuming starts the count from zero. Open trades are untouched.

## Inbox messages
One message when an auto trade is placed (confirmed by the EA), when AutoTrading pauses itself, and when the tier changes.

## Admin
Admin > Copy trading > AutoTrading access shows per account: equity, tier, chosen symbols, symbols not allowed now, status, last sync, plus the last 40 eligibility decisions (`auto_events`).

## Database changes (all additive, created at start-up)
`auto_events` (new); columns added to `auto_jobs` (profit, closed_at) and `auto_settings` (loss_limit, pending, paused_reason, streak_since, tier, equity, equity_at).

## Not built yet
Public mode; conversion for non-USD accounts.

## Demo trial checklist
1. Admin: confirm access card shows "testers", add the tester e-mail.
2. On a demo account with EA 1.55 and AllowExecute on, tick Day + one symbol, 0.01 fixed lot, Save with ON ticked.
3. When a matching signal appears, check history shows sent then open, the trade has SL/TP and comment TLA-...
4. Pause: no new trades; waiting ones cancelled.
5. Try max open 1 with two signals: second is skipped with the reason.

## Incident, 10 Nov 2026: V75 lot size 6.322 (fixed 11 Nov)
AutoTrading sized a Volatility 75 SELL at 6.322 lots on a $1,062 account (setting: risk 4.5%) and the broker stopped the trade out (reason "so") for -$946.97. Cause: Deriv's MT5 reports Volatility 75 Index with tick size 0.01 but tick value 0.0001 (100x too small; contract size 1). Risk sizing used tick value / tick size = 0.01 as the money per price unit per lot; the true figure is 1. The stop was 725.9 points away, so one lot really risked about $726 and the trade risked about $4,589 (450%), not $47.79. Equity protection saw the real figure from MT5 (risk 4589.20 of 1019.83) but could not place a closer stop (broker minimum distance) and, by design, left the trade open. The other Deriv symbols checked (V10, V50, V100, V75 (1s)) report consistent values; V100 traded correctly the day before (3.55 lots, loss 4.5%).
Fix: `config.unit_per_lot(spec)` is now the single place that works out money per price unit per lot. For Deriv synthetic indices it never goes below the contract size (so a wrong broker tick value can only make a lot smaller). It is used by AutoTrading and copy trading sizing (`copy_trading.size`) and by the Equity-protection pre-check and Open-trade risk figure (`pending_orders._risk_pct`). Forex and gold keep the broker's tick value.
Not changed: the Open-trade button (no lot given = the symbol's minimum lot).
Second layer (EA 1.56): the EA reports `unit` for every symbol, measured with MT5's own OrderCalcProfit (the call Equity protection already trusts). `unit_per_lot` takes the larger of the broker tick-value figure, the EA's `unit`, and (for Deriv synthetics) the contract size, so a lot can only get smaller. An older EA simply does not send `unit`; the synthetic floor still protects Volatility indices.
