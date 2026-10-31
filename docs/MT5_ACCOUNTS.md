# Several MT5 accounts per member

A member can connect up to 3 MT5 accounts (for example Demo and Live, or Standard and Swap Free) and manage each one the way the single
account was managed before: live status and positions, risk rules, auto-manage, close, one-click trades from Signals, and pending orders.

## How it works
- **One key per MT5 terminal.** MT5 page > Add account gives a key (shown once). Paste it into the EA on that terminal. The EA itself is unchanged.
- **Existing members:** the current connection becomes the account "Main", with the same key, rules and auto-manage settings. A connected EA keeps working untouched.
- **Choosing an account:** with more than one account, an account chooser appears on the MT5, Signals, Pending orders and Risk pages. The choice is remembered in the browser.
- **Nothing is guessed.** With several accounts, every action (trade, close, rules, auto-manage, pending order) must name the account; the server answers 422 otherwise. With one account nothing changes. Another member's account id always answers 404.
- **Same key on two terminals** (different MT5 logins within 2 minutes) is refused with 409 so two accounts' data never mix. Use one key per account.
- Included in the existing MT5/Pro entitlement; no pricing change. Limit: env `DRIVERLINE_MAX_MT5_ACCOUNTS` (default 3).
- Not included: copying one trade to all accounts.

## Database
`migrations/2026_10_mt5_accounts.sql`: five new tables (`ea_accounts`, `ea_acct_state`, `ea_acct_rules`, `ea_acct_manage`, `ea_meta`), one column
`ea_commands.account_id`, two indexes, and a one-time copy of existing connections. Additive and idempotent; the app applies it by itself on first use,
so running it by hand is optional. The old tables (`ea_keys`, `ea_state`, `ea_rules`, `ea_manage`) are left untouched.

## Rollback
`git revert` the commit. The old code reads the old tables, which still hold each member's first connection. Accounts added or changes made
after the upgrade exist only in the new tables and are not seen by the old code.

## Known limits
- Tested on SQLite only; the SQL is portable to Postgres but has not been run against Supabase.
- During a rolling deploy, old and new instances briefly use different tables; an EA report may be ignored for a few seconds.
- A phone with an old cached page works for members with one account only; a reload fixes it.
- A terminal that switches MT5 logins on the same key is refused for up to 2 minutes.
- Perks founder verification and journal imports are unchanged (journal imports are already per login).
