"""MetaTrader 5 execution (forex, indices, metals, CFDs).

Requires Windows with a running MT5 terminal and ``pip install MetaTrader5``.
Pending limit orders carry the stop-loss and take-profit natively and expire
server-side. Use a demo account first.
"""

from __future__ import annotations

import logging
import math
from datetime import datetime, timedelta, timezone
from typing import Any

from ..config import BrokerConfig
from ..core.timeframes import timeframe_minutes
from ..core.types import LONG, Bar, Signal
from ..risk import AccountState
from .broker import Broker

log = logging.getLogger(__name__)


class MT5Broker(Broker):
    name = "mt5"

    def __init__(self, cfg: BrokerConfig) -> None:
        import MetaTrader5 as mt5  # optional dependency

        if not mt5.initialize():
            raise RuntimeError(f"MT5 initialize() failed: {mt5.last_error()}")
        self.mt5 = mt5
        self.cfg = cfg
        self.tickets: dict[str, int] = {}

    def equity(self) -> float:
        info = self.mt5.account_info()
        return float(info.equity) if info else 0.0

    def account_state(self) -> AccountState:
        positions = self.mt5.positions_get() or []
        orders = self.mt5.orders_get() or []
        magic = self.cfg.mt5_magic
        return AccountState(
            equity=self.equity(),
            open_symbols={p.symbol for p in positions if p.magic == magic},
            pending_symbols={o.symbol for o in orders if o.magic == magic},
        )

    def size(self, sig: Signal, risk_amount: float) -> float:
        info = self.mt5.symbol_info(sig.symbol)
        if info is None:
            raise RuntimeError(f"unknown MT5 symbol {sig.symbol}")
        ticks = sig.risk / info.trade_tick_size
        loss_per_lot = ticks * info.trade_tick_value
        if loss_per_lot <= 0:
            return 0.0
        lots = risk_amount / loss_per_lot
        step = info.volume_step
        lots = math.floor(lots / step) * step
        if lots < info.volume_min:
            return 0.0
        return round(min(lots, info.volume_max), 8)

    def place(self, sig: Signal, qty: float) -> str:
        mt5 = self.mt5
        if qty <= 0:
            raise RuntimeError("position size rounds to zero lots for this stop distance")
        old = self.tickets.get(sig.symbol)
        if old:
            self._remove(old)
        tick = mt5.symbol_info_tick(sig.symbol)
        long = sig.direction == LONG
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
        self.tickets[sig.symbol] = res.order
        log.info("mt5: %s %s %.2f lots @ %s sl %s tp %s ticket %s", "BUY" if long else "SELL",
                 sig.symbol, qty, req["price"], sig.sl, sig.tp, res.order)
        return str(res.order)

    def _remove(self, ticket: int) -> None:
        mt5 = self.mt5
        if not mt5.orders_get(ticket=ticket):
            return
        res = mt5.order_send({"action": mt5.TRADE_ACTION_REMOVE, "order": ticket})
        if res is None or res.retcode != mt5.TRADE_RETCODE_DONE:
            log.warning("mt5: could not remove order %s: %s", ticket, getattr(res, "comment", ""))

    def on_bar(self, symbol: str, bar: Bar, t: int) -> list[dict[str, Any]]:
        # expiry and SL/TP are handled server-side; cancel if target traded before fill
        ticket = self.tickets.get(symbol)
        if not ticket:
            return []
        orders = self.mt5.orders_get(ticket=ticket)
        if not orders:
            self.tickets.pop(symbol, None)
            return [{"event": "order_done", "symbol": symbol, "ticket": ticket}]
        o = orders[0]
        long = o.type == self.mt5.ORDER_TYPE_BUY_LIMIT
        if (long and bar.high >= o.tp) or (not long and bar.low <= o.tp):
            self._remove(ticket)
            self.tickets.pop(symbol, None)
            return [{"event": "cancelled", "symbol": symbol, "ticket": ticket, "reason": "missed"}]
        return []

    def close(self) -> None:
        self.mt5.shutdown()
