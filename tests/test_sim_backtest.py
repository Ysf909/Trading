from datetime import datetime, timezone

import pytest

from smc_agent.backtest import collect_outcomes, run_backtest
from smc_agent.config import StrategyConfig
from smc_agent.core.types import Bar, Signal
from smc_agent.execution.sim import Trade, settle, step

from .conftest import BULL_REVERSAL, frame, random_walk

T0 = datetime(2024, 1, 1, tzinfo=timezone.utc)


def sig(direction=1, entry=100.0, sl=98.0, tp=104.0, bar=0, expiry=5):
    return Signal("x", "S", "15m", T0, bar, direction, "reversal", entry, sl, tp,
                  abs(tp - entry) / abs(entry - sl), 1.0, 1.0, expiry, 6, "A", {}, [])


def b(o, h, l, c):
    return Bar(T0, o, h, l, c)


def test_limit_fill_then_target():
    tr = Trade(sig())
    assert step(tr, b(101, 102, 100.5, 101), 1) is None
    assert step(tr, b(101, 101.5, 99.5, 100.5), 2) == "filled"
    assert tr.fill_price == 100.0
    assert step(tr, b(100.5, 104.5, 100, 104), 3) == "closed"
    assert tr.exit_reason == "tp" and tr.r_multiple == pytest.approx(2.0)


def test_gap_fill_at_open_and_same_bar_stop():
    tr = Trade(sig())
    assert step(tr, b(99, 99.5, 97, 97.5), 1) == "filled+closed"
    assert tr.fill_price == 99 and tr.exit_price == 98 and tr.exit_reason == "sl"


def test_target_on_fill_bar_is_not_assumed():
    tr = Trade(sig())
    assert step(tr, b(101, 105, 99.8, 104), 1) == "filled"
    assert tr.status == "open"


def test_missed_and_expired():
    tr = Trade(sig())
    assert step(tr, b(101, 104.2, 100.2, 104), 1) == "cancelled" and tr.exit_reason == "missed"
    tr = Trade(sig(expiry=2))
    step(tr, b(101, 102, 100.5, 101), 1)
    step(tr, b(101, 102, 100.5, 101), 2)
    assert step(tr, b(101, 102, 100.5, 101), 3) == "cancelled" and tr.exit_reason == "expired"


def test_stop_checked_before_target():
    tr = Trade(sig())
    step(tr, b(100.5, 101, 99.9, 100.2), 1)
    step(tr, b(100, 104.5, 97.5, 103), 2)
    assert tr.exit_reason == "sl" and tr.r_multiple == pytest.approx(-1.0)


def test_short_side_and_breakeven():
    tr = Trade(sig(direction=-1, entry=100, sl=102, tp=96))
    step(tr, b(99.5, 100.2, 99, 99.5), 1, breakeven_at_r=1.0)
    step(tr, b(99.5, 99.8, 97.9, 98.2), 2, breakeven_at_r=1.0)  # +1R reached -> stop to entry
    assert tr.be_moved and tr.sl == 100
    step(tr, b(98.2, 100.5, 98, 100.3), 3, breakeven_at_r=1.0)
    assert tr.exit_reason == "be" and tr.r_multiple == pytest.approx(0.0)


def test_settle_fees():
    tr = Trade(sig(), qty=2.0)
    step(tr, b(100.5, 101, 99.9, 100.2), 1)
    step(tr, b(100.5, 104.5, 100.1, 104), 2)
    settle(tr, commission_pct=0.1)
    assert tr.fees == pytest.approx((100 + 104) * 2 * 0.001)
    assert tr.pnl == pytest.approx(8 - tr.fees)


def test_backtest_takes_the_reversal(tiny_cfg):
    res = run_backtest(frame(BULL_REVERSAL), tiny_cfg, symbol="T", timeframe="15m")
    assert len(res.trades) == 1
    t = res.trades[0]
    assert t.exit_reason == "tp" and t.r_multiple > 2.5
    assert res.metrics["summary"]["trades"] == 1
    assert res.metrics["account"]["final_equity"] > res.starting_equity


def test_random_walk_has_no_edge():
    """Sanity check against look-ahead: on a random walk the average result
    should be close to zero (conservative fills push it slightly negative)."""
    rs = []
    for seed in range(4):
        rs += [t.r_multiple for t in collect_outcomes(random_walk(3000, seed), StrategyConfig(min_score=0))
               if t.status == "closed"]
    assert len(rs) > 100
    avg = sum(rs) / len(rs)
    assert -0.4 < avg < 0.3


def test_collect_outcomes_resolves_everything():
    outs = collect_outcomes(random_walk(2000, 7), StrategyConfig(min_score=0))
    assert outs and all(t.status in ("closed", "cancelled") for t in outs)
