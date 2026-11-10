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

## Not built yet
Pause after N consecutive losses (no per-trade outcome data yet); a 24 h delay on loosening limits (as for equity protection); inbox message when a trade is placed; public mode.

## Demo trial checklist
1. Admin: confirm access card shows "testers", add the tester e-mail.
2. On a demo account with EA 1.55 and AllowExecute on, tick Day + one symbol, 0.01 fixed lot, Save with ON ticked.
3. When a matching signal appears, check history shows sent then open, the trade has SL/TP and comment TLA-...
4. Pause: no new trades; waiting ones cancelled.
5. Try max open 1 with two signals: second is skipped with the reason.
