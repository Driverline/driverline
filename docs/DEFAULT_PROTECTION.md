# Every MT5 account starts protected

New defaults (`app/ea.py`: `DEFAULT_RULES`, `MANAGE_DEFAULT`), applied to every account that has no saved settings of its own:
- Risk rules: 4.5% max risk per trade, 10% daily loss limit, 3 open positions (this 4.5% is also the default per-trade limit that gates copy trades).
- Auto-manage: breakeven ON (after 1R, lock 0.1R), trail stop loss ON (from 1.5R, distance 1R), trail take-profit ON (0.3R / 1R). The numbers are the previous defaults.
- Equity protection: ON, "Set a protective stop loss" (4.5% / 4.5% / wait 45 s / breakeven 1%).
Members can change or switch any of it off in the MT5 tab. Existing safeguard kept: loosening equity protection takes effect 24 hours after the request.

**Existing accounts.** Once (marker `ea_meta.defaults_protected_2026_11`), accounts whose saved risk rules are still exactly the OLD defaults (1% / 3% / 3) and accounts whose auto-manage settings are still exactly the old all-off defaults move to the new ones; each such member gets one message. Accounts with any other saved values (for example the admin's own 10% / 20%) are not touched. A member who had deliberately left everything at the old values cannot be told apart from one who never opened the page.
**New connections.** The first time a connection reports, the member gets one message saying what protects the account (`PROTECTED_TEXT`). A member's second account still starts from their first account's settings (`_seed`).
Tests: `python3 tests/test_default_protection.py`. No schema change.
