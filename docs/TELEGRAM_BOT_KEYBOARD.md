# @TradeLensagent: persistent navigation keyboard

In the bot's private chat: `/start` shows a welcome and a persistent 2x2 keyboard (App, Resources, Channel, Traders Hub). Tapping a button makes the
bot reply with a short message and an inline URL button to the exact destination (App link keeps all UTM parameters). The keyboard is never removed
and the welcome is only sent for /start, /help or /menu.

## How it works
- Telegram calls `POST /api/telegram/webhook` (app/tg_bot.py). The request is accepted only with the secret header (`TELEGRAM_WEBHOOK_SECRET`).
- The reply is returned in the webhook response itself, so the server needs no bot token and makes no outgoing call. Without
  `TELEGRAM_WEBHOOK_SECRET` the route returns 404 (feature off).
- Only private chats are handled; groups, channel posts, bots and edits are ignored. Nothing is stored or logged.
- Trade-signal posting is unchanged: the Companion EA only uses sendMessage, never getUpdates, so a webhook cannot compete with it.

## Deploy (in this order)
1. Push the code (see the delivery message). The route is dormant until the secret exists.
2. On your phone: `python3 scripts/telegram_webhook.py secret` prints a random secret. In Render > Environment, add `TELEGRAM_WEBHOOK_SECRET` = that
   value (this triggers a redeploy). Wait until the deploy is Live.
3. `read -s TELEGRAM_BOT_TOKEN; export TELEGRAM_BOT_TOKEN`, then `read -s TELEGRAM_WEBHOOK_SECRET; export TELEGRAM_WEBHOOK_SECRET` (same value as Render).
4. `python3 scripts/telegram_webhook.py info` (is a webhook already set? it should say none), then `python3 scripts/telegram_webhook.py set`.
5. Open @TradeLensagent, send /start, and tap each button.

## Rollback
`python3 scripts/telegram_webhook.py delete` stops it at once. Removing the Render variable also switches the route off.

## Notes
- Registering a webhook turns off getUpdates for this bot. Nothing in TradeLens uses it; `info` and `set` refuse to replace a different existing webhook.
- To show the keyboard to existing users, they send /start once (a bot cannot push a keyboard to a chat it has not been messaged in).
