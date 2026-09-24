# The agent's school: how it reads the chart

The agent is an ICT / Smart Money Concepts trader implemented as rules, then refined by data.
Everything below is computed bar by bar on closed candles only (`smc_agent/core/engine.py`), and
the TradingView indicator runs the identical logic.

## 1. What it tracks

| Concept | Definition used |
|---|---|
| Pivot high (strength *n*) | `high[c]` strictly above the *n* candles before it and ≥ the *n* candles after it (confirmed *n* bars later) |
| Internal / swing structure | pivots of strength `internal_len` (5) and `swing_len` (20) |
| BOS / CHoCH | a close through the latest unbroken pivot; CHoCH if it flips the prior trend, else BOS |
| Order block | on every break: the candle with the lowest low (bullish) / highest high (bearish) between the broken pivot and the break |
| Fair value gap | three-candle imbalance: `low[t] > high[t-2]` with the middle candle closing beyond `high[t-2]`, size ≥ 0.1 ATR (mirrored for bearish) |
| Liquidity | resting beyond internal & swing pivots, equal highs/lows (within 0.1 ATR), previous-day high/low, Asia and London session high/low |
| Sweep | a candle trades through a tracked level (the level is then removed) |
| Premium / discount | above / below the midpoint of the trailing swing range |
| HTF bias | swing structure (pivot 5) of completed higher-timeframe candles; auto 1m→15m, 5m→1H, 15m/1H→4H, 4H→D, D→W |
| Killzones (NY time) | Asia 20–00, London 02–05, NY AM 07–10, London close 10–12, NY PM 13:30–16 |

## 2. Entry models

**Reversal — sweep → MSS → FVG.** Sell-side liquidity is taken (the lowest point of that move is
the *sweep extreme*). Within `mss_window` bars price closes above the latest internal pivot high
(market structure shift). The entry is a limit at the consequent encroachment (50%) of the most
recent bullish FVG formed after the sweep; if no FVG forms within `fvg_wait` bars the MSS order
block is used. Stop: sweep extreme − 0.1 ATR. Bearish is the mirror.

**Continuation — BOS with the trend.** Swing structure is bullish and an internal BOS prints up.
Entry at the leg's FVG / order block, stop below the protected internal higher low.

**Target.** The nearest untaken opposing liquidity pool whose reward:risk is between `min_rr`
(1.5) and `max_rr` (6); otherwise a fixed `rr_target` (2R).

**Order handling.** Limit orders live `entry_expiry` (20) bars and are cancelled if price reaches
the target first. A fresher setup replaces a pending order; setups are ignored while a position is
open.

## 3. Confluence score

| Confluence | Points |
|---|---|
| HTF bias in trade direction | 2 |
| Entry in discount (long) / premium (short) | 2 |
| A *major* pool was swept (swing, EQH/EQL, PDH/PDL, session) | 1 |
| Entry FVG overlaps an order block (or vice versa) | 1 |
| Armed inside a scoring killzone | 1 |
| Displacement candle (body ≥ 1 ATR) | 1 |
| Swing structure in trade direction | 1 |
| RR ≥ 3 | 1 |

Grades: **A+** ≥ 8, **A** ≥ 6, **B** ≥ 4, C below. Default threshold: 6.

## 4. Research results (and their limits)

Backtests use conservative fills: limit fills at the entry (or better on a gap), on the fill candle
only the stop is checked, afterwards the stop is always checked before the target. Results are
gross R multiples. Data available to this project: EURUSD 1H (2017–18), EURUSD 4H, six altcoin/BTC
pairs on 5m (Jan 2018), a BTC-quoted pair on 1m, XRP/USDT perpetual 5m (2021), an equity index 5m
(2006), S&P 500 1m (Nov 2019), GOOG daily (2004–13) — 14 datasets, ~70k candles. The same code on
six random-walk series is the control: an edge there would indicate a bug or look-ahead.

| Score threshold | Trades | Win rate | Avg R | Profit factor | Datasets positive | Random walk avg R |
|---|---|---|---|---|---|---|
| none | 525 | 36% | +0.21 | 1.33 | 12 / 14 | −0.06 |
| ≥ 4 | 435 | 35% | +0.23 | 1.36 | 11 / 14 | −0.03 |
| ≥ 5 | 332 | 36% | +0.31 | 1.51 | 10 / 14 | −0.08 |
| **≥ 6 (default)** | **191** | **39%** | **+0.55** | **1.95** | **11 / 12** | **+0.00** |

Per-feature evidence (all setups, independent outcomes): entries in discount/premium averaged
+0.30R vs +0.09R, HTF-aligned +0.23R vs +0.12R, OB+FVG overlap +0.26R vs +0.13R. Killzone and
displacement flags showed no edge on this (mostly 24/7 crypto) data, so they carry weight 1.

How the defaults were chosen: `internal_len` 5 and `swing_len` 20 won a small sweep and are
standard SMC settings; the score weights for HTF and premium/discount were raised to 2 because
the data and ICT theory agreed. That is *some* in-sample selection on these datasets — treat the
table as evidence the logic is sound, not as a forecast. Costs are not included above
(`smc-agent backtest` reports fees; limit entries at maker fees cost roughly 0.05–0.15R per trade
on 5m crypto).

Weak spots seen: the two equity-index intraday samples lost (few trades), and results vary a lot
between markets. That is why the agent ships with:

* `smc-agent optimize` — walk-forward search reporting in-sample **and** out-of-sample results;
* `smc-agent train` — learns *your* market's confluence weights (logistic model, walk-forward
  validated) and filters trades by expected R;
* paper trading by default.

## 5. The learner

`smc_agent/ai/learner.py` fits `P(target before stop)` from the setup's confluence flags, reward:risk
and stop size, pooled over the markets you train on. A setup is taken only when
`p·RR − (1−p) ≥ min_expected_r`. On the seven intraday markets above, trained on the first 70% of
each market and tested on the last 30%: all setups +0.23R/trade (185 trades), model-filtered
+0.26R/trade (95 trades, win rate 38% → 41%). Modest but out-of-sample. Retrain periodically.

## 6. The Claude reviewer

With `ai.enabled`, every setup that passes the rules, learner and risk checks is sent to Claude
with the engine's market snapshot (structure, PD arrays, liquidity map, HTF bias, session) and the
last 80 candles. Claude answers `take`, `reduce` (half size) or `skip`, with its bias, draw on
liquidity, reasoning and risks — it can veto or downsize, never move prices. Unavailable API → the
trade is skipped (`fail_open: false`).
