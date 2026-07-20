-- Rollback for migration 003 (P5): drop the standalone user journal.
BEGIN;
DROP TABLE IF EXISTS journal_entries;
COMMIT;
