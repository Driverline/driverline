-- Member wallet (app/wallet.py). Additive; the app creates these itself on first use. Safe to run twice. Back up first.
CREATE TABLE IF NOT EXISTS wallet_balances(user_id INTEGER PRIMARY KEY, balance_cents BIGINT NOT NULL DEFAULT 0 CHECK (balance_cents >= 0));
CREATE TABLE IF NOT EXISTS wallet_ledger(id BIGSERIAL PRIMARY KEY, user_id INTEGER NOT NULL, delta_cents BIGINT NOT NULL, kind TEXT NOT NULL,
    ref TEXT NOT NULL, note TEXT, admin_id INTEGER, created_at BIGINT NOT NULL);
CREATE UNIQUE INDEX IF NOT EXISTS ux_wallet_ledger_ref ON wallet_ledger(user_id, kind, ref);
CREATE INDEX IF NOT EXISTS ix_wallet_ledger_user ON wallet_ledger(user_id, id);
-- Rollback (only while no wallet holds money): DROP TABLE wallet_ledger; DROP TABLE wallet_balances; DELETE FROM billing_settings WHERE key='wallet_mode';
