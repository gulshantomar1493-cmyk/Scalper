"""V3 L5 — Session Timing (the owner's IST scalping-timing guide).

Pure lookup: candle ts → the active IST window (rating ⭐ + effect). Windows
live in config, not code. IST = UTC + 5:30. Sunday (IST) downgades everything.
Effects: BLOCK (no setups) · WARN_DOWNGRADE (grade −1 + warning) · NORMAL ·
BOOST (counts as a confluence) · STRONG_ONLY (B suppressed).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from marketscalper.v3.config import V3Config, DEFAULT

_IST = timezone(timedelta(hours=5, minutes=30))


def window_at(ts: int, cfg: V3Config = DEFAULT) -> dict:
    """The session window for an epoch-seconds timestamp."""
    ist = datetime.fromtimestamp(ts, tz=_IST)
    minute = ist.hour * 60 + ist.minute
    # windows are defined on 210..1650 IST minutes; the day wraps at 1650→210
    # via the 22:30-00:30 / 00:30-02:00 / 02:00-03:30 entries (minute+1440 form)
    m = minute if minute >= 210 else minute + 1440
    win = None
    for w in cfg.session_windows:
        s, e = w["ist"]
        if s <= m < e:
            win = w
            break
    if win is None:                                # defensive: shouldn't happen
        win = {"ist": (0, 0), "rating": 3, "effect": "NORMAL", "label": "unmapped"}
    out = {"rating": win["rating"], "effect": win["effect"],
           "label": win["label"], "ist_time": ist.strftime("%H:%M"),
           "min_grade": win.get("min_grade"),
           "sunday": ist.weekday() == 6}
    if out["sunday"] and out["effect"] not in ("BLOCK",):
        out = {**out, "effect": cfg.sunday_effect,
               "min_grade": cfg.sunday_min_grade,
               "label": out["label"] + " · SUNDAY (erratic structure)"}
    return out
