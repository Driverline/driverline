-- TradeLens / Driverline: "Pay with Naira" (manual Moniepoint bank transfers). ADDITIVE and safe to run more than once.
--
-- You do NOT have to run this by hand: the app applies exactly the same changes by itself the first time billing is used after the new
-- version starts (app/credits.py: _migrate). Run it yourself (Supabase > SQL Editor) only if you want the change in place BEFORE the new
-- code deploys. Nothing here edits or deletes existing rows, orders, balances, subscriptions or invites, and no table is created or dropped.
--
-- Back up first if you like (Supabase > Database > Backups): billing_orders, billing_subs, credit_ledger, credit_balances, billing_settings.

-- 1. The payment narration of an order (for example TL-7K2M9P4Q). Nullable: every existing order keeps NULL.
ALTER TABLE billing_orders ADD COLUMN IF NOT EXISTS narration TEXT;

-- 2. A narration identifies one order only.
CREATE UNIQUE INDEX IF NOT EXISTS ux_orders_narration ON billing_orders(narration) WHERE narration IS NOT NULL;

-- 3. One verified bank transaction reference (billing_orders.txid) can settle one Naira order only. It does not touch manual USDT orders,
--    whose txid is whatever the member typed.
CREATE UNIQUE INDEX IF NOT EXISTS ux_orders_bank_txid ON billing_orders(txid) WHERE method = 'moniepoint' AND txid IS NOT NULL;

-- Everything else Pay with Naira uses already exists: billing_orders (method = 'moniepoint'), payment_events (audit trail), credit_ledger and
-- billing_subs (through credits.grant_product), billing_settings (ngn_rate, bank_name, bank_account_name, bank_account_number, support,
-- and one new row naira_enabled written by Admin > Naira payments).
