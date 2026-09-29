# The risk guard: how the agent stays cautious

The SMC engine *finds* setups. The guard decides whether a setup is safe to take **right now**,
across every timeframe, and keeps watching pending orders and open trades on every candle. It
runs the same rules in the backtester, the live agent (`smc_agent/guard.py`) and the TradingView
indicator ("Risk guard" inputs). Settings live under `guard:` in the config.

Built for XAUUSD first: gold hunts stops around the London and New York opens, explodes on US
data, widens its spread at the 17:00 NY rollover and gaps over weekends.

## 1. Top-down, every timeframe (H1 / H4 / D1 / W1)

Before any entry the agent reads the higher timeframes from **completed candles only**. For each
one it tracks the swing trend, the dealing range, the unbroken swing high/low, the latest unfilled
bullish and bearish FVG, and the previous candle's high/low.

| Rule | Default | Why |
|---|---|---|
| Never against the **bias** timeframe | D1 | the daily trend is the tide |
| Against the **structure** timeframe only from its discount (longs) / premium (shorts) | H4 | buy an H4 pullback inside a D1 uptrend, never the top of it |
| At least *n* higher timeframes agree | 1 | at least one higher TF must point the same way |
| No longs above 90% / shorts below 10% of the H4 or D1 range | 0.9 | don't buy the high of the range |
| No entry inside an opposing HTF fair value gap | on | don't buy into H1/H4/D1 supply |
| **Obstacles**: PDH/PDL, PWH/PWL, HTF swing highs/lows and HTF FVGs between entry and target | on | the target is moved just before the obstacle; if that leaves less than `min_rr` the trade is refused |
| No usable higher-timeframe structure yet | block | never trade blind |

## 2. News

| Source | Blocks new entries | Pending orders | Open trades |
|---|---|---|---|
| Calendar events (ForexFactory weekly feed, cached; or your CSV `news_file`) for `news_currencies` at `news_min_impact` | 30 min before → 30 min after | cancelled | `news_open_action`: **close** (default), **protect** (stop to entry if in profit) or hold, 30 min before |
| Standard US release windows 08:30 / 10:00 / 14:00 NY (always on, feed or not) | 10 min before → 20 min after | kept | – |
| Calendar bank holidays for USD | whole day | cancelled | – |

If the feed can't be reached, the agent keeps going with the standard windows **at half size**
and tells you. On TradingView (Pine has no calendar) you enter up to three high-impact events per
week (*Guard: news* inputs); the standard windows are always checked.

## 3. Abnormal price action

| Rule | Default |
|---|---|
| **Shock**: a candle range or an open gap larger than 4× the normal range (ATR 100, measured *before* the candle) | no entries and pending orders cancelled for 90 minutes |
| **Extreme regime**: ATR(14) / ATR(100) above 2.5 | stand aside |
| **Elevated regime**: above 1.8 | half size |
| **Extended day**: today's range ≥ 1.3× the 10-day average range | no new entries |

Normal ICT displacement candles (1.5–3× ATR) are *not* shocks; news spikes and flash moves are.

## 4. Sessions (New York time, `market_hours: forex`)

| Rule | Default |
|---|---|
| Daily rollover (spreads widen) | 16:45 – 18:30 no entries |
| Friday afternoon | no entries after 14:00, pending orders cancelled |
| Weekend | positions **closed** at Friday 16:00 (`weekend_action: close`) |
| Sunday open | no entries before 19:00 |
| Holidays | 12-24, 12-25, 12-31, 01-01 (add your own) |

Candle times follow the broker day for gold (`strategy.day_tz: America/New_York`,
`day_roll_hour: 17`): daily candles run 17:00 → 17:00 NY, weekly candles start Sunday 17:00, H4
candles start at 17:00 / 21:00 / 01:00 …, so PDH/PDL, PWH/PWL and the HTF structure match
TradingView and MT5.

## 5. Losing streaks and drawdown (in R, identical on TradingView)

| Rule | Default |
|---|---|
| 3 losses in a row | pause 16 bars (4 h on M15) |
| Daily loss | −3R → done for the day |
| Weekly loss | −6R → done for the week |
| Drawdown from the equity peak | −12R → **halted** until you restart |

The account-level limits in `risk:` (daily loss %, max positions, trades per day) still apply on
top.

## 6. Execution and infrastructure

| Scenario | What the agent does |
|---|---|
| Spread wider than `max_spread` (0.60 on gold) or 30% of ATR | skips the entry (live: MT5 / exchange spread; backtests: a `spread` column in price units, if your data has one) |
| Price already beyond the stop when the order is sent | refuses the order (MT5) |
| Position size above `max_leverage` × equity | capped (MT5) |
| Bad ticks (zero/negative prices, high below low) | dropped / repaired when data is loaded |
| Data feed stale (newest candle older than 3 bars) | no new entries, notification, recovery notice |
| Broker / API errors | the order is skipped and journaled; repeated poll failures are escalated to Telegram/Discord |
| Restart with positions already open | reconciled and reported; no duplicate entries |
| `touch state/HALT` | no new entries |
| `touch state/FLATTEN` | cancels every order, closes every position, then halts |
| Claude API down | the trade is skipped (`ai.fail_open: false`) |

## 7. What the guard costs and buys

Same 14 historical datasets as `docs/strategy.md` (forex, crypto, index, stock; 1m–1D), default
strategy (score ≥ 6):

| | Trades | Win rate | Avg R | Profit factor | Worst drawdown | Avg drawdown |
|---|---|---|---|---|---|---|
| Without guard | 192 | 39% | +0.51 | 1.88 | 8.7R | 4.2R |
| **With guard** | **64** | **47%** | **+0.85** | **2.63** | **4.0R** | **1.5R** |

It takes about a third of the trades, and each one is better. Drawdowns are roughly halved. Most
refusals come from the top-down rules (trades against D1/H4 or into HTF obstacles), then
volatility and news. Three guard settings were adjusted while looking at these same datasets
(soft H4 rule, shock measured on the slow ATR, pending orders not cancelled by the generic
windows), so treat the table as evidence, not a forecast. None of this data is gold. Run it on
your own XAUUSD history:

```bash
smc-agent -c config.example.yaml backtest --csv OANDA_XAUUSD_15.csv            # with guard
smc-agent -c config.example.yaml backtest --csv OANDA_XAUUSD_15.csv --no-guard # compare
smc-agent -c config.example.yaml backtest --csv OANDA_XAUUSD_15.csv --set guard.mtf_min_aligned=2
smc-agent -c config.example.yaml scan --csv OANDA_XAUUSD_15.csv                # what it sees now
```

For backtests with real news, export a calendar to CSV (`time,currency,impact,title`, ISO time
with timezone; impact `high`, `medium`, `low` or `holiday`) and set `guard.news_file`. The format
is shown in `examples/news_events.example.csv` (its dates are illustrative only, so always use
the official schedule). The same file is merged with the live feed, which is useful for events
the feed misses.
