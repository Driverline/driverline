-- Rollback for 2026_10_naira.sql. You almost never need this: the additions are unused (and harmless) while Pay with Naira is switched Off in
-- Admin, and the previous code version simply ignores them. Use it only to remove them completely.
--
-- READ FIRST: this deletes the narrations of Naira orders. If any exist, export them first:
--   SELECT * FROM billing_orders WHERE method = 'moniepoint';
-- The credits already given (credit_ledger, credit_balances, billing_subs) are NOT touched by this script.
-- Deploy the OLD code first, then run this (the new code needs the column).

DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM billing_orders WHERE method = 'moniepoint') THEN
    RAISE EXCEPTION 'Naira orders exist. Export them, then remove this check if you really want to drop their narrations.';
  END IF;
END $$;

DROP INDEX IF EXISTS ux_orders_bank_txid;
DROP INDEX IF EXISTS ux_orders_narration;
ALTER TABLE billing_orders DROP COLUMN IF EXISTS narration;
DELETE FROM billing_settings WHERE key = 'naira_enabled';
