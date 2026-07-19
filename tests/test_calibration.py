"""Tests for the config-sweep calibration tooling (roadmap P5.3)."""

from __future__ import annotations

from datetime import timedelta

from conftest import TxPool
from marketscalper import db
from marketscalper.calibration import config_stats, rank_sweep, sweep


# ---------------------------------------------------- config_stats (pure)


def _admit(fee_r, direction="LONG"):
    return {"fee_r": fee_r, "direction": direction}


def _term(status, outcome, eval_r):
    return {"status": status, "outcome": outcome, "eval_r": eval_r}


def test_config_stats_net_expectancy_subtracts_fees_hand_computed():
    admitted = {
        ("t1", "S1"): _admit(0.002),
        ("t2", "S1"): _admit(0.002),
        ("t3", "S2"): _admit(0.010),          # invalidated -> excluded
    }
    terminal = {
        ("t1", "S1"): _term("evaluated", "tp1", 2.0),
        ("t2", "S1"): _term("evaluated", "sl", -1.0),
        ("t3", "S2"): _term("invalidated", None, None),
    }
    s = config_stats(admitted, terminal)
    assert s["n_admitted"] == 3
    assert s["n_evaluated"] == 2                    # t3 never filled
    assert s["gross_expectancy"] == (2.0 - 1.0) / 2      # 0.5
    # net subtracts each trade's fee-in-R: ((2-0.002)+(-1-0.002))/2
    assert abs(s["net_expectancy"] - 0.498) < 1e-12
    assert s["win_rate"] == 0.5                     # 1 win / 2


def test_config_stats_excludes_missing_and_nonterminal():
    admitted = {("t1", "S1"): _admit(0.0), ("t2", "S1"): _admit(0.0)}
    terminal = {("t1", "S1"): _term("expired", None, None)}   # no fill; t2 absent
    s = config_stats(admitted, terminal)
    assert s["n_admitted"] == 2 and s["n_evaluated"] == 0
    assert s["net_expectancy"] is None and s["win_rate"] is None


def test_config_stats_empty():
    s = config_stats({}, {})
    assert s == {"n_admitted": 0, "n_evaluated": 0, "gross_expectancy": None,
                 "net_expectancy": None, "win_rate": None}


# ---------------------------------------------------- rank_sweep (pure)


def _res(label, n_eval, net):
    return {"label": label,
            "stats": {"n_evaluated": n_eval, "net_expectancy": net}}


def test_rank_sweep_orders_by_net_expectancy_above_the_sample_floor():
    results = [_res("A", 5, 0.3), _res("B", 5, 0.5),
               _res("C", 1, 0.9), _res("D", 0, None)]
    rep = rank_sweep(results, min_evaluated=3)
    assert rep["n_configs"] == 4 and rep["n_eligible"] == 2   # A, B
    assert [r["label"] for r in rep["ranked"]] == ["B", "A"]  # net desc
    assert rep["best_label"] == "B"
    assert [r["label"] for r in rep["results"]] == ["A", "B", "C", "D"]
    assert "owner" in rep["note"]


def test_rank_sweep_low_floor_admits_the_small_sample():
    results = [_res("A", 5, 0.3), _res("B", 5, 0.5), _res("C", 1, 0.9)]
    rep = rank_sweep(results, min_evaluated=1)
    assert rep["best_label"] == "C"                # 0.9 wins once C is eligible


def test_rank_sweep_ties_keep_input_order():
    results = [_res("A", 5, 0.4), _res("B", 5, 0.4), _res("C", 5, 0.4)]
    rep = rank_sweep(results, min_evaluated=1)
    assert [r["label"] for r in rep["ranked"]] == ["A", "B", "C"]  # stable


def test_rank_sweep_none_eligible_is_inconclusive():
    rep = rank_sweep([_res("A", 1, 0.9), _res("B", 2, 0.5)], min_evaluated=10)
    assert rep["n_eligible"] == 0 and rep["best_label"] is None
    assert "inconclusive" in rep["note"]


# ---------------------------------------------------- sweep (integration)


async def test_sweep_drives_the_real_composition_per_config(db_conn):
    """The sweep runs the full composition once per combo via the injected
    ReplayFeed + wiring, capturing the admitted recommendations. The seeded
    rec_dataset admits one S1 recommendation, so every config's stats carry
    it — proving the sweep drives the real chain (not a stub)."""
    from rec_dataset import REC_M0, REC_MINUTES, rec_candles, rec_seed
    from marketscalper.engines.momentum import RegimeConfig
    from marketscalper.main import _wire_structure_engines
    from marketscalper.providers.replay import ReplayFeed

    v4 = rec_candles("BTCUSDT")
    await db.insert_candles(
        db_conn,
        [(c.symbol, c.tf, c.ts, c.o, c.h, c.l, c.c, c.v, c.qv,
          c.n_trades, c.taker_buy_v) for c in v4],
    )
    start, end = REC_M0, REC_M0 + timedelta(minutes=REC_MINUTES)
    seed = {"BTCUSDT": rec_seed("BTCUSDT")}
    combos = [
        {"label": "default"},                                    # D9 defaults
        {"label": "wide-expansion",
         "regime_cfg": RegimeConfig(0.6, 3.0, 240)},             # tuned
    ]
    report = await sweep(
        TxPool(db_conn), "BTCUSDT", start, end, combos,
        replay_cls=ReplayFeed, wiring=_wire_structure_engines,
        seed_candles=seed, min_evaluated=1)

    assert report["n_configs"] == 2
    assert [r["label"] for r in report["results"]] == ["default",
                                                       "wide-expansion"]
    default_stats = report["results"][0]["stats"]
    assert default_stats["n_admitted"] >= 1        # the known S1 recommendation
