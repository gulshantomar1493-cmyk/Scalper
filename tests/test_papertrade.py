"""Paper-trading engine (core/papertrade.py) — pure math unit tests (P6, D31).

No DB, no execution. Validates the isolated-margin fill logic: open / increase /
reduce / close / reverse, PnL, fees, leverage, liquidation, and the portfolio.
"""

from __future__ import annotations

from marketscalper.core import papertrade as pt

FEE = 0.0005


def _open(side_buy_sell, qty, price, lev):
    return pt.apply_fill(None, side=side_buy_sell, qty=qty, price=price,
                         leverage=lev, taker_fee=FEE)


def test_open_long_market():
    pos, realized, fee, filled = _open("BUY", 1.0, 100.0, 10.0)
    assert pos["side"] == "LONG" and pos["qty"] == 1.0 and pos["avg_entry"] == 100.0
    assert pos["leverage"] == 10.0 and pos["margin"] == 10.0        # 1*100/10
    assert pos["liq_price"] == 90.0                                 # 100*(1-0.1)
    assert realized == 0.0 and fee == 1.0 * 100.0 * FEE and filled == 1.0


def test_increase_weighted_avg_entry():
    pos, *_ = _open("BUY", 1.0, 100.0, 10.0)
    pos, realized, _f, _q = pt.apply_fill(pos, side="BUY", qty=1.0, price=120.0,
                                          leverage=10.0, taker_fee=FEE)
    assert pos["qty"] == 2.0 and pos["avg_entry"] == 110.0 and realized == 0.0
    assert pos["margin"] == 2.0 * 110.0 / 10.0                      # 22


def test_reduce_realizes_pnl_keeps_entry():
    pos, *_ = _open("BUY", 2.0, 110.0, 10.0)
    pos, realized, _f, _q = pt.apply_fill(pos, side="SELL", qty=1.0, price=130.0,
                                          leverage=10.0, taker_fee=FEE)
    assert realized == 1.0 * (130.0 - 110.0)                        # +20
    assert pos["qty"] == 1.0 and pos["avg_entry"] == 110.0          # entry unchanged


def test_close_returns_none_with_pnl():
    pos, *_ = _open("BUY", 1.0, 110.0, 10.0)
    pos, realized, _f, _q = pt.apply_fill(pos, side="SELL", qty=1.0, price=130.0,
                                          leverage=10.0, taker_fee=FEE)
    assert pos is None and realized == 20.0


def test_reverse_flips_side_fresh_entry():
    pos, *_ = _open("BUY", 2.0, 110.0, 10.0)
    pos, realized, _f, _q = pt.apply_fill(pos, side="SELL", qty=3.0, price=130.0,
                                          leverage=10.0, taker_fee=FEE)
    assert realized == 2.0 * (130.0 - 110.0)                        # realized on the closed 2
    assert pos["side"] == "SHORT" and pos["qty"] == 1.0 and pos["avg_entry"] == 130.0


def test_short_open_and_close_mirror():
    pos, *_ = _open("SELL", 1.0, 100.0, 5.0)
    assert pos["side"] == "SHORT" and pos["margin"] == 20.0 and pos["liq_price"] == 120.0
    pos, realized, _f, _q = pt.apply_fill(pos, side="BUY", qty=1.0, price=90.0,
                                          leverage=5.0, taker_fee=FEE)
    assert pos is None and realized == 10.0                         # 1*(100-90)


def test_reduce_only_rejects_open_and_caps_close():
    # reduce-only on flat -> no-op
    pos, r, f, q = pt.apply_fill(None, side="BUY", qty=1.0, price=100.0,
                                 leverage=10.0, taker_fee=FEE, reduce_only=True)
    assert pos is None and (r, f, q) == (0.0, 0.0, 0.0)
    # reduce-only opposing but oversized -> capped to exactly close (no flip)
    pos, *_ = _open("BUY", 2.0, 100.0, 10.0)
    pos, realized, _f, filled = pt.apply_fill(pos, side="SELL", qty=5.0, price=110.0,
                                              leverage=10.0, taker_fee=FEE, reduce_only=True)
    assert pos is None and filled == 2.0 and realized == 2.0 * 10.0


def test_unrealized_pnl_and_liquidation():
    assert pt.unrealized_pnl("LONG", 100.0, 2.0, 110.0) == 20.0
    assert pt.unrealized_pnl("SHORT", 100.0, 2.0, 110.0) == -20.0
    assert pt.liquidation_price("LONG", 100.0, 4.0) == 75.0
    assert pt.liquidation_price("SHORT", 100.0, 4.0) == 125.0
    pos, *_ = _open("BUY", 1.0, 100.0, 10.0)                        # margin 10
    assert pt.is_liquidated(pos, 90.0) is True                     # -10 <= -10
    assert pt.is_liquidated(pos, 91.0) is False


def test_order_triggers():
    assert pt.order_triggers({"type": "limit", "side": "BUY", "price": 100.0}, 99.0)
    assert not pt.order_triggers({"type": "limit", "side": "BUY", "price": 100.0}, 101.0)
    assert pt.order_triggers({"type": "limit", "side": "SELL", "price": 100.0}, 101.0)
    assert pt.order_triggers({"type": "stop", "side": "BUY", "stop_price": 105.0}, 106.0)
    assert not pt.order_triggers({"type": "stop", "side": "SELL", "stop_price": 95.0}, 96.0)
    assert pt.order_triggers({"type": "market", "side": "BUY"}, 123.0)


def test_portfolio_summary():
    acct = {"balance": 10000.0, "starting_balance": 10000.0}
    pos = [{"symbol": "BTCUSDT", "side": "LONG", "qty": 1.0, "avg_entry": 100.0, "margin": 10.0}]
    p = pt.portfolio(acct, pos, {"BTCUSDT": 110.0})
    assert p["unrealized_pnl"] == 10.0 and p["equity"] == 10010.0
    assert p["used_margin"] == 10.0 and p["available_margin"] == 9990.0
    assert round(p["roi_pct"], 3) == 0.1 and p["open_positions"] == 1
    assert p["realized_pnl"] == 0.0 and p["total_pnl"] == 10.0    # B3: realized + open


def test_portfolio_realized_and_total_pnl():
    """B3: realized = balance - starting; total = realized + unrealized."""
    acct = {"balance": 10250.0, "starting_balance": 10000.0}      # +250 closed
    pos = [{"symbol": "BTCUSDT", "side": "LONG", "qty": 1.0, "avg_entry": 100.0, "margin": 10.0}]
    p = pt.portfolio(acct, pos, {"BTCUSDT": 90.0})                # -10 open
    assert p["realized_pnl"] == 250.0
    assert p["unrealized_pnl"] == -10.0
    assert p["total_pnl"] == 240.0
