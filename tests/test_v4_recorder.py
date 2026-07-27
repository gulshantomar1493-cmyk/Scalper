"""V4 recorder + outcome accounting tests — the honesty guarantees."""
import numpy as np
import pytest

from marketscalper.v4.outcome import advance, summarise, TP, SL, TIME, CANCELLED
from marketscalper.v4 import config as C


class S:
    def __init__(self, d, entry, stop, target, ts=0):
        self.direction, self.entry, self.stop, self.target, self.decision_ts = d, entry, stop, target, ts


def bars(prices, start=0):
    """prices: list of (o,h,l,c) at 1-minute spacing."""
    n = len(prices)
    return dict(ts=np.arange(n, dtype=np.int64) * 60 + start,
                o=np.array([p[0] for p in prices], float),
                h=np.array([p[1] for p in prices], float),
                l=np.array([p[2] for p in prices], float),
                c=np.array([p[3] for p in prices], float),
                v=np.ones(n), tf_s=60)


def test_stop_order_fills_only_when_price_trades_through():
    b = bars([(100, 100, 100, 100)] * 3 + [(100, 112, 100, 111)] * 2)
    o = advance(S(1, 110, 100, 200, 0), b)
    assert o.fill_price == 110.0


def test_gap_through_trigger_fills_worse_never_better():
    b = bars([(100, 100, 100, 100), (120, 125, 119, 124), (124, 126, 123, 125)])
    o = advance(S(1, 110, 100, 300, 0), b)
    assert o.fill_price == 120.0, "a gapped stop must fill at the open, not the trigger"


def test_loss_is_not_floored_at_minus_one_R():
    # fill 100, stop 90 (risk 10); next bar OPENS at 80 -> a real -2R before costs
    b = bars([(100, 101, 99, 100), (80, 85, 78, 80), (80, 81, 79, 80)])
    o = advance(S(1, 100, 90, 500, 0), b)
    assert o.status == SL
    assert o.gross_r == pytest.approx(-2.0, abs=1e-6)
    assert o.net_r < -2.0, "fees and funding must make it worse than the raw move"


def test_favourable_gap_through_target_is_not_credited():
    b = bars([(100, 101, 99, 100), (140, 150, 139, 145)])
    o = advance(S(1, 100, 90, 120, 0), b)
    assert o.status == TP
    assert o.gross_r == pytest.approx(2.0, abs=1e-6), "must credit the target, not the gap"


def test_same_bar_ambiguity_resolves_to_stop():
    b = bars([(100, 101, 99, 100), (100, 130, 80, 100)])
    o = advance(S(1, 100, 90, 120, 0), b)
    assert o.status == SL


def test_fees_and_funding_are_always_charged():
    b = bars([(100, 101, 99, 100)] + [(100, 121, 99, 120)] * 2)
    o = advance(S(1, 100, 90, 120, 0), b)
    assert o.fee_r > 0 and o.funding_r >= 0
    assert o.net_r == pytest.approx(o.gross_r - o.fee_r - o.funding_r, abs=1e-9)


def test_unfilled_order_is_cancelled_not_counted_as_a_loss():
    b = bars([(100, 101, 99, 100)] * (C.ENTRY_VALID_BARS_MIN + 5))
    o = advance(S(1, 200, 190, 300, 0), b)
    assert o.status == CANCELLED
    assert o.net_r is None, "a cancelled order must not contribute an R value"


def test_time_exit_is_a_real_exit_and_is_reported_separately():
    n = 1440 * C.MAX_HOLD_DAYS + 30
    b = bars([(100, 101, 99, 100)] + [(100, 105, 99, 101)] * n)
    o = advance(S(1, 100, 90, 900, 0), b)
    assert o.status == TIME
    assert o.fee_r > 0, "a horizon exit is a market close and pays fees"
    s = summarise([dict(status=o.status, net_r=o.net_r, fee_r=o.fee_r,
                        hold_minutes=o.hold_minutes)])
    assert s["time_exit"] == 1 and s["tp"] == 0


def test_summarise_never_mixes_time_exits_into_target_hits():
    rows = [dict(status=TP, net_r=2.0, fee_r=0.01, hold_minutes=10),
            dict(status=SL, net_r=-1.1, fee_r=0.01, hold_minutes=5),
            dict(status=TIME, net_r=0.05, fee_r=0.01, hold_minutes=99)]
    s = summarise(rows)
    assert s["tp"] == 1 and s["sl"] == 1 and s["time_exit"] == 1
    assert s["n"] == 3
    assert s["total_r"] == pytest.approx(0.95, abs=1e-9)


# ------------------------------------------------ the cycle's tape windowing --
# open_setups() returns rows NEWEST-first. Keying the 1m tape off the first row
# would start the window AFTER older rows began, so those rows could never fill
# or resolve — they would sit OPEN forever. The tape must start at the OLDEST
# open row per symbol.

class _FakeStore:
    def __init__(self, rows):
        self.rows = rows
        self.applied = []

    async def record(self, setups):
        return 0

    async def open_setups(self):
        return list(self.rows)          # newest-first, as the real query returns

    async def apply_outcome(self, key, out):
        self.applied.append((key, out.status))
        return True


class _FakeChart:
    """Serves 1m candles from a fixed tape, honouring the requested window."""

    def __init__(self, first_ts, prices):
        self.first_ts = first_ts
        self.prices = prices
        self.requested = []

    async def get_chart(self, symbol, tf, start, end):
        from datetime import datetime, timezone
        self.requested.append(int(start.timestamp()))
        out = []
        for i, (o, h, l, c) in enumerate(self.prices):
            ts = self.first_ts + i * 60
            if start.timestamp() <= ts < end.timestamp():
                out.append({"ts": datetime.fromtimestamp(ts, tz=timezone.utc),
                            "o": o, "h": h, "l": l, "c": c, "v": 1.0})
        return {"candles": out}


class _FakeSvc:
    async def all_setups(self):
        return []


def _row(key, ts):
    return {"setup_key": key, "symbol": "ETHUSDT", "direction": 1, "decision_ts": ts,
            "entry": 100.0, "stop": 90.0, "target": 200.0}


@pytest.mark.asyncio
async def test_cycle_tape_starts_at_the_oldest_open_row_not_the_newest():
    import time
    from marketscalper.v4.recorder import V4Recorder

    now = int(time.time()) // 60 * 60
    old_ts = now - 120 * 60                      # an old row, still open
    new_ts = now - 10 * 60                       # a newer row on the same symbol

    # tape covers the old row: it triggers at 100 and runs to the 200 target
    first = old_ts - 10 * 60
    prices = [(95, 96, 94, 95)] * 12             # before the old decision
    prices += [(95, 99, 94, 98)] * 100           # no trigger yet
    prices += [(99, 210, 99, 205)] * 20          # trigger, then target

    store = _FakeStore([_row("new", new_ts), _row("old", old_ts)])   # NEWEST first
    chart = _FakeChart(first, prices)
    rec = V4Recorder(_FakeSvc(), store, chart)
    await rec.cycle()

    # exactly one fetch per symbol, and it starts before the OLDEST row
    assert len(chart.requested) == 1
    assert chart.requested[0] <= old_ts
    # the old row resolved — with the buggy window it never could
    assert ("old", TP) in store.applied


@pytest.mark.asyncio
async def test_cycle_fetches_one_tape_per_symbol():
    import time
    from marketscalper.v4.recorder import V4Recorder

    now = int(time.time()) // 60 * 60
    rows = [_row("a", now - 30 * 60), _row("b", now - 60 * 60)]
    rows.append({**_row("c", now - 90 * 60), "symbol": "BTCUSDT"})
    chart = _FakeChart(now - 200 * 60, [(95, 96, 94, 95)] * 200)
    rec = V4Recorder(_FakeSvc(), _FakeStore(rows), chart)
    await rec.cycle()
    assert len(chart.requested) == 2          # two symbols, two fetches
