"""Market data feeds. Every feed returns an OHLCV DataFrame with a UTC
``DatetimeIndex`` (bar open time) and columns open/high/low/close/volume,
containing *closed* candles only."""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Protocol

import pandas as pd

from ..config import BrokerConfig, MarketConfig
from ..core.timeframes import timeframe_minutes

log = logging.getLogger(__name__)

COLUMNS = ["open", "high", "low", "close", "volume"]

def normalize(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df.columns = [str(c).strip().lower() for c in df.columns]
    missing = [c for c in ("open", "high", "low", "close") if c not in df.columns]
    if missing:
        raise ValueError(f"data is missing columns {missing}")
    if "volume" not in df.columns:
        df["volume"] = 0.0
    idx = pd.to_datetime(df.index)
    idx = idx.tz_localize("UTC") if idx.tz is None else idx.tz_convert("UTC")
    df.index = idx
    cols = COLUMNS + (["spread"] if "spread" in df.columns else [])
    df = df[cols].astype(float)
    df = df[~df.index.duplicated(keep="last")].sort_index()
    df = df.dropna(subset=["open", "high", "low", "close"])
    # bad ticks: non-positive prices are dropped, inconsistent high/low repaired
    df = df[(df[["open", "high", "low", "close"]] > 0).all(axis=1)]
    df["high"] = df[["open", "high", "low", "close"]].max(axis=1)
    df["low"] = df[["open", "high", "low", "close"]].min(axis=1)
    return df


def load_csv(path: str | Path) -> pd.DataFrame:
    """Load a CSV exported from TradingView, MT5, exchanges or backtesting libs.

    Recognised time columns: time / date / datetime / timestamp (unix s or ms,
    or any date string), or separate Date + Time columns. First unnamed column
    is used as the index when no time column is found.
    """
    raw = pd.read_csv(path)
    cols = {c.lower().strip(): c for c in raw.columns}
    if "date" in cols and "time" in cols:
        ts = pd.to_datetime(raw[cols["date"]].astype(str) + " " + raw[cols["time"]].astype(str))
        raw = raw.drop(columns=[cols["date"], cols["time"]])
    else:
        key = next((cols[k] for k in ("time", "datetime", "timestamp", "date", "open time") if k in cols), None)
        if key is None:
            key = raw.columns[0]
        col = raw[key]
        if pd.api.types.is_numeric_dtype(col):
            unit = "ms" if col.max() > 1e11 else "s"
            ts = pd.to_datetime(col, unit=unit, utc=True)
        else:
            ts = pd.to_datetime(col, utc=True, format="mixed")
        raw = raw.drop(columns=[key])
    raw.index = pd.DatetimeIndex(ts)
    return normalize(raw)


def resample(df: pd.DataFrame, tf: str) -> pd.DataFrame:
    rule = f"{timeframe_minutes(tf)}min"
    out = df.resample(rule, label="left", closed="left").agg(
        {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
    )
    return out.dropna()


class Feed(Protocol):
    def history(self, bars: int) -> pd.DataFrame: ...

    def latest(self, bars: int = 5) -> pd.DataFrame: ...


def _drop_open_candle(df: pd.DataFrame, tf_minutes: int) -> pd.DataFrame:
    if df.empty:
        return df
    now = datetime.now(timezone.utc)
    last_open = df.index[-1].to_pydatetime()
    if last_open + timedelta(minutes=tf_minutes) > now:
        return df.iloc[:-1]
    return df


class CSVFeed:
    def __init__(self, path: str | Path, timeframe: str | None = None) -> None:
        df = load_csv(path)
        self.df = resample(df, timeframe) if timeframe else df

    def history(self, bars: int) -> pd.DataFrame:
        return self.df.iloc[-bars:] if bars else self.df

    def latest(self, bars: int = 5) -> pd.DataFrame:
        return self.df.iloc[-bars:]


class CCXTFeed:
    """Crypto data from any ccxt exchange (public endpoints, no keys needed)."""

    def __init__(self, exchange: str, symbol: str, timeframe: str) -> None:
        import ccxt  # optional dependency

        self.ex = getattr(ccxt, exchange)({"enableRateLimit": True})
        self.symbol = symbol
        self.tf = timeframe
        self.minutes = timeframe_minutes(timeframe)

    def _fetch(self, since_ms: int | None, limit: int) -> list[list[float]]:
        return self.ex.fetch_ohlcv(self.symbol, self.tf, since=since_ms, limit=limit)

    def history(self, bars: int) -> pd.DataFrame:
        per_call = 1000
        since = int((time.time() - (bars + 2) * self.minutes * 60) * 1000)
        rows: list[list[float]] = []
        while True:
            chunk = self._fetch(since, per_call)
            if not chunk:
                break
            rows.extend(chunk)
            last = chunk[-1][0]
            if len(chunk) < 2 or last <= since:
                break
            since = last + 1
            if last >= (time.time() - self.minutes * 60) * 1000:
                break
        return self._frame(rows).iloc[-bars:]

    def latest(self, bars: int = 5) -> pd.DataFrame:
        return self._frame(self._fetch(None, bars + 1))

    def _frame(self, rows: list[list[float]]) -> pd.DataFrame:
        df = pd.DataFrame(rows, columns=["ts", *COLUMNS])
        df.index = pd.to_datetime(df.pop("ts"), unit="ms", utc=True)
        return _drop_open_candle(normalize(df), self.minutes)


class YFinanceFeed:
    """Stocks, indices, forex (``EURUSD=X``), futures (``NQ=F``) via yfinance.

    Yahoo limits intraday history (1m: 7 days, <1h: 60 days, 1h: 730 days)."""

    _INTERVALS = {1: "1m", 2: "2m", 5: "5m", 15: "15m", 30: "30m", 60: "60m", 90: "90m", 1440: "1d", 10080: "1wk"}

    def __init__(self, symbol: str, timeframe: str) -> None:
        import yfinance  # optional dependency

        self.yf = yfinance
        self.symbol = symbol
        self.minutes = timeframe_minutes(timeframe)
        self.resample_to = None
        if self.minutes not in self._INTERVALS:
            if self.minutes % 60 == 0 and self.minutes < 1440:
                self.resample_to = timeframe
                self.interval = "60m"
            else:
                raise ValueError(f"yfinance cannot serve timeframe {timeframe}")
        else:
            self.interval = self._INTERVALS[self.minutes]

    def _download(self, days: int) -> pd.DataFrame:
        df = self.yf.download(self.symbol, period=f"{days}d", interval=self.interval, progress=False, auto_adjust=False)
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        df = normalize(df)
        if self.resample_to:
            df = resample(df, self.resample_to)
        return _drop_open_candle(df, self.minutes)

    def history(self, bars: int) -> pd.DataFrame:
        cap = {1: 7, 2: 59, 5: 59, 15: 59, 30: 59, 60: 729, 90: 59}.get(
            60 if self.resample_to else self.minutes, 3650
        )
        days = min(cap, max(2, int(bars * self.minutes / 1440 * 1.6) + 2))
        return self._download(days).iloc[-bars:]

    def latest(self, bars: int = 5) -> pd.DataFrame:
        return self._download(max(2, int(bars * self.minutes / 1440) + 2)).iloc[-bars:]


class MT5Feed:
    """MetaTrader 5 terminal data (Windows, ``pip install MetaTrader5``).

    Candle times arrive in the broker's server clock and are converted to UTC
    (``broker.mt5_server_time``), so sessions, killzones and news windows line up."""

    def __init__(self, symbol: str, timeframe: str, broker: BrokerConfig | None = None) -> None:
        from ..execution.mt5_common import ServerClock, connect, ensure_symbol, mt5_timeframe

        cfg = broker or BrokerConfig()
        self.mt5 = mt5 = connect(cfg)
        self.symbol = symbol
        self.info = ensure_symbol(mt5, symbol)
        self.clock = ServerClock(cfg.mt5_server_time)
        log.info("mt5: %s server clock: %s", symbol, self.clock.detect(mt5, [symbol]))
        self.minutes = timeframe_minutes(timeframe)
        self.tf = mt5_timeframe(mt5, self.minutes)

    def _rates(self, count: int) -> pd.DataFrame:
        # position 1 skips the still-forming candle
        rates = self.mt5.copy_rates_from_pos(self.symbol, self.tf, 1, count)
        if rates is None or len(rates) == 0:
            raise RuntimeError(f"MT5 copy_rates failed for {self.symbol}: {self.mt5.last_error()}")
        df = pd.DataFrame(rates)
        df.index = self.clock.to_utc(df.pop("time"))
        df = df.rename(columns={"tick_volume": "volume"})
        point = getattr(self.info, "point", 0) or 0
        if point and "spread" in df:
            df["spread"] = df["spread"] * point  # points -> price units
        return normalize(df)

    def history(self, bars: int) -> pd.DataFrame:
        return self._rates(bars)

    def latest(self, bars: int = 5) -> pd.DataFrame:
        return self._rates(bars)


def make_feed(m: MarketConfig, broker: BrokerConfig | None = None) -> Feed:
    kind = m.feed.lower()
    if kind == "ccxt":
        return CCXTFeed(m.exchange, m.symbol, m.timeframe)
    if kind == "yfinance":
        return YFinanceFeed(m.symbol, m.timeframe)
    if kind == "mt5":
        return MT5Feed(m.symbol, m.timeframe, broker)
    if kind == "csv":
        return CSVFeed(m.csv_path, m.timeframe or None)
    raise ValueError(f"unknown feed {m.feed!r}")


def load_json_klines(path: str | Path) -> pd.DataFrame:
    """Load ``[[ts_ms, o, h, l, c, v], ...]`` JSON (freqtrade / exchange dumps)."""
    rows = json.loads(Path(path).read_text())
    df = pd.DataFrame(rows, columns=["ts", *COLUMNS])
    df.index = pd.to_datetime(df.pop("ts"), unit="ms", utc=True)
    return normalize(df)
