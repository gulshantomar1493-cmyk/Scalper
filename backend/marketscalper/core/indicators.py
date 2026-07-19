"""Display-only technical indicators (chart UX item 2/3).

Computed in the BACKEND (owner instruction): one source of truth, the same
values on every device, and formula changes touch one place. The frontend only
renders. These are VISUAL AIDS, isolated from the decision engine:

  * the ChartService (already engine-isolated, display-only, compute-on-read)
    calls the series functions over the candles it returns for /api/chart;
  * the incremental state classes feed the interim values that ride the live
    forming-bar stream, so the browser never extends an indicator itself;
  * NOTHING here touches structure / liquidity / signals / recommendations, and
    nothing here is hashed by the determinism harness.

Series functions take a list of closes and return a list aligned to the input
(None until warm). Standard conventions: EMA k = 2/(n+1) seeded with the SMA of
the first n closes; RSI is Wilder's smoothing.
"""

from __future__ import annotations


def ema(closes, period):
    n = len(closes)
    out = [None] * n
    if period <= 0 or n < period:
        return out
    k = 2.0 / (period + 1)
    prev = sum(closes[:period]) / period          # SMA seed at index period-1
    out[period - 1] = prev
    for i in range(period, n):
        prev = closes[i] * k + prev * (1.0 - k)
        out[i] = prev
    return out


def sma(closes, period):
    n = len(closes)
    out = [None] * n
    if period <= 0 or n < period:
        return out
    s = sum(closes[:period])
    out[period - 1] = s / period
    for i in range(period, n):
        s += closes[i] - closes[i - period]
        out[i] = s / period
    return out


def _rsi_value(avg_gain, avg_loss):
    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0     # flat -> neutral
    rs = avg_gain / avg_loss
    return 100.0 - 100.0 / (1.0 + rs)


def rsi(closes, period):
    n = len(closes)
    out = [None] * n
    if period <= 0 or n <= period:
        return out
    gains = losses = 0.0
    for i in range(1, period + 1):                 # first avg = mean of n changes
        ch = closes[i] - closes[i - 1]
        if ch >= 0:
            gains += ch
        else:
            losses -= ch
    avg_gain = gains / period
    avg_loss = losses / period
    out[period] = _rsi_value(avg_gain, avg_loss)
    for i in range(period + 1, n):                 # Wilder smoothing
        ch = closes[i] - closes[i - 1]
        gain = ch if ch > 0 else 0.0
        loss = -ch if ch < 0 else 0.0
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period
        out[i] = _rsi_value(avg_gain, avg_loss)
    return out


# --------------------------------------------------------------- incremental
# For the LIVE forming-bar stream: seed from history, advance on each closed
# candle, then evaluate the interim value against the forming close — so the
# browser never extends an indicator itself.


class EmaState:
    """Incremental EMA. Seed with the last `period` closes (or more — only the
    running value matters once warm)."""

    def __init__(self, period: int) -> None:
        self.period = period
        self.k = 2.0 / (period + 1)
        self.value = None

    def seed(self, closes) -> None:
        vals = ema(list(closes), self.period)
        self.value = vals[-1] if vals else None

    def update(self, close: float) -> None:
        """Advance on a CLOSED candle."""
        self.value = close if self.value is None else close * self.k + self.value * (1.0 - self.k)

    def peek(self, forming_close: float):
        """Interim value if the current forming close were the next close —
        does NOT mutate state."""
        if self.value is None:
            return None
        return forming_close * self.k + self.value * (1.0 - self.k)


class RsiState:
    """Incremental Wilder RSI."""

    def __init__(self, period: int) -> None:
        self.period = period
        self.avg_gain = None
        self.avg_loss = None
        self.last_close = None

    def seed(self, closes) -> None:
        closes = list(closes)
        if len(closes) <= self.period:
            self.last_close = closes[-1] if closes else None
            return
        gains = losses = 0.0
        for i in range(1, self.period + 1):
            ch = closes[i] - closes[i - 1]
            if ch >= 0:
                gains += ch
            else:
                losses -= ch
        ag, al = gains / self.period, losses / self.period
        for i in range(self.period + 1, len(closes)):
            ch = closes[i] - closes[i - 1]
            ag = (ag * (self.period - 1) + (ch if ch > 0 else 0.0)) / self.period
            al = (al * (self.period - 1) + (-ch if ch < 0 else 0.0)) / self.period
        self.avg_gain, self.avg_loss, self.last_close = ag, al, closes[-1]

    def _advanced(self, close: float):
        if self.last_close is None or self.avg_gain is None:
            return None
        ch = close - self.last_close
        ag = (self.avg_gain * (self.period - 1) + (ch if ch > 0 else 0.0)) / self.period
        al = (self.avg_loss * (self.period - 1) + (-ch if ch < 0 else 0.0)) / self.period
        return ag, al

    def update(self, close: float) -> None:
        adv = self._advanced(close)
        if adv is not None:
            self.avg_gain, self.avg_loss = adv
        self.last_close = close

    @property
    def value(self):
        """RSI at the last CLOSED candle (None until warm)."""
        if self.avg_gain is None:
            return None
        return _rsi_value(self.avg_gain, self.avg_loss)

    def peek(self, forming_close: float):
        adv = self._advanced(forming_close)
        if adv is None:
            return None
        return _rsi_value(adv[0], adv[1])
