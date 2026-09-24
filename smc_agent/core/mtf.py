"""Top-down multi-timeframe context (H1 / H4 / D1 / W1 ...).

For every higher timeframe the agent keeps the same compact picture an ICT
trader reads before taking an intraday entry:

* swing trend (pivot structure on completed HTF candles)
* trailing dealing range -> is price in premium or discount of that TF?
* the latest unbroken swing high / low (resting liquidity, reaction points)
* the latest *unfilled* bullish and bearish fair value gap
* the previous candle's high / low (previous day / week on D1 / W1)

Only completed HTF candles are used, exactly like the Pine port's
``request.security(..., f_mtf(n), lookahead_on)`` which returns values from
the previous HTF bar, so nothing here can see the future.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from .structure import StructureTracker
from .timeframes import Bucketer, timeframe_label
from .types import Bar


@dataclass
class TFState:
    minutes: int
    label: str
    bars: int = 0
    trend: int = 0
    top: float | None = None
    bottom: float | None = None
    swing_high: float | None = None
    swing_low: float | None = None
    bull_fvg: tuple[float, float] | None = None  # (top, bottom)
    bear_fvg: tuple[float, float] | None = None
    prev_high: float | None = None
    prev_low: float | None = None

    def position(self, price: float) -> float | None:
        """0 = bottom of the dealing range, 1 = top."""
        if self.top is None or self.bottom is None or self.top <= self.bottom:
            return None
        return (price - self.bottom) / (self.top - self.bottom)

    def to_dict(self, price: float | None = None) -> dict[str, Any]:
        d: dict[str, Any] = {
            "tf": self.label,
            "trend": {1: "bullish", -1: "bearish", 0: "undetermined"}[self.trend],
            "range_top": self.top,
            "range_bottom": self.bottom,
            "unbroken_swing_high": self.swing_high,
            "unbroken_swing_low": self.swing_low,
            "unfilled_bullish_fvg": list(self.bull_fvg) if self.bull_fvg else None,
            "unfilled_bearish_fvg": list(self.bear_fvg) if self.bear_fvg else None,
            "prev_candle_high": self.prev_high,
            "prev_candle_low": self.prev_low,
        }
        if price is not None:
            pos = self.position(price)
            d["price_position_pct"] = None if pos is None else round(pos * 100, 1)
        return d


class TFTracker:
    def __init__(self, minutes: int, n: int, tz: str = "UTC", roll_hour: int = 0) -> None:
        self.bucketer = Bucketer(minutes, tz, roll_hour)
        self.state = TFState(minutes, timeframe_label(minutes))
        self.structure = StructureTracker(n, "mtf")
        self.highs: list[float] = []
        self.lows: list[float] = []
        self.closes: list[float] = []
        self.times: list[datetime] = []
        self._key: int | None = None
        self._cur: Bar | None = None

    def update(self, bar: Bar) -> bool:
        """Feed one chart bar; returns True when an HTF candle completed."""
        key = self.bucketer.key(bar.time)
        if self._key is None:
            self._key, self._cur = key, Bar(bar.time, bar.open, bar.high, bar.low, bar.close)
            return False
        if key != self._key:
            self._complete()
            self._key, self._cur = key, Bar(bar.time, bar.open, bar.high, bar.low, bar.close)
            return True
        c = self._cur
        assert c is not None
        c.high = max(c.high, bar.high)
        c.low = min(c.low, bar.low)
        c.close = bar.close
        return False

    def _complete(self) -> None:
        b = self._cur
        assert b is not None
        s = self.state
        self.highs.append(b.high)
        self.lows.append(b.low)
        self.closes.append(b.close)
        self.times.append(b.time)
        i = len(self.highs) - 1
        # 1) existing gaps filled by this candle
        if s.bull_fvg is not None and b.low <= s.bull_fvg[1]:
            s.bull_fvg = None
        if s.bear_fvg is not None and b.high >= s.bear_fvg[0]:
            s.bear_fvg = None
        # 2) structure
        st = self.structure
        st.update(i, self.highs, self.lows, b.close, self.times)
        s.trend = st.trend
        s.top, s.bottom = st.trail_top, st.trail_bottom
        s.swing_high = st.high.price if st.high is not None and not st.high.crossed else None
        s.swing_low = st.low.price if st.low is not None and not st.low.crossed else None
        # 3) new gaps
        if i >= 2:
            h, l, c = self.highs, self.lows, self.closes
            if l[i] > h[i - 2] and c[i - 1] > h[i - 2]:
                s.bull_fvg = (l[i], h[i - 2])
            if h[i] < l[i - 2] and c[i - 1] < l[i - 2]:
                s.bear_fvg = (l[i - 2], h[i])
        s.prev_high, s.prev_low = b.high, b.low
        s.bars = i + 1


class MTFContext:
    """Tracks every configured timeframe above the chart timeframe."""

    def __init__(self, chart_minutes: int, timeframes: list[int], n: int = 3, tz: str = "UTC",
                 roll_hour: int = 0) -> None:
        self.chart_minutes = chart_minutes
        self.trackers = [TFTracker(m, n, tz, roll_hour) for m in sorted(set(timeframes)) if m > chart_minutes]

    def update(self, bar: Bar) -> None:
        for tr in self.trackers:
            tr.update(bar)

    def states(self) -> list[TFState]:
        return [tr.state for tr in self.trackers]

    def state(self, minutes: int) -> TFState | None:
        for tr in self.trackers:
            if tr.state.minutes == minutes:
                return tr.state
        return None

    def to_list(self, price: float | None = None) -> list[dict[str, Any]]:
        return [s.to_dict(price) for s in self.states() if s.bars > 0]
