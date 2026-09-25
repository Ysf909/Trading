from datetime import datetime, timedelta, timezone

import pytest

from smc_agent.core.structure import ATR, HTFBias, StructureTracker, pivot_high, pivot_low
from smc_agent.core.types import Bar


def test_pivot_high_requires_strict_left_and_weak_right():
    highs = [1, 2, 5, 3, 4]
    assert pivot_high(highs, 4, 2) == 5
    assert pivot_high([1, 5, 5, 3, 4], 4, 2) is None  # equal bar on the left
    assert pivot_high([1, 2, 5, 5, 4], 4, 2) == 5  # equal bar on the right is allowed
    assert pivot_high(highs, 3, 2) is None  # not enough bars yet


def test_pivot_low_mirror():
    assert pivot_low([5, 4, 1, 3, 2], 4, 2) == 1
    assert pivot_low([5, 1, 1, 3, 2], 4, 2) is None


def test_atr_matches_wilder():
    atr = ATR(3)
    bars = [(10, 8, 9), (11, 9, 10), (12, 9, 11), (13, 10, 12)]
    vals = [atr.update(h, l, c) for h, l, c in bars]
    trs = [2, 2, 3, 3]  # true ranges
    assert vals[:2] == [None, None]
    assert vals[2] == pytest.approx(sum(trs[:3]) / 3)
    assert vals[3] == pytest.approx((vals[2] * 2 + trs[3]) / 3)


def _run(tracker, highs, lows, closes):
    t0 = datetime(2024, 1, 1, tzinfo=timezone.utc)
    times = [t0 + timedelta(minutes=i) for i in range(len(highs))]
    events = []
    for t in range(len(highs)):
        events += tracker.update(t, highs[: t + 1], lows[: t + 1], closes[t], times[: t + 1])
    return events


def test_bos_then_choch():
    tr = StructureTracker(1, "internal")
    highs = [10, 12, 11, 13, 12, 9, 8, 10, 11]
    lows = [9, 11, 10, 12, 10, 7, 6, 8, 10]
    closes = [9.5, 11.5, 10.5, 12.5, 11, 8, 7, 9, 10.5]
    ev = _run(tr, highs, lows, closes)
    kinds = [(e.kind, e.direction, e.price) for e in ev]
    # close 12.5 breaks the pivot high 12 (trend unknown -> BOS), close 8 breaks the
    # pivot low 10 against an up-trend -> CHoCH, close 10.5 breaks pivot high 10? no:
    assert kinds[0] == ("BOS", 1, 12)
    assert ("CHoCH", -1, 10) in kinds
    assert tr.trend in (-1, 1)


def test_each_pivot_breaks_once():
    tr = StructureTracker(1, "internal")
    highs = [10, 12, 11, 13, 14, 15]
    lows = [9, 11, 10, 12, 13, 14]
    closes = [9.5, 11.5, 10.5, 12.5, 13.5, 14.5]
    ev = _run(tr, highs, lows, closes)
    assert len([e for e in ev if e.price == 12]) == 1


def test_htf_bias_uses_only_closed_buckets():
    htf = HTFBias(60, 1)
    t0 = datetime(2024, 1, 1, tzinfo=timezone.utc)
    trend_seen = []
    # three 60m buckets of 15m bars forming a pivot high, then a breakout bucket
    prices = [10, 11, 12, 11, 13, 14, 15, 14, 12, 12, 11, 12, 16, 17, 18, 19]
    for i, p in enumerate(prices):
        trend_seen.append(htf.update(Bar(t0 + timedelta(minutes=15 * i), p, p + 0.5, p - 0.5, p)))
    # the trend can only change on the first bar of a new bucket
    changes = [i for i in range(1, len(trend_seen)) if trend_seen[i] != trend_seen[i - 1]]
    assert all(i % 4 == 0 for i in changes)
