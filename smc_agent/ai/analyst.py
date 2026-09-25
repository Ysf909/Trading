"""Claude as the agent's senior ICT/SMC reviewer.

The rules engine finds setups mechanically; Claude reviews each one with the
full market context (structure, PD arrays, liquidity map, HTF bias, recent
candles, the learner's probability) and decides to take, reduce or skip it.
It never changes prices - it only gates trades. It can also write a
narrative market brief for manual trading.

Requires ``pip install anthropic`` and an API key (``ANTHROPIC_API_KEY``) or an
``ant auth login`` profile.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from ..config import AIConfig
from ..core.engine import SMCEngine
from ..core.types import Signal

log = logging.getLogger(__name__)

REVIEW_SYSTEM = """You are the senior risk reviewer for an automated trading agent that trades \
ICT / Smart Money Concepts setups. A rules engine has detected a setup and proposes a resting \
limit order with a fixed stop and target. You receive the engine's view of the market \
(structure, order blocks, fair value gaps, liquidity pools, higher-timeframe bias, session) \
and the recent candles.

Decide whether the agent should take the trade at full size, take it at reduced size, or skip it. \
Review it the way an experienced ICT trader would:
- Higher-timeframe bias and the obvious draw on liquidity: is the trade pointed at it?
- Was liquidity genuinely taken, and did the market structure shift come with displacement, \
or is it a choppy break inside a range?
- Is the entry PD array (FVG / order block) in discount for longs / premium for shorts?
- What sits between entry and target (opposing order blocks, gaps, untaken liquidity)?
- Is the stop protected, or resting where liquidity will obviously be hunted?
- Session and timing.
- The risk context: the higher-timeframe picture (trend, premium/discount, unbroken swings and \
unfilled gaps on H1/H4/D1/W1), upcoming news, the volatility regime and recent shocks, today's \
range versus its average, and the losing-streak state. The rules engine and a risk guard have \
already filtered this setup; look for what they cannot see - e.g. a target that needs to break a \
level that has rejected price repeatedly, a trade straight into a news release, or a market that \
is chopping around its equilibrium.

For XAUUSD specifically: gold hunts stops aggressively around the London and New York opens and \
US data releases (08:30 / 10:00 NY), spreads widen around the 17:00 NY rollover, and it can gap \
over weekends; favour setups whose stop sits beyond real liquidity rather than an obvious swing.

Most mechanical setups are average; reserve "take" for setups where the context clearly \
supports the idea, use "reduce" when the idea is valid but the context is mixed, and "skip" \
when the context argues against it. You only gate the trade - never propose different prices. \
Keep the reasoning short and concrete, citing the levels you relied on."""

BRIEF_SYSTEM = """You are an ICT / Smart Money Concepts trading analyst writing a concise, \
actionable plan for a discretionary trader, from the structured market state and candles you \
are given. Cover: higher-timeframe bias; the current dealing range and whether price is in \
premium or discount; the most likely draw on liquidity; the key PD arrays and liquidity levels \
(with prices); a primary and an alternative scenario, each with the trigger that confirms it, \
entry area, invalidation and target; and what would make you stand aside. Read every timeframe \
you are given top-down (W1 -> D1 -> H4 -> H1 -> entry TF) and say where they conflict. Call out \
the risks: upcoming news, volatility shocks, an extended day, the weekend or the rollover. Use \
only levels that appear in the data. Format as short markdown sections."""

REVIEW_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "decision": {"type": "string", "enum": ["take", "reduce", "skip"]},
        "confidence": {"type": "number", "description": "0 to 1"},
        "bias": {"type": "string", "enum": ["bullish", "bearish", "neutral"]},
        "draw_on_liquidity": {"type": "string"},
        "reasoning": {"type": "string"},
        "risks": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["decision", "confidence", "bias", "draw_on_liquidity", "reasoning", "risks"],
    "additionalProperties": False,
}


class ClaudeAnalyst:
    def __init__(self, cfg: AIConfig, client: Any | None = None) -> None:
        self.cfg = cfg
        if client is None:
            import anthropic  # optional dependency

            client = anthropic.Anthropic(timeout=cfg.timeout_s)
        self.client = client
        # server-side refusal fallbacks are available on the Opus 5 / Fable 5 families
        self.use_fallbacks = cfg.model.startswith(("claude-opus-5", "claude-fable-5"))

    # -------------------------------------------------------------- plumbing
    def _create(self, system: str, content: str, fmt: dict[str, Any] | None) -> Any:
        output_config: dict[str, Any] = {"effort": self.cfg.effort}
        if fmt is not None:
            output_config["format"] = fmt
        kwargs: dict[str, Any] = dict(
            model=self.cfg.model,
            max_tokens=16000,
            thinking={"type": "adaptive"},
            output_config=output_config,
            system=system,
            messages=[{"role": "user", "content": content}],
        )
        if self.use_fallbacks:
            return self.client.beta.messages.create(
                betas=["server-side-fallback-2026-07-01"], fallbacks="default", **kwargs
            )
        return self.client.messages.create(**kwargs)

    @staticmethod
    def _text(resp: Any) -> str:
        return "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")

    # ---------------------------------------------------------------- review
    def review(self, sig: Signal, engine: SMCEngine | None = None,
               context: dict[str, Any] | None = None) -> dict[str, Any] | None:
        """Return the review dict, or ``None`` if the call failed or was refused."""
        payload: dict[str, Any] = {
            "proposed_trade": {
                "symbol": sig.symbol,
                "timeframe": sig.timeframe,
                "side": sig.side,
                "model": sig.model,
                "entry_limit": sig.entry,
                "stop": sig.sl,
                "target": sig.tp,
                "reward_risk": round(sig.rr, 2),
                "stop_distance_atr": round(sig.risk_atr, 2),
                "confluence_score": f"{sig.score}/10 ({sig.grade})",
                "engine_reasons": sig.reasons,
                "confluence_flags": {k: v for k, v in sig.features.items() if k not in ("rr", "risk_atr")},
                "pending_order_valid_bars": sig.expiry_bars,
            }
        }
        if sig.probability is not None:
            payload["learned_edge"] = {
                "p_target_before_stop": sig.probability,
                "expected_r": sig.expected_r,
                "note": "from the agent's walk-forward trained model on this market's history",
            }
        if engine is not None:
            payload["market_state"] = engine.snapshot()
            payload["recent_candles"] = engine.recent_bars(self.cfg.bars_context)
        if context:
            payload.update(context)
        content = "Review this setup.\n\n" + json.dumps(payload, default=str)
        try:
            resp = self._create(REVIEW_SYSTEM, content, {"type": "json_schema", "schema": REVIEW_SCHEMA})
        except Exception as exc:  # noqa: BLE001 - network/API errors must not kill the agent
            log.error("Claude review failed: %s", exc)
            return None
        if resp.stop_reason == "refusal":
            log.warning("Claude declined to review %s", sig.id)
            return None
        if resp.stop_reason == "max_tokens":
            log.warning("Claude review truncated for %s", sig.id)
            return None
        try:
            review = json.loads(self._text(resp))
        except json.JSONDecodeError:
            log.error("Claude review was not valid JSON")
            return None
        review["model"] = getattr(resp, "model", self.cfg.model)
        sig.ai_review = review
        return review

    def approves(self, review: dict[str, Any] | None) -> tuple[bool, float, str]:
        """(take?, size multiplier, why) under the configured policy."""
        if review is None:
            ok = self.cfg.fail_open
            return ok, 1.0 if ok else 0.0, "review unavailable (" + ("fail-open" if ok else "fail-closed") + ")"
        decision = review.get("decision")
        conf = float(review.get("confidence", 0.0))
        if decision == "skip":
            return False, 0.0, "AI skipped: " + review.get("reasoning", "")[:200]
        if conf < self.cfg.min_confidence:
            return False, 0.0, f"AI confidence {conf:.2f} < {self.cfg.min_confidence}"
        return True, (0.5 if decision == "reduce" else 1.0), f"AI {decision} ({conf:.2f})"

    # ----------------------------------------------------------------- brief
    def brief(self, engine: SMCEngine, context: dict[str, Any] | None = None) -> str:
        payload: dict[str, Any] = {
            "market_state": engine.snapshot(max_items=8),
            "recent_candles": engine.recent_bars(max(self.cfg.bars_context, 120)),
        }
        if context:
            payload.update(context)
        resp = self._create(BRIEF_SYSTEM, "Write the trading plan.\n\n" + json.dumps(payload, default=str), None)
        if resp.stop_reason == "refusal":
            return "The model declined to produce a brief for this request."
        return self._text(resp)
