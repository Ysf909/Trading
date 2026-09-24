"""Event-driven backtester (bar by bar, no look-ahead).

The same ``SMCEngine`` that runs live produces the signals; orders are
simulated with :mod:`smc_agent.execution.sim` (conservative intrabar rules).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Callable

import pandas as pd

from .config import CostConfig, RiskConfig, StrategyConfig
from .core.engine import SMCEngine, bars_from_df
from .core.types import Signal
from .execution.sim import Trade, settle, step

SignalFilter = Callable[[Signal], bool]


@dataclass
class BacktestResult:
    trades: list[Trade]
    cancelled: list[Trade]
    signals: list[Signal]
    equity_curve: list[tuple[Any, float]]
    starting_equity: float
    engine: SMCEngine
    metrics: dict[str, Any] = field(default_factory=dict)

    def trades_frame(self) -> pd.DataFrame:
        return pd.DataFrame([t.to_dict() for t in self.trades + self.cancelled])


def position_size(equity: float, risk_pct: float, signal: Signal) -> float:
    risk = signal.risk
    return 0.0 if risk <= 0 else equity * risk_pct / 100.0 / risk


def run_backtest(
    df: pd.DataFrame,
    strategy: StrategyConfig,
    risk: RiskConfig | None = None,
    costs: CostConfig | None = None,
    symbol: str = "",
    timeframe: str = "",
    starting_equity: float = 10_000.0,
    signal_filter: SignalFilter | None = None,
) -> BacktestResult:
    """One position (or pending order) at a time; a fresher signal replaces a
    still-pending order, signals are ignored while a position is open."""
    risk = risk or RiskConfig()
    costs = costs or CostConfig()
    engine = SMCEngine(strategy, symbol, timeframe)
    equity = starting_equity
    active: Trade | None = None
    trades: list[Trade] = []
    cancelled: list[Trade] = []
    taken: list[Signal] = []
    curve: list[tuple[Any, float]] = []
    last_bar = None

    for t, bar in enumerate(bars_from_df(df)):
        last_bar = bar
        if active is not None:
            ev = step(active, bar, t, strategy.breakeven_at_r)
            if active.status == "closed":
                settle(active, costs.commission_pct, costs.slippage_pct)
                equity += active.pnl
                trades.append(active)
                active = None
            elif ev == "cancelled":
                cancelled.append(active)
                active = None

        for sig in sorted(engine.update(bar), key=lambda s: -s.score):
            if signal_filter is not None and not signal_filter(sig):
                continue
            if sig.rr < risk.min_rr:
                continue
            if active is not None and active.status == "open":
                break
            if active is not None:  # replace stale pending order
                active.status, active.exit_reason, active.exit_bar = "cancelled", "replaced", t
                cancelled.append(active)
            active = Trade(sig, qty=position_size(equity, risk.risk_per_trade_pct, sig))
            taken.append(sig)
            break
        curve.append((bar.time, equity))

    if active is not None and active.status == "open" and last_bar is not None:
        active.status = "closed"
        active.exit_price, active.exit_bar = last_bar.close, engine.t
        active.exit_time, active.exit_reason = last_bar.time, "eod"
        settle(active, costs.commission_pct, costs.slippage_pct)
        equity += active.pnl
        trades.append(active)
        if curve:
            curve[-1] = (curve[-1][0], equity)

    res = BacktestResult(trades, cancelled, taken, curve, starting_equity, engine)
    res.metrics = compute_metrics(res)
    return res


def collect_outcomes(df: pd.DataFrame, strategy: StrategyConfig, symbol: str = "", timeframe: str = "") -> list[Trade]:
    """Simulate *every* signal independently (no position overlap rules).

    Used to label signals for the learner: each returned trade is closed or
    cancelled with its own outcome."""
    engine = SMCEngine(strategy, symbol, timeframe)
    live: list[Trade] = []
    done: list[Trade] = []
    last_bar = None
    for t, bar in enumerate(bars_from_df(df)):
        last_bar = bar
        still: list[Trade] = []
        for tr in live:
            step(tr, bar, t, strategy.breakeven_at_r)
            (done if tr.status in ("closed", "cancelled") else still).append(tr)
        live = still
        live.extend(Trade(sig, qty=1.0) for sig in engine.update(bar))
    for tr in live:  # unresolved at the end of the data
        if tr.status == "open" and last_bar is not None:
            tr.status, tr.exit_price, tr.exit_reason = "closed", last_bar.close, "eod"
            tr.exit_bar, tr.exit_time = engine.t, last_bar.time
            done.append(tr)
    done.sort(key=lambda tr: tr.signal.bar)
    return done


def _stats(rs: list[float]) -> dict[str, Any]:
    n = len(rs)
    if n == 0:
        return {"trades": 0}
    wins = [r for r in rs if r > 1e-9]
    losses = [r for r in rs if r < -1e-9]
    gross_win, gross_loss = sum(wins), -sum(losses)
    cum = peak = max_dd = 0.0
    for r in rs:
        cum += r
        peak = max(peak, cum)
        max_dd = max(max_dd, peak - cum)
    mean = sum(rs) / n
    sd = math.sqrt(sum((r - mean) ** 2 for r in rs) / (n - 1)) if n > 1 else 0.0
    return {
        "trades": n,
        "wins": len(wins),
        "losses": len(losses),
        "breakeven": n - len(wins) - len(losses),
        "win_rate": round(len(wins) / n, 4),
        "avg_r": round(mean, 4),
        "total_r": round(sum(rs), 3),
        "profit_factor": round(gross_win / gross_loss, 3) if gross_loss > 0 else float("inf"),
        "max_drawdown_r": round(max_dd, 3),
        "sqn": round(mean / sd * math.sqrt(min(n, 100)), 3) if sd > 0 else 0.0,
    }


def compute_metrics(res: BacktestResult) -> dict[str, Any]:
    closed = res.trades
    rs = [t.r_multiple for t in closed]
    m: dict[str, Any] = {"summary": _stats(rs)}
    final = res.equity_curve[-1][1] if res.equity_curve else res.starting_equity
    peak = res.starting_equity
    max_dd_pct = 0.0
    for _, eq in res.equity_curve:
        peak = max(peak, eq)
        max_dd_pct = max(max_dd_pct, (peak - eq) / peak * 100.0 if peak > 0 else 0.0)
    fills = len(closed)
    unfilled = sum(1 for t in res.cancelled if t.exit_reason in ("expired", "missed"))
    m["account"] = {
        "starting_equity": res.starting_equity,
        "final_equity": round(final, 2),
        "return_pct": round((final / res.starting_equity - 1.0) * 100.0, 2),
        "max_drawdown_pct": round(max_dd_pct, 2),
        "fees": round(sum(t.fees for t in closed), 2),
    }
    m["orders"] = {
        "signals_taken": len(res.signals),
        "filled": fills,
        "expired": sum(1 for t in res.cancelled if t.exit_reason == "expired"),
        "missed": sum(1 for t in res.cancelled if t.exit_reason == "missed"),
        "replaced": sum(1 for t in res.cancelled if t.exit_reason == "replaced"),
        "fill_rate": round(fills / (fills + unfilled), 3) if fills + unfilled else 0.0,
        "avg_bars_held": round(sum(t.exit_bar - t.fill_bar for t in closed) / fills, 1) if fills else 0.0,
    }
    for key, attr in (("by_model", "model"), ("by_grade", "grade"), ("by_side", "side")):
        groups: dict[str, list[float]] = {}
        for t in closed:
            groups.setdefault(str(getattr(t.signal, attr)), []).append(t.r_multiple)
        m[key] = {k: _stats(v) for k, v in sorted(groups.items())}
    m["engine_rejections"] = dict(res.engine.rejections)
    return m


def format_report(metrics: dict[str, Any], title: str = "Backtest") -> str:
    s, a, o = metrics["summary"], metrics["account"], metrics["orders"]
    lines = [f"== {title} =="]
    if s.get("trades", 0) == 0:
        lines.append("No trades.")
    else:
        lines += [
            f"Trades {s['trades']}  win-rate {s['win_rate']:.1%}  avg {s['avg_r']:+.3f}R  total {s['total_r']:+.2f}R",
            f"Profit factor {s['profit_factor']}  max DD {s['max_drawdown_r']}R  SQN {s['sqn']}",
        ]
    lines += [
        f"Equity {a['starting_equity']:.0f} -> {a['final_equity']:.2f} ({a['return_pct']:+.2f}%), "
        f"max DD {a['max_drawdown_pct']:.2f}%, fees {a['fees']:.2f}",
        f"Orders: {o['signals_taken']} placed, {o['filled']} filled, {o['expired']} expired, "
        f"{o['missed']} missed, {o['replaced']} replaced (fill rate {o['fill_rate']:.0%}, "
        f"avg hold {o['avg_bars_held']} bars)",
    ]
    for key in ("by_model", "by_grade", "by_side"):
        parts = [
            f"{k}: {v['trades']} tr, {v['win_rate']:.0%} win, {v['avg_r']:+.2f}R avg"
            for k, v in metrics[key].items()
            if v.get("trades")
        ]
        if parts:
            lines.append(f"{key.replace('_', ' ')}: " + " | ".join(parts))
    return "\n".join(lines)
