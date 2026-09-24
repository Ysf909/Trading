"""MetaTrader 5 helpers shared by the MT5 feed, the MT5 broker and ``smc-agent check``.

MT5 details handled here:

* the terminal returns candle and tick times in the **broker server's clock**,
  not UTC. Most forex / gold brokers run "New York + 7 h" (GMT+2 in winter,
  GMT+3 in summer) so that the daily candle closes at 17:00 New York;
* each symbol accepts only some order filling modes (FOK / IOC / RETURN);
* pending-order expiration is in server time, and some symbols only allow
  good-till-cancelled orders;
* symbol names differ between brokers (XAUUSD, XAUUSD.m, XAUUSDm, GOLD) and a
  symbol must be in Market Watch to be traded.
"""

from __future__ import annotations

import logging
import os
import re
import time
from datetime import datetime, timezone
from typing import Any, Iterable
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from ..config import BrokerConfig

log = logging.getLogger(__name__)

NY = ZoneInfo("America/New_York")


def connect(cfg: BrokerConfig) -> Any:
    """Import MetaTrader5 and attach to the terminal (optionally logging in)."""
    import MetaTrader5 as mt5  # optional dependency (Windows)

    kwargs: dict[str, Any] = {}
    if cfg.mt5_login:
        kwargs["login"] = int(cfg.mt5_login)
        password = os.environ.get(cfg.mt5_password_env, "")
        if password:
            kwargs["password"] = password
        if cfg.mt5_server:
            kwargs["server"] = cfg.mt5_server
    ok = mt5.initialize(cfg.mt5_path, **kwargs) if cfg.mt5_path else mt5.initialize(**kwargs)
    if not ok:
        raise RuntimeError(
            f"MT5 initialize() failed: {mt5.last_error()} - open the MetaTrader 5 terminal and log in "
            "to your account (or set broker.mt5_path / mt5_login / mt5_server and the "
            f"{cfg.mt5_password_env} environment variable)")
    return mt5


def ny7_offset_hours(epoch: float) -> float:
    """UTC offset of a 'New York + 7 h' server clock at ``epoch``: +2 (winter) or +3 (summer)."""
    off = datetime.fromtimestamp(epoch, NY).utcoffset()
    return (off.total_seconds() if off else 0.0) / 3600.0 + 7.0


class ServerClock:
    """Converts MT5 server timestamps to UTC.

    ``mode``: ``auto`` (detect from a fresh tick, else assume New York + 7 h),
    ``ny+7``, ``utc``, or a fixed offset in hours such as ``+2`` / ``3``.
    """

    def __init__(self, mode: str = "auto") -> None:
        m = (mode or "auto").strip().lower().replace(" ", "")
        self.fixed_h = 0.0
        self.detected = False
        if m in ("auto", "ny+7", "utc"):
            self.mode = m
        else:
            try:
                self.fixed_h = float(m)
            except ValueError as exc:
                raise ValueError(f"broker.mt5_server_time must be auto, ny+7, utc or hours like +2 (got {mode!r})") from exc
            self.mode = "fixed"

    def describe(self) -> str:
        if self.mode == "ny+7":
            return "New York + 7 h (GMT+2 winter / GMT+3 summer)"
        if self.mode == "utc":
            return "UTC"
        if self.mode == "fixed":
            return f"fixed GMT{self.fixed_h:+g}"
        return "auto (not detected yet)"

    def detect(self, mt5: Any, symbols: Iterable[str], now: float | None = None) -> str:
        """Resolve ``auto`` from the newest tick. A tick is fresh when its
        server time differs from UTC now by (almost exactly) whole half hours."""
        if self.mode != "auto":
            return self.describe()
        now = time.time() if now is None else now
        for sym in symbols:
            tick = mt5.symbol_info_tick(sym)
            if tick is None or not getattr(tick, "time", 0):
                continue
            diff = float(tick.time) - now
            hours = round(diff / 1800.0) / 2.0
            if abs(diff - hours * 3600.0) > 90 or abs(hours) > 14:
                continue
            ny7 = ny7_offset_hours(now)
            if hours == ny7:
                self.mode = "ny+7"
            elif hours == 0:
                self.mode = "utc"
            elif abs(hours - ny7) <= 1:
                continue  # probably a tick from a quiet minute (daily break): don't trust it
            else:
                self.mode, self.fixed_h = "fixed", hours
            self.detected = True
            return self.describe()
        self.mode = "ny+7"
        log.info("mt5: no fresh tick to read the server clock (market closed?) - assuming New York + 7 h. "
                 "Set broker.mt5_server_time if your broker differs.")
        return self.describe()

    def to_utc(self, server_secs: Any) -> pd.DatetimeIndex:
        naive = pd.DatetimeIndex(pd.to_datetime(np.asarray(server_secs, dtype="int64"), unit="s"))
        if self.mode == "utc":
            return naive.tz_localize("UTC")
        if self.mode in ("ny+7", "auto"):
            ny = (naive - pd.Timedelta(hours=7)).tz_localize(
                "America/New_York", ambiguous=np.zeros(len(naive), dtype=bool), nonexistent="shift_forward")
            return ny.tz_convert("UTC")
        return (naive - pd.Timedelta(hours=self.fixed_h)).tz_localize("UTC")

    def utc_to_server(self, epoch: float) -> float:
        if self.mode == "utc":
            return epoch
        if self.mode in ("ny+7", "auto"):
            return epoch + ny7_offset_hours(epoch) * 3600.0
        return epoch + self.fixed_h * 3600.0


def mt5_timeframe(mt5: Any, minutes: int) -> int:
    mapping = {
        1: "TIMEFRAME_M1", 5: "TIMEFRAME_M5", 15: "TIMEFRAME_M15", 30: "TIMEFRAME_M30",
        60: "TIMEFRAME_H1", 240: "TIMEFRAME_H4", 1440: "TIMEFRAME_D1",
    }
    if minutes not in mapping:
        raise ValueError(f"MT5 feed supports M1, M5, M15, M30, H1, H4, D1 (got {minutes} minutes)")
    return getattr(mt5, mapping[minutes])


def filling_for(mt5: Any, info: Any) -> int:
    """A filling mode the symbol accepts for market orders (error 10030 otherwise)."""
    flags = int(getattr(info, "filling_mode", 0) or 0)
    if flags & int(getattr(mt5, "SYMBOL_FILLING_IOC", 2)):
        return mt5.ORDER_FILLING_IOC
    if flags & int(getattr(mt5, "SYMBOL_FILLING_FOK", 1)):
        return mt5.ORDER_FILLING_FOK
    return mt5.ORDER_FILLING_RETURN


def allows_specified_expiry(mt5: Any, info: Any) -> bool:
    return bool(int(getattr(info, "expiration_mode", 0) or 0) & int(getattr(mt5, "SYMBOL_EXPIRATION_SPECIFIED", 4)))


def suggest_symbols(mt5: Any, symbol: str, limit: int = 8) -> list[str]:
    """Broker symbols that look like ``symbol`` (XAUUSD -> XAUUSD.m, XAUUSDm, GOLD ...)."""
    key = re.sub(r"[^A-Z]", "", symbol.upper())
    keys = {key[:6], key[:3]} if len(key) >= 6 else {key}
    if key.startswith("XAU") or key.startswith("GOLD"):
        keys |= {"XAU", "GOLD"}
    names = [s.name for s in (mt5.symbols_get() or [])]
    exact = [n for n in names if re.sub(r"[^A-Z]", "", n.upper()).startswith(key[:6])]
    loose = [n for n in names if n not in exact and any(k and k in n.upper() for k in keys)]
    return (exact + loose)[:limit]


def ensure_symbol(mt5: Any, symbol: str) -> Any:
    """Return ``symbol_info`` and make sure the symbol is in Market Watch."""
    info = mt5.symbol_info(symbol)
    if info is None:
        similar = suggest_symbols(mt5, symbol)
        hint = f" - your broker has: {', '.join(similar)}" if similar else ""
        raise RuntimeError(f"symbol {symbol!r} not found in MT5{hint}. Put the exact name in markets[].symbol.")
    if not getattr(info, "visible", True):
        mt5.symbol_select(symbol, True)
    return info


def server_time_now(mt5: Any, symbol: str, clock: ServerClock) -> int:
    """Current server time: the latest tick if it is fresh, else UTC now shifted by the clock."""
    est = clock.utc_to_server(datetime.now(timezone.utc).timestamp())
    tick = mt5.symbol_info_tick(symbol)
    if tick is not None and getattr(tick, "time", 0) and abs(float(tick.time) - est) < 600:
        return int(tick.time)
    return int(est)
