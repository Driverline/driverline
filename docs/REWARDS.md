# Referrals, points and levels (Phase 1)

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

## Not in Phase 1
Free signals and free Copy/MT5 for referrals (Phase 2); commission import and payout, and the prize catalogue (Phase 3).
