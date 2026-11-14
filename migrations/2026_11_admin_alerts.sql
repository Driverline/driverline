-- TradeLens / Driverline: alerts for the admins. ADDITIVE and safe to run more than once. The app creates it itself (app/admin_alerts.py).
CREATE TABLE IF NOT EXISTS admin_alerts (id BIGSERIAL PRIMARY KEY, kind TEXT NOT NULL, user_id INTEGER, title TEXT NOT NULL, body TEXT NOT NULL,
    link TEXT, ref TEXT, created_at BIGINT NOT NULL, read_at BIGINT);
CREATE UNIQUE INDEX IF NOT EXISTS ux_admin_alert_ref ON admin_alerts(kind, ref);
CREATE INDEX IF NOT EXISTS ix_admin_alert_unread ON admin_alerts(read_at, id);

-- Supabase exposes every public table through its REST API. Row-level security with no policy closes that door;
-- the app reaches the data directly as the table owner, which is not affected.
ALTER TABLE admin_alerts ENABLE ROW LEVEL SECURITY;
