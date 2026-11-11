# Price alerts

A member picks a symbol and a price; TradeLens sends a push notification when the price gets CLOSE to it, so a manual trader has time to open MT5 and decide (enter, move a take profit, or leave it). No MT5 connection is needed.

## How it works
- Prices: Deriv's public feed (the same one the signal scanner uses). The member's own MT5 price can differ slightly, which is why the alert fires early. The message says so.
- Early warning: worked out when the alert is created from the symbol's average 15-minute candle range over the last 12 hours (R). Options: A little early = 1R, Early = 2R (default), Very early = 4R before the level. Never more than half the way from the current price to the level. A level closer than 1R to the current price is refused (it would fire at once).
- Checker (`price_alerts.alert_loop`): every 30 s, one request per symbol that has an active alert (at most 40), reading the last three one-minute candles so a quick wick through the zone is not missed. Only candles from just before the alert was created count. An alert fires once, then is marked `fired`.
- Delivery: push to the member's devices; if no device received it (none registered, or the browser removed it), an inbox message is left instead, and the page says so.
- Entitlement: the same as notifications (`credits.entitled(user, "notify")`: members and admin, or an active Signals plan).

## Limits (so the server is never flooded)
10 active alerts per member, 30 new alerts per 24 hours, one alert per price level and symbol, 1 to 30 days of life (7 default, then `expired`), one price look-up per member every 3 seconds, notes up to 80 characters (control characters and angle brackets removed). `PRICE_ALERTS=0` in Render switches the whole feature off.

## Files
`app/price_alerts.py`, table `price_alerts` (created at start-up, RLS on; `migrations/2026_11_price_alerts.sql`), routes `GET/POST /api/alerts`, `GET /api/alerts/preview`, `POST /api/alerts/{id}/cancel`, `static/alerts.html`, an Alerts tab (`static/auth.js`) and Home tile (`static/index.html`), `tests/test_price_alerts.py`.

## Not done
Repeating alerts, alerts tied to an open MT5 trade ("80% to target"), quiet hours (an alert the member set on purpose is sent whenever it triggers), a per-symbol "market closed" warning for forex and gold.
