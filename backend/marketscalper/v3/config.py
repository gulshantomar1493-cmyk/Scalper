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
    sweep_resolve_bars: int = 6        # post-sweep outcome judged N bars later
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

    # ---- market map (L2) ----------------------------------------------
    map_merge_atr: float = 0.30        # zones merge if gap ≤ 0.3×ATR(higher tf)
    map_max_width_atr: float = 1.5     # a merged map-zone never exceeds 1.5×ATR(higher tf)
    map_fresh_bonus: float = 0.5       # FRESH component adds to map-zone weight
    map_max_zones: int = 14            # decision points kept per symbol
    map_max_targets: int = 6           # liquidity targets kept per side
    bias_weights: dict = field(default_factory=lambda: {
        "1d": 4.0, "4h": 3.0, "1h": 2.0, "15m": 1.0})   # structure-only vote
    bias_min_share: float = 0.4        # winner needs ≥40% of total vote weight
    bias_min_margin: float = 0.3       # AND a ≥30%-of-total margin (else NEUTRAL)

    # ---- virtual trader (L4) ------------------------------------------
    watch_dist_atr: float = 1.5        # price within 1.5×ATR of a zone → WATCHING
    confirm_bars: int = 30             # 5m bars scanned for zone entry + confirmation
    sweep_into_zone_bars: int = 24     # a sweep within N 5m bars counts as fuel
    sl_pad_atr: float = 0.25           # SL beyond the zone edge / sweep wick
    min_rr_net: float = 1.5            # net-of-fees floor to TP1
    taker_fee: float = 0.0005          # per side; round trip ×2
    rejection_wick_frac: float = 0.60  # wick ≥60% of bar range = rejection
    grade_a_plus: int = 5              # confluence counts (of 7)
    grade_a: int = 3
    grade_b: int = 2                   # label threshold
    min_issue_confluences: int = 3     # replay-validated: B-grade issuance lost;
                                       # only A/A+ (≥3 factors) get issued
    # replay-validated trader rules (P4 false-positive reduction):
    entry_at_edge: bool = True         # enter the zone EDGE (retest), not the mid
                                       # (mid entries expired 43% of the time)
    counter_trend_needs_fuel: bool = True   # a rejection wick AGAINST the 5m trend
                                            # without sweep fuel = noise, skip
    boost_needs_fuel: bool = True      # trend sessions (London/NY) reward SWEEP
                                       # reversals; plain fades there lost 94-97%
    strict_confirmation: bool = True   # calibration C2: a reversal confirmation
                                       # must be STRUCTURAL — a displaced candle
                                       # (body >=1.2xATR) or a 5m CHOCH; plain
                                       # small-candle wicks/engulfings are noise
    reversal_bias_aligned_only: bool = True   # calibration C1: a reversal must
                                       # NOT fight the HTF ladder (bias-aligned or
                                       # NEUTRAL); counter-ladder fades = watch only
    max_watching_out: int = 6
    max_setups_out: int = 3

    # ---- calibration candidates (proven-playbook rules; replay-compared) --
    breakout_bias_aligned_only: bool = True   # C5 KEPT: breaks must not fight a
                                       # decided HTF ladder ("trade breaks WITH
                                       # the higher-TF trend"); replay-validated
    be_at_1r: bool = False             # C9 REJECTED as a global rule (totR/win
                                       # fell; reversals damaged). Kept OFF as a
                                       # replay-experiment knob; archetype-scoped
                                       # BE is a future candidate.
    dead_exit_bars: int = 0            # C8 REJECTED (totR & PF fell). OFF knob.

    # ---- breakout archetype (one structural addition; replay-compared) --
    breakout_body_atr: float = 1.2     # the break candle must displace (body ≥1.2×ATR)
    breakout_retest_tol_atr: float = 0.25   # retest counts within 0.25×ATR of the level
    breakout_max_age_bars: int = 18    # break older than 90min without retest → stale

    # ---- session timing (L5) — the owner's IST guide, verbatim ---------
    # IST minutes-of-day [start, end) → rating ⭐ + effect.
    # Effects: BLOCK · WARN_DOWNGRADE · NORMAL · BOOST (counts as a confluence)
    #          · STRONG_ONLY (A+/A only; B suppressed)
    session_windows: tuple = (
        {"ist": (210, 330),  "rating": 1, "effect": "BLOCK",
         "label": "03:30-05:30 dead zone (fake breakouts)"},
        {"ist": (330, 510),  "rating": 4, "effect": "NORMAL",
         "label": "05:30-08:30 Tokyo momentum"},
        {"ist": (510, 690),  "rating": 4, "effect": "NORMAL",
         "label": "08:30-11:30"},
        {"ist": (690, 810),  "rating": 2, "effect": "WARN_DOWNGRADE",
         "label": "11:30-13:30 Asian lunch chop"},
        {"ist": (810, 870),  "rating": 4, "effect": "NORMAL",
         "label": "13:30-14:30 pre-London"},
        # replay-validated: London open/peak are TREND sessions — hostile to
        # mean-reversion (8-12% win on fades even with fuel). Reversals here
        # must be A+; the BOOST still counts as a confluence when structural.
        {"ist": (870, 1050), "rating": 5, "effect": "BOOST", "min_grade": "A+",
         "label": "14:30-17:30 London open"},
        {"ist": (1050, 1170), "rating": 5, "effect": "BOOST", "min_grade": "A+",
         "label": "17:30-19:30 London peak"},
        {"ist": (1170, 1350), "rating": 6, "effect": "BOOST",
         "label": "19:30-22:30 London+NY overlap (best)"},
        {"ist": (1350, 1470), "rating": 4, "effect": "NORMAL",
         "label": "22:30-00:30"},
        {"ist": (1470, 1560), "rating": 3, "effect": "STRONG_ONLY",
         "label": "00:30-02:00 strong setups only"},
        {"ist": (1560, 1650), "rating": 1, "effect": "BLOCK",
         "label": "02:00-03:30 US wind-down"},
    )
    sunday_effect: str = "WARN_DOWNGRADE"   # erratic weekend structure
    sunday_min_grade: str = "A+"           # replay-validated: Sunday fades lost

    # ---- replay / validation (P4) --------------------------------------
    replay_step_bars: int = 3          # trader pass every N 5m bars (15 min)
    replay_entry_window_bars: int = 24 # limit at zone-mid must fill within 2h
    replay_horizon_bars: int = 288     # 24h max hold, then mark-to-market
    replay_missed_rr: float = 2.0      # ARMED-but-unissued zone that ran ≥2R = missed

    # ---- rendering caps (payload size) --------------------------------
    max_swings_out: int = 40
    max_zones_out: int = 30
    max_pools_out: int = 20


DEFAULT = V3Config()
