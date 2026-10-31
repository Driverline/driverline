-- TradeLens / Driverline: Signals Plus + IB "Signals or Copy trading" switch. ADDITIVE and safe to run more than once. The app creates it itself.
CREATE TABLE IF NOT EXISTS plan_mode (user_id BIGINT PRIMARY KEY, mode TEXT NOT NULL, since BIGINT, carry DOUBLE PRECISION NOT NULL DEFAULT 0, updated_at BIGINT NOT NULL);
-- The new "Copy Trading" product needs no database change: it is a plan row in billing_subs like Plus and Pro.
