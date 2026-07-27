"""V4 strategy-layer tests — invariants that must hold for the evidence to transfer.

The full 9-year parity check (V4 reproduces the research numbers exactly for all
six strategies) lives in scripts/v4_parity.py and needs the research data cache.
These tests are the fast, self-contained guarantees.
"""
from datetime import datetime, timezone

import numpy as np
import pytest

from marketscalper.v4 import config as C
from marketscalper.v4 import levels as L
from marketscalper.v4 import filters as F
from marketscalper.v4.signals import build_setups


def mk(n, tf_s, start=0, base=100.0, drift=0.0, seed=1):
    rng = np.random.default_rng(seed)
    c = base + np.cumsum(rng.normal(drift, 1.0, n))
    c = np.maximum(c, 1.0)
    return dict(ts=np.arange(n, dtype=np.int64) * tf_s + start,
                o=c.copy(), h=c + 1.0, l=c - 1.0, c=c,
                v=np.ones(n), tf_s=tf_s)


def test_catalogue_is_coherent():
    assert len(C.STRATEGIES) == len(C.BY_ID)
    for s in C.STRATEGIES:
        assert s.symbol in C.SYMBOLS
        assert 1 <= s.min_filters <= 3
        assert s.level_source in ("donchian", "swing", "pdh_pdl", "round")
        # every catalogue entry must carry its evidence
        assert s.backtest_t_stat > 2.0, f"{s.id} shipped without significant evidence"
        assert s.backtest_net_r > 0


def test_atr_is_causal():
    b = mk(300, 3600)
    a1 = L.atr(b, 14)
    b2 = {k: (v.copy() if isinstance(v, np.ndarray) else v) for k, v in b.items()}
    b2["h"][-20:] *= 10           # mutate only the future
    a2 = L.atr(b2, 14)
    assert np.allclose(a1[:-20], a2[:-20], equal_nan=True)


def test_donchian_uses_only_past():
    b = mk(200, 3600)
    hi, lo = L.donchian(b, 20)
    for i in (50, 120, 199):
        assert hi[i] == b["h"][i - 19:i + 1].max()
        assert lo[i] == b["l"][i - 19:i + 1].min()


def test_prior_day_rolls_at_utc_midnight():
    b = mk(72, 3600)              # 3 days of hourly bars
    pdh, pdl = L.prior_day(b)
    assert np.isnan(pdh[0])       # nothing known on day 0
    assert not np.isnan(pdh[30])  # day 1 knows day 0
    d0h = b["h"][:24].max()
    assert pdh[24] == pytest.approx(d0h)


def test_round_levels_bracket_price():
    assert L.round_levels(67432.0, "BTCUSDT") == [68000.0, 67000.0]
    assert L.round_levels(3140.0, "ETHUSDT") == [3200.0, 3100.0]


def test_structure_trend_is_causal_and_bounded():
    b = mk(400, 3600)
    st = F.structure_trend(b)
    assert set(np.unique(st)).issubset({-1, 0, 1})
    b2 = {k: (v.copy() if isinstance(v, np.ndarray) else v) for k, v in b.items()}
    b2["c"][-30:] *= 3
    st2 = F.structure_trend(b2)
    assert np.array_equal(st[:-32], st2[:-32])


def test_align_causal_never_returns_an_unclosed_bar():
    src = mk(50, 3600)
    tgt = src["ts"] + src["tf_s"]          # each bar's own close
    j = F.align_causal(src, tgt)
    # bar i closes exactly at tgt[i], so it IS known -> index i
    assert j[0] == 0 and j[-1] == len(src["ts"]) - 1
    # one second before the close, that bar must NOT be visible
    j2 = F.align_causal(src, tgt - 1)
    assert np.all(j2 == np.arange(len(tgt)) - 1)


def _strategy(**kw):
    base = dict(id="t", label="t", symbol="ETHUSDT", level_source="donchian",
                level_tf="4h", eval_tf="4h", lookback=20, min_filters=1,
                backtest_trades_per_year=1.0, backtest_net_r=0.1,
                backtest_t_stat=3.0, backtest_profit_factor=1.2)
    base.update(kw)
    return C.Strategy(**base)


def test_setup_geometry_matches_the_validated_rules():
    lb = mk(600, 14400, drift=0.05)
    dly = mk(120, 86400, drift=0.05)
    m5 = mk(9000, 300, drift=0.002)
    out = build_setups(_strategy(), lb, dly, m5)
    assert out, "expected some setups on a trending series"
    for s in out[:40]:
        risk = abs(s.entry - s.stop)
        # Target is TARGET_R risk units away. Prices are rounded to 2dp for
        # display/execution, so allow the error that rounding can introduce
        # (up to ~0.005 on entry and on target, amplified by 1/risk).
        tol = 0.02 + (0.005 * (1 + C.TARGET_R)) / risk
        assert abs(abs(s.target - s.entry) / risk - C.TARGET_R) < tol
        assert (s.target > s.entry) == (s.direction > 0)
        assert (s.stop < s.entry) == (s.direction > 0)
        assert s.filters_passed >= 1
        assert s.rr > 0


def test_no_lookahead_decision_ts_is_a_bar_close():
    lb = mk(400, 14400, drift=0.05)
    dly = mk(80, 86400, drift=0.05)
    m5 = mk(6000, 300)
    out = build_setups(_strategy(), lb, dly, m5)
    closes = set((lb["ts"] + lb["tf_s"]).tolist())
    for s in out:
        assert s.decision_ts in closes


def test_min_filters_is_enforced_and_reduces_count():
    lb = mk(600, 14400, drift=0.05)
    dly = mk(120, 86400, drift=0.05)
    m5 = mk(9000, 300)
    loose = build_setups(_strategy(min_filters=1), lb, dly, m5)
    strict = build_setups(_strategy(min_filters=3), lb, dly, m5)
    assert len(strict) <= len(loose)
    assert all(s.filters_passed == 3 for s in strict)


def test_deterministic():
    lb = mk(400, 14400, drift=0.05); dly = mk(80, 86400); m5 = mk(6000, 300)
    a = build_setups(_strategy(), lb, dly, m5)
    b = build_setups(_strategy(), lb, dly, m5)
    assert [x.to_dict() for x in a] == [x.to_dict() for x in b]


def test_rejected_ideas_are_documented_not_implemented():
    """Guard: the losing concepts must never reappear as code."""
    import pathlib
    src = pathlib.Path(__file__).resolve().parents[1] / "backend" / "marketscalper" / "v4"
    blob = " ".join(p.read_text(encoding="utf-8") for p in src.glob("*.py") if p.name != "config.py")
    for banned in ("premium_discount", "liquidity_sweep", "bounce_entry"):
        assert banned not in blob, f"{banned} was rejected by research but appears in v4 code"


# ------------------------------------------------- runtime strategy switch ----

class _FakeSettings:
    def __init__(self):
        self.off = set()

    def v4_strategy_enabled(self, sid):
        return sid not in self.off

    def set_v4_strategy(self, sid, enabled):
        (self.off.discard if enabled else self.off.add)(sid)


def _service(settings=None):
    from marketscalper.v4.service import V4Service
    return V4Service(chart_service=None, settings=settings)


def test_service_without_settings_follows_the_catalogue():
    svc = _service()
    for st in C.STRATEGIES:
        assert svc.is_enabled(st) is st.enabled
    assert all(row["can_toggle"] is False for row in svc.catalogue())


def test_runtime_switch_disables_and_reenables_a_strategy():
    settings = _FakeSettings()
    svc = _service(settings)
    sid = C.STRATEGIES[0].id
    assert svc.set_enabled(sid, False) is False
    assert svc.is_enabled(C.BY_ID[sid]) is False
    cat = {r["id"]: r for r in svc.catalogue()}
    assert cat[sid]["enabled"] is False and cat[sid]["can_toggle"] is True
    assert svc.set_enabled(sid, True) is True
    assert svc.is_enabled(C.BY_ID[sid]) is True


def test_runtime_switch_cannot_enable_a_catalogue_disabled_strategy():
    """The catalogue is the hard authority — the UI can only take away."""
    from dataclasses import replace
    st = replace(C.STRATEGIES[0], enabled=False)
    svc = _service(_FakeSettings())
    assert svc.is_enabled(st) is False


def test_switch_drops_the_cached_setup_list():
    """A disabled strategy must not keep serving its last cached setups."""
    settings = _FakeSettings()
    svc = _service(settings)
    sid = C.STRATEGIES[0].id
    svc._setups[sid] = (float("inf"), [{"strategy_id": sid}])
    svc.set_enabled(sid, False)
    assert sid not in svc._setups


def test_switch_without_a_settings_store_is_refused():
    import pytest
    with pytest.raises(RuntimeError):
        _service().set_enabled(C.STRATEGIES[0].id, False)


# ---------------------------------------------------------- switch endpoint ---
# The route handlers are called directly: the repo's HTTP round-trip harness
# needs a database, and this layer is pure validation over the service.

def _routes(settings=None):
    import asyncio
    from fastapi import HTTPException
    from marketscalper.v4.api import build_router
    svc = _service(settings)
    router = build_router(svc, lambda: None)
    by_path = {(r.path, tuple(sorted(r.methods))[0]): r.endpoint
               for r in router.routes}

    def call(path, method="GET", **kw):
        return asyncio.run(by_path[("/api/v4" + path, method)](**kw))

    def status(path, method="POST", **kw):
        try:
            call(path, method, **kw)
        except HTTPException as exc:
            return exc.status_code
        return 200

    return svc, call, status


def test_endpoint_toggles_a_strategy():
    svc, call, _ = _routes(_FakeSettings())
    sid = C.STRATEGIES[0].id
    out = call("/strategies/{strategy_id}/enabled", "POST",
               strategy_id=sid, payload={"enabled": False})
    assert out == {"id": sid, "enabled": False}
    assert svc.is_enabled(C.BY_ID[sid]) is False
    cat = {x["id"]: x for x in call("/strategies")["strategies"]}
    assert cat[sid]["enabled"] is False


def test_endpoint_rejects_unknown_strategy_and_bad_body():
    _, _, status = _routes(_FakeSettings())
    sid = C.STRATEGIES[0].id
    p = "/strategies/{strategy_id}/enabled"
    assert status(p, strategy_id="nope", payload={"enabled": True}) == 400
    assert status(p, strategy_id=sid, payload={}) == 400
    assert status(p, strategy_id=sid, payload=None) == 400
    assert status(p, strategy_id=sid, payload={"enabled": "yes"}) == 400


def test_endpoint_503_without_a_settings_store():
    _, _, status = _routes(None)
    assert status("/strategies/{strategy_id}/enabled",
                  strategy_id=C.STRATEGIES[0].id, payload={"enabled": False}) == 503


# ------------------------------------------------- 5m folded from canonical 1m

def _m1(n, start=0, step=60, skip=()):
    """n one-minute bars from `start`, omitting any index in `skip`."""
    ts, o, h, l, c, v = [], [], [], [], [], []
    for i in range(n):
        if i in skip:
            continue
        ts.append(start + i * step)
        o.append(100.0 + i); h.append(101.0 + i); l.append(99.0 + i)
        c.append(100.5 + i); v.append(2.0)
    return dict(ts=np.array(ts, np.int64), o=np.array(o, float),
                h=np.array(h, float), l=np.array(l, float),
                c=np.array(c, float), v=np.array(v, float), tf_s=60)


def test_fold_5m_matches_a_hand_computed_window():
    from marketscalper.v4.service import fold_5m
    out = fold_5m(_m1(10))
    assert list(out["ts"]) == [0, 300]
    assert out["tf_s"] == 300
    # window 0: minutes 0..4 -> open of the first, close of the last
    assert out["o"][0] == 100.0 and out["c"][0] == 104.5
    assert out["h"][0] == 105.0 and out["l"][0] == 99.0
    assert out["v"][0] == 10.0
    # window 1: minutes 5..9
    assert out["o"][1] == 105.0 and out["c"][1] == 109.5


def test_fold_5m_discards_incomplete_windows():
    """The live builder never publishes a partial 5m window (D7 / fix F1) —
    a folded series that did would silently disagree with stored bars."""
    from marketscalper.v4.service import fold_5m
    holed = fold_5m(_m1(10, skip=(2,)))          # minute 2 missing
    assert list(holed["ts"]) == [300]            # first window dropped, second kept
    tail = fold_5m(_m1(8))                       # last window only 3 minutes long
    assert list(tail["ts"]) == [0]


def test_fold_5m_ignores_bars_before_the_first_window_head():
    from marketscalper.v4.service import fold_5m
    mid = _m1(10)
    mid = {k: (val[2:] if k != "tf_s" else val) for k, val in mid.items()}
    out = fold_5m(mid)                           # starts at minute 2, not a head
    assert list(out["ts"]) == [300]


def test_fold_5m_of_nothing_is_empty_not_an_error():
    from marketscalper.v4.service import fold_5m
    out = fold_5m(_m1(0))
    assert len(out["c"]) == 0 and out["tf_s"] == 300


class _ChartStub:
    """ChartService serves 1m/5m from STORED rows only (D28.1). Over history
    no 5m rows exist — this reproduces that exactly."""

    def __init__(self, minutes):
        self.minutes = minutes
        self.asked = []

    async def get_chart(self, symbol, tf, start, end, **kw):
        self.asked.append(tf)
        if tf != "1m":
            return {"candles": []}
        base = int(start.timestamp()) // 300 * 300
        return {"candles": [
            {"ts": datetime.fromtimestamp(base + i * 60, tz=timezone.utc),
             "o": 100.0 + i, "h": 101.0 + i, "l": 99.0 + i, "c": 100.5 + i, "v": 2.0}
            for i in range(self.minutes)]}


async def test_service_folds_5m_when_no_5m_rows_are_stored():
    """The defect this guards: with a full 1m history and no stored 5m rows,
    every strategy failed its warm-up check and the tool reported 'no setup'
    forever while looking perfectly healthy."""
    from marketscalper.v4.service import V4Service
    chart = _ChartStub(minutes=3000)
    svc = V4Service(chart)
    bars = await svc.bars("BTCUSDT", "5m")
    assert len(bars["c"]) >= 400 and bars["tf_s"] == 300
    assert "1m" in chart.asked                          # fell back to canonical 1m


async def test_service_prefers_stored_5m_when_it_is_complete():
    from marketscalper.v4.service import V4Service

    class _Stored(_ChartStub):
        async def get_chart(self, symbol, tf, start, end, **kw):
            self.asked.append(tf)
            if tf != "5m":
                return {"candles": []}
            base = int(start.timestamp()) // 300 * 300
            return {"candles": [
                {"ts": datetime.fromtimestamp(base + i * 300, tz=timezone.utc),
                 "o": 1.0, "h": 2.0, "l": 0.5, "c": 1.5, "v": 1.0}
                for i in range(500)]}

    chart = _Stored(0)
    bars = await V4Service(chart).bars("BTCUSDT", "5m")
    assert len(bars["c"]) == 500
    assert chart.asked == ["5m"]                        # no 1m fallback needed
