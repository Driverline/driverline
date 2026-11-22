-- TradeLens Connect (app/connect.py): Telegram account links and funnel events. Additive and idempotent; the app creates these itself on first use. Back up first.
-- user_id has no foreign key on purpose, like every other table in this app (credits, wallet): users.id is an integer and rows here only ever reference it.
CREATE TABLE IF NOT EXISTS telegram_links(id BIGSERIAL PRIMARY KEY, user_id INTEGER NOT NULL, telegram_id BIGINT NOT NULL,
    telegram_username TEXT, source TEXT NOT NULL, linked_at BIGINT NOT NULL, last_seen_at BIGINT NOT NULL, revoked_at BIGINT);
CREATE UNIQUE INDEX IF NOT EXISTS ux_tglink_telegram ON telegram_links(telegram_id) WHERE revoked_at IS NULL;
CREATE UNIQUE INDEX IF NOT EXISTS ux_tglink_user ON telegram_links(user_id) WHERE revoked_at IS NULL;
CREATE TABLE IF NOT EXISTS connect_events(id BIGSERIAL PRIMARY KEY, telegram_id BIGINT, user_id INTEGER, event TEXT NOT NULL,
    props TEXT, created_at BIGINT NOT NULL);
CREATE INDEX IF NOT EXISTS ix_cevents_event ON connect_events(event, created_at);
-- Supabase exposes every public table over its REST API: keep these closed (the app reaches them directly as the table owner).
ALTER TABLE telegram_links ENABLE ROW LEVEL SECURITY;
ALTER TABLE connect_events ENABLE ROW LEVEL SECURITY;
-- Rollback (loses every link and event; users simply link again): DROP TABLE connect_events; DROP TABLE telegram_links;
