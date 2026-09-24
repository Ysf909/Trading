"""Plain data objects produced by the analysis engine."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any

LONG = 1
SHORT = -1


def side_name(direction: int) -> str:
    return "long" if direction == LONG else "short"


@dataclass(slots=True)
class Bar:
    time: datetime  # bar open time, timezone-aware UTC
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0


@dataclass(slots=True)
class Pivot:
    price: float
    bar: int
    time: datetime
    label: str  # HH / LH / HL / LL / H / L
    crossed: bool = False


@dataclass(slots=True)
class StructureEvent:
    level: str  # internal | swing | htf
    direction: int
    kind: str  # BOS | CHoCH
    price: float
    pivot_bar: int
    bar: int


@dataclass(slots=True)
class Zone:
    """An order block or fair value gap."""

    kind: str  # OB | FVG
    direction: int
    top: float
    bottom: float
    bar: int  # left anchor bar
    created: int  # bar on which the zone was confirmed
    level: str = ""  # internal/swing for OBs
    touched: bool = False
    end: int = -1  # bar on which it was invalidated / filled (-1 = active)

    @property
    def mid(self) -> float:
        return (self.top + self.bottom) / 2.0

    @property
    def active(self) -> bool:
        return self.end < 0

    def overlaps(self, top: float, bottom: float) -> bool:
        return self.bottom <= top and bottom <= self.top


@dataclass(slots=True)
class LiquidityLevel:
    price: float
    side: int  # +1 buy-side (resting above highs), -1 sell-side (below lows)
    kind: str  # internal | swing | eqh | eql | pdh | pdl | asia_high | ...
    bar: int
    major: bool
    swept_bar: int = -1


@dataclass(slots=True)
class SweepEvent:
    bar: int
    side: int  # side of the liquidity that was taken
    price: float
    kind: str
    major: bool
    rejected: bool  # candle closed back on the other side (wick sweep)


@dataclass
class Signal:
    """A fully specified trade idea produced when a setup arms."""

    id: str
    symbol: str
    timeframe: str
    time: datetime  # close time context: open time of the arming bar
    bar: int
    direction: int
    model: str  # reversal | continuation
    entry: float
    sl: float
    tp: float
    rr: float
    risk_atr: float
    atr: float
    expiry_bars: int
    score: int
    grade: str
    features: dict[str, float]
    reasons: list[str]
    zone: Zone | None = None
    probability: float | None = None
    expected_r: float | None = None
    ai_review: dict[str, Any] | None = None
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def side(self) -> str:
        return side_name(self.direction)

    @property
    def risk(self) -> float:
        return abs(self.entry - self.sl)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["time"] = self.time.isoformat()
        d["side"] = self.side
        return d


def signal_from_dict(d: dict[str, Any]) -> Signal:
    """Inverse of ``Signal.to_dict`` (used for persisted paper-broker state)."""
    d = dict(d)
    d.pop("side", None)
    d["time"] = datetime.fromisoformat(d["time"])
    z = d.get("zone")
    d["zone"] = Zone(**z) if isinstance(z, dict) else None
    return Signal(**d)
