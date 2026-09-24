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
from typing import Any

import pandas as pd

from .config import AppConfig, MarketConfig
from .core.engine import SMCEngine, bars_from_df
from .core.timeframes import timeframe_minutes
from .core.types import LONG, SHORT, Signal
from .data.feeds import Feed, make_feed
from .execution.broker import Broker, PaperBroker
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
            state_path=cfg.broker.state_path,
        )
    if kind == "ccxt":
        from .execution.ccxt_broker import CCXTBroker

        return CCXTBroker(cfg.broker)
    if kind == "mt5":
        from .execution.mt5_broker import MT5Broker

        return MT5Broker(cfg.broker)
    raise ValueError(f"unknown broker {cfg.broker.kind!r}")


def _norm(sym: str) -> str:
    return sym.split(":")[-1].replace("/", "").replace("-", "").replace("_", "").upper()


@dataclass
class MarketRuntime:
    cfg: MarketConfig
    feed: Feed
    engine: SMCEngine
    minutes: int
    last_time: pd.Timestamp | None = None


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
    ) -> None:
        self.cfg = cfg
        self.broker = broker or make_broker(cfg)
        self.risk = RiskManager(cfg.risk)
        self.journal = Journal(cfg.journal_path)
        self.notifier = notifier or Notifier(cfg.notify)
        self.self_signals = self_signals
        self.lock = threading.RLock()

        self.model = model
        if self.model is None and cfg.learner.enabled:
            from .ai.learner import EdgeModel

            self.model = EdgeModel.load(cfg.learner.model_path)
        self.analyst = analyst
        if self.analyst is None and cfg.ai.enabled:
            from .ai.analyst import ClaudeAnalyst

            self.analyst = ClaudeAnalyst(cfg.ai)

        self.markets: list[MarketRuntime] = []
        for m in cfg.markets:
            feed = (feeds or {}).get(m.symbol) or make_feed(m)
            eng = SMCEngine(cfg.strategy, m.symbol, m.timeframe)
            self.markets.append(MarketRuntime(m, feed, eng, timeframe_minutes(m.timeframe)))

    # ----------------------------------------------------------------- data
    def warmup(self) -> None:
        for m in self.markets:
            df = m.feed.history(self.cfg.warmup_bars)
            for bar in bars_from_df(df):
                m.engine.update(bar)
            m.last_time = df.index[-1] if len(df) else None
            snap = m.engine.snapshot()
            log.info("%s %s warmed up on %d bars | HTF %s, swing %s, internal %s", m.cfg.symbol, m.cfg.timeframe,
                     len(df), snap.get("htf_trend"), snap.get("swing_trend"), snap.get("internal_trend"))
            self.journal.write("warmup", symbol=m.cfg.symbol, timeframe=m.cfg.timeframe, bars=len(df))

    def poll_market(self, m: MarketRuntime) -> list[dict[str, Any]]:
        n = 10
        if m.last_time is not None:
            behind = (datetime.now(timezone.utc) - m.last_time.to_pydatetime()) / timedelta(minutes=m.minutes)
            n = int(min(1000, max(10, behind + 3)))
        df = m.feed.latest(n)
        if m.last_time is not None:
            df = df[df.index > m.last_time]
        outcomes: list[dict[str, Any]] = []
        for i, bar in enumerate(bars_from_df(df)):
            with self.lock:
                for ev in self.broker.on_bar(m.cfg.symbol, bar, m.engine.t + 1):
                    self._broker_event(ev)
                signals = m.engine.update(bar)
            m.last_time = df.index[i]
            if signals and self.self_signals and i == len(df) - 1:  # act only on the newest candle
                for sig in signals:
                    outcomes.append(self.handle_signal(sig, m.engine))
        return outcomes

    # ------------------------------------------------------------- decisions
    def handle_signal(self, sig: Signal, engine: SMCEngine | None = None) -> dict[str, Any]:
        """Run one setup through the full decision pipeline."""
        with self.lock:
            return self._handle(sig, engine)

    def _handle(self, sig: Signal, engine: SMCEngine | None) -> dict[str, Any]:
        decision: dict[str, Any] = {"id": sig.id, "symbol": sig.symbol, "side": sig.side, "taken": False}
        self.journal.write("signal", **sig.to_dict())

        if self.model is not None and sig.features:
            ev = self.model.score_signal(sig)
            if ev < self.cfg.learner.min_expected_r:
                return self._skip(decision, f"learned E[R] {ev:+.2f} below {self.cfg.learner.min_expected_r}")

        state = self.broker.account_state()
        ok, why = self.risk.check(sig, state)
        if not ok:
            return self._skip(decision, why)

        mult = 1.0
        if self.analyst is not None:
            review = self.analyst.review(sig, engine)
            ok, mult, why = self.analyst.approves(review)
            decision["ai"] = review
            if not ok:
                return self._skip(decision, why)

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
        msg = format_signal(sig, f"Order placed ({self.broker.name}) qty {qty:.6g}")
        log.info("\n%s", msg)
        self.notifier.send(msg)
        return decision

    def _skip(self, decision: dict[str, Any], why: str) -> dict[str, Any]:
        decision["reason"] = why
        self.journal.write("skip", **decision)
        log.info("skip %s: %s", decision["id"], why)
        return decision

    def _broker_event(self, ev: dict[str, Any]) -> None:
        self.journal.write("broker_" + ev.get("event", "event"), **ev)
        if ev.get("event") in ("filled", "closed"):
            text = f"{ev['event'].upper()} {ev.get('symbol')} {ev.get('side', '')}"
            if ev.get("event") == "closed":
                text += f" {ev.get('exit_reason', '')} {ev.get('r', 0):+.2f}R pnl {ev.get('pnl', 0):+.2f}"
            log.info(text)
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
            meta={"source": "tradingview"},
        )
        if sig.features:
            sig.features.setdefault("rr", sig.rr)
            sig.features.setdefault("risk_atr", sig.risk_atr)
        return self.handle_signal(sig, m.engine if m.engine.t > 0 else None)

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
            wait = self._next_wake()
            if stop.wait(wait):
                break
            for m in self.markets:
                try:
                    self.poll_market(m)
                except Exception:  # noqa: BLE001 - keep other markets alive
                    log.exception("polling %s failed", m.cfg.symbol)
        self.broker.close()
