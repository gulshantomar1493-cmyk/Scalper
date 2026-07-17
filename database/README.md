# Database — migrations

Plain SQL only. No ORM, no migration framework, no tracking table.
Target: the project's single PostgreSQL database (version-neutral SQL; see root README for the project's stack requirements).

## Convention

Files live in `database/migrations/` as `NNN_name_up.sql` / `NNN_name_down.sql`.
Apply `*_up.sql` manually, in ascending numeric order. Each `*_down.sql` reverses exactly its own `up` file.

## Apply / rollback

```bash
psql "$MARKETSCALPER_DB_DSN" -f database/migrations/001_candles_up.sql
psql "$MARKETSCALPER_DB_DSN" -f database/migrations/001_candles_down.sql   # rollback
```

## Migration 002 — analysis + journal tables (P0.6)

Creates the remaining five tables of the frozen v1.2 data model, verbatim from Architecture §3: `pivots` (confirmed swings, `ts` + `confirmed_ts` repaint audit), `levels` (all liquidity/SMC objects + trendlines, lifecycle status; A4 TRENDLINE semantics in comments), `signals` (append-only scored setups with `gates`/`components`/`state_snapshot` jsonb + `engine_version` stamp), `recommendations` (entry/SL/TP plan, lifecycle status, hypothetical evaluator columns; FK → signals), `journal` (one row per recommendation, PK = FK → recommendations; auto context + manual outcome). Allowed enum values are documented as SQL comments — validation and append-only discipline belong to the application access layer (P0.7), so there are deliberately no CHECK constraints, triggers, or extra indexes. The data model is now complete; no further migrations are defined by the roadmap.

## Migration 001 — candles (P0.5)

Creates: the `candles` table (Architecture §3, monthly `PARTITION BY RANGE (ts)`, PK `(symbol, tf, ts)`) and the single partition helper `ensure_candle_partitions(p_from, p_months_ahead)` (Decision D2 — idempotent, UTC month boundaries, partitions named `candles_YYYY_MM`; defaults = current + next month). Applying the migration also creates the initial current/next-month partitions. Inserting into a month with no partition fails loudly by design — partitions are always created ahead by the app (startup + UTC midnight) or the bootstrap job.
