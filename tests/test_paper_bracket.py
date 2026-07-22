"""Paper Trading V2 — bracket orders (migration 006).

A resting order can carry its own SL/TP, applied to the position when the order
fills. This is what the one-click "Take setup" needs: a limit at the setup entry
whose bracket activates on fill and survives a page close. DB-level tests against
the real paper_service (isolated subsystem — no frozen table, no determinism).
"""

from __future__ import annotations

from marketscalper.core import paper_service as ps

_SYM = "BTCUSDT"


async def test_bracket_limit_applies_sltp_on_fill(db_conn):
    await ps.reset_wallet(db_conn, 10000.0)
    r = await ps.place_order(db_conn, {
        "symbol": _SYM, "side": "BUY", "type": "limit",
        "qty": 1.0, "price": 100.0, "sl": 90.0, "tp": 130.0, "leverage": 10}, {_SYM: 110.0})
    assert "order_id" in r
    # mark 110 > 100 -> the buy limit has not triggered; the order carries the bracket
    st = await ps.get_state(db_conn, {_SYM: 110.0})
    assert len(st["orders"]) == 1 and not st["positions"]
    assert st["orders"][0]["sl"] == 90.0 and st["orders"][0]["tp"] == 130.0
    # price returns to 100 -> the limit fills, and the bracket lands on the new position
    st = await ps.get_state(db_conn, {_SYM: 100.0})
    assert not st["orders"]
    assert len(st["positions"]) == 1
    pos = st["positions"][0]
    assert pos["side"] == "LONG" and pos["sl"] == 90.0 and pos["tp"] == 130.0


async def test_bracket_market_sets_position_sltp_immediately(db_conn):
    await ps.reset_wallet(db_conn, 10000.0)
    await ps.place_order(db_conn, {
        "symbol": _SYM, "side": "BUY", "type": "market",
        "qty": 1.0, "sl": 95.0, "tp": 120.0, "leverage": 10}, {_SYM: 100.0})
    st = await ps.get_state(db_conn, {_SYM: 100.0})
    assert len(st["positions"]) == 1
    assert st["positions"][0]["sl"] == 95.0 and st["positions"][0]["tp"] == 120.0


async def test_bracketed_position_closes_at_tp(db_conn):
    await ps.reset_wallet(db_conn, 10000.0)
    await ps.place_order(db_conn, {
        "symbol": _SYM, "side": "BUY", "type": "market",
        "qty": 1.0, "sl": 95.0, "tp": 120.0, "leverage": 10}, {_SYM: 100.0})
    # mark hits the take-profit -> the existing sync SL/TP path closes it
    st = await ps.get_state(db_conn, {_SYM: 120.0})
    assert not st["positions"]


async def test_order_without_bracket_leaves_position_unbracketed(db_conn):
    await ps.reset_wallet(db_conn, 10000.0)
    await ps.place_order(db_conn, {
        "symbol": _SYM, "side": "BUY", "type": "market", "qty": 1.0, "leverage": 10}, {_SYM: 100.0})
    st = await ps.get_state(db_conn, {_SYM: 100.0})
    assert len(st["positions"]) == 1
    assert st["positions"][0]["sl"] is None and st["positions"][0]["tp"] is None
