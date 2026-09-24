"""Economic calendar for the news guard.

Sources, in order of preference:

1. the ForexFactory weekly JSON feed (``news_url``), cached on disk;
2. a CSV you maintain (``news_file``: ``time,currency,impact,title`` with an
   ISO timestamp incl. timezone) - also how you feed history to backtests;
3. always: the typical US release windows (08:30 / 10:00 / 14:00 New York)
   handled by the guard itself, so an unreachable feed never means "no news".
"""

from __future__ import annotations

import csv
import json
import logging
import time
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

log = logging.getLogger(__name__)

IMPACT_RANK = {"low": 1, "medium": 2, "high": 3, "holiday": 0}


@dataclass(frozen=True)
class NewsEvent:
    time: datetime  # UTC
    currency: str
    impact: str  # high | medium | low | holiday
    title: str

    def to_dict(self) -> dict[str, str]:
        return {"time": self.time.isoformat(), "currency": self.currency, "impact": self.impact, "title": self.title}


def _parse_time(value: str) -> datetime:
    t = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    return t.astimezone(timezone.utc)


def _norm_impact(value: str) -> str:
    v = (value or "").strip().lower()
    if v.startswith("high") or v == "red":
        return "high"
    if v.startswith("med") or v == "orange":
        return "medium"
    if v.startswith("hol"):
        return "holiday"
    return "low"


class NewsCalendar:
    def __init__(self, events: Iterable[NewsEvent] = (), available: bool = True, source: str = "") -> None:
        self.events = sorted(events, key=lambda e: e.time)
        self.available = available
        self.source = source

    # ---------------------------------------------------------------- loaders
    @classmethod
    def from_forexfactory(cls, rows: list[dict[str, Any]], source: str = "forexfactory") -> "NewsCalendar":
        events = []
        for r in rows:
            try:
                events.append(NewsEvent(_parse_time(r["date"]), str(r.get("country", "")).upper(),
                                        _norm_impact(str(r.get("impact", ""))), str(r.get("title", ""))))
            except (KeyError, ValueError):
                continue
        return cls(events, True, source)

    @classmethod
    def from_csv(cls, path: str | Path) -> "NewsCalendar":
        events = []
        with Path(path).open() as fh:
            for r in csv.DictReader(fh):
                try:
                    events.append(NewsEvent(_parse_time(r["time"]), r.get("currency", "USD").upper(),
                                            _norm_impact(r.get("impact", "high")), r.get("title", "")))
                except (KeyError, ValueError):
                    continue
        return cls(events, True, str(path))

    @classmethod
    def fetch(cls, url: str, cache_path: str | Path | None = None, max_age_h: float = 6.0,
              timeout: float = 10.0) -> "NewsCalendar":
        """Download the feed (using a fresh cache when possible); never raises."""
        cache = Path(cache_path) if cache_path else None
        if cache and cache.exists() and time.time() - cache.stat().st_mtime < max_age_h * 3600:
            try:
                return cls.from_forexfactory(json.loads(cache.read_text()), f"cache:{cache}")
            except (OSError, ValueError):
                pass
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "smc-agent/1.0"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
            rows = json.loads(raw)
            if cache:
                cache.parent.mkdir(parents=True, exist_ok=True)
                cache.write_bytes(raw)
            return cls.from_forexfactory(rows, url)
        except Exception as exc:  # noqa: BLE001 - offline must degrade to cautious mode, not crash
            log.warning("news calendar unavailable (%s); using standard release windows only", exc)
            if cache and cache.exists():
                try:
                    return cls.from_forexfactory(json.loads(cache.read_text()), f"stale-cache:{cache}")
                except (OSError, ValueError):
                    pass
            return cls([], False, "unavailable")

    def merge(self, other: "NewsCalendar") -> "NewsCalendar":
        return NewsCalendar([*self.events, *other.events], self.available or other.available,
                            f"{self.source}+{other.source}")

    # ---------------------------------------------------------------- queries
    def relevant(self, currencies: Iterable[str], min_impact: str) -> list[NewsEvent]:
        cur = {c.upper() for c in currencies}
        floor = IMPACT_RANK[min_impact]
        return [e for e in self.events if e.currency in cur and IMPACT_RANK.get(e.impact, 0) >= floor]

    def window_hit(self, now: datetime, currencies: Iterable[str], min_impact: str,
                   before_min: int, after_min: int) -> NewsEvent | None:
        """The event whose [t - before, t + after] window contains ``now``."""
        for e in self.relevant(currencies, min_impact):
            if e.time - timedelta(minutes=before_min) <= now <= e.time + timedelta(minutes=after_min):
                return e
        return None

    def upcoming(self, now: datetime, currencies: Iterable[str], min_impact: str, within_min: int) -> list[NewsEvent]:
        end = now + timedelta(minutes=within_min)
        return [e for e in self.relevant(currencies, min_impact) if now <= e.time <= end]

    def holiday(self, now: datetime, currencies: Iterable[str], tz: Any) -> NewsEvent | None:
        cur = {c.upper() for c in currencies}
        day = now.astimezone(tz).date()
        for e in self.events:
            if e.impact == "holiday" and e.currency in cur and e.time.astimezone(tz).date() == day:
                return e
        return None
