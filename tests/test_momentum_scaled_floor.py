"""Momentum sets how much distance a setup must have.

THE OPERATOR'S READING, 2026-09-24: "a lot of momentum can mean distance can
revert fast". Momentum should decide how far from the strike you need to be,
not which way you bet.

MEASURED on 2,145 corpus markets sampled 660-360s, priced on the Kalshi book,
day-clustered over 68 days with a chronological split:

    deployed  >=10x any momentum       n=4350  +0.0268  [+0.012, +0.041]
    proposed  calm >=10x, moving >=15x n=2861  +0.0364  [+0.020, +0.051]

out-of-sample half +0.0380, better than the whole.

TWO THINGS WERE MEASURED AND REJECTED, and the tests below pin both because
they are the tempting mistakes:

  * LOWERING the floor for calm setups. Edge rises with the floor even when
    calm (+0.0318 at 4x, +0.0560 at 15x), and the marginal band below 10x
    spans zero in every cut. So calm never buys a discount.
  * ALIGNMENT. On BRTI, aligned is +0.027 and against is +0.026. The
    Binance-era separation (84.2% vs 71.4%) does not reproduce, so this is
    magnitude and not direction.
"""

import sys
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from btc15_signal.brti import BRTIFeatures  # noqa: E402
from btc15_signal.kalshi_brti import KalshiBRTIRule  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
RULE = KalshiBRTIRule(enabled=True)


def features(distance, momentum, side="UP"):
    return BRTIFeatures(
        event_ticker="E", ts_ms=0, target=100.0, value=101.0,
        signed_distance_bps=100.0 if side == "UP" else -100.0,
        brti_momentum_bps=momentum, brti_volatility_bps=10.0,
        brti_normalized_distance=distance, samples=60, span_ms=60_000,
        stale=False, settlement_projection=101.0,
        brti_retrace=0.0, brti_choppiness=0.0,
    )


def distance_fact(rule, f):
    return next(x for x in rule.check_facts(f, 0.80, 500)
                if x["name"] == "BRTI distance")


# ------------------------------------------------------------ the two floors

def test_a_calm_market_uses_the_lower_floor():
    assert RULE.distance_floor_for(0.0) == (10.0, "calm")
    assert RULE.distance_floor_for(4.9) == (10.0, "calm")


def test_a_moving_market_needs_more_distance():
    assert RULE.distance_floor_for(5.0) == (15.0, "moving")
    assert RULE.distance_floor_for(30.0) == (15.0, "moving")


def test_direction_does_not_matter_only_magnitude():
    """Aligned +0.027 against against +0.026 on BRTI - the Binance-era
    separation does not reproduce, so this gate reads |momentum|."""
    assert RULE.distance_floor_for(-12.0) == RULE.distance_floor_for(12.0)
    assert RULE.distance_floor_for(-2.0) == RULE.distance_floor_for(2.0)


def test_missing_momentum_is_treated_as_calm():
    """A feature that could not be computed must not silently raise the bar
    to 15x and refuse everything."""
    assert RULE.distance_floor_for(None) == (10.0, "calm")


# --------------------------------------------- what it actually admits now

def test_a_moving_setup_at_twelve_times_is_now_refused():
    """It passed yesterday. Non-calm at 10x returns +0.0160 with a lower
    bound of exactly zero; it needs 15x before the interval clears."""
    fact = distance_fact(RULE, features(12.0, momentum=11.0))
    assert fact["passed"] is False
    assert "15x" in fact["fail_text"]
    assert "moving" in fact["fail_text"]


def test_the_same_setup_when_calm_still_passes():
    fact = distance_fact(RULE, features(12.0, momentum=1.0))
    assert fact["passed"] is True
    assert "calm" in fact["pass_text"]


def test_a_moving_setup_at_sixteen_times_passes():
    assert distance_fact(RULE, features(16.0, momentum=20.0))["passed"] is True


# ------------------------------------- the rejected alternative, pinned

def test_calm_never_buys_a_lower_floor():
    """The tempting move, and it was measured and refused: edge RISES with
    the floor even for calm setups, and the band below 10x spans zero."""
    calm_floor, _ = RULE.distance_floor_for(0.0)
    assert calm_floor == RULE.min_brti_normalized_distance
    assert calm_floor >= 10.0, "calm must never drop below the measured floor"


def test_the_floor_only_ever_rises_with_momentum():
    floors = [RULE.distance_floor_for(m)[0] for m in (0, 2, 4, 6, 10, 20, 50)]
    assert floors == sorted(floors), "a busier market must never need LESS"


# ------------------------------------------------- the gate says which floor

def test_the_alert_names_the_floor_that_applied():
    """A threshold that moves without saying so reads as an inconsistent
    gate - the operator would see 12x pass one minute and fail the next."""
    calm = distance_fact(RULE, features(11.0, momentum=1.0))
    moving = distance_fact(RULE, features(11.0, momentum=20.0))
    assert "calm" in calm["pass_text"] and "10x" in calm["pass_text"]
    assert "moving" in moving["fail_text"] and "15x" in moving["fail_text"]
    assert "20.0 bps" in moving["fail_text"]


# ------------------------------------------------------- shipped settings

def test_both_instruments_carry_the_preference():
    for name in ("strategy_kalshi.json", "strategy_kalshi_eth.json"):
        rule = KalshiBRTIRule.load(str(ROOT / name))
        assert rule.calm_momentum_bps == 5.0, name
        assert rule.moving_min_brti_normalized_distance > \
            rule.min_brti_normalized_distance, name


def test_alignment_remains_off():
    """This is magnitude, not direction. Turning alignment on alongside it
    would be shipping the Binance-era finding that does not reproduce."""
    for name in ("strategy_kalshi.json", "strategy_kalshi_eth.json"):
        rule = KalshiBRTIRule.load(str(ROOT / name))
        assert rule.require_momentum_alignment is False, name


def test_a_rule_file_without_the_fields_still_loads():
    """An older deployment must not break on a field it has never seen."""
    rule = replace(KalshiBRTIRule(), calm_momentum_bps=5.0)
    assert rule.distance_floor_for(1.0)[0] == 10.0


def test_the_moving_floor_can_never_sit_below_the_calm_one():
    """Two independent fields, so a config could invert them and a busy
    market would need LESS cushion than a quiet one. It also meant raising
    `min_brti_normalized_distance` alone stopped tightening the gate - which
    is how a test setting it to 1e9 still saw setups pass."""
    inverted = KalshiBRTIRule(min_brti_normalized_distance=20.0,
                              moving_min_brti_normalized_distance=12.0)
    assert inverted.distance_floor_for(1.0)[0] == 20.0
    assert inverted.distance_floor_for(30.0)[0] == 20.0


def test_raising_the_calm_floor_alone_still_tightens_everything():
    huge = KalshiBRTIRule(min_brti_normalized_distance=1e9)
    for momentum in (0.0, 4.9, 5.0, 50.0):
        assert huge.distance_floor_for(momentum)[0] == 1e9, momentum
