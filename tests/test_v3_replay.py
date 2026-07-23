"""V3 P4 Replay Engine — outcome simulation + aggregation + walk determinism."""

from __future__ import annotations

import json

from marketscalper.v3.config import V3Config
from marketscalper.v3.replay import simulate_outcome, aggregate

CFG = V3Config()
T0 = 1_700_000_000


def bar(i, o, h, l, c):
    return {"ts": T0 + i * 300, "o": o, "h": h, "l": l, "c": c}


def flat(i, px):
    return bar(i, px, px + 0.2, px - 0.2, px)


LONG = {"direction": "LONG", "entry": 100.0, "sl": 98.0, "tp1": 104.0, "tp2": 106.0}


# ------------------------------------------------------------- simulation

def test_win_path_tp1_and_tp2():
    bars = [flat(0, 101)]                       # confirm bar
    bars.append(bar(1, 101, 101.2, 99.8, 100.1))    # fills the 100 limit
    bars.append(bar(2, 100.1, 103, 99.5, 102.8))
    bars.append(bar(3, 102.8, 104.5, 102.5, 104.2))  # TP1 104 hit
    bars.append(bar(4, 104.2, 106.5, 104.0, 106.2))  # TP2 106 later
    out = simulate_outcome(dict(LONG), bars, 0, CFG)
    assert out["outcome"] == "TP1" and out["r"] == 2.0 and out["tp2_hit"] is True
    assert out["mfe"] >= 2.0 and out["mae"] < 1.0


def test_loss_path_sl():
    bars = [flat(0, 101), bar(1, 101, 101.2, 99.9, 100.0),
            bar(2, 100, 100.4, 97.8, 98.1)]     # SL 98 hit
    out = simulate_outcome(dict(LONG), bars, 0, CFG)
    assert out["outcome"] == "STOPPED" and out["r"] == -1.0


def test_same_bar_ambiguity_is_sl_first():
    bars = [flat(0, 101), bar(1, 101, 101.2, 99.9, 100.0),
            bar(2, 100, 104.5, 97.9, 99.0)]     # bar spans BOTH SL and TP1
    out = simulate_outcome(dict(LONG), bars, 0, CFG)
    assert out["outcome"] == "STOPPED"          # conservative


def test_expired_when_entry_never_fills():
    bars = [flat(0, 101)] + [flat(i, 103) for i in range(1, 40)]
    out = simulate_outcome(dict(LONG), bars, 0, CFG)
    assert out["outcome"] == "EXPIRED"


def test_timeout_marks_to_market():
    cfg = V3Config(replay_horizon_bars=5)
    bars = [flat(0, 101), bar(1, 101, 101.2, 99.9, 100.0)]
    bars += [flat(i, 101.0) for i in range(2, 12)]      # drifts, no SL/TP
    out = simulate_outcome(dict(LONG), bars, 0, cfg)
    assert out["outcome"] == "TIMEOUT" and 0 < out["r"] < 1


def test_short_mirror():
    s = {"direction": "SHORT", "entry": 100.0, "sl": 102.0, "tp1": 96.0, "tp2": None}
    bars = [flat(0, 99), bar(1, 99, 100.3, 98.8, 99.5),
            bar(2, 99.5, 99.8, 95.8, 96.0)]
    out = simulate_outcome(s, bars, 0, CFG)
    assert out["outcome"] == "TP1" and out["r"] == 2.0


# ------------------------------------------------------------- aggregation

def test_aggregate_scoreboard_math():
    trades = [
        {"outcome": "TP1", "r": 2.0, "hold": 10, "tp2_hit": True},
        {"outcome": "TP1", "r": 2.0, "hold": 6, "tp2_hit": False},
        {"outcome": "STOPPED", "r": -1.0, "hold": 4, "tp2_hit": False},
        {"outcome": "TIMEOUT", "r": 0.5, "hold": 288, "tp2_hit": False},
        {"outcome": "EXPIRED", "r": 0.0},
    ]
    a = aggregate(trades)
    assert a["n"] == 4 and a["expired"] == 1
    assert a["win_rate"] == 0.75                        # 3 of 4 positive
    assert a["expectancy"] == round((2 + 2 - 1 + 0.5) / 4, 2)
    assert a["profit_factor"] == 4.5                    # 4.5 win / 1.0 loss
    assert a["total_r"] == 3.5
    assert a["tp2_rate"] == 0.25
    assert a["max_drawdown"] == 1.0                     # the -1 dip after peak


def test_aggregate_empty():
    a = aggregate([{"outcome": "EXPIRED", "r": 0.0}])
    assert a["n"] == 0 and a["expired"] == 1 and a["win_rate"] is None


# ------------------------------------------------------------- determinism

def test_simulation_deterministic():
    bars = [flat(0, 101), bar(1, 101, 101.2, 99.8, 100.1),
            bar(2, 100.1, 104.5, 99.9, 104.2)]
    a = json.dumps(simulate_outcome(dict(LONG), bars, 0, CFG), sort_keys=True)
    b = json.dumps(simulate_outcome(dict(LONG), bars, 0, CFG), sort_keys=True)
    assert a == b
