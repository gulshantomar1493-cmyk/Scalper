-- Migration 005 (P6): SL / TP brackets on paper positions (draggable chart lines).
-- ADDITIVE — two nullable columns on the existing paper_positions table. When set
-- (via /api/paper/sltp, i.e. dragging the line on the chart), the sync engine
-- closes the position when the mark hits the stop or the target.
--
-- Apply:    psql "$MARKETSCALPER_DB_DSN" -f database/migrations/005_paper_sltp_up.sql
-- Rollback: psql "$MARKETSCALPER_DB_DSN" -f database/migrations/005_paper_sltp_down.sql

BEGIN;
ALTER TABLE paper_positions ADD COLUMN IF NOT EXISTS sl numeric;   -- stop-loss trigger (NULL = none)
ALTER TABLE paper_positions ADD COLUMN IF NOT EXISTS tp numeric;   -- take-profit trigger (NULL = none)
COMMIT;
