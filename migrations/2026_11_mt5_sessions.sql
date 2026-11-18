-- Journal: one row per period in which an MT5 login was connected. Additive and idempotent (the app also creates and backfills this at start-up).
CREATE TABLE IF NOT EXISTS mt5_sessions(id BIGSERIAL PRIMARY KEY, user_id INTEGER NOT NULL, ea_account_id INTEGER NOT NULL, login TEXT NOT NULL, server TEXT,
    started_at BIGINT NOT NULL, last_seen BIGINT NOT NULL);
CREATE INDEX IF NOT EXISTS ix_mt5_sessions ON mt5_sessions(user_id, login);
INSERT INTO mt5_sessions(user_id,ea_account_id,login,server,started_at,last_seen)
SELECT a.user_id,a.id,a.login,NULL,a.created_at,COALESCE(a.last_seen,a.created_at) FROM ea_accounts a
WHERE a.login IS NOT NULL AND a.login<>'' AND NOT EXISTS (SELECT 1 FROM mt5_sessions s WHERE s.ea_account_id=a.id);
