"""The live trading agent.

Loop per market: fetch closed candles -> update the SMC engine -> for every
new setup run the decision pipeline:

    learned edge filter -> risk manager -> Claude review -> size -> broker

Everything is journaled to JSONL and optionally pushed to Telegram/Discord.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from .config import AppConfig, MarketConfig, guard_for
from .core.engine import SMCEngine, bars_from_df
from .core.timeframes import timeframe_minutes
from .core.types import LONG, SHORT, Signal
from .data.feeds import Feed, make_feed
from .execution.broker import Broker, PaperBroker
from .guard import Guard, GuardDecision, apply_decision
from .news import NewsCalendar
from .notify import Journal, Notifier, format_signal
from .risk import RiskManager

log = logging.getLogger(__name__)


def make_broker(cfg: AppConfig) -> Broker:
    kind = cfg.broker.kind.lower()
    if kind == "paper":
        return PaperBroker(
            starting_equity=cfg.broker.starting_equity,
            commission_pct=cfg.costs.commission_pct,
            slippage_pct=cfg.costs.slippage_pct,
            breakeven_at_r=cfg.strategy.breakeven_at_r,
            tp1_r=cfg.strategy.tp1_r,
            tp1_pct=cfg.strategy.tp1_pct,
            state_path=cfg.broker.state_path,
        )
    if kind == "ccxt":
        from .execution.ccxt_broker import CCXTBroker

        return CCXTBroker(cfg.broker)
    if kind == "mt5":
        from .execution.mt5_broker import MT5Broker

        return MT5Broker(cfg.broker, tp1_r=cfg.strategy.tp1_r, tp1_pct=cfg.strategy.tp1_pct)
    raise ValueError(f"unknown broker {cfg.broker.kind!r}")


def _norm(sym: str) -> str:
    return sym.split(":")[-1].replace("/", "").replace("-", "").replace("_", "").upper()


@dataclass
class MarketRuntime:
    cfg: MarketConfig
    feed: Feed
    engine: SMCEngine
    guard: Guard | None
    minutes: int
    last_time: pd.Timestamp | None = None
    errors: int = 0
    stale: bool = False


class TradingAgent:
    def __init__(
        self,
        cfg: AppConfig,
        broker: Broker | None = None,
        feeds: dict[str, Feed] | None = None,
        analyst: Any | None = None,
        model: Any | None = None,
        notifier: Notifier | None = None,
        self_signals: bool = True,
        calendar: NewsCalendar | None = None,
    ) -> None:
        self.cfg = cfg
        self.broker = broker or make_broker(cfg)
        self.risk = RiskManager(cfg.risk)
        self.journal = Journal(cfg.journal_path)
        self.notifier = notifier or Notifier(cfg.notify)
        self.self_signals = self_signals
        self.lock = threading.RLock()
        state_dir = Path(cfg.journal_path).parent
        self.halt_file = state_dir / "HALT"
        self.kill_switch_poll_s = 5.0
        self.flatten_file = state_dir / "FLATTEN"

        self.model = model
        if self.model is None and cfg.learner.enabled:
            from .ai.learner import EdgeModel

            self.model = EdgeModel.load(cfg.learner.model_path)
        self.analyst = analyst
        if self.analyst is None and cfg.ai.enabled:
            from .ai.analyst import ClaudeAnalyst

            self.analyst = ClaudeAnalyst(cfg.ai)

        g = cfg.guard
        self.calendar = calendar
        if self.calendar is None and g.enabled and g.news:
            self.calendar = self.load_calendar()
        self._calendar_at = time.time()

        self.markets: list[MarketRuntime] = []
        for m in cfg.markets:
            feed = (feeds or {}).get(m.symbol) or make_feed(m, cfg.broker)
            eng = SMCEngine(cfg.strategy, m.symbol, m.timeframe)
            mg = guard_for(cfg, m)
            guard = Guard(mg, cfg.strategy, self.calendar) if mg.enabled else None
            self.markets.append(MarketRuntime(m, feed, eng, guard, timeframe_minutes(m.timeframe)))

    # --------------------------------------------------------------- calendar
    def load_calendar(self) -> NewsCalendar:
        g = self.cfg.guard
        cal = NewsCalendar.fetch(g.news_url, g.news_cache) if g.news_url else NewsCalendar([], False, "none")
        if g.news_file and Path(g.news_file).exists():
            cal = cal.merge(NewsCalendar.from_csv(g.news_file))
        log.info("news calendar: %s (%d events, available=%s)", cal.source, len(cal.events), cal.available)
        if not cal.available:
            self.notifier.send("News calendar unavailable - trading with standard release windows and half size.")
        return cal

    def refresh_calendar(self, every_s: float = 3600.0) -> None:
        if self.calendar is None or time.time() - self._calendar_at < every_s:
            return
        self._calendar_at = time.time()
        self.calendar = self.load_calendar()
        for m in self.markets:
            if m.guard is not None:
                m.guard.calendar = self.calendar

    # ------------------------------------------------------------ kill switch
    def halted(self) -> bool:
        return self.halt_file.exists()

    def check_kill_switch(self) -> None:
        """``touch state/FLATTEN`` closes everything and halts; ``state/HALT`` stops new entries."""
        if not self.flatten_file.exists():
            return
        with self.lock:
            for sym in sorted(self.broker.symbols()):
                for ev in self.broker.cancel_pending(sym, "kill switch") + self.broker.close_position(sym, "kill switch"):
                    self._broker_event(ev, None)
            self.flatten_file.replace(self.halt_file)
        self.notifier.send("KILL SWITCH: all positions closed, orders cancelled, agent halted (delete state/HALT to resume).")
        self.journal.write("kill_switch")

    # ----------------------------------------------------------------- data
    def warmup(self) -> None:
        for m in self.markets:
            df = m.feed.history(self.cfg.warmup_bars)
            for bar in bars_from_df(df):
                if m.guard is not None:
                    m.guard.begin_bar(bar)
                m.engine.update(bar)
                if m.guard is not None:
                    m.guard.on_bar(bar, m.engine)
            m.last_time = df.index[-1] if len(df) else None
            snap = m.engine.snapshot()
            mtf = ", ".join(f"{s.label} {'+' if s.trend > 0 else '-' if s.trend < 0 else '0'}"
                            for s in (m.guard.states() if m.guard else []))
            log.info("%s %s warmed up on %d bars | HTF %s, swing %s, internal %s | MTF %s", m.cfg.symbol,
                     m.cfg.timeframe, len(df), snap.get("htf_trend"), snap.get("swing_trend"),
                     snap.get("internal_trend"), mtf or "n/a")
            self.journal.write("warmup", symbol=m.cfg.symbol, timeframe=m.cfg.timeframe, bars=len(df))
        self.reconcile()

    def reconcile(self) -> None:
        """Report positions / orders that already exist at the venue (e.g. after a restart)."""
        try:
            st = self.broker.account_state()
        except Exception as exc:  # noqa: BLE001
            log.error("could not read broker state: %s", exc)
            self.notifier.send(f"WARNING: cannot read broker state at startup: {exc}")
            return
        found = sorted(st.open_symbols | st.pending_symbols)
        if found:
            msg = f"Found existing positions/orders on {', '.join(found)} - the guard will manage them, no duplicates will be opened."
            log.warning(msg)
            self.notifier.send(msg)
            self.journal.write("reconcile", symbols=found)

    def poll_market(self, m: MarketRuntime) -> list[dict[str, Any]]:
        n = 10
        now = datetime.now(timezone.utc)
        if m.last_time is not None:
            behind = (now - m.last_time.to_pydatetime()) / timedelta(minutes=m.minutes)
            n = int(min(1000, max(10, behind + 3)))
        df = m.feed.latest(n)
        g = self.cfg.guard
        if len(df) and g.enabled and g.stale_bars > 0:
            newest_close = df.index[-1].to_pydatetime() + timedelta(minutes=m.minutes)
            stale = (now - newest_close) > timedelta(minutes=m.minutes * g.stale_bars)
            if stale != m.stale:
                m.stale = stale
                msg = (f"{m.cfg.symbol}: data feed is stale (last candle closed {newest_close:%H:%M} UTC) - no new entries"
                       if stale else f"{m.cfg.symbol}: data feed recovered")
                log.warning(msg)
                self.notifier.send(msg)
        if m.last_time is not None:
            df = df[df.index > m.last_time]
        outcomes: list[dict[str, Any]] = []
        for i, bar in enumerate(bars_from_df(df)):
            with self.lock:
                if m.guard is not None:
                    m.guard.begin_bar(bar)
                for ev in self.broker.on_bar(m.cfg.symbol, bar, m.engine.t + 1):
                    self._broker_event(ev, m)
                signals = m.engine.update(bar)
                if m.guard is not None:
                    m.guard.on_bar(bar, m.engine)
            m.last_time = df.index[i]
            if i == len(df) - 1:
                self.manage_position(m, bar)
                if signals and self.self_signals:  # act only on the newest candle
                    for sig in signals:
                        outcomes.append(self.handle_signal(sig, m.engine, m, bar.spread or None))
        return outcomes

    def manage_position(self, m: MarketRuntime, bar: Any) -> None:
        """Let the guard cancel / close / protect what is working on this market."""
        if m.guard is None:
            return
        with self.lock:
            info = self.broker.position_info(m.cfg.symbol)
            if info is None:
                return
            act = m.guard.trade_action(info["status"], info["direction"], info["fill_price"], info["be_moved"],
                                       bar.close, m.engine)
            if act is None:
                return
            if act.kind == "cancel":
                events = self.broker.cancel_pending(m.cfg.symbol, act.reason)
            elif act.kind == "close":
                events = self.broker.close_position(m.cfg.symbol, act.reason)
            else:
                events = self.broker.protect(m.cfg.symbol, act.reason)
            self.journal.write("guard_action", symbol=m.cfg.symbol, action=act.kind, reason=act.reason)
            for ev in events:
                self._broker_event(ev, m)
            if not events:
                self.notifier.send(f"GUARD could not {act.kind} {m.cfg.symbol}: {act.reason} - check the terminal")

    # ------------------------------------------------------------- decisions
    def handle_signal(self, sig: Signal, engine: SMCEngine | None = None, m: MarketRuntime | None = None,
                      spread: float | None = None) -> dict[str, Any]:
        """Run one setup through the full decision pipeline."""
        with self.lock:
            return self._handle(sig, engine, m or self.market_for(sig.symbol), spread)

    def _handle(self, sig: Signal, engine: SMCEngine | None, m: MarketRuntime | None,
                spread: float | None) -> dict[str, Any]:
        decision: dict[str, Any] = {"id": sig.id, "symbol": sig.symbol, "side": sig.side, "taken": False}
        self.journal.write("signal", **sig.to_dict())
        if self.halted():
            return self._skip(decision, "kill switch: agent halted (state/HALT)")
        if m is not None and m.stale:
            return self._skip(decision, "data feed is stale")

        if self.model is not None and sig.features:
            ev = self.model.score_signal(sig)
            if ev < self.cfg.learner.min_expected_r:
                return self._skip(decision, f"learned E[R] {ev:+.2f} below {self.cfg.learner.min_expected_r}")

        mult = 1.0
        guard = m.guard if m is not None else None
        dec: GuardDecision | None = None
        if guard is not None and engine is not None:
            live_spread = self.broker.spread(sig.symbol)
            dec = guard.check(sig, engine, live_spread if live_spread is not None else spread)
            decision["guard"] = dec.to_dict()
            if not dec.allowed:
                return self._skip(decision, "guard: " + " | ".join(dec.blocks))
            sig = apply_decision(sig, dec)
            mult *= dec.size_mult

        state = self.broker.account_state()
        ok, why = self.risk.check(sig, state)
        if not ok:
            return self._skip(decision, why)

        if self.analyst is not None:
            context = {"risk_context": guard.context(engine), "guard_warnings": dec.warnings if dec else []} \
                if guard is not None and engine is not None else None
            review = self.analyst.review(sig, engine, context)
            ok, ai_mult, why = self.analyst.approves(review)
            decision["ai"] = review
            if not ok:
                return self._skip(decision, why)
            mult *= ai_mult

        qty = self.broker.size(sig, self.risk.risk_amount(state.equity) * mult)
        if qty <= 0:
            return self._skip(decision, "position size is zero")
        try:
            order_id = self.broker.place(sig, qty)
        except Exception as exc:  # noqa: BLE001 - never crash the loop on a broker error
            log.exception("order placement failed")
            return self._skip(decision, f"broker error: {exc}")
        self.risk.record_entry()
        decision.update(taken=True, order_id=order_id, qty=qty, size_mult=mult)
        self.journal.write("order", **decision, entry=sig.entry, sl=sig.sl, tp=sig.tp)
        msg = format_signal(sig, f"Order placed ({self.broker.name}) qty {qty:.6g}" +
                            (f" - size x{mult:g}" if mult != 1 else ""))
        log.info("\n%s", msg)
        self.notifier.send(msg)
        return decision

    def _skip(self, decision: dict[str, Any], why: str) -> dict[str, Any]:
        decision["reason"] = why
        self.journal.write("skip", **decision)
        log.info("skip %s: %s", decision["id"], why)
        return decision

    def _broker_event(self, ev: dict[str, Any], m: MarketRuntime | None) -> None:
        kind = ev.get("event", "event")
        self.journal.write("broker_" + kind, **ev)
        if kind == "closed" and m is not None and m.guard is not None and ev.get("r") is not None:
            m.guard.on_trade_closed(float(ev["r"]))
        if kind in ("filled", "partial", "closed", "guard_close", "protected", "cancelled"):
            text = f"{kind.upper()} {ev.get('symbol')} {ev.get('side', '')}".strip()
            if kind == "closed":
                text += f" {ev.get('exit_reason', '')} {float(ev.get('r', 0) or 0):+.2f}R pnl {float(ev.get('pnl', 0) or 0):+.2f}"
            if kind == "partial":
                text += f" - {float(ev.get('partial', 0) or 0):.0%} closed at {ev.get('partial_price')}, stop moved to entry"
            if ev.get("reason") or (kind == "cancelled" and ev.get("exit_reason")):
                text += f" - {ev.get('reason') or ev.get('exit_reason')}"
            log.info(text)
            if kind != "cancelled" or ":" in str(ev.get("reason") or ev.get("exit_reason") or ""):
                self.notifier.send(text)

    # -------------------------------------------------------- external input
    def market_for(self, ticker: str) -> MarketRuntime | None:
        key = _norm(ticker)
        for m in self.markets:
            if key in (_norm(m.cfg.symbol), _norm(m.cfg.tv_symbol or m.cfg.symbol)):
                return m
        return None

    def handle_external(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Execute a setup sent by the TradingView indicator's webhook alert."""
        ticker = str(payload.get("ticker", ""))
        m = self.market_for(ticker)
        if m is None:
            return {"taken": False, "reason": f"ticker {ticker!r} is not configured in markets"}
        side = str(payload.get("side", "")).lower()
        if side not in ("long", "short"):
            return {"taken": False, "reason": "side must be long or short"}
        try:
            entry, sl, tp = float(payload["entry"]), float(payload["sl"]), float(payload["tp"])
        except (KeyError, TypeError, ValueError):
            return {"taken": False, "reason": "entry/sl/tp missing or not numeric"}
        d = LONG if side == "long" else SHORT
        if (entry - sl) * d <= 0 or (tp - entry) * d <= 0:
            return {"taken": False, "reason": "inconsistent entry/sl/tp for side"}
        risk = abs(entry - sl)
        now = datetime.now(timezone.utc)
        feats = payload.get("features") if isinstance(payload.get("features"), dict) else {}
        score = int(payload.get("score", 0))
        meta: dict[str, Any] = {"source": "tradingview"}
        try:
            tp1 = float(payload["tp1"])
            if 0 < (tp1 - entry) * d < (tp - entry) * d:
                meta["tp1"] = tp1
        except (KeyError, TypeError, ValueError):
            pass
        sig = Signal(
            id=f"tv|{m.cfg.symbol}|{payload.get('time', int(now.timestamp()))}|{side}",
            symbol=m.cfg.symbol,
            timeframe=m.cfg.timeframe,
            time=now,
            bar=max(m.engine.t, 0),
            direction=d,
            model=str(payload.get("model", "tradingview")),
            entry=entry,
            sl=sl,
            tp=tp,
            rr=(tp - entry) * d / risk,
            risk_atr=risk / m.engine.atr if m.engine.atr else 0.0,
            atr=m.engine.atr or 0.0,
            expiry_bars=int(payload.get("expiry_bars", self.cfg.strategy.entry_expiry)),
            score=score,
            grade=str(payload.get("grade", "")),
            features={k: float(v) for k, v in feats.items()},
            reasons=[f"TradingView alert: {payload.get('model', 'setup')}"],
            meta=meta,
        )
        if sig.features:
            sig.features.setdefault("rr", sig.rr)
            sig.features.setdefault("risk_atr", sig.risk_atr)
        return self.handle_signal(sig, m.engine if m.engine.t > 0 else None, m)

    # ------------------------------------------------------------------ loop
    def _next_wake(self) -> float:
        now = time.time()
        waits = []
        for m in self.markets:
            period = m.minutes * 60
            waits.append(period - (now % period))
        return min(waits) + self.cfg.poll_delay_s

    def run_forever(self, stop: threading.Event | None = None) -> None:
        stop = stop or threading.Event()
        self.warmup()
        log.info("agent running on %d market(s) with %s broker", len(self.markets), self.broker.name)
        while not stop.is_set():
            deadline = time.time() + self._next_wake()
            while not stop.is_set() and time.time() < deadline:
                # the emergency switch (state/FLATTEN) is honoured within seconds, not at the next candle
                stop.wait(min(self.kill_switch_poll_s, max(0.0, deadline - time.time())))
                self.check_kill_switch()
            if stop.is_set():
                break
            self.refresh_calendar()
            for m in self.markets:
                try:
                    self.poll_market(m)
                    if m.errors >= 3:
                        self.notifier.send(f"{m.cfg.symbol}: recovered after {m.errors} failed polls")
                    m.errors = 0
                except Exception as exc:  # noqa: BLE001 - keep other markets alive
                    m.errors += 1
                    log.exception("polling %s failed", m.cfg.symbol)
                    if m.errors in (3, 10, 50):
                        self.notifier.send(f"ERROR: {m.cfg.symbol} failed {m.errors} polls in a row: {exc}")
        self.broker.close()
