# Copy trading

The admin **flags** a trade on a master MT5 account. It is copied to every member who has a copy-trading subscription and has switched copying
on for their MT5 account. Only flagged trades are copied.

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
Admin > Copy trading (`/admin-copy.html`): choose the master account (one of your own MT5 accounts), flag trades, see each flag's per-member results, activate or revoke members' subscriptions (days stack). Subscriptions are granted by the admin after you confirm payment; there is no shop product yet.

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
