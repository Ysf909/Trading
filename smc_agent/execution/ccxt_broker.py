"""Live crypto execution through ccxt (Binance, Bybit, OKX, Bitget, ...).

Entries are resting limit orders with exchange-side stop-loss / take-profit
attached through ccxt's unified ``stopLoss`` / ``takeProfit`` parameters.
Always start with ``testnet: true``; exchanges differ in which order types and
parameters they accept, so verify behaviour on the testnet first.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from ..config import BrokerConfig
from ..core.types import LONG, Bar, Signal
from ..risk import AccountState
from .broker import Broker

log = logging.getLogger(__name__)


class CCXTBroker(Broker):
    name = "ccxt"

    def __init__(self, cfg: BrokerConfig) -> None:
        import ccxt  # optional dependency

        key = os.environ.get(cfg.api_key_env)
        secret = os.environ.get(cfg.api_secret_env)
        if not key or not secret:
            raise RuntimeError(f"set {cfg.api_key_env} and {cfg.api_secret_env} for live ccxt trading")
        self.cfg = cfg
        self.ex = getattr(ccxt, cfg.exchange)({
            "apiKey": key,
            "secret": secret,
            "enableRateLimit": True,
            "options": {"defaultType": cfg.market_type},
        })
        if cfg.testnet:
            self.ex.set_sandbox_mode(True)
        self.ex.load_markets()
        # symbol -> {"id", "signal", "status"}
        self.orders: dict[str, dict[str, Any]] = {}

    def equity(self) -> float:
        bal = self.ex.fetch_balance()
        return float((bal.get("total") or {}).get(self.cfg.quote_currency, 0.0) or 0.0)

    def _positions(self) -> set[str]:
        if self.ex.has.get("fetchPositions"):
            try:
                return {
                    p["symbol"].split(":")[0]
                    for p in self.ex.fetch_positions()
                    if abs(float(p.get("contracts") or 0.0)) > 0
                }
            except Exception as exc:  # noqa: BLE001 - exchange specific failures
                log.warning("fetch_positions failed: %s", exc)
        return {s for s, o in self.orders.items() if o["status"] == "filled"}

    def account_state(self) -> AccountState:
        return AccountState(
            equity=self.equity(),
            open_symbols=self._positions(),
            pending_symbols={s for s, o in self.orders.items() if o["status"] == "open"},
        )

    def place(self, sig: Signal, qty: float) -> str:
        sym = sig.symbol
        prev = self.orders.get(sym)
        if prev and prev["status"] == "open":
            self._cancel(sym)
        side = "buy" if sig.direction == LONG else "sell"
        amount = float(self.ex.amount_to_precision(sym, qty))
        price = float(self.ex.price_to_precision(sym, sig.entry))
        params = {
            "stopLoss": {"triggerPrice": float(self.ex.price_to_precision(sym, sig.sl))},
            "takeProfit": {"triggerPrice": float(self.ex.price_to_precision(sym, sig.tp))},
        }
        order = self.ex.create_order(sym, "limit", side, amount, price, params)
        self.orders[sym] = {"id": order["id"], "signal": sig, "status": "open"}
        log.info("ccxt: placed %s %s %s @ %s (sl %s tp %s) id=%s", side, amount, sym, price, sig.sl, sig.tp, order["id"])
        return str(order["id"])

    def _cancel(self, sym: str) -> None:
        o = self.orders.get(sym)
        if not o:
            return
        try:
            self.ex.cancel_order(o["id"], sym)
        except Exception as exc:  # noqa: BLE001
            log.warning("cancel %s failed: %s", o["id"], exc)
        o["status"] = "cancelled"

    def spread(self, symbol: str) -> float | None:
        try:
            tk = self.ex.fetch_ticker(symbol)
            if tk.get("ask") and tk.get("bid"):
                return float(tk["ask"]) - float(tk["bid"])
        except Exception as exc:  # noqa: BLE001
            log.warning("fetch_ticker failed: %s", exc)
        return None

    def _position(self, symbol: str) -> dict[str, Any] | None:
        if not self.ex.has.get("fetchPositions"):
            return None
        try:
            for p in self.ex.fetch_positions([symbol]):
                if abs(float(p.get("contracts") or 0.0)) > 0:
                    return p
        except Exception as exc:  # noqa: BLE001
            log.warning("fetch_positions failed: %s", exc)
        return None

    def position_info(self, symbol: str) -> dict[str, Any] | None:
        p = self._position(symbol)
        if p is not None:
            sig = self.orders.get(symbol, {}).get("signal")
            return {"status": "open", "direction": 1 if p.get("side") == "long" else -1,
                    "fill_price": float(p.get("entryPrice") or (sig.entry if sig else 0.0)), "be_moved": False}
        o = self.orders.get(symbol)
        if o and o["status"] == "open":
            return {"status": "pending", "direction": o["signal"].direction, "fill_price": 0.0, "be_moved": False}
        return None

    def cancel_pending(self, symbol: str, reason: str) -> list[dict[str, Any]]:
        o = self.orders.get(symbol)
        if not o or o["status"] != "open":
            return []
        self._cancel(symbol)
        return [{"event": "cancelled", "symbol": symbol, "id": o["id"], "reason": reason}]

    def close_position(self, symbol: str, reason: str) -> list[dict[str, Any]]:
        p = self._position(symbol)
        if p is None:
            return []
        side = "sell" if p.get("side") == "long" else "buy"
        amount = abs(float(p.get("contracts") or 0.0))
        self.ex.create_order(symbol, "market", side, amount, None, {"reduceOnly": True})
        try:
            self.ex.cancel_all_orders(symbol)  # attached stop / target
        except Exception as exc:  # noqa: BLE001
            log.warning("cancel_all_orders failed: %s", exc)
        return [{"event": "guard_close", "symbol": symbol, "reason": reason}]

    def protect(self, symbol: str, reason: str) -> list[dict[str, Any]]:
        log.warning("ccxt: moving an attached stop is exchange specific; %s left unchanged (%s)", symbol, reason)
        return []

    def on_bar(self, symbol: str, bar: Bar, t: int) -> list[dict[str, Any]]:
        o = self.orders.get(symbol)
        if not o or o["status"] != "open":
            return []
        sig: Signal = o["signal"]
        try:
            info = self.ex.fetch_order(o["id"], symbol)
        except Exception as exc:  # noqa: BLE001
            log.warning("fetch_order failed: %s", exc)
            return []
        if info.get("status") == "closed":
            o["status"] = "filled"
            return [{"event": "filled", "id": sig.id, "symbol": symbol, "price": info.get("average") or sig.entry}]
        if info.get("status") in ("canceled", "cancelled", "expired", "rejected"):
            o["status"] = "cancelled"
            return [{"event": "cancelled", "id": sig.id, "symbol": symbol, "reason": info.get("status")}]
        reached_tp = bar.high >= sig.tp if sig.direction == LONG else bar.low <= sig.tp
        if t - sig.bar > sig.expiry_bars or reached_tp:
            self._cancel(symbol)
            return [{"event": "cancelled", "id": sig.id, "symbol": symbol,
                     "reason": "missed" if reached_tp else "expired"}]
        return []
