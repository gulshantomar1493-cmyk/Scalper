"""Simulation-only paper-trading engine (P6, decision D31).

PURE math — no real execution, no exchange API, no networking, no persistence.
The API layer loads state from the papertrade tables, applies these functions,
and writes the result back. Fully isolated from the frozen analysis engines, the
event bus, the `structure` payload, and the §10 determinism stream.

Accounting model (isolated margin, retail-simple):
  * balance = free cash, adjusted by (realized PnL - fees) on every fill.
  * a position reserves `margin` = notional / leverage (a constraint + the
    liquidation basis); no cash is moved on open.
  * equity  = balance + sum(unrealized PnL of open positions).
  * a position liquidates when its unrealized loss reaches its margin.
"""

from __future__ import annotations

DEFAULT_TAKER_FEE = 0.0005
DEFAULT_LEVERAGE = 1.0
_EPS = 1e-9


def unrealized_pnl(side: str, avg_entry: float, qty: float, mark: float) -> float:
    diff = (mark - avg_entry) if side == "LONG" else (avg_entry - mark)
    return diff * qty


def liquidation_price(side: str, avg_entry: float, leverage: float) -> float:
    """Price at which the isolated margin is exhausted (fees/maintenance ignored)."""
    if leverage is None or leverage <= 0:
        return 0.0
    frac = 1.0 / leverage
    return avg_entry * (1.0 - frac) if side == "LONG" else avg_entry * (1.0 + frac)


def margin_of(qty: float, price: float, leverage: float) -> float:
    lev = leverage if leverage and leverage > 0 else 1.0
    return qty * price / lev


def _signed(position) -> float:
    if not position:
        return 0.0
    return position["qty"] if position["side"] == "LONG" else -position["qty"]


def apply_fill(position, *, side: str, qty: float, price: float,
               leverage: float, taker_fee: float, reduce_only: bool = False):
    """Apply a filled order to `position` (dict or None).
    Returns (new_position|None, realized_pnl, fee, filled_qty).
    side = 'BUY' | 'SELL'. Handles open / increase / reduce / close / reverse."""
    if qty <= 0 or price <= 0:
        return position, 0.0, 0.0, 0.0
    cur = _signed(position)
    entry = position["avg_entry"] if position else 0.0
    lev = leverage if leverage and leverage > 0 else 1.0
    delta = qty if side == "BUY" else -qty

    if reduce_only:
        if not position or (cur > 0) == (delta > 0):
            return position, 0.0, 0.0, 0.0          # would open/increase — rejected
        if abs(delta) > abs(cur):
            delta = -cur                            # cap at a full close (no flip)

    filled_qty = abs(delta)
    fee = filled_qty * price * taker_fee
    realized = 0.0
    if cur != 0 and (cur > 0) != (delta > 0):       # opposing fill -> a reduction
        reduced = min(abs(cur), abs(delta))
        realized = reduced * ((price - entry) if cur > 0 else (entry - price))

    new_signed = cur + delta
    if abs(new_signed) < _EPS:                      # closed flat
        return None, realized, fee, filled_qty

    same_dir_increase = cur != 0 and (cur > 0) == (delta > 0)
    flipped = cur != 0 and (cur > 0) != (new_signed > 0)
    new_qty = abs(new_signed)
    if same_dir_increase:
        new_entry = (abs(cur) * entry + qty * price) / (abs(cur) + qty)
    elif flipped:
        new_entry = price                           # fresh position at the fill price
    else:
        new_entry = entry if cur != 0 else price    # opened from flat, or partial reduce

    new_side = "LONG" if new_signed > 0 else "SHORT"
    pos = {
        "symbol": position["symbol"] if position else None,
        "side": new_side, "qty": new_qty, "avg_entry": new_entry, "leverage": lev,
        "margin": margin_of(new_qty, new_entry, lev),
        "liq_price": liquidation_price(new_side, new_entry, lev),
    }
    return pos, realized, fee, filled_qty


def is_liquidated(position, mark: float) -> bool:
    """True when the position's unrealized loss has reached its margin."""
    if not position:
        return False
    up = unrealized_pnl(position["side"], position["avg_entry"], position["qty"], mark)
    return up <= -position["margin"] + _EPS


def order_triggers(order, mark: float) -> bool:
    """Does a resting limit/stop order fill at the current mark price?"""
    t = order["type"]
    if t == "limit":
        return (order["side"] == "BUY" and mark <= order["price"]) or \
               (order["side"] == "SELL" and mark >= order["price"])
    if t == "stop":
        sp = order["stop_price"]
        return (order["side"] == "BUY" and mark >= sp) or \
               (order["side"] == "SELL" and mark <= sp)
    return True                                     # market: always


def portfolio(account, positions, marks: dict) -> dict:
    """A mark-to-market portfolio summary."""
    used_margin = sum(p["margin"] for p in positions)
    unreal = sum(unrealized_pnl(p["side"], p["avg_entry"], p["qty"],
                                marks.get(p["symbol"], p["avg_entry"])) for p in positions)
    balance = account["balance"]
    equity = balance + unreal
    start = account["starting_balance"] or balance
    realized = balance - start                 # Paper V2 (B3): explicit realized P&L
    return {
        "balance": balance,
        "equity": equity,
        "used_margin": used_margin,
        "available_margin": balance - used_margin,
        "unrealized_pnl": unreal,
        "realized_pnl": realized,              # cumulative closed-trade P&L (this account)
        "total_pnl": realized + unreal,        # realized + open — the "Total P&L"
        "roi_pct": ((equity - start) / start * 100.0) if start else 0.0,
        "open_positions": len(positions),
    }
