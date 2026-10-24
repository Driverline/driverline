-- TradeLens / Driverline: NOWPayments automatic crypto payments. ADDITIVE and safe to run more than once.
--
-- You do NOT have to run this by hand: the app applies exactly the same changes by itself the first time billing is used after
-- the new version starts (app/credits.py: SCHEMA and _migrate). Run it yourself (Supabase > SQL Editor) only if you want the
-- change in place BEFORE the new code deploys. Nothing here edits or deletes existing rows, orders, balances or subscriptions.
--
-- Back up first (Supabase > Database > Backups, or export these tables): billing_orders, billing_subs, credit_ledger,
-- credit_balances, billing_settings, users.

-- 1. New columns on the existing orders table. All nullable: existing (manual) orders keep NULL in each.
ALTER TABLE billing_orders ADD COLUMN IF NOT EXISTS provider            TEXT;
ALTER TABLE billing_orders ADD COLUMN IF NOT EXISTS provider_payment_id TEXT;
ALTER TABLE billing_orders ADD COLUMN IF NOT EXISTS provider_status     TEXT;
ALTER TABLE billing_orders ADD COLUMN IF NOT EXISTS pay_address         TEXT;
ALTER TABLE billing_orders ADD COLUMN IF NOT EXISTS pay_amount          DOUBLE PRECISION;
ALTER TABLE billing_orders ADD COLUMN IF NOT EXISTS pay_currency        TEXT;
ALTER TABLE billing_orders ADD COLUMN IF NOT EXISTS pay_network         TEXT;
ALTER TABLE billing_orders ADD COLUMN IF NOT EXISTS actually_paid       DOUBLE PRECISION;
ALTER TABLE billing_orders ADD COLUMN IF NOT EXISTS expires_at          BIGINT;
ALTER TABLE billing_orders ADD COLUMN IF NOT EXISTS fulfilled_at        BIGINT;
ALTER TABLE billing_orders ADD COLUMN IF NOT EXISTS review_reason       TEXT;
ALTER TABLE billing_orders ADD COLUMN IF NOT EXISTS last_polled_at      BIGINT;
ALTER TABLE billing_orders ADD COLUMN IF NOT EXISTS updated_at          BIGINT;

-- 2. One provider payment can belong to one order only (database-level guard against cross-matching).
CREATE UNIQUE INDEX IF NOT EXISTS ux_orders_provider_pid ON billing_orders(provider_payment_id) WHERE provider_payment_id IS NOT NULL;

-- 3. Audit trail of everything that happens to an automatic order.
CREATE TABLE IF NOT EXISTS payment_events(
    id BIGSERIAL PRIMARY KEY, order_id BIGINT, provider_payment_id TEXT, source TEXT NOT NULL, kind TEXT NOT NULL,
    provider_status TEXT, detail TEXT, dedupe_key TEXT UNIQUE, admin_id INTEGER, created_at BIGINT NOT NULL);
CREATE INDEX IF NOT EXISTS ix_pevents_order ON payment_events(order_id, id);
CREATE INDEX IF NOT EXISTS ix_pevents_kind  ON payment_events(kind, id);

-- 4. Same protection as every other table: close Supabase's public REST API to it. The app reaches the data directly as the
--    table owner, which row-level security does not affect.
ALTER TABLE payment_events ENABLE ROW LEVEL SECURITY;
