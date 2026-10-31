-- TradeLens / Driverline: several MT5 accounts per member. ADDITIVE and safe to run more than once.
--
-- You do NOT have to run this by hand: the app applies the same changes itself the first time MT5 is used after the new version starts
-- (app/ea.py: _migrate). Run it yourself (Supabase > SQL Editor) only if you want it in place BEFORE the new code deploys.
-- Nothing here edits or deletes existing rows. The old tables (ea_keys, ea_state, ea_rules, ea_manage) are left exactly as they are.

CREATE TABLE IF NOT EXISTS ea_accounts (id BIGSERIAL PRIMARY KEY, user_id BIGINT NOT NULL, label TEXT NOT NULL, key_hash TEXT NOT NULL UNIQUE,
  created_at BIGINT, last_seen BIGINT, login TEXT);
CREATE TABLE IF NOT EXISTS ea_acct_state (account_id BIGINT PRIMARY KEY, state TEXT, specs TEXT, updated_at BIGINT);
CREATE TABLE IF NOT EXISTS ea_acct_rules (account_id BIGINT PRIMARY KEY, max_risk_pct DOUBLE PRECISION, daily_loss_pct DOUBLE PRECISION, max_positions INTEGER);
CREATE TABLE IF NOT EXISTS ea_acct_manage (account_id BIGINT PRIMARY KEY, settings TEXT);
CREATE TABLE IF NOT EXISTS ea_meta (k TEXT PRIMARY KEY, v TEXT);
ALTER TABLE ea_commands ADD COLUMN IF NOT EXISTS account_id INTEGER;
CREATE UNIQUE INDEX IF NOT EXISTS ux_ea_accounts_label ON ea_accounts(user_id, label);
CREATE INDEX IF NOT EXISTS ix_ea_commands_account ON ea_commands(account_id, status);

-- Each existing connection becomes the member's first account "Main", with the SAME key, so a connected EA keeps working untouched.
INSERT INTO ea_accounts(user_id,label,key_hash,created_at,last_seen)
  SELECT k.user_id,'Main',k.key_hash,k.created_at,k.last_seen FROM ea_keys k
  WHERE NOT EXISTS (SELECT 1 FROM ea_accounts x WHERE x.key_hash=k.key_hash)
    AND NOT EXISTS (SELECT 1 FROM ea_accounts y WHERE y.user_id=k.user_id AND y.label='Main')
  ON CONFLICT DO NOTHING;
INSERT INTO ea_acct_state(account_id,state,specs,updated_at)
  SELECT a.id,s.state,s.specs,s.updated_at FROM ea_state s JOIN ea_accounts a ON a.user_id=s.user_id AND a.label='Main'
  WHERE NOT EXISTS (SELECT 1 FROM ea_acct_state z WHERE z.account_id=a.id);
INSERT INTO ea_acct_rules(account_id,max_risk_pct,daily_loss_pct,max_positions)
  SELECT a.id,r.max_risk_pct,r.daily_loss_pct,r.max_positions FROM ea_rules r JOIN ea_accounts a ON a.user_id=r.user_id AND a.label='Main'
  WHERE NOT EXISTS (SELECT 1 FROM ea_acct_rules z WHERE z.account_id=a.id);
INSERT INTO ea_acct_manage(account_id,settings)
  SELECT a.id,m.settings FROM ea_manage m JOIN ea_accounts a ON a.user_id=m.user_id AND a.label='Main'
  WHERE NOT EXISTS (SELECT 1 FROM ea_acct_manage z WHERE z.account_id=a.id);
-- Do not run the four copies above again after members have removed accounts: a removed "Main" would be brought back. The app guards this
-- itself with a marker row; by hand, run them once only.
