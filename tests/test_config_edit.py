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
