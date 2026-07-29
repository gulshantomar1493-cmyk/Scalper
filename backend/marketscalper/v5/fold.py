"""Fold canonical 1m candles into the higher timeframes the engine reads.

One folding implementation, used by BOTH the live path and the backtest. If they
folded differently, a backtest number would be about a market that never
existed.

Only COMPLETE buckets are emitted, matching the live candle builder's own rule
(D7/F1: a partial window is discarded, never published). A bucket with a gap in
it is dropped rather than filled — a candle assembled from four minutes out of
five is a lie about the high and the low, and structure is read from exactly
those.
"""
from __future__ import annotations

import numpy as np

TF_SECONDS = {"1m": 60, "5m": 300, "15m": 900, "30m": 1800,
              "1h": 3600, "4h": 14400, "1d": 86400}


def fold(m1: dict, tf: str) -> dict:
    """1m bars -> `tf` bars. Complete buckets only, in order."""
    step = TF_SECONDS[tf]
    if step == 60:
        return m1
    per = step // 60
    ts, o, h, l, c, v = m1["ts"], m1["o"], m1["h"], m1["l"], m1["c"], m1["v"]
    n = len(ts)
    out_ts, out_o, out_h, out_l, out_c, out_v = [], [], [], [], [], []
    i = 0
    while i < n:
        head = int(ts[i])
        if head % step != 0:              # not a bucket head — walk forward
            i += 1
            continue
        end = i + per
        if end > n or int(ts[end - 1]) != head + (per - 1) * 60:
            i += 1                        # gap inside the bucket: drop it
            continue
        out_ts.append(head)
        out_o.append(float(o[i]))
        out_c.append(float(c[end - 1]))
        out_h.append(float(np.max(h[i:end])))
        out_l.append(float(np.min(l[i:end])))
        out_v.append(float(np.sum(v[i:end])))
        i = end
    return dict(ts=np.asarray(out_ts, np.int64), o=np.asarray(out_o, float),
                h=np.asarray(out_h, float), l=np.asarray(out_l, float),
                c=np.asarray(out_c, float), v=np.asarray(out_v, float),
                tf_s=step)


def slice_upto(bars: dict, ts_limit: int) -> dict:
    """Every bar that had already CLOSED at `ts_limit`.

    The anti-lookahead cut. A backtest that hands the engine a 4H candle which
    has not finished forming is measuring a strategy nobody could have traded.
    """
    close_ts = bars["ts"] + bars["tf_s"]
    k = int(np.searchsorted(close_ts, ts_limit, side="right"))
    if k <= 0:
        return {kk: (bars[kk][:0] if kk != "tf_s" else bars["tf_s"]) for kk in bars}
    return {kk: (bars[kk][:k] if kk != "tf_s" else bars["tf_s"]) for kk in bars}
