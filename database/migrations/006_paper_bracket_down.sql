-- Revert 006: drop the bracket-order columns.
ALTER TABLE paper_orders DROP COLUMN IF EXISTS sl;
ALTER TABLE paper_orders DROP COLUMN IF EXISTS tp;
