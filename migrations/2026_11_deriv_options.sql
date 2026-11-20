-- TradeLens / Driverline: Deriv options (Rise/Fall, Accumulators) and options AutoTrading. ADDITIVE and safe to run more than once.
-- The app creates these tables itself the first time app/deriv_options.py runs; this file is for running them by hand in Supabase.
CREATE TABLE IF NOT EXISTS opt_meta (k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS opt_settings (user_id INTEGER PRIMARY KEY, cfg TEXT NOT NULL, auto_on INTEGER NOT NULL DEFAULT 0, account_id TEXT,
    account_type TEXT, session_started BIGINT, session_ends BIGINT, reset_at BIGINT, last_note TEXT, updated_at BIGINT NOT NULL);
CREATE TABLE IF NOT EXISTS opt_trades (id BIGSERIAL PRIMARY KEY, user_id INTEGER NOT NULL, account_id TEXT NOT NULL, account_type TEXT NOT NULL,
    symbol TEXT NOT NULL, strategy TEXT NOT NULL, contract_type TEXT NOT NULL, duration INTEGER, stake DOUBLE PRECISION NOT NULL,
    ask_price DOUBLE PRECISION, payout DOUBLE PRECISION, buy_price DOUBLE PRECISION, contract_id TEXT, step INTEGER NOT NULL DEFAULT 0,
    source TEXT NOT NULL, status TEXT NOT NULL, error TEXT, result_status TEXT, profit DOUBLE PRECISION, created_at BIGINT NOT NULL, closed_at BIGINT);
CREATE INDEX IF NOT EXISTS ix_opt_trades_user ON opt_trades(user_id, id);
CREATE TABLE IF NOT EXISTS opt_validation (strategy TEXT NOT NULL, symbol TEXT NOT NULL, duration INTEGER NOT NULL, n INTEGER NOT NULL,
    wins INTEGER NOT NULL, hit DOUBLE PRECISION NOT NULL, ratio DOUBLE PRECISION NOT NULL, breakeven DOUBLE PRECISION NOT NULL,
    lower DOUBLE PRECISION NOT NULL, ev DOUBLE PRECISION NOT NULL, edge INTEGER NOT NULL, bars INTEGER NOT NULL, updated_at BIGINT NOT NULL,
    PRIMARY KEY (strategy, symbol, duration));
CREATE TABLE IF NOT EXISTS opt_events (id BIGSERIAL PRIMARY KEY, user_id INTEGER NOT NULL, kind TEXT NOT NULL, detail TEXT, created_at BIGINT NOT NULL);
CREATE INDEX IF NOT EXISTS ix_opt_events_user ON opt_events(user_id, id);

-- Row-level security with no policy keeps Supabase's public REST API away from these tables; the app reaches them directly as the owner.
ALTER TABLE opt_meta ENABLE ROW LEVEL SECURITY;
ALTER TABLE opt_settings ENABLE ROW LEVEL SECURITY;
ALTER TABLE opt_trades ENABLE ROW LEVEL SECURITY;
ALTER TABLE opt_validation ENABLE ROW LEVEL SECURITY;
ALTER TABLE opt_events ENABLE ROW LEVEL SECURITY;
