"""Timeframe helpers shared by the engine, feeds and CLI."""

from __future__ import annotations

_UNITS = {"m": 1, "h": 60, "d": 1440, "w": 10080}


def timeframe_minutes(tf: str) -> int:
    """'15m' -> 15, '1h' -> 60, '4h' -> 240, '1d' -> 1440; TradingView '15', '60', 'D', 'W' too."""
    tf = str(tf).strip()
    if tf.isdigit():
        return int(tf)
    up = tf.upper()
    if up in ("D", "1D"):
        return 1440
    if up in ("W", "1W"):
        return 10080
    unit = tf[-1].lower()
    if unit not in _UNITS or not (tf[:-1] or "1").isdigit():
        raise ValueError(f"unsupported timeframe {tf!r}")
    return int(tf[:-1] or 1) * _UNITS[unit]


def auto_htf_minutes(chart_minutes: int) -> int:
    """ICT-style bias timeframe for a chart timeframe (same table as the Pine script)."""
    if chart_minutes <= 1:
        return 15
    if chart_minutes <= 5:
        return 60
    if chart_minutes <= 60:
        return 240
    if chart_minutes <= 240:
        return 1440
    return 10080
