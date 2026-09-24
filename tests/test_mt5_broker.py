"""MT5 broker against a fake terminal: the partial take-profit split, the runner
moved to entry after TP1, and never leaving half a trade behind."""

import sys
from datetime import datetime, timezone
from types import SimpleNamespace as NS

import pytest

from smc_agent.config import BrokerConfig
from smc_agent.core.types import Bar, Signal

T0 = datetime(2026, 6, 1, 14, tzinfo=timezone.utc)
MAGIC = BrokerConfig().mt5_magic


class FakeMT5:
    TRADE_ACTION_DEAL, TRADE_ACTION_PENDING, TRADE_ACTION_SLTP, TRADE_ACTION_REMOVE = 1, 5, 6, 8
    ORDER_TYPE_BUY, ORDER_TYPE_SELL, ORDER_TYPE_BUY_LIMIT, ORDER_TYPE_SELL_LIMIT = 0, 1, 2, 3
    POSITION_TYPE_BUY, POSITION_TYPE_SELL = 0, 1
    ORDER_TIME_SPECIFIED, ORDER_FILLING_IOC, ORDER_FILLING_RETURN = 2, 1, 2
    TRADE_RETCODE_PLACED, TRADE_RETCODE_DONE, TRADE_RETCODE_INVALID_STOPS = 10008, 10009, 10016
    ACCOUNT_MARGIN_MODE_RETAIL_NETTING, ACCOUNT_MARGIN_MODE_RETAIL_HEDGING = 0, 2

    def __init__(self, hedging=True, reject_after=None):
        self.margin_mode = 2 if hedging else 0
        self.bid, self.ask = 2003.0, 2003.2
        self.orders, self.positions, self.deals, self.requests = {}, {}, [], []
        self.next = 100
        self.opened = 0
        self.reject_after = reject_after  # reject new orders after this many (closes still work)

    def initialize(self):
        return True

    def shutdown(self):
        pass

    def account_info(self):
        return NS(equity=10_000.0, margin_mode=self.margin_mode)

    def symbol_info(self, symbol):
        return NS(trade_tick_size=0.01, trade_tick_value=1.0, trade_contract_size=100.0,
                  volume_step=0.01, volume_min=0.01, volume_max=100.0)

    def symbol_info_tick(self, symbol):
        return NS(bid=self.bid, ask=self.ask)

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


def make_broker(monkeypatch, fake, tp1_r=1.5, tp1_pct=50):
    monkeypatch.setitem(sys.modules, "MetaTrader5", fake)
    from smc_agent.execution.mt5_broker import MT5Broker

    return MT5Broker(BrokerConfig(), tp1_r=tp1_r, tp1_pct=tp1_pct)


def gold_sig(entry=2000.0, sl=1995.0, tp=2015.0):
    return Signal("g1", "XAUUSD", "15m", T0, 10, 1, "reversal", entry, sl, tp, (tp - entry) / (entry - sl),
                  1.0, 5.0, 20, 7, "A", {}, [])


def bar(c):
    return Bar(T0, c, c + 1, c - 1, c)


def test_hedging_account_splits_into_tp1_and_runner(monkeypatch):
    fake = FakeMT5()
    br = make_broker(monkeypatch, fake)
    br.place(gold_sig(), 0.10)
    legs = sorted(fake.orders.values(), key=lambda o: o.ticket)
    assert [o.volume for o in legs] == [0.05, 0.05]
    assert [o.tp for o in legs] == [2007.5, 2015.0]  # TP1 = entry + 1.5R
    assert all(o.sl == 1995.0 and o.price_open == 2000.0 for o in legs)
    assert br.tracked["XAUUSD"].split


def test_netting_account_uses_a_single_target(monkeypatch):
    fake = FakeMT5(hedging=False)
    br = make_broker(monkeypatch, fake)
    br.place(gold_sig(), 0.10)
    assert [(o.volume, o.tp) for o in fake.orders.values()] == [(0.10, 2015.0)]


def test_tp1_moves_the_runner_stop_to_entry(monkeypatch):
    fake = FakeMT5()
    br = make_broker(monkeypatch, fake)
    br.place(gold_sig(), 0.10)
    t1, t2 = sorted(fake.orders)
    fake.fill(t1)
    fake.fill(t2)
    assert [e["event"] for e in br.on_bar("XAUUSD", bar(2002), 11)] == ["filled"]
    fake.close(t1, 2007.5)  # TP1 leg hits its target
    fake.bid, fake.ask = 2006.0, 2006.2
    events = br.on_bar("XAUUSD", bar(2006), 12)
    assert [e["event"] for e in events] == ["partial", "protected"]
    assert fake.positions[t2].sl == 2000.0 and fake.positions[t2].tp == 2015.0
    fake.close(t2, 2000.0)  # runner stopped at entry
    closed = br.on_bar("XAUUSD", bar(2000), 13)
    assert closed[-1]["event"] == "closed"
    assert closed[-1]["r"] == pytest.approx(0.05 * 7.5 * 100 / (0.10 * 5.0 * 100))  # +0.75R
    assert "XAUUSD" not in br.tracked


def test_runner_closed_when_price_is_already_back_through_entry(monkeypatch):
    fake = FakeMT5()
    br = make_broker(monkeypatch, fake)
    br.place(gold_sig(), 0.10)
    t1, t2 = sorted(fake.orders)
    fake.fill(t1)
    fake.fill(t2)
    br.on_bar("XAUUSD", bar(2002), 11)
    fake.close(t1, 2007.5)
    fake.bid, fake.ask = 1999.0, 1999.2  # the stop at entry would be rejected
    events = br.on_bar("XAUUSD", bar(1999), 12)
    assert [e["event"] for e in events] == ["partial", "guard_close", "closed"]
    assert not fake.positions and "XAUUSD" not in br.tracked
    assert events[-1]["r"] > 0  # +0.75R banked, runner -0.1R


def test_rejected_second_leg_cancels_the_first(monkeypatch):
    fake = FakeMT5(reject_after=1)
    br = make_broker(monkeypatch, fake)
    with pytest.raises(RuntimeError):
        br.place(gold_sig(), 0.10)
    assert not fake.orders and "XAUUSD" not in br.tracked


def test_rejected_second_leg_at_market_closes_the_first(monkeypatch):
    fake = FakeMT5(reject_after=1)
    fake.bid, fake.ask = 1999.8, 2000.0  # already at the entry: market orders
    br = make_broker(monkeypatch, fake)
    with pytest.raises(RuntimeError):
        br.place(gold_sig(), 0.10)
    assert not fake.positions
