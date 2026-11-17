-- Copy trading: automatic set-up and proportional sizing. Additive and idempotent (the app also adds these columns itself at start-up).
ALTER TABLE copy_flags   ADD COLUMN IF NOT EXISTS master_lots DOUBLE PRECISION;
ALTER TABLE copy_flags   ADD COLUMN IF NOT EXISTS master_risk_pct DOUBLE PRECISION;
ALTER TABLE copy_members ADD COLUMN IF NOT EXISTS how TEXT;
