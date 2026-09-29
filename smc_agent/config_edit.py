"""Change a few settings in config.yaml without disturbing its comments:
the trading profile and where the orders go (``smc-agent mode``)."""

from __future__ import annotations

import re
import shutil
import tempfile
from pathlib import Path

from .config import PROFILES, AppConfig, load_config

BROKERS = ("paper", "mt5", "ccxt")


def _value_with_comment(line: str, value: str) -> str:
    """Replace the value of ``key: value   # comment`` keeping indentation and the aligned comment."""
    m = re.match(r"^(\s*[A-Za-z_]\w*\s*:\s*)([^#\n]*?)(\s*)(#.*)?$", line)
    if not m:
        return line
    head, old, gap, comment = m.groups()
    if not comment:
        return f"{head}{value}"
    return f"{head}{value}{' ' * max(1, len(old) + len(gap) - len(value))}{comment}"


def set_top_level(lines: list[str], key: str, value: str) -> list[str]:
    for i, line in enumerate(lines):
        if re.match(rf"^{key}\s*:", line):
            lines[i] = _value_with_comment(line, value)
            return lines
    # insert after the leading comment block
    i = 0
    while i < len(lines) and (lines[i].startswith("#") or not lines[i].strip()):
        i += 1
    return lines[:i] + [f"{key}: {value}", ""] + lines[i:]


def set_section_key(lines: list[str], section: str, key: str, value: str) -> list[str]:
    start = next((i for i, line in enumerate(lines) if re.match(rf"^{section}\s*:", line)), None)
    if start is None:
        return lines + ["", f"{section}:", f"  {key}: {value}"]
    end = start + 1
    while end < len(lines) and (not lines[end].strip() or lines[end].startswith((" ", "\t", "#"))):
        end += 1
    for i in range(start + 1, end):
        if re.match(rf"^\s+{key}\s*:", lines[i]):
            lines[i] = _value_with_comment(lines[i], value)
            return lines
    return lines[: start + 1] + [f"  {key}: {value}"] + lines[start + 1:]


CRYPTO = ("BTC", "ETH", "SOL", "XRP", "LTC", "DOGE", "ADA", "BNB")
TIMEFRAMES = ("1m", "5m", "15m", "30m", "1h")


def _section(lines: list[str], name: str) -> tuple[int, int] | None:
    """(first line, end) of a top-level section; end is the next top-level key."""
    start = next((i for i, line in enumerate(lines) if re.match(rf"^{name}\s*:", line)), None)
    if start is None:
        return None
    end = start + 1
    while end < len(lines) and (not lines[end].strip() or lines[end].startswith((" ", "\t", "#"))):
        end += 1
    return start, end


def set_markets_timeframe(lines: list[str], timeframe: str) -> list[str]:
    sec = _section(lines, "markets")
    if sec is None:
        return lines
    for i in range(sec[0] + 1, sec[1]):
        if re.match(r"^\s+timeframe\s*:", lines[i]):
            lines[i] = _value_with_comment(lines[i], timeframe)
    return lines


def add_market(lines: list[str], symbol: str, timeframe: str, feed: str) -> list[str]:
    sec = _section(lines, "markets")
    if sec is None:
        return lines + ["", "markets:"] + _market_block(symbol, timeframe, feed, "  ")
    start, end = sec
    items = [line for line in lines[start + 1:end] if re.match(r"^\s*- symbol\s*:", line)]
    if any(re.match(rf"^\s*- symbol\s*:\s*{re.escape(symbol)}\s*(#.*)?$", line) for line in items):
        return lines  # already there
    indent = re.match(r"^(\s*)-", items[0]).group(1) if items else "  "
    at = end
    while at > start + 1 and not lines[at - 1].strip():
        at -= 1
    return lines[:at] + _market_block(symbol, timeframe, feed, indent) + lines[at:]


def _market_block(symbol: str, timeframe: str, feed: str, indent: str) -> list[str]:
    block = [f"{indent}- symbol: {symbol}", f"{indent}  timeframe: {timeframe}", f"{indent}  feed: {feed}"]
    if any(k in symbol.upper() for k in CRYPTO):
        block += [f"{indent}  guard:", f"{indent}    market_hours: 24x7", f"{indent}    max_spread: 0"]
    return block


def describe(cfg: AppConfig) -> str:
    where = {"paper": "PAPER - trades are only simulated, nothing is sent to MT5",
             "mt5": "MT5 - real orders on the account the MT5 terminal is logged into",
             "ccxt": "crypto exchange (ccxt)"}.get(cfg.broker.kind.lower(), cfg.broker.kind)
    return (f"  orders : {where}\n"
            f"  profile: {cfg.profile} (minimum setup score {cfg.strategy.min_score}/10)\n"
            f"  markets: {', '.join(f'{m.symbol} {m.timeframe}' for m in cfg.markets)}")


def change_mode(path: str | Path, broker: str | None = None, profile: str | None = None,
                timeframe: str | None = None, add_markets: list[str] | None = None) -> AppConfig:
    """Edit ``path`` in place (a .bak copy is kept) and return the new configuration.
    Nothing is written if the result would not load."""
    path = Path(path)
    if broker is not None and broker not in BROKERS:
        raise ValueError(f"broker must be one of {', '.join(BROKERS)}")
    if profile is not None and profile not in PROFILES:
        raise ValueError(f"profile must be one of {', '.join(PROFILES)}")
    if timeframe is not None and timeframe not in TIMEFRAMES:
        raise ValueError(f"timeframe must be one of {', '.join(TIMEFRAMES)}")
    text = path.read_text()
    lines = text.split("\n")
    if profile is not None:
        lines = set_top_level(lines, "profile", profile)
    if broker is not None:
        lines = set_section_key(lines, "broker", "kind", broker)
    if timeframe is not None:
        lines = set_markets_timeframe(lines, timeframe)
    if add_markets:
        current = load_config(path)
        tf = timeframe or (current.markets[0].timeframe if current.markets else "15m")
        feed = current.markets[0].feed if current.markets else "mt5"
        for sym in add_markets:
            sym = sym.strip()
            if sym:
                lines = add_market(lines, sym, tf, feed)
    new = "\n".join(lines)
    with tempfile.TemporaryDirectory() as tmp:
        probe = Path(tmp) / "config.yaml"
        probe.write_text(new)
        cfg = load_config(probe)  # raises (and nothing is written) if the edit broke the file
    if new != text:
        shutil.copyfile(path, path.with_suffix(path.suffix + ".bak"))
        path.write_text(new)
    return cfg
