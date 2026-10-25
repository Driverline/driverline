# Naira payments with Paystack (test mode first)

Members pick a product on the Billing page, press **Pay in Naira**, pay on Paystack's own secure page (card, bank transfer, USSD, whatever your
Paystack account offers), and the signals or plan are added automatically once Paystack confirms the payment. Manual payments and NOWPayments
keep working exactly as before. This is fixed-duration access, like everything else in TradeLens: **no recurring billing, no saved cards, no Paystack plans.**

## How it works (short)
1. **Order.** The server prices the product from `CATALOG` in `app/credits.py` (partner prices included) and converts it with the **same `ngn_rate`
   setting the manual bank-transfer flow uses** (`round(usd × ngn_rate)` Naira). Nothing the browser sends can change the price, the quantity, the
   plan or the user. The order is saved in the existing `billing_orders` table (`method = paystack`) **before** Paystack is called.
2. **Initialise.** The server calls `POST /transaction/initialize` (amount in kobo, currency NGN, our reference, member's email, the order and user in
   `metadata`, callback `https://tradelens360ai.live/paystack.html`) and sends the member to the `checkout.paystack.com` address it returns.
3. **Return.** Paystack sends the member to `/paystack.html?reference=...`. That page only asks our server for the status. It cannot credit anything.
4. **Webhook.** Paystack calls `/api/pay/paystack/webhook`. The signature (`x-paystack-signature`, HMAC-SHA512 of the raw body with the secret key) is
   checked first. The webhook only says *which order to look at*.
5. **Verification.** The server calls `GET /transaction/verify/{reference}` with the secret key and decides from that answer alone.
6. **Fulfilment.** Only Paystack status `success` can credit, and only if reference, currency `NGN`, amount (exact kobo), live/test domain, the
   order/user in the metadata and the customer email all match. It uses the same code as manual approval and NOWPayments (`credits.grant_product`), in
   one database transaction. A second credit for the same order is impossible: the status guard, the ledger's unique row, and a unique index on the Paystack transaction id.
7. **Safety net.** Every minute a background loop re-checks open orders with Paystack. A lost or late webhook costs minutes, not money.

Anything that does not fit (wrong amount or currency, a transaction for another order or user, payment after the order closed, a reversal, a duplicate
transaction id, a bank transfer stuck for a day) goes to **review** in *Admin > Naira payments*. Nothing is credited until you decide.

## Test mode is safe by construction
* A key starting `sk_test_` makes the button visible **only to admin accounts**, in every mode. Members can never pay with a test key. "Public" mode cannot even be switched on.
* A key starting `sk_live_` is **refused** unless `PAYSTACK_ALLOW_LIVE=1` is also set. Do not set it until you have decided to take real money.
* Test orders are tagged: provider `paystack-test`, reference `TLN-T-...`, and the ledger note contains the reference. A test order can never be fulfilled under a live key, or the reverse.
* **What test mode does touch:** the database is the live one, so a successful test purchase really adds signals/plan days to *the admin account that made it*.
  Use the cheapest pack (Top-Up), and afterwards remove the signals in Admin > Billing > "Add or remove signals by hand" (e.g. `-267`, note "Paystack test").
  A plan bought in test mode is an ordinary 30-day row for that admin account (admins already have every feature, so nothing changes in practice).
  `migrations/2026_10_paystack_check.sql` lists everything test mode left behind.

## What you must set on Render (Environment tab)
Type the values in Render yourself. Never paste them into chat, GitHub or a screenshot. Saving makes Render restart the service.

| Name | Value | Notes |
|---|---|---|
| `PAYSTACK_SECRET_KEY` | your Paystack **test** secret key (`sk_test_...`) | secret. Server only. |
| `PAYSTACK_PUBLIC_KEY` | your Paystack **test** public key (`pk_test_...`) | optional. Not needed by this flow (checkout is hosted by Paystack); if set it must be the same mode as the secret key |
| `DRIVERLINE_BASE_URL` | `https://tradelens360ai.live` | already set for the other payment methods |
| `PAYSTACK_ORDER_TTL_MIN` | optional, default 60 | minutes an unpaid order stays open |
| `PAYSTACK_ALLOW_LIVE` | **leave unset** | only ever set to `1` when you decide to go live |

Also needed, in the app (Admin > Billing settings): the **Naira rate** (`ngn_rate`, Naira per 1 USD). Without it Naira payments stay off.

## What you must set in the Paystack dashboard (test mode)
1. Switch the dashboard to **Test mode**.
2. Settings > API Keys & Webhooks: copy the test secret key (for Render) and set
   * **Test Callback URL:** `https://tradelens360ai.live/paystack.html`
   * **Test Webhook URL:** `https://tradelens360ai.live/api/pay/paystack/webhook`
   (the same two URLs, with copy buttons, are shown in Admin > Naira payments). The callback is also sent with every transaction.
3. Nothing else. No transfers, payouts, plans or subscriptions are used.

## Modes (Admin > Naira payments > "Who can pay in Naira")
* **Off**: nobody can start a payment. Payments already started are still detected and credited.
* **Pilot: admin accounts only**: the only mode that works with a test key.
* **Public**: every member. Needs a live key, `PAYSTACK_ALLOW_LIVE=1`, and billing open. Do not use until you have finished testing and agreed to go live.

## Deployment
1. Apply the patch (commit and push). Render auto-deploys `main`. With no new variables nothing changes for members: confirm Billing, a manual order and a crypto order behave as before.
2. Add `PAYSTACK_SECRET_KEY` (test) on Render, set the dashboard URLs, check Admin > Billing has a Naira rate.
3. Admin > Naira payments: press **Check connection to Paystack** (must say the key was accepted, mode test), set the mode to **Pilot**.
4. Work through the manual checklist below as an admin account.

No database migration is needed: Paystack uses columns the NOWPayments change already added. `migrations/2026_10_paystack_check.sql` is read-only.

## Rollback
* **Fastest, no deploy:** Admin > Naira payments > mode **Off** (or delete `PAYSTACK_SECRET_KEY` on Render). The button disappears. Money already on its way is still processed while the key exists.
* **Code:** `git revert <the commit>` and push (Render redeploys). Orders already created stay in `billing_orders` as ordinary rows; the older code ignores them. Credits already given are untouched.
* **Database:** nothing to roll back.

## Manual test checklist (admin account, test mode, cheapest pack first)
Use the test cards / test bank options Paystack documents for test mode.
- [ ] Billing shows **Pay in Naira ... (TEST MODE)** for an admin and **not** for a normal member account.
- [ ] Top-Up: pay with a successful test card. You return to `/paystack.html`, it shows *Payment confirmed*, signals +267, one order approved, one ledger row.
- [ ] Open the order's **History** in Admin: `created, provider_init, webhook, fulfilled`. (No `webhook` row but still fulfilled = the webhook did not reach us: check the URL, payments still complete through the page and the minute loop.)
- [ ] Reload `/paystack.html?reference=...` several times and resend the webhook from the Paystack dashboard: the balance does not move again.
- [ ] Plus (30 days): signals +550, a `billing_subs` row of 30 days. Buy it again: the second row starts when the first ends.
- [ ] Pro: MT5 and notifications entitlement row appears (admins already have access, so check Admin/ledger rather than the feature).
- [ ] Declined test card: page shows *Payment failed* and offers to continue; nothing credited. Then pay successfully on the same page: credited once.
- [ ] Close the Paystack page without paying: order stays *Waiting*; *Cancel this order* closes it; after expiry an unpaid order shows *Order expired*.
- [ ] Bank-transfer / pending test option (if offered): shows *Waiting for confirmation* and credits when it completes.
- [ ] Another member's order reference typed into `/paystack.html?reference=...` shows "could not find that payment".
- [ ] Manual Naira bank transfer and a USDT (NOWPayments) order still work as before.
- [ ] Afterwards: remove the test signals (Admin > Billing, negative adjustment) and run `migrations/2026_10_paystack_check.sql`'s last two queries.
- [ ] **Before going live (not part of this phase):** repeat with live keys and the smallest real amount, set `PAYSTACK_ALLOW_LIVE=1`, replace the dashboard *Live* URLs, and only then consider Public mode.

## Automated tests
`python tests/test_paystack.py` (97 tests, fake Paystack, no network, no real keys) and `python tests/test_nowpayments.py` (65 tests, unchanged). Run each as its own process.

## Known limits
* Verified on SQLite only in automated tests; the SQL is the same style as the NOWPayments code that already runs on PostgreSQL (placeholders, conditional UPDATEs, unique indexes), but the first real run on Supabase is the real proof. Run the checklist.
* Paystack's own responses were modelled from its documented fields (`status`, `reference`, `amount` in kobo, `currency`, `domain`, `id`, `metadata`, `customer.email`). Confirm with a real test payment.
* A Paystack **refund/reversal after fulfilment** is flagged in the history (`anomaly`), never clawed back automatically.
* A customer who closes Paystack's page cannot tell us "cancelled" directly; an unpaid order is shown as waiting, then expired (or cancelled by the member).
* Webhook source-IP allow-listing is not used: the signature plus the server-side verify call is the protection.
