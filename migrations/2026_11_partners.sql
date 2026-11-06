-- TradeLens / Driverline: partner referral programme (Master Partner -> TradeLens sub-partners -> referred members).
-- ADDITIVE ONLY and safe to run more than once. Nothing existing is altered. The app creates these tables itself at start-up (app/partners.py).
-- TradeLens keeps its own referral records here. Deriv alone decides partner approval, client tagging and commissions.
CREATE TABLE IF NOT EXISTS tl_partners (id BIGSERIAL PRIMARY KEY, user_id INTEGER UNIQUE, partner_code TEXT UNIQUE NOT NULL, ref_code TEXT UNIQUE NOT NULL,
    is_house INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL, applied_via TEXT, deriv_partner_id TEXT, deriv_referral_link TEXT,
    link_status TEXT NOT NULL DEFAULT 'unverified', link_verified_at BIGINT, verification_source TEXT, verification_date TEXT,
    consent_at BIGINT, approved_at BIGINT, created_at BIGINT NOT NULL, updated_at BIGINT NOT NULL);
CREATE UNIQUE INDEX IF NOT EXISTS ux_tl_partner_house ON tl_partners(is_house) WHERE is_house=1;
CREATE TABLE IF NOT EXISTS tl_partner_referrals (referred_id INTEGER PRIMARY KEY, partner_id INTEGER NOT NULL, code TEXT NOT NULL,
    attribution_status TEXT NOT NULL DEFAULT 'attributed', created_at BIGINT NOT NULL);
CREATE INDEX IF NOT EXISTS ix_tl_partner_ref_partner ON tl_partner_referrals(partner_id);
CREATE TABLE IF NOT EXISTS tl_partner_events (id BIGSERIAL PRIMARY KEY, partner_id INTEGER, user_id INTEGER, kind TEXT NOT NULL, detail TEXT, created_at BIGINT NOT NULL);
CREATE INDEX IF NOT EXISTS ix_tl_partner_ev ON tl_partner_events(partner_id, kind);
CREATE TABLE IF NOT EXISTS tl_partner_consents (id BIGSERIAL PRIMARY KEY, user_id INTEGER NOT NULL, kind TEXT NOT NULL, version TEXT NOT NULL,
    partner_id INTEGER, created_at BIGINT NOT NULL);
CREATE TABLE IF NOT EXISTS tl_partner_verifications (id BIGSERIAL PRIMARY KEY, partner_id INTEGER NOT NULL, kind TEXT NOT NULL, subject TEXT,
    source TEXT NOT NULL, verified_on TEXT NOT NULL, note TEXT, admin_id INTEGER NOT NULL, created_at BIGINT NOT NULL);
CREATE TABLE IF NOT EXISTS tl_partner_audit (id BIGSERIAL PRIMARY KEY, admin_id INTEGER, partner_id INTEGER, action TEXT NOT NULL, detail TEXT, created_at BIGINT NOT NULL);
CREATE TABLE IF NOT EXISTS tl_partner_settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);

-- Supabase exposes every public table through its REST API. Row-level security with no policy closes that door;
-- the app reaches the data directly as the table owner, which is not affected. No partner row can be read or changed by a browser.
ALTER TABLE tl_partners ENABLE ROW LEVEL SECURITY;
ALTER TABLE tl_partner_referrals ENABLE ROW LEVEL SECURITY;
ALTER TABLE tl_partner_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE tl_partner_consents ENABLE ROW LEVEL SECURITY;
ALTER TABLE tl_partner_verifications ENABLE ROW LEVEL SECURITY;
ALTER TABLE tl_partner_audit ENABLE ROW LEVEL SECURITY;
ALTER TABLE tl_partner_settings ENABLE ROW LEVEL SECURITY;

-- Everything starts OFF: with no 'mode' row the programme is off (only admins see anything, nothing is recorded).
-- Rollback (only if the tables are empty or you accept losing the records): DROP TABLE tl_partner_settings, tl_partner_audit, tl_partner_verifications,
--   tl_partner_consents, tl_partner_events, tl_partner_referrals, tl_partners;
