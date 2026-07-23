"""V3 Trade Recommendation History — pure status fold + DB round-trips."""

from __future__ import annotations

from datetime import datetime, timezone

from marketscalper.v3 import history as v3h
from marketscalper.v3.config import V3Config

CFG = V3Config()
T0 = 1_700_000_000


def bar(i, o, h, l, c):
    return {"ts": T0 + (i + 1) * 300, "o": o, "h": h, "l": l, "c": c}


def rec(direction="LONG", entry=100.0, sl=98.0, tp1=104.0, tp2=106.0):
    return {"direction": direction, "entry": entry, "sl": sl,
            "tp1": tp1, "tp2": tp2,
            "ts": datetime.fromtimestamp(T0, tz=timezone.utc).isoformat()}


# ------------------------------------------------------------- pure fold

def test_fill_then_tp1_then_tp2():
    bars = [bar(0, 101, 101.2, 99.8, 100.1),      # fills 100
            bar(1, 100.1, 104.5, 99.9, 104.2),    # TP1
            bar(2, 104.2, 106.5, 104.0, 106.2)]   # TP2
    upd = v3h.advance_recommendation(rec(), bars, CFG)
    assert upd["status"] == "TP2_HIT" and upd["result_r"] == 3.0
    assert upd["points_captured"] == 6.0 and upd["points_lost"] == 0.0
    assert upd["holding_minutes"] == 10


def test_tp1_only_at_horizon_end():
    cfg = V3Config(replay_horizon_bars=3)
    bars = [bar(0, 101, 101.2, 99.8, 100.1),
            bar(1, 100.1, 104.5, 99.9, 104.2),    # TP1, no TP2 after
            bar(2, 104.2, 104.4, 103.9, 104.0),
            bar(3, 104.0, 104.2, 103.8, 104.0)]
    upd = v3h.advance_recommendation(rec(), bars, cfg)
    assert upd["status"] == "TP1_HIT" and upd["result_r"] == 2.0
    assert upd["points_captured"] == 4.0


def test_stop_loss_books_points_lost():
    bars = [bar(0, 101, 101.2, 99.8, 100.1),
            bar(1, 100.1, 100.4, 97.8, 98.1)]
    upd = v3h.advance_recommendation(rec(), bars, CFG)
    assert upd["status"] == "STOP_LOSS" and upd["result_r"] == -1.0
    assert upd["points_lost"] == 2.0 and upd["points_captured"] == 0.0


def test_cancelled_when_invalidated_before_fill():
    bars = [bar(0, 99.5, 99.6, 97.5, 97.6)]       # closes through SL, no fill at 100
    upd = v3h.advance_recommendation(rec(), bars, CFG)
    assert upd["status"] == "CANCELLED"


def test_expired_when_never_filled():
    bars = [bar(i, 103, 103.4, 102.6, 103) for i in range(CFG.replay_entry_window_bars)]
    upd = v3h.advance_recommendation(rec(), bars, CFG)
    assert upd["status"] == "EXPIRED"


def test_still_active_returns_none():
    bars = [bar(0, 101, 101.2, 99.8, 100.1),      # filled, drifting — no exit
            bar(1, 100.1, 101.0, 99.9, 100.6)]
    assert v3h.advance_recommendation(rec(), bars, CFG) is None


# ---------------------------------------------------------------- DB layer

def _setup(idx=1, symbol="BTCUSDT"):
    return {"id": f"{symbol}:v3:test:{idx}", "symbol": symbol,
            "direction": "LONG", "setup_type": "Zone Reversal", "grade": "A",
            "entry": 100.0, "sl": 98.0, "tp1": 104.0, "tp2": 106.0, "rr": 2.0,
            "created_ts": datetime.fromtimestamp(T0, tz=timezone.utc).isoformat(),
            "session": {"label": "test window", "rating": 4},
            "grade_reason": "Grade A: 3 of 7", "confluences": 3,
            "confluences_total": 7, "risk_level": "MEDIUM",
            "market_context": "ctx", "reasons": ["r1"],
            "reasons_to_avoid": ["a1"], "invalidation": "inv",
            "early_exit": ["e1"], "management_notes": ["m1"],
            "why": {"why_now": "now"}, "zone": {"lo": 99, "hi": 100},
            "htf_bias": "BULLISH", "ltf_trend": "BULLISH",
            "holding_time": "INTRADAY"}


class _TxPool:
    def __init__(self, conn):
        self._conn = conn

    def acquire(self):
        conn = self._conn

        class _Ctx:
            async def __aenter__(self):
                return conn

            async def __aexit__(self, *a):
                return False
        return _Ctx()


async def test_record_dedupe_and_list_filters(db_conn):
    pool = _TxPool(db_conn)
    n1 = await v3h.record_setups(pool, [_setup(1), _setup(2)])
    n2 = await v3h.record_setups(pool, [_setup(1)])            # dupe
    assert n1 == 2 and n2 == 0
    out = await v3h.list_recommendations(pool, symbol="BTCUSDT")
    assert out["total"] == 2 and len(out["items"]) == 2
    it = out["items"][0]
    assert it["status"] == "ACTIVE" and it["analysis"]["reasons"] == ["r1"]
    # filters
    assert (await v3h.list_recommendations(pool, grade="A+"))["total"] == 0
    assert (await v3h.list_recommendations(pool, q="ctx"))["total"] == 2
    assert (await v3h.list_recommendations(pool, setup_type="Breakout"))["total"] == 0
    one = await v3h.get_recommendation(pool, it["id"])
    assert one and one["setup_id"] == it["setup_id"]
    csv_text = v3h.to_csv(out["items"])
    assert csv_text.splitlines()[0].startswith("id,ts,symbol")
    assert len(csv_text.splitlines()) == 3
