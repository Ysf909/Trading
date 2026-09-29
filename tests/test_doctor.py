"""`smc-agent check` against a fake MT5 terminal."""

import time

import pytest

from smc_agent.config import AppConfig, BrokerConfig, MarketConfig, StrategyConfig
from smc_agent.doctor import Doctor
from smc_agent.execution.mt5_common import ny7_offset_hours

from .fake_mt5 import FakeMT5, make_rates


def gold_cfg(tmp_path, **broker):
    return AppConfig(markets=[MarketConfig(symbol="XAUUSD", timeframe="15m", feed="mt5")],
                     strategy=StrategyConfig(tp1_r=1.5, tp1_pct=50),
                     broker=BrokerConfig(kind="mt5", **broker), journal_path=str(tmp_path / "state" / "j.jsonl"),
                     warmup_bars=2000)


def fake_terminal(**kw):
    fake = FakeMT5(server_offset_h=ny7_offset_hours(time.time()), **kw)
    fake.rates = make_rates(fake.server_now() - 3000 * 900, 3000)
    return fake


def run(cfg, fake):
    doc = Doctor(cfg, connect=lambda b: fake)
    doc.run()
    return doc, {c.title: c for c in doc.results}


def statuses(doc, word):
    return [c.status for c in doc.results if word in c.title]


def test_healthy_demo_setup_passes(tmp_path):
    doc, _ = run(gold_cfg(tmp_path), fake_terminal())
    assert not doc.failed, doc.report()
    report = doc.report()
    assert "DEMO account" in report and "Algo Trading is on" in report
    assert "Hedging account" in report and "New York + 7 h" in report
    assert "a typical trade is" in report and "3000 candles" not in report  # warmup 2000 read
    assert "2000 candles of history" in report


def test_algo_trading_off_and_python_api_disabled_fail(tmp_path):
    fake = fake_terminal()
    fake.algo_trading = False
    fake.api_disabled = True
    doc, _ = run(gold_cfg(tmp_path), fake)
    assert doc.failed
    report = doc.report()
    assert "Algo Trading' button" in report and "trading via external Python API" in report


def test_wrong_symbol_name_suggests_the_broker_symbol(tmp_path):
    doc, _ = run(gold_cfg(tmp_path), fake_terminal(symbols=("XAUUSD.m", "EURUSD")))
    assert doc.failed and "XAUUSD.m" in doc.report()


def test_account_too_small_for_the_minimum_lot(tmp_path):
    fake = fake_terminal()
    fake.account_info = lambda: FakeMT5.account_info(fake).__class__(
        **{**vars(FakeMT5.account_info(fake)), "equity": 100.0})
    doc, _ = run(gold_cfg(tmp_path), fake)
    assert doc.failed and "smallest lot" in doc.report()


def test_netting_account_and_real_money_warn(tmp_path):
    fake = fake_terminal(hedging=False)
    fake.trade_mode = fake.ACCOUNT_TRADE_MODE_REAL
    doc, _ = run(gold_cfg(tmp_path), fake)
    assert statuses(doc, "Netting account") == ["WARN"]
    assert statuses(doc, "REAL-money") == ["WARN"]


def test_short_history_and_terminal_down(tmp_path):
    fake = fake_terminal()
    fake.rates = fake.rates[-500:]
    doc, _ = run(gold_cfg(tmp_path), fake)
    assert "Max bars in chart" in doc.report() and doc.failed

    def down(b):
        raise RuntimeError("MT5 initialize() failed: (-10005, 'IPC timeout') - open the MetaTrader 5 terminal")

    doc = Doctor(gold_cfg(tmp_path), connect=down)
    doc.run()
    assert doc.failed and "open the MetaTrader 5 terminal" in doc.report()


def test_paper_config_needs_no_terminal(tmp_path):
    cfg = AppConfig(markets=[MarketConfig(symbol="XAUUSD", timeframe="15m", feed="csv", csv_path="missing.csv")],
                    journal_path=str(tmp_path / "j.jsonl"))
    doc = Doctor(cfg, connect=lambda b: pytest.fail("must not connect"))
    doc.run()
    assert doc.failed and "missing.csv" in doc.report()


def test_connection_test_places_and_cancels_a_far_away_order(tmp_path):
    fake = fake_terminal()
    doc = Doctor(gold_cfg(tmp_path), connect=lambda b: fake, test_order=True)
    doc.run()
    assert not doc.failed, doc.report()
    assert "Connection test PASSED" in doc.report()
    sent = [r for r in fake.requests if r["action"] == fake.TRADE_ACTION_PENDING]
    assert len(sent) == 1 and sent[0]["volume"] == 0.01
    assert sent[0]["price"] <= fake.bid * 0.9 + 0.01  # far below the market: can't fill
    assert sent[0]["sl"] < sent[0]["price"] < sent[0]["tp"]
    assert not fake.orders and not fake.positions  # cancelled, nothing opened


def test_connection_test_explains_a_refusal(tmp_path):
    fake = fake_terminal()
    doc = Doctor(gold_cfg(tmp_path), connect=lambda b: fake, test_order=True)
    fake.algo_trading = True
    real_send = fake.order_send
    fake.order_send = lambda req: (type("R", (), {"retcode": 10017, "order": 0, "comment": "Trade disabled"})()
                                   if req["action"] == fake.TRADE_ACTION_PENDING else real_send(req))
    doc.run()
    assert doc.failed and "trading is disabled for this account" in doc.report()


def test_connection_test_skipped_when_setup_is_broken(tmp_path):
    fake = fake_terminal()
    fake.algo_trading = False
    doc = Doctor(gold_cfg(tmp_path), connect=lambda b: fake, test_order=True)
    doc.run()
    assert "Connection test skipped" in doc.report() and not fake.requests


def test_paper_mode_on_mt5_prices_is_flagged(tmp_path):
    cfg = gold_cfg(tmp_path)
    cfg.broker.kind = "paper"
    doc, _ = run(cfg, fake_terminal())
    assert any(c.status == "WARN" and "PAPER mode" in c.title for c in doc.results)


def test_broker_announces_the_account(monkeypatch, caplog):
    import logging
    import sys

    fake = fake_terminal()
    fake.algo_trading = False
    monkeypatch.setitem(sys.modules, "MetaTrader5", fake)
    from smc_agent.execution.mt5_broker import MT5Broker

    with caplog.at_level(logging.INFO):
        MT5Broker(BrokerConfig(kind="mt5"))
    text = caplog.text
    assert "orders go to DEMO account 5012345 on DemoBroker-Server" in text
    assert "ALGO TRADING IS OFF" in text
