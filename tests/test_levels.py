"""The support/resistance gate: no lookahead, and it refuses when it cannot see.

The gate reduces trading by about half, so the two things that must hold are
that a level is never known before it could be, and that an empty or stale
cache blocks rather than silently waves trades through.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from btc15_signal.snapshot import MarketSnapshot  # noqa: E402
from btc15_signal.levels import (  # noqa: E402
    LevelTracker,
    blocking_level,
    confirmed_pivots,
)
from btc15_signal.model import Prediction  # noqa: E402
from btc15_signal.strategy import EntryRule  # noqa: E402

MIN = 60_000


def bars(highs, lows=None, start=0):
    lows = lows if lows is not None else [h - 10 for h in highs]
    return [(start + i * MIN, h, low) for i, (h, low) in enumerate(zip(highs, lows, strict=True))]


def test_a_pivot_is_not_knowable_until_it_is_confirmed():
    """The whole result depends on this. A swing high at minute 5 is only a
    swing high once nothing beat it for `confirm` more minutes, so using its
    formation time would let a decision consult bars that had not happened."""
    highs = [100.0] * 11
    highs[5] = 200.0
    pivots = confirmed_pivots(bars(highs), confirm=5)
    peak = [p for p in pivots if p.price == 200.0]
    assert peak, "the swing high should be found"
    # Formed at minute 5, confirmed at minute 10 - not before.
    assert peak[0].confirmed_ms == 10 * MIN


def test_only_a_level_in_the_adverse_path_counts():
    """Betting DOWN, the danger is a rally, so only a resistance BETWEEN price
    and target is in the way. A support underneath is irrelevant to the bet."""
    from btc15_signal.levels import Pivot

    between = Pivot(0, 150.0, "resistance")
    below = Pivot(0, 50.0, "support")
    wrong_kind = Pivot(0, 150.0, "support")
    # price 100 < target 200: a rally is what beats us.
    assert blocking_level([between], 1, 100.0, 200.0) is between
    assert blocking_level([below], 1, 100.0, 200.0) is None
    assert blocking_level([wrong_kind], 1, 100.0, 200.0) is None


def test_a_level_confirmed_after_the_decision_is_not_used():
    from btc15_signal.levels import Pivot

    future = Pivot(5 * MIN, 150.0, "resistance")
    assert blocking_level([future], 1 * MIN, 100.0, 200.0) is None
    assert blocking_level([future], 6 * MIN, 100.0, 200.0) is future


def test_a_stale_level_falls_out_of_the_lookback():
    from btc15_signal.levels import Pivot

    old = Pivot(0, 150.0, "resistance")
    now = 10_000 * MIN
    assert blocking_level([old], now, 100.0, 200.0, lookback_min=60) is None


def _rule(**kw):
    return EntryRule(
        enabled=True, min_ask=0.70, max_ask=0.93, min_raw_probability=0.5,
        min_normalized_distance=0.0, **kw
    )


def _inputs():
    prediction = Prediction(
        side="DOWN", raw_probability=0.9, distance_bps=50.0, score=1.0, bucket=9
    )
    snapshot = MarketSnapshot(
        price=100.0, target=200.0, bid_imbalance=0.0, taker_imbalance=0.0,
        momentum_5m_bps=0.0, volatility_5m_bps=10.0, futures_basis_bps=0.0,
        spread_bps=1.0,
    )
    return prediction, snapshot


def test_the_gate_refuses_when_no_level_is_in_the_way():
    prediction, snapshot = _inputs()
    rule = _rule(require_blocking_level=True)
    ok, failed = rule.matches(prediction, snapshot, 0.85, blocking_level=None)
    assert not ok
    assert "support/resistance" in failed


def test_the_gate_passes_when_a_level_blocks():
    prediction, snapshot = _inputs()
    rule = _rule(require_blocking_level=True)
    ok, _ = rule.matches(prediction, snapshot, 0.85, blocking_level=150.0)
    assert ok


def test_unknown_levels_refuse_rather_than_wave_through():
    """A display cache that has not loaded must not make the gate optional. The
    guard discipline is that a check which cannot be evaluated answers no, and
    it says so distinctly so an outage cannot look like a normal rejection."""
    prediction, snapshot = _inputs()
    rule = _rule(require_blocking_level=True)
    ok, failed = rule.matches(
        prediction, snapshot, 0.85, blocking_level=None, levels_ready=False
    )
    assert not ok
    assert "levels unavailable" in failed


def test_the_gate_is_off_by_default_so_the_code_default_stays_the_measured_rule():
    prediction, snapshot = _inputs()
    ok, _ = EntryRule(
        enabled=True, min_ask=0.70, max_ask=0.93, min_raw_probability=0.5,
        min_normalized_distance=0.0,
    ).matches(prediction, snapshot, 0.85, blocking_level=None, levels_ready=False)
    assert ok, "with the gate off, unknown levels must not block anything"


def test_the_tracker_throttles_and_survives_a_failing_client():
    import asyncio

    class Boom:
        async def recent_bars(self, limit):
            raise RuntimeError("binance is unwell")

    tracker = LevelTracker(refresh_s=300)
    asyncio.run(tracker.maybe_refresh(Boom(), 1_000_000))
    assert tracker.pivots == []  # no crash, no levels

    class Good:
        calls = 0

        async def recent_bars(self, limit):
            Good.calls += 1
            highs = [100.0] * 61
            highs[30] = 200.0
            return bars(highs)

    tracker = LevelTracker(refresh_s=300)
    asyncio.run(tracker.maybe_refresh(Good(), 1_000_000))
    assert tracker.pivots
    asyncio.run(tracker.maybe_refresh(Good(), 1_000_000 + 1000))
    assert Good.calls == 1, "a second call inside the window must not refetch"
