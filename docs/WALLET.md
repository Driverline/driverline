# Member wallet

Members keep money in a wallet and spend it on any TradeLens product. Money added is **not used for anything** until the member pays with it.

## What members see
* Header, top right (same row as the logo): `WALLET $40.00` and an eye button that hides it (`••••`) or shows it. The choice is remembered on that device. Tapping the amount opens the wallet on the Billing page.
* Billing page, "Your wallet": balance, "Add money" ($10, $25, $50, $100, $250) which opens the same payment choices as any purchase (automatic USDT, Naira, manual USDT), and the wallet history.
* On every product, a first option "Pay with wallet · $X available" (disabled with an explanation when the balance is too small). It takes the price instantly and activates the product.

## How it works
* A top-up is an ordinary billing order for a wallet product (`wallet10` ... `wallet250` in `credits.CATALOG`). All existing payment safeguards apply. When the order is paid, `credits.grant_product` credits the wallet instead of adding signals (once per order).
* Paying from the wallet (`POST /api/wallet/pay`) is one transaction: debit, wallet ledger row, an order created already approved (method `wallet`), and the product via `grant_product`. Any failure rolls everything back. The balance cannot go below zero (conditional UPDATE and a CHECK). A repeat of the same product within 15 seconds is refused.
* Referral points and referral rewards count the purchase paid from the wallet, not the top-up, so nothing is counted twice (`rewards.py` ignores wallet top-up orders).
* Money is stored as whole cents. Tables: `wallet_balances`, `wallet_ledger` (`migrations/2026_11_wallet.sql`).
* The admin receives the usual "Payment" alert for a wallet payment.

## Admin: Payments > Wallet
* **Mode**: Off (default; nothing is visible, no top-ups; a balance already held stays visible), Pilot (admin accounts only), Public.
* Totals held, and **Adjust a wallet** (add or remove money by email with a required note, recorded with your admin id). Use this for refunds. There is no withdrawal.

## Rollout
1. Deploy. Nothing changes for members (mode Off).
2. Set mode **Pilot**, add $10 to your own wallet with a small real payment, then buy Copy Trading with it. Check the wallet history, the order list and the header.
3. Set **Public** and tell members.
Rollback: set mode Off (balances stay and can still be spent). Code: revert the commit; the tables are ignored by older code.

## Decisions for the owner
* The wording "cannot be withdrawn" and "non-refundable" is a policy choice; the Terms page does not yet mention the wallet. Have the Terms and Refund policy updated (lawyer) before Public.
* Top-ups are fixed amounts for now. A custom amount needs a variable-amount path in all three payment channels and is a separate step.
* NOWPayments may have a minimum for a $10 USDT payment; check with Check connection.
