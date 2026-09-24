"""Streaming building blocks: ATR, pivots, market structure, HTF aggregation.

All classes are updated one closed bar at a time so that the exact same code
drives backtests (no look-ahead) and live trading. The definitions match the
Pine Script port bar-for-bar:

* pivot high at bar ``c = t - n``: ``high[c] > max(high[c-n:c])`` and
  ``high[c] >= max(high[c+1:t+1])`` (pivot low mirrored).
* a bullish break happens when ``close > last pivot high`` that has not been
  broken yet; it is a CHoCH if the prior trend was bearish, otherwise a BOS.
"""

from __future__ import annotations

from datetime import datetime

from .types import LONG, SHORT, Bar, Pivot, StructureEvent


class ATR:
    """Wilder ATR identical to Pine's ``ta.atr`` (RMA seeded with an SMA)."""

    def __init__(self, length: int) -> None:
        self.n = length
        self.value: float | None = None
        self._prev_close: float | None = None
        self._seed_sum = 0.0
        self._seed_count = 0

    def update(self, high: float, low: float, close: float) -> float | None:
        if self._prev_close is None:
            tr = high - low
        else:
            tr = max(high - low, abs(high - self._prev_close), abs(low - self._prev_close))
        self._prev_close = close
        if self.value is None:
            self._seed_sum += tr
            self._seed_count += 1
            if self._seed_count == self.n:
                self.value = self._seed_sum / self.n
        else:
            self.value = (self.value * (self.n - 1) + tr) / self.n
        return self.value


def pivot_high(highs: list[float], t: int, n: int) -> float | None:
    if t < 2 * n:
        return None
    c = t - n
    center = highs[c]
    if center > max(highs[c - n : c]) and center >= max(highs[c + 1 : t + 1]):
        return center
    return None


def pivot_low(lows: list[float], t: int, n: int) -> float | None:
    if t < 2 * n:
        return None
    c = t - n
    center = lows[c]
    if center < min(lows[c - n : c]) and center <= min(lows[c + 1 : t + 1]):
        return center
    return None


class StructureTracker:
    """Tracks pivots, trend and BOS/CHoCH for one pivot strength."""

    def __init__(self, n: int, level: str) -> None:
        self.n = n
        self.level = level
        self.high: Pivot | None = None
        self.low: Pivot | None = None
        self.trend = 0
        # trailing extremes of the current dealing range (premium/discount)
        self.trail_top: float | None = None
        self.trail_bottom: float | None = None
        self.trail_top_bar = -1
        self.trail_bottom_bar = -1
        self.new_high: Pivot | None = None
        self.new_low: Pivot | None = None
        self.prev_high: Pivot | None = None
        self.prev_low: Pivot | None = None

    def update(
        self,
        t: int,
        highs: list[float],
        lows: list[float],
        close: float,
        times: list[datetime],
    ) -> list[StructureEvent]:
        self.new_high = self.new_low = None
        ph = pivot_high(highs, t, self.n)
        if ph is not None:
            c = t - self.n
            label = "H" if self.high is None else ("HH" if ph > self.high.price else "LH")
            self.prev_high = self.high
            self.high = Pivot(ph, c, times[c], label)
            self.new_high = self.high
            self.trail_top, self.trail_top_bar = ph, c
        pl = pivot_low(lows, t, self.n)
        if pl is not None:
            c = t - self.n
            label = "L" if self.low is None else ("HL" if pl > self.low.price else "LL")
            self.prev_low = self.low
            self.low = Pivot(pl, c, times[c], label)
            self.new_low = self.low
            self.trail_bottom, self.trail_bottom_bar = pl, c

        if self.trail_top is not None and highs[t] > self.trail_top:
            self.trail_top, self.trail_top_bar = highs[t], t
        if self.trail_bottom is not None and lows[t] < self.trail_bottom:
            self.trail_bottom, self.trail_bottom_bar = lows[t], t

        events: list[StructureEvent] = []
        if self.high is not None and not self.high.crossed and close > self.high.price:
            kind = "CHoCH" if self.trend == SHORT else "BOS"
            self.high.crossed = True
            self.trend = LONG
            events.append(StructureEvent(self.level, LONG, kind, self.high.price, self.high.bar, t))
        if self.low is not None and not self.low.crossed and close < self.low.price:
            kind = "CHoCH" if self.trend == LONG else "BOS"
            self.low.crossed = True
            self.trend = SHORT
            events.append(StructureEvent(self.level, SHORT, kind, self.low.price, self.low.bar, t))
        return events

    @property
    def equilibrium(self) -> float | None:
        if self.trail_top is None or self.trail_bottom is None:
            return None
        return (self.trail_top + self.trail_bottom) / 2.0


class HTFBias:
    """Aggregates base bars into a higher timeframe and tracks its structure.

    The reported trend only uses *completed* HTF candles, which is what the
    Pine port gets from ``request.security(..., expr[1], lookahead_on)``.
    """

    WEEK = 7 * 86400
    MONDAY_OFFSET = 3 * 86400  # 1970-01-01 was a Thursday

    def __init__(self, minutes: int, n: int) -> None:
        self.seconds = minutes * 60
        self.tracker = StructureTracker(n, "htf")
        self.highs: list[float] = []
        self.lows: list[float] = []
        self.times: list[datetime] = []
        self._key: int | None = None
        self._bar: Bar | None = None

    def _bucket(self, ts: float) -> int:
        if self.seconds == self.WEEK:
            return int((ts + self.MONDAY_OFFSET) // self.WEEK)
        return int(ts // self.seconds)

    def update(self, bar: Bar) -> int:
        key = self._bucket(bar.time.timestamp())
        if self._key is None:
            self._key = key
            self._bar = Bar(bar.time, bar.open, bar.high, bar.low, bar.close, bar.volume)
        elif key != self._key:
            self._close_bar()
            self._key = key
            self._bar = Bar(bar.time, bar.open, bar.high, bar.low, bar.close, bar.volume)
        else:
            b = self._bar
            assert b is not None
            b.high = max(b.high, bar.high)
            b.low = min(b.low, bar.low)
            b.close = bar.close
            b.volume += bar.volume
        return self.tracker.trend

    def _close_bar(self) -> None:
        b = self._bar
        assert b is not None
        self.highs.append(b.high)
        self.lows.append(b.low)
        self.times.append(b.time)
        self.tracker.update(len(self.highs) - 1, self.highs, self.lows, b.close, self.times)

    @property
    def trend(self) -> int:
        return self.tracker.trend
