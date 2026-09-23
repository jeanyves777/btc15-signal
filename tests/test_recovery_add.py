"""Order safety for the recovery add-on. Correctness and bounded exposure.

The operator's bar for shipping this is explicit and it is not profitability:
"Launch readiness means correct execution and bounded exposure. Profitability
is what this live test will establish or reject." So these tests pin the ways a
resting order can go wrong with real money:

  * a duplicate contract from a retry, a restart, or a lost response
  * an order that outlives its justification
  * exposure counted on fills only, so resting orders breach the cap
  * an unreadable exposure treated as room
  * an add that cannot pay the recovery portion it exists for

The profitability question is deliberately NOT tested here. It cannot be: a
minute candle records that the ask reached the limit, not that a queued order
traded.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from btc15_signal.brti import BRTIFeatures  # noqa: E402
from btc15_signal.execution import event_order  # noqa: E402
from btc15_signal.recovery_add import (  # noqa: E402
    AddLimits,
    AddState,
    client_order_id,
    conditions,
    evaluate,
    should_cancel,
)
from btc15_signal.validation import kalshi_fee_charged as fee  # noqa: E402

LIMITS = AddLimits()


def features(
    side: str = "DOWN", distance: float = 15.0, momentum: float = -20.0,
    stale: bool = False,
) -> BRTIFeatures:
    signed = -50.0 if side == "DOWN" else 50.0
    return BRTIFeatures(
        event_ticker="KXBTC15M-26SEP221330", ts_ms=1_000, target=86_430.49,
        value=86_430.49 * (1 + signed / 10_000), signed_distance_bps=signed,
        brti_momentum_bps=momentum, brti_volatility_bps=3.0,
        brti_normalized_distance=distance, samples=300, span_ms=300_000,
        stale=stale, settlement_projection=86_400.0,
    )


def decide(**overrides):
    kwargs = {
        "features": features(),
        "entry_side": "DOWN",
        "entry_fill": 0.77,
        "current_ask": 0.75,
        "crossed_since_entry": False,
        "remaining_s": 500,
        "required_per_trade": 0.30,
        "recovery_active": True,
        "already_added": False,
        "open_exposure": 2.0,
        "limits": LIMITS,
        "fee": fee,
    }
    kwargs.update(overrides)
    return evaluate(**kwargs)


# ------------------------------------------------------- no duplicate orders

def test_client_order_id_is_deterministic_for_one_position():
    """A retry after a lost response must reuse the id, so Kalshi rejects the
    duplicate instead of opening a second contract."""
    first = client_order_id("KXBTC15M-26SEP221330-30", "DOWN", 1_790_000_000_000)
    second = client_order_id("KXBTC15M-26SEP221330-30", "DOWN", 1_790_000_000_000)
    assert first == second


def test_client_order_id_differs_across_positions():
    base = ("KXBTC15M-26SEP221330-30", "DOWN", 1_790_000_000_000)
    assert client_order_id(*base) != client_order_id(
        "KXBTC15M-26SEP221345-45", "DOWN", 1_790_000_000_000
    )
    assert client_order_id(*base) != client_order_id(
        "KXBTC15M-26SEP221330-30", "UP", 1_790_000_000_000
    )
    assert client_order_id(*base) != client_order_id(
        "KXBTC15M-26SEP221330-30", "DOWN", 1_790_000_900_000
    )


def test_one_add_per_position():
    assert not decide(already_added=True).place
    assert "already used" in decide(already_added=True).reason


# ------------------------------------------------------------ side and price

def test_order_side_conversion_matches_the_operator_specification():
    """Buy UP at 75c -> YES bid 0.75. Buy DOWN at 75c -> YES ask 0.25."""
    assert event_order("UP", 0.75) == ("bid", 0.75)
    side, price = event_order("DOWN", 0.75)
    assert side == "ask"
    assert price == pytest.approx(0.25)


def test_the_limit_rests_two_cents_below_the_actual_fill():
    """Below the FILL, not the decision ask - they differ, and the fill is the
    only one that describes money already spent."""
    decision = decide(entry_fill=0.81)
    assert decision.place
    assert decision.price == pytest.approx(0.79)


# ------------------------------------------------------- bounded exposure

def test_the_thirty_dollar_cap_is_enforced_including_resting_orders():
    assert decide(open_exposure=29.5).place is False
    assert "cap" in decide(open_exposure=29.5).reason


def test_unknown_exposure_is_treated_as_no_room_not_as_zero():
    """`resting_exposure` returns -1 when it could not be read. Treating that
    as zero is how a cap is breached by exactly the amount nobody could see."""
    decision = decide(open_exposure=-1.0)
    assert not decision.place
    assert "unknown" in decision.reason


# ------------------------------------------- the conditions do the gating

def test_every_condition_can_block_the_add():
    assert not decide(features=features(stale=True)).place
    assert not decide(features=features(side="UP")).place          # flipped
    assert not decide(crossed_since_entry=True).place
    assert not decide(features=features(momentum=+20.0)).place     # against
    assert not decide(features=features(distance=2.0)).place       # collapsed


def test_a_blocked_add_names_every_failure_not_just_the_first():
    decision = decide(
        features=features(side="UP", momentum=+5.0, distance=1.0),
        crossed_since_entry=True,
    )
    assert not decision.place
    assert len(decision.failed) >= 3


def test_conditions_holding_permits_the_add():
    decision = decide()
    assert decision.place
    assert decision.state is AddState.PENDING


def test_conditions_helper_reports_ok_and_failures_separately():
    ok, failed = conditions(features(), "DOWN", False, LIMITS)
    assert ok and failed == ()
    ok, failed = conditions(features(side="UP"), "DOWN", False, LIMITS)
    assert not ok and failed


# --------------------------------------------- it must be able to do its job

def test_an_add_that_cannot_cover_the_recovery_portion_is_refused():
    """The operator's arithmetic: a 90c fill plus a 75c add averages 82.5c and
    can make about 35c gross - not enough for a 41c recovery portion."""
    decision = decide(entry_fill=0.90, required_per_trade=0.41)
    assert not decision.place
    assert "recovery needs" in decision.reason


def test_a_cheaper_combined_average_clears_the_same_requirement():
    decision = decide(entry_fill=0.77, required_per_trade=0.30)
    assert decision.place


def test_recovery_inactive_means_no_add_at_all():
    assert not decide(recovery_active=False).place


def test_past_the_deadline_no_add():
    assert not decide(remaining_s=60).place


# ------------------------------------------------------------- cancellation

def cancel(**overrides):
    kwargs = {
        "features": features(),
        "entry_side": "DOWN",
        "crossed_since_entry": False,
        "remaining_s": 500,
        "recovery_active": True,
        "base_position_open": True,
        "limits": LIMITS,
    }
    kwargs.update(overrides)
    return should_cancel(**kwargs)


def test_a_healthy_position_keeps_its_resting_order():
    assert cancel() == (False, "")


def test_every_operator_cancel_trigger_fires():
    assert cancel(features=features(side="UP"))[0]        # direction changed
    assert cancel(features=features(distance=1.0))[0]     # distance collapsed
    assert cancel(features=features(momentum=+9.0))[0]    # momentum against
    assert cancel(features=features(stale=True))[0]       # reference stale
    assert cancel(recovery_active=False)[0]               # recovery completed
    assert cancel(remaining_s=30)[0]                      # deadline passed
    assert cancel(base_position_open=False)[0]            # base exited


def test_cancel_when_the_reference_is_missing_entirely():
    cancelled, reason = cancel(features=None)
    assert cancelled
    assert "no BRTI reference" in reason


def test_cancel_reason_is_always_populated_when_cancelling():
    for override in (
        {"features": features(side="UP")},
        {"recovery_active": False},
        {"base_position_open": False},
        {"remaining_s": 10},
    ):
        cancelled, reason = cancel(**override)
        assert cancelled and reason
