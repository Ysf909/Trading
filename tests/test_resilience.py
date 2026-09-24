"""Unexpected-scenario tests for the live agent (no network, no real broker)."""

from datetime import datetime, timezone

import numpy as np
import pandas as pd

from smc_agent.config import AppConfig, GuardConfig, MarketConfig, StrategyConfig
from smc_agent.data.feeds import normalize
from smc_agent.execution.broker import PaperBroker
from smc_agent.live import TradingAgent
from smc_agent.news import NewsCalendar

from .conftest import BULL_REVERSAL, frame
from .test_agent import FrameFeed, make_sig


def agent(tmp_path, df, guard=None, broker=None, notifier=None):
    cfg = AppConfig(markets=[MarketConfig(symbol="XAUUSD", timeframe="15m", feed="csv", tv_symbol="XAUUSD")],
                    strategy=StrategyConfig(internal_len=2, swing_len=3, atr_len=3, fvg_min_atr=0.0, min_score=0,
                                            min_risk_atr=0.1, max_risk_atr=10, htf_minutes=60, min_rr=1.0),
                    guard=guard or GuardConfig(enabled=False), journal_path=str(tmp_path / "j.jsonl"))
    feed = FrameFeed(df)
    a = TradingAgent(cfg, broker=broker or PaperBroker(10_000), feeds={"XAUUSD": feed},
                     calendar=NewsCalendar([], available=False), notifier=notifier)
    return a, feed


class Collect:
    def __init__(self):
        self.msgs = []

    def send(self, text):
        self.msgs.append(text)


def test_bad_ticks_are_cleaned():
    idx = pd.date_range("2026-01-05", periods=4, freq="15min", tz="UTC")
    df = pd.DataFrame({"open": [10, 10, 0, 10], "high": [11, 9, 11, np.nan], "low": [9, 9.5, 9, 9],
                       "close": [10.5, 10.2, 10, 10]}, index=idx)
    out = normalize(df)
    assert len(out) == 2  # zero price and NaN rows dropped
    assert (out["high"] >= out[["open", "close"]].max(axis=1)).all()  # high < open repaired


def test_stale_feed_blocks_new_entries(tmp_path):
    notes = Collect()
    a, feed = agent(tmp_path, frame(BULL_REVERSAL), guard=GuardConfig(stale_bars=3, mtf_timeframes=[]),
                    notifier=notes)
    feed.n = 10
    a.warmup()
    decisions = []
    for n in range(11, len(feed.df) + 1):
        feed.n = n
        decisions += a.poll_market(a.markets[0])
    assert a.markets[0].stale
    assert all(not d["taken"] for d in decisions)
    assert any("stale" in m for m in notes.msgs)


def test_kill_switch_flattens_and_halts(tmp_path):
    a, feed = agent(tmp_path, frame(BULL_REVERSAL))
    feed.n = 10
    a.warmup()
    for n in range(11, 19):  # position opens and fills on the retrace
        feed.n = n
        a.poll_market(a.markets[0])
    assert a.broker.account_state().open_symbols == {"XAUUSD"}
    a.flatten_file.parent.mkdir(parents=True, exist_ok=True)
    a.flatten_file.touch()
    a.check_kill_switch()
    assert a.broker.account_state().open_symbols == set()
    assert a.halted() and not a.flatten_file.exists()
    res = a.handle_signal(make_sig("XAUUSD"))
    assert not res["taken"] and "kill switch" in res["reason"]


def test_broker_failure_does_not_crash(tmp_path):
    class Broken(PaperBroker):
        def place(self, sig, qty):
            raise ConnectionError("terminal disconnected")

    a, _ = agent(tmp_path, frame(BULL_REVERSAL), broker=Broken(10_000))
    res = a.handle_signal(make_sig("XAUUSD"))
    assert not res["taken"] and "terminal disconnected" in res["reason"]


def test_paper_broker_guard_actions():
    br = PaperBroker(10_000, commission_pct=0.0)
    s = make_sig("XAUUSD")
    br.place(s, 1.0)
    assert br.position_info("XAUUSD")["status"] == "pending"
    assert br.cancel_pending("XAUUSD", "news: NFP")[0]["exit_reason"] == "news: NFP"
    br.place(s, 1.0)
    from smc_agent.core.types import Bar
    br.on_bar("XAUUSD", Bar(datetime(2026, 1, 6, tzinfo=timezone.utc), 100.5, 100.8, 99.9, 100.6), 11)
    assert br.position_info("XAUUSD")["status"] == "open"
    assert br.protect("XAUUSD", "news")[0]["sl"] == 100.0
    ev = br.close_position("XAUUSD", "weekend: flat")
    assert ev[0]["exit_reason"] == "weekend: flat" and ev[0]["r"] > 0
    assert br.position_info("XAUUSD") is None
