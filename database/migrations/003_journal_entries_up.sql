-- Migration 003 (P5): standalone user trade journal.
-- ADDITIVE + isolated. Unlike the recommendation-bound `journal` table from 002
-- (append-only, one row per system recommendation), `journal_entries` is a
-- STANDALONE, user-owned journal the owner can CREATE, EDIT, DELETE, search and
-- filter. The frozen 002 tables are untouched; `recommendation_id` is a plain
-- nullable reference column (NO foreign key) so entries are fully independent.
-- Validation stays in the app layer (no CHECK constraints), matching 002.
--
-- Apply:    psql "$MARKETSCALPER_DB_DSN" -f database/migrations/003_journal_entries_up.sql
-- Rollback: psql "$MARKETSCALPER_DB_DSN" -f database/migrations/003_journal_entries_down.sql

BEGIN;

CREATE TABLE journal_entries (
  id bigserial PRIMARY KEY,
  created_at timestamptz DEFAULT now(),
  updated_at timestamptz DEFAULT now(),
  title text,
  symbol text,
  direction text,                     -- 'LONG' | 'SHORT' | NULL
  entry numeric, exit_px numeric, sl numeric, tp numeric,
  risk_pct numeric,                   -- risk as a percent
  confidence int,                     -- self-rated 1..10
  emotion text, mistakes text, lessons text,
  strategy text, notes text,
  screenshot text,                    -- image URL / data-uri / path
  tags text[],
  recommendation_id bigint            -- optional link to a system recommendation (no FK: decoupled)
);

COMMIT;
