"""``smc-agent check``: verify an installation before the agent trades.

It reads everything the agent will depend on (the config, the MT5 terminal,
account, symbol, prices, history, position sizing) and changes nothing: no
order is sent. Every problem comes with the fix.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import numpy as np

from .config import AppConfig, MarketConfig
from .core.timeframes import timeframe_minutes


@dataclass
class Check:
    status: str  # OK | WARN | FAIL | INFO
    title: str
    fix: str = ""


class Doctor:
    def __init__(self, cfg: AppConfig, connect: Callable[[Any], Any] | None = None) -> None:
        self.cfg = cfg
        self.results: list[Check] = []
        self._connect = connect

    # ------------------------------------------------------------ helpers
    def add(self, status: str, title: str, fix: str = "") -> None:
        self.results.append(Check(status, title, fix))

    @property
    def failed(self) -> bool:
        return any(c.status == "FAIL" for c in self.results)

    def report(self) -> str:
        lines = []
        for c in self.results:
            lines.append(f"[{c.status:^4}] {c.title}")
            if c.fix:
                lines.append(f"       -> {c.fix}")
        fails = sum(c.status == "FAIL" for c in self.results)
        warns = sum(c.status == "WARN" for c in self.results)
        lines.append("")
        if fails:
            lines.append(f"Result: {fails} problem(s) to fix and {warns} warning(s). The agent must not trade yet.")
        elif warns:
            lines.append(f"Result: ready, with {warns} warning(s) to read.")
        else:
            lines.append("Result: all good - the agent can trade with this setup.")
        return "\n".join(lines)

    # --------------------------------------------------------------- checks
    def run(self) -> list[Check]:
        self.check_general()
        uses_mt5 = self.cfg.broker.kind.lower() == "mt5" or any(m.feed.lower() == "mt5" for m in self.cfg.markets)
        if uses_mt5:
            self.check_mt5()
        return self.results

    def check_general(self) -> None:
        cfg = self.cfg
        v = sys.version_info
        self.add("OK" if v >= (3, 10) else "FAIL", f"Python {v.major}.{v.minor}.{v.micro}",
                 "" if v >= (3, 10) else "install Python 3.12 (64-bit)")
        kind = cfg.broker.kind.lower()
        names = ", ".join(f"{m.symbol} {m.timeframe} ({m.feed})" for m in cfg.markets)
        self.add("INFO", f"Broker: {kind} | markets: {names}")
        if kind == "paper":
            self.add("INFO", "Paper trading: orders are simulated, no money at risk",
                     "set broker.kind: mt5 in config.yaml when the paper results look right")
        if kind == "mt5":
            for m in cfg.markets:
                if m.feed.lower() != "mt5":
                    self.add("WARN", f"{m.symbol}: trades on MT5 but reads candles from {m.feed}",
                             "use feed: mt5 so the agent sees your broker's prices")
        r = cfg.risk.risk_per_trade_pct
        self.add("OK" if r <= 1.0 else "WARN", f"Risk per trade {r:g}% of equity",
                 "" if r <= 1.0 else "above 1% a normal losing streak hurts a lot; 0.25-1% is typical")
        s = cfg.strategy
        if s.tp1_r > 0:
            self.add("INFO", f"Partial take-profit: {s.tp1_pct:g}% at {s.tp1_r:g}R, then stop to entry")
        self.add("OK" if cfg.guard.enabled else "WARN", "Risk guard " + ("on" if cfg.guard.enabled else "OFF"),
                 "" if cfg.guard.enabled else "guard.enabled: true is strongly recommended for XAUUSD")
        for m in cfg.markets:
            if m.feed.lower() == "csv" and not Path(m.csv_path).exists():
                self.add("FAIL", f"{m.symbol}: CSV {m.csv_path!r} not found", "fix markets[].csv_path")
        state = Path(cfg.journal_path).parent
        try:
            state.mkdir(parents=True, exist_ok=True)
            probe = state / ".write_test"
            probe.write_text("ok")
            probe.unlink()
            self.add("OK", f"State folder writable: {state.resolve()}")
        except OSError as exc:
            self.add("FAIL", f"Cannot write to {state}: {exc}", "run the agent from a folder you own")
        n = cfg.notify
        if (os.environ.get(n.telegram_token_env) and os.environ.get(n.telegram_chat_id_env)) or os.environ.get(n.discord_webhook_env):
            self.add("OK", "Notifications configured (Telegram / Discord)")
        else:
            self.add("INFO", "No Telegram / Discord notifications",
                     f"set {n.telegram_token_env} and {n.telegram_chat_id_env} to get trades on your phone")
        if cfg.ai.enabled and not os.environ.get("ANTHROPIC_API_KEY"):
            self.add("FAIL", "ai.enabled is true but ANTHROPIC_API_KEY is not set",
                     "set the key, or ai.enabled: false (with fail_open false every trade would be skipped)")

    def check_mt5(self) -> None:
        from .execution.mt5_common import (ServerClock, allows_specified_expiry, connect, filling_for,
                                           suggest_symbols)

        b = self.cfg.broker
        if sys.platform != "win32":
            self.add("WARN", f"This computer runs {sys.platform}: the MetaTrader5 package needs Windows",
                     "run the agent on the Windows PC / VPS where MT5 is installed")
        try:
            mt5 = (self._connect or connect)(b)
        except ImportError:
            self.add("FAIL", "MetaTrader5 Python package not installed",
                     "run install.bat again, or: pip install MetaTrader5 (64-bit Python on Windows)")
            return
        except RuntimeError as exc:
            self.add("FAIL", "Cannot connect to the MetaTrader 5 terminal", str(exc))
            return
        try:
            self._mt5_terminal(mt5)
            acct = self._mt5_account(mt5)
            symbols = [m.symbol for m in self.cfg.markets if m.feed.lower() == "mt5" or b.kind.lower() == "mt5"]
            clock = ServerClock(b.mt5_server_time)
            desc = clock.detect(mt5, symbols)
            if clock.mode == "fixed":
                self.add("WARN", f"Broker server clock: {desc}",
                         "unusual for forex/gold brokers - confirm it, then set broker.mt5_server_time")
            else:
                self.add("OK", f"Broker server clock: {desc}" + ("" if clock.detected or b.mt5_server_time != "auto"
                                                              else " (assumed: market closed now)"))
            for m in self.cfg.markets:
                if m.symbol in symbols:
                    self._mt5_symbol(mt5, m, acct, clock, filling_for, allows_specified_expiry, suggest_symbols)
            mine_p = [p for p in (mt5.positions_get() or []) if p.magic == b.mt5_magic]
            mine_o = [o for o in (mt5.orders_get() or []) if o.magic == b.mt5_magic]
            if mine_p or mine_o:
                self.add("INFO", f"{len(mine_p)} open position(s) and {len(mine_o)} pending order(s) from this agent "
                                 f"(magic {b.mt5_magic}); they are reconciled at start-up")
        finally:
            try:
                mt5.shutdown()
            except Exception:  # noqa: BLE001
                pass

    def _mt5_terminal(self, mt5: Any) -> None:
        ti = mt5.terminal_info()
        if ti is None:
            self.add("FAIL", "MT5 terminal info unavailable", "restart the MetaTrader 5 terminal")
            return
        self.add("OK" if ti.connected else "FAIL",
                 f"MetaTrader 5 terminal {'connected to' if ti.connected else 'NOT connected to'} the broker "
                 f"({getattr(ti, 'company', '')}, build {getattr(ti, 'build', '?')})",
                 "" if ti.connected else "check the internet connection and the login in the MT5 terminal")
        if not ti.trade_allowed:
            self.add("FAIL", "Algo Trading is switched off in the terminal",
                     "click the 'Algo Trading' button in the MT5 toolbar so it turns green")
        else:
            self.add("OK", "Algo Trading is on")
        if getattr(ti, "tradeapi_disabled", False):
            self.add("FAIL", "Trading from Python is disabled in the terminal",
                     "Tools > Options > Expert Advisors: untick 'Disable automated trading via external Python API'")

    def _mt5_account(self, mt5: Any) -> Any:
        a = mt5.account_info()
        if a is None:
            self.add("FAIL", "No account logged in", "File > Login to Trade Account in the MT5 terminal")
            return None
        demo = a.trade_mode == getattr(mt5, "ACCOUNT_TRADE_MODE_DEMO", 0)
        real = a.trade_mode == getattr(mt5, "ACCOUNT_TRADE_MODE_REAL", 2)
        self.add("OK", f"Account {a.login} on {a.server} ({a.company}), {a.currency} {a.equity:,.2f}, "
                       f"leverage 1:{a.leverage}")
        if demo:
            self.add("OK", "DEMO account - the right place to start")
        elif real and self.cfg.broker.kind.lower() == "mt5":
            self.add("WARN", "REAL-money account: the agent will place real orders",
                     "run on a demo account first until the journal shows the behaviour you expect")
        if not a.trade_allowed:
            self.add("FAIL", "Trading is not allowed on this login",
                     "you are probably logged in with the investor (read-only) password - use the master password")
        if not getattr(a, "trade_expert", True):
            self.add("FAIL", "The broker disabled automated trading for this account", "ask the broker to enable it")
        hedging = a.margin_mode == getattr(mt5, "ACCOUNT_MARGIN_MODE_RETAIL_HEDGING", 2)
        if hedging:
            self.add("OK", "Hedging account: partial take-profit (TP1 + runner) supported")
        elif self.cfg.strategy.tp1_r > 0:
            self.add("WARN", "Netting account: the TP1 partial can't be split here, one full target is used",
                     "ask your broker for a hedging account, or set strategy.tp1_r: 0")
        return a

    def _mt5_symbol(self, mt5: Any, m: MarketConfig, acct: Any, clock: Any, filling_for: Any,
                    allows_specified_expiry: Any, suggest_symbols: Any) -> None:
        from .execution.mt5_common import mt5_timeframe

        sym = m.symbol
        info = mt5.symbol_info(sym)
        if info is None:
            similar = suggest_symbols(mt5, sym)
            self.add("FAIL", f"Symbol {sym!r} does not exist at this broker",
                     f"use the exact Market Watch name in markets[].symbol, e.g. {', '.join(similar)}" if similar
                     else "open Market Watch (Ctrl+M), right-click > Symbols, and copy the gold symbol's exact name")
            return
        if not getattr(info, "visible", True):
            mt5.symbol_select(sym, True)
        mode = getattr(info, "trade_mode", 4)
        if mode == getattr(mt5, "SYMBOL_TRADE_MODE_FULL", 4):
            self.add("OK", f"{sym}: tradable")
        elif mode in (0, getattr(mt5, "SYMBOL_TRADE_MODE_CLOSEONLY", 3)):
            self.add("FAIL", f"{sym}: trading disabled / close-only on this account", "ask the broker or pick another symbol")
        else:
            self.add("WARN", f"{sym}: long-only or short-only on this account")
        tick = mt5.symbol_info_tick(sym)
        if tick is None or not tick.bid:
            self.add("WARN", f"{sym}: no live price now (market closed?)", "run the check again when the market is open")
        else:
            spread = tick.ask - tick.bid
            lim = self.cfg.guard.max_spread
            self.add("OK" if not lim or spread <= lim else "WARN",
                     f"{sym}: bid {tick.bid} / ask {tick.ask}, spread {spread:.2f}",
                     "" if not lim or spread <= lim else
                     f"wider than guard.max_spread {lim} (normal around rollover/news; the guard skips entries meanwhile)")
        fill = filling_for(mt5, info)
        fill_name = {getattr(mt5, "ORDER_FILLING_FOK", 0): "FOK", getattr(mt5, "ORDER_FILLING_IOC", 1): "IOC"}.get(fill, "RETURN")
        expiry = "server-side expiry" if allows_specified_expiry(mt5, info) else "good-till-cancelled, the agent cancels expired orders"
        self.add("OK", f"{sym}: market orders fill {fill_name}; pending orders use {expiry}")
        stops = (getattr(info, "trade_stops_level", 0) or 0) * (getattr(info, "point", 0) or 0)
        if stops > 0:
            self.add("INFO", f"{sym}: broker minimum stop distance {stops:g}")

        # history and position size with a typical stop (1 ATR on the entry timeframe)
        minutes = timeframe_minutes(m.timeframe)
        try:
            tf = mt5_timeframe(mt5, minutes)
        except ValueError as exc:
            self.add("FAIL", f"{sym}: {exc}", "use 1m, 5m, 15m, 30m, 1h, 4h or 1d")
            return
        want = self.cfg.warmup_bars
        rates = mt5.copy_rates_from_pos(sym, tf, 1, want)
        n = 0 if rates is None else len(rates)
        if n >= want:
            self.add("OK", f"{sym} {m.timeframe}: {n} candles of history")
        elif n >= 1000:
            self.add("WARN", f"{sym} {m.timeframe}: only {n} of {want} candles of history",
                     "Tools > Options > Charts > Max bars in chart = Unlimited, open the chart and scroll back")
        else:
            self.add("FAIL", f"{sym} {m.timeframe}: {n} candles of history (need {want})",
                     "open the chart in MT5 and scroll back, and set Max bars in chart to Unlimited")
        if n >= 20 and acct is not None:
            h, lo, c = rates["high"].astype(float), rates["low"].astype(float), rates["close"].astype(float)
            tr = np.maximum(h[1:] - lo[1:], np.maximum(abs(h[1:] - c[:-1]), abs(lo[1:] - c[:-1])))
            atr = float(tr[-14:].mean())
            last = clock.to_utc([int(rates["time"][-1])])[0]
            self.add("INFO", f"{sym}: ATR(14) {atr:.2f}, last closed candle {last:%Y-%m-%d %H:%M} UTC")
            risk_money = acct.equity * self.cfg.risk.risk_per_trade_pct / 100.0
            loss_per_lot = atr / info.trade_tick_size * info.trade_tick_value if info.trade_tick_size else 0.0
            if loss_per_lot > 0:
                raw = risk_money / loss_per_lot
                lots = np.floor(raw / info.volume_step) * info.volume_step
                min_risk = info.volume_min * loss_per_lot
                if lots < info.volume_min:
                    self.add("FAIL", f"{sym}: the smallest lot ({info.volume_min}) risks {min_risk:,.2f} {acct.currency} "
                                     f"with a typical stop, more than {self.cfg.risk.risk_per_trade_pct:g}% "
                                     f"({risk_money:,.2f})",
                             "every trade would be skipped: add funds or raise risk.risk_per_trade_pct")
                else:
                    self.add("OK", f"{sym}: a typical trade is {lots:.2f} lots risking {risk_money:,.2f} {acct.currency}")
                    if self.cfg.strategy.tp1_r > 0 and lots < 2 * info.volume_min:
                        self.add("WARN", f"{sym}: {lots:.2f} lots can't be split for the TP1 partial",
                                 "trades use one full target until the size allows two parts")
