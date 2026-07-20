# D31 — Paper Trading (simulation-only) — v1.2 SCOPE AMENDMENT

**Status:** Approved (owner decision, 2026-07-20) — amends the frozen v1.2
"decision-support only, no PaperBroker/ExecutionProvider" boundary.
**Isolation:** a self-contained `papertrade` subsystem. It NEVER places a real
order, NEVER calls an exchange order API, and NEVER touches the frozen analysis
engines, the event bus, the `structure` payload, or the §10 determinism stream.

## Why this amends a frozen boundary

`CLAUDE.md` v1.2 froze OUT order placement / position management / PaperBroker,
and this was declined once before. The owner has now explicitly approved a
**simulation-only** paper-trading engine (everything in our own PostgreSQL, zero
real execution) built as an isolated module. This decision records that
amendment; the frozen ANALYSIS scope (1m/5m decision engine, no real execution)
is unchanged.

## Architecture (isolation-first)

- New tables (migration 004): `paper_account`, `paper_orders`, `paper_positions`,
  `paper_trades` — additive; the 6 existing tables + the frozen data model are
  untouched.
- New pure engine `core/papertrade.py` — order fills, position math (avg entry,
  leverage, isolated margin, liquidation price), realized/unrealized PnL, taker
  fees. Deterministic given (order, mark price); no wall-clock in the math.
- New API `/api/paper/*` (under the existing `/api/*` reverse-proxy matcher).
- New frontend "Paper Trading" page + Live-chart markers (entry/SL/TP lines,
  position box) — display + a simulated order ticket.
- It READS the live mark price (already available in the app) for
  mark-to-market, but has NO write path to any exchange and no coupling to the
  analysis pipeline. It cannot move the V1–V4 determinism hash (off the engine
  path entirely).

## V1 scope (this decision)

IN: a virtual wallet (configurable USD balance in Settings); Market + Limit +
Stop-Market orders; Long/Short; open / increase / reduce / close / reverse a
position; leverage; **isolated-margin** model; liquidation price; taker fees
(Delta 0.05%/side default); realized + unrealized PnL; ROI; a portfolio
dashboard (equity, balance, used/available margin, open positions, PnL, win rate,
total trades, avg RR, largest win/loss); trade history; Live-chart trade markers;
full synchronisation (order fill → position → PnL → history all update).

V1 SIMPLIFICATIONS (documented, can extend later): Stop-Limit is modelled as a
Stop-Market trigger; cross-margin, funding rate, and partial-fill microstructure
are omitted (single-fill at the mark/limit price); one account, one symbol side
per position (hedge mode off). No real execution, ever.

## Safety invariants (must hold)

1. No exchange order API is ever called. `papertrade` imports no provider and no
   networking beyond reading the already-computed live price.
2. `papertrade` never publishes on the engine bus, never writes `structure`,
   never persists into the 6 frozen tables → determinism V1–V4 byte-identical.
3. Grep-guarded frontend: the Paper Trading page has no real-broker calls; it
   talks only to `/api/paper/*`.
