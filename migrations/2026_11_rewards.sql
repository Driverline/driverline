-- TradeLens / Driverline: referrals, points and levels (Phase 1). ADDITIVE and safe to run more than once. The app creates it itself (app/rewards.py).
CREATE TABLE IF NOT EXISTS ref_codes (user_id INTEGER PRIMARY KEY, code TEXT UNIQUE NOT NULL, created_at BIGINT NOT NULL);
CREATE TABLE IF NOT EXISTS referrals (referred_id INTEGER PRIMARY KEY, referrer_id INTEGER NOT NULL, code TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending', created_at BIGINT NOT NULL, qualified_at BIGINT);
CREATE INDEX IF NOT EXISTS ix_referrals_referrer ON referrals(referrer_id);
CREATE TABLE IF NOT EXISTS points_ledger (id BIGSERIAL PRIMARY KEY, user_id INTEGER NOT NULL, delta INTEGER NOT NULL, lifetime INTEGER NOT NULL,
    kind TEXT NOT NULL, ref TEXT NOT NULL, day TEXT, note TEXT, admin_id INTEGER, created_at BIGINT NOT NULL, UNIQUE(user_id, kind, ref));
CREATE INDEX IF NOT EXISTS ix_points_user ON points_ledger(user_id, id);
CREATE INDEX IF NOT EXISTS ix_points_day ON points_ledger(user_id, kind, day);
CREATE TABLE IF NOT EXISTS points_balances (user_id INTEGER PRIMARY KEY, spendable INTEGER NOT NULL DEFAULT 0, lifetime INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS rewards_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);

-- Supabase exposes every public table through its REST API. Row-level security with no policy closes that door;
-- the app reaches the data directly as the table owner, which is not affected.
ALTER TABLE ref_codes ENABLE ROW LEVEL SECURITY;
ALTER TABLE referrals ENABLE ROW LEVEL SECURITY;
ALTER TABLE points_ledger ENABLE ROW LEVEL SECURITY;
ALTER TABLE points_balances ENABLE ROW LEVEL SECURITY;
ALTER TABLE rewards_meta ENABLE ROW LEVEL SECURITY;
