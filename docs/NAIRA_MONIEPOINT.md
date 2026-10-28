# Pay with Naira (manual Moniepoint transfer)

Replaces the Paystack integration. A member starts an order, pays by bank transfer to the Moniepoint account, and an administrator confirms the
credit in the Moniepoint account and approves the order. Nothing is granted until an administrator approves. There is no bank API, no webhook,
no proof upload and no automatic reconciliation. NOWPayments and manual USDT are unchanged.

## How it works

1. Member taps **Pay with Naira** on a product. The server creates a `pending` order (`method = 'moniepoint'`, ref `TLM-XXXXXXXX`), prices it as
   USD price x `ngn_rate`, and stores a unique random narration (`TL-XXXXXXXX`). Amounts sent by the browser are ignored.
2. The member sees account name, bank, account number, exact amount and narration, each with a Copy button, plus instructions and the support
   contact. "I have sent it" saves an optional note for the admin; it never grants anything.
3. Admin > **Naira payments (Moniepoint)**: open the order, check the credit in Moniepoint, type the bank transaction reference and the amount
   actually received, then **Approve Payment**.
   - Exact amount: the order is approved and fulfilled through `credits.grant_product` in one transaction.
   - Different amount (underpaid or overpaid): the order goes to **review** and grants nothing. It can only be approved by ticking
     "accept the difference" with a note.
   - **Reject** needs an internal reason, which the member never sees. **Flag** moves an order to review. **Flag unmatched credit** records a
     bank credit that matches no order.
4. Idempotent: a second click or a concurrent approval credits once. One bank reference can settle only one Naira order (unique index).
   Every step is recorded in `payment_events` (History button).

## Setup

- Admin > Billing: set bank name, account name, account number, `ngn_rate`, support contact.
- Admin > Naira payments: mode **Off / Pilot (admins only) / Public**. Public requires billing to be open.
- Existing orders stay approvable when the mode is Off.

## Database

`migrations/2026_10_naira.sql`: one nullable column `billing_orders.narration` and two partial unique indexes. Additive and idempotent. The app
applies the same changes itself on first use, so running it by hand is optional. Rollback: `2026_10_naira_rollback.sql` (refuses to run if Naira
orders exist). Nothing in `payment_events` or credits is touched.

## Deployment

1. Apply the patch (see the delivery message) and push; Render deploys.
2. Admin > Billing: fill in the bank details. Admin > Naira payments: set **Pilot**.
3. Pilot test with a small real transfer from an admin account: order, transfer, approve, check balance, try Approve again (no second credit).
4. Set **Public** when satisfied.

## Rollback

Fastest: set the mode to **Off** (hides the option, nothing else changes). Full: `git revert` the commit. The extra column and indexes are harmless
to the old code.

## Paystack clean-up (manual, after you are satisfied)

- Delete `PAYSTACK_SECRET_KEY` / `PAYSTACK_PUBLIC_KEY` in Render; remove the webhook in the Paystack dashboard.
- Two approved Paystack **test** orders credited 817 signals to admin user 1. Reverse with Admin > "Add or remove signals by hand" (-817, note).
- Historical Paystack orders and events are kept untouched; the `paystack_enabled` setting row is now unused.

## Known limits

No automated bank matching, no new-order alert to admins, and `static/terms.html` still mentions sending a transfer reference for Naira.
Tested on SQLite only; the same SQL is portable to Postgres, but it has not been run against Supabase.
