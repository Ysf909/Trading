"""The TradingView scripts pass the static checks TradingView's compiler enforces
(declaration order, scopes, globals modified in functions, typos)."""

import sys
from pathlib import Path

import pytest

TV = Path(__file__).resolve().parents[1] / "tradingview"
sys.path.insert(0, str(TV))

from check_pine import check  # noqa: E402


@pytest.mark.parametrize("name", ["SMC_ICT_Pro.pine", "SMC_ICT_Pro_Strategy.pine"])
def test_pine_script_static_checks(name):
    assert check(TV / name) == []


def test_checker_catches_forward_reference(tmp_path):
    bad = tmp_path / "bad.pine"
    bad.write_text('//@version=6\nindicator("x")\nf_a(int n) =>\n    f_b(n)\nf_b(int n) =>\n    n\nplot(f_a(1))\n')
    assert any("f_b used" in e for e in check(bad))
