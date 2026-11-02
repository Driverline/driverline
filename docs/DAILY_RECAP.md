# Daily Telegram recap

One post a day in the TradeLens Telegram channel: the trades that closed that day, how each ended, the day's result, and a short
encouragement / discipline line that depends on how the day went. Built by `app/recap.py`. **Off until you switch it on in Render.**

## What the post looks like
```
📊 TradeLens Daily Recap
Mon 02 Nov 2026

1. Volatility 25 · Buy · ✅ Take profit +1.9R
2. Volatility 50 · Sell · ❌ Stop loss -1.0R
3. Volatility 100 · Buy · ➖ Breakeven

Closed today: 3 · 1 in profit · 1 at a loss · 1 breakeven/flat
Net result: +0.9R

💬 <one line chosen from the day's result>

Educational only, not financial advice. Use your own lot size and risk.
```
- **Results are in R, never money** (same rule as the EA's channel posts). Net result is shown only when every trade's R is reliable.
- Outcomes: Take profit, Stop loss, Trailing stop in profit, Breakeven (closed within 0.01% of its entry), Closed manually, Stopped out by the broker.
- Day tone: none / strong (2+ wins, 65%+ winners) / green / even (wins = losses) / flat (only breakeven) / red / heavy (0 wins and 3+ losses, or 3 more losses than wins). Several lines per tone, rotated by date.
- Up to 25 trades are listed; a longer day says "...and N more". It never exceeds one Telegram message.

## What it covers (and does not)
- Trades the Companion EA imported into the journal for the **admin** account(s) (all admins, or only `RECAP_USER_IDS`). A trade counts on the day it **closed** (Lagos time).
- Open positions are not in the journal, so they are not listed. Members' own trades are not included.
- The reason a trade closed (TP / SL / manual) is now **saved on each trade** (`trades.reason`, new nullable column). Trades from before the upgrade have none:
  they are judged from their saved stop and target, otherwise shown only as "Closed in profit" / "Closed at a loss" (never guessed as TP).
  This needs the **current Companion EA** on your terminal (it sends the reason). With an older EA the recap still works, with the neutral wording.

## Switch it on (Render > driverline > Environment)
| Setting | Meaning |
|---|---|
| `TELEGRAM_BOT_TOKEN` | the bot token (same bot as the EA). A secret: never logged, shown or sent to a browser. |
| `TELEGRAM_RECAP_CHAT` | the channel, `@yourchannel` or `-100...`. The bot needs **Post messages** there. |
| `DAILY_RECAP=1` | turns the schedule on. Without it nothing is ever posted automatically. |
| `RECAP_HOUR` | post after this local hour (default `21`). `RECAP_TZ_OFFSET` = hours from UTC (default `1`, Lagos). |
| `RECAP_USER_IDS` | optional: only these admin user ids, e.g. `1`. |

Admin > **Daily Telegram recap** shows what is set, a live preview of today so far, the last 7 days, and **Post today's recap now** (works even before `DAILY_RECAP=1`, to test).

## Safeguards
- One post per day: the day is reserved in `recap_log` **before** sending, so a restart or a second copy of the app cannot post it twice. A repeat by hand asks for confirmation.
- A failed post is retried every 10 minutes, up to 6 times, and the reason is shown in Admin.
- If the service was asleep at the posting time, the missed day is made up (yesterday only), and only if a recap was posted on an earlier day: switching it on never posts an old day.
- The token only ever appears in the request to Telegram; errors show Telegram's own description, never the URL.
- Server-side Telegram is a deliberate exception: `tests/test_telegram_access.py` still forbids Telegram code in every other `app/` file.

## Rollback
Remove `DAILY_RECAP` (or set it to `0`) in Render: nothing more is posted. The new column and table are additive and harmless.
