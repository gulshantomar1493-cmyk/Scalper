"""Paper-trading DB orchestration (P6, decision D31).

Loads state from the four papertrade tables, applies the PURE engine
(core.papertrade), and writes the result back. A self-contained subsystem — it
touches only its own tables, never a frozen table, never the event bus, never the
`structure` payload, never the determinism stream. Async; each call runs inside
the caller's DB transaction. It READS the live mark price (passed in) for
mark-to-market — it never places a real order.
"""

from __future__ import annotations

from datetime import datetime, timezone

from marketscalper.core import papertrade as pt

DEFAULT_BALANCE = 10000.0
_POS_COLS = ("id, symbol, side, qty, avg_entry, leverage, margin, liq_price,"
             " realized_pnl, fees_paid, opened_at")


def _f(v):
    return float(v) if v is not None else None


def _pos_dict(row) -> dict:
    return {"id": row["id"], "symbol": row["symbol"], "side": row["side"],
            "qty": float(row["qty"]), "avg_entry": float(row["avg_entry"]),
            "leverage": float(row["leverage"]), "margin": float(row["margin"]),
            "liq_price": _f(row["liq_price"]) or 0.0,
            "realized_pnl": float(row["realized_pnl"] or 0),
            "fees_paid": float(row["fees_paid"] or 0),
            "opened_at": row["opened_at"].isoformat() if row["opened_at"] else None}


async def get_or_create_account(conn) -> dict:
    row = await conn.fetchrow(
        "SELECT id, balance, starting_balance, taker_fee FROM paper_account ORDER BY id LIMIT 1")
    if row is None:
        row = await conn.fetchrow(
            "INSERT INTO paper_account (balance, starting_balance, taker_fee)"
            " VALUES ($1, $1, $2)"
            " RETURNING id, balance, starting_balance, taker_fee",
            DEFAULT_BALANCE, pt.DEFAULT_TAKER_FEE)
    return {"id": row["id"], "balance": _f(row["balance"]),
            "starting_balance": _f(row["starting_balance"]), "taker_fee": _f(row["taker_fee"])}


async def reset_wallet(conn, balance: float) -> dict:
    acct = await get_or_create_account(conn)
    await conn.execute("UPDATE paper_positions SET status='closed', closed_at=now() WHERE status='open'")
    await conn.execute("UPDATE paper_orders SET status='cancelled' WHERE status='open'")
    await conn.execute(
        "UPDATE paper_account SET balance=$2, starting_balance=$2, updated_at=now() WHERE id=$1",
        acct["id"], balance)
    return await get_or_create_account(conn)


async def _apply_fill(conn, acct, *, symbol, side, qty, price, leverage,
                      reduce_only, order_id=None):
    row = await conn.fetchrow(
        f"SELECT {_POS_COLS} FROM paper_positions WHERE symbol=$1 AND status='open' ORDER BY id LIMIT 1",
        symbol)
    cur = _pos_dict(row) if row else None
    lev = leverage or (cur["leverage"] if cur else pt.DEFAULT_LEVERAGE)
    new_pos, realized, fee, filled = pt.apply_fill(
        cur, side=side, qty=qty, price=price, leverage=lev,
        taker_fee=acct["taker_fee"], reduce_only=reduce_only)
    if filled <= 0:
        return {"filled": 0.0, "realized": 0.0, "fee": 0.0}
    now = datetime.now(timezone.utc)
    if new_pos is None:
        await conn.execute(
            "UPDATE paper_positions SET status='closed', closed_at=$2,"
            " realized_pnl=realized_pnl+$3, fees_paid=fees_paid+$4 WHERE id=$1",
            row["id"], now, realized, fee)
        position_id = row["id"]
    elif cur is None:
        rec = await conn.fetchrow(
            "INSERT INTO paper_positions (symbol, side, qty, avg_entry, leverage,"
            " margin, liq_price, fees_paid) VALUES ($1,$2,$3,$4,$5,$6,$7,$8) RETURNING id",
            symbol, new_pos["side"], new_pos["qty"], new_pos["avg_entry"],
            new_pos["leverage"], new_pos["margin"], new_pos["liq_price"], fee)
        position_id = rec["id"]
    else:
        await conn.execute(
            "UPDATE paper_positions SET side=$2, qty=$3, avg_entry=$4, leverage=$5,"
            " margin=$6, liq_price=$7, realized_pnl=realized_pnl+$8, fees_paid=fees_paid+$9"
            " WHERE id=$1",
            row["id"], new_pos["side"], new_pos["qty"], new_pos["avg_entry"],
            new_pos["leverage"], new_pos["margin"], new_pos["liq_price"], realized, fee)
        position_id = row["id"]
    await conn.execute(
        "UPDATE paper_account SET balance=balance+$2, updated_at=now() WHERE id=$1",
        acct["id"], realized - fee)
    acct["balance"] += realized - fee
    await conn.execute(
        "INSERT INTO paper_trades (symbol, side, qty, price, fee, realized_pnl, position_id, order_id)"
        " VALUES ($1,$2,$3,$4,$5,$6,$7,$8)",
        symbol, side, filled, price, fee, realized, position_id, order_id)
    return {"filled": filled, "realized": realized, "fee": fee}


async def _sync(conn, acct, marks: dict):
    # 1) trigger resting limit/stop orders at the mark
    orders = await conn.fetch(
        "SELECT id, symbol, side, type, qty, price, stop_price, leverage, reduce_only"
        " FROM paper_orders WHERE status='open' ORDER BY id")
    for o in orders:
        mark = marks.get(o["symbol"])
        if mark is None:
            continue
        order = {"type": o["type"], "side": o["side"],
                 "price": _f(o["price"]), "stop_price": _f(o["stop_price"])}
        if not pt.order_triggers(order, mark):
            continue
        fill_price = _f(o["price"]) if o["type"] == "limit" else mark   # limit at its price, stop at mark
        await _apply_fill(conn, acct, symbol=o["symbol"], side=o["side"],
                          qty=float(o["qty"]), price=fill_price, leverage=_f(o["leverage"]),
                          reduce_only=o["reduce_only"], order_id=o["id"])
        await conn.execute("UPDATE paper_orders SET status='filled', fill_price=$2, filled_at=now() WHERE id=$1",
                           o["id"], fill_price)
    # 2) liquidate positions whose loss reached the margin
    for row in await conn.fetch(f"SELECT {_POS_COLS} FROM paper_positions WHERE status='open' ORDER BY id"):
        pos = _pos_dict(row)
        mark = marks.get(pos["symbol"])
        if mark is not None and pt.is_liquidated(pos, mark):
            side = "SELL" if pos["side"] == "LONG" else "BUY"
            await _apply_fill(conn, acct, symbol=pos["symbol"], side=side,
                              qty=pos["qty"], price=pos["liq_price"] or mark,
                              leverage=pos["leverage"], reduce_only=True)


async def get_state(conn, marks: dict) -> dict:
    acct = await get_or_create_account(conn)
    await _sync(conn, acct, marks)
    acct = await get_or_create_account(conn)          # re-read post-sync balance
    positions = [_pos_dict(r) for r in await conn.fetch(
        f"SELECT {_POS_COLS} FROM paper_positions WHERE status='open' ORDER BY id")]
    for p in positions:
        mk = marks.get(p["symbol"], p["avg_entry"])
        p["mark"] = mk
        p["unrealized_pnl"] = pt.unrealized_pnl(p["side"], p["avg_entry"], p["qty"], mk)
    orders = [{"id": o["id"], "symbol": o["symbol"], "side": o["side"], "type": o["type"],
               "qty": _f(o["qty"]), "price": _f(o["price"]), "stop_price": _f(o["stop_price"]),
               "leverage": _f(o["leverage"]), "reduce_only": o["reduce_only"]}
              for o in await conn.fetch(
                  "SELECT id, symbol, side, type, qty, price, stop_price, leverage, reduce_only"
                  " FROM paper_orders WHERE status='open' ORDER BY id")]
    history = [{"ts": t["ts"].isoformat() if t["ts"] else None, "symbol": t["symbol"],
                "side": t["side"], "qty": _f(t["qty"]), "price": _f(t["price"]),
                "fee": _f(t["fee"]), "realized_pnl": _f(t["realized_pnl"])}
               for t in await conn.fetch(
                   "SELECT ts, symbol, side, qty, price, fee, realized_pnl"
                   " FROM paper_trades ORDER BY id DESC LIMIT 100")]
    return {"account": acct, "positions": positions, "orders": orders,
            "portfolio": pt.portfolio(acct, positions, marks), "history": history}


async def place_order(conn, spec: dict, marks: dict) -> dict:
    acct = await get_or_create_account(conn)
    symbol, otype = spec["symbol"], spec["type"]
    if otype == "market":
        mark = marks.get(symbol)
        if mark is None:
            raise ValueError("no live price for this symbol yet")
        return await _apply_fill(conn, acct, symbol=symbol, side=spec["side"],
                                 qty=spec["qty"], price=mark, leverage=spec.get("leverage"),
                                 reduce_only=spec.get("reduce_only", False))
    rec = await conn.fetchrow(
        "INSERT INTO paper_orders (symbol, side, type, qty, price, stop_price, leverage, reduce_only)"
        " VALUES ($1,$2,$3,$4,$5,$6,$7,$8) RETURNING id",
        symbol, spec["side"], otype, spec["qty"], spec.get("price"),
        spec.get("stop_price"), spec.get("leverage"), spec.get("reduce_only", False))
    return {"order_id": rec["id"], "status": "open"}


async def cancel_order(conn, order_id: int) -> bool:
    res = await conn.execute(
        "UPDATE paper_orders SET status='cancelled' WHERE id=$1 AND status='open'", order_id)
    return int(res.split()[-1]) > 0


async def close_position(conn, position_id: int, marks: dict) -> dict:
    acct = await get_or_create_account(conn)
    row = await conn.fetchrow(
        f"SELECT {_POS_COLS} FROM paper_positions WHERE id=$1 AND status='open'", position_id)
    if row is None:
        raise ValueError("position not found or already closed")
    pos = _pos_dict(row)
    mark = marks.get(pos["symbol"], pos["avg_entry"])
    side = "SELL" if pos["side"] == "LONG" else "BUY"
    return await _apply_fill(conn, acct, symbol=pos["symbol"], side=side,
                             qty=pos["qty"], price=mark, leverage=pos["leverage"],
                             reduce_only=True)
