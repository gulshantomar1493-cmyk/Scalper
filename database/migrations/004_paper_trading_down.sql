-- Rollback for migration 004 (P6): drop the paper-trading tables.
BEGIN;
DROP TABLE IF EXISTS paper_trades;
DROP TABLE IF EXISTS paper_orders;
DROP TABLE IF EXISTS paper_positions;
DROP TABLE IF EXISTS paper_account;
COMMIT;
