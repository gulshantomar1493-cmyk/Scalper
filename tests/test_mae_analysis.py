"""Tests for the MAE-distribution SL-tuning tool (§11 P5.2)."""

from __future__ import annotations

from datetime import datetime, timezone

from marketscalper.analytics import (
    _mae_histogram,
    _preserve_stop,
    mae_distribution,
)

UTC = timezone.utc


def _row(strategy="S1", outcome="tp1", mae=-0.3, mfe=2.2):
    return {"strategy": strategy,
            "ts": datetime(2026, 7, 22, 9, 0, tzinfo=UTC),
            "eval_outcome": outcome, "eval_r": 2.0, "eval_mae": mae,
            "eval_mfe": mfe, "status": "evaluated", "taken": True,
            "result": "win", "actual_r": 1.8}


def test_histogram_bands_and_counts():
    h = _mae_histogram([-0.3, -0.7, -1.2, -2.0, 0.0])
    assert [b["band"] for b in h] == [
        "0.0..-0.5R", "-0.5..-1.0R", "-1.0..-1.5R", "<=-1.5R"]
    assert [b["count"] for b in h] == [2, 1, 1, 1]     # 0.0 and -0.3 in band 0


def test_histogram_band_boundaries():
    # exactly -0.5 -> band 0 (<= lo=0.0 AND > hi=-0.5 is FALSE at -0.5) ->
    # -0.5 is NOT > -0.5, so it falls to band 1 [-0.5, -1.0)
    h = _mae_histogram([-0.5])
    assert h[0]["count"] == 0 and h[1]["count"] == 1


def test_preserve_stop_keeps_fraction_of_winners():
    # winner MAEs ascending (worst first): -1.5, -0.8, -0.4, -0.2, -0.1
    maes = [-1.5, -0.8, -0.4, -0.2, -0.1]
    # keep 90% -> index int(0.1*5)=0 -> the worst (-1.5): a stop that loose
    assert _preserve_stop(maes, 0.90) == -1.5
    # keep 75% -> index int(0.25*5)=1 -> -0.8
    assert _preserve_stop(maes, 0.75) == -0.8
    assert _preserve_stop([], 0.9) is None


def test_mae_distribution_per_strategy():
    rows = [
        _row("S1", "tp1", -0.3), _row("S1", "tp1", -0.6),
        _row("S1", "sl", -1.2),                          # loser
        _row("S2", "tp2", -0.9),
    ]
    d = mae_distribution(rows)
    assert set(d) == {"S1", "S2"}
    s1 = d["S1"]
    assert s1["n_evaluated"] == 3 and s1["n_winners"] == 2
    # winners' worst MAE (most adverse) = -0.6; median of [-0.6,-0.3] = -0.3
    assert s1["winner_mae_worst"] == -0.6
    assert s1["winner_mae_median"] == -0.3
    assert s1["sl_preserve_90"] == -0.6                 # loosest keeps all
    # histogram over the 3 evaluated: -0.3,-0.6 in band0/band1, -1.2 band2
    counts = [b["count"] for b in s1["mae_histogram"]]
    assert counts == [1, 1, 1, 0]
    assert d["S2"]["n_winners"] == 1


def test_mae_distribution_ignores_unevaluated_and_missing_mae():
    rows = [_row("S1", "none", None), _row("S1", "tp1", None)]
    d = mae_distribution(rows)
    assert d["S1"]["n_evaluated"] == 0 and d["S1"]["n_winners"] == 0
    assert d["S1"]["winner_mae_worst"] is None
