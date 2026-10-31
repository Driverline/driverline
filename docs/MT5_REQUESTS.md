# MT5 connection requests (TradeLens connects the account for the member)

Members do not get the Companion EA and do not create keys. They send their MT5 details; an administrator connects the account on the TradeLens VPS.

## Member
MT5 page > Connect your MT5: paste

    Login: 140374097
    Password: ...
    Server: DerivSVG-Server-03

tick the consent box, send. Status shows: waiting, being set up, rejected (with the admin's reason), or the account appears as Connected. They can cancel (the password is deleted). Max 3 accounts including pending requests, 5 requests a day.

## Admin (Admin > MT5 connections, `/admin-mt5.html`)
1. **Reveal details** (audited) and set the account up in MT5 on the VPS.
2. **Create account and key**: creates the member's MT5 account and shows its connection key once. Put it in that terminal's EA (set AllowExecute / AllowRemoteClose / AllowManage / AllowPending as you want for that member).
3. When the EA first reports, the request becomes **connected** and the **stored password is deleted automatically**. Nothing else to press.
- **Reject** (needs a reason the member sees) and **Delete password now** also delete the password. **New key** replaces the key if you need to redo the EA.
- Requests not completed in 14 days are expired and their password deleted.

## How the password is protected
- Encrypted at rest (Fernet) with **`DRIVERLINE_VAULT_KEY`**, which lives only in the server environment. If it is not set, members cannot send details at all (nothing is stored in clear).
- Never logged, never returned to the member, never in the list response; the reveal endpoint is admin-only, no-store, and each reveal is written to `mt5_request_log`.
- Wiped on connect, cancel, reject, manual wipe, and after 14 days.
- Back up the vault key somewhere safe: without it stored passwords cannot be decrypted (they can only be re-requested).

### Setting the vault key (you, in Render > Environment; never paste it in chat)
Generate one in Termux: `python3 -c "from cryptography.fernet import Fernet;print(Fernet.generate_key().decode())"`, add it as `DRIVERLINE_VAULT_KEY`, redeploy.

## Self-service keys
Off for members by default (admins can still create keys). `DRIVERLINE_MEMBER_KEYS=1` turns member-created keys back on. Existing connected accounts are unaffected.

## Database
`migrations/2026_10_mt5_requests.sql`: tables `mt5_requests` and `mt5_request_log`. Additive; the app creates them itself.

## Honest limits
- A trading password gives full control of the member's account. This flow makes TradeLens responsible for it: get the consent wording and Terms reviewed (including Deriv's terms about sharing credentials) before relying on it.
- After the password is deleted, re-doing the setup needs the member to send details again.
- Tested on SQLite only.
