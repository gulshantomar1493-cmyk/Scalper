-- V4 recommendation history. Append-only core; only outcome columns are updated.
-- Honest accounting: gross_r, fee_r, funding_r and net_r are stored SEPARATELY so
-- nobody can ever again present a gross figure as if it were net.
CREATE TABLE IF NOT EXISTS v4_recommendations (
    id              bigserial PRIMARY KEY,
    setup_key       text NOT NULL UNIQUE,      -- strategy|symbol|decision_ts|direction
    strategy_id     text NOT NULL,
    symbol          text NOT NULL,
    direction       smallint NOT NULL,          -- +1 long, -1 short
    level_source    text NOT NULL,
    level_tf        text NOT NULL,
    filters_passed  smallint NOT NULL,
    entry           double precision NOT NULL,
    stop            double precision NOT NULL,
    target          double precision NOT NULL,
    risk_pct        double precision,
    rr              double precision,
    reason          text,
    decision_ts     timestamptz NOT NULL,
    valid_until_ts  timestamptz NOT NULL,

    status          text NOT NULL DEFAULT 'OPEN',  -- OPEN|FILLED|TP|SL|TIME|CANCELLED
    fill_price      double precision,
    exit_price      double precision,
    gross_r         double precision,
    fee_r           double precision,
    funding_r       double precision,
    net_r           double precision,
    mae_r           double precision,
    mfe_r           double precision,
    hold_minutes    integer,
    filled_ts       timestamptz,
    closed_ts       timestamptz,

    created_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS v4_rec_decision_idx ON v4_recommendations (decision_ts DESC);
CREATE INDEX IF NOT EXISTS v4_rec_strategy_idx ON v4_recommendations (strategy_id, decision_ts DESC);
CREATE INDEX IF NOT EXISTS v4_rec_status_idx   ON v4_recommendations (status);
CREATE INDEX IF NOT EXISTS v4_rec_symbol_idx   ON v4_recommendations (symbol, decision_ts DESC);
