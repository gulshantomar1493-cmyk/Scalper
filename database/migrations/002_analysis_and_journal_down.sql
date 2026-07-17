-- Migration 002 rollback (roadmap P0.6).
-- Drops exactly the five tables 002 created, in FK-safe order
-- (journal -> recommendations -> signals, then levels, pivots).
-- Migration 001 objects (candles, ensure_candle_partitions) are untouched.

BEGIN;

DROP TABLE IF EXISTS journal;
DROP TABLE IF EXISTS recommendations;
DROP TABLE IF EXISTS signals;
DROP TABLE IF EXISTS levels;
DROP TABLE IF EXISTS pivots;

COMMIT;
