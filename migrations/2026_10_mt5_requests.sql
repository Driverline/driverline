-- TradeLens / Driverline: MT5 connection requests. ADDITIVE and safe to run more than once. The app creates the same tables itself on first use.
CREATE TABLE IF NOT EXISTS mt5_requests (id BIGSERIAL PRIMARY KEY, user_id BIGINT NOT NULL, label TEXT NOT NULL, login TEXT NOT NULL, server TEXT NOT NULL,
  pw_enc TEXT, status TEXT NOT NULL, note TEXT, account_id BIGINT, created_at BIGINT NOT NULL, updated_at BIGINT NOT NULL, pw_wiped_at BIGINT);
CREATE TABLE IF NOT EXISTS mt5_request_log (id BIGSERIAL PRIMARY KEY, request_id BIGINT NOT NULL, admin_id BIGINT, action TEXT NOT NULL, created_at BIGINT NOT NULL);
CREATE INDEX IF NOT EXISTS ix_mt5_requests_status ON mt5_requests(status, user_id);
