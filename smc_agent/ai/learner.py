"""The agent's "own school": a small, transparent model that learns which
ICT/SMC confluences actually pay on *your* market and timeframe.

It is an L2-regularised logistic regression (pure numpy) that estimates the
probability that a setup reaches its target before its stop. Combined with
the setup's reward:risk this gives an expected value per trade:

    E[R] = p * RR - (1 - p)

and the agent only takes setups whose expected value clears a threshold.
Training is walk-forward: fit on the first part of history, report results
on the unseen remainder, then refit on everything for deployment.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

import numpy as np

from ..core.types import Signal
from ..execution.sim import Trade

FEATURES = [
    "htf_aligned",
    "htf_opposed",
    "swing_aligned",
    "killzone",
    "pd_ok",
    "major_sweep",
    "zone_confluence",
    "displacement",
    "model_reversal",
    "zone_fvg",
    "rr",
    "risk_atr",
]
CONTINUOUS = {"rr", "risk_atr"}


def expected_r(p: float, rr: float) -> float:
    return p * rr - (1.0 - p)


@dataclass
class EdgeModel:
    features: list[str] = field(default_factory=lambda: list(FEATURES))
    weights: list[float] = field(default_factory=list)
    bias: float = 0.0
    mean: dict[str, float] = field(default_factory=dict)
    std: dict[str, float] = field(default_factory=dict)
    meta: dict[str, Any] = field(default_factory=dict)

    # ------------------------------------------------------------------ core
    def _row(self, feats: dict[str, float]) -> np.ndarray:
        row = []
        for f in self.features:
            v = float(feats.get(f, 0.0))
            if f in CONTINUOUS:
                v = (v - self.mean.get(f, 0.0)) / (self.std.get(f, 1.0) or 1.0)
            row.append(v)
        return np.asarray(row, dtype=float)

    def fit(self, rows: Sequence[dict[str, float]], wins: Sequence[int], l2: float = 2.0,
            epochs: int = 4000, lr: float = 0.3) -> "EdgeModel":
        if len(rows) < 10:
            raise ValueError("need at least 10 resolved trades to train")
        for f in CONTINUOUS & set(self.features):
            vals = np.asarray([float(r.get(f, 0.0)) for r in rows])
            self.mean[f] = float(vals.mean())
            self.std[f] = float(vals.std() or 1.0)
        X = np.vstack([self._row(r) for r in rows])
        y = np.asarray(wins, dtype=float)
        n, k = X.shape
        w = np.zeros(k)
        base = min(max(y.mean(), 1e-3), 1 - 1e-3)
        b = math.log(base / (1 - base))
        for _ in range(epochs):
            z = np.clip(X @ w + b, -30, 30)
            p = 1.0 / (1.0 + np.exp(-z))
            err = p - y
            w -= lr * (X.T @ err / n + l2 * w / n)
            b -= lr * err.mean()
        self.weights = [float(v) for v in w]
        self.bias = float(b)
        return self

    def proba(self, feats: dict[str, float]) -> float:
        if not self.weights:
            raise RuntimeError("model is not trained")
        z = float(self._row(feats) @ np.asarray(self.weights)) + self.bias
        return 1.0 / (1.0 + math.exp(-max(-30.0, min(30.0, z))))

    def score_signal(self, sig: Signal) -> float:
        """Set ``sig.probability`` / ``sig.expected_r`` and return expected R."""
        p = self.proba(sig.features)
        sig.probability = round(p, 4)
        sig.expected_r = round(expected_r(p, sig.rr), 4)
        return sig.expected_r

    # ------------------------------------------------------------ persistence
    def save(self, path: str | Path) -> Path:
        out = Path(path)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps({
            "features": self.features, "weights": self.weights, "bias": self.bias,
            "mean": self.mean, "std": self.std, "meta": self.meta,
        }, indent=2))
        return out

    @classmethod
    def load(cls, path: str | Path) -> "EdgeModel":
        d = json.loads(Path(path).read_text())
        return cls(d["features"], d["weights"], d["bias"], d["mean"], d["std"], d.get("meta", {}))

    def describe(self) -> str:
        pairs = sorted(zip(self.features, self.weights), key=lambda kv: -abs(kv[1]))
        lines = [f"  {name:16s} {w:+.3f}" for name, w in pairs]
        return "learned weights (log-odds of reaching target):\n" + "\n".join(lines)


def _xy(trades: Sequence[Trade]) -> tuple[list[dict[str, float]], list[int], list[float], list[float]]:
    rows, wins, rs, rrs = [], [], [], []
    for tr in trades:
        if tr.status != "closed":
            continue
        rows.append(tr.signal.features)
        r = tr.r_multiple
        wins.append(int(r > 0))
        rs.append(r)
        rrs.append(tr.signal.rr)
    return rows, wins, rs, rrs


def _summary(rs: Sequence[float]) -> dict[str, Any]:
    if not rs:
        return {"trades": 0, "avg_r": 0.0, "total_r": 0.0, "win_rate": 0.0}
    return {
        "trades": len(rs),
        "avg_r": round(float(np.mean(rs)), 4),
        "total_r": round(float(np.sum(rs)), 3),
        "win_rate": round(float(np.mean([r > 0 for r in rs])), 4),
    }


def train_walk_forward(groups: Sequence[Sequence[Trade]], split: float = 0.7, min_expected_r: float = 0.05,
                       l2: float = 2.0, rule_min_score: int | None = None) -> tuple[EdgeModel, dict[str, Any]]:
    """Walk-forward training over one or more markets.

    ``groups`` holds the simulated setups of each market. Every market is split
    in time (first ``split`` -> train, rest -> test), the parts are pooled, the
    model is fit on train and evaluated on test, then refit on everything.
    Returns (deployable model, report)."""
    train: list[Trade] = []
    test: list[Trade] = []
    ordered: list[Trade] = []
    for group in groups:
        closed = sorted((t for t in group if t.status == "closed"), key=lambda t: t.signal.time)
        cut = int(len(closed) * split)
        train += closed[:cut]
        test += closed[cut:]
        ordered += closed
    if len(ordered) < 30:
        raise ValueError(f"only {len(ordered)} filled setups; need 30+ (use more history or more markets)")
    rows, wins, _, _ = _xy(train)
    oos_model = EdgeModel().fit(rows, wins, l2=l2)

    t_rows, _, t_rs, t_rr = _xy(test)
    ev = [expected_r(oos_model.proba(r), rr) for r, rr in zip(t_rows, t_rr)]
    kept = [r for r, e in zip(t_rs, ev) if e >= min_expected_r]
    report = {
        "train_trades": len(train),
        "test_trades": len(test),
        "out_of_sample_all": _summary(t_rs),
        "out_of_sample_filtered": _summary(kept),
        "min_expected_r": min_expected_r,
    }
    if rule_min_score is not None:
        report[f"out_of_sample_score>={rule_min_score}"] = _summary(
            [t.r_multiple for t in test if t.signal.score >= rule_min_score]
        )

    all_rows, all_wins, _, _ = _xy(ordered)
    model = EdgeModel().fit(all_rows, all_wins, l2=l2)
    model.meta = {
        "trained_at": datetime.now(timezone.utc).isoformat(),
        "samples": len(ordered),
        "base_win_rate": round(float(np.mean(all_wins)), 4),
        "walk_forward": report,
    }
    return model, report
