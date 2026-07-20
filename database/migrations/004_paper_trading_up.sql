-- Migration 004 (P6, decision D31): simulation-only paper trading.
-- ADDITIVE + ISOLATED. No real execution — everything is simulated in these four
-- tables. The frozen data model (001/002) + the 6 existing tables are untouched.
-- Validation stays in the app layer (no CHECK constraints), matching 001/002.
--
-- Apply:    psql "$MARKETSCALPER_DB_DSN" -f database/migrations/004_paper_trading_up.sql
-- Rollback: psql "$MARKETSCALPER_DB_DSN" -f database/migrations/004_paper_trading_down.sql

BEGIN;

CREATE TABLE paper_account (
  id bigserial PRIMARY KEY,
  balance numeric,                    -- free cash (USD), adjusted by realized PnL - fees
  starting_balance numeric,           -- ROI baseline
  taker_fee numeric DEFAULT 0.0005,   -- per side
  updated_at timestamptz DEFAULT now()
);

CREATE TABLE paper_positions (
  id bigserial PRIMARY KEY,
  symbol text,
  side text,                          -- 'LONG' | 'SHORT'
  qty numeric,                        -- base qty (> 0)
  avg_entry numeric,
  leverage numeric,
  margin numeric,                     -- isolated margin (notional / leverage)
  liq_price numeric,
  realized_pnl numeric DEFAULT 0,     -- realized over the life of this position
  fees_paid numeric DEFAULT 0,
  status text DEFAULT 'open',         -- 'open' | 'closed'
  opened_at timestamptz DEFAULT now(),
  closed_at timestamptz
);

CREATE TABLE paper_orders (
  id bigserial PRIMARY KEY,
  symbol text,
  side text,                          -- 'BUY' | 'SELL'
  type text,                          -- 'market' | 'limit' | 'stop'
  qty numeric,
  price numeric,                      -- limit price (NULL for market)
  stop_price numeric,                 -- stop trigger (stop orders)
  leverage numeric,
  reduce_only boolean DEFAULT false,
  status text DEFAULT 'open',         -- 'open' | 'filled' | 'cancelled'
  fill_price numeric,
  created_at timestamptz DEFAULT now(),
  filled_at timestamptz
);

CREATE TABLE paper_trades (           -- fill / history log
  id bigserial PRIMARY KEY,
  ts timestamptz DEFAULT now(),
  symbol text,
  side text,                          -- 'BUY' | 'SELL'
  qty numeric,
  price numeric,
  fee numeric,
  realized_pnl numeric,               -- realized on this fill (0 for opens / increases)
  position_id bigint,
  order_id bigint
);

COMMIT;
