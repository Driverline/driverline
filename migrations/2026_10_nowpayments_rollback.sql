-- Rollback for 2026_10_nowpayments.sql. You almost never need this: the additions are unused (and harmless) while the feature is
-- switched Off in Admin, and the previous code version simply ignores them. Use it only to remove them completely.
--
-- READ FIRST: this deletes the audit trail and the provider details of automatic orders. If any exist, export them first:
--   SELECT * FROM billing_orders WHERE method = 'nowpayments';   SELECT * FROM payment_events;
-- The credits already given (credit_ledger, credit_balances, billing_subs) are NOT touched by this script.
-- Deploy the OLD code first, then run this (the new code needs these columns).

DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM billing_orders WHERE method = 'nowpayments') THEN
    RAISE EXCEPTION 'Automatic orders exist. Export them, then remove this check if you really want to drop their details.';
  END IF;
END $$;

DROP INDEX IF EXISTS ux_orders_provider_pid;
ALTER TABLE billing_orders
  DROP COLUMN IF EXISTS provider, DROP COLUMN IF EXISTS provider_payment_id, DROP COLUMN IF EXISTS provider_status,
  DROP COLUMN IF EXISTS pay_address, DROP COLUMN IF EXISTS pay_amount, DROP COLUMN IF EXISTS pay_currency,
  DROP COLUMN IF EXISTS pay_network, DROP COLUMN IF EXISTS actually_paid, DROP COLUMN IF EXISTS expires_at,
  DROP COLUMN IF EXISTS fulfilled_at, DROP COLUMN IF EXISTS review_reason, DROP COLUMN IF EXISTS last_polled_at,
  DROP COLUMN IF EXISTS updated_at;
DROP TABLE IF EXISTS payment_events;
DELETE FROM billing_settings WHERE key = 'nowpayments_enabled';
