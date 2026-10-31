-- TradeLens / Driverline: copy trading. ADDITIVE and safe to run more than once. Five new tables and two indexes; nothing existing is changed.
-- The app creates the same tables itself on first use (app/ea.py), so running this by hand is optional.
CREATE TABLE IF NOT EXISTS copy_subs (user_id BIGINT PRIMARY KEY, ends_at BIGINT NOT NULL, note TEXT, admin_id BIGINT, updated_at BIGINT NOT NULL);
CREATE TABLE IF NOT EXISTS copy_members (account_id BIGINT PRIMARY KEY, user_id BIGINT NOT NULL, enabled INTEGER NOT NULL DEFAULT 0,
  mode TEXT NOT NULL DEFAULT 'fixed', fixed_lots DOUBLE PRECISION, risk_pct DOUBLE PRECISION, max_risk_pct DOUBLE PRECISION, updated_at BIGINT NOT NULL);
CREATE TABLE IF NOT EXISTS copy_flags (id BIGSERIAL PRIMARY KEY, master_account_id BIGINT NOT NULL, master_ticket BIGINT NOT NULL, symbol TEXT NOT NULL,
  dir TEXT NOT NULL, open_price DOUBLE PRECISION NOT NULL, sl DOUBLE PRECISION NOT NULL, tp DOUBLE PRECISION NOT NULL, status TEXT NOT NULL,
  admin_id BIGINT, created_at BIGINT NOT NULL, closed_at BIGINT);
CREATE TABLE IF NOT EXISTS copy_jobs (id BIGSERIAL PRIMARY KEY, flag_id BIGINT NOT NULL, account_id BIGINT NOT NULL, user_id BIGINT NOT NULL,
  status TEXT NOT NULL, note TEXT, command_id BIGINT, lots DOUBLE PRECISION, risk_pct DOUBLE PRECISION, ticket BIGINT, before TEXT, symbol TEXT, dir TEXT,
  close_req INTEGER NOT NULL DEFAULT 0, close_cmd BIGINT, close_tries INTEGER NOT NULL DEFAULT 0, created_at BIGINT NOT NULL, updated_at BIGINT NOT NULL);
CREATE UNIQUE INDEX IF NOT EXISTS ux_copy_jobs_flag_acct ON copy_jobs(flag_id, account_id);
CREATE INDEX IF NOT EXISTS ix_copy_jobs_acct ON copy_jobs(account_id, status);
CREATE INDEX IF NOT EXISTS ix_copy_flags_status ON copy_flags(status);
