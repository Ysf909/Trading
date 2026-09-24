"""MT5 broker against a fake terminal: the partial take-profit split, the runner
moved to entry after TP1, and never leaving half a trade behind."""

import sys
from datetime import datetime, timezone

import pytest

from smc_agent.config import BrokerConfig
from smc_agent.core.types import Bar, Signal

from .fake_mt5 import FakeMT5

T0 = datetime(2026, 6, 1, 14, tzinfo=timezone.utc)

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


def test_pending_expiry_is_in_server_time(monkeypatch):
    fake = FakeMT5(server_offset_h=3)
    br = make_broker(monkeypatch, fake)
    br.place(gold_sig(), 0.10)
    req = fake.requests[0]
    assert req["type_time"] == fake.ORDER_TIME_SPECIFIED
    assert abs(req["expiration"] - (fake.server_now() + 15 * 60 * 20)) <= 5


def test_gtc_fallback_and_own_expiry(monkeypatch):
    fake = FakeMT5()
    fake.expiration_flags = fake.SYMBOL_EXPIRATION_GTC
    br = make_broker(monkeypatch, fake)
    br.place(gold_sig(), 0.10)  # signal bar 10, valid 20 bars
    assert all(r["type_time"] == fake.ORDER_TIME_GTC and "expiration" not in r for r in fake.requests)
    assert br.on_bar("XAUUSD", bar(2004), 30) == []
    events = br.on_bar("XAUUSD", bar(2004), 31)
    assert [e["event"] for e in events] == ["cancelled", "cancelled"] and not fake.orders
    assert "XAUUSD" not in br.tracked


def test_market_entry_uses_a_filling_mode_the_symbol_allows(monkeypatch):
    fake = FakeMT5(hedging=False)
    fake.filling_flags = fake.SYMBOL_FILLING_FOK
    fake.bid, fake.ask = 1999.8, 2000.0  # already at the entry
    br = make_broker(monkeypatch, fake)
    br.place(gold_sig(), 0.10)
    assert fake.requests[0]["action"] == fake.TRADE_ACTION_DEAL
    assert fake.requests[0]["type_filling"] == fake.ORDER_FILLING_FOK


def test_unknown_symbol_suggests_the_broker_name(monkeypatch):
    fake = FakeMT5(symbols=("XAUUSD.m", "EURUSD"))
    br = make_broker(monkeypatch, fake)
    with pytest.raises(RuntimeError, match="XAUUSD.m"):
        br.place(gold_sig(), 0.10)


def test_login_details_are_passed_to_the_terminal(monkeypatch):
    fake = FakeMT5()
    monkeypatch.setitem(sys.modules, "MetaTrader5", fake)
    monkeypatch.setenv("MT5_PASSWORD", "secret")
    from smc_agent.execution.mt5_broker import MT5Broker

    MT5Broker(BrokerConfig(mt5_login=5012345, mt5_server="DemoBroker-Server", mt5_path="C:\\MT5\\terminal64.exe"))
    args, kwargs = fake.init_kwargs
    assert args == ("C:\\MT5\\terminal64.exe",)
    assert kwargs == {"login": 5012345, "password": "secret", "server": "DemoBroker-Server"}
