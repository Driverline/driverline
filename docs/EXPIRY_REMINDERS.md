# Plan expiry reminders

`app/expiry_reminders.py`, started with the app (every 30 minutes; `DRIVERLINE_EXPIRY=0` switches it off).

For each member and each service (signal notifications, MT5 connection incl. AutoTrading, copy trading) the end is the latest end among all plans that include it, plus copy-trading grants (`copy_subs`). Stages: `3d` (3 to 1 day before), `1d` (last day), `ended` (ended within the last 3 days; older lapses are never announced). Each (member, service, stage, end) is recorded in `expiry_notices` before sending, so it is sent once; renewing changes the end and starts a new cycle. Everything due in one run is ONE message: Messages (inbox) plus a push to the member's devices, linking to Billing.
Not sent: billing off, blocked members, admins, members for notifications and MT5 (they keep these regardless of plans), a service something else still provides, members inside their quiet hours (retried on the next run).
Admin: Admin > Payments > "Expiring plans" (`GET /api/admin/expiring`): services ending in 7 days or ended in 3.
Table `expiry_notices` is created by the app (migrations/2026_11_expiry_notices.sql). Tests: `python3 tests/test_expiry_reminders.py`.
