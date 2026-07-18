"""Tests for the forward-run ops helpers + deployment hardening (P4.13)."""

from __future__ import annotations

import pathlib
from datetime import datetime, timedelta, timezone

from marketscalper.ops import (
    FEED_GAP_ALERT_S,
    feed_gap_alerts,
    format_daily_summary,
)

UTC = timezone.utc
NOW = datetime(2026, 7, 22, 12, 0, tzinfo=UTC)
DEPLOY = pathlib.Path(__file__).resolve().parent.parent / "deployment"


# ------------------------------------------------------- feed-gap alerts


def test_no_alert_when_fresh():
    last = {"BTCUSDT": NOW - timedelta(seconds=30),
            "ETHUSDT": NOW - timedelta(seconds=10)}
    assert feed_gap_alerts(last, NOW) == []


def test_alert_when_stale():
    last = {"BTCUSDT": NOW - timedelta(seconds=FEED_GAP_ALERT_S + 60),
            "ETHUSDT": NOW - timedelta(seconds=10)}
    alerts = feed_gap_alerts(last, NOW)
    assert len(alerts) == 1
    assert alerts[0][0] == "BTCUSDT" and alerts[0][1] > FEED_GAP_ALERT_S


def test_boundary_is_strict():
    # exactly the threshold is NOT an alert (> threshold)
    last = {"BTCUSDT": NOW - timedelta(seconds=FEED_GAP_ALERT_S)}
    assert feed_gap_alerts(last, NOW) == []
    last = {"BTCUSDT": NOW - timedelta(seconds=FEED_GAP_ALERT_S + 1)}
    assert len(feed_gap_alerts(last, NOW)) == 1


def test_never_seen_symbol_not_alerted():
    # a symbol still warming up (None) is never a feed-gap alert
    assert feed_gap_alerts({"BTCUSDT": None}, NOW) == []


def test_alerts_are_sorted_deterministic():
    old = NOW - timedelta(seconds=FEED_GAP_ALERT_S + 100)
    last = {"ETHUSDT": old, "BTCUSDT": old}
    assert [a[0] for a in feed_gap_alerts(last, NOW)] == ["BTCUSDT", "ETHUSDT"]


# ------------------------------------------------ daily stats snapshot


def _analytics(n=3):
    grp = {"n": n,
           "hypothetical": {"win_rate": 0.66, "expectancy": 0.84},
           "manual": {"win_rate": 0.6, "expectancy": 0.55},
           "system_vs_actual": {"delta": -0.2}}
    return {"n_recommendations": n, "overall": grp,
            "by_strategy": {"S1": grp}, "by_session": {}}


def test_daily_summary_lines():
    out = format_daily_summary(_analytics(5))
    assert "daily stats snapshot: 5 recommendations" in out
    assert "S1:" in out
    assert "hyp_win=66%" in out and "hyp_exp=+0.84R" in out
    assert "man_win=60%" in out and "sys_vs_actual=-0.20R" in out


def test_daily_summary_empty():
    out = format_daily_summary({"n_recommendations": 0, "by_strategy": {}})
    assert "0 recommendations" in out
    assert "(no recommendations today)" in out


def test_daily_summary_handles_none_metrics():
    a = {"n_recommendations": 1, "by_strategy": {"S3": {
        "n": 1,
        "hypothetical": {"win_rate": None, "expectancy": None},
        "manual": {"win_rate": None, "expectancy": None},
        "system_vs_actual": {"delta": None}}}}
    out = format_daily_summary(a)
    assert "hyp_win=—" in out and "man_exp=—" in out


# ------------------------------------------------------ systemd hardening


def test_service_file_has_hardening_directives():
    unit = (DEPLOY / "marketscalper.service").read_text(encoding="utf-8")
    for directive in ("NoNewPrivileges=yes", "ProtectSystem=strict",
                      "ProtectHome=yes", "PrivateTmp=yes",
                      "ProtectKernelModules=yes", "RestrictNamespaces=yes",
                      "LockPersonality=yes"):
        assert directive in unit, directive
    # ProtectSystem=strict needs the log dir writable
    assert "ReadWritePaths=/var/log/marketscalper" in unit
    # network + unix socket (postgres) address families only
    assert "RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX" in unit
    # unchanged core (D4)
    assert "Restart=always" in unit and "RestartSec=5" in unit
