from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from smc_agent.config import StrategyConfig


def frame(rows, start="2024-01-02 08:00", freq="15min") -> pd.DataFrame:
    idx = pd.date_range(start, periods=len(rows), freq=freq, tz="UTC")
    df = pd.DataFrame(rows, columns=["open", "high", "low", "close"], index=idx, dtype=float)
    df["volume"] = 1.0
    return df


def mirror(rows, pivot=200.0):
    """Reflect a price path so a bullish pattern becomes the bearish one."""
    return [(pivot - o, pivot - l, pivot - h, pivot - c) for o, h, l, c in rows]


# Downtrend -> internal low 100 (also London low) -> lower high 103 -> sweep to 99
# -> displacement through 103 leaving an FVG [100.6, 101.2] -> retrace fills the
# CE at 100.9 -> rally takes the London high at 107.
BULL_REVERSAL = [
    (106, 107, 105, 105.5), (105.5, 106, 104, 104.5), (104.5, 105, 103, 103.5), (103.5, 104, 102, 102.5),
    (102.5, 103, 101, 101.5), (101.5, 102, 100.5, 101), (101, 101.5, 100, 100.8), (100.8, 102, 100.6, 101.8),
    (101.8, 102.6, 101.2, 102.4), (102.4, 103, 102, 102.2), (102.2, 102.5, 101, 101.2), (101.2, 101.6, 100.2, 100.4),
    (100.4, 100.6, 99, 100.3), (100.3, 101.5, 100.1, 101.3), (101.3, 104.5, 101.2, 104.2), (104.2, 105, 103.8, 104.8),
    (104.8, 105.2, 102.5, 103), (103, 103.2, 100.8, 101.5), (101.5, 104, 101.4, 103.8), (103.8, 107, 103.5, 106.8),
    (106.8, 108, 106, 107.5),
]


@pytest.fixture
def tiny_cfg() -> StrategyConfig:
    return StrategyConfig(
        internal_len=2, swing_len=3, atr_len=3, fvg_min_atr=0.0, min_score=0,
        min_risk_atr=0.1, max_risk_atr=10, htf_minutes=60, min_rr=1.0,
    )


def random_walk(n=3000, seed=0, freq="15min") -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    close = 100 * np.exp(np.cumsum(rng.standard_t(4, n) * 0.003))
    open_ = np.r_[100.0, close[:-1]]
    wick = np.abs(rng.normal(0, 0.002, (n, 2))) * close[:, None]
    idx = pd.date_range("2023-01-02", periods=n, freq=freq, tz="UTC")
    return pd.DataFrame({
        "open": open_, "high": np.maximum(open_, close) + wick[:, 0],
        "low": np.minimum(open_, close) - wick[:, 1], "close": close, "volume": 1.0,
    }, index=idx)


def gold_like(days=45, seed=0, spike_every=7, start="2026-06-01") -> pd.DataFrame:
    """Synthetic XAUUSD-style M15 data: broker hours (Sunday 18:00 -> Friday 17:00
    NY, daily 17:00-18:00 break), quiet Asia, active London / NY, trend regimes
    and an occasional 08:30 NY news spike."""
    from zoneinfo import ZoneInfo

    ny = ZoneInfo("America/New_York")
    rng = np.random.default_rng(seed)
    idx = pd.date_range(pd.Timestamp(start, tz=ny), periods=days * 96, freq="15min")
    rows, times = [], []
    price, drift, day_n = 2400.0, 0.0, 0
    for ts in idx:
        wd, h, m = ts.weekday(), ts.hour, ts.minute
        closed = (wd == 4 and h >= 17) or wd == 5 or (wd == 6 and h < 18) or h == 17
        if closed:
            continue
        if h == 18 and m == 0:
            day_n += 1
            drift = rng.normal(0, 0.08)
        vol = 0.6 if (h >= 19 or h < 2) else 1.6 if 2 <= h < 11 else 1.0
        o = price
        c = o + drift + rng.normal(0, vol)
        if spike_every and day_n % spike_every == 3 and h == 8 and m == 30 and wd < 5:
            c = o + rng.choice([-1, 1]) * rng.uniform(12, 20)  # news candle
        hi = max(o, c) + abs(rng.normal(0, vol * 0.5))
        lo = min(o, c) - abs(rng.normal(0, vol * 0.5))
        rows.append((o, hi, lo, c))
        times.append(ts.tz_convert("UTC"))
        price = c
    df = pd.DataFrame(rows, columns=["open", "high", "low", "close"], index=pd.DatetimeIndex(times))
    df["volume"] = 1.0
    return df
