"""Command line interface: ``smc-agent <command>``.

    backtest   replay history through the engine and simulate the trades
    train      learn the agent's own confluence weights (walk-forward)
    optimize   walk-forward parameter search
    scan       print the current SMC state and any fresh setups per market
    brief      ask Claude for an ICT-style trading plan for a market
    run        start the autonomous agent (paper by default)
    webhook    execute setups sent by the TradingView indicator
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any

import pandas as pd
import yaml

from .config import AppConfig, MarketConfig, load_config, strategy_from_overrides

log = logging.getLogger("smc_agent")


def _tf_name(minutes: int) -> str:
    if minutes % 1440 == 0:
        return f"{minutes // 1440}d"
    if minutes % 60 == 0:
        return f"{minutes // 60}h"
    return f"{minutes}m"


def _market(cfg: AppConfig, symbol: str | None) -> MarketConfig:
    if symbol is None:
        return cfg.markets[0]
    for m in cfg.markets:
        if m.symbol == symbol or m.tv_symbol == symbol:
            return m
    raise SystemExit(f"market {symbol!r} not found in config")


def load_data(args: argparse.Namespace, cfg: AppConfig) -> tuple[pd.DataFrame, str, str]:
    """Return (ohlcv, symbol, timeframe) from --csv or the configured feed."""
    from .data.feeds import load_csv, make_feed, resample

    if args.csv:
        path = args.csv[0]
        df = load_csv(path)
        if args.timeframe:
            df = resample(df, args.timeframe)
            tf = args.timeframe
        else:
            spacing = df.index.to_series().diff().dropna().median()
            tf = _tf_name(max(1, int(spacing.total_seconds() // 60)))
        if args.bars:
            df = df.iloc[-args.bars:]
        return df, Path(path).stem, tf
    m = _market(cfg, args.market)
    if args.timeframe:
        m = MarketConfig(**{**asdict(m), "timeframe": args.timeframe})
    df = make_feed(m).history(args.bars or 5000)
    return df, m.symbol, m.timeframe


def _datasets(args: argparse.Namespace, cfg: AppConfig) -> list[tuple[pd.DataFrame, str, str]]:
    """All datasets for multi-market commands: every --csv, else every configured market."""
    out = []
    if args.csv:
        for path in args.csv:
            out.append(load_data(argparse.Namespace(**{**vars(args), "csv": [path]}), cfg))
    elif args.market:
        out.append(load_data(args, cfg))
    else:
        for m in cfg.markets:
            out.append(load_data(argparse.Namespace(**{**vars(args), "market": m.symbol}), cfg))
    return out


def _apply_overrides(cfg: AppConfig, sets: list[str]) -> None:
    overrides: dict[str, Any] = {}
    for item in sets or []:
        if "=" not in item:
            raise SystemExit(f"--set expects key=value, got {item!r}")
        k, v = item.split("=", 1)
        overrides[k.removeprefix("strategy.")] = yaml.safe_load(v)
    if overrides:
        cfg.strategy = strategy_from_overrides(cfg.strategy, overrides)


# ----------------------------------------------------------------- commands
def cmd_backtest(args: argparse.Namespace, cfg: AppConfig) -> None:
    from .backtest import format_report, run_backtest

    df, symbol, tf = load_data(args, cfg)
    model = None
    if args.model:
        from .ai.learner import EdgeModel

        model = EdgeModel.load(args.model)
    flt = None
    if model is not None:
        min_ev = cfg.learner.min_expected_r
        flt = lambda s: model.score_signal(s) >= min_ev  # noqa: E731
    res = run_backtest(df, cfg.strategy, cfg.risk, cfg.costs, symbol, tf, cfg.broker.starting_equity, flt)
    if args.json:
        print(json.dumps(res.metrics, indent=2, default=str))
    else:
        print(f"{symbol} {tf}: {len(df)} bars {df.index[0]:%Y-%m-%d} -> {df.index[-1]:%Y-%m-%d}")
        print(format_report(res.metrics, f"{symbol} {tf}"))
    if args.trades:
        res.trades_frame().to_csv(args.trades, index=False)
        print(f"trades -> {args.trades}")
    if args.chart:
        from .chart import render_chart

        out = render_chart(df, res.engine, res.trades + res.cancelled, args.chart, last_n=args.chart_bars,
                           include_plotlyjs="cdn" if not args.offline_chart else True)
        print(f"chart -> {out}")


def cmd_train(args: argparse.Namespace, cfg: AppConfig) -> None:
    from .ai.learner import train_walk_forward
    from .backtest import collect_outcomes

    strat = strategy_from_overrides(cfg.strategy, {"min_score": 0})  # learn from every setup
    groups = []
    for df, symbol, tf in _datasets(args, cfg):
        outcomes = collect_outcomes(df, strat, symbol, tf)
        filled = sum(1 for t in outcomes if t.status == "closed")
        print(f"{symbol} {tf}: {len(df)} bars, {len(outcomes)} setups, {filled} filled and resolved")
        groups.append(outcomes)
    model, report = train_walk_forward(groups, split=args.split, min_expected_r=cfg.learner.min_expected_r,
                                       rule_min_score=cfg.strategy.min_score)
    out = model.save(args.out or cfg.learner.model_path)
    print(json.dumps(report, indent=2))
    print(model.describe())
    print(f"model -> {out}  (enable with learner.enabled: true)")


def cmd_optimize(args: argparse.Namespace, cfg: AppConfig) -> None:
    from .optimize import format_rows, optimize

    df, symbol, tf = load_data(args, cfg)
    rows = optimize(df, cfg.strategy, split=args.split, symbol=symbol, timeframe=tf)
    print(f"{symbol} {tf}: {len(rows)} parameter sets, split at {df.index[int(len(df) * args.split)]:%Y-%m-%d}")
    print(format_rows(rows, args.top))


def cmd_scan(args: argparse.Namespace, cfg: AppConfig) -> None:
    from .core.engine import SMCEngine, bars_from_df

    for df, symbol, tf in _datasets(args, cfg):
        eng = SMCEngine(cfg.strategy, symbol, tf)
        last_sigs = []
        for bar in bars_from_df(df):
            sigs = eng.update(bar)
            if sigs:
                last_sigs = sigs
        snap = eng.snapshot()
        print(json.dumps(snap, indent=2, default=str))
        recent = [s for s in last_sigs if eng.t - s.bar <= s.expiry_bars]
        for s in recent:
            print(f"ACTIVE SETUP {s.side.upper()} {s.model} {s.grade} entry {s.entry:.6g} sl {s.sl:.6g} "
                  f"tp {s.tp:.6g} ({s.rr:.2f}R), {eng.t - s.bar} bars ago")


def cmd_brief(args: argparse.Namespace, cfg: AppConfig) -> None:
    from .ai.analyst import ClaudeAnalyst
    from .core.engine import SMCEngine, bars_from_df

    df, symbol, tf = load_data(args, cfg)
    eng = SMCEngine(cfg.strategy, symbol, tf)
    for bar in bars_from_df(df):
        eng.update(bar)
    print(ClaudeAnalyst(cfg.ai).brief(eng))


def cmd_run(args: argparse.Namespace, cfg: AppConfig) -> None:
    from .live import TradingAgent

    TradingAgent(cfg).run_forever()


def cmd_webhook(args: argparse.Namespace, cfg: AppConfig) -> None:
    from .live import TradingAgent
    from .webhook import serve

    agent = TradingAgent(cfg, self_signals=False)
    if not cfg.webhook.execute:
        from .execution.broker import PaperBroker

        agent.broker = PaperBroker(cfg.broker.starting_equity)
        log.warning("webhook.execute is false: alerts are journaled and simulated on a throwaway paper broker")
    serve(cfg.webhook.host, cfg.webhook.port, cfg.webhook.passphrase_env, agent.handle_external)
    agent.run_forever()


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="smc-agent", description="SMC / ICT trading agent")
    p.add_argument("-c", "--config", help="YAML config (see config.example.yaml)")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="command", required=True)

    def data_args(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--csv", action="append", help="OHLCV CSV (TradingView export, MT5, exchange dump ...);"
                        " repeat for multiple markets (train / scan)")
        sp.add_argument("--market", help="symbol from the config's markets list")
        sp.add_argument("--timeframe", help="resample / override timeframe, e.g. 15m, 1h")
        sp.add_argument("--bars", type=int, default=0, help="number of most recent bars to use")
        sp.add_argument("--set", action="append", default=[], metavar="KEY=VALUE",
                        help="override a strategy parameter, e.g. --set min_score=5")

    sp = sub.add_parser("backtest", help="replay history and simulate trades")
    data_args(sp)
    sp.add_argument("--model", help="apply a trained edge model as a filter")
    sp.add_argument("--chart", help="write an interactive HTML chart")
    sp.add_argument("--chart-bars", type=int, default=400)
    sp.add_argument("--offline-chart", action="store_true", help="embed plotly.js in the HTML")
    sp.add_argument("--trades", help="write trades to CSV")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_backtest)

    sp = sub.add_parser("train", help="learn confluence weights (walk-forward)")
    data_args(sp)
    sp.add_argument("--out", help="model path (default learner.model_path)")
    sp.add_argument("--split", type=float, default=0.7)
    sp.set_defaults(func=cmd_train)

    sp = sub.add_parser("optimize", help="walk-forward parameter search")
    data_args(sp)
    sp.add_argument("--split", type=float, default=0.7)
    sp.add_argument("--top", type=int, default=10)
    sp.set_defaults(func=cmd_optimize)

    sp = sub.add_parser("scan", help="current SMC state and active setups")
    data_args(sp)
    sp.set_defaults(func=cmd_scan)

    sp = sub.add_parser("brief", help="Claude-written trading plan")
    data_args(sp)
    sp.set_defaults(func=cmd_brief)

    sp = sub.add_parser("run", help="start the autonomous agent")
    sp.set_defaults(func=cmd_run)

    sp = sub.add_parser("webhook", help="receive TradingView alerts")
    sp.set_defaults(func=cmd_webhook)
    return p


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = load_config(args.config)
    _apply_overrides(cfg, getattr(args, "set", []))
    args.func(args, cfg)


if __name__ == "__main__":  # pragma: no cover
    main(sys.argv[1:])
