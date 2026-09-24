"""A fake MetaTrader5 module: enough of the terminal API for the MT5 feed,
broker and ``smc-agent check``. Constant values match the real package."""

import time
from types import SimpleNamespace as NS

import numpy as np

RATE_DTYPE = [("time", "<i8"), ("open", "<f8"), ("high", "<f8"), ("low", "<f8"), ("close", "<f8"),
              ("tick_volume", "<u8"), ("spread", "<i4"), ("real_volume", "<u8")]


class FakeMT5:
    TRADE_ACTION_DEAL, TRADE_ACTION_PENDING, TRADE_ACTION_SLTP, TRADE_ACTION_REMOVE = 1, 5, 6, 8
    ORDER_TYPE_BUY, ORDER_TYPE_SELL, ORDER_TYPE_BUY_LIMIT, ORDER_TYPE_SELL_LIMIT = 0, 1, 2, 3
    POSITION_TYPE_BUY, POSITION_TYPE_SELL = 0, 1
    ORDER_TIME_GTC, ORDER_TIME_DAY, ORDER_TIME_SPECIFIED = 0, 1, 2
    ORDER_FILLING_FOK, ORDER_FILLING_IOC, ORDER_FILLING_RETURN = 0, 1, 2
    SYMBOL_FILLING_FOK, SYMBOL_FILLING_IOC = 1, 2
    SYMBOL_EXPIRATION_GTC, SYMBOL_EXPIRATION_DAY, SYMBOL_EXPIRATION_SPECIFIED = 1, 2, 4
    SYMBOL_TRADE_MODE_DISABLED, SYMBOL_TRADE_MODE_CLOSEONLY, SYMBOL_TRADE_MODE_FULL = 0, 3, 4
    ACCOUNT_TRADE_MODE_DEMO, ACCOUNT_TRADE_MODE_REAL = 0, 2
    TRADE_RETCODE_PLACED, TRADE_RETCODE_DONE, TRADE_RETCODE_INVALID_STOPS = 10008, 10009, 10016
    ACCOUNT_MARGIN_MODE_RETAIL_NETTING, ACCOUNT_MARGIN_MODE_RETAIL_HEDGING = 0, 2
    TIMEFRAME_M1, TIMEFRAME_M5, TIMEFRAME_M15, TIMEFRAME_M30 = 1, 5, 15, 30
    TIMEFRAME_H1, TIMEFRAME_H4, TIMEFRAME_D1 = 16385, 16388, 16408

    def __init__(self, hedging=True, reject_after=None, server_offset_h=3.0, symbols=("XAUUSD", "XAGUSD", "EURUSD")):
        self.margin_mode = 2 if hedging else 0
        self.bid, self.ask = 2003.0, 2003.2
        self.orders, self.positions, self.deals, self.requests = {}, {}, [], []
        self.next = 100
        self.opened = 0
        self.reject_after = reject_after  # reject new orders after this many (closes still work)
        self.server_offset_h = server_offset_h
        self.symbols = list(symbols)
        self.selected = set()
        self.initialized = True
        self.algo_trading = True
        self.api_disabled = False
        self.trade_mode = 0  # demo
        self.filling_flags = 2  # IOC
        self.expiration_flags = 1 | 2 | 4 | 8
        self.rates = None  # numpy structured array returned by copy_rates_from_pos
        self.init_kwargs = None

    # ------------------------------------------------------------ terminal
    def initialize(self, *args, **kwargs):
        self.init_kwargs = (args, kwargs)
        return self.initialized

    def shutdown(self):
        pass

    def last_error(self):
        return (-10005, "IPC timeout") if not self.initialized else (1, "Success")

    def terminal_info(self):
        return NS(connected=True, trade_allowed=self.algo_trading, tradeapi_disabled=self.api_disabled,
                  name="MetaTrader 5", company="Demo Broker Ltd", build=4755, path="C:\\MT5")

    def account_info(self):
        return NS(equity=10_000.0, balance=10_000.0, margin_mode=self.margin_mode, login=5012345,
                  server="DemoBroker-Server", company="Demo Broker Ltd", currency="USD", leverage=100,
                  trade_mode=self.trade_mode, trade_allowed=True, trade_expert=True, name="Test")

    def symbols_get(self, group=None):
        return [NS(name=n) for n in self.symbols + ["GOLD", "XAUUSD.m", "USDJPY"]]

    def symbol_select(self, symbol, enable=True):
        self.selected.add(symbol)
        return True

    def symbol_info(self, symbol):
        if symbol not in self.symbols:
            return None
        return NS(name=symbol, visible=symbol in self.selected, trade_mode=4, point=0.01, digits=2,
                  trade_tick_size=0.01, trade_tick_value=1.0, trade_contract_size=100.0,
                  volume_step=0.01, volume_min=0.01, volume_max=100.0, trade_stops_level=0,
                  filling_mode=self.filling_flags, expiration_mode=self.expiration_flags, spread=20)

    def server_now(self):
        return int(time.time() + self.server_offset_h * 3600)

    def symbol_info_tick(self, symbol):
        if symbol not in self.symbols:
            return None
        return NS(bid=self.bid, ask=self.ask, time=self.server_now())

    def copy_rates_from_pos(self, symbol, timeframe, start, count):
        if self.rates is None or symbol not in self.symbols:
            return None
        return self.rates[-count:]

    # ------------------------------------------------------------- trading
    def order_send(self, req):
        self.requests.append(req)
        a = req["action"]
        if a == self.TRADE_ACTION_PENDING or (a == self.TRADE_ACTION_DEAL and "position" not in req):
            self.opened += 1
            if self.reject_after is not None and self.opened > self.reject_after:
                return NS(retcode=10013, order=0, comment="rejected")
        if a == self.TRADE_ACTION_PENDING:
            self.next += 1
            self.orders[self.next] = NS(ticket=self.next, symbol=req["symbol"], type=req["type"], magic=req["magic"],
                                        volume=req["volume"], price_open=req["price"], sl=req["sl"], tp=req["tp"])
            return NS(retcode=self.TRADE_RETCODE_PLACED, order=self.next, comment="")
        if a == self.TRADE_ACTION_DEAL and "position" in req:
            self.close(req["position"], req["price"])
            return NS(retcode=self.TRADE_RETCODE_DONE, order=0, comment="")
        if a == self.TRADE_ACTION_DEAL:
            self.next += 1
            self.positions[self.next] = NS(ticket=self.next, symbol=req["symbol"], magic=req["magic"],
                                           type=0 if req["type"] == self.ORDER_TYPE_BUY else 1,
                                           volume=req["volume"], price_open=req["price"], sl=req["sl"], tp=req["tp"])
            return NS(retcode=self.TRADE_RETCODE_DONE, order=self.next, comment="")
        if a == self.TRADE_ACTION_SLTP:
            p = self.positions[req["position"]]
            buy = p.type == 0
            if (buy and req["sl"] >= self.bid) or (not buy and req["sl"] <= self.ask):
                return NS(retcode=self.TRADE_RETCODE_INVALID_STOPS, order=0, comment="invalid stops")
            p.sl, p.tp = req["sl"], req["tp"]
            return NS(retcode=self.TRADE_RETCODE_DONE, order=0, comment="")
        if a == self.TRADE_ACTION_REMOVE:
            self.orders.pop(req["order"], None)
            return NS(retcode=self.TRADE_RETCODE_DONE, order=0, comment="")
        raise AssertionError(req)

    # terminal-side events
    def fill(self, tk):
        o = self.orders.pop(tk)
        self.positions[tk] = NS(ticket=tk, symbol=o.symbol, magic=o.magic, volume=o.volume,
                                type=0 if o.type == self.ORDER_TYPE_BUY_LIMIT else 1,
                                price_open=o.price_open, sl=o.sl, tp=o.tp)

    def close(self, tk, price):
        p = self.positions.pop(tk)
        d = 1 if p.type == 0 else -1
        self.deals.append(NS(position=tk, profit=(price - p.price_open) * d * p.volume * 100, commission=0.0, swap=0.0))

    # queries
    def positions_get(self, symbol=None, ticket=None):
        return [p for p in self.positions.values()
                if (symbol is None or p.symbol == symbol) and (ticket is None or p.ticket == ticket)]

    def orders_get(self, symbol=None, ticket=None):
        return [o for o in self.orders.values()
                if (symbol is None or o.symbol == symbol) and (ticket is None or o.ticket == ticket)]

    def history_deals_get(self, position=None):
        return [d for d in self.deals if d.position == position]


def make_rates(server_start: int, n: int, minutes: int = 15, price: float = 2000.0) -> np.ndarray:
    """``n`` candles starting at ``server_start`` (server-clock epoch seconds)."""
    rng = np.random.default_rng(1)
    close = price + np.cumsum(rng.normal(0, 1.0, n))
    open_ = np.r_[price, close[:-1]]
    rows = np.zeros(n, dtype=RATE_DTYPE)
    rows["time"] = server_start + np.arange(n) * minutes * 60
    rows["open"], rows["close"] = open_, close
    rows["high"] = np.maximum(open_, close) + 0.5
    rows["low"] = np.minimum(open_, close) - 0.5
    rows["tick_volume"] = 100
    rows["spread"] = 25
    return rows
