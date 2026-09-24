# SMC / ICT Trading Agent + TradingView Indicator

Two tools that share one brain:

1. **An AI trading agent** (Python) that watches your markets candle by candle, reads them the ICT
   / Smart Money Concepts way, finds A-grade setups, and runs each one through a **risk guard**
   that reads every timeframe (H1/H4/D1/W1) and watches for news, volatility shocks, sessions,
   weekends and losing streaks. It then filters with a model it trains on your own market, can ask
   Claude for a senior-trader review, sizes the position and executes: paper by default, live
   through MetaTrader 5 (XAUUSD, forex) or ccxt (crypto). Built with **XAUUSD** in mind.
2. **A TradingView indicator** (`tradingview/SMC_ICT_Pro.pine`, Pine v6) for manual trading.
   It shows the same analysis on your chart: structure, order blocks, FVGs, breakers, inverse
   FVGs, BPRs, OTE, liquidity, sweeps, Judas swings, SMT divergence, killzones, Silver Bullet,
   the midnight open and opening gaps, premium/discount and HTF bias. It also shows the same
   BUY/SELL setups step by step, with a 15-point confirmation checklist, entry, stop, TP1, target
   and grade. Around that sit a live stats dashboard, phone alerts, and a webhook that lets the
   agent execute what the chart sees.

```
                ┌──────────────── TradingView ────────────────┐
                │ SMC ICT Pro indicator  ── alert() JSON ──┐  │
                └──────────────────────────────────────────┼──┘
 market data ─► SMC engine ─► setup ─► RISK GUARD ─► learned ─► risk ─► Claude ─► broker
 (MT5/csv/      (structure, OB,       (H1-W1 top-down,  edge    manager  review  (paper/MT5/ccxt)
  ccxt/yf)       FVG, liquidity)       news, shocks,                              │
                                       sessions, streaks)     guard keeps watching open trades
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
| `smc_agent/guard.py`, `core/mtf.py`, `news.py` | the risk guard, top-down timeframe context, news calendar ([guide](docs/guard.md)) |
| `smc_agent/ai/analyst.py` | Claude trade reviewer and market brief writer |
| `smc_agent/live.py`, `webhook.py` | the autonomous agent loop and the TradingView webhook server |
| `smc_agent/execution/` | paper broker, ccxt broker, MT5 broker |
| `docs/strategy.md` | the trading model in detail, scoring, research results |
| `docs/guard.md` | every caution rule, defaults, and what the guard costs / buys |
| `config.example.yaml` | XAUUSD configuration, every setting documented (`config.crypto.example.yaml` for crypto) |

## The trading model in one minute

* **Reversal (ICT 2022 model):** liquidity is swept, then an internal market structure shift
  with displacement. Entry is a limit at the 50% of the fair value gap (or the order block), the
  stop goes beyond the sweep, and the target is the next opposing liquidity pool (≥ 1.5R).
* **Continuation:** an internal BOS in the direction of swing structure, with the entry at the
  leg's FVG/OB and the stop beyond the protected higher low / lower high.
* **Confluence score 0–10:** HTF bias (2), premium/discount (2), major liquidity swept, OB+FVG
  overlap, killzone, displacement, swing trend, RR ≥ 3 (1 each). By default only **A-grade
  (≥ 6)** setups trade.

## The risk guard (why it is cautious)

Every setup must pass these checks before it becomes an order. Pending orders and open trades are
re-checked on every candle ([details](docs/guard.md)).

* **Every timeframe, top-down.** It never trades against the D1 trend. It trades against H4 only
  from H4 discount/premium (a pullback), never at the top or bottom 10% of the H4/D1 range, and
  never inside an opposing HTF fair value gap. If PDH/PWH, an HTF swing or an HTF FVG sits in the
  way, the target is moved in front of it, or the trade is refused.
* **News.** Uses the ForexFactory calendar (or your CSV) plus the 08:30 / 10:00 / 14:00 NY release
  windows. Pending orders are cancelled and open trades closed before high-impact USD events. If
  the feed is down, it trades half size.
* **Unexpected moves.** Candles or gaps over 4× normal pause trading for 90 minutes. It stands
  aside when volatility is extreme, halves size when it is elevated, and stops once the day has
  moved 1.3× its average range.
* **Sessions.** No entries around the daily rollover, on Friday afternoon, at the Sunday open or
  on holidays. Positions are closed before the weekend.
* **Streaks.** Pauses after 3 losses, stops at −3R for the day or −6R for the week, and halts at a
  −12R drawdown.
* **Infrastructure.** Watches the spread, cleans bad ticks, stops on a stale feed, reconciles open
  positions after a restart, alerts on errors, and has a kill switch (`state/HALT`,
  `state/FLATTEN`).

Across 14 historical datasets (forex, crypto, index, stock; 1m–1D) with the default A-grade
filter, the rules alone produced 192 trades: 39% win rate, +0.51R per trade, profit factor 1.88,
worst drawdown 8.7R. **With the guard** they produced 64 trades: 47% win rate, **+0.85R per
trade**, profit factor 2.63, worst drawdown **4.0R**. Random-walk control data scored ≈0R without
the guard (12 trades with it: too few to mean anything). None of this data is gold, so validate on
your own XAUUSD history. Details and caveats: [docs/strategy.md](docs/strategy.md),
[docs/guard.md](docs/guard.md).

### Win rate vs profit

A 39% or 47% win rate does **not** mean losing money. What matters is win rate × average win vs
loss rate × average loss. In the guarded sample the average winner was **+2.93R** and the average
loser **−0.98R**, so 47 wins and 53 losses per 100 trades net about **+85R**. At $50 risk per trade
that is roughly +$4,266 per 100 trades, before costs.

If losing more often than winning is hard to live with, the agent and the indicator now take a
**partial profit at TP1**. At +1.5R, 50% of the position closes and the stop of the rest moves to
the entry. The same trades then show a **67% win rate**, +0.72R per trade, profit factor 3.2 and
the smallest drawdown (3.1R). This is the default for XAUUSD (`strategy.tp1_r: 1.5`; set it to 0
for one full target). The table for every exit variant is in
[docs/strategy.md](docs/strategy.md#trade-management-win-rate-is-not-profit).

## Quick start: TradingView indicator

Pine Editor → new indicator → paste `tradingview/SMC_ICT_Pro.pine` → Save → Add to chart.
Every setup is numbered on the chart as it forms: ① liquidity taken, ② market structure shift,
**BUY / SELL** with the entry, SL, TP1 and TP lines, ④ filled. Hover the label for the 15-point
ICT/SMC confirmation checklist and for how similar setups did on your chart. One alert
(*Any alert() function call*) sends each setup to your phone with the entry, SL, TP1 and TP. See
[tradingview/README.md](tradingview/README.md).

## Quick start: the agent

```bash
pip install -e ".[all]"            # core + anthropic, ccxt, yfinance, plotly  (+ MetaTrader5 on Windows)
cp config.example.yaml config.yaml # XAUUSD by default: edit symbol, risk, broker
```

**Backtest** on any CSV (TradingView: chart → *Export chart data*) or on the configured feed:

```bash
smc-agent -c config.yaml backtest --csv OANDA_XAUUSD_15.csv --chart gold.html --trades gold.csv
smc-agent -c config.yaml backtest --csv OANDA_XAUUSD_15.csv --no-guard     # compare without the guard
smc-agent -c config.yaml backtest --csv OANDA_XAUUSD_15.csv --set min_score=5 --set guard.mtf_min_aligned=2
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
smc-agent -c config.yaml scan                 # structure, zones, liquidity, H1-W1 context, guard state
export ANTHROPIC_API_KEY=...                  # or `ant auth login`
smc-agent -c config.yaml brief --market XAUUSD
```

**Run the agent** (paper broker unless you change `broker.kind`):

```bash
smc-agent -c config.yaml run
```

Every candle close it updates the engine and the guard. The guard first manages what is already
working: it cancels pending orders, closes positions before news or the weekend, and moves stops
to entry. Each new setup then runs the decision pipeline:

1. the learned expected-R filter
2. the **risk guard** (which may also cap the target)
3. the account risk manager (daily loss limit, max positions, trades per day, RR floor)
4. the Claude review (`ai.enabled`), which receives the whole multi-timeframe risk context
5. position sizing and a limit order with attached stop and target

Everything is written to `state/journal.jsonl`. Set `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID` or
`DISCORD_WEBHOOK_URL` for notifications.

Emergency controls: `touch state/HALT` stops new entries. `touch state/FLATTEN` closes everything
and halts.

### Going live

| Broker | Config | Notes |
|---|---|---|
| Paper | `broker.kind: paper` | default; same fill rules as the backtester; state survives restarts |
| Crypto (Binance, Bybit, OKX, …) | `kind: ccxt`, `exchange`, `market_type`, `testnet: true` | keys from `EXCHANGE_API_KEY` / `EXCHANGE_API_SECRET`; limit entry with exchange-side SL/TP |
| **XAUUSD**, forex, CFDs, indices | `kind: mt5` + `feed: mt5` | Windows + MT5 terminal, `pip install MetaTrader5`; pending limit with native SL/TP and expiry; lot size from tick value, capped by `max_leverage`; the guard can close, cancel and move stops; live spread checked |

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
