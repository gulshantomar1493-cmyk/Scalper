"""V3 configuration — every threshold lives here. No magic numbers in engine code.

All values are plain floats/ints on a dataclass so tests can construct variants
and the owner can calibrate later without touching engine logic.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class V3Config:
    # ---- timeframes ----------------------------------------------------
    read_tfs: tuple = ("5m", "15m", "1h", "4h", "1d")   # L1 chart-read TFs
    history_bars: int = 600            # candles folded per TF read (cold start)

    # ---- swings / structure -------------------------------------------
    swing_k: int = 2                   # fractal wing: swing = extreme of 2k+1
    atr_period: int = 14               # Wilder ATR
    displacement_atr: float = 1.2      # body > 1.2×ATR = displacement candle

    # ---- trendlines ----------------------------------------------------
    tl_anchor_swings: int = 12         # last N swings per side considered
    tl_touch_tol_atr: float = 0.15     # touch tolerance = 0.15×ATR (log space)
    tl_break_atr: float = 0.25         # decisive close beyond line by 0.25×ATR
    tl_keep_per_side: int = 3          # best lines kept per side
    tl_max_age_bars: int = 400         # older than this without a touch → INVALID

    # ---- zones ---------------------------------------------------------
    sr_cluster_atr: float = 0.25       # swings within 0.25×ATR cluster into S/R
    sr_min_members: int = 2
    base_body_atr: float = 0.5         # base candle: body < 0.5×ATR
    base_max_candles: int = 3
    impulse_body_atr: float = 1.5      # impulse candle: body ≥ 1.5×ATR
    fvg_min_atr: float = 0.3           # FVG minimum gap size
    zone_weak_touches: int = 3         # 3rd+ touch → WEAK
    zone_max_age_bars: int = 500       # untouched/old zones RETIRE
    zone_pad_atr: float = 0.05         # band padding

    # ---- liquidity -----------------------------------------------------
    eq_pool_atr: float = 0.10          # equal highs/lows within 0.10×ATR
    pool_priorities: dict = field(default_factory=lambda: {
        "PWH": 5, "PWL": 5, "PDH": 5, "PDL": 5,
        "EQH": 4, "EQL": 4,
        "SESSION_H": 3, "SESSION_L": 3,
        "INTERNAL_H": 2, "INTERNAL_L": 2,
        "MINOR_H": 1, "MINOR_L": 1,
    })

    # ---- sessions (UTC bounds; from the owner's IST guide) -------------
    # ASIA 05:30–13:30 IST = 00:00–08:00 UTC · LONDON 14:30–19:30 IST =
    # 09:00–14:00 UTC · NY 19:30–02:00 IST = 14:00–20:30 UTC
    session_asia_utc: tuple = (0, 8)          # [start_hour, end_hour)
    session_london_utc: tuple = (9, 14)
    session_ny_utc: tuple = (14, 20.5)

    # ---- premium / discount -------------------------------------------
    range_swings: int = 20             # dealing range from the last N swings

    # ---- rendering caps (payload size) --------------------------------
    max_swings_out: int = 40
    max_zones_out: int = 30
    max_pools_out: int = 20


DEFAULT = V3Config()
