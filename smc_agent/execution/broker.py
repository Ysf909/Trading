"""Broker interface and the paper-trading broker (the default)."""

from __future__ import annotations

import json
import logging
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

from ..core.types import Bar, Signal, signal_from_dict
from ..risk import AccountState
from .sim import Trade, force_close, settle, step

log = logging.getLogger(__name__)


class Broker(ABC):
    name = "broker"

    @abstractmethod
    def equity(self) -> float: ...

    @abstractmethod
    def account_state(self) -> AccountState: ...

    def size(self, sig: Signal, risk_amount: float) -> float:
        """Quantity in base units so that hitting the stop loses ``risk_amount``."""
        return 0.0 if sig.risk <= 0 else risk_amount / sig.risk

    @abstractmethod
    def place(self, sig: Signal, qty: float) -> str:
        """Submit a limit entry with attached stop and target; return an order id."""

    @abstractmethod
    def on_bar(self, symbol: str, bar: Bar, t: int) -> list[dict[str, Any]]:
        """Called for every new closed bar of ``symbol`` (engine bar index ``t``)."""

    # --- position management used by the risk guard ------------------------
    def position_info(self, symbol: str) -> dict[str, Any] | None:
        """``{"status": "pending"|"open", "direction", "fill_price", "be_moved"}`` or None."""
        return None

    def cancel_pending(self, symbol: str, reason: str) -> list[dict[str, Any]]:
        return []

    def close_position(self, symbol: str, reason: str) -> list[dict[str, Any]]:
        return []

    def protect(self, symbol: str, reason: str) -> list[dict[str, Any]]:
        """Move the stop of an open position to its entry price."""
        return []

    def spread(self, symbol: str) -> float | None:
        """Current spread in price units, when the venue exposes it."""
        return None

    def symbols(self) -> set[str]:
        st = self.account_state()
        return st.open_symbols | st.pending_symbols

    def close(self) -> None:  # pragma: no cover - optional hook
        pass


class PaperBroker(Broker):
    """Simulated execution using the exact backtest fill rules, with state
    persisted to JSON so open positions survive a restart."""

    name = "paper"

    def __init__(
        self,
        starting_equity: float = 10_000.0,
        commission_pct: float = 0.02,
        slippage_pct: float = 0.0,
        breakeven_at_r: float = 0.0,
        tp1_r: float = 0.0,
        tp1_pct: float = 50.0,
        state_path: str | Path | None = None,
    ) -> None:
        self.cash = starting_equity
        self.commission_pct = commission_pct
        self.slippage_pct = slippage_pct
        self.breakeven_at_r = breakeven_at_r
        self.tp1_r = tp1_r
        self.tp1_pct = tp1_pct
        self.state_path = Path(state_path) if state_path else None
        self.active: dict[str, Trade] = {}
        self.closed: list[dict[str, Any]] = []
        self.last_price: dict[str, float] = {}
        self._load()

    # ----------------------------------------------------------------- state
    def equity(self) -> float:
        unreal = 0.0
        for sym, tr in self.active.items():
            if tr.status == "open" and sym in self.last_price:
                unreal += (self.last_price[sym] - tr.fill_price) * tr.direction * tr.qty
        return self.cash + unreal

    def account_state(self) -> AccountState:
        return AccountState(
            equity=self.equity(),
            open_symbols={s for s, t in self.active.items() if t.status == "open"},
            pending_symbols={s for s, t in self.active.items() if t.status == "pending"},
        )

    # ---------------------------------------------------------------- orders
    def place(self, sig: Signal, qty: float) -> str:
        cur = self.active.get(sig.symbol)
        if cur is not None and cur.status == "open":
            raise RuntimeError(f"position already open on {sig.symbol}")
        if cur is not None:
            log.info("paper: replacing pending order %s", cur.signal.id)
        self.active[sig.symbol] = Trade(sig, qty=qty)
        self._save()
        return sig.id

    def position_info(self, symbol: str) -> dict[str, Any] | None:
        tr = self.active.get(symbol)
        if tr is None:
            return None
        return {"status": tr.status, "direction": tr.direction, "fill_price": tr.fill_price,
                "be_moved": tr.be_moved}

    def cancel_pending(self, symbol: str, reason: str) -> list[dict[str, Any]]:
        tr = self.active.get(symbol)
        if tr is None or tr.status != "pending":
            return []
        tr.status, tr.exit_reason = "cancelled", reason
        del self.active[symbol]
        self._save()
        return [{"event": "cancelled", **tr.to_dict()}]

    def close_position(self, symbol: str, reason: str) -> list[dict[str, Any]]:
        from datetime import datetime, timezone

        tr = self.active.get(symbol)
        if tr is None or tr.status != "open":
            return []
        price = self.last_price.get(symbol, tr.fill_price)
        force_close(tr, price, tr.fill_bar, datetime.now(timezone.utc), reason)
        settle(tr, self.commission_pct, self.slippage_pct)
        self.cash += tr.pnl
        rec = tr.to_dict()
        self.closed.append(rec)
        del self.active[symbol]
        self._save()
        return [{"event": "closed", **rec}]

    def protect(self, symbol: str, reason: str) -> list[dict[str, Any]]:
        tr = self.active.get(symbol)
        if tr is None or tr.status != "open" or tr.be_moved:
            return []
        tr.sl, tr.be_moved = tr.fill_price, True
        self._save()
        return [{"event": "protected", "symbol": symbol, "sl": tr.sl, "reason": reason}]

    def on_bar(self, symbol: str, bar: Bar, t: int) -> list[dict[str, Any]]:
        self.last_price[symbol] = bar.close
        tr = self.active.get(symbol)
        if tr is None:
            return []
        ev = step(tr, bar, t, self.breakeven_at_r, self.tp1_r, self.tp1_pct)
        events: list[dict[str, Any]] = []
        if ev in ("filled", "filled+closed"):
            events.append({"event": "filled", **tr.to_dict()})
        if ev == "partial":
            events.append({"event": "partial", **tr.to_dict()})
        if tr.status == "closed":
            settle(tr, self.commission_pct, self.slippage_pct)
            self.cash += tr.pnl
            rec = tr.to_dict()
            self.closed.append(rec)
            events.append({"event": "closed", **rec})
            del self.active[symbol]
        elif tr.status == "cancelled":
            events.append({"event": "cancelled", **tr.to_dict()})
            del self.active[symbol]
        if ev or events:
            self._save()
        return events

    # ------------------------------------------------------------ persistence
    def _save(self) -> None:
        if self.state_path is None:
            return
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "cash": self.cash,
            "closed": self.closed[-500:],
            "open": [
                {"signal": tr.signal.to_dict(), "qty": tr.qty, "sl": tr.sl, "be_moved": tr.be_moved,
                 "part_frac": tr.part_frac, "part_price": tr.part_price,
                 "fill_price": tr.fill_price, "fill_time": tr.fill_time.isoformat() if tr.fill_time else None}
                for tr in self.active.values() if tr.status == "open"
            ],
        }
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=1, default=str))
        tmp.replace(self.state_path)

    def _load(self) -> None:
        if self.state_path is None or not self.state_path.exists():
            return
        from datetime import datetime

        data = json.loads(self.state_path.read_text())
        self.cash = float(data.get("cash", self.cash))
        self.closed = list(data.get("closed", []))
        for rec in data.get("open", []):
            sig = signal_from_dict(rec["signal"])
            tr = Trade(sig, qty=rec["qty"], status="open", sl=rec["sl"], be_moved=rec.get("be_moved", False))
            tr.fill_price = rec["fill_price"]
            tr.part_frac = float(rec.get("part_frac", 0.0))
            tr.part_price = float(rec.get("part_price", 0.0))
            tr.fill_time = datetime.fromisoformat(rec["fill_time"]) if rec.get("fill_time") else None
            self.active[sig.symbol] = tr
        # pending orders are not restored: their bar clock restarts with the engine
        log.info("paper: restored %d open position(s), cash %.2f", len(self.active), self.cash)
