-- Migration 001 (roadmap P0.5): candles table + monthly range partitioning.
-- Source of truth: docs/Architecture.md §3 (FROZEN v1.2) + docs/decisions/P0.4 (D2).
--
-- Apply:    psql "$MARKETSCALPER_DB_DSN" -f database/migrations/001_candles_up.sql
-- Rollback: psql "$MARKETSCALPER_DB_DSN" -f database/migrations/001_candles_down.sql
--
-- Plain SQL. No ORM, no migration framework, no tracking table (migrations are
-- applied manually, in numeric order — see database/README.md).

BEGIN;

-- Architecture §3, verbatim. Append-only discipline.
CREATE TABLE candles (
  symbol      text NOT NULL,
  tf          text NOT NULL,          -- '1m' | '5m'
  ts          timestamptz NOT NULL,   -- candle open time UTC
  o numeric, h numeric, l numeric, c numeric,
  v numeric,                          -- base volume
  qv numeric,                         -- quote volume
  n_trades int,
  taker_buy_v numeric,                -- delta/aggression proxy
  PRIMARY KEY (symbol, tf, ts)
) PARTITION BY RANGE (ts);            -- monthly partitions

-- Decision D2: the single partition helper. Idempotent, deterministic,
-- UTC month boundaries, partitions named candles_YYYY_MM.
-- Defaults create the current + next month. The app calls it at startup and
-- after each UTC midnight rollover; the bootstrap job (P0.16) calls it over
-- the historical range, e.g.:
--   SELECT ensure_candle_partitions(now() - interval '90 days', 4);
-- Returns the number of partitions actually created (0 = all existed).
CREATE FUNCTION ensure_candle_partitions(
  p_from         timestamptz DEFAULT now(),
  p_months_ahead int         DEFAULT 1
) RETURNS int
LANGUAGE plpgsql
AS $$
DECLARE
  m       date := date_trunc('month', p_from AT TIME ZONE 'UTC')::date;
  last_m  date := (date_trunc('month', p_from AT TIME ZONE 'UTC')
                   + make_interval(months => p_months_ahead))::date;
  created int  := 0;
  part    text;
BEGIN
  IF p_months_ahead < 0 THEN
    RAISE EXCEPTION 'p_months_ahead must be >= 0, got %', p_months_ahead;
  END IF;
  WHILE m <= last_m LOOP
    part := format('candles_%s', to_char(m, 'YYYY_MM'));
    IF to_regclass(part) IS NULL THEN
      EXECUTE format(
        'CREATE TABLE %I PARTITION OF candles FOR VALUES FROM (%L) TO (%L)',
        part,
        m::timestamp AT TIME ZONE 'UTC',                       -- month start, UTC
        (m + interval '1 month') AT TIME ZONE 'UTC'            -- next month start, UTC
      );
      created := created + 1;
    END IF;
    m := (m + interval '1 month')::date;
  END LOOP;
  RETURN created;
END;
$$;

-- Initial partitions: current month + next (D2 defaults).
SELECT ensure_candle_partitions();

COMMIT;
