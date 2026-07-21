# V2 Backend API Contract — FROZEN v1.0

**Status:** FROZEN as of Phase 2.2 (before Phase 3 / Professional Chart).
**Scope:** the two read-only endpoints the Chart, Paper Trading, and future AI
modules depend on — `GET /api/htf` and `GET /api/setups`.
**Version:** every response carries `"contract_version": "1.0"`.

**Freeze policy:** during Phase 3 this contract does not change. Only bug fixes
are allowed. **Additive** fields (new optional keys) do **not** bump the version.
A **breaking** change (rename / remove / retype / enum change) bumps the major
version and must be documented in Migration Notes. Consumers should ignore
unknown keys and never assume field order.

Both endpoints are `GET`, Bearer-authenticated, read-only, and engine-isolated
(they never execute, never mutate state, never touch the determinism stream).

---

## Common

**Auth:** `Authorization: Bearer <token>` (or the login-cookie flow). Missing/bad
token → **401** `{"detail": "invalid or missing token"}`.
**Errors:** `422` if `symbol` is absent; `503` `{"detail": "htf service not
configured"}` if the backend has no HTF service (only `/api/htf`).
**Timestamps:** ISO-8601 with a UTC offset (`2026-07-21T12:00:00+00:00`).
**Prices/floats:** JSON numbers. **Enums:** exact case as documented below.

---

## 1. `GET /api/htf?symbol=<SYMBOL>`

Higher-timeframe market read (1D / 4H / 1H / 15M). Direction is **price action
only**; indicators only move conviction (see `docs/V2/HTF-DESIGN.md`).

### Response `200`

| Field | Type | Meaning / values |
|---|---|---|
| `contract_version` | string | `"1.0"` |
| `symbol` | string | e.g. `"BTCUSDT"` |
| `timeframes` | object | keys `"1d","4h","1h","15m"` → a **TF-Analysis** (below) |
| `overall` | object | the roll-up (below) |

**`overall`**

| Field | Type | Values / meaning |
|---|---|---|
| `bias` | enum string | `BULLISH` \| `BEARISH` \| `NEUTRAL` — the timeframe-weighted vote |
| `conviction` | enum string | `STRONG` \| `MODERATE` \| `WEAK` — confirmation strength of the agreeing TFs |
| `confidence` | integer | `0..100` — % of timeframe weight that agrees with `bias` |
| `market_story` | string | deterministic top-down narrative |
| `explanation` | string | one-line "with-bias vs caution" guidance |

**TF-Analysis** (each timeframe). When `ready` is `false`, only
`{tf, ready, reason, trend:null, bias:"NEUTRAL", conviction:"WEAK"}` is present.

| Field | Type | Values / meaning |
|---|---|---|
| `tf` | string | `"1d"\|"4h"\|"1h"\|"15m"` |
| `ready` | boolean | `false` = not enough history |
| `trend` | enum string \| null | `Uptrend` \| `Downtrend` \| `Range` (always consistent with `bias`) |
| `bias` | enum string | `BULLISH` \| `BEARISH` \| `NEUTRAL` — **from structure/BOS/CHOCH only** |
| `conviction` | enum string | `STRONG` \| `MODERATE` \| `WEAK` |
| `structure` | string | swing labels, e.g. `"HH / HL"`, `"LH / LL"`, or `"forming"` |
| `bos` | object \| null | `{direction:"UP"\|"DOWN", ts, close}` — last Break of Structure |
| `choch` | object \| null | `{direction:"UP"\|"DOWN", ts, close}` — last Change of Character |
| `swing_high` / `swing_low` | object \| null | `{price, label, ts}` (label e.g. `HH`,`LL`) |
| `liquidity` | array | pools `[{kind, price, size, strength}]` (`kind` e.g. `EQH`/`EQL`) |
| `liquidity_sweep` | object \| null | `{side:"HIGH"\|"LOW", target, price, ts}` |
| `supply` / `demand` | array | zones `[{lo, hi, status:"active"\|"mitigated"}]` |
| `support` / `resistance` | number | recent range floor / ceiling |
| `trendlines` | array | `[{side:"support"\|"resistance", price, touches, slope:"up"\|"down"}]` |
| `ema_alignment` | enum string | `bullish`\|`bearish`\|`mixed-up`\|`mixed-down`\|`mixed`\|`n/a` — **confirmation only** |
| `momentum` | object | `{velocity, acceleration, shift, body_dominance, direction:"up"\|"down"\|"flat"}` — **confirmation only** |

### Example

```json
{
  "contract_version": "1.0",
  "symbol": "BTCUSDT",
  "overall": {
    "bias": "BULLISH", "conviction": "MODERATE", "confidence": 100,
    "market_story": "Higher-timeframe bias is BULLISH (100% timeframe agreement, moderate conviction). Daily: bullish (HH / HL, recent CHOCH down), weak conviction. 4H: bullish (HH / HL), strong conviction. ...",
    "explanation": "BULLISH bias led by Daily, 4H, 1H, 15M timeframe(s); no conflicting timeframe. With-bias 1m/5m signals are best supported."
  },
  "timeframes": {
    "1h": {
      "tf": "1h", "ready": true, "trend": "Uptrend", "bias": "BULLISH",
      "conviction": "STRONG", "structure": "HH / HL",
      "bos": {"direction": "UP", "ts": "2026-07-21T09:00:00+00:00", "close": 68120.0},
      "choch": null,
      "swing_high": {"price": 68250.0, "label": "HH", "ts": "..."},
      "swing_low": {"price": 67400.0, "label": "HL", "ts": "..."},
      "liquidity": [{"kind": "EQH", "price": 68500.0, "size": 3, "strength": 0.82}],
      "liquidity_sweep": {"side": "LOW", "target": "EQL", "price": 67380.0, "ts": "..."},
      "supply": [{"lo": 68400.0, "hi": 68520.0, "status": "active"}],
      "demand": [{"lo": 67350.0, "hi": 67480.0, "status": "active"}],
      "support": 67300.0, "resistance": 68550.0,
      "trendlines": [{"side": "support", "price": 67500.0, "touches": 3, "slope": "up"}],
      "ema_alignment": "bullish",
      "momentum": {"velocity": 12.4, "acceleration": 1.1, "shift": false, "body_dominance": 0.62, "direction": "up"}
    }
    /* "1d","4h","15m" similar */
  }
}
```

---

## 2. `GET /api/setups?symbol=<SYMBOL>`

The Trade Engine V2 output: HTF-gated, fully-explained setups, or a confident
"no setup". Reuses HTF (bias) + the live 1m structure (see
`docs/V2/ENGINE-AUDIT.md`).

### Response `200`

| Field | Type | Meaning / values |
|---|---|---|
| `contract_version` | string | `"1.0"` |
| `symbol` | string | e.g. `"BTCUSDT"` |
| `htf_bias` | enum string \| null | `BULLISH`\|`BEARISH`\|`NEUTRAL` — HTF summary (full read at `/api/htf`) |
| `htf_confidence` | integer \| null | `0..100` timeframe agreement, or null |
| `market_story` | string \| null | the HTF narrative, or null |
| `setups` | array | **Trade-Setup** objects, ranked best-first; `[]` when none |
| `message` | string \| null | `null` when there are setups, else exactly `"No high-probability setup available."` |

**Trade-Setup**

| Field | Type | Values / meaning |
|---|---|---|
| `symbol` | string | e.g. `"BTCUSDT"` |
| `direction` | enum string | `LONG` \| `SHORT` |
| `setup_type` | enum string | `Liquidity Sweep Reversal` \| `Trend Pullback` \| `Fake-Break Trap` |
| `grade` | enum string | `A+` \| `A` \| `B` — emergent from confluence agreement (NOT a probability) |
| `confluences` | integer | how many independent confluences aligned (1..5) |
| `confluences_total` | integer | the maximum (always `5`); render `"{confluences}/{confluences_total}"` |
| `risk_level` | enum string | `LOW` \| `MEDIUM` \| `HIGH` |
| `entry` | number | suggested entry price |
| `sl` | number | stop-loss price |
| `tp1` | number | first target (the nearer pool) |
| `tp2` | number \| null | second target, or null |
| `rr` | number | **net-of-fees** reward:risk to TP1 (always ≥ 1.5) |
| `htf_bias` | enum string | `BULLISH`\|`BEARISH`\|`NEUTRAL` — the direction filter |
| `ltf_trend` | enum string | `BULLISH`\|`BEARISH`\|`RANGE`\|`UNKNOWN` — the 1m structure |
| `market_context` | string | the narrative — control / what was taken / trapped / the draw / next |
| `reasons` | string[] | the aligned confluences (clean strings; the UI adds any ✓) |
| `reasons_to_avoid` | string[] | the honest bear case for this idea (always ≥ 1) |
| `invalidation` | string | the condition that voids the idea |
| `early_exit` | string[] | conditions to bail before the stop |
| `management_notes` | string[] | display-only management guidance |
| `holding_time` | enum string | `INTRADAY` (minutes to a few hours) |
| `why` | object | 6 keys: `why_exists, why_now, why_entry, why_sl, why_targets, why_edge` (all strings) |
| `created_ts` | string \| null | ISO-8601 UTC of the trigger, or null |

### Example — a setup

```json
{
  "contract_version": "1.0",
  "symbol": "BTCUSDT",
  "htf_bias": "BULLISH", "htf_confidence": 80,
  "market_story": "Higher-timeframe bias is BULLISH ...",
  "message": null,
  "setups": [{
    "symbol": "BTCUSDT", "direction": "LONG", "setup_type": "Liquidity Sweep Reversal",
    "grade": "A+", "confluences": 5, "confluences_total": 5, "risk_level": "LOW",
    "entry": 100.0, "sl": 98.0, "tp1": 104.5, "tp2": 108.0, "rr": 2.1,
    "htf_bias": "BULLISH", "ltf_trend": "BULLISH",
    "market_context": "Buyers control the higher timeframe. Sell-side liquidity was just swept at 97.9 and structure shifted — the late shorts who sold that low are now trapped. The 1m is trending. Price is likely drawn to the buy-side liquidity at 104.5, and the long is the execution of that story.",
    "reasons": [
      "aligned with the bullish higher-timeframe bias",
      "strong HTF conviction (80% timeframe agreement)",
      "entry at an unmitigated bullish order block",
      "entry in discount — the correct half of the range",
      "volume confirms (elevated rvol, delta leaning with the move)"
    ],
    "reasons_to_avoid": ["if the swept level is reclaimed, the reversal thesis is void — do not average down"],
    "invalidation": "a decisive close beyond 98.0 voids the idea",
    "early_exit": ["price fails to displace away from the zone within a few bars", "an opposing change-of-character prints", "the swept level is reclaimed and holds"],
    "management_notes": ["risk to 98.0 only; size so the loss is a fixed, small % of the account", "move the stop to break-even once price reaches +1R", "take partials at TP1 (104.5); trail the remainder toward TP2 (108.0)"],
    "holding_time": "INTRADAY",
    "why": {
      "why_exists": "a with-bias long: liquidity was swept and structure shifted into a discount location",
      "why_now": "the sweep + shift just printed and the trigger is still inside its validity window",
      "why_entry": "entry at an unmitigated bullish order block left by the shift",
      "why_sl": "beyond 98.0, the swept extreme — the price that proves the read wrong",
      "why_targets": "the draw at 104.5 (TP1 104.5, TP2 108.0; net R:R 2.10)",
      "why_edge": "an A+ setup: 5 independent confluences agree on a with-context long after a confirmed liquidity raid + structure shift into a valid location — the sweep→shift→zone pattern, not an indicator signal"
    },
    "created_ts": "2026-07-21T12:00:00+00:00"
  }]
}
```

### Example — no setup (the common, healthy case)

```json
{
  "contract_version": "1.0", "symbol": "BTCUSDT",
  "htf_bias": "BULLISH", "htf_confidence": 100,
  "market_story": "Higher-timeframe bias is BULLISH ...",
  "setups": [],
  "message": "No high-probability setup available."
}
```

---

## Migration Notes (pre-freeze → v1.0)

Applied on `/api/setups` only (it had **no** frontend consumer yet, so these are
non-breaking in practice). `/api/htf` was already at its v1.0 shape after Phase 2.1.

- `market_bias` → **`ltf_trend`** (it is the 1m trend, not a bias; distinct enum).
- `confluence_score` (string `"N of M ..."`) → **`confluences`** (int) + **`confluences_total`** (int). Parseable, not a string.
- **Removed** `primary_confluence` / `secondary_confluence` (redundant — use `reasons[0]` / `reasons[1]`).
- `expected_holding_time` (free text) → **`holding_time`** (enum, currently `INTRADAY`).
- `reasons` / `reasons_to_avoid` are now **clean strings** (the `✓` prefix moved to the UI).
- `why.why_exists` is now **concise**; the full narrative lives in `market_context` (was duplicated).
- `setup_type` never leaks a raw strategy code (`S1`…) — always one of the three enum labels.
- **Added** `contract_version` to both endpoints.

## Consumer guidance

- Ignore unknown keys; treat missing optional keys as null/empty.
- Render `grade` + `confluences/confluences_total`; **never** show `rr`, `confidence`, or
  `confluences` as a probability.
- `message` is authoritative for "no setup" — show it verbatim when `setups` is empty.
- AI modules: the enums above are stable join keys; the free-text fields
  (`market_context`, `reasons*`, `why.*`) are human-readable and safe to summarize.
