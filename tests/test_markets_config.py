"""Several markets in one config (XAUUSD + BTCUSD at an MT5 broker)."""

import sys
import time

import pytest

from smc_agent.cli import main
from smc_agent.config import AppConfig, BrokerConfig, MarketConfig, guard_for, load_config
from smc_agent.core.types import Signal
from smc_agent.doctor import Doctor
from smc_agent.execution.mt5_common import ny7_offset_hours

from .fake_mt5 import FakeMT5, make_rates

TWO_MARKETS = """
markets:
  - symbol: XAUUSD_
    timeframe: 15m
    feed: csv
    csv_path: x.csv
  - symbol: BTCUSD_
    timeframe: 15m
    feed: csv
    csv_path: x.csv
    guard:
      market_hours: 24/7
      max_spread: 0
guard:
  max_spread: 0.6
"""


def write(tmp_path, text):
    p = tmp_path / "config.yaml"
    p.write_text(text)
    return p


def test_several_symbols_in_one_line_is_explained(tmp_path):
    p = write(tmp_path, "markets:\n  - symbol: XAUUSD,BTCUSD,XAUUSD_\n    feed: mt5\n")
    with pytest.raises(ValueError) as exc:
        load_config(p)
    msg = str(exc.value)
    assert "own entry" in msg and "  - symbol: BTCUSD\n" in msg and "  - symbol: XAUUSD_\n" in msg


def test_cli_prints_config_problems_without_a_traceback(tmp_path, capsys):
    p = write(tmp_path, "markets:\n  - symbol: XAUUSD,BTCUSD\n")
    with pytest.raises(SystemExit) as exc:
        main(["-c", str(p), "check"])
    assert exc.value.code == 2
    assert "Give each market its own entry" in capsys.readouterr().err


def test_per_market_guard_overrides(tmp_path):
    cfg = load_config(write(tmp_path, TWO_MARKETS))
    gold, btc = guard_for(cfg, "XAUUSD_"), guard_for(cfg, cfg.markets[1])
    assert gold.market_hours == "forex" and gold.max_spread == 0.6
    assert btc.market_hours == "24x7" and btc.max_spread == 0
    assert btc.news == gold.news  # everything else comes from guard:


def test_unknown_market_guard_key_and_duplicates_fail(tmp_path):
    with pytest.raises(ValueError, match="max_sprad"):
        load_config(write(tmp_path, "markets:\n  - symbol: BTCUSD\n    guard: {max_sprad: 0}\n"))
    with pytest.raises(ValueError, match="twice"):
        load_config(write(tmp_path, "markets:\n  - symbol: BTCUSD\n  - symbol: BTCUSD\n"))


def test_live_agent_builds_each_market_with_its_own_guard(tmp_path):
    from smc_agent.execution.broker import PaperBroker
    from smc_agent.live import TradingAgent
    from smc_agent.news import NewsCalendar

    from .conftest import BULL_REVERSAL, frame
    from .test_agent import FrameFeed

    cfg = load_config(write(tmp_path, TWO_MARKETS))
    cfg.journal_path = str(tmp_path / "j.jsonl")
    feeds = {"XAUUSD_": FrameFeed(frame(BULL_REVERSAL)), "BTCUSD_": FrameFeed(frame(BULL_REVERSAL))}
    a = TradingAgent(cfg, broker=PaperBroker(10_000), feeds=feeds, calendar=NewsCalendar([], available=False))
    assert [m.guard.cfg.market_hours for m in a.markets] == ["forex", "24x7"]


def test_mt5_size_is_capped_by_the_brokers_margin(monkeypatch):
    fake = FakeMT5(symbols=("XAUUSD", "BTCUSD"))
    fake.leverage["BTCUSD"] = 2.0  # crypto: 50% margin
    monkeypatch.setitem(sys.modules, "MetaTrader5", fake)
    from datetime import datetime, timezone

    from smc_agent.execution.mt5_broker import MT5Broker

    br = MT5Broker(BrokerConfig(max_margin_pct=50, max_leverage=0))
    sig = Signal("b", "BTCUSD", "15m", datetime(2026, 6, 1, tzinfo=timezone.utc), 1, 1, "reversal",
                 60_000.0, 59_990.0, 60_030.0, 3.0, 1.0, 10.0, 20, 7, "A", {}, [])
    lots = br.size(sig, 50.0)  # risk alone would allow 0.05 lots (10 x 100 per lot)
    # margin per lot = 60,000 x 100 / 2 = 3,000,000 -> 50% of 10,000 free margin buys 0.0016 lots -> 0
    assert lots == 0.0
    fake.leverage["BTCUSD"] = 20_000.0
    assert br.size(sig, 50.0) == pytest.approx(0.05)


def test_check_warns_when_crypto_uses_forex_hours(tmp_path):
    fake = FakeMT5(server_offset_h=ny7_offset_hours(time.time()), symbols=("XAUUSD", "BTCUSD"))
    fake.rates = make_rates(fake.server_now() - 3000 * 900, 3000)
    cfg = AppConfig(markets=[MarketConfig(symbol="BTCUSD", timeframe="15m", feed="mt5")],
                    broker=BrokerConfig(kind="mt5"), journal_path=str(tmp_path / "j.jsonl"), warmup_bars=2000)
    doc = Doctor(cfg, connect=lambda b: fake)
    doc.run()
    assert any(c.status == "WARN" and "looks like crypto" in c.title for c in doc.results)
    cfg.markets[0].guard = {"market_hours": "24x7", "max_spread": 0}
    doc = Doctor(cfg, connect=lambda b: fake)
    doc.run()
    assert not any("looks like crypto" in c.title for c in doc.results)
    assert any("own guard settings" in c.title for c in doc.results)
