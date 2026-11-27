-- TradeLens Connect: record of Telegram signal alerts, so Telegram can be sent IN ADDITION to web push.
-- ADDITIVE and safe to run more than once. Touches no existing table. The app also creates it itself (app/connect.py SCHEMA).
CREATE TABLE IF NOT EXISTS connect_alert_sent (user_id INTEGER NOT NULL, signal_id INTEGER NOT NULL, day TEXT NOT NULL,
    sent_at BIGINT NOT NULL, PRIMARY KEY (user_id, signal_id));
CREATE INDEX IF NOT EXISTS ix_calert_day ON connect_alert_sent(user_id, day);

-- Supabase exposes every public table through its REST API. Row-level security with no policy closes that door;
-- the app reaches the data directly as the table owner, which is not affected.
ALTER TABLE connect_alert_sent ENABLE ROW LEVEL SECURITY;
