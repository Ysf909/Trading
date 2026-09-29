"""smc-agent mode / windows\\settings.bat: switch paper <-> mt5 and the profile in config.yaml."""

import pytest

from smc_agent.cli import main
from smc_agent.config import load_config
from smc_agent.config_edit import change_mode

OLD_STYLE = """# SMC / ICT agent - XAUUSD (gold) configuration. Copy to config.yaml and edit.
# Every key is optional.

markets:
  - symbol: XAUUSD_
    timeframe: 15m
    feed: csv
    csv_path: x.csv

broker:
  kind: paper                 # paper = simulated | mt5 = real orders
  mt5_magic: 909909           # tags the agent's orders
"""


def test_switch_to_mt5_and_active_keeps_comments(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text(OLD_STYLE)
    cfg = change_mode(p, broker="mt5", profile="active")
    assert cfg.broker.kind == "mt5" and cfg.profile == "active" and cfg.strategy.min_score == 4
    text = p.read_text()
    assert "profile: active\n" in text and text.index("profile: active") < text.index("markets:")
    assert "  kind: mt5                   # paper = simulated | mt5 = real orders" in text
    assert "# tags the agent's orders" in text and (tmp_path / "config.yaml.bak").read_text() == OLD_STYLE
    again = change_mode(p, broker="paper", profile="balanced")
    assert again.broker.kind == "paper" and again.profile == "balanced"
    assert p.read_text().count("profile:") == 1 and p.read_text().count("kind:") == 1


def test_broker_section_added_when_missing(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text("markets:\n  - symbol: XAUUSD_\n    feed: csv\n    csv_path: x.csv\n")
    assert change_mode(p, broker="mt5").broker.kind == "mt5"
    assert load_config(p).broker.kind == "mt5"


def test_bad_values_write_nothing(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text(OLD_STYLE)
    with pytest.raises(ValueError):
        change_mode(p, profile="yolo")
    assert p.read_text() == OLD_STYLE


def test_mode_command(tmp_path, capsys):
    p = tmp_path / "config.yaml"
    p.write_text(OLD_STYLE)
    main(["-c", str(p), "mode"])
    out = capsys.readouterr().out
    assert "PAPER" in out and "profile: safe" in out
    main(["-c", str(p), "mode", "--broker", "mt5", "--profile", "active"])
    out = capsys.readouterr().out
    assert "Saved" in out and "MT5 - real orders" in out and "profile: active (minimum setup score 4/10)" in out
    assert load_config(p).broker.kind == "mt5"


def test_timeframe_and_added_markets(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text(OLD_STYLE)
    cfg = change_mode(p, profile="active", timeframe="5m", add_markets=["BTCUSD", "XAGUSD_", "XAUUSD_"])
    assert [(m.symbol, m.timeframe) for m in cfg.markets] == [("XAUUSD_", "5m"), ("BTCUSD", "5m"), ("XAGUSD_", "5m")]
    btc = cfg.markets[1]
    assert btc.feed == "csv" and btc.guard == {"market_hours": "24x7", "max_spread": 0}  # crypto hours
    assert cfg.markets[2].guard == {}
    assert cfg.risk.max_open_positions == 3 and cfg.guard.max_spread == 0  # active profile
    text = p.read_text()
    assert text.index("XAGUSD_") < text.index("broker:")  # inside the markets list


def test_mode_command_adds_markets_from_one_string(tmp_path, capsys):
    p = tmp_path / "config.yaml"
    p.write_text(OLD_STYLE)
    main(["-c", str(p), "mode", "--timeframe", "5m", "--add-market", "EURUSD_ GBPUSD_"])
    out = capsys.readouterr().out
    assert "XAUUSD_ 5m, EURUSD_ 5m, GBPUSD_ 5m" in out and "check.bat" in out
