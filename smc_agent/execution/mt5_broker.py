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
from typing import Any

from ..config import BrokerConfig
from ..core.timeframes import timeframe_minutes
from ..core.types import LONG, Bar, Signal
from ..risk import AccountState
from .broker import Broker
from .mt5_common import (ServerClock, allows_specified_expiry, connect, ensure_symbol, filling_for,
                         margin_lot_cap, server_time_now)

log = logging.getLogger(__name__)


@dataclass
class _Tracked:
    tickets: list[int]  # order tickets == position identifiers once filled; [tp1 leg, runner] when split
    signal: Signal
    risk_money: float
    status: str = "pending"  # pending | open
    split: bool = False
    tp1_done: bool = False

    @property
    def ticket(self) -> int:
        return self.tickets[-1]


class MT5Broker(Broker):
    name = "mt5"

    def __init__(self, cfg: BrokerConfig, tp1_r: float = 0.0, tp1_pct: float = 50.0) -> None:
        self.mt5 = connect(cfg)
        self.cfg = cfg
        self.clock = ServerClock(cfg.mt5_server_time)
        self._clock_checked = False
        self.tp1_r = tp1_r
        self.tp1_pct = tp1_pct
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
        # ... nor more than max_margin_pct of the free margin at the broker's own margin rate
        # (crypto / index CFDs often have much lower leverage than gold)
        cap = margin_lot_cap(self.mt5, sig.symbol, sig.direction == LONG, sig.entry, self.cfg.max_margin_pct)
        if cap is not None:
            lots = min(lots, cap)
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
        info = ensure_symbol(mt5, sig.symbol)
        if not self._clock_checked:
            self.clock.detect(mt5, [sig.symbol])
            self._clock_checked = True
        tick = mt5.symbol_info_tick(sig.symbol)
        if tick is None:
            raise RuntimeError(f"no tick for {sig.symbol} (market closed?)")
        long = sig.direction == LONG
        # never chase: if price already traded through the stop, the setup is dead
        if (long and tick.bid <= sig.sl) or (not long and tick.ask >= sig.sl):
            raise RuntimeError("price is already beyond the stop - setup invalidated")
        market_now = (tick.ask <= sig.entry) if long else (tick.bid >= sig.entry)
        tf_min = timeframe_minutes(sig.timeframe) if sig.timeframe else 15
        # expiration is in the broker's server clock; symbols without it get GTC and the
        # agent cancels the order itself after expiry_bars (see on_bar)
        specified = allows_specified_expiry(mt5, info)
        expires = server_time_now(mt5, sig.symbol, self.clock) + tf_min * 60 * sig.expiry_bars
        legs: list[tuple[float, float]] = [(float(qty), float(sig.tp))]
        acct = mt5.account_info()
        hedging = acct is not None and acct.margin_mode == getattr(mt5, "ACCOUNT_MARGIN_MODE_RETAIL_HEDGING", 2)
        if self.tp1_r > 0 and not hedging:
            log.warning("mt5: netting account - partial take-profit needs a hedging account; single target")
        if self.tp1_r > 0 and hedging and 0 < self.tp1_pct < 100 and self.tp1_r < sig.rr and info is not None:
            step = info.volume_step
            v1 = math.floor(qty * self.tp1_pct / 100.0 / step) * step
            v2 = round(qty - v1, 8)
            if v1 >= info.volume_min and v2 >= info.volume_min:
                tp1 = sig.entry + sig.direction * self.tp1_r * sig.risk
                legs = [(round(v1, 8), float(tp1)), (v2, float(sig.tp))]
            else:
                log.warning("mt5: %.2f lots cannot be split for a partial take-profit; single target", qty)
        tickets: list[int] = []
        for volume, target in legs:
            req: dict[str, Any] = {
                "symbol": sig.symbol,
                "volume": volume,
                "sl": float(sig.sl),
                "tp": target,
                "deviation": self.cfg.mt5_deviation,
                "magic": self.cfg.mt5_magic,
                "comment": f"smc {sig.model[:4]} {sig.grade}" + (" tp1" if len(legs) == 2 and not tickets else ""),
            }
            if market_now:  # price already at/through the entry: take it at market
                req.update(action=mt5.TRADE_ACTION_DEAL, type=mt5.ORDER_TYPE_BUY if long else mt5.ORDER_TYPE_SELL,
                           price=tick.ask if long else tick.bid, type_filling=filling_for(mt5, info))
            else:
                req.update(action=mt5.TRADE_ACTION_PENDING,
                           type=mt5.ORDER_TYPE_BUY_LIMIT if long else mt5.ORDER_TYPE_SELL_LIMIT,
                           price=float(sig.entry), type_filling=mt5.ORDER_FILLING_RETURN)
                if specified:
                    req.update(type_time=mt5.ORDER_TIME_SPECIFIED, expiration=int(expires))
                else:
                    req.update(type_time=mt5.ORDER_TIME_GTC)
            res = mt5.order_send(req)
            if res is None or res.retcode not in (mt5.TRADE_RETCODE_DONE, mt5.TRADE_RETCODE_PLACED):
                for tk in tickets:  # never leave half a trade behind
                    if market_now:
                        self._close_ticket(sig.symbol, tk, "second leg rejected")
                    else:
                        self._send({"action": mt5.TRADE_ACTION_REMOVE, "order": tk}, f"cancel {tk}")
                raise RuntimeError(f"MT5 order_send failed: {getattr(res, 'retcode', None)} {getattr(res, 'comment', '')}")
            tickets.append(res.order)
            log.info("mt5: %s %s %.2f lots @ %s sl %s tp %s ticket %s", "BUY" if long else "SELL",
                     sig.symbol, volume, req["price"], sig.sl, target, res.order)
        risk_money = qty * sig.risk / info.trade_tick_size * info.trade_tick_value if info else 0.0
        self.tracked[sig.symbol] = _Tracked(tickets, sig, risk_money, "open" if market_now else "pending",
                                            split=len(tickets) == 2)
        return ",".join(str(t) for t in tickets)

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

    def _close_ticket(self, symbol: str, ticket: int, reason: str) -> bool:
        mt5 = self.mt5
        for p in mt5.positions_get(ticket=ticket) or []:
            tick = mt5.symbol_info_tick(symbol)
            buy = p.type == mt5.POSITION_TYPE_BUY
            req = {
                "action": mt5.TRADE_ACTION_DEAL, "symbol": symbol, "volume": p.volume, "position": p.ticket,
                "type": mt5.ORDER_TYPE_SELL if buy else mt5.ORDER_TYPE_BUY,
                "price": tick.bid if buy else tick.ask, "deviation": self.cfg.mt5_deviation,
                "magic": self.cfg.mt5_magic, "comment": f"smc exit: {reason}"[:31],
                "type_filling": filling_for(mt5, mt5.symbol_info(symbol)),
            }
            return self._send(req, f"close {p.ticket}")
        return False

    def close_position(self, symbol: str, reason: str) -> list[dict[str, Any]]:
        events = []
        for p in self._positions(symbol):
            if self._close_ticket(symbol, p.ticket, reason):
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

    def _runner_to_entry(self, symbol: str) -> list[dict[str, Any]]:
        """After TP1: stop of the runner to its entry. If price is already back
        beyond the entry the broker would reject that stop, so the runner is
        closed at market instead (the simulator exits at the next open)."""
        mt5 = self.mt5
        tick = mt5.symbol_info_tick(symbol)
        for p in self._positions(symbol):
            buy = p.type == mt5.POSITION_TYPE_BUY
            if tick is not None and (tick.bid <= p.price_open if buy else tick.ask >= p.price_open):
                return self.close_position(symbol, "first target hit, price back at the entry")
        return self.protect(symbol, "first target hit")

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
        deals = [d for tk in tr.tickets for d in (self.mt5.history_deals_get(position=tk) or [])]
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
        live = [tk for tk in tr.tickets if mt5.positions_get(ticket=tk)]
        waiting = [tk for tk in tr.tickets if mt5.orders_get(ticket=tk)]
        if tr.status == "pending":
            if waiting and not live:
                sig = tr.signal
                if t - sig.bar > sig.expiry_bars:
                    return self.cancel_pending(symbol, "expired")
                reached_tp = bar.high >= sig.tp if sig.direction == LONG else bar.low <= sig.tp
                if reached_tp:
                    return self.cancel_pending(symbol, "missed: target traded before the fill")
                return []
            if live:
                tr.status = "open"
                events.append({"event": "filled", "symbol": symbol, "tickets": tr.tickets, "side": tr.signal.side})
            else:  # expired / removed without a fill, or filled and already closed
                deals = [d for tk in tr.tickets for d in (mt5.history_deals_get(position=tk) or [])]
                events.append(self._closed_result(tr) if deals else
                              {"event": "cancelled", "symbol": symbol, "tickets": tr.tickets, "reason": "expired"})
                del self.tracked[symbol]
                return events
        if tr.status == "open":
            if tr.split and not tr.tp1_done and tr.tickets[0] not in live and tr.tickets[1] in live:
                tr.tp1_done = True  # first leg took profit: the runner becomes risk-free
                events.append({"event": "partial", "symbol": symbol, "partial": self.tp1_pct / 100.0,
                               "side": tr.signal.side, "ticket": tr.tickets[0]})
                events += self._runner_to_entry(symbol)
                live = [tk for tk in tr.tickets if mt5.positions_get(ticket=tk)]
            if not live and not waiting:
                events.append(self._closed_result(tr))
                del self.tracked[symbol]
        return events

    def close(self) -> None:
        self.mt5.shutdown()
