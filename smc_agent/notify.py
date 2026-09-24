"""Trade journal (JSONL) and optional Telegram / Discord notifications."""

from __future__ import annotations

import json
import logging
import os
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import NotifyConfig
from .core.types import Signal

log = logging.getLogger(__name__)


class Journal:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, kind: str, /, **data: Any) -> None:
        data.pop("event", None)
        rec = {"ts": datetime.now(timezone.utc).isoformat(), "event": kind, **data}
        with self.path.open("a") as fh:
            fh.write(json.dumps(rec, default=str) + "\n")


class Notifier:
    def __init__(self, cfg: NotifyConfig) -> None:
        self.tg_token = os.environ.get(cfg.telegram_token_env, "")
        self.tg_chat = os.environ.get(cfg.telegram_chat_id_env, "")
        self.discord = os.environ.get(cfg.discord_webhook_env, "")

    @property
    def enabled(self) -> bool:
        return bool((self.tg_token and self.tg_chat) or self.discord)

    def send(self, text: str) -> None:
        if self.tg_token and self.tg_chat:
            self._post(
                f"https://api.telegram.org/bot{self.tg_token}/sendMessage",
                urllib.parse.urlencode({"chat_id": self.tg_chat, "text": text}).encode(),
                "application/x-www-form-urlencoded",
            )
        if self.discord:
            self._post(self.discord, json.dumps({"content": text[:1900]}).encode(), "application/json")

    @staticmethod
    def _post(url: str, body: bytes, ctype: str) -> None:
        req = urllib.request.Request(url, data=body, headers={"Content-Type": ctype}, method="POST")
        try:
            urllib.request.urlopen(req, timeout=10).read()
        except Exception as exc:  # noqa: BLE001 - notifications are best effort
            log.warning("notification failed: %s", exc)


def format_signal(sig: Signal, extra: str = "") -> str:
    arrow = "BUY" if sig.direction == 1 else "SELL"
    lines = [
        f"{arrow} {sig.symbol} {sig.timeframe} - {sig.model} setup, grade {sig.grade} ({sig.score}/10)",
        f"Limit {sig.entry:.6g} | SL {sig.sl:.6g} | TP {sig.tp:.6g} | {sig.rr:.2f}R",
    ]
    if sig.probability is not None:
        lines.append(f"Learned P(win) {sig.probability:.0%}, E[R] {sig.expected_r:+.2f}")
    if sig.ai_review:
        r = sig.ai_review
        lines.append(f"AI: {r.get('decision')} ({float(r.get('confidence', 0)):.0%}) - {r.get('reasoning', '')[:300]}")
    lines += [f"- {r}" for r in sig.reasons]
    if extra:
        lines.append(extra)
    return "\n".join(lines)
