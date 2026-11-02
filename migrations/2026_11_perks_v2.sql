-- TradeLens / Driverline: perks v2 (3 welcome signals, partner members get 5 in total, 3 founder places claimed by funding).
-- ADDITIVE and safe to run more than once. The app applies the same statements itself the first time it starts (app/perks.py _migrate).
-- No signals, ledger rows, balances or subscriptions are changed or removed by this file.

-- The MT5 login that verified a founder: one funded MT5 account can verify only one founder.
ALTER TABLE partner_perks ADD COLUMN IF NOT EXISTS founder_login TEXT;
CREATE UNIQUE INDEX IF NOT EXISTS ux_perks_login ON partner_perks(founder_login) WHERE founder_login IS NOT NULL;

-- Founder places used to be reserved when a partner was confirmed (ranks 1..10). They are now claimed when the member shows the
-- funded balance, so a member still waiting holds no rank. Members already verified keep theirs. (Today: 3 members are waiting.)
UPDATE partner_perks SET founder_rank = NULL WHERE founder_status = 'pending';
