-- Migration 002 (roadmap P0.6): pivots, levels, signals, recommendations, journal.
-- Source of truth: docs/Architecture.md §3 (FROZEN v1.2), verbatim, incl. the
-- Decision A4 TRENDLINE column semantics. Completes the v1.2 data model
-- (candles shipped in migration 001). Exactly five tables — nothing else.
--
-- Allowed values for kind/label/status/direction/result are documented as
-- comments, NOT CHECK constraints: Architecture §3 defines no CHECKs and
-- assigns validation + append-only discipline to the application access
-- layer (roadmap P0.7). No triggers, no views, no functions, no indexes
-- beyond primary keys.
--
-- Apply:    psql "$MARKETSCALPER_DB_DSN" -f database/migrations/002_analysis_and_journal_up.sql
-- Rollback: psql "$MARKETSCALPER_DB_DSN" -f database/migrations/002_analysis_and_journal_down.sql

BEGIN;

CREATE TABLE pivots (
  id bigserial PRIMARY KEY,
  symbol text, tf text,
  ts timestamptz,                     -- pivot candle time
  confirmed_ts timestamptz,           -- jab confirm hua (repaint audit)
  kind text,                          -- 'H' | 'L'
  price numeric,
  label text                          -- 'HH','HL','LH','LL'
);

CREATE TABLE levels (                 -- liquidity + SMC objects
  id bigserial PRIMARY KEY,
  symbol text, tf text,
  kind text,        -- 'EQH','EQL','PDH','PDL','SESSION_H','SESSION_L',
                    -- 'OB_BULL','OB_BEAR','FVG_BULL','FVG_BEAR','TRENDLINE'
  p1 numeric, p2 numeric,             -- zone top/bottom
                                      -- (A4: kind='TRENDLINE' => p1 = price at t1,
                                      --  p2 = price at t2; slope stored redundantly)
  t1 timestamptz, t2 timestamptz,     -- trendline anchors
  slope numeric,                      -- trendline only
  touches int DEFAULT 0,
  status text DEFAULT 'active',       -- 'active','swept','mitigated','broken'
  created_ts timestamptz, status_ts timestamptz
);

CREATE TABLE signals (                -- immutable, append-only (enforced by access layer, P0.7)
  id bigserial PRIMARY KEY,
  ts timestamptz, symbol text, tf text,
  strategy text,
  direction text,                     -- 'LONG'|'SHORT'
  score numeric,
  gates jsonb,                        -- har gate ka pass/fail
  components jsonb,                   -- structure:91, liquidity:95 ...
  state_snapshot jsonb,               -- full StateStore dump (forensics)
  engine_version text                 -- hash-freeze discipline (Decision D1 stamp)
);

CREATE TABLE recommendations (        -- core immutable; sirf status/eval columns update hote (access layer, P0.7)
  id bigserial PRIMARY KEY,
  signal_id bigint REFERENCES signals(id),
  ts timestamptz,
  direction text,                     -- 'LONG'|'SHORT'
  entry_px numeric, sl numeric, tp1 numeric, tp2 numeric,
  suggested_qty numeric, risk_amt numeric, est_fees numeric,
  net_rr_tp1 numeric,
  status text DEFAULT 'active',       -- 'active','invalidated','expired','evaluated'
  status_ts timestamptz, status_reason text,
  -- hypothetical outcome: candle-based evaluator (execution NAHI — pure analysis)
  eval_outcome text,                  -- 'tp1','tp2','sl','none'
  eval_r numeric,
  eval_mae numeric, eval_mfe numeric  -- MAE/MFE analysis pattern, candles se
);

CREATE TABLE journal (                -- recommendation-based; outcome MANUAL entry
  recommendation_id bigint PRIMARY KEY REFERENCES recommendations(id),
  reason_text text,                   -- rule-trace explanation (AUTO)
  chart_snapshot_path text,           -- PNG at recommendation (AUTO)
  taken boolean,                      -- Taken / Skipped   (MANUAL)
  result text,                        -- 'win','loss','be' (MANUAL; NULL if skipped)
  actual_entry numeric, actual_exit numeric,  -- (MANUAL, optional)
  actual_pnl numeric, actual_r numeric,       -- (MANUAL, optional)
  rule_violations jsonb,              -- psychology layer
  notes text,                         -- user notes (MANUAL)
  tags text[]
);

COMMIT;
