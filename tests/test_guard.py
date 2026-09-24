import json
from datetime import datetime, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from smc_agent.backtest import run_backtest
from smc_agent.config import GuardConfig, StrategyConfig
from smc_agent.core.engine import SMCEngine, bars_from_df
from smc_agent.core.mtf import MTFContext, TFTracker
from smc_agent.core.sessions import PeriodLevels
from smc_agent.core.timeframes import Bucketer
from smc_agent.core.types import Bar, LiquidityLevel, Signal
from smc_agent.guard import Guard, apply_decision
from smc_agent.news import NewsCalendar, NewsEvent

from .conftest import gold_like

NY = ZoneInfo("America/New_York")
GOLD = StrategyConfig(day_tz="America/New_York", day_roll_hour=17)


def ny(*args):
    return datetime(*args, tzinfo=NY).astimezone(timezone.utc)


def sig(direction=1, entry=100.0, sl=99.0, tp=104.0):
    return Signal("s", "XAUUSD", "15m", ny(2026, 9, 22, 10, 45), 10, direction, "reversal", entry, sl, tp,
                  abs(tp - entry) / abs(entry - sl), 1.0, 1.0, 16, 7, "A", {}, ["r"])


def fake_engine(levels=(), atr=1.0):
    return SimpleNamespace(atr=atr, levels=list(levels), daily=PeriodLevels(), bar_events=[], t=10,
                           chart_minutes=15, closes=[100.0])


def guard(now, **over):
    g = Guard(GuardConfig(**over), GOLD)
    g.now, g.t = now, 10
    return g


TUESDAY_1045 = ny(2026, 9, 22, 10, 45)  # a quiet time: no session / news block


# ------------------------------------------------------------------ time
def test_bucketer_follows_the_gold_trading_day():
    b = Bucketer(1440, "America/New_York", 17)
    assert b.key(ny(2026, 9, 21, 16, 59)) != b.key(ny(2026, 9, 21, 17, 0))
    assert b.key(ny(2026, 9, 21, 17, 0)) == b.key(ny(2026, 9, 22, 16, 59))
    w = Bucketer(10080, "America/New_York", 17)
    assert w.key(ny(2026, 9, 18, 16, 0)) != w.key(ny(2026, 9, 20, 18, 0))  # Friday vs Sunday open
    assert w.key(ny(2026, 9, 20, 18, 0)) == w.key(ny(2026, 9, 25, 16, 0))


# ------------------------------------------------------------------ sessions
@pytest.mark.parametrize("when,blocked", [
    (ny(2026, 9, 22, 10, 45), None),
    (ny(2026, 9, 22, 17, 15), "rollover"),
    (ny(2026, 9, 25, 14, 30), "Friday"),
    (ny(2026, 9, 26, 12, 0), "weekend"),
    (ny(2026, 9, 27, 18, 30), "Sunday"),
    (ny(2026, 12, 25, 10, 0), "holiday"),
])
def test_session_rules(when, blocked):
    why = guard(when).session_block(when)
    assert (why is None) if blocked is None else (blocked in why)


def test_24x7_markets_have_no_session_rules():
    assert guard(ny(2026, 9, 26, 12, 0), market_hours="24x7").session_block(ny(2026, 9, 26, 12, 0)) is None


# ------------------------------------------------------------------ news
FF = [
    {"title": "Non-Farm Employment Change", "country": "USD", "date": "2026-10-02T08:30:00-04:00", "impact": "High"},
    {"title": "German Prelim CPI", "country": "EUR", "date": "2026-10-02T08:00:00-04:00", "impact": "High"},
    {"title": "Unemployment Claims", "country": "USD", "date": "2026-10-01T08:30:00-04:00", "impact": "Medium"},
    {"title": "Bank Holiday", "country": "USD", "date": "2026-10-12T00:00:00-04:00", "impact": "Holiday"},
]


def test_forexfactory_parsing_and_windows():
    cal = NewsCalendar.from_forexfactory(FF)
    g = guard(ny(2026, 10, 2, 8, 5))
    g.calendar = cal
    assert "Non-Farm" in g.news_block(ny(2026, 10, 2, 8, 5))
    assert "Non-Farm" in g.news_block(ny(2026, 10, 2, 8, 55))
    assert g.news_block(ny(2026, 10, 2, 11, 30)) is None
    assert g.news_block(ny(2026, 10, 1, 8, 5)) is None  # medium impact ignored at "high"
    assert "holiday" in g.news_block(ny(2026, 10, 12, 11, 0))
    g.cfg.news_min_impact = "medium"
    assert "Claims" in g.news_block(ny(2026, 10, 1, 8, 5))


def test_standard_release_windows_without_calendar():
    g = guard(TUESDAY_1045)
    assert "08:30" in g.news_block(ny(2026, 9, 22, 8, 25))
    assert "10:00" in g.news_block(ny(2026, 9, 22, 10, 15))
    assert g.news_block(ny(2026, 9, 22, 10, 25)) is None
    assert g.news_block(ny(2026, 9, 26, 8, 30)) is None  # Saturday


def test_calendar_csv_and_offline_fallback(tmp_path, monkeypatch):
    p = tmp_path / "news.csv"
    p.write_text("time,currency,impact,title\n2026-10-02T08:30:00-04:00,USD,high,NFP\n")
    assert NewsCalendar.from_csv(p).events[0].title == "NFP"

    def boom(*a, **k):
        raise OSError("offline")

    monkeypatch.setattr("urllib.request.urlopen", boom)
    cal = NewsCalendar.fetch("https://example.invalid/feed.json", tmp_path / "cache.json")
    assert not cal.available
    (tmp_path / "cache.json").write_text(json.dumps(FF))
    cal = NewsCalendar.fetch("https://example.invalid/feed.json", tmp_path / "cache.json")
    assert cal.available and len(cal.events) == 4


def test_calendar_down_means_half_size():
    g = guard(TUESDAY_1045)
    g.calendar = NewsCalendar([], available=False)
    dec = g.check(sig(), fake_engine())
    assert dec.allowed and dec.size_mult == 0.5 and "unavailable" in dec.warnings[0]


# ------------------------------------------------------------------ volatility
def _feed(g, eng, bars):
    for b in bars:
        eng.t += 1
        g.begin_bar(b)
        g.on_bar(b, eng)


def test_shock_candle_pauses_entries():
    g = Guard(GuardConfig(regime_atr_len=5, shock_cooldown_min=60), GOLD)
    eng = SimpleNamespace(t=-1, chart_minutes=15, atr=1.0, times=[], opens=[], highs=[], lows=[], closes=[],
                          levels=[], daily=PeriodLevels(), bar_events=[])
    t0 = ny(2026, 9, 22, 9, 0)
    from datetime import timedelta
    calm = [Bar(t0 + timedelta(minutes=15 * i), 100, 100.5, 99.5, 100) for i in range(8)]
    _feed(g, eng, calm)
    assert g.volatility_block(eng) is None
    spike = Bar(t0 + timedelta(minutes=120), 100, 108, 99.8, 107.5)  # 8x the normal range
    _feed(g, eng, [spike])
    assert "shock" in g.volatility_block(eng)
    after = [Bar(t0 + timedelta(minutes=135 + 15 * i), 107, 107.4, 106.6, 107) for i in range(4)]
    _feed(g, eng, after)
    assert g.volatility_block(eng) is None  # 60 minutes = 4 M15 bars later


def test_extended_day_blocks():
    g = guard(TUESDAY_1045)
    eng = fake_engine()
    for i in range(12):  # ten+ completed days of ~10 range
        eng.daily.update(ny(2026, 9, 1 + i, 12, 0), i, 110.0, 100.0)
    eng.daily.high, eng.daily.low = 116.0, 100.0  # today already 16 = 1.6x ADR
    assert "average range" in g.volatility_block(eng)


# ------------------------------------------------------------------ multi-timeframe
def _state(g, minutes, **kw):
    if g.mtf is None:
        g.mtf = MTFContext(15, [], 3)
    tr = TFTracker(minutes, 3)
    g.mtf.trackers.append(tr)
    st = tr.state
    st.bars = 50
    for k, v in kw.items():
        setattr(st, k, v)
    return st


def test_never_against_the_daily_trend():
    g = guard(TUESDAY_1045)
    _state(g, 1440, trend=-1, top=120, bottom=90)
    dec = g.check(sig(), fake_engine())
    assert not dec.allowed and any("D trend is bearish" in b for b in dec.blocks)


def test_h4_pullback_allowed_only_from_discount():
    g = guard(TUESDAY_1045)
    _state(g, 240, trend=-1, top=110, bottom=90)
    _state(g, 1440, trend=1, top=130, bottom=80)
    assert g.check(sig(entry=95, sl=94, tp=99), fake_engine()).allowed  # 25% of the H4 range, D1 bullish
    dec = g.check(sig(entry=105, sl=104, tp=109), fake_engine())  # 75% -> premium
    assert not dec.allowed and "not in its discount" in dec.blocks[0]


def test_premium_extreme_and_entry_inside_htf_gap():
    g = guard(TUESDAY_1045)
    _state(g, 1440, trend=1, top=101, bottom=90)
    assert any("premium" in b for b in g.check(sig(), fake_engine()).blocks)  # 100 = 91% of the range
    g = guard(TUESDAY_1045)
    _state(g, 240, trend=1, top=130, bottom=90, bear_fvg=(101, 99.5))
    assert any("inside the H4 bearish FVG" in b for b in g.check(sig(), fake_engine()).blocks)


def test_tracker_fvg_lifecycle_and_prev_candle():
    tr = TFTracker(60, 1)
    t0 = ny(2026, 9, 22, 2, 0)
    from datetime import timedelta
    candles = [(100, 101, 99, 100.5), (100.5, 104, 100.4, 103.8), (103.8, 106, 102, 105.5),  # bull FVG 101..102
               (105.5, 105.8, 104, 104.2), (104.2, 104.5, 100.5, 101)]  # the last one fills it
    for i, (o, h, l, c) in enumerate(candles):
        tr.update(Bar(t0 + timedelta(hours=i), o, h, l, c))
        if i == 3:
            assert tr.state.bull_fvg == (102, 101)
            assert tr.state.prev_high == 106
    tr.update(Bar(t0 + timedelta(hours=5), 101, 101.2, 100.8, 101))
    assert tr.state.bull_fvg is None and tr.state.prev_low == 100.5


def test_mtf_context_is_causal():
    df = gold_like(20, seed=4)
    full = MTFContext(15, [60, 240, 1440], 3, "America/New_York", 17)
    snaps = {}
    for i, b in enumerate(bars_from_df(df)):
        full.update(b)
        if i in (300, 700, 1100):
            snaps[i] = [vars(s).copy() for s in full.states()]
    for cut, snap in snaps.items():
        part = MTFContext(15, [60, 240, 1440], 3, "America/New_York", 17)
        for b in bars_from_df(df.iloc[: cut + 1]):
            part.update(b)
        assert [vars(s) for s in part.states()] == snap


# ------------------------------------------------------------------ obstacles
def test_target_capped_before_obstacle():
    g = guard(TUESDAY_1045)
    eng = fake_engine([LiquidityLevel(102.5, 1, "pdh", 0, True)], atr=1.0)
    dec = g.check(sig(), eng)  # entry 100, sl 99, tp 104
    assert dec.allowed and dec.tp == pytest.approx(102.45)
    s2 = apply_decision(sig(), dec)
    assert s2.tp == pytest.approx(102.45) and s2.meta["tp_capped"] and s2.rr == pytest.approx(2.45)


def test_refused_when_obstacle_too_close():
    g = guard(TUESDAY_1045)
    eng = fake_engine([LiquidityLevel(101.0, 1, "pwh", 0, True)])
    dec = g.check(sig(), eng)
    assert not dec.allowed and "previous week high" in dec.blocks[0]


# ------------------------------------------------------------------ breakers & spread
def test_losing_streak_pause_and_daily_limit():
    g = guard(TUESDAY_1045, max_consec_losses=3, loss_pause_bars=5, max_daily_loss_r=10)
    for _ in range(3):
        g.on_trade_closed(-1.0)
    assert "losses in a row" in g.breaker_block()
    g.t = 16
    assert g.breaker_block() is None
    g = guard(TUESDAY_1045, max_consec_losses=0, max_daily_loss_r=2.0)
    g.on_trade_closed(-1.0)
    g.on_trade_closed(-1.0)
    assert "daily loss" in g.breaker_block()


def test_drawdown_halt():
    g = guard(TUESDAY_1045, max_drawdown_r=4, max_daily_loss_r=0, max_weekly_loss_r=0, max_consec_losses=0)
    for r in (2, 2, -1, -1, -1, -1):
        g.on_trade_closed(r)
    assert "halted" in g.breaker_block()


def test_spread_limits():
    g = guard(TUESDAY_1045, max_spread=0.6)
    assert any("spread" in b for b in g.check(sig(), fake_engine(), spread=0.9).blocks)
    g = guard(TUESDAY_1045, max_spread=0.0, max_spread_atr=0.3)
    assert any("of ATR" in b for b in g.check(sig(), fake_engine(atr=1.0), spread=0.5).blocks)
    assert g.check(sig(), fake_engine(atr=1.0), spread=0.2).allowed


# ------------------------------------------------------------------ open trades
def test_trade_actions():
    eng = fake_engine()
    g = guard(ny(2026, 9, 25, 16, 15))  # Friday 16:15
    assert g.trade_action("open", 1, 100, False, 101, eng).kind == "close"
    assert g.trade_action("pending", 1, 0, False, 101, eng).kind == "cancel"
    g = guard(ny(2026, 10, 2, 8, 10))
    g.calendar = NewsCalendar.from_forexfactory(FF)
    assert "Non-Farm" in g.trade_action("open", 1, 100, False, 101, eng).reason
    g.cfg.news_open_action = "protect"
    assert g.trade_action("open", 1, 100, False, 101, eng).kind == "protect"
    assert g.trade_action("open", 1, 100, False, 99, eng) is None  # losing: protect cannot help
    g = guard(TUESDAY_1045, structure_exit=True)
    eng.bar_events = [SimpleNamespace(level="internal", kind="CHoCH", direction=-1)]
    assert g.trade_action("open", 1, 100, False, 101, eng).kind == "close"


# ------------------------------------------------------------------ integration
def test_guarded_backtest_on_gold_like_data():
    df = gold_like(60, seed=1)
    res = run_backtest(df, StrategyConfig(min_score=4, day_tz="America/New_York", day_roll_hour=17),
                       symbol="XAUUSD", timeframe="15m", guard=GuardConfig())
    g = res.metrics["guard"]
    assert g["setups_blocked"] > 0 and "blocks_by_rule" in g
    for tr in res.trades:  # nothing may be held into the weekend
        loc = tr.exit_time.astimezone(NY)
        assert not (loc.weekday() == 4 and loc.hour >= 17) and loc.weekday() != 5
        assert tr.exit_reason in ("tp", "sl", "be", "eod") or ":" in tr.exit_reason
    # every entry was allowed at the time it armed: no fills from setups armed inside a news window
    for s in res.signals:
        close = s.time.astimezone(NY)
        assert not (close.weekday() == 4 and close.hour >= 14)


def test_guarded_backtest_is_causal():
    df = gold_like(40, seed=2)
    cfg = StrategyConfig(min_score=3, day_tz="America/New_York", day_roll_hour=17)
    full = run_backtest(df, cfg, symbol="X", timeframe="15m", guard=GuardConfig())
    cut = int(len(df) * 0.6)
    part = run_backtest(df.iloc[:cut], cfg, symbol="X", timeframe="15m", guard=GuardConfig())
    cut_time = df.index[cut - 1]
    assert [s.id for s in part.signals] == [s.id for s in full.signals if s.time <= cut_time]
