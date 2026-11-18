-- Plan expiry reminders: one row per notice sent. Additive and idempotent (the app also creates this table at start-up).
CREATE TABLE IF NOT EXISTS expiry_notices(user_id INTEGER NOT NULL, service TEXT NOT NULL, stage TEXT NOT NULL, end_ts BIGINT NOT NULL, sent_at BIGINT NOT NULL,
    PRIMARY KEY(user_id, service, stage, end_ts));
