> **UPDATE Nov 2026: commission, Phase 2 referral purchase rewards, "referral links Deriv" points and Deriv-button trade points are RETIRED.** See `docs/PARTNER_PROGRAMME.md`. The sections below describe the old design and are kept for history.

# Referrals, points and levels (Phase 1), referral rewards (Phase 2), commission and prizes (Phase 3)

Switched OFF until `REWARDS_ON=1` is set in Render. Nothing existing changes: new tables only (`migrations/2026_11_rewards.sql`, the app also creates them itself).

## For members (Rewards tab)
Referral link `https://tradelens360ai.live/login.html?rc=CODE` (share/WhatsApp/copy), friends joined and linked, level and progress, monthly and all-time leaderboard (names masked), ways to earn, recent points.

## Points
| Activity | Points |
|---|---|
| Open the app each day | 2 (+10 at a 7-day streak, +50 at 30 days) |
| AI analysis | 1 each, max 5 a day |
| Trade (EA close or Deriv-button trade) | 3 live, 1 demo or unknown account, max 5 a day |
| MT5 connection | 50 per account, max 2, one login only ever pays once |
| Copy Trading | 100 first activation, 20 each month it is active with a plan |
| Purchase | 10 per USD (approved orders); reversed if refunded/failed |
| Referral links Deriv | 50 to the referrer |
| Referral first purchase | 100 to the referrer |
| Referral later purchases | 2 per USD to the referrer |

Levels by lifetime points: Bronze 0, Silver 1,000, Gold 5,000, Diamond 20,000, Platinum 60,000. Spendable points (for prizes, Phase 3) are kept separately, so spending never lowers a level.

## How it works
Points are collected by sweeps that read what the app already records; payments, trades and logins are never touched. A sweep runs when a member opens the app (at most every 10 minutes) and when a referrer opens Rewards. Only activity from the moment Rewards is first used counts. Every award is unique per member + kind + reference, so repeats cannot double-pay. Days are UTC days.
Referrals: the code is kept in a 30-day cookie by `static/track.js`, attached at signup (first touch wins, never changed, self-referral ignored), and pays once the friend has a Deriv account linked.

## Admin
Admin > Rewards: status, totals, top referrers, latest points, manual adjustment (reason required, cannot take spendable below 0).

## Switch on / off / roll back
Render > Environment: `REWARDS_ON=1` to start, remove or `0` to stop (points and links stay stored). Rollback of the code is a Git revert; the tables can stay (they are empty if unused).

## Phase 2: free signals and free connections (needs `REWARDS_ON=1` AND `REWARDS_BONUS_ON=1`)
Only the referrer's QUALIFIED referrals count (Deriv account linked). Granted as soon as the purchase is confirmed: no hold, because the terms say there are no refunds.
- **Free signals:** 5% of the signals of a Signal Vault (5,500 gives 275) or Signal Vault Lite (2,500 gives 125) a referral buys, credited to the referrer's signal balance through the credits ledger (kind `ref_signals`, one per order, so repeating never pays twice).
- **Free connection:** for every 3 DIFFERENT referrals who bought a plan that includes MT5 or Copy trading (Signals Pro, Copy Trading, Signal Vault Lite, Signal Vault), the referrer gets 30 days of MT5 connection + notifications (plan `ref_mt5` in `billing_subs`, marked with `order_id = -k` so each reward is created once) and Copy trading for the same days (`copy_subs`, never shortened if a longer grant exists). Rewards stack back to back. Signals and Copy trading can both be used: the member simply turns Copy trading on or off.
- Orders marked refunded, failed or partially paid do not count. A reward already given is not taken back (use Admin > Rewards > adjust, or the Copy trading grant screen, if a correction is needed).
- Shown to members in Rewards > "Your referral rewards".

## Phase 3: commission and prizes
Switches (all need `REWARDS_ON=1`): `REWARDS_COMMISSION_ON=1` shows commission and payouts to members; `REWARDS_PRIZES_ON=1` shows the prize catalogue. The admin tools (Payouts tab) always work for admins.

**Commission (10%).** Admin > Payouts > Commission import, once a month AFTER Deriv has paid TradeLens:
1. Choose the month, load or paste the Partners Hub commission report (CSV), choose which column is the client ID and which is the commission (USD). The report format is not assumed, and a row with extra columns (for example an unquoted 1,000.00) is refused rather than misread.
2. Preview shows the totals, who earns what, and why other rows were left out. Nothing is saved.
3. Tick the USD / Deriv-has-paid confirmation and Apply. A month already imported is only replaced if you tick Replace (balances follow the corrected report; a referrer paid more than the corrected total is flagged "PAID MORE THAN EARNED").
A client earns a share only if Deriv's partner records CONFIRM the account as tagged to TradeLens (partner_tags status "confirmed", which covers accounts created through TradeLens), the account belongs to a member, and that member was referred. Everything else stays with TradeLens.
**Payout is manual:** the member saves a USDT address (TRC20 or BEP20, checked) and requests a payout (minimum $10, one at a time). Admin pays from their own wallet, pastes the transaction hash and presses Paid (or Rejects with a reason; the amount returns to the member's balance). The member is messaged in their inbox either way. The app never sends money.

**Prizes.** Admin > Payouts > Prize catalogue: points, stock, cost (private), shown/hidden. The starting six use 100 points per $1 of cost and start with stock 0 ("coming soon"): Pen 500, Journal and pen 1,000, Powerbank 5,000, Tablet 11,000, Phone 25,000, Laptop 50,000. Members spend spendable points (their level never drops), give a name, phone and address, and have one request open at a time. Stock, points and request change in one transaction (no overselling). Admin marks a request sent (courier and tracking) or rejects it (points and stock return); the member can also cancel while it is waiting. Delivery details are visible to admins only.

## Switch on / off
Render > Environment. Off: remove the variable or set it to 0. Members stop seeing the cards; stored data stays.
