-- Rollback for migration 005 (P6): drop the SL/TP columns.
BEGIN;
ALTER TABLE paper_positions DROP COLUMN IF EXISTS tp;
ALTER TABLE paper_positions DROP COLUMN IF EXISTS sl;
COMMIT;
