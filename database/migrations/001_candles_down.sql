-- Migration 001 rollback (roadmap P0.5).
-- Drops everything 001_candles_up.sql created, including all monthly partitions.

BEGIN;

DROP FUNCTION IF EXISTS ensure_candle_partitions(timestamptz, int);
DROP TABLE IF EXISTS candles CASCADE;   -- CASCADE removes all partitions

COMMIT;
