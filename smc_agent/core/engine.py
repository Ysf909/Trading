"""The SMC / ICT analysis engine.

``SMCEngine.update(bar)`` consumes one *closed* candle and returns any trade
signals that armed on that candle. Per bar, in this exact order (the Pine
port follows the same order):

1.  ATR, higher-timeframe bias, killzones
2.  session ranges (Asia / London) and previous-day high/low -> liquidity
3.  internal + swing structure: pivots, BOS / CHoCH
4.  pivots -> liquidity levels, equal highs / lows
5.  liquidity sweeps (a tracked level traded through)
6.  order blocks from every structure break
7.  fair value gaps
8.  zone touches / invalidation
9.  entry models:

    * **reversal** (ICT 2022 model): sell-side (buy-side) liquidity is
      taken -> an internal bullish (bearish) market-structure shift within
      ``mss_window`` bars -> limit entry in the displacement FVG (or the MSS
      order block), stop beyond the sweep extreme.
    * **continuation**: internal BOS in the direction of swing structure ->
      limit entry in the leg's FVG / OB, stop beyond the last internal
      higher-low (lower-high).

    Targets are the nearest opposing liquidity pool that gives at least
    ``min_rr`` (capped by ``max_rr``), otherwise a fixed ``rr_target``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

import pandas as pd

from ..config import KILLZONES, StrategyConfig
from .sessions import PeriodLevels, SessionRange, in_window, ny_minutes
from .structure import ATR, HTFBias, StructureTracker
from .timeframes import auto_htf_minutes, timeframe_minutes
from .types import (
    LONG,
    SHORT,
    Bar,
    LiquidityLevel,
    Pivot,
    Signal,
    StructureEvent,
    SweepEvent,
    Zone,
    side_name,
)

HISTORY_CAP = 5000
SCORE_MAX = 10


def grade_for(score: int) -> str:
    if score >= 8:
        return "A+"
    if score >= 6:
        return "A"
    if score >= 4:
        return "B"
    return "C"


@dataclass
class _ReversalState:
    stage: int = 0  # 0 idle, 1 liquidity taken, 2 MSS confirmed (awaiting entry zone)
    ext: float = 0.0  # sweep extreme
    ext_bar: int = -1
    major: bool = False
    kinds: set[str] = field(default_factory=set)
    mss_bar: int = -1
    mss_kind: str = ""
    mss_price: float = 0.0
    mss_ob: Zone | None = None


class SMCEngine:
    def __init__(self, cfg: StrategyConfig | None = None, symbol: str = "", timeframe: str = "") -> None:
        self.cfg = cfg or StrategyConfig()
        self.cfg.validate()
        self.symbol = symbol
        self.timeframe = timeframe
        self.t = -1

        self.times: list = []
        self.opens: list[float] = []
        self.highs: list[float] = []
        self.lows: list[float] = []
        self.closes: list[float] = []
        self.volumes: list[float] = []

        c = self.cfg
        self._atr = ATR(c.atr_len)
        self.atr: float | None = None
        self.internal = StructureTracker(c.internal_len, "internal")
        self.swing = StructureTracker(c.swing_len, "swing")
        self.htf: HTFBias | None = None
        self.htf_minutes = c.htf_minutes
        self.chart_minutes: int | None = None
        if timeframe:
            try:
                self.chart_minutes = timeframe_minutes(timeframe)
            except ValueError:
                self.chart_minutes = None
        if not self.htf_minutes and self.chart_minutes:
            self.htf_minutes = auto_htf_minutes(self.chart_minutes)
        if self.htf_minutes:
            self.htf = HTFBias(self.htf_minutes, c.htf_len, c.day_tz, c.day_roll_hour)
        self.htf_trend = 0
        self.daily = PeriodLevels(1440, c.day_tz, c.day_roll_hour)
        self.weekly = PeriodLevels(10080, c.day_tz, c.day_roll_hour)
        self.sessions = {name: SessionRange(name) for name in ("asia", "london")}
        self.killzones_now: list[str] = []

        self.obs: list[Zone] = []
        self.fvgs: list[Zone] = []
        self.levels: list[LiquidityLevel] = []
        self.rev = {LONG: _ReversalState(), SHORT: _ReversalState()}

        # history (charting, AI context, debugging)
        self.zone_history: list[Zone] = []
        self.structure_history: list[StructureEvent] = []
        self.sweep_history: list[SweepEvent] = []
        self.pivot_history: list[tuple[str, Pivot]] = []
        self.eq_history: list[tuple[int, int, float, int]] = []  # bar_a, bar_b, price, side
        self.signals: list[Signal] = []
        self.rejections: dict[str, int] = {}

        # per-bar scratch
        self.bar_events: list[StructureEvent] = []
        self.bar_sweeps: list[SweepEvent] = []
        self._new_obs: dict[tuple[str, int], Zone] = {}

    # ------------------------------------------------------------------ feed
    def run(self, df: pd.DataFrame) -> list[Signal]:
        out: list[Signal] = []
        for bar in bars_from_df(df):
            out.extend(self.update(bar))
        return out

    def update(self, bar: Bar) -> list[Signal]:
        c = self.cfg
        self.t += 1
        t = self.t
        self.times.append(bar.time)
        self.opens.append(bar.open)
        self.highs.append(bar.high)
        self.lows.append(bar.low)
        self.closes.append(bar.close)
        self.volumes.append(bar.volume)
        h, l, cl = bar.high, bar.low, bar.close

        # 1. volatility, HTF bias, killzones
        self.atr = self._atr.update(h, l, cl)
        if self.chart_minutes is None and t == 1:  # unknown timeframe: infer from bar spacing
            self.chart_minutes = max(1, round((bar.time - self.times[0]).total_seconds() / 60))
        if self.htf is None and t == 1:
            self.htf_minutes = auto_htf_minutes(self.chart_minutes or 1)
            self.htf = HTFBias(self.htf_minutes, c.htf_len, c.day_tz, c.day_roll_hour)
            self.htf.update(Bar(self.times[0], self.opens[0], self.highs[0], self.lows[0], self.closes[0]))
        if self.htf is not None:
            self.htf_trend = self.htf.update(bar)
        nym = ny_minutes(bar.time)
        self.killzones_now = [k for k in KILLZONES if in_window(nym, k)]

        # 2. session ranges / previous day -> liquidity
        for name, sr in self.sessions.items():
            done = sr.update(nym, t, h, l)
            if done is not None:
                self._replace_level(f"{name}_high", done[0], LONG, done[2])
                self._replace_level(f"{name}_low", done[1], SHORT, done[2])
        done = self.daily.update(bar.time, t, h, l)
        if done is not None:
            self._replace_level("pdh", done[0], LONG, done[2])
            self._replace_level("pdl", done[1], SHORT, done[2])
        done = self.weekly.update(bar.time, t, h, l)
        if done is not None:
            self._replace_level("pwh", done[0], LONG, done[2])
            self._replace_level("pwl", done[1], SHORT, done[2])

        # 3. structure
        int_events = self.internal.update(t, self.highs, self.lows, cl, self.times)
        sw_events = self.swing.update(t, self.highs, self.lows, cl, self.times)
        self.bar_events = int_events + sw_events
        self.structure_history.extend(self.bar_events)

        # 4. pivots -> liquidity
        self._register_pivots(self.internal, "internal", major=False)
        self._register_pivots(self.swing, "swing", major=True)

        # 5. sweeps
        swept = self._sweep(t, h, l, cl)

        # 6. order blocks
        self._new_obs = {}
        for ev in self.bar_events:
            ob = self._make_ob(ev)
            if ob is not None:
                self._new_obs[(ev.level, ev.direction)] = ob

        # 7. fair value gaps
        self._detect_fvg(t)

        # 8. zone maintenance
        self._maintain_zones(t, h, l, cl)

        # 9. entry models
        signals: list[Signal] = []
        if self.atr is None or t < 2 * max(c.swing_len, c.internal_len):
            return signals
        int_break = {ev.direction: ev for ev in int_events}
        for d in (LONG, SHORT):
            sig = None
            if c.models in ("reversal", "both"):
                sig = self._reversal(d, swept, int_break.get(d))
            if sig is None and c.models in ("continuation", "both") and d in int_break:
                sig = self._continuation(d, int_break[d])
            if sig is not None:
                signals.append(sig)
        self.signals.extend(signals)
        self._trim_history()
        return signals

    # ------------------------------------------------------------ liquidity
    def _add_level(self, price: float, side: int, kind: str, bar: int, major: bool) -> None:
        for lv in self.levels:
            if lv.side == side and lv.price == price:
                if major and not lv.major:
                    lv.major, lv.kind = True, kind
                return
        self.levels.append(LiquidityLevel(price, side, kind, bar, major))
        same_side = [lv for lv in self.levels if lv.side == side]
        if len(same_side) > self.cfg.max_levels:
            oldest = min(same_side, key=lambda lv: lv.bar)
            self.levels.remove(oldest)

    def _replace_level(self, kind: str, price: float, side: int, bar: int) -> None:
        self.levels = [lv for lv in self.levels if lv.kind != kind]
        self._add_level(price, side, kind, bar, major=True)

    def _register_pivots(self, tracker: StructureTracker, kind: str, major: bool) -> None:
        tol = (self.atr or 0.0) * self.cfg.eq_tolerance_atr
        if tracker.new_high is not None:
            p = tracker.new_high
            self.pivot_history.append((kind, p))
            self._add_level(p.price, LONG, kind, p.bar, major)
            prev = tracker.prev_high
            if kind == "internal" and prev is not None and tol > 0 and abs(p.price - prev.price) <= tol:
                self._add_level(max(p.price, prev.price), LONG, "eqh", p.bar, True)
                self.eq_history.append((prev.bar, p.bar, max(p.price, prev.price), LONG))
        if tracker.new_low is not None:
            p = tracker.new_low
            self.pivot_history.append((kind, p))
            self._add_level(p.price, SHORT, kind, p.bar, major)
            prev = tracker.prev_low
            if kind == "internal" and prev is not None and tol > 0 and abs(p.price - prev.price) <= tol:
                self._add_level(min(p.price, prev.price), SHORT, "eql", p.bar, True)
                self.eq_history.append((prev.bar, p.bar, min(p.price, prev.price), SHORT))

    def _sweep(self, t: int, h: float, l: float, cl: float) -> list[LiquidityLevel]:
        swept: list[LiquidityLevel] = []
        self.bar_sweeps = []
        for lv in self.levels:
            if (lv.side == LONG and h > lv.price) or (lv.side == SHORT and l < lv.price):
                lv.swept_bar = t
                swept.append(lv)
                rejected = cl < lv.price if lv.side == LONG else cl > lv.price
                self.bar_sweeps.append(SweepEvent(t, lv.side, lv.price, lv.kind, lv.major, rejected))
        if swept:
            self.levels = [lv for lv in self.levels if lv.swept_bar < 0]
            self.sweep_history.extend(self.bar_sweeps)
        return swept

    # ---------------------------------------------------------------- zones
    def _make_ob(self, ev: StructureEvent) -> Zone | None:
        t = self.t
        lo = max(ev.pivot_bar, t - self.cfg.ob_lookback, 0)
        best = t
        if ev.direction == LONG:
            for j in range(t, lo - 1, -1):
                if self.lows[j] < self.lows[best]:
                    best = j
        else:
            for j in range(t, lo - 1, -1):
                if self.highs[j] > self.highs[best]:
                    best = j
        for z in self.obs:
            if z.direction == ev.direction and z.bar == best:
                if ev.level == "swing":
                    z.level = "swing"
                return z
        ob = Zone("OB", ev.direction, self.highs[best], self.lows[best], best, t, ev.level)
        self._push_zone(self.obs, ob)
        return ob

    def _detect_fvg(self, t: int) -> None:
        if t < 2 or self.atr is None:
            return
        min_gap = self.cfg.fvg_min_atr * self.atr
        h, l, c = self.highs, self.lows, self.closes
        if l[t] > h[t - 2] and c[t - 1] > h[t - 2] and (l[t] - h[t - 2]) >= min_gap:
            self._push_zone(self.fvgs, Zone("FVG", LONG, l[t], h[t - 2], t - 1, t))
        if h[t] < l[t - 2] and c[t - 1] < l[t - 2] and (l[t - 2] - h[t]) >= min_gap:
            self._push_zone(self.fvgs, Zone("FVG", SHORT, l[t - 2], h[t], t - 1, t))

    def _push_zone(self, bucket: list[Zone], z: Zone) -> None:
        bucket.append(z)
        self.zone_history.append(z)
        same = [x for x in bucket if x.direction == z.direction]
        if len(same) > self.cfg.max_zones:
            old = same[0]
            old.end = self.t
            bucket.remove(old)

    def _maintain_zones(self, t: int, h: float, l: float, cl: float) -> None:
        c = self.cfg
        for bucket, max_age in ((self.obs, c.ob_max_age), (self.fvgs, c.fvg_max_age)):
            for z in bucket:
                if z.created == t:
                    continue
                if z.direction == LONG:
                    if l <= z.top:
                        z.touched = True
                    dead = (l <= z.bottom) if z.kind == "FVG" else (cl < z.bottom)
                else:
                    if h >= z.bottom:
                        z.touched = True
                    dead = (h >= z.top) if z.kind == "FVG" else (cl > z.top)
                if dead or t - z.created > max_age:
                    z.end = t
            bucket[:] = [z for z in bucket if z.end < 0]

    def _latest_fvg(self, d: int, since: int) -> Zone | None:
        for z in reversed(self.fvgs):
            if z.direction == d and z.created >= since and z.active:
                return z
        return None

    # --------------------------------------------------------- entry models
    def _reversal(self, d: int, swept: list[LiquidityLevel], brk: StructureEvent | None) -> Signal | None:
        c, t, st = self.cfg, self.t, self.rev[d]
        ext_now = self.lows[t] if d == LONG else self.highs[t]
        taken = [lv for lv in swept if lv.side == -d]
        beyond = st.stage > 0 and ((ext_now < st.ext) if d == LONG else (ext_now > st.ext))
        if taken:
            major = any(lv.major for lv in taken)
            if st.stage == 0:
                st.stage, st.ext, st.ext_bar = 1, ext_now, t
                st.major, st.kinds = major, {lv.kind for lv in taken}
            elif beyond:
                st.stage, st.ext, st.ext_bar = 1, ext_now, t
                st.major = st.major or major
                st.kinds |= {lv.kind for lv in taken}
            elif st.stage == 1:
                st.major = st.major or major
                st.kinds |= {lv.kind for lv in taken}
        elif beyond:
            st.stage, st.ext, st.ext_bar = 1, ext_now, t

        if st.stage == 1 and t - st.ext_bar > c.mss_window:
            st.stage = 0
        if st.stage == 1 and brk is not None:
            st.stage, st.mss_bar, st.mss_kind, st.mss_price = 2, t, brk.kind, brk.price
            st.mss_ob = self._new_obs.get(("internal", d))
        if st.stage != 2:
            return None

        zone = self._latest_fvg(d, since=st.ext_bar + 2)
        if zone is None:
            if t - st.mss_bar < c.fvg_wait:
                return None
            if c.use_ob_entry and st.mss_ob is not None and st.mss_ob.active:
                zone = st.mss_ob
            else:
                st.stage = 0
                self._reject("no_entry_zone")
                return None
        st.stage = 0
        sl = st.ext - d * c.sl_buffer_atr * (self.atr or 0.0)
        liq = ", ".join(sorted(st.kinds)) or "range extreme"
        reasons = [
            f"Took {'sell' if d == LONG else 'buy'}-side liquidity ({liq}) at {st.ext:.6g}",
            f"{'Bullish' if d == LONG else 'Bearish'} MSS ({st.mss_kind}) through {st.mss_price:.6g}",
        ]
        return self._build_signal(d, "reversal", zone, sl, st.major, reasons, (st.ext, st.ext_bar))

    def _continuation(self, d: int, brk: StructureEvent) -> Signal | None:
        c = self.cfg
        if brk.kind != "BOS" or self.swing.trend != d:
            return None
        anchor = self.internal.low if d == LONG else self.internal.high
        if anchor is None:
            return None
        zone = self._latest_fvg(d, since=anchor.bar + 2)
        if zone is None and c.use_ob_entry:
            ob = self._new_obs.get(("internal", d))
            zone = ob if ob is not None and ob.active else None
        if zone is None:
            self._reject("no_entry_zone")
            return None
        sl = anchor.price - d * c.sl_buffer_atr * (self.atr or 0.0)
        reasons = [
            f"Swing structure {'bullish' if d == LONG else 'bearish'}; internal BOS through {brk.price:.6g}",
            f"Protected {'higher low' if d == LONG else 'lower high'} at {anchor.price:.6g}",
        ]
        return self._build_signal(d, "continuation", zone, sl, False, reasons, (anchor.price, anchor.bar))

    def _target(self, d: int, entry: float, risk: float) -> tuple[float, str]:
        c = self.cfg
        if c.tp_mode == "liquidity":
            pools = sorted(
                (lv for lv in self.levels if lv.side == d and (lv.price - entry) * d > 0),
                key=lambda lv: (lv.price - entry) * d,
            )
            for lv in pools:
                rr = (lv.price - entry) * d / risk
                if rr > c.max_rr:
                    break
                if rr >= c.min_rr:
                    return lv.price, lv.kind
        return entry + d * c.rr_target * risk, f"{c.rr_target:g}R"

    def ote_retracement(self, d: int, entry: float, origin: float, origin_bar: int) -> float | None:
        """How deep ``entry`` sits in the leg from ``origin`` to its extreme (0..1).

        ICT's optimal trade entry is the 62-79% retracement of that leg."""
        t = self.t
        lo = max(0, origin_bar)
        if d == LONG:
            far = max(self.highs[lo : t + 1])
            return (far - entry) / (far - origin) if far > origin else None
        far = min(self.lows[lo : t + 1])
        return (entry - far) / (origin - far) if origin > far else None

    def _build_signal(
        self, d: int, model: str, zone: Zone, sl: float, major_sweep: bool, reasons: list[str],
        leg: tuple[float, int] | None = None,
    ) -> Signal | None:
        c, t = self.cfg, self.t
        atr = self.atr or 0.0
        if c.entry_mode == "ce":
            entry = zone.mid
        else:
            entry = zone.top if d == LONG else zone.bottom
        risk = (entry - sl) * d
        if risk <= 0 or atr <= 0:
            self._reject("bad_risk")
            return None
        risk_atr = risk / atr
        if risk_atr < c.min_risk_atr or risk_atr > c.max_risk_atr:
            self._reject("risk_out_of_range")
            return None
        tp, tp_kind = self._target(d, entry, risk)
        rr = (tp - entry) * d / risk
        if rr < c.min_rr - 1e-9:
            self._reject("rr_below_min")
            return None

        eq = self.swing.equilibrium
        pd_ok = eq is not None and ((entry < eq) if d == LONG else (entry > eq))
        in_kz = any(k in self.killzones_now for k in c.killzones)
        if zone.kind == "FVG":
            body = abs(self.closes[zone.bar] - self.opens[zone.bar])
            confluence = any(ob.direction == d and ob.overlaps(zone.top, zone.bottom) for ob in self.obs)
        else:
            body = max(abs(self.closes[j] - self.opens[j]) for j in range(zone.bar, t + 1))
            confluence = any(f.direction == d and f.overlaps(zone.top, zone.bottom) for f in self.fvgs)
        displacement = body >= c.displacement_atr * atr
        retr = self.ote_retracement(d, entry, leg[0], leg[1]) if leg is not None else None
        ote = retr is not None and 0.62 <= retr <= 0.79

        features = {
            "htf_aligned": float(self.htf_trend == d),
            "htf_opposed": float(self.htf_trend == -d),
            "swing_aligned": float(self.swing.trend == d),
            "killzone": float(in_kz),
            "pd_ok": float(pd_ok),
            "major_sweep": float(major_sweep),
            "zone_confluence": float(confluence),
            "displacement": float(displacement),
            "model_reversal": float(model == "reversal"),
            "zone_fvg": float(zone.kind == "FVG"),
            "is_long": float(d == LONG),
            "ote": float(ote),
            "rr": rr,
            "risk_atr": risk_atr,
        }
        # confluence score, 0..10 (weights mirrored in the Pine script)
        score = (
            2 * int(self.htf_trend == d)
            + 2 * int(pd_ok)
            + int(major_sweep)
            + int(confluence)
            + int(in_kz)
            + int(displacement)
            + int(self.swing.trend == d)
            + int(rr >= 3.0)
        )

        if c.htf_filter and self.htf_trend != d:
            self._reject("htf_filter")
            return None
        if c.killzone_filter and not in_kz:
            self._reject("killzone_filter")
            return None
        if score < c.min_score:
            self._reject("min_score")
            return None

        reasons = list(reasons)
        reasons.append(
            f"Entry {zone.kind} {'CE' if c.entry_mode == 'ce' else 'edge'} {entry:.6g}"
            + (" + OB/FVG overlap" if confluence else "")
        )
        reasons.append(f"Target {tp_kind} {tp:.6g} ({rr:.2f}R), stop {sl:.6g}")
        if self.htf_trend == d:
            reasons.append("HTF bias aligned")
        elif self.htf_trend == -d:
            reasons.append("Counter HTF bias")
        if pd_ok:
            reasons.append("Entry in " + ("discount" if d == LONG else "premium"))
        if in_kz:
            reasons.append("Inside killzone: " + "/".join(k for k in self.killzones_now if k in c.killzones))
        if displacement:
            reasons.append("Displacement candle")
        if ote:
            reasons.append(f"Entry in the OTE ({retr:.0%} retracement)")

        time = self.times[t]
        return Signal(
            id=f"{self.symbol}|{self.timeframe}|{time:%Y%m%d%H%M}|{side_name(d)}",
            symbol=self.symbol,
            timeframe=self.timeframe,
            time=time,
            bar=t,
            direction=d,
            model=model,
            entry=entry,
            sl=sl,
            tp=tp,
            rr=rr,
            risk_atr=risk_atr,
            atr=atr,
            expiry_bars=c.entry_expiry,
            score=score,
            grade=grade_for(score),
            features=features,
            reasons=reasons,
            zone=zone,
            meta={"tp_kind": tp_kind, "retracement": None if retr is None else round(retr, 3),
                  "tp1": entry + d * c.tp1_r * risk if 0 < c.tp1_r < rr else None},
        )

    def _reject(self, why: str) -> None:
        self.rejections[why] = self.rejections.get(why, 0) + 1

    def _trim_history(self) -> None:
        for name in ("zone_history", "structure_history", "sweep_history", "pivot_history", "eq_history"):
            lst = getattr(self, name)
            if len(lst) > HISTORY_CAP:
                del lst[: len(lst) - HISTORY_CAP]

    # ------------------------------------------------------------- snapshot
    def snapshot(self, max_items: int = 6) -> dict[str, Any]:
        """A compact, JSON-serialisable view of the current market state."""
        t = self.t
        if t < 0:
            return {}
        close = self.closes[t]

        def zone_d(z: Zone) -> dict[str, Any]:
            return {
                "type": z.kind,
                "side": side_name(z.direction),
                "top": z.top,
                "bottom": z.bottom,
                "level": z.level or None,
                "touched": z.touched,
                "age_bars": t - z.created,
            }

        def nearest(zones: Iterable[Zone]) -> list[dict[str, Any]]:
            zs = sorted(zones, key=lambda z: abs(z.mid - close))[:max_items]
            return [zone_d(z) for z in zs]

        above = sorted((lv for lv in self.levels if lv.price > close), key=lambda lv: lv.price)
        below = sorted((lv for lv in self.levels if lv.price < close), key=lambda lv: -lv.price)
        eq = self.swing.equilibrium

        def piv(p: Pivot | None) -> dict[str, Any] | None:
            return None if p is None else {"price": p.price, "label": p.label, "bars_ago": t - p.bar}

        trend_name = {1: "bullish", -1: "bearish", 0: "undetermined"}
        recent_events = [
            {
                "bars_ago": t - e.bar,
                "level": e.level,
                "type": e.kind,
                "side": side_name(e.direction),
                "price": e.price,
            }
            for e in self.structure_history[-8:]
        ]
        recent_sweeps = [
            {
                "bars_ago": t - s.bar,
                "liquidity": "buy-side" if s.side == LONG else "sell-side",
                "kind": s.kind,
                "price": s.price,
                "major": s.major,
                "rejected": s.rejected,
            }
            for s in self.sweep_history[-8:]
        ]
        return {
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "time": self.times[t].isoformat(),
            "close": close,
            "atr": self.atr,
            "htf_trend": trend_name[self.htf_trend],
            "swing_trend": trend_name[self.swing.trend],
            "internal_trend": trend_name[self.internal.trend],
            "swing_high": piv(self.swing.high),
            "swing_low": piv(self.swing.low),
            "internal_high": piv(self.internal.high),
            "internal_low": piv(self.internal.low),
            "dealing_range": {
                "top": self.swing.trail_top,
                "bottom": self.swing.trail_bottom,
                "equilibrium": eq,
                "price_zone": None if eq is None else ("premium" if close > eq else "discount"),
            },
            "killzones_active": self.killzones_now,
            "order_blocks": nearest(self.obs),
            "fair_value_gaps": nearest(self.fvgs),
            "liquidity_above": [
                {"price": lv.price, "kind": lv.kind, "major": lv.major} for lv in above[:max_items]
            ],
            "liquidity_below": [
                {"price": lv.price, "kind": lv.kind, "major": lv.major} for lv in below[:max_items]
            ],
            "recent_structure": recent_events,
            "recent_sweeps": recent_sweeps,
            "setup_state": {
                "long": ["idle", "sell-side taken", "MSS confirmed"][self.rev[LONG].stage],
                "short": ["idle", "buy-side taken", "MSS confirmed"][self.rev[SHORT].stage],
            },
        }

    def recent_bars(self, n: int) -> list[dict[str, Any]]:
        start = max(0, self.t - n + 1)
        return [
            {
                "t": self.times[i].strftime("%Y-%m-%d %H:%M"),
                "o": self.opens[i],
                "h": self.highs[i],
                "l": self.lows[i],
                "c": self.closes[i],
            }
            for i in range(start, self.t + 1)
        ]


def bars_from_df(df: pd.DataFrame) -> Iterable[Bar]:
    """Yield ``Bar`` objects from an OHLCV frame indexed by UTC timestamps."""
    times = df.index.to_pydatetime()
    o, h, l, c = (df[k].astype(float).tolist() for k in ("open", "high", "low", "close"))
    v = df["volume"].astype(float).tolist() if "volume" in df else [0.0] * len(df)
    sp = df["spread"].astype(float).tolist() if "spread" in df else [0.0] * len(df)
    for i in range(len(df)):
        yield Bar(times[i], o[i], h[i], l[i], c[i], v[i], sp[i])
