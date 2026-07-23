-- 007: Trade Recommendation History (V3). Every setup the engine issues is
-- recorded automatically and permanently searchable — completely independent
-- of the paper-trading tables. Status/outcome fields are updated by the
-- live tracker as candles close; the analysis JSONB stores the engine's FULL
-- reasoning at issue time. Idempotent.
CREATE TABLE IF NOT EXISTS v3_recommendations (
    id            bigserial PRIMARY KEY,
    setup_id      text NOT NULL UNIQUE,          -- engine id (dedupe)
    ts            timestamptz NOT NULL,          -- issue time (confirm bar)
    symbol        text NOT NULL,
    timeframe     text NOT NULL DEFAULT '5m',
    session_label text,
    session_rating int,
    direction     text NOT NULL,                 -- LONG | SHORT
    setup_type    text NOT NULL,                 -- Zone Reversal | Breakout | Breakdown
    grade         text NOT NULL,                 -- A+ | A
    entry         double precision NOT NULL,
    sl            double precision NOT NULL,
    tp1           double precision NOT NULL,
    tp2           double precision,
    rr            double precision,
    status        text NOT NULL DEFAULT 'ACTIVE',
    -- ACTIVE | TP1_HIT | TP2_HIT | STOP_LOSS | CANCELLED | EXPIRED | TIMEOUT
    result_r      double precision,
    points_captured double precision,
    points_lost   double precision,
    mae_r         double precision,
    mfe_r         double precision,
    holding_minutes int,
    filled_ts     timestamptz,
    closed_ts     timestamptz,
    analysis      jsonb NOT NULL,                -- full engine reasoning
    created_at    timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS v3_rec_ts_idx      ON v3_recommendations (ts DESC);
CREATE INDEX IF NOT EXISTS v3_rec_symbol_idx  ON v3_recommendations (symbol, ts DESC);
CREATE INDEX IF NOT EXISTS v3_rec_status_idx  ON v3_recommendations (status);
CREATE INDEX IF NOT EXISTS v3_rec_grade_idx   ON v3_recommendations (grade);
CREATE INDEX IF NOT EXISTS v3_rec_type_idx    ON v3_recommendations (setup_type);
