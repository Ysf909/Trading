"""ICT killzones, session ranges and previous-day levels."""

from __future__ import annotations

from datetime import datetime
from functools import lru_cache
from zoneinfo import ZoneInfo

from ..config import KILLZONES
from .timeframes import Bucketer

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


class PeriodLevels:
    """Tracks the running day's / week's range; returns the prior one on rollover.

    Periods follow the broker session (see ``Bucketer``): for XAUUSD use
    ``tz="America/New_York", roll_hour=17``."""

    def __init__(self, minutes: int = 1440, tz: str = "UTC", roll_hour: int = 0) -> None:
        self.bucketer = Bucketer(minutes, tz, roll_hour)
        self.key: int | None = None
        self.high = float("-inf")
        self.low = float("inf")
        self.start_bar = -1
        self.history: list[tuple[float, float]] = []  # completed (high, low)

    def update(self, t_time: datetime, t: int, high: float, low: float) -> tuple[float, float, int] | None:
        k = self.bucketer.key(t_time)
        finished = None
        if self.key is None or k != self.key:
            if self.key is not None:
                finished = (self.high, self.low, self.start_bar)
                self.history.append((self.high, self.low))
                if len(self.history) > 60:
                    del self.history[0]
            self.key, self.high, self.low, self.start_bar = k, high, low, t
        else:
            self.high = max(self.high, high)
            self.low = min(self.low, low)
        return finished

    def average_range(self, n: int) -> float | None:
        if len(self.history) < n:
            return None
        rows = self.history[-n:]
        return sum(h - l for h, l in rows) / n
