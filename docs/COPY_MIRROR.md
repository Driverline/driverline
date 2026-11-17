# Copy trading: automatic set-up and proportional sizing

**Automatic start.** When a member has an active copy plan (paid, or granted by admin) and their MT5 account reports to TradeLens, `copy_trading.enrol` creates their copy settings: enabled, mode `mirror`, `how='auto'`. It runs only when the account has no copy settings yet, so a member who switched copying off or chose their own settings is never overridden. Not done for the master, for unpaid members, or for Signals Plus members whose copy trading replaces their signals (they must switch themselves). The member gets a message in Messages; admin gets a "Copy trading on" alert.

**Admin.** Admin > Copy trading > "Paid members and copy set-up" lists paid members: no MT5, not set up, copying, or switched off. "Set up with default settings and tell them" calls `POST /api/copy/admin/member`. Granting days also sets up accounts that are already connected.

**Mirror sizing.** When the admin flags a trade, the master's risk % is stored (`copy_flags.master_lots`, `master_risk_pct` = lots x |entry-SL| x value per lot / master balance). Each follower in `mirror` mode gets the lot that risks the same % of THEIR balance, rounded down to the lot step. Same entry, SL and TP as before.
Example: master $1,000, 0.40 lot, $40 stop = 4%. A $100 account trades 0.04, a $10,000 account trades 4.00.

**Gates (unchanged, never bypassed).** A copy is skipped (not shrunk) when it would risk more than the member's own cap (`max_risk_pct` in copy settings, else their account risk rule, default 1%), when the smallest lot already risks more than the master's %, when the daily loss limit or equity protection stops the account, when max positions is reached, or when slippage exceeds the guard. Members who want to follow larger-risk trades raise their cap themselves.

**Schema.** `copy_flags.master_lots`, `copy_flags.master_risk_pct`, `copy_members.how` (see migrations/2026_11_copy_mirror.sql). Tests: `python3 tests/test_copy_mirror.py`.
