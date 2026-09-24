"""MetaTrader 5 execution (XAUUSD, forex, indices, CFDs).

Requires Windows with a running MT5 terminal and ``pip install MetaTrader5``.
Pending limit orders carry the stop-loss and take-profit natively and expire
server-side. The broker also exposes what the risk guard needs: position
state, closing / cancelling / moving the stop to entry, the live spread, and
the result of closed trades in R (for the losing-streak breaker).
Use a demo account first.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from ..config import BrokerConfig
from ..core.timeframes import timeframe_minutes
from ..core.types import LONG, Bar, Signal
from ..risk import AccountState
from .broker import Broker

log = logging.getLogger(__name__)


@dataclass
class _Tracked:
    ticket: int  # order ticket == position identifier once filled
    signal: Signal
    risk_money: float
    status: str = "pending"  # pending | open


class MT5Broker(Broker):
    name = "mt5"

    def __init__(self, cfg: BrokerConfig) -> None:
        import MetaTrader5 as mt5  # optional dependency

        if not mt5.initialize():
            raise RuntimeError(f"MT5 initialize() failed: {mt5.last_error()}")
        self.mt5 = mt5
        self.cfg = cfg
        self.tracked: dict[str, _Tracked] = {}

    # ------------------------------------------------------------ account
    def equity(self) -> float:
        info = self.mt5.account_info()
        return float(info.equity) if info else 0.0

    def _positions(self, symbol: str | None = None) -> list[Any]:
        rows = self.mt5.positions_get(symbol=symbol) if symbol else self.mt5.positions_get()
        return [p for p in (rows or []) if p.magic == self.cfg.mt5_magic]

    def _orders(self, symbol: str | None = None) -> list[Any]:
        rows = self.mt5.orders_get(symbol=symbol) if symbol else self.mt5.orders_get()
        return [o for o in (rows or []) if o.magic == self.cfg.mt5_magic]

    def account_state(self) -> AccountState:
        return AccountState(
            equity=self.equity(),
            open_symbols={p.symbol for p in self._positions()},
            pending_symbols={o.symbol for o in self._orders()},
        )

    def spread(self, symbol: str) -> float | None:
        tick = self.mt5.symbol_info_tick(symbol)
        return None if tick is None else float(tick.ask - tick.bid)

    def size(self, sig: Signal, risk_amount: float) -> float:
        info = self.mt5.symbol_info(sig.symbol)
        if info is None:
            raise RuntimeError(f"unknown MT5 symbol {sig.symbol}")
        ticks = sig.risk / info.trade_tick_size
        loss_per_lot = ticks * info.trade_tick_value
        if loss_per_lot <= 0:
            return 0.0
        lots = risk_amount / loss_per_lot
        # never exceed the configured leverage (XAUUSD: 1 lot = 100 oz)
        acct = self.mt5.account_info()
        if acct is not None and self.cfg.max_leverage > 0 and info.trade_contract_size > 0:
            max_lots = acct.equity * self.cfg.max_leverage / (sig.entry * info.trade_contract_size)
            lots = min(lots, max_lots)
        step = info.volume_step
        lots = math.floor(lots / step) * step
        if lots < info.volume_min:
            return 0.0
        return round(min(lots, info.volume_max), 8)

    # ------------------------------------------------------------- orders
    def place(self, sig: Signal, qty: float) -> str:
        mt5 = self.mt5
        if qty <= 0:
            raise RuntimeError("position size rounds to zero lots for this stop distance")
        self.cancel_pending(sig.symbol, "replaced by a new setup")
        tick = mt5.symbol_info_tick(sig.symbol)
        if tick is None:
            raise RuntimeError(f"no tick for {sig.symbol} (market closed?)")
        long = sig.direction == LONG
        # never chase: if price already traded through the stop, the setup is dead
        if (long and tick.bid <= sig.sl) or (not long and tick.ask >= sig.sl):
            raise RuntimeError("price is already beyond the stop - setup invalidated")
        market_now = (tick.ask <= sig.entry) if long else (tick.bid >= sig.entry)
        tf_min = timeframe_minutes(sig.timeframe) if sig.timeframe else 15
        expires = datetime.now(timezone.utc) + timedelta(minutes=tf_min * sig.expiry_bars)
        req: dict[str, Any] = {
            "symbol": sig.symbol,
            "volume": float(qty),
            "sl": float(sig.sl),
            "tp": float(sig.tp),
            "deviation": self.cfg.mt5_deviation,
            "magic": self.cfg.mt5_magic,
            "comment": f"smc {sig.model[:4]} {sig.grade}",
        }
        if market_now:  # price already at/through the entry: take it at market
            req.update(action=mt5.TRADE_ACTION_DEAL, type=mt5.ORDER_TYPE_BUY if long else mt5.ORDER_TYPE_SELL,
                       price=tick.ask if long else tick.bid, type_filling=mt5.ORDER_FILLING_IOC)
        else:
            req.update(action=mt5.TRADE_ACTION_PENDING,
                       type=mt5.ORDER_TYPE_BUY_LIMIT if long else mt5.ORDER_TYPE_SELL_LIMIT,
                       price=float(sig.entry), type_time=mt5.ORDER_TIME_SPECIFIED,
                       expiration=int(expires.timestamp()), type_filling=mt5.ORDER_FILLING_RETURN)
        res = mt5.order_send(req)
        if res is None or res.retcode not in (mt5.TRADE_RETCODE_DONE, mt5.TRADE_RETCODE_PLACED):
            raise RuntimeError(f"MT5 order_send failed: {getattr(res, 'retcode', None)} {getattr(res, 'comment', '')}")
        info = mt5.symbol_info(sig.symbol)
        risk_money = qty * sig.risk / info.trade_tick_size * info.trade_tick_value if info else 0.0
        self.tracked[sig.symbol] = _Tracked(res.order, sig, risk_money, "open" if market_now else "pending")
        log.info("mt5: %s %s %.2f lots @ %s sl %s tp %s ticket %s", "BUY" if long else "SELL",
                 sig.symbol, qty, req["price"], sig.sl, sig.tp, res.order)
        return str(res.order)

    def _send(self, req: dict[str, Any], what: str) -> bool:
        res = self.mt5.order_send(req)
        ok = res is not None and res.retcode == self.mt5.TRADE_RETCODE_DONE
        if not ok:
            log.error("mt5: %s failed: %s %s", what, getattr(res, "retcode", None), getattr(res, "comment", ""))
        return ok

    def cancel_pending(self, symbol: str, reason: str) -> list[dict[str, Any]]:
        events = []
        for o in self._orders(symbol):
            if self._send({"action": self.mt5.TRADE_ACTION_REMOVE, "order": o.ticket}, f"cancel {o.ticket}"):
                events.append({"event": "cancelled", "symbol": symbol, "ticket": o.ticket, "reason": reason})
        tr = self.tracked.get(symbol)
        if tr is not None and tr.status == "pending" and events:
            del self.tracked[symbol]
        return events

    def close_position(self, symbol: str, reason: str) -> list[dict[str, Any]]:
        mt5 = self.mt5
        events = []
        for p in self._positions(symbol):
            tick = mt5.symbol_info_tick(symbol)
            buy = p.type == mt5.POSITION_TYPE_BUY
            req = {
                "action": mt5.TRADE_ACTION_DEAL, "symbol": symbol, "volume": p.volume, "position": p.ticket,
                "type": mt5.ORDER_TYPE_SELL if buy else mt5.ORDER_TYPE_BUY,
                "price": tick.bid if buy else tick.ask, "deviation": self.cfg.mt5_deviation,
                "magic": self.cfg.mt5_magic, "comment": "smc guard exit", "type_filling": mt5.ORDER_FILLING_IOC,
            }
            if self._send(req, f"close {p.ticket}"):
                events.append({"event": "guard_close", "symbol": symbol, "ticket": p.ticket, "reason": reason})
        return events

    def protect(self, symbol: str, reason: str) -> list[dict[str, Any]]:
        mt5 = self.mt5
        events = []
        for p in self._positions(symbol):
            if abs(p.sl - p.price_open) < 1e-9:
                continue
            req = {"action": mt5.TRADE_ACTION_SLTP, "symbol": symbol, "position": p.ticket,
                   "sl": p.price_open, "tp": p.tp, "magic": self.cfg.mt5_magic}
            if self._send(req, f"protect {p.ticket}"):
                events.append({"event": "protected", "symbol": symbol, "ticket": p.ticket, "sl": p.price_open,
                               "reason": reason})
        return events

    def position_info(self, symbol: str) -> dict[str, Any] | None:
        pos = self._positions(symbol)
        if pos:
            p = pos[0]
            return {"status": "open", "direction": 1 if p.type == self.mt5.POSITION_TYPE_BUY else -1,
                    "fill_price": p.price_open, "be_moved": abs(p.sl - p.price_open) < 1e-9}
        orders = self._orders(symbol)
        if orders:
            o = orders[0]
            return {"status": "pending", "direction": 1 if o.type == self.mt5.ORDER_TYPE_BUY_LIMIT else -1,
                    "fill_price": 0.0, "be_moved": False}
        return None

    def _closed_result(self, tr: _Tracked) -> dict[str, Any]:
        deals = self.mt5.history_deals_get(position=tr.ticket) or []
        pnl = float(sum(d.profit + d.commission + d.swap for d in deals))
        r = pnl / tr.risk_money if tr.risk_money > 0 else 0.0
        return {"event": "closed", "symbol": tr.signal.symbol, "ticket": tr.ticket, "pnl": pnl, "r": r,
                "side": tr.signal.side}

    def on_bar(self, symbol: str, bar: Bar, t: int) -> list[dict[str, Any]]:
        tr = self.tracked.get(symbol)
        if tr is None:
            return []
        mt5 = self.mt5
        events: list[dict[str, Any]] = []
        if tr.status == "pending":
            if mt5.orders_get(ticket=tr.ticket):
                sig = tr.signal
                reached_tp = bar.high >= sig.tp if sig.direction == LONG else bar.low <= sig.tp
                if reached_tp:
                    return self.cancel_pending(symbol, "missed: target traded before the fill")
                return []
            if mt5.positions_get(ticket=tr.ticket):
                tr.status = "open"
                events.append({"event": "filled", "symbol": symbol, "ticket": tr.ticket, "side": tr.signal.side})
            else:  # expired / removed without a fill, or filled and already closed
                deals = mt5.history_deals_get(position=tr.ticket) or []
                if deals:
                    events.append(self._closed_result(tr))
                else:
                    events.append({"event": "cancelled", "symbol": symbol, "ticket": tr.ticket, "reason": "expired"})
                del self.tracked[symbol]
                return events
        if tr.status == "open" and not mt5.positions_get(ticket=tr.ticket):
            events.append(self._closed_result(tr))
            del self.tracked[symbol]
        return events

    def close(self) -> None:
        self.mt5.shutdown()
