-- TradeLens / Driverline: in-app messages and broadcasts. ADDITIVE and safe to run more than once. The app creates it itself (app/inbox.py).
CREATE TABLE IF NOT EXISTS inbox_messages (id BIGSERIAL PRIMARY KEY, user_id INTEGER NOT NULL, sender TEXT NOT NULL, admin_id INTEGER,
    body TEXT NOT NULL, created_at BIGINT NOT NULL, read_at BIGINT);
CREATE INDEX IF NOT EXISTS ix_inbox_user ON inbox_messages(user_id, id);
CREATE INDEX IF NOT EXISTS ix_inbox_unread ON inbox_messages(sender, read_at);
CREATE TABLE IF NOT EXISTS inbox_broadcasts (id BIGSERIAL PRIMARY KEY, admin_id INTEGER NOT NULL, title TEXT, body TEXT NOT NULL, created_at BIGINT NOT NULL);
CREATE TABLE IF NOT EXISTS inbox_broadcast_reads (user_id INTEGER PRIMARY KEY, last_id BIGINT NOT NULL DEFAULT 0, updated_at BIGINT NOT NULL);

-- Supabase exposes every public table through its REST API. Row-level security with no policy closes that door;
-- the app reaches the data directly as the table owner, which is not affected.
ALTER TABLE inbox_messages ENABLE ROW LEVEL SECURITY;
ALTER TABLE inbox_broadcasts ENABLE ROW LEVEL SECURITY;
ALTER TABLE inbox_broadcast_reads ENABLE ROW LEVEL SECURITY;
