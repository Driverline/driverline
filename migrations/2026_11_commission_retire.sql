-- TradeLens / Driverline: retire commission sharing. NON-DESTRUCTIVE: no row, table or column is deleted or changed.
-- It adds a guard that REFUSES every new row in the four historical tables, so the retired system cannot create a new liability or payout.
-- Safe to run more than once. The app installs the same guard itself at start-up (app/commission.py).
-- Run SELECT-only checks first:  select count(*) from commission_rows; ... payout_requests;   (all were 0 on 2026-11-06)
CREATE OR REPLACE FUNCTION commission_retired() RETURNS trigger AS $$
BEGIN
  RAISE EXCEPTION 'Commission sharing has been retired: no new commission or payout records can be created.';
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_commission_rows_retired ON commission_rows;
CREATE TRIGGER trg_commission_rows_retired BEFORE INSERT ON commission_rows FOR EACH ROW EXECUTE FUNCTION commission_retired();
DROP TRIGGER IF EXISTS trg_commission_imports_retired ON commission_imports;
CREATE TRIGGER trg_commission_imports_retired BEFORE INSERT ON commission_imports FOR EACH ROW EXECUTE FUNCTION commission_retired();
DROP TRIGGER IF EXISTS trg_payout_details_retired ON payout_details;
CREATE TRIGGER trg_payout_details_retired BEFORE INSERT ON payout_details FOR EACH ROW EXECUTE FUNCTION commission_retired();
DROP TRIGGER IF EXISTS trg_payout_requests_retired ON payout_requests;
CREATE TRIGGER trg_payout_requests_retired BEFORE INSERT ON payout_requests FOR EACH ROW EXECUTE FUNCTION commission_retired();

-- Rollback: DROP TRIGGER trg_<table>_retired ON <table>;  (for the four tables) and DROP FUNCTION commission_retired();
