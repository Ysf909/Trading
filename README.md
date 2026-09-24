# SMC / ICT Trading Agent + TradingView Indicator

Two tools that share one brain:

1. **An AI trading agent** (Python) that watches your markets candle by candle, reads them the ICT
   / Smart Money Concepts way, finds A-grade setups, filters them with a model it trains on your
   own market, can ask Claude for a senior-trader review, sizes the position from your risk
   settings and executes: paper by default, live through ccxt (crypto) or MetaTrader 5.
2. **A TradingView indicator** (`tradingview/SMC_ICT_Pro.pine`, Pine v6) for manual trading.
   It shows the same analysis on your chart: structure, order blocks, FVGs, liquidity, sweeps,
   killzones, premium/discount and HTF bias. It also shows the same BUY/SELL setups with entry,
   stop, target, grade, a live stats dashboard, alerts, and a webhook that lets the agent execute
   what the chart sees.

```
                ┌──────────────── TradingView ────────────────┐
                │ SMC ICT Pro indicator  ── alert() JSON ──┐  │
                └──────────────────────────────────────────┼──┘
 market data ─► SMC engine ─► setup ─► learned edge ─► risk ─► Claude ─► broker
 (ccxt/yf/MT5/  (structure, OB,  (reversal /   filter       manager   review   (paper/ccxt/MT5)
  CSV)           FVG, liquidity)  continuation)                                 │
                                                             journal + Telegram/Discord
```

> **Risk warning.** No indicator or agent can guarantee profitable trades. The results below are
> historical, include in-sample choices, and vary a lot between markets. Run the agent on paper
> and on testnets first, and risk only what you can afford to lose. This is not financial advice.

## Contents

| Path | What |
|---|---|
| `tradingview/SMC_ICT_Pro.pine` | the TradingView indicator ([guide](tradingview/README.md)) |
| `tradingview/SMC_ICT_Pro_Strategy.pine` | same logic as a strategy for the Strategy Tester |
| `smc_agent/core/` | streaming SMC/ICT engine (structure, PD arrays, liquidity, sessions, setups) |
| `smc_agent/backtest.py`, `optimize.py` | conservative backtester, walk-forward optimizer |
| `smc_agent/ai/learner.py` | the agent's own school: learned confluence weights |
| `smc_agent/ai/analyst.py` | Claude trade reviewer and market brief writer |
| `smc_agent/live.py`, `webhook.py` | the autonomous agent loop and the TradingView webhook server |
| `smc_agent/execution/` | paper broker, ccxt broker, MT5 broker |
| `docs/strategy.md` | the trading model in detail, scoring, research results |
| `config.example.yaml` | every setting, documented |

## The trading model in one minute

* **Reversal (ICT 2022 model):** liquidity is swept, then an internal market structure shift
  with displacement. Entry is a limit at the 50% of the fair value gap (or the order block), the
  stop goes beyond the sweep, and the target is the next opposing liquidity pool (≥ 1.5R).
* **Continuation:** an internal BOS in the direction of swing structure, with the entry at the
  leg's FVG/OB and the stop beyond the protected higher low / lower high.
* **Confluence score 0–10:** HTF bias (2), premium/discount (2), major liquidity swept, OB+FVG
  overlap, killzone, displacement, swing trend, RR ≥ 3 (1 each). By default only **A-grade
  (≥ 6)** setups trade.

Across 14 historical datasets (forex, crypto, index, stock; 1m–1D), the default A-grade filter
produced 191 trades with a 39% win rate, **+0.55R per trade** on average and profit factor 1.95.
11 of the 12 datasets with enough trades were positive, and random-walk control data scored +0.00R.
Details, caveats and per-feature evidence: [docs/strategy.md](docs/strategy.md).

## Quick start: TradingView indicator

Pine Editor → new indicator → paste `tradingview/SMC_ICT_Pro.pine` → Save → Add to chart.
Hover a **BUY/SELL** label for the full reasoning. See [tradingview/README.md](tradingview/README.md)
for settings and alerts.

## Quick start: the agent

```bash
pip install -e ".[all]"            # core + anthropic, ccxt, yfinance, plotly
cp config.example.yaml config.yaml # edit markets, risk, broker
```

**Backtest** on any CSV (TradingView: chart → *Export chart data*) or on the configured feed:

```bash
smc-agent backtest --csv BINANCE_BTCUSDT_15.csv --chart btc.html --trades btc_trades.csv
smc-agent -c config.yaml backtest --market BTC/USDT --bars 20000
smc-agent backtest --csv eurusd_1h.csv --set min_score=5 --set entry_mode=edge   # try settings
```

**Teach it your market** (walk-forward: fit on the first 70%, report on the unseen 30%, then
refit on everything):

```bash
smc-agent -c config.yaml train --csv btc_15m.csv --csv eth_15m.csv --csv sol_15m.csv
# then in config.yaml: learner.enabled: true (consider strategy.min_score: 4 and let the model filter)
smc-agent -c config.yaml backtest --csv btc_15m.csv --model models/edge_model.json
```

**Find robust settings** for a market (reports in-sample and out-of-sample side by side):

```bash
smc-agent optimize --csv btc_15m.csv --top 10
```

**Look at the market now / get a Claude-written plan:**

```bash
smc-agent -c config.yaml scan                 # structure, zones, liquidity, active setups (JSON)
export ANTHROPIC_API_KEY=...                  # or `ant auth login`
smc-agent -c config.yaml brief --market BTC/USDT
```

**Run the agent** (paper broker unless you change `broker.kind`):

```bash
smc-agent -c config.yaml run
```

Every candle close it updates the engine and, for each new setup, runs the decision pipeline:
learned expected-R filter, then the risk manager (daily loss limit, max positions, trades per day,
RR floor), then the Claude review (`ai.enabled`), then position sizing and a limit order with
attached stop and target. Everything is written to `state/journal.jsonl`. Set
`TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID` or `DISCORD_WEBHOOK_URL` for notifications.

### Going live

| Broker | Config | Notes |
|---|---|---|
| Paper | `broker.kind: paper` | default; same fill rules as the backtester; state survives restarts |
| Crypto (Binance, Bybit, OKX, …) | `kind: ccxt`, `exchange`, `market_type`, `testnet: true` | keys from `EXCHANGE_API_KEY` / `EXCHANGE_API_SECRET`; limit entry with exchange-side SL/TP |
| Forex / CFDs / indices | `kind: mt5` + `feed: mt5` | Windows + MT5 terminal, `pip install MetaTrader5`; pending limit with native SL/TP and expiry; lot size from tick value |

Keep `testnet: true` or a demo account until the journal shows the behaviour you expect.

### Let TradingView drive the agent

```bash
export WEBHOOK_PASSPHRASE=your-secret      # same value as the indicator input
smc-agent -c config.yaml webhook           # listens on :8080 (put HTTPS in front: Caddy, nginx, Cloudflare Tunnel, ngrok)
```

In TradingView create an alert on *SMC ICT Pro* with condition **Any alert() function call** and
your webhook URL. Setups from the chart then go through the agent's risk manager (and the learner
and Claude, if enabled) before execution. Map TradingView tickers to your markets with
`tv_symbol` in the config.

## Development

```bash
pip install -e ".[dev]"
pytest                                   # engine, fills, backtest causality, learner, agent, webhook
python tradingview/build_strategy.py     # regenerate the strategy after editing the indicator
```

The engine is strictly causal (tests replay truncated histories and require identical signals), so
backtests and live trading run the same code path.
