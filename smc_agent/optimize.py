"""Walk-forward parameter search.

Each parameter set is backtested once over the whole history (the engine is
strictly causal, so no trade can see the future). Trades whose signal falls
before the split are *in-sample* and used for ranking; trades after it are
*out-of-sample* and only reported. Pick settings whose out-of-sample numbers
hold up, not the ones with the best in-sample line.
"""

from __future__ import annotations

import itertools
import math
from typing import Any

import pandas as pd

from .backtest import run_backtest
from .config import StrategyConfig, strategy_from_overrides

DEFAULT_GRID: dict[str, list[Any]] = {
    "internal_len": [3, 5, 7],
    "swing_len": [10, 20, 30],
    "entry_mode": ["ce", "edge"],
    "tp_mode": ["liquidity", "fixed"],
    "min_score": [4, 5, 6],
}


def _stats(rs: list[float]) -> dict[str, Any]:
    n = len(rs)
    if n == 0:
        return {"trades": 0, "avg_r": 0.0, "total_r": 0.0, "win_rate": 0.0, "pf": 0.0}
    gw = sum(r for r in rs if r > 0)
    gl = -sum(r for r in rs if r < 0)
    return {
        "trades": n,
        "avg_r": round(sum(rs) / n, 4),
        "total_r": round(sum(rs), 3),
        "win_rate": round(sum(r > 0 for r in rs) / n, 3),
        "pf": round(gw / gl, 3) if gl > 0 else float("inf"),
    }


def optimize(
    df: pd.DataFrame,
    base: StrategyConfig,
    grid: dict[str, list[Any]] | None = None,
    split: float = 0.7,
    symbol: str = "",
    timeframe: str = "",
    min_trades: int = 10,
) -> list[dict[str, Any]]:
    grid = grid or DEFAULT_GRID
    cut_time = df.index[int(len(df) * split)]
    keys = list(grid)
    rows: list[dict[str, Any]] = []
    for combo in itertools.product(*(grid[k] for k in keys)):
        params = dict(zip(keys, combo))
        cfg = strategy_from_overrides(base, params)
        res = run_backtest(df, cfg, symbol=symbol, timeframe=timeframe)
        ins = [t.r_multiple for t in res.trades if t.signal.time < cut_time]
        oos = [t.r_multiple for t in res.trades if t.signal.time >= cut_time]
        s_in, s_out = _stats(ins), _stats(oos)
        # rank by a t-stat-like objective: rewards edge *and* sample size
        objective = s_in["avg_r"] * math.sqrt(s_in["trades"]) if s_in["trades"] >= min_trades else -1e9
        rows.append({"params": params, "in_sample": s_in, "out_of_sample": s_out, "objective": round(objective, 4)})
    rows.sort(key=lambda r: r["objective"], reverse=True)
    return rows


def format_rows(rows: list[dict[str, Any]], top: int = 10) -> str:
    lines = [f"{'rank':>4}  {'in-sample':>26}  {'out-of-sample':>26}  params"]
    for i, r in enumerate(rows[:top], 1):
        a, b = r["in_sample"], r["out_of_sample"]
        lines.append(
            f"{i:>4}  {a['trades']:>4} tr {a['avg_r']:+.3f}R pf {a['pf']:<5}  "
            f"{b['trades']:>4} tr {b['avg_r']:+.3f}R pf {b['pf']:<5}  {r['params']}"
        )
    return "\n".join(lines)
