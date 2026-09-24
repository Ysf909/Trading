"""ICT killzones, session ranges and previous-day levels."""

from __future__ import annotations

from datetime import date, datetime
from functools import lru_cache
from zoneinfo import ZoneInfo

from ..config import KILLZONES

NY = ZoneInfo("America/New_York")


def _minutes(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


@lru_cache(maxsize=None)
def _window(name: str) -> tuple[int, int]:
    start, end = KILLZONES[name]
    return _minutes(start), _minutes(end)


def ny_minutes(t: datetime) -> int:
    local = t.astimezone(NY)
    return local.hour * 60 + local.minute


def in_window(minute_of_day: int, name: str) -> bool:
    start, end = _window(name)
    if start < end:
        return start <= minute_of_day < end
    # wraps midnight (e.g. 20:00 -> 00:00)
    return minute_of_day >= start or minute_of_day < end


def active_killzones(t: datetime) -> list[str]:
    m = ny_minutes(t)
    return [name for name in KILLZONES if in_window(m, name)]


class SessionRange:
    """High/low of a recurring session; reports the range when it closes."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.active = False
        self.high = float("-inf")
        self.low = float("inf")
        self.start_bar = -1

    def update(self, ny_minute: int, t: int, high: float, low: float) -> tuple[float, float, int] | None:
        inside = in_window(ny_minute, self.name)
        finished = None
        if inside:
            if not self.active:
                self.active = True
                self.high, self.low, self.start_bar = high, low, t
            else:
                self.high = max(self.high, high)
                self.low = min(self.low, low)
        elif self.active:
            self.active = False
            finished = (self.high, self.low, self.start_bar)
        return finished


class DailyLevels:
    """Tracks the running day's range; returns the prior day on rollover."""

    def __init__(self, tz: str = "UTC") -> None:
        self.tz = ZoneInfo(tz)
        self.day: date | None = None
        self.high = float("-inf")
        self.low = float("inf")
        self.start_bar = -1

    def update(self, t_time: datetime, t: int, high: float, low: float) -> tuple[float, float, int] | None:
        d = t_time.astimezone(self.tz).date()
        finished = None
        if self.day is None or d != self.day:
            if self.day is not None:
                finished = (self.high, self.low, self.start_bar)
            self.day, self.high, self.low, self.start_bar = d, high, low, t
        else:
            self.high = max(self.high, high)
            self.low = min(self.low, low)
        return finished
