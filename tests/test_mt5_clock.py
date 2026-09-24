"""Broker server clock -> UTC for MT5 candles (sessions and news depend on it)."""

import sys
import time
from datetime import datetime, timezone

import pandas as pd
import pytest

from smc_agent.config import BrokerConfig
from smc_agent.execution.mt5_common import ServerClock, ny7_offset_hours

from .fake_mt5 import FakeMT5, make_rates


def naive_epoch(s):
    return int(datetime.fromisoformat(s).replace(tzinfo=timezone.utc).timestamp())


def test_ny7_clock_follows_us_daylight_saving():
    clock = ServerClock("ny+7")
    idx = clock.to_utc([naive_epoch("2026-07-01T00:00:00"), naive_epoch("2026-01-05T00:00:00")])
    assert list(idx) == [pd.Timestamp("2026-06-30 21:00", tz="UTC"), pd.Timestamp("2026-01-04 22:00", tz="UTC")]
    # server midnight = 17:00 New York, summer and winter
    assert [t.tz_convert("America/New_York").hour for t in idx] == [17, 17]


def test_fixed_and_utc_clocks():
    assert ServerClock("+2").to_utc([naive_epoch("2026-07-01T02:00:00")])[0] == pd.Timestamp("2026-07-01", tz="UTC")
    assert ServerClock("utc").to_utc([naive_epoch("2026-07-01T02:00:00")])[0].hour == 2
    with pytest.raises(ValueError):
        ServerClock("GMT plus two")


@pytest.mark.parametrize("offset,expected", [(None, "New York"), (0, "UTC"), (9, "GMT+9")])
def test_auto_detection_from_a_fresh_tick(offset, expected):
    fake = FakeMT5(server_offset_h=ny7_offset_hours(time.time()) if offset is None else offset)
    assert expected in ServerClock("auto").detect(fake, ["XAUUSD"])


def test_auto_falls_back_to_ny7_when_the_market_is_closed():
    fake = FakeMT5()
    fake.server_now = lambda: int(time.time() - 2 * 86400)  # Friday's last tick
    clock = ServerClock("auto")
    assert "New York" in clock.detect(fake, ["XAUUSD"]) and not clock.detected


def test_mt5_feed_returns_utc_candles_and_spread_in_price(monkeypatch):
    fake = FakeMT5()
    fake.rates = make_rates(naive_epoch("2026-07-01T00:00:00"), 50)
    monkeypatch.setitem(sys.modules, "MetaTrader5", fake)
    from smc_agent.data.feeds import MT5Feed

    df = MT5Feed("XAUUSD", "15m", BrokerConfig(mt5_server_time="ny+7")).history(50)
    assert df.index[0] == pd.Timestamp("2026-06-30 21:00", tz="UTC")
    assert df["spread"].iloc[0] == pytest.approx(0.25)
    assert "XAUUSD" in fake.selected  # added to Market Watch
