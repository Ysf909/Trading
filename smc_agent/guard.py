"""The risk guard: the agent's caution layer.

Every setup the engine finds must pass the guard before it can become an
order, and every open trade or pending order is re-checked on every candle.
The rules are deterministic and bar-driven (so they run identically in the
backtester, the live agent and the TradingView indicator):

* **Multi-timeframe (top-down):** never against the H4 / D1 trend, enough
  higher timeframes agreeing, no buying at the top of an H4/D1 range (or
  selling at the bottom), no entry inside an opposing HTF fair value gap, and
  targets capped before HTF obstacles (PDH/PWH, HTF swing highs/lows, HTF
  FVGs) - or the trade is refused when the obstacle is too close.
* **News:** calendar events (ForexFactory feed or your CSV) plus the typical
  US release windows (08:30 / 10:00 / 14:00 NY) block new entries and cancel
  pending orders; open trades are closed or protected before high-impact news.
* **Abnormal volatility:** shock candles and gaps (> 3x ATR) pause entries,
  extreme ATR regimes stand aside, elevated regimes trade half size, and an
  over-extended day (range > 1.3x ADR) takes no new trades.
* **Sessions (forex/XAUUSD hours):** no entries around the daily rollover,
  on Friday afternoon, in the first hour after the Sunday open or on holidays;
  positions are closed before the weekend.
* **Losing streaks / drawdown (in R):** pause after consecutive losses, stop
  for the day / week at a loss limit, halt at a maximum drawdown.
* **Execution sanity:** spread too wide relative to ATR (or absolute).
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from .config import GuardConfig, StrategyConfig
from .core.engine import SMCEngine
from .core.mtf import MTFContext, TFState
from .core.structure import ATR
from .core.timeframes import Bucketer
from .core.types import LONG, Bar, Signal
from .news import NewsCalendar

NY = ZoneInfo("America/New_York")
TREND = {1: "bullish", -1: "bearish", 0: "undetermined"}


def _hm(s: str) -> int:
    h, m = s.split(":")
    return int(h) * 60 + int(m)


def _in_range(m: int, start: int, end: int) -> bool:
    return start <= m < end if start <= end else (m >= start or m < end)


@dataclass
class GuardDecision:
    allowed: bool = True
    blocks: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    size_mult: float = 1.0
    tp: float | None = None  # capped target, if any

    def block(self, why: str) -> None:
        self.allowed = False
        self.blocks.append(why)

    def to_dict(self) -> dict[str, Any]:
        return {"allowed": self.allowed, "blocks": self.blocks, "warnings": self.warnings,
                "size_mult": self.size_mult, "tp": self.tp}


@dataclass
class TradeAction:
    kind: str  # cancel | close | protect
    reason: str


class Guard:
    def __init__(self, cfg: GuardConfig, strategy: StrategyConfig, calendar: NewsCalendar | None = None) -> None:
        cfg.validate()
        self.cfg = cfg
        self.strategy = strategy
        self.calendar = calendar
        self.mtf: MTFContext | None = None
        self._slow_atr = ATR(cfg.regime_atr_len)
        self.prev_atr: float | None = None
        self.prev_close: float | None = None
        self.regime: float | None = None
        self.shock_until = -1
        self.shock_reason = ""
        self.t = -1
        self.now: datetime | None = None  # close time of the current candle
        # breakers (R multiples, same as the Pine dashboard)
        self._day = Bucketer(1440, strategy.day_tz, strategy.day_roll_hour)
        self._week = Bucketer(10080, strategy.day_tz, strategy.day_roll_hour)
        self.day_key: int | None = None
        self.week_key: int | None = None
        self.day_r = 0.0
        self.week_r = 0.0
        self.cum_r = 0.0
        self.peak_r = 0.0
        self.consec_losses = 0
        self.pause_until = -1
        self.halted = ""
        self.block_counts: Counter[str] = Counter()

    # ------------------------------------------------------------ bar updates
    def begin_bar(self, bar: Bar) -> None:
        """Call before trades are stepped with ``bar`` (rolls day / week P&L)."""
        self.t += 1
        dk, wk = self._day.key(bar.time), self._week.key(bar.time)
        if dk != self.day_key:
            self.day_key, self.day_r = dk, 0.0
        if wk != self.week_key:
            self.week_key, self.week_r = wk, 0.0

    def on_bar(self, bar: Bar, engine: SMCEngine) -> None:
        """Call after ``engine.update(bar)``."""
        c = self.cfg
        t = engine.t
        self.t = t
        cm = engine.chart_minutes or 1
        self.now = bar.time + timedelta(minutes=cm)
        if self.mtf is None and engine.chart_minutes:
            self.mtf = MTFContext(engine.chart_minutes, c.mtf_timeframes, c.mtf_len,
                                  self.strategy.day_tz, self.strategy.day_roll_hour)
            for i in range(t):  # replay bars seen before the timeframe was known
                self.mtf.update(Bar(engine.times[i], engine.opens[i], engine.highs[i], engine.lows[i],
                                    engine.closes[i]))
        if self.mtf is not None:
            self.mtf.update(bar)

        ref = self._slow_atr.value  # slow ATR *before* this candle: normal displacement is not a shock
        slow = self._slow_atr.update(bar.high, bar.low, bar.close)
        if ref:
            rng = bar.high - bar.low
            gap = abs(bar.open - self.prev_close) if self.prev_close is not None else 0.0
            if rng > c.shock_atr_mult * ref or gap > c.shock_atr_mult * ref:
                self.shock_until = t + max(1, -(-c.shock_cooldown_min // cm))
                kind, size = ("gap", gap) if gap > c.shock_atr_mult * ref else ("candle", rng)
                self.shock_reason = f"{kind} of {size / ref:.1f}x average range at {bar.time:%Y-%m-%d %H:%M}"
        self.prev_atr = engine.atr
        self.prev_close = bar.close
        self.regime = engine.atr / slow if engine.atr and slow else None

    def on_trade_closed(self, r: float) -> None:
        c = self.cfg
        self.cum_r += r
        self.day_r += r
        self.week_r += r
        self.peak_r = max(self.peak_r, self.cum_r)
        if self.peak_r - self.cum_r >= c.max_drawdown_r > 0:
            self.halted = f"max drawdown {self.peak_r - self.cum_r:.1f}R reached - trading halted"
        if r < -1e-9:
            self.consec_losses += 1
            if c.max_consec_losses > 0 and self.consec_losses >= c.max_consec_losses:
                self.pause_until = self.t + c.loss_pause_bars
                self.consec_losses = 0
        elif r > 1e-9:
            self.consec_losses = 0

    # ------------------------------------------------------------ conditions
    def session_block(self, now: datetime) -> str | None:
        c = self.cfg
        if c.market_hours != "forex":
            return None
        loc = now.astimezone(NY)
        wd, m = loc.weekday(), loc.hour * 60 + loc.minute
        if (wd == 4 and m >= 17 * 60) or wd == 5 or (wd == 6 and m < 18 * 60):
            return "session: market closed for the weekend"
        if f"{loc.month:02d}-{loc.day:02d}" in c.holidays:
            return "session: holiday - thin liquidity"
        if _in_range(m, _hm(c.rollover_start), _hm(c.rollover_end)):
            return "session: daily rollover - spreads widen"
        if wd == 4 and m >= _hm(c.friday_cutoff):
            return "session: Friday afternoon - no new trades into the weekend"
        if wd == 6 and m < _hm(c.sunday_open_until):
            return "session: Sunday open - waiting for liquidity"
        return None

    def news_block(self, now: datetime, standard: bool = True) -> str | None:
        c = self.cfg
        if not c.news:
            return None
        cal = self.calendar
        if cal is not None and cal.available:
            e = cal.window_hit(now, c.news_currencies, c.news_min_impact, c.news_before_min, c.news_after_min)
            if e is not None:
                return f"news: {e.currency} {e.title} at {e.time.astimezone(NY):%a %H:%M} NY"
            if c.block_bank_holidays:
                h = cal.holiday(now, c.news_currencies, NY)
                if h is not None:
                    return f"news: {h.currency} bank holiday ({h.title})"
        loc = now.astimezone(NY)
        if standard and loc.weekday() < 5:
            m = loc.hour * 60 + loc.minute
            for w in c.std_windows:
                ev = _hm(w)
                if ev - c.std_before_min <= m <= ev + c.std_after_min:
                    return f"news: typical US release window {w} NY"
        return None

    def news_imminent(self, now: datetime) -> str | None:
        """A known high-impact event within ``news_before_min`` (for open trades)."""
        cal = self.calendar
        c = self.cfg
        if not c.news or cal is None or not cal.available:
            return None
        e = cal.window_hit(now, c.news_currencies, c.news_min_impact, c.news_before_min, 0)
        return None if e is None else f"{e.currency} {e.title} at {e.time.astimezone(NY):%H:%M} NY"

    def breaker_block(self) -> str | None:
        c = self.cfg
        if self.halted:
            return "breaker: " + self.halted
        if self.t < self.pause_until:
            return f"breaker: {c.max_consec_losses} losses in a row - pausing {self.pause_until - self.t} more bars"
        if c.max_daily_loss_r > 0 and self.day_r <= -c.max_daily_loss_r:
            return f"breaker: daily loss {self.day_r:.1f}R - done for the day"
        if c.max_weekly_loss_r > 0 and self.week_r <= -c.max_weekly_loss_r:
            return f"breaker: weekly loss {self.week_r:.1f}R - done for the week"
        return None

    def volatility_block(self, engine: SMCEngine) -> str | None:
        c = self.cfg
        if self.t < self.shock_until:
            return f"volatility: shock ({self.shock_reason}) - cooling down"
        if self.regime is not None and self.regime > c.regime_max_ratio:
            return f"volatility: extreme regime (ATR {self.regime:.1f}x its average)"
        adr = engine.daily.average_range(c.adr_len)
        if adr and c.adr_max_mult > 0:
            day_range = engine.daily.high - engine.daily.low
            if day_range >= c.adr_max_mult * adr:
                return f"volatility: day already moved {day_range / adr:.1f}x its average range"
        return None

    def states(self) -> list[TFState]:
        return [s for s in self.mtf.states() if s.bars > 0] if self.mtf is not None else []

    def obstacles(self, d: int, engine: SMCEngine) -> list[tuple[float, str]]:
        """Reaction points in the path of a trade in direction ``d``."""
        out: list[tuple[float, str]] = []
        names = {"pdh": "previous day high", "pdl": "previous day low",
                 "pwh": "previous week high", "pwl": "previous week low"}
        for lv in engine.levels:
            if lv.side == d and lv.kind in names:
                out.append((lv.price, names[lv.kind]))
        for s in self.states():
            swing = s.swing_high if d == LONG else s.swing_low
            if swing is not None:
                out.append((swing, f"{s.label} swing {'high' if d == LONG else 'low'}"))
            fvg = s.bear_fvg if d == LONG else s.bull_fvg
            if fvg is not None:
                out.append((fvg[1] if d == LONG else fvg[0], f"{s.label} {'bearish' if d == LONG else 'bullish'} FVG"))
        return out

    # ------------------------------------------------------------------ checks
    def check(self, sig: Signal, engine: SMCEngine, spread: float | None = None) -> GuardDecision:
        dec = GuardDecision()
        c = self.cfg
        if not c.enabled:
            return dec
        now = self.now or sig.time
        d = sig.direction
        for why in (self.breaker_block(), self.session_block(now), self.news_block(now), self.volatility_block(engine)):
            if why:
                dec.block(why)
        if c.news and self.calendar is not None and not self.calendar.available:
            dec.warnings.append("news calendar unavailable - standard windows only, half size")
            dec.size_mult = min(dec.size_mult, 0.5)
        if self.regime is not None and self.regime > c.caution_ratio:
            dec.warnings.append(f"elevated volatility (ATR {self.regime:.1f}x average) - half size")
            dec.size_mult = min(dec.size_mult, 0.5)

        atr = engine.atr or 0.0
        if spread is not None and spread > 0:
            if c.max_spread > 0 and spread > c.max_spread:
                dec.block(f"spread: {spread:.5g} above the {c.max_spread:.5g} limit")
            elif c.max_spread_atr > 0 and atr > 0 and spread > c.max_spread_atr * atr:
                dec.block(f"spread: {spread:.5g} is {spread / atr:.0%} of ATR")

        # ---- top-down: every higher timeframe must allow the trade
        states = self.states()
        usable = [s for s in states if s.trend != 0]
        for s in usable:
            if s.trend != -d:
                continue
            if s.minutes in c.mtf_block_opposing:
                dec.block(f"mtf: {s.label} trend is {TREND[s.trend]}")
            elif s.minutes in c.mtf_soft_opposing:
                pos = s.position(sig.entry)
                if pos is None or (pos > 0.5 if d == LONG else pos < 0.5):
                    dec.block(f"mtf: {s.label} trend is {TREND[s.trend]} and entry is not in its "
                              f"{'discount' if d == LONG else 'premium'}")
        if usable:
            aligned = sum(1 for s in usable if s.trend == d)
            need = min(c.mtf_min_aligned, len(usable))
            if aligned < need:
                dec.block(f"mtf: only {aligned}/{len(usable)} higher timeframes agree")
        elif c.mtf_timeframes and self.mtf is not None and self.mtf.trackers:
            dec.block("mtf: higher-timeframe structure not established yet")
        for s in states:
            if s.minutes in c.mtf_block_opposing or s.minutes in c.mtf_soft_opposing:
                pos = s.position(sig.entry)
                if pos is not None:
                    if d == LONG and pos > c.mtf_pd_extreme:
                        dec.block(f"mtf: buying at {pos:.0%} of the {s.label} range (premium)")
                    elif d != LONG and pos < 1 - c.mtf_pd_extreme:
                        dec.block(f"mtf: selling at {pos:.0%} of the {s.label} range (discount)")
            fvg = s.bear_fvg if d == LONG else s.bull_fvg
            if fvg is not None and fvg[1] <= sig.entry <= fvg[0]:
                dec.block(f"mtf: entry inside the {s.label} {'bearish' if d == LONG else 'bullish'} FVG")

        # ---- obstacles between entry and target
        if c.obstacle_check and sig.risk > 0:
            path = [(p, n) for p, n in self.obstacles(d, engine)
                    if (p - sig.entry) * d > 0 and (sig.tp - p) * d > 0]
            if path:
                p, name = min(path, key=lambda pn: (pn[0] - sig.entry) * d)
                new_tp = p - d * c.obstacle_buffer_atr * atr
                rr = (new_tp - sig.entry) * d / sig.risk
                if rr >= self.strategy.min_rr - 1e-9:
                    dec.tp = new_tp
                    dec.warnings.append(f"target capped before the {name} ({p:.6g}) at {rr:.2f}R")
                else:
                    dec.block(f"obstacle: {name} at {p:.6g} only {rr:.2f}R away")

        for b in dec.blocks:
            self.block_counts[b.split(":", 1)[0]] += 1
        return dec

    def trade_action(self, status: str, direction: int, fill_price: float, be_moved: bool,
                     price: float, engine: SMCEngine) -> TradeAction | None:
        """What to do with an existing pending order / open position now."""
        c = self.cfg
        if not c.enabled or self.now is None:
            return None
        now = self.now
        if status == "pending":
            why = self.breaker_block() or self.session_block(now) or self.news_block(now, c.std_cancel_pending)
            if why is None and self.t < self.shock_until:
                why = f"volatility: shock ({self.shock_reason})"
            return TradeAction("cancel", why) if why else None
        if status != "open":
            return None
        if c.market_hours == "forex" and c.weekend_action == "close":
            loc = now.astimezone(NY)
            wd, m = loc.weekday(), loc.hour * 60 + loc.minute
            if (wd == 4 and m >= _hm(c.weekend_close)) or wd == 5 or (wd == 6 and m < 18 * 60):
                return TradeAction("close", "weekend: flat before the market closes")
        news = self.news_imminent(now)
        if news is not None:
            if c.news_open_action == "close":
                return TradeAction("close", f"news: {news}")
            if c.news_open_action == "protect" and not be_moved and (price - fill_price) * direction > 0:
                return TradeAction("protect", f"news: {news} - stop to entry")
        if c.structure_exit:
            for ev in engine.bar_events:
                if ev.level == "internal" and ev.kind == "CHoCH" and ev.direction == -direction:
                    return TradeAction("close", "structure: internal CHoCH against the trade")
        return None

    # --------------------------------------------------------------- context
    def context(self, engine: SMCEngine) -> dict[str, Any]:
        """Risk picture for the Claude reviewer and the `scan` command."""
        now = self.now
        price = engine.closes[-1] if engine.closes else None
        upcoming = []
        if self.calendar is not None and now is not None:
            upcoming = [e.to_dict() for e in self.calendar.upcoming(now, self.cfg.news_currencies, "medium", 24 * 60)][:8]
        adr = engine.daily.average_range(self.cfg.adr_len)
        return {
            "timeframes": self.mtf.to_list(price) if self.mtf is not None else [],
            "volatility_regime_atr_ratio": None if self.regime is None else round(self.regime, 2),
            "shock_cooldown_bars_left": max(0, self.shock_until - self.t),
            "last_shock": self.shock_reason or None,
            "today_range_vs_adr": None if not adr else round((engine.daily.high - engine.daily.low) / adr, 2),
            "session_block": self.session_block(now) if now else None,
            "news_block": self.news_block(now) if now else None,
            "news_calendar": None if self.calendar is None else
            {"available": self.calendar.available, "source": self.calendar.source, "upcoming_24h": upcoming},
            "breakers": {"day_r": round(self.day_r, 2), "week_r": round(self.week_r, 2),
                         "drawdown_r": round(self.peak_r - self.cum_r, 2), "paused": self.breaker_block()},
        }


def apply_decision(sig: Signal, dec: GuardDecision) -> Signal:
    """Copy of ``sig`` with the guard's capped target and warnings (if any)."""
    if dec.tp is None and not dec.warnings:
        return sig
    tp = sig.tp if dec.tp is None else dec.tp
    rr = (tp - sig.entry) * sig.direction / sig.risk if sig.risk > 0 else sig.rr
    meta = dict(sig.meta)
    if dec.tp is not None:
        meta.update(tp_capped=True, tp_original=sig.tp)
    return replace(sig, tp=tp, rr=rr, reasons=[*sig.reasons, *dec.warnings], meta=meta)
