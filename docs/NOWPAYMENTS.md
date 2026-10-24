# Automatic USDT (TRC20) payments with NOWPayments

Members pick a product on the Billing page, choose the network (USDT on TRON TRC20, and BEP20 once you switch it on), pay to the address NOWPayments gives them, and the signals
or plan are added automatically a few minutes later. No screenshot, no transaction ID, no manual approval. The manual
payment methods keep working exactly as before.

## How it works (short)
1. **Order.** The member picks a network only from the list you allow. The server prices the product from `CATALOG` in `app/credits.py` (partner prices included). Nothing the browser sends
   can change the price, the signals or the user. It asks NOWPayments for a payment (`POST /payment`, USD price, `usdttrc20`) and
   saves the order in the existing `billing_orders` table (`method = nowpayments`).
2. **Pay.** The page shows the address, the exact USDT amount and the network from NOWPayments, and checks the status every 8 seconds.
3. **Notification (IPN).** NOWPayments calls `/api/pay/nowpayments/ipn`. The signature (`x-nowpayments-sig`, HMAC-SHA512 with your
   IPN secret) is verified first. The notification only says *which order to look at*.
4. **Verification.** The server fetches the payment from the NOWPayments API with your API key and decides from that.
5. **Fulfilment.** Only provider status `finished` can credit, and only if payment id, order id, USD price, currency, deposit address and
   the amount actually paid all match. It uses the same code as the manual approval (`credits.grant_product`), in one database
   transaction. A second credit for the same order is impossible (status guard + the ledger's unique row).
6. **Safety net.** Every minute a background loop re-checks open orders with NOWPayments. A lost or late notification costs minutes.

Anything that does not fit goes to **review** in *Admin > Crypto payments*: underpaid, paid after the order expired, wrong currency or
price, an extra deposit. Nothing is credited until you decide. Overpayments are credited once and flagged (the extra is not credited).

No payout or withdrawal function of NOWPayments is used, and none is needed. Do not enable automatic withdrawals for this.

## What you must set on Render (Environment tab)
Type the values in Render yourself. Never paste them into chat, GitHub or a screenshot.

| Name | Value | Notes |
|---|---|---|
| `NOWPAYMENTS_API_KEY` | your NOWPayments **API key** | secret |
| `NOWPAYMENTS_IPN_SECRET` | your NOWPayments **IPN secret key** | secret. The "Public key" is not used |
| `DRIVERLINE_BASE_URL` | `https://tradelens360ai.live` | must be the public https address. Probably already set for reset emails |
| `NOWPAYMENTS_API_BASE` | leave unset for the live API | only to test on the NOWPayments sandbox: set to the sandbox address from their documentation |
| `NOWPAYMENTS_PAY_CURRENCIES` | optional. Unset = USDT TRC20 only (`usdttrc20`). To let members choose, set `usdttrc20,usdtbsc` | the networks offered, first one is the default. `usdtbsc` is the NOWPayments code for USDT on BNB Smart Chain (BEP20). Run *Check connection* in Admin first: it says whether each one is offered on your account. Only these two codes are accepted |
| `NOWPAYMENTS_ORDER_TTL_MIN` | optional, default 60 | minutes an order stays open if NOWPayments gives no expiry |

Saving environment variables makes Render restart the service.

## What you must set in the NOWPayments dashboard
1. Settings > Payments: make sure **USDT on TRON (TRC20)** is an enabled coin and an outgoing wallet is set for it.
2. Settings > Payments > **IPN**: generate the IPN secret key (this is `NOWPAYMENTS_IPN_SECRET`) and set the **IPN callback URL** to
   `https://tradelens360ai.live/api/pay/nowpayments/ipn`. The same URL is shown, with a copy button, in Admin > Crypto payments.
3. Settings > API keys: create an API key (this is `NOWPAYMENTS_API_KEY`).
4. Leave withdrawal-IP whitelisting and payout permissions as they are.

## Modes (Admin > Crypto payments > "Who can pay automatically")
* **Off**: nobody can start an automatic payment. Payments already sent are still detected and credited.
* **Pilot: admin accounts only**: only admin accounts see the button and can pay. Works even while billing is closed to members.
* **Public**: every member. Needs billing to be open, like manual payments.

## Rollout checklist
1. Deploy the code with no new variables: nothing changes for members. Confirm Billing and a manual order still behave as before.
2. Add the three variables on Render. In Admin > Crypto payments press **Check connection to NOWPayments**: it must say reachable and that
   `usdttrc20` is offered. (From a terminal, `python scripts/nowpayments_check.py --create 5` does the same and also shows whether $5 is above NOWPayments' minimum.)
3. Set the mode to **Pilot**. Sign in as admin, Billing, buy the cheapest pack with the automatic button and pay the exact amount.
   Expect: status goes Waiting > Confirming > Signals added, balance rises, and the order shows under *Admin > Crypto payments > approved*.
4. Press **History** on that order: you should see `created`, `provider_payment`, `ipn`, `fulfilled`. If there is no `ipn` row but it was
   fulfilled, the notification did not reach us or its signature was refused (Render logs say `IPN rejected: bad signature`): payments still
   complete through the check-in loop and the page's own refresh, but fix the IPN secret/URL.
5. Do a plan purchase (Plus) and confirm notifications turn on. Then set the mode to **Public**.

## When something needs you (Admin > Crypto payments > "Needs your attention")
| Reason shown | Meaning | What to do |
|---|---|---|
| underpaid | less than the order total arrived | ask the member to send the difference or refund from your NOWPayments dashboard, then **Reject**; or **Approve** if you accept it |
| paid_after_close | paid after the order expired | check the amount in NOWPayments, then **Approve** or **Reject** |
| mismatch | currency, price, order or address differs | look at *History* and NOWPayments; normally **Reject** and refund |
| extra_deposit | a second deposit to the same address | decide per the amounts |
| stuck | seen on-chain but not finished a day after expiry | **Re-check with NOWPayments**, then decide |

*Re-check with NOWPayments* re-reads the order from NOWPayments and applies it: the safe answer to any lost notification. Every decision needs a note and is recorded with your admin id.

## Free Render plan
The instance sleeps when idle. NOWPayments waits 3 seconds for an answer, so a notification can arrive while it is asleep. This costs nothing but time: a member who keeps the payment page open wakes the service
with the page's own 8-second refresh, which asks NOWPayments directly. If they close the page, the check-in loop credits the order as soon as the service next wakes (any visit). Tell members to keep the page open until it says *Signals added*.

## Rollback
1. Fastest: Admin > Crypto payments > mode **Off**. Members stop seeing it. Nothing else changes and money in flight is still credited.
2. Remove the three variables on Render if you want it fully inert.
3. Code: on Render, *Events > Rollback* to the previous deploy, or `git revert` the commit and push.
4. Database: nothing needs to be undone. The added columns and `payment_events` are ignored by the old code. `migrations/2026_10_nowpayments_rollback.sql` removes them completely (it refuses to run while automatic orders exist).

## Database changes
Additive only, applied by the app itself on first use, or by running `migrations/2026_10_nowpayments.sql` in Supabase's SQL Editor beforehand (safe to run twice). Back up first. Nothing existing is edited or deleted.

## Tests
`python tests/test_nowpayments.py`: 65 tests with a fake NOWPayments (no network): signature, pilot modes, order creation, fulfilment, duplicates and out-of-order notifications, lost notifications, underpaid and overpaid,
wrong currency, access control, concurrency, secrets in logs, migration of an old database, and the manual flow.

## Known limits
* The exact NOWPayments sandbox behaviour, their minimum amount for USDT TRC20 (a $5 order may be below it), and the byte-exact IPN signature text could not be tested against the real service from the build environment. The signature check accepts the documented form (sorted keys, compact JSON, HMAC-SHA512) in three number formattings and is tested against Node's `JSON.stringify`; the first real test payment confirms it.
* The PostgreSQL path of the app (psycopg) was not run in the build environment; the migration SQL was run against PostgreSQL 16. The code uses the same helpers and constructs as the existing billing code.
* Refunds are not automated: refund from the NOWPayments dashboard and reject the order in review.
