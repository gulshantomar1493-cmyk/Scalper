"""V4 configuration — every number here was validated by the research programme.

DO NOT tune these casually. Each value has replay evidence behind it, recorded in
docs/V4/ARCHITECTURE.md §1. Values that were tested and REJECTED are listed at the
bottom so they are never silently reintroduced.
"""
from __future__ import annotations
from dataclasses import dataclass, field

# ----------------------------------------------------------------- geometry ---

TAKER_FEE = 0.0005          # per side. Research showed the edge survives to 0.10%.
FUNDING_PER_DAY = 0.0003    # perpetual funding charged on notional per day held.

STOP_ATR5M_MULT = 5.0       # stop = 5 x ATR(5m). THE critical number:
                            #   2x -> fee/R 0.27 -> net -0.374  (dead)
                            #   5x -> fee/R 0.09 -> net +0.338  (works)
TARGET_R = 10.0             # expectancy rose monotonically with target size.
ATR_PERIOD = 14
MAX_HOLD_DAYS = 3           # 5-day horizon was best; 3d keeps capital turning.
ENTRY_VALID_BARS_MIN = 240  # a resting order stays live 4h before being cancelled.


@dataclass(frozen=True)
class Strategy:
    """One tradeable strategy. `backtest_*` are the RESEARCH numbers over 9 years —
    they are displayed alongside live paper stats, never merged with them."""
    id: str
    label: str
    symbol: str
    level_source: str          # donchian | swing | pdh_pdl | round
    level_tf: str              # timeframe the LEVEL comes from
    eval_tf: str               # grid we CHECK the level on (<= level_tf).
                               # A resting order can be re-armed more often than
                               # the level updates; the research evaluated daily
                               # levels on the 4h grid and the parity test caught
                               # the difference (11 vs 54 trades/yr).
    lookback: int              # for donchian
    min_filters: int           # how many of the 3 trend filters must agree (1..3)
    backtest_trades_per_year: float
    backtest_net_r: float
    backtest_t_stat: float
    backtest_profit_factor: float
    enabled: bool = True
    note: str = ""


# The catalogue. Ordered best-evidence first.
STRATEGIES: tuple[Strategy, ...] = (
    Strategy("eth_4h_core", "ETH 4H Core", "ETHUSDT", "donchian", "4h", "4h", 20, 3,
             100.0, 0.474, 4.50, 1.56,
             note="Highest conviction. Strictest filter, ~2 setups/week."),
    Strategy("eth_4h_wide", "ETH 4H Wide", "ETHUSDT", "donchian", "4h", "4h", 20, 1,
             153.0, 0.372, 4.60, 1.43,
             note="Same levels, one filter instead of three. More trades."),
    Strategy("eth_1h_fast", "ETH 1H Fast", "ETHUSDT", "donchian", "1h", "1h", 20, 1,
             251.0, 0.196, 3.17, 1.22,
             note="~1 setup/day. Lowest timeframe where the edge is still significant."),
    Strategy("eth_1d_swing", "ETH 1D Swing", "ETHUSDT", "swing", "1d", "4h", 20, 3,
             54.0, 0.643, 4.08, 1.75,
             note="Best per-trade expectancy, fewest trades."),
    Strategy("eth_pdhl", "ETH Prior-Day H/L", "ETHUSDT", "pdh_pdl", "4h", "4h", 0, 3,
             112.0, 0.334, 3.49, 1.38,
             note="Classic prior-day levels."),
    Strategy("btc_4h_core", "BTC 4H Core", "BTCUSDT", "donchian", "4h", "4h", 20, 1,
             159.0, 0.204, 2.61, 1.23,
             note="BTC is materially weaker than ETH. Only 4h survives."),
)

BY_ID = {s.id: s for s in STRATEGIES}
SYMBOLS = ("BTCUSDT", "ETHUSDT")

# --------------------------------------------------------- honesty metadata ---

#: Displayed verbatim in the UI. The research could NOT establish modern-era
#: significance, and the tool must say so rather than imply a proven system.
CONFIDENCE_NOTE = (
    "Backtest covers 2017-2026 (9 years). Over the full sample these strategies are "
    "statistically significant (t = 2.6 to 4.6). Over the last 2-4 years they remain "
    "POSITIVE but are NOT statistically significant (t = 0.7 to 1.7) - the recent "
    "sample is too small to confirm. Treat live results as the real test."
)

#: Tested and REJECTED. Listed so they are never silently reintroduced.
REJECTED_IDEAS = {
    "premium_discount": "lift -0.229 (BTC) / -0.454 (ETH) - mean-reversion logic fighting a breakout system",
    "liquidity_sweep": "lift -0.227 / -0.258 - same reason",
    "level_bounce": "fading a level loses on EVERY level type, both symbols (t to -3.95)",
    "mean_reversion": "gross-negative in every regime, including ranging markets",
    "smc_sweep_choch_ob": "not significant even at ZERO fees (t 1.02 / 0.66)",
    "trendline_bounce": "negative even at zero fees (t -3.79)",
    "time_of_day": "era-to-era sign stability 42-46% - a coin flip",
    "btc_eth_lead_lag": "correlation ~0; spread 14x below transaction cost",
    "market_entry": "edge decays 36% in 1 minute and inverts by 15 minutes",
    "tight_stops": "2xATR(5m) gives fee/R 0.27 and turns the system negative",
}
