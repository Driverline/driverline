# Perks v2 and in-app messages

Status: **code ready and tested on SQLite; not deployed; production database not changed.**

## Free signals and perks (what the app now does)

| Who | Gets |
|---|---|
| Every new member | **3 welcome signals**, once (`credits.WELCOME`). |
| Partner member (Deriv confirms the account is tagged to us) | **5 free signals in total**, labelled "Partner member bonus signals". Topped up from what they hold: 3 welcome + 2 bonus, or 5 bonus if confirmed before any welcome. Never 3 + 5. Signals Pro at $15 (unchanged). **No free notifications any more.** |
| Founding members: first **3** to fund | Notifications **and** MT5 connection free for **30 days**. Funded = live Deriv MT5 account, USD, balance **>= $100** when the EA reports. First three to show it win, in funding order (not sign-up order). |

Rules that keep it honest
- A waiting partner holds no place and no rank. The 3rd verification closes everyone still waiting; partners confirmed later are shown "closed".
- One funded MT5 login can verify one member only (unique index `ux_perks_login`).
- Nothing is ever taken back: members who already hold 10 signals, or a `partner_notify` plan, keep them until they run out or end.
- Everything applies only while billing is switched on, like paid plans.

## In-app messages (`app/inbox.py`)
- **Members**: new **Messages** tab (with unread badge): announcements on top, one conversation with the team below.
- **Admins**: new **Inbox** tab (`/admin-messages.html`): conversations list (unread first), reply, start a conversation by member email, broadcast to all members with a "seen by N of M" count.
- A broadcast is stored once; each member only has a "seen up to" marker. Members who joined after a broadcast do not see it as unread.
- Plain text only (never HTML). Limits: member 1000 chars, admin 2000, 5 messages/minute and 30/day per member. Double-click protection on broadcasts.
- Not included: push/e-mail alerts for new messages, attachments, editing or recalling a broadcast.

## Database (additive, safe to repeat; the app applies it itself on first start)
- `migrations/2026_11_perks_v2.sql`: column `partner_perks.founder_login`, unique index, and clears the ranks of members still *pending* (the 3 existing ones keep waiting and can claim by funding).
- `migrations/2026_11_inbox.sql`: three new tables, with row-level security enabled.
- No signal, ledger row, balance, plan or order is changed.

## Deploy checklist
1. Apply the patch to the repository; run `python3 tests/test_perks.py`, `test_inbox.py`, `test_app_wiring.py`, `test_nowpayments.py`, `test_copy_plan.py`.
2. Deploy to Render. The first request creates the tables and applies the migration.
3. Check: Admin > "Partner perks" shows `3 of 3 places left`; send yourself a message from a free account and answer it from Admin > Inbox.
4. Rollback: redeploy the previous commit. The added column, index and tables are harmless to the old code.

## Known limits
- "Funded" is the balance the MT5 account shows when it connects. It does not prove where the money came from, that it stays, or that the account is the same Deriv account Deriv tagged to you. Admin sees the login and balance for each founder.
- Tested on SQLite only. The SQL is portable, but run the first deploy against a Supabase branch if you want certainty on PostgreSQL.
