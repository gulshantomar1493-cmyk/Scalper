# MarketScalper

**Deterministic Market Analysis & Decision-Support Terminal** — a production-grade personal platform for scalping decisions.

- Markets (v1): **BTCUSDT + ETHUSDT** only · Timeframes: **1m** primary, **5m** context
- Analysis feed: Binance WS (`aggTrade` + `kline_1m` + `bookTicker`) · Optional: Delta Exchange public market data
- Philosophy: no repaint, replay-first, no fake confidence %, validate before trust
- **MarketScalper never places orders.** It generates trade recommendations; you execute manually on your exchange (Delta or any other) and log the outcome. No broker integration, no automated trade management, no position sync.

## Status

**Phase P0 (Spine) complete** — the P0.28 acceptance gate passed 2026-07-17 (tag `v1.0.0-foundation`): live 1m chart, 90-day replay, zero candle mismatch vs official klines, provider conformance + determinism green. See `docs/P0-CLOSURE.md` and `docs/decisions/P0.28-acceptance-gate.md`. Phase P1 (Structure + Trendlines) is next.
A strategy is marked **TRUSTED** only after the P5 validation gate (200+ logged recommendations, positive expectancy after fees).

## How it works

Every closed candle flows through one pipeline — identical in live and replay:

```
Feed Provider (Binance / Replay) → normalized events → Candle Builder (1m/5m)
  → Analysis Engines (Structure, Trendline, Liquidity, SmartMoney, Volume)
  → Strategy → Qualification (gates + score) → Trade Planning → Reasoning
  → TRADE RECOMMENDATION (entry / SL / targets / score / invalidation)
  → manual execution by you → manual outcome logging → Analytics
```

Launch strategies: **S1** Liquidity Sweep Reversal · **S2** Trend Pullback Continuation · **S3** Trendline Fake-Break Trap.

## Tech stack

Python 3.12 + asyncio · FastAPI (WebSocket + REST) · PostgreSQL 16 (partitioned, append-only) · Vanilla JS + TradingView Lightweight Charts v5 · single systemd service on a self-hosted Linux server (provider-agnostic).

## Repository layout

| Path | Contents |
|------|----------|
| `backend/` | Feed providers, candle builder, engines, decision & recommendation layers, FastAPI app |
| `frontend/` | LWC v5 terminal UI (vanilla JS) |
| `database/` | PostgreSQL migrations |
| `deployment/` | systemd unit, deploy scripts |
| `scripts/` | CI / tooling, incl. the determinism gate |
| `tests/` | pytest suites |
| `docs/` | Frozen docs + `decisions/` |

## Documentation

- [`docs/Architecture.md`](docs/Architecture.md) — Blueprint **v1.2 (FROZEN)** — the single source of truth
- [`docs/IMPLEMENTATION_PLAN.md`](docs/IMPLEMENTATION_PLAN.md) — approved roadmap **v2.0** (117 core tasks, P0–P5, optional P6)
- [`CLAUDE.md`](CLAUDE.md) — working rules for AI-assisted sessions

## Running

Requires Python ≥ 3.12 and PostgreSQL 16.

```bash
pip install -e .                                  # install package + dependencies
cp backend/config.example.yaml backend/config.yaml   # optional local overrides (git-ignored)
marketscalper                                     # or: python -m marketscalper.main
```

Configuration loads in a fixed order: `backend/config.example.yaml` (committed base) → `backend/config.yaml` (git-ignored local overrides) → environment variables (`MARKETSCALPER_LOG_LEVEL`, `MARKETSCALPER_DB_DSN`, `MARKETSCALPER_SYMBOLS`, …) override everything. **Secrets are never committed** — the DB DSN lives only in the local config or environment.

The entrypoint is the full composition root (P0.27): it refuses to start without `MARKETSCALPER_API_TOKEN` and a database DSN (exit code 2), then runs the live pipeline and serves the REST/WebSocket API (default `127.0.0.1:8000`). Open `frontend/index.html?api=HOST:PORT&token=TOKEN` — from disk or any static host — for the terminal. Deployment artifacts for the single Linux server live in `deployment/`.

## Testing

```bash
pip install -e ".[dev]"   # installs pytest + pytest-asyncio
pytest                    # run the suite
bash scripts/ci.sh        # the CI gate: full suite incl. provider-conformance, import-boundary and determinism gates
```

Database tests run against the local development database addressed by `MARKETSCALPER_DB_DSN`, with migrations 001/002 already applied (see `database/README.md`). Without the variable they skip; with an unprepared database they fail with instructions — the suite never applies migrations itself. Every test rolls back its transaction, leaving no data behind.
