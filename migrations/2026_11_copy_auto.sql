-- TradeLens / Driverline: automatic copying of every master trade. ADDITIVE and safe to run more than once. The app also does this itself.
ALTER TABLE copy_flags ADD COLUMN IF NOT EXISTS source TEXT;
CREATE TABLE IF NOT EXISTS copy_auto_seen (master_account_id INTEGER NOT NULL, ticket BIGINT NOT NULL, symbol TEXT, dir TEXT, action TEXT NOT NULL,
    note TEXT, flag_id INTEGER, first_seen BIGINT NOT NULL, updated_at BIGINT NOT NULL, PRIMARY KEY(master_account_id, ticket));
CREATE INDEX IF NOT EXISTS ix_copy_auto_seen_acct ON copy_auto_seen(master_account_id, first_seen);
ALTER TABLE copy_auto_seen ENABLE ROW LEVEL SECURITY;
