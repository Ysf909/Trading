"""Timeframe helpers shared by the engine, feeds and CLI."""

from __future__ import annotations

from datetime import datetime, timedelta
from functools import lru_cache
from zoneinfo import ZoneInfo

_UNITS = {"m": 1, "h": 60, "d": 1440, "w": 10080}


def timeframe_minutes(tf: str) -> int:
    """'15m' -> 15, '1h' -> 60, '4h' -> 240, '1d' -> 1440; TradingView '15', '60', 'D', 'W' too."""
    tf = str(tf).strip()
    if tf.isdigit():
        return int(tf)
    up = tf.upper()
    if up in ("D", "1D"):
        return 1440
    if up in ("W", "1W"):
        return 10080
    unit = tf[-1].lower()
    if unit not in _UNITS or not (tf[:-1] or "1").isdigit():
        raise ValueError(f"unsupported timeframe {tf!r}")
    return int(tf[:-1] or 1) * _UNITS[unit]


def auto_htf_minutes(chart_minutes: int) -> int:
    """ICT-style bias timeframe for a chart timeframe (same table as the Pine script)."""
    if chart_minutes <= 1:
        return 15
    if chart_minutes <= 5:
        return 60
    if chart_minutes <= 60:
        return 240
    if chart_minutes <= 240:
        return 1440
    return 10080


def timeframe_label(minutes: int) -> str:
    if minutes % 10080 == 0:
        return "W" if minutes == 10080 else f"{minutes // 10080}W"
    if minutes % 1440 == 0:
        return "D" if minutes == 1440 else f"{minutes // 1440}D"
    if minutes % 60 == 0:
        return f"H{minutes // 60}"
    return f"M{minutes}"


@lru_cache(maxsize=256)
def _shifted_local(ts: float, tz: str, shift_h: int) -> datetime:
    local = datetime.fromtimestamp(ts, ZoneInfo(tz)).replace(tzinfo=None)
    return local + timedelta(hours=shift_h)


_EPOCH = datetime(1970, 1, 1)


class Bucketer:
    """Assigns bars to higher-timeframe candles the way a broker builds them.

    ``tz`` + ``roll_hour`` define when a trading day starts in local time. The
    default (UTC, 0) gives plain UTC candles (crypto). XAUUSD / forex brokers
    and TradingView use New York 17:00: ``Bucketer(1440, "America/New_York", 17)``
    makes the Monday-17:00 -> Tuesday-17:00 session one daily candle, H4 candles
    start at 17, 21, 01, ... NY and weekly candles start Sunday 17:00 NY.
    """

    def __init__(self, minutes: int, tz: str = "UTC", roll_hour: int = 0) -> None:
        self.minutes = minutes
        self.tz = tz
        self.shift = (24 - roll_hour) % 24

    def local(self, t: datetime) -> datetime:
        return _shifted_local(t.timestamp(), self.tz, self.shift)

    def key(self, t: datetime) -> int:
        loc = self.local(t)
        if self.minutes >= 10080:
            y, w, _ = loc.date().isocalendar()
            return y * 100 + w
        if self.minutes % 1440 == 0:
            return loc.date().toordinal() // (self.minutes // 1440)
        return int((loc - _EPOCH).total_seconds() // (self.minutes * 60))
