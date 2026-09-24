import json
import threading
import urllib.error
import urllib.request
from datetime import datetime, timezone
from types import SimpleNamespace

import numpy as np
import pytest

from smc_agent.ai.analyst import ClaudeAnalyst
from smc_agent.ai.learner import EdgeModel, train_walk_forward
from smc_agent.backtest import collect_outcomes
from smc_agent.config import AIConfig, AppConfig, MarketConfig, RiskConfig, StrategyConfig, load_config
from smc_agent.core.engine import SMCEngine, bars_from_df
from smc_agent.core.types import Signal
from smc_agent.execution.broker import PaperBroker
from smc_agent.live import TradingAgent
from smc_agent.risk import AccountState, RiskManager
from smc_agent.webhook import make_handler

from .conftest import BULL_REVERSAL, frame, random_walk

T0 = datetime(2024, 1, 1, tzinfo=timezone.utc)


def make_sig(symbol="BTC/USDT", rr=2.0, features=None):
    return Signal(f"{symbol}|x", symbol, "15m", T0, 10, 1, "reversal", 100.0, 99.0, 100 + rr,
                  rr, 1.0, 1.0, 5, 7, "A", features or {"pd_ok": 1.0, "rr": rr, "risk_atr": 1.0}, ["test"])


# ------------------------------------------------------------------ learner
def test_learner_learns_a_real_edge():
    rng = np.random.default_rng(0)
    rows, wins = [], []
    for _ in range(600):
        pd_ok = float(rng.random() < 0.5)
        rows.append({"pd_ok": pd_ok, "rr": 2.0, "risk_atr": 1.0})
        wins.append(int(rng.random() < (0.55 if pd_ok else 0.2)))
    m = EdgeModel().fit(rows, wins)
    w = dict(zip(m.features, m.weights))
    assert w["pd_ok"] > 1.0
    assert m.proba({"pd_ok": 1, "rr": 2, "risk_atr": 1}) > m.proba({"pd_ok": 0, "rr": 2, "risk_atr": 1})


def test_learner_roundtrip_and_walk_forward(tmp_path):
    outs = collect_outcomes(random_walk(4000, 11), StrategyConfig(min_score=0))
    model, report = train_walk_forward([outs], rule_min_score=6)
    assert report["train_trades"] > report["test_trades"] > 0
    assert "out_of_sample_score>=6" in report
    path = model.save(tmp_path / "m.json")
    again = EdgeModel.load(path)
    s = make_sig()
    assert again.proba(s.features) == pytest.approx(model.proba(s.features))
    again.score_signal(s)
    assert s.expected_r == pytest.approx(s.probability * s.rr - (1 - s.probability), abs=1e-3)


# --------------------------------------------------------------------- risk
def test_risk_manager_limits():
    rm = RiskManager(RiskConfig(max_open_positions=1, max_trades_per_day=2, max_daily_loss_pct=2.0))
    ok, _ = rm.check(make_sig(), AccountState(10_000))
    assert ok
    ok, why = rm.check(make_sig("ETH/USDT"), AccountState(10_000, open_symbols={"BTC/USDT"}))
    assert not ok and "max open" in why
    ok, why = rm.check(make_sig(), AccountState(10_000, open_symbols={"BTC/USDT"}))
    assert not ok and "already" in why
    ok, why = rm.check(make_sig(rr=1.0), AccountState(10_000))
    assert not ok and "RR" in why
    ok, why = rm.check(make_sig(), AccountState(9_700))  # -3% on the day
    assert not ok and "daily loss" in why
    rm2 = RiskManager(RiskConfig(max_trades_per_day=1))
    rm2.check(make_sig(), AccountState(10_000))
    rm2.record_entry()
    assert not rm2.check(make_sig(), AccountState(10_000))[0]


# ------------------------------------------------------------ paper broker
def test_paper_broker_persists_open_positions(tmp_path, tiny_cfg):
    path = tmp_path / "paper.json"
    br = PaperBroker(10_000, commission_pct=0.0, state_path=path)
    eng = SMCEngine(tiny_cfg, "T", "15m")
    placed = False
    for t, bar in enumerate(bars_from_df(frame(BULL_REVERSAL[:18]))):
        br.on_bar("T", bar, t)
        for s in eng.update(bar):
            if not placed and s.model == "reversal":
                br.place(s, 10.0)
                placed = True
    assert br.account_state().open_symbols == {"T"}
    br2 = PaperBroker(10_000, state_path=path)
    assert br2.account_state().open_symbols == {"T"}
    assert br2.active["T"].fill_price == pytest.approx(100.9)


# ------------------------------------------------------------------ analyst
class FakeClient:
    def __init__(self, text, stop_reason="end_turn"):
        self.calls = []
        resp = SimpleNamespace(content=[SimpleNamespace(type="text", text=text)], stop_reason=stop_reason,
                               model="claude-opus-5")
        create = lambda **kw: (self.calls.append(kw), resp)[1]  # noqa: E731
        self.messages = SimpleNamespace(create=create)
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=create))


def test_analyst_review_and_policy():
    review = {"decision": "reduce", "confidence": 0.7, "bias": "bullish", "draw_on_liquidity": "PDH",
              "reasoning": "ok", "risks": []}
    client = FakeClient(json.dumps(review))
    a = ClaudeAnalyst(AIConfig(min_confidence=0.6), client=client)
    eng = SMCEngine(StrategyConfig(), "T", "15m")
    for bar in bars_from_df(random_walk(300)):
        eng.update(bar)
    got = a.review(make_sig(), eng)
    assert got["decision"] == "reduce"
    ok, mult, _ = a.approves(got)
    assert ok and mult == 0.5
    call = client.calls[0]
    assert call["output_config"]["format"]["type"] == "json_schema"
    assert call["fallbacks"] == "default" and call["thinking"] == {"type": "adaptive"}
    payload = json.loads(call["messages"][0]["content"].split("\n\n", 1)[1])
    assert "market_state" in payload and "recent_candles" in payload


def test_analyst_refusal_and_fail_closed():
    a = ClaudeAnalyst(AIConfig(), client=FakeClient("", stop_reason="refusal"))
    assert a.review(make_sig()) is None
    assert a.approves(None)[0] is False
    assert ClaudeAnalyst(AIConfig(fail_open=True), client=FakeClient("{}")).approves(None)[0] is True
    skip = {"decision": "skip", "confidence": 0.9, "reasoning": "into resistance"}
    assert a.approves(skip)[0] is False


# -------------------------------------------------------------------- agent
class FrameFeed:
    def __init__(self, df):
        self.df = df
        self.n = 0

    def history(self, bars):
        return self.df.iloc[: self.n]

    def latest(self, bars=5):
        return self.df.iloc[max(0, self.n - bars): self.n]


def _agent(tmp_path, tiny_cfg, df, **kw):
    cfg = AppConfig(markets=[MarketConfig(symbol="TEST/USDT", timeframe="15m", feed="csv", tv_symbol="TESTUSDT")],
                    strategy=tiny_cfg, journal_path=str(tmp_path / "j.jsonl"))
    feed = FrameFeed(df)
    agent = TradingAgent(cfg, broker=PaperBroker(10_000), feeds={"TEST/USDT": feed}, **kw)
    return agent, feed


def test_agent_trades_the_setup_live(tmp_path, tiny_cfg):
    df = frame(BULL_REVERSAL)
    agent, feed = _agent(tmp_path, tiny_cfg, df)
    feed.n = 10
    agent.warmup()
    decisions = []
    for n in range(11, len(df) + 1):  # candles arrive one by one
        feed.n = n
        decisions += agent.poll_market(agent.markets[0])
    taken = [d for d in decisions if d["taken"]]
    assert taken and taken[0]["side"] == "long"
    closed = agent.broker.closed
    assert closed and closed[0]["exit_reason"] == "tp"
    events = [json.loads(line)["event"] for line in (tmp_path / "j.jsonl").read_text().splitlines()]
    assert "order" in events and "broker_closed" in events


def test_agent_ai_gate_blocks(tmp_path, tiny_cfg):
    df = frame(BULL_REVERSAL)
    skip = json.dumps({"decision": "skip", "confidence": 0.9, "bias": "bearish", "draw_on_liquidity": "SSL",
                       "reasoning": "no", "risks": []})
    analyst = ClaudeAnalyst(AIConfig(), client=FakeClient(skip))
    agent, feed = _agent(tmp_path, tiny_cfg, df, analyst=analyst)
    feed.n = 10
    agent.warmup()
    decisions = []
    for n in range(11, len(df) + 1):
        feed.n = n
        decisions += agent.poll_market(agent.markets[0])
    assert decisions and not any(d["taken"] for d in decisions)
    assert any("AI skipped" in d["reason"] for d in decisions)


def test_webhook_external_signal(tmp_path, tiny_cfg):
    agent, feed = _agent(tmp_path, tiny_cfg, frame(BULL_REVERSAL))
    res = agent.handle_external({"ticker": "BINANCE:TESTUSDT", "side": "long", "entry": 100, "sl": 99, "tp": 103,
                                 "score": 7, "grade": "A", "model": "reversal"})
    assert res["taken"]
    assert agent.handle_external({"ticker": "NOPE", "side": "long", "entry": 1, "sl": 0.5, "tp": 2})["taken"] is False
    assert "inconsistent" in agent.handle_external({"ticker": "TESTUSDT", "side": "long", "entry": 1, "sl": 2,
                                                   "tp": 3})["reason"]


def test_webhook_http_passphrase():
    from http.server import ThreadingHTTPServer

    got = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler("s3cret", lambda p: (got.append(p), {"ok": 1})[1]))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_address[1]}/"

    def post(body):
        req = urllib.request.Request(url, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, None

    try:
        assert post({"passphrase": "wrong", "side": "long"})[0] == 403
        code, body = post({"passphrase": "s3cret", "event": "setup", "side": "long"})
        assert code == 200 and body == {"ok": 1}
        assert got == [{"event": "setup", "side": "long"}]  # passphrase stripped
        assert post({"passphrase": "s3cret", "event": "heartbeat"})[1] == {"ignored": "heartbeat"}
    finally:
        server.shutdown()


# ------------------------------------------------------------------- config
def test_example_config_loads():
    cfg = load_config("config.example.yaml")
    assert cfg.markets and cfg.broker.kind == "paper"
    assert cfg.strategy.min_score == 6


def test_unknown_config_key(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text("strategy:\n  not_a_key: 1\n")
    with pytest.raises(ValueError):
        load_config(p)
