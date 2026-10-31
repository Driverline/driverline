# Copy trading

The admin **flags** a trade on a master MT5 account. It is copied to every member who has a copy-trading subscription and has switched copying
on for their MT5 account. Only flagged trades are copied.

## Who can use it
| Plan | Copy trading | Signals / notifications |
|---|---|---|
| **Copy Trading** ($15 / 30 days) | yes, MT5 connection included | no |
| **Signals Pro** ($30) | yes | yes, at the same time |
| **Signals Plus** ($15), Deriv account **linked to our IB** (confirmed partner tag) | yes, **instead of** signals | switch: one at a time |
| Signals Plus, not linked | no | yes |
| Admin grant (Admin > Copy trading) | yes | unchanged |
Existing members and admins get no free copy trading: it always needs one of the above (billing must be open for plans to count).

**Signals Plus + IB switch.** The member is either on *Signals* (default) or *Copy trading* (MT5 page > Copy trading card, or by switching copying on, which asks first).
- Copy mode pauses signals (opening a setup is refused) and notifications, and turns the MT5 connection on.
- Every second in copy mode costs the Plus allocation pro rata: 550 signals / 30 days = **about 18.3 signals a day**, deducted from the signal balance (never below zero, exact with fractional carry, only for time the Plus plan was active). It is charged when they switch back and whenever they open Billing or the Copy trading card.
- Switching back to Signals deducts what has accrued, switches copying off and cancels waiting copies. Copies already open still close with the master trade.
- Plus + the Copy Trading plan (or Pro) together = no switch, both active, no deduction (they paid for both).
- Database: one new table `plan_mode` (in `migrations/2026_10_copy_plan.sql`, also created by the app).

## Rules
- **Flag needs SL and TP.** The master position must have a stop loss and a take profit (on the correct sides of the entry), otherwise it cannot be flagged. Copies are never unprotected.
- **Sizing (per member, per account).** Default: fixed lot = the symbol's smallest lot. Option: percentage risk (lot sized so the stop loss risks X% of balance).
- **Hard limit.** If the lot would risk more than the member's limit, nothing is traded and the reason is shown. The limit is the member's own "never risk more than" setting, or by default their risk rule (MT5 page > My risk rules).
- **Protection.** Each copy gets the master's stop and target *distances* applied to the member's own fill, and the EA refuses the fill if price has moved too far from the master's entry (same guard as one-click trades).
- **Other guards, each recorded as a skip with a reason:** EA offline for 2 minutes (never placed late), AllowExecute off, daily loss limit reached, max open positions reached, symbol not in Market Watch, subscription ended, copying off.
- **Closing.** When the master trade closes, or the admin presses "End copies", each copy is closed (needs AllowRemoteClose). Copies also close by their own SL/TP. The copy's ticket is found as the new position after the command; if that is ambiguous it is **not guessed** and the member is told to close it.
- **Later joiners** do not receive trades flagged before they enabled copying.
- Members' EAs are unchanged: nothing runs unless AllowExecute (and AllowRemoteClose) are on.

## Admin
Admin > Copy trading (`/admin-copy.html`): choose the master account (one of your own MT5 accounts), flag trades, see each flag's per-member results, activate or revoke members' subscriptions (days stack). Admin grants (days) are for gifts and corrections. Members normally subscribe in Billing (Copy Trading product, or Pro, or Plus with IB link).

## Members
MT5 page > Copy trading (per account): switch on, choose Fixed lot or Percentage risk, optional limit, recent copies with status and reasons.

## Database
`migrations/2026_10_copy_trading.sql`: `copy_subs`, `copy_members`, `copy_flags`, `copy_jobs` (+ indexes). Additive; the app creates them itself. Rollback: deploy the previous code, then `DROP TABLE copy_jobs, copy_flags, copy_members, copy_subs;` (this deletes the copy history).

## Known limits
- Tested on SQLite and with a simulated EA only. **Test on Demo accounts first** (make a Demo master and Demo followers) before using real money.
- Copies are placed within a few seconds of the flag, at the follower's own price, so results differ from the master's. The slippage guard may refuse a late fill.
- If a member opens another identical trade on the same symbol at the same moment, the copy's ticket cannot be told apart and that copy is not closed automatically.
- If you later move the master trade's SL/TP, followers' trades are not changed.
- Copying real trades for payment can be regulated in some countries. Check this and add a copy-trading section and risk disclaimer to the Terms before selling it.
