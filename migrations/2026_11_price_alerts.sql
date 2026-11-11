-- TradeLens / Driverline: price alerts. ADDITIVE and safe to run more than once. The app creates it itself (app/price_alerts.py).
CREATE TABLE IF NOT EXISTS price_alerts (id BIGSERIAL PRIMARY KEY, user_id INTEGER NOT NULL, instrument TEXT NOT NULL, level DOUBLE PRECISION NOT NULL,
    dir TEXT NOT NULL, buffer DOUBLE PRECISION NOT NULL, trigger_price DOUBLE PRECISION NOT NULL, price0 DOUBLE PRECISION, early INTEGER NOT NULL,
    note TEXT, status TEXT NOT NULL, created_at BIGINT NOT NULL, expires_at BIGINT NOT NULL, fired_at BIGINT, fired_price DOUBLE PRECISION, delivered INTEGER);
CREATE INDEX IF NOT EXISTS ix_price_alerts_status ON price_alerts(status, instrument);
CREATE INDEX IF NOT EXISTS ix_price_alerts_user ON price_alerts(user_id, id);

-- Row-level security with no policy keeps Supabase's public REST API away from the table; the app reaches it directly as the owner.
ALTER TABLE price_alerts ENABLE ROW LEVEL SECURITY;
