-- TradeLens / Driverline: Paystack (Naira) payments. READ-ONLY CHECKS. This file changes nothing.
--
-- Paystack needs NO new tables, columns or indexes. It stores its orders in the existing billing_orders table (method = 'paystack') and its audit
-- trail in the existing payment_events table, using columns that the NOWPayments change (2026_10_nowpayments.sql) already added. Nothing has to be
-- migrated, so there is nothing to roll back in the database: to go back, redeploy the previous code (see docs/PAYSTACK.md).
--
-- Run this in Supabase > SQL Editor if you want to confirm the database is ready. Every row of the first query must say present = true.

SELECT c AS column_name,
       EXISTS (SELECT 1 FROM information_schema.columns
               WHERE table_schema = 'public' AND table_name = 'billing_orders' AND column_name = c) AS present
FROM unnest(ARRAY['provider', 'provider_payment_id', 'provider_status', 'pay_address', 'pay_amount', 'pay_currency', 'pay_network',
                  'actually_paid', 'expires_at', 'fulfilled_at', 'review_reason', 'last_polled_at', 'updated_at', 'terms_at']) AS c;

-- The unique index that stops one provider transaction from settling two orders (must return one row).
SELECT indexname FROM pg_indexes WHERE schemaname = 'public' AND indexname = 'ux_orders_provider_pid';

-- The ledger index that makes a double credit impossible (must return one row).
SELECT indexname FROM pg_indexes WHERE schemaname = 'public' AND indexname = 'ux_ledger_ref';

-- The Naira rate Paystack prices use (must return one row with a positive number, set in the app under Billing settings).
SELECT key, value FROM billing_settings WHERE key = 'ngn_rate';

-- After testing: what test mode left behind (test orders have provider 'paystack-test' and references starting TLN-T-).
SELECT id, user_id, product, status, ref, created_at FROM billing_orders WHERE provider = 'paystack-test' ORDER BY id;
SELECT id, user_id, delta, kind, note FROM credit_ledger WHERE note LIKE '%TLN-T-%' ORDER BY id;
