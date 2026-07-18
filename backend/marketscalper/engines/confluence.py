"""Confluence Engine — COMPLETE and FROZEN (engine-wise freeze after the
D15 conformance audit; Architecture §4.5 stacking; Decision D15 incl. its
freeze-audit addenda; roadmap P2.18/P2.19). Modify only on a genuine
production defect.

zone_quality = overlap count of {OB, FVG, trendline, EQH/EQL, key level}
within a 0.3×ATR band (inclusive), anchored at the tradeable zone objects
(active OBs, active breakers, unfilled FVGs); 3+ overlapping objects =
"HTF magnet zone". The VWAP-band member is ABSENT until the Volume Engine
(P2.3) lands — flagged placeholder per D15.1.

Pure, stateless, on-demand recompute per closed candle from frozen-engine
outputs (the TrendlineDetector.candidates() precedent) — nothing here is
retained, nothing upstream is recomputed. No persistence (D15.3).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from math import exp

# Frozen §4.5/D15 literal — module constant, not config.
CONFLUENCE_BAND_ATR_RATIO = 0.3        # inclusive "within" (D15.2)
HTF_MAGNET_MIN_COUNT = 3               # §4.5: "3+ overlapping objects"

_KIND_ORDER = {"BREAKER": 0, "FVG": 1, "OB": 2}


@dataclass(frozen=True)
class ConfluenceZone:
    """One anchored stack: a tradeable zone plus everything within band."""

    kind: str                  # 'OB' | 'BREAKER' | 'FVG'
    direction: str             # the anchor's 'BULL' | 'BEAR'
    lo: float
    hi: float
    count: int                 # zone_quality (anchor included)
    members: tuple             # kind tags, anchor first (D15.2)
    htf_magnet: bool
    created_ts: datetime       # the anchor's created_ts


def _interval_gap(a_lo: float, a_hi: float, b_lo: float, b_hi: float) -> float:
    """Distance between two closed intervals; 0 when they intersect."""
    return max(0.0, max(a_lo, b_lo) - min(a_hi, b_hi))


def confluence_zones(
    *,
    blocks,
    breakers,
    gaps,
    lines,
    pools,
    key_levels: dict,
    atr: float | None,
    bar_index: int,
) -> list[ConfluenceZone]:
    """D15 stacking over frozen-engine state; [] while ATR is unwarm.

    blocks/breakers: OrderBlock lists (active status only participates);
    gaps: FairValueGap list (unfilled by construction); lines: KeptTrendline
    list priced here at bar_index; pools: LiquidityPool list; key_levels:
    the LiquidityEngine promoted dict.
    """
    if atr is None:
        return []
    tol = CONFLUENCE_BAND_ATR_RATIO * atr

    # (tag, lo, hi) evidence objects — §4.5: mitigated OB = weight 0
    anchors: list[tuple[str, str, float, float, datetime]] = []
    for block in blocks:
        if block.status == "active":
            anchors.append(("OB", block.direction,
                            block.zone_lo, block.zone_hi, block.created_ts))
    for block in breakers:
        if block.status == "active":
            anchors.append(("BREAKER", block.direction,
                            block.zone_lo, block.zone_hi, block.created_ts))
    for gap in gaps:
        anchors.append(("FVG", gap.direction, gap.lo, gap.hi, gap.created_ts))

    evidence: list[tuple[str, float, float]] = [
        (kind, lo, hi) for kind, _d, lo, hi, _ts in anchors]
    for line in lines:
        price = exp(line.intercept + line.slope * (bar_index - line.a_index))
        evidence.append(("TRENDLINE", price, price))
    for pool in pools:
        evidence.append((pool.kind, pool.price, pool.price))
    for name in sorted(key_levels):
        price = key_levels[name]
        evidence.append((name, price, price))

    zones: list[ConfluenceZone] = []
    for i, (kind, direction, lo, hi, created_ts) in enumerate(anchors):
        members = [kind]
        for j, (tag, e_lo, e_hi) in enumerate(evidence):
            if j == i:
                continue                       # the anchor itself
            if _interval_gap(lo, hi, e_lo, e_hi) <= tol:
                members.append(tag)
        count = len(members)
        zones.append(ConfluenceZone(
            kind, direction, lo, hi, count, tuple(members),
            count >= HTF_MAGNET_MIN_COUNT, created_ts))

    zones.sort(key=_sort_key)
    return zones


def _sort_key(zone: ConfluenceZone):
    """D15.2 total order: count desc, created_ts desc, kind, direction."""
    return (-zone.count, -zone.created_ts.timestamp(),
            _KIND_ORDER[zone.kind], zone.direction)
