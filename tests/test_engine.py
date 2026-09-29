import json

import pytest

from smc_agent.config import StrategyConfig, strategy_from_overrides
from smc_agent.core.engine import SMCEngine, bars_from_df
from smc_agent.core.types import LONG, SHORT

from .conftest import BULL_REVERSAL, frame, mirror, random_walk


def run(df, cfg):
    eng = SMCEngine(cfg, "TEST", "15m")
    sigs = []
    for bar in bars_from_df(df):
        sigs += eng.update(bar)
    return eng, sigs


def test_bullish_reversal_setup(tiny_cfg):
    eng, sigs = run(frame(BULL_REVERSAL), tiny_cfg)
    rev = [s for s in sigs if s.model == "reversal"]
    assert len(rev) == 1
    s = rev[0]
    assert s.direction == LONG and s.bar == 14
    assert s.zone.kind == "FVG" and (s.zone.bottom, s.zone.top) == (100.6, 101.2)
    assert s.entry == pytest.approx(100.9)  # consequent encroachment
    assert s.sl == pytest.approx(99 - 0.1 * s.atr)  # beyond the sweep low
    assert s.tp == 107.0 and s.meta["tp_kind"] == "london_high"
    assert s.rr == pytest.approx((107 - 100.9) / (100.9 - s.sl))
    assert any("liquidity" in r for r in s.reasons)


def test_bearish_reversal_is_the_mirror(tiny_cfg):
    eng, sigs = run(frame(mirror(BULL_REVERSAL)), tiny_cfg)
    rev = [s for s in sigs if s.model == "reversal"]
    assert len(rev) == 1
    s = rev[0]
    assert s.direction == SHORT and s.bar == 14
    assert s.entry == pytest.approx(200 - 100.9)
    assert s.sl == pytest.approx(200 - 99 + 0.1 * s.atr)
    assert s.tp == pytest.approx(200 - 107.0)


def test_edge_entry_mode(tiny_cfg):
    cfg = strategy_from_overrides(tiny_cfg, {"entry_mode": "edge"})
    _, sigs = run(frame(BULL_REVERSAL), cfg)
    s = [s for s in sigs if s.model == "reversal"][0]
    assert s.entry == 101.2  # proximal edge of the bullish FVG


def test_fixed_target_mode(tiny_cfg):
    cfg = strategy_from_overrides(tiny_cfg, {"tp_mode": "fixed", "rr_target": 2.0})
    _, sigs = run(frame(BULL_REVERSAL), cfg)
    s = [s for s in sigs if s.model == "reversal"][0]
    assert s.rr == pytest.approx(2.0)


def test_filters_reject(tiny_cfg):
    cfg = strategy_from_overrides(tiny_cfg, {"min_score": 11})
    eng, sigs = run(frame(BULL_REVERSAL), cfg)
    assert sigs == [] and eng.rejections.get("min_score", 0) >= 1


def test_fvg_detection_and_fill(tiny_cfg):
    eng, _ = run(frame(BULL_REVERSAL), tiny_cfg)
    fvg = [z for z in eng.zone_history if z.kind == "FVG" and z.created == 14][0]
    assert fvg.direction == LONG and fvg.bar == 13
    assert fvg.touched  # the retrace traded into it
    assert fvg.active  # ... but never filled through its bottom


def test_order_block_on_break(tiny_cfg):
    eng, _ = run(frame(BULL_REVERSAL), tiny_cfg)
    obs = [z for z in eng.zone_history if z.kind == "OB" and z.direction == LONG and z.created == 14]
    assert obs, "bullish break should create an order block"
    ob = obs[0]
    # the OB candle is the lowest candle between the broken pivot high and the break
    assert ob.bar == 12 and ob.bottom == 99 and ob.top == 100.6


def test_sweep_and_levels(tiny_cfg):
    eng, _ = run(frame(BULL_REVERSAL), tiny_cfg)
    sweeps = [s for s in eng.sweep_history if s.bar == 12]
    assert sweeps and sweeps[0].side == SHORT and sweeps[0].price == 100 and sweeps[0].rejected


def test_equal_highs_detected():
    rows = [(10, 10.5, 9.5, 10), (10, 12, 9.8, 11.5), (11.5, 11.8, 10, 10.2), (10.2, 11, 9.9, 10.8),
            (10.8, 12.02, 10.5, 11.6), (11.6, 11.7, 10.4, 10.6), (10.6, 10.9, 10.1, 10.3), (10.3, 10.6, 9.9, 10.1)]
    cfg = StrategyConfig(internal_len=1, swing_len=2, atr_len=2, eq_tolerance_atr=0.2, min_score=0)
    eng, _ = run(frame(rows), cfg)
    assert any(side == LONG for *_, side in eng.eq_history)
    assert any(lv.kind == "eqh" for lv in eng.levels)


def test_engine_is_causal():
    """Signals must not depend on future bars: a truncated run reproduces the prefix."""
    df = random_walk(1500, seed=3)
    cfg = StrategyConfig(min_score=0)
    _, full = run(df, cfg)
    for cut in (400, 900, 1300):
        _, part = run(df.iloc[:cut], cfg)
        assert [s.id for s in part] == [s.id for s in full if s.bar < cut]
        assert [(s.entry, s.sl, s.tp) for s in part] == [(s.entry, s.sl, s.tp) for s in full if s.bar < cut]


def test_snapshot_is_json_serialisable():
    eng, _ = run(random_walk(800, seed=1), StrategyConfig())
    snap = eng.snapshot()
    json.dumps(snap)
    json.dumps(eng.recent_bars(20))
    assert snap["dealing_range"]["price_zone"] in ("premium", "discount")
    assert set(snap["setup_state"]) == {"long", "short"}


def test_signal_scores_bounded():
    _, sigs = run(random_walk(3000, seed=5), StrategyConfig(min_score=0))
    assert sigs
    for s in sigs:
        assert 0 <= s.score <= 10
        assert (s.entry - s.sl) * s.direction > 0 and (s.tp - s.entry) * s.direction > 0
        assert s.rr >= 1.5 - 1e-9
