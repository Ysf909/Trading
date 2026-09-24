"""Configuration for the SMC/ICT agent.

Every tunable lives here as a dataclass so the same values drive the live
agent, the backtester, the optimizer and (by name) the TradingView inputs in
``tradingview/SMC_ICT_Pro.pine``. YAML files map 1:1 onto these dataclasses.
"""

from __future__ import annotations

import os
from dataclasses import asdict, dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any

import yaml

# Killzones in New York local time (ICT convention). end < start wraps midnight.
KILLZONES: dict[str, tuple[str, str]] = {
    "asia": ("20:00", "00:00"),
    "london": ("02:00", "05:00"),
    "ny_am": ("07:00", "10:00"),
    "london_close": ("10:00", "12:00"),
    "ny_pm": ("13:30", "16:00"),
}


@dataclass
class StrategyConfig:
    """Market-structure / entry-model parameters (mirrors the Pine inputs)."""

    # --- structure ---------------------------------------------------------
    swing_len: int = 20  # pivot strength for swing structure
    internal_len: int = 5  # pivot strength for internal structure (MSS)
    atr_len: int = 14

    # --- PD arrays ---------------------------------------------------------
    fvg_min_atr: float = 0.1  # min FVG size as a multiple of ATR
    fvg_max_age: int = 300  # bars before an unfilled FVG is dropped
    ob_max_age: int = 500  # bars before an unmitigated OB is dropped
    ob_lookback: int = 300  # max bars searched back for an OB candle
    max_zones: int = 20  # per type & direction

    # --- liquidity ---------------------------------------------------------
    eq_tolerance_atr: float = 0.1  # equal highs/lows tolerance (x ATR)
    max_levels: int = 40  # per side
    day_tz: str = "UTC"  # timezone used to roll previous-day high/low

    # --- entry models --------------------------------------------------------
    models: str = "both"  # reversal | continuation | both
    mss_window: int = 20  # bars allowed between sweep extreme and MSS
    fvg_wait: int = 3  # bars after MSS to wait for an FVG before OB fallback
    use_ob_entry: bool = True  # fall back to the MSS order block if no FVG
    entry_mode: str = "ce"  # "edge" (proximal edge) | "ce" (50% / mean threshold)
    entry_expiry: int = 20  # bars a pending limit order stays alive
    sl_buffer_atr: float = 0.1
    min_risk_atr: float = 0.25
    max_risk_atr: float = 4.0

    # --- targets -------------------------------------------------------------
    tp_mode: str = "liquidity"  # liquidity | fixed
    rr_target: float = 2.0  # fixed RR (and fallback when no liquidity fits)
    min_rr: float = 1.5
    max_rr: float = 6.0
    breakeven_at_r: float = 0.0  # move SL to entry after +xR (0 = off)

    # --- filters / confluence -----------------------------------------------
    htf_minutes: int = 0  # bias timeframe in minutes; 0 = auto from chart timeframe
    htf_len: int = 5  # pivot strength on the HTF
    htf_filter: bool = False  # only trade in HTF direction
    killzone_filter: bool = False  # only trade inside killzones
    killzones: list[str] = field(default_factory=lambda: ["london", "ny_am", "ny_pm"])
    displacement_atr: float = 1.0  # body size that counts as displacement
    min_score: int = 6  # 0..10 confluence score threshold (6 = grade A and above)

    def validate(self) -> None:
        if self.models not in ("reversal", "continuation", "both"):
            raise ValueError(f"models must be reversal|continuation|both, got {self.models!r}")
        if self.entry_mode not in ("edge", "ce"):
            raise ValueError(f"entry_mode must be edge|ce, got {self.entry_mode!r}")
        if self.tp_mode not in ("liquidity", "fixed"):
            raise ValueError(f"tp_mode must be liquidity|fixed, got {self.tp_mode!r}")
        for kz in self.killzones:
            if kz not in KILLZONES:
                raise ValueError(f"unknown killzone {kz!r}; choose from {list(KILLZONES)}")
        if self.internal_len < 1 or self.swing_len < 1 or self.htf_len < 1:
            raise ValueError("pivot lengths must be >= 1")


@dataclass
class RiskConfig:
    risk_per_trade_pct: float = 0.5  # % of equity risked per trade
    max_daily_loss_pct: float = 2.0  # stop opening trades after this daily drawdown
    max_open_positions: int = 3  # across all markets
    max_trades_per_day: int = 6
    one_position_per_symbol: bool = True
    min_rr: float = 1.5


@dataclass
class CostConfig:
    commission_pct: float = 0.02  # per side, % of notional (crypto maker ~0.02, forex ~0.003)
    slippage_pct: float = 0.0  # applied against you on market-style fills


@dataclass
class LearnerConfig:
    enabled: bool = False
    model_path: str = "models/edge_model.json"
    min_expected_r: float = 0.05  # take a trade only if p*RR-(1-p) >= this


@dataclass
class AIConfig:
    enabled: bool = False  # ask Claude to review every signal before execution
    model: str = "claude-opus-5"
    effort: str = "high"  # low | medium | high | xhigh | max
    min_confidence: float = 0.55
    bars_context: int = 80  # recent candles sent with each review
    timeout_s: float = 120.0
    fail_open: bool = False  # if the API errors: False = skip trade, True = take it


@dataclass
class MarketConfig:
    symbol: str = "BTC/USDT"
    timeframe: str = "15m"
    feed: str = "ccxt"  # ccxt | yfinance | mt5 | csv
    exchange: str = "binance"  # for ccxt
    csv_path: str = ""  # for csv
    tv_symbol: str = ""  # TradingView ticker used by webhook alerts (e.g. BTCUSDT)


@dataclass
class BrokerConfig:
    kind: str = "paper"  # paper | ccxt | mt5
    exchange: str = "binance"
    testnet: bool = True
    api_key_env: str = "EXCHANGE_API_KEY"
    api_secret_env: str = "EXCHANGE_API_SECRET"
    starting_equity: float = 10_000.0
    state_path: str = "state/paper_broker.json"
    mt5_magic: int = 909_909
    mt5_deviation: int = 20
    quote_currency: str = "USDT"  # balance currency for ccxt equity
    market_type: str = "future"  # ccxt defaultType: spot | future | swap


@dataclass
class NotifyConfig:
    telegram_token_env: str = "TELEGRAM_BOT_TOKEN"
    telegram_chat_id_env: str = "TELEGRAM_CHAT_ID"
    discord_webhook_env: str = "DISCORD_WEBHOOK_URL"


@dataclass
class WebhookConfig:
    host: str = "0.0.0.0"
    port: int = 8080
    passphrase_env: str = "WEBHOOK_PASSPHRASE"
    execute: bool = True  # False = log/notify only


@dataclass
class AppConfig:
    markets: list[MarketConfig] = field(default_factory=lambda: [MarketConfig()])
    strategy: StrategyConfig = field(default_factory=StrategyConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    costs: CostConfig = field(default_factory=CostConfig)
    learner: LearnerConfig = field(default_factory=LearnerConfig)
    ai: AIConfig = field(default_factory=AIConfig)
    broker: BrokerConfig = field(default_factory=BrokerConfig)
    notify: NotifyConfig = field(default_factory=NotifyConfig)
    webhook: WebhookConfig = field(default_factory=WebhookConfig)
    journal_path: str = "state/journal.jsonl"
    warmup_bars: int = 1000
    poll_delay_s: float = 5.0  # wait after candle close before fetching

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _build(cls: type, data: dict[str, Any] | None) -> Any:
    """Recursively build dataclass ``cls`` from a (possibly partial) dict."""
    data = data or {}
    known = {f.name: f for f in fields(cls)}
    unknown = set(data) - set(known)
    if unknown:
        raise ValueError(f"unknown keys for {cls.__name__}: {sorted(unknown)}")
    kwargs: dict[str, Any] = {}
    for name, value in data.items():
        f = known[name]
        default = f.default_factory() if callable(f.default_factory) else f.default  # type: ignore[misc]
        if is_dataclass(default) and isinstance(value, dict):
            kwargs[name] = _build(type(default), value)
        elif name == "markets":
            kwargs[name] = [_build(MarketConfig, m) for m in value]
        else:
            kwargs[name] = value
    return cls(**kwargs)


def load_config(path: str | os.PathLike | None) -> AppConfig:
    if path is None:
        cfg = AppConfig()
    else:
        raw = yaml.safe_load(Path(path).read_text()) or {}
        cfg = _build(AppConfig, raw)
    cfg.strategy.validate()
    return cfg


def strategy_from_overrides(base: StrategyConfig, overrides: dict[str, Any]) -> StrategyConfig:
    data = asdict(base)
    data.update(overrides)
    cfg = StrategyConfig(**data)
    cfg.validate()
    return cfg
