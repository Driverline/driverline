-- TradeLens / Driverline: referral commission and prizes (Phase 3). ADDITIVE and safe to run more than once. The app creates these itself.
CREATE TABLE IF NOT EXISTS commission_rows (id BIGSERIAL PRIMARY KEY, period TEXT NOT NULL, client_id TEXT NOT NULL, referred_id INTEGER NOT NULL,
    referrer_id INTEGER NOT NULL, commission_cents BIGINT NOT NULL, pct INTEGER NOT NULL, share_cents BIGINT NOT NULL, import_id INTEGER,
    created_at BIGINT NOT NULL, UNIQUE(period, client_id));
CREATE INDEX IF NOT EXISTS ix_comm_referrer ON commission_rows(referrer_id, period);
CREATE TABLE IF NOT EXISTS commission_imports (id BIGSERIAL PRIMARY KEY, period TEXT NOT NULL, admin_id INTEGER, filename TEXT, rows_total INTEGER NOT NULL,
    rows_shared INTEGER NOT NULL, commission_cents BIGINT NOT NULL, share_cents BIGINT NOT NULL, replaced INTEGER NOT NULL DEFAULT 0, created_at BIGINT NOT NULL);
CREATE TABLE IF NOT EXISTS payout_details (user_id INTEGER PRIMARY KEY, network TEXT NOT NULL, address TEXT NOT NULL, updated_at BIGINT NOT NULL);
CREATE TABLE IF NOT EXISTS payout_requests (id BIGSERIAL PRIMARY KEY, user_id INTEGER NOT NULL, amount_cents BIGINT NOT NULL, network TEXT NOT NULL,
    address TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'requested', txid TEXT, note TEXT, created_at BIGINT NOT NULL, decided_at BIGINT, admin_id INTEGER);
CREATE INDEX IF NOT EXISTS ix_payout_user ON payout_requests(user_id, id);
CREATE UNIQUE INDEX IF NOT EXISTS ux_payout_open ON payout_requests(user_id) WHERE status='requested';

CREATE TABLE IF NOT EXISTS prizes (id BIGSERIAL PRIMARY KEY, key TEXT UNIQUE NOT NULL, name TEXT NOT NULL, description TEXT, points INTEGER NOT NULL,
    cost_usd DOUBLE PRECISION NOT NULL DEFAULT 0, stock INTEGER, active INTEGER NOT NULL DEFAULT 1, sort INTEGER NOT NULL DEFAULT 0, created_at BIGINT NOT NULL);
CREATE TABLE IF NOT EXISTS redemptions (id BIGSERIAL PRIMARY KEY, user_id INTEGER NOT NULL, prize_id INTEGER NOT NULL, prize_name TEXT NOT NULL,
    points INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'requested', full_name TEXT, phone TEXT, address TEXT, note TEXT,
    created_at BIGINT NOT NULL, decided_at BIGINT, admin_id INTEGER);
CREATE INDEX IF NOT EXISTS ix_redeem_user ON redemptions(user_id, id);
CREATE UNIQUE INDEX IF NOT EXISTS ux_redeem_open ON redemptions(user_id) WHERE status='requested';

-- Row-level security with no policy keeps Supabase's public REST API away from these tables (they hold payout addresses and delivery details).
ALTER TABLE commission_rows ENABLE ROW LEVEL SECURITY;
ALTER TABLE commission_imports ENABLE ROW LEVEL SECURITY;
ALTER TABLE payout_details ENABLE ROW LEVEL SECURITY;
ALTER TABLE payout_requests ENABLE ROW LEVEL SECURITY;
ALTER TABLE prizes ENABLE ROW LEVEL SECURITY;
ALTER TABLE redemptions ENABLE ROW LEVEL SECURITY;
