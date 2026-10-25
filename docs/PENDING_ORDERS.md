# Pending orders on NO TRADE cards

A NO TRADE card now has a **Pending orders** section under the "What to wait for" picture.

- `app/pending_orders.py` builds up to six orders (buy/sell limit, buy/sell stop, buy/sell stop limit) from the ceiling, floor and
  average already drawn on the picture, with the stop loss and take profit in ATR steps. An order is offered only if it is a valid
  MT5 order and its reward is at least 1.5x its risk. Types that are not offered are listed with the reason.
  Spike risk offers nothing; "overextended" offers only the pullback order.
- Each order carries a signed token. The browser sends back only the token, a lot size and an expiry, so prices cannot be edited.
  Tokens go stale after `DRIVERLINE_PENDING_TTL_MIN` minutes (default 180).
- `POST /api/pending/place` applies the member's own rules (daily loss stop, max positions counting waiting orders, max risk %
  at the chosen lot size), then queues one `pending_order` command for the member's own EA.
- The EA (`DriverlineCompanion.mq5` v1.40) needs the new input **AllowPending = true**. It places a real broker-side pending order
  with stop loss, take profit and an expiry, so MT5 fires it even if the app is closed. It reports waiting orders back, can cancel
  them (`cancel_order`), removes expired orders itself when the broker cannot hold an expiry, and removes a limit order whose
  take profit was reached without a fill. Recompile the EA in MetaEditor and re-attach it.
- Waiting orders are listed on the card and on the MT5 page, each with Cancel.

Lot size works like "Open trade in MT5": type one, or leave it empty for the symbol's smallest lot. Risk above your own rule is a warning, not a block.

Limits: MT5 only (Deriv multiplier contracts cannot wait for a price). Stop orders fire on touch, not on a candle close.
Run `python tests/test_pending_orders.py`.
