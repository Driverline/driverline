# Copy every master trade automatically (Nov 2026)

Why: flagging by hand took long enough for the price to move, and small followers' copies were then refused by the slippage guard. With the switch ON, every NEW trade on the master account is flagged by the system the moment its report arrives (the EA reports every few seconds), so followers get a price close to the master's.

## Switch: Admin > Copy trading > "Copy every master trade automatically" (OFF by default; `ea_meta copy_auto`)
Same rules as a manual flag, nothing weaker:
- Stop loss AND take profit are required, on the right sides of the entry. A new trade without both is given 45 s (`AUTO_SLTP_WAIT`); then it is left uncopied and the admin gets a "Master trade not copied" alert. It can still be flagged by hand later.
- Each follower keeps: its own sizing (fixed lot or % risk), own risk cap, daily loss limit, maximum open positions, AllowExecute, equity protection and auto-manage; and the slippage guard (`maxdev`) and the 120 s job expiry.
- Followers = accounts with copying switched on and an active copy entitlement. The master itself never copies.
- The copy closes when the master trade closes (existing behaviour).

Safeguards:
- Trades already open when the switch is turned on are recorded as "baseline" and NEVER copied. Turning it on needs the master account connected.
- If the master's EA was silent for more than 90 s (`AUTO_GAP`), trades found open afterwards are baseline too: nobody can tell when they opened.
- Brake: at most 40 automatic copies per rolling 24 h (`AUTO_MAX_PER_DAY`); extra trades are skipped with an alert.
- Every master trade seen is logged in `copy_auto_seen` (copied / waiting / skipped / baseline, with the reason); the admin card shows the latest.
- A bug in automatic copying is caught and logged; it never stops MT5 reporting. Switch OFF stops new copies at once; flagged trades keep running and can be ended from the flag list.
- A flag made by the system has `copy_flags.source='auto'`, by hand `'manual'`.

## Consent
Members' wording changed: the MT5 tab and the Terms now say the team's trades (normally every new one) are copied, that risk limits and protection apply, and that a copy can fill at a different price. A lawyer should read the Terms change. Tell members who already switched copying on (broadcast message) before using the switch.

## Rollback
Switch OFF in Admin. The patch can be reverted; the added table and column are additive.
