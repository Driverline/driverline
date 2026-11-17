-- Admin per-trade limit offers that the member must accept. Additive and idempotent (the app also creates this table at start-up).
CREATE TABLE IF NOT EXISTS copy_limit_offers(id BIGSERIAL PRIMARY KEY, account_id INTEGER NOT NULL, user_id INTEGER NOT NULL, from_pct DOUBLE PRECISION,
    offered_pct DOUBLE PRECISION NOT NULL, message TEXT, status TEXT NOT NULL, admin_id INTEGER, created_at BIGINT NOT NULL, decided_at BIGINT);
CREATE INDEX IF NOT EXISTS ix_limit_offers ON copy_limit_offers(account_id, status);
