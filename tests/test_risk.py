"""Tests for the Risk / Trade Management Engine (§7; Decision D17)."""

from __future__ import annotations

from marketscalper.engines.risk import (
    DEFAULT_TAKER_FEE,
    management_guidance,
    plan_trade,
)


def _long(**kw):
    base = dict(direction="LONG", entry=100.0, sl=99.0, tp1=102.0,
                equity=10000.0)
    base.update(kw)
    return plan_trade(**base)


def test_long_plan_suggested_with_the_verbatim_math():
    plan = _long()
    assert plan.status == "suggested" and plan.reasons == ()
    assert plan.risk_amt == 50.0                   # 10000 x 0.5%
    assert plan.r_per_unit == 1.0
    assert plan.qty == 50.0                        # display only (§7)
    assert abs(plan.fee_per_unit - 0.1) < 1e-12    # 100 x 0.0005 x 2
    assert abs(plan.net_rr_tp1 - 1.9 / 1.1) < 1e-12
    assert plan.net_rr_tp2 is None
    # §7 total fees identity: qty x entry x taker x 2
    assert abs(plan.qty * plan.entry * DEFAULT_TAKER_FEE * 2 - 5.0) < 1e-9


def test_short_mirror_matches_long():
    long = _long()
    short = plan_trade(direction="SHORT", entry=100.0, sl=101.0, tp1=98.0,
                       equity=10000.0)
    assert short.status == "suggested"
    assert short.net_rr_tp1 == long.net_rr_tp1     # pinned mirror (D17.2)
    assert short.qty == long.qty and short.r_per_unit == long.r_per_unit


def test_reject_below_one_strict_boundary():
    # fee 0 -> exact arithmetic: RR exactly 1.0 passes (strict reject)
    plan = _long(tp1=101.0, taker_fee=0.0)
    assert plan.net_rr_tp1 == 1.0 and plan.status == "suggested"
    plan = _long(tp1=100.99, taker_fee=0.0)
    assert plan.status == "rejected"
    assert plan.reasons == ("net RR to TP1 below 1.0 after fees",)
    assert plan.rr_floor_ok is False
    assert plan.net_rr_tp1 is not None             # RR reject keeps numbers
    plan = _long(tp1=102.0)                        # fees eat RR: still >1
    assert plan.status == "suggested"


def test_rr_floor_flag_inclusive_boundaries():
    ok = _long(tp1=101.0, tp2=101.5, taker_fee=0.0)
    assert ok.net_rr_tp2 == 1.5 and ok.rr_floor_ok is True   # inclusive
    low = _long(tp1=101.0, tp2=101.4, taker_fee=0.0)
    assert low.status == "suggested"               # floors never reject tp2
    assert low.rr_floor_ok is False
    solo = _long(tp1=101.0, taker_fee=0.0)
    assert solo.rr_floor_ok is True                # tp2 absent


def test_qty_division_with_wider_stop():
    plan = _long(sl=98.0, tp1=104.0)               # r = 2: qty = 50/2
    assert plan.r_per_unit == 2.0 and plan.qty == 25.0
    assert plan.risk_amt == 50.0                   # kills a field swap


def test_strict_equality_boundaries_all_checks():
    short = dict(direction="SHORT", entry=100.0, sl=101.0, tp1=98.0,
                 equity=10000.0)
    plan = plan_trade(**{**short, "sl": 100.0})    # SHORT sl == entry
    assert plan.reasons == ("stop loss not on the loss side",)
    plan = _long(tp1=100.0)                        # LONG tp1 == entry
    assert plan.reasons == ("tp1 not on the profit side",)
    plan = plan_trade(**{**short, "tp1": 100.0})   # SHORT tp1 == entry
    assert plan.reasons == ("tp1 not on the profit side",)
    plan = _long(tp1=102.0, tp2=102.0)             # tp2 == tp1
    assert plan.reasons == ("tp2 not beyond tp1",)
    plan = _long(sl=0.0)                           # zero price is not > 0
    assert plan.reasons == ("prices must be positive",)


def test_reason_order_pinned_across_guards():
    plan = _long(taker_fee=-1.0, sl=-1.0)          # check 3 before check 4
    assert plan.reasons == ("taker fee must be non-negative",
                            "prices must be positive")
    plan = _long(sl=110.0, tp1=99.5, tp2=98.0)     # checks 5 -> 6 -> 7
    assert plan.reasons == ("stop loss not on the loss side",
                            "tp1 not on the profit side",
                            "tp2 not beyond tp1")


def test_geometry_validations_and_reason_accumulation():
    plan = plan_trade(direction="BUY", entry=100.0, sl=99.0, tp1=102.0,
                      equity=0.0)
    assert plan.status == "rejected"
    assert plan.reasons == ("unknown direction", "equity must be positive")
    assert plan.qty is None and plan.net_rr_tp1 is None      # D7: no math
    assert plan.rr_floor_ok is None
    plan = _long(sl=100.0)                         # equality: no distance
    assert plan.reasons == ("stop loss not on the loss side",)
    plan = _long(tp1=99.5)
    assert plan.reasons == ("tp1 not on the profit side",)
    plan = _long(tp1=102.0, tp2=101.0)
    assert plan.reasons == ("tp2 not beyond tp1",)
    plan = _long(sl=-1.0)
    assert plan.reasons == ("prices must be positive",)
    plan = _long(taker_fee=-0.001)
    assert plan.reasons == ("taker fee must be non-negative",)
    short = plan_trade(direction="SHORT", entry=100.0, sl=99.0, tp1=98.0,
                       equity=10000.0)             # SL below a short entry
    assert short.reasons == ("stop loss not on the loss side",)


def test_management_guidance_display_only_text():
    lines = management_guidance(_long())
    assert len(lines) == 4
    assert "+1R (101)" in lines[0] and "break-even (100)" in lines[0]
    assert "TP1 (102)" in lines[1] and "50%" in lines[1]
    assert "15 minutes" in lines[2]
    assert "1.5×ATR" in lines[3]
    short = plan_trade(direction="SHORT", entry=100.0, sl=101.0, tp1=98.0,
                       equity=10000.0)
    assert "+1R (99)" in management_guidance(short)[0]
    assert management_guidance(_long(tp1=100.5, taker_fee=0.0)) == ()


def test_guidance_precision_at_btc_scale():
    """Freeze-audit fix: 6-sig-digit %g collapsed BE and +1R into the
    same displayed number at BTCUSDT prices — levels must stay exact."""
    plan = plan_trade(direction="LONG", entry=118456.7, sl=118456.2,
                      tp1=118460.0, equity=10000.0, taker_fee=0.0)
    line = management_guidance(plan)[0]
    assert "+1R (118457.2)" in line               # not "118457" twice
    assert "break-even (118456.7)" in line
    assert "e+" not in " ".join(management_guidance(plan))


def test_determinism_pure_functions():
    assert _long(tp2=104.0) == _long(tp2=104.0)
    assert management_guidance(_long()) == management_guidance(_long())
