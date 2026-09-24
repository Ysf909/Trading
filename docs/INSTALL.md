# Installation guide

Two parts, independent of each other:

* **Part A - the TradingView indicator** for your own manual trades (5 minutes).
* **Part B - the automatic bot on MetaTrader 5** (about 45 minutes the first time).

> Start the bot on a **demo account**. Move to real money only after a few weeks of demo trades
> that you have checked yourself. No indicator or bot can guarantee profits.

---

## Part A - TradingView indicator

TradingView doesn't import files: you paste the code into the Pine Editor.

1. Download **`SMC_ICT_Pro.pine`** (folder `tradingview/`) and open it with Notepad. Select all
   (`Ctrl+A`), then copy (`Ctrl+C`).
2. Open a chart on tradingview.com (for example **OANDA:XAUUSD**, 15 minutes).
3. At the bottom of the chart, open **Pine Editor**. Then **Open > New blank indicator**, delete
   everything in the editor, and paste (`Ctrl+V`).
4. Click **Save** (name it *SMC ICT Pro*), then **Add to chart**.
5. **Phone notifications:**
   1. Install the TradingView app on your phone, log in, and allow its notifications.
   2. On the chart, click **Alert** (the clock icon, or `Alt+A`).
   3. Set *Condition* to **SMC ICT Pro**, then **Any alert() function call**.
   4. Set *Expiration* to open-ended, tick **Notify in app**, and click **Create**.

   You then get every setup with its entry, SL, TP1, TP and the confirmation checklist.

Optional: repeat steps 1-4 with `SMC_ICT_Pro_Strategy.pine` to test the rules in the
**Strategy Tester**.

What every mark on the chart means: [tradingview/README.md](../tradingview/README.md).

---

## Part B - the bot on MetaTrader 5

### How it works

The bot is a Python program that runs **next to** your MT5 terminal on a Windows computer. It
controls MT5 through MetaQuotes' official Python connection. It is **not** an Expert Advisor: you
don't drag it onto a chart, and it doesn't appear in MT5's Navigator. Its orders appear in MT5's
*Trade* tab with the comment `smc ...` and the magic number `909909`. It never touches trades it
didn't open.

```
  MetaTrader 5 terminal (logged in, Algo Trading ON)
          ^   prices, orders
          |
  SMC agent (windows\start_agent.bat)  --->  Telegram on your phone
```

### What you need

* A Windows 10/11 PC that stays on while you trade, or a Windows VPS (best for 24/5 trading).
* MetaTrader 5 from **your broker's website**, with a **demo account** first. Ask the broker for
  a **hedging** account: the partial take-profit (TP1) needs one.
* Python **3.12, 64-bit** (free).

### Step 1 - Prepare MetaTrader 5

1. Install MT5 from your broker. Log in with **File > Login to Trade Account**, using the
   account number, the **master** password (not the investor password) and the server.
2. **Tools > Options > Expert Advisors**:
   * tick **Allow algorithmic trading**
   * untick **Disable automated trading via external Python API** (if it is shown)
3. Click the **Algo Trading** button in the top toolbar so it turns **green**.
4. **Tools > Options > Charts > Max bars in chart**: choose **Unlimited**, then restart MT5.
5. Press `Ctrl+M` (Market Watch). Right-click, choose **Symbols**, search *XAU* or *gold*,
   select your broker's gold symbol and click **Show**. **Write down its exact name**: `XAUUSD`,
   `XAUUSD.m`, `XAUUSDm`, `GOLD`, etc.
6. Open a **XAUUSD M15** chart and press `Home` a few times so MT5 downloads a few months of
   history.

### Step 2 - Install Python

1. Go to <https://www.python.org/downloads/windows/> and download **Python 3.12.x, Windows
   installer (64-bit)**.
2. On the installer's first screen tick **Add python.exe to PATH**, then click **Install Now**.

### Step 3 - Install the bot

1. Download **`SMC-ICT-Agent-Windows.zip`** and extract it to a simple folder such as
   `C:\SMC-Agent`. Avoid *Program Files*, *OneDrive* and *Downloads*.
2. Open `C:\SMC-Agent\windows` and double-click **`install.bat`**.
   * If Windows shows "Windows protected your PC", click **More info > Run anyway**.
   * The first install takes 2-5 minutes. At the end it opens `config.yaml` in Notepad.

### Step 4 - Settings (`config.yaml`)

Change these lines, save, and close Notepad:

```yaml
markets:
  - symbol: XAUUSD      # <- the EXACT name from Step 1.5 (e.g. XAUUSD.m)
    timeframe: 15m
    feed: mt5

risk:
  risk_per_trade_pct: 0.5   # % of the account risked per trade (0.25 - 1 is sensible)

broker:
  kind: paper               # paper first; mt5 = real orders on the logged-in account
```

Everything else is already set for gold: the risk guard, news, sessions, and the TP1 partial at
1.5R. Each line is explained in the file itself.

### Step 5 - Check the installation

Double-click **`windows\check.bat`**. It checks the whole chain without sending any order:

* MT5 is running, connected and has Algo Trading on
* the account type (demo or real), and hedging vs netting
* the symbol name, spread, filling mode and order expiry
* your broker's clock, the candle history, and the position size for a typical trade

Fix every `[FAIL]` line; each one says how. Run it again until the last line reads **Result:
ready** or **all good**.

### Step 6 - Test on your broker's history

Double-click **`windows\backtest.bat`**. The bot replays up to 20,000 recent candles of your
broker's gold, sends no orders, and opens a chart of every trade it would have taken. The
trade list is saved to `state\backtest_trades.csv`.

### Step 7 - Paper trading with live prices (1-2 weeks)

Keep `broker: kind: paper` and double-click **`windows\start_agent.bat`**. The bot reads live
MT5 prices and simulates the trades. It writes everything to `state\journal.jsonl` and sends
Telegram messages if you set them up (step 10). Leave the window open; closing it stops the
bot.

### Step 8 - Real orders on the demo account

1. In `config.yaml` set `broker: kind: mt5`. Keep MT5 logged in to the **demo** account.
2. Run `check.bat` again, then `start_agent.bat`.
3. The bot's orders appear in MT5 (*Toolbox > Trade*) with their SL and TP:
   * a limit order at the entry, with the stop and the target
   * on a hedging account, two orders: the TP1 half and the runner
   * when TP1 is hit, the runner's stop moves to the entry

### Step 9 - Real account (only after the demo looks right)

Log MT5 in to the real account, then run `check.bat`. It shows a `REAL-money account`
warning; read it before starting. Start with the minimum risk.

### Step 10 - Telegram notifications (optional, recommended)

1. In Telegram, talk to **@BotFather**, send `/newbot`, follow the steps and copy the **token**.
2. Send any message to your new bot. Then open
   `https://api.telegram.org/bot<TOKEN>/getUpdates` in a browser and copy the number after
   `"chat":{"id":`.
3. Open `windows\secrets.bat` with Notepad and fill in `TELEGRAM_BOT_TOKEN=` and
   `TELEGRAM_CHAT_ID=`. Save, then restart `start_agent.bat`.

### Controls

| Double-click | Effect |
|---|---|
| `start_agent.bat` | runs the bot; it restarts by itself after an error. Close the window to stop it |
| `stop_new_trades.bat` | no new trades; open trades keep their SL/TP and are still managed |
| `resume_trading.bat` | allows new trades again (after the guard's drawdown halt, restart `start_agent.bat` instead) |
| `close_everything.bat` | cancels all its orders and closes all its positions within seconds, then pauses |
| `check.bat` / `scan.bat` | installation check / what the bot sees on the market now |

If the bot stops (PC off, window closed), its open trades **keep their stop loss and take
profit at the broker**. When it restarts, it finds and manages them again.

### Keep it running 24/5

* Use a Windows VPS near your broker's server, or set the PC to never sleep: **Settings > System
  > Power > Sleep: Never**.
* MT5 must stay open and logged in, with Algo Trading green. MT5 remembers the login.
* To start the bot automatically after a reboot: press `Win+R`, type `shell:startup`, and put a
  shortcut to `windows\start_agent.bat` in that folder. Put one to `terminal64.exe` there too.

### Several MT5 terminals, or logging in from the bot

If you have more than one MT5 installed, or you want the bot to log in by itself, set these in
`config.yaml`:

```yaml
broker:
  mt5_path: C:\Program Files\YourBroker MetaTrader 5\terminal64.exe
  mt5_login: 12345678
  mt5_server: YourBroker-Demo
```

Put the password in `windows\secrets.bat` as `MT5_PASSWORD=...`, never in `config.yaml`.

### Troubleshooting

| Message | Fix |
|---|---|
| `MT5 initialize() failed ... IPC timeout` | MT5 isn't running or is still starting. Open it, log in, and try again. With several terminals, set `mt5_path` |
| `symbol 'XAUUSD' not found ... your broker has: XAUUSD.m` | put that exact name in `markets: symbol:` |
| `10027 AutoTrading disabled by client` | the **Algo Trading** button is off (Step 1.3) |
| `Trading is not allowed on this login` | you logged in with the investor password; use the master password |
| `10019 not enough money` | the account is too small for the risk setting; lower `risk_per_trade_pct` or add funds |
| `10018 market closed` | outside gold trading hours; the bot waits by itself |
| `the smallest lot risks ...` (check) | 0.01 lot is already more than your risk %; raise the risk % or the balance |
| `Netting account` warning | TP1 can't be split; one full target is used. Ask the broker for a hedging account |
| Times look wrong (sessions, news) | `check.bat` shows the broker clock it detected. If your broker isn't "New York + 7h", set `broker: mt5_server_time: +2` (or the right offset) |
| `pip` errors mentioning MetaTrader5 | install **Python 3.12 64-bit** and run `install.bat` again |

For more detail, read the log in the bot's window and `state\journal.jsonl`. Every decision,
order, fill, guard action and error is written there.

### TradingView -> MT5 (optional)

The bot can also execute the TradingView indicator's setups. Set `WEBHOOK_PASSPHRASE` in
`secrets.bat` and start `windows\start_webhook.bat` instead of `start_agent.bat`. Then create a
TradingView alert with the webhook URL of this PC. TradingView only sends webhooks to public
HTTPS addresses, so this needs a VPS with a domain or a tunnel such as Cloudflare Tunnel or
ngrok. Details: [tradingview/README.md](../tradingview/README.md#agent-webhook-automatic-execution).
