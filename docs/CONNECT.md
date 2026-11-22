# TradeLens Connect (Phase 1): Telegram Mini App + Connect API

Off by default. Nothing changes for the website or the bot until `CONNECT_SECRET` is set in Render.

## Settings (Render > Environment)
| Name | What | Needed for |
|---|---|---|
| `CONNECT_SECRET` | NEW random value, 32+ chars. Never reuse DRIVERLINE_SECRET. | Turns Connect on |
| `CONNECT_BOT_TOKEN` | Token of the bot that opens the Mini App (use a TEST bot first). Only used to verify Telegram's signed data. | Mini App |
| `CONNECT_MINIAPP_URL` | `https://<your-domain>/tg/` | The `/app` bot command |

Without `CONNECT_SECRET` every `/api/connect/v1/*` route answers 404 and the new bot commands do nothing.

## Code change in app/main.py (two lines)
1. In the `from . import ...` line, add `connect`.
2. Right after `*tg_bot.routes,` add `*connect.routes,`.

## Routes
POST `/session` (initData) | POST `/link` (initData, email, password) | POST `/unlink` | GET `/me` | GET `/entitlements` | POST `/events`.
All except `/session` and `/link` need `Authorization: Bearer <token>`. Tokens last 15 minutes and stop working at once on unlink, block or password reset.

## Bot commands (only when Connect is on)
`/app` opens the Mini App | `/status` shows link and access | `/unlink` then `/unlink confirm`.
`/start` and the four buttons are unchanged.

## Setting up the test bot
1. @BotFather > /newbot (a separate bot, not @TradeLensagent). Copy its token into `CONNECT_BOT_TOKEN`.
2. @BotFather > /mybots > your test bot > Bot Settings > Menu Button > set the URL to `CONNECT_MINIAPP_URL`.
3. Open the test bot in Telegram and tap the menu button.

## Rollback
Delete `CONNECT_SECRET` in Render (Connect is off again at once). Optional full removal: remove the two main.py lines; tables can stay (see migrations/2026_11_connect.sql for DROP).

## Known differences from the spec doc
Linking sends email and password in the request body, not the website cookie. `bot_start` and `announce_sent` are not recorded in Phase 1.
