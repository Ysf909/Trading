"""Deterministic order/position simulation shared by the backtester and the
paper broker (and mirrored by the Pine stats tracker).

Conservative intrabar rules (we never know the path inside a candle):

* pending limit: fills at the open if the bar gaps through the entry,
  otherwise at the entry price when the bar trades through it. If the bar
  reaches the target without filling, the order is cancelled ("missed").
* on the fill bar only the stop is checked (a target on the same bar is not
  assumed to have happened after the fill).
* afterwards the stop is always checked before the target.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from ..core.types import LONG, Bar, Signal


@dataclass
class Trade:
    signal: Signal
    qty: float = 0.0
    status: str = "pending"  # pending | open | closed | cancelled
    sl: float = 0.0
    be_moved: bool = False
    fill_bar: int = -1
    fill_price: float = 0.0
    fill_time: datetime | None = None
    exit_bar: int = -1
    exit_price: float = 0.0
    exit_time: datetime | None = None
    exit_reason: str = ""
    fees: float = 0.0
    pnl: float = 0.0
    meta: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.sl:
            self.sl = self.signal.sl

    @property
    def direction(self) -> int:
        return self.signal.direction

    @property
    def r_multiple(self) -> float:
        """Gross R: result relative to the *planned* risk."""
        if self.status != "closed":
            return 0.0
        s = self.signal
        return (self.exit_price - self.fill_price) * s.direction / s.risk

    def to_dict(self) -> dict[str, Any]:
        s = self.signal
        return {
            "id": s.id,
            "symbol": s.symbol,
            "side": s.side,
            "model": s.model,
            "grade": s.grade,
            "score": s.score,
            "probability": s.probability,
            "signal_time": s.time.isoformat(),
            "entry": s.entry,
            "sl": s.sl,
            "tp": s.tp,
            "rr": round(s.rr, 3),
            "status": self.status,
            "fill_time": self.fill_time.isoformat() if self.fill_time else None,
            "fill_price": self.fill_price,
            "exit_time": self.exit_time.isoformat() if self.exit_time else None,
            "exit_price": self.exit_price,
            "exit_reason": self.exit_reason,
            "r": round(self.r_multiple, 4),
            "qty": self.qty,
            "fees": round(self.fees, 6),
            "pnl": round(self.pnl, 6),
        }


def _close(trade: Trade, price: float, t: int, time: datetime, reason: str) -> None:
    trade.status = "closed"
    trade.exit_price = price
    trade.exit_bar = t
    trade.exit_time = time
    trade.exit_reason = reason


def step(trade: Trade, bar: Bar, t: int, breakeven_at_r: float = 0.0) -> str | None:
    """Advance ``trade`` by one bar. Returns the event name if one occurred:
    'filled', 'closed', 'filled+closed', 'cancelled' or None."""
    s = trade.signal
    d = s.direction
    if trade.status == "pending":
        if t - s.bar > s.expiry_bars:
            trade.status, trade.exit_reason, trade.exit_bar = "cancelled", "expired", t
            return "cancelled"
        gap_fill = bar.open <= s.entry if d == LONG else bar.open >= s.entry
        touched = bar.low <= s.entry if d == LONG else bar.high >= s.entry
        if gap_fill or touched:
            trade.status = "open"
            trade.fill_price = bar.open if gap_fill else s.entry
            trade.fill_bar, trade.fill_time = t, bar.time
            stopped = bar.low <= trade.sl if d == LONG else bar.high >= trade.sl
            if stopped:
                gap_stop = bar.open <= trade.sl if d == LONG else bar.open >= trade.sl
                _close(trade, bar.open if gap_stop else trade.sl, t, bar.time, "sl")
                return "filled+closed"
            return "filled"
        reached_tp = bar.high >= s.tp if d == LONG else bar.low <= s.tp
        if reached_tp:
            trade.status, trade.exit_reason, trade.exit_bar = "cancelled", "missed", t
            return "cancelled"
        return None

    if trade.status == "open":
        sl, tp = trade.sl, s.tp
        if d == LONG:
            if bar.open <= sl:
                _close(trade, bar.open, t, bar.time, "be" if trade.be_moved else "sl")
            elif bar.low <= sl:
                _close(trade, sl, t, bar.time, "be" if trade.be_moved else "sl")
            elif bar.open >= tp:
                _close(trade, bar.open, t, bar.time, "tp")
            elif bar.high >= tp:
                _close(trade, tp, t, bar.time, "tp")
        else:
            if bar.open >= sl:
                _close(trade, bar.open, t, bar.time, "be" if trade.be_moved else "sl")
            elif bar.high >= sl:
                _close(trade, sl, t, bar.time, "be" if trade.be_moved else "sl")
            elif bar.open <= tp:
                _close(trade, bar.open, t, bar.time, "tp")
            elif bar.low <= tp:
                _close(trade, tp, t, bar.time, "tp")
        if trade.status == "closed":
            return "closed"
        if breakeven_at_r > 0 and not trade.be_moved:
            trigger = s.entry + d * breakeven_at_r * s.risk
            if (bar.high >= trigger) if d == LONG else (bar.low <= trigger):
                trade.sl = trade.fill_price
                trade.be_moved = True
        return None
    return None


def settle(trade: Trade, commission_pct: float, slippage_pct: float = 0.0) -> None:
    """Fill in fees and money PnL for a closed trade (qty must be set)."""
    if trade.status != "closed":
        return
    d = trade.direction
    exit_px = trade.exit_price
    if trade.exit_reason in ("sl", "be") and slippage_pct:
        exit_px -= d * exit_px * slippage_pct / 100.0
    notional = (trade.fill_price + exit_px) * trade.qty
    trade.fees = notional * commission_pct / 100.0
    trade.pnl = (exit_px - trade.fill_price) * d * trade.qty - trade.fees
