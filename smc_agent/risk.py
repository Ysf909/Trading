"""Pre-trade risk checks shared by the live agent and the webhook server."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone

from .config import RiskConfig
from .core.types import Signal


@dataclass
class AccountState:
    equity: float
    open_symbols: set[str] = field(default_factory=set)
    pending_symbols: set[str] = field(default_factory=set)


class RiskManager:
    """Daily loss limit, position limits, trade count limit, RR floor."""

    def __init__(self, cfg: RiskConfig) -> None:
        self.cfg = cfg
        self.day: date | None = None
        self.day_start_equity = 0.0
        self.trades_today = 0
        self.halted_reason = ""

    def _roll(self, equity: float, now: datetime) -> None:
        today = now.astimezone(timezone.utc).date()
        if self.day != today:
            self.day = today
            self.day_start_equity = equity
            self.trades_today = 0
            self.halted_reason = ""

    def check(self, sig: Signal, state: AccountState, now: datetime | None = None) -> tuple[bool, str]:
        now = now or datetime.now(timezone.utc)
        self._roll(state.equity, now)
        c = self.cfg
        if self.day_start_equity > 0:
            dd = (self.day_start_equity - state.equity) / self.day_start_equity * 100.0
            if dd >= c.max_daily_loss_pct:
                self.halted_reason = f"daily loss limit hit ({dd:.2f}% >= {c.max_daily_loss_pct}%)"
                return False, self.halted_reason
        if self.trades_today >= c.max_trades_per_day:
            return False, f"max trades per day reached ({c.max_trades_per_day})"
        if c.one_position_per_symbol and sig.symbol in state.open_symbols:
            return False, f"already in a position on {sig.symbol}"
        if len(state.open_symbols) >= c.max_open_positions and sig.symbol not in state.open_symbols:
            return False, f"max open positions reached ({c.max_open_positions})"
        if sig.rr < c.min_rr:
            return False, f"RR {sig.rr:.2f} below minimum {c.min_rr}"
        if sig.risk <= 0:
            return False, "invalid stop distance"
        return True, "ok"

    def risk_amount(self, equity: float) -> float:
        return equity * self.cfg.risk_per_trade_pct / 100.0

    def record_entry(self) -> None:
        self.trades_today += 1
