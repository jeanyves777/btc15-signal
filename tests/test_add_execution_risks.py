"""Two execution risks: a working remainder, and an exposure nobody verified.

PARTIAL FILLS ARE REAL. Kalshi's quantities are fixed point - the order is
sent as `count: "%.2f"` and every count field on the response ends `_fp` - so
an order for 1.00 can fill 0.40 and leave 0.60 working. The row was written
`RECOVERY ADD EXECUTED` for any fill above zero and `_step` then returned on
every later poll, so the remainder was never maintained, never cancelled at
the deadline, never pulled under the crossing rule, and a later fill of it was
refused by the old `filled_count = 0` guard. It also released the whole fund
reservation while the remainder was still committed.

EXPOSURE WAS READ WITHOUT ITS AGE. `open_mark` is written only by the
60-second settlement sweep, so the cap could be applied to a figure taken
before the current position existed, or one still counting a position already
exited. The CAP AND THE ARITHMETIC ARE UNCHANGED here - what changed is that
an input which cannot be verified defers instead of being used.
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from btc15_signal.execution import parse_fill  # noqa: E402
from btc15_signal.recovery_add import AddState  # noqa: E402
from btc15_signal.recovery_add_runner import RecoveryAddRunner  # noqa: E402
from test_recovery_add_runner import (  # noqa: E402
    NOW,
    WINDOW,
    FakeContract,
    FakeTrader,
    features,
    make,
    run,
)


def order(fill="0.40", remaining="0.60", cost="0.28", fee="0.004",
          status="resting"):
    return {
        "status": status,
        "fill_count_fp": fill,
        "initial_count_fp": "1.00",
        "remaining_count_fp": remaining,
        "maker_fill_cost_dollars": cost,
        "taker_fill_cost_dollars": "0.0",
        "maker_fees_dollars": fee,
        "taker_fees_dollars": "0.0",
        "no_price_dollars": "0.70",
        "yes_price_dollars": "0.30",
        "outcome_side": "no",
    }


class PartialBroker(FakeTrader):
    """A broker that fills part of the order, then the rest."""

    def __init__(self, sequence):
        super().__init__()
        self.sequence = list(sequence)
        self.polls = 0

    async def order_status(self, order_id):
        if not self.sequence:
            return {"order_id": order_id, "status": "resting"}
        self.polls += 1
        return self.sequence[0] if len(self.sequence) == 1 \
            else self.sequence.pop(0)


# ------------------------------------------- the broker's own quantities

def test_the_remainder_is_read_from_the_broker():
    detail = parse_fill(order(), "DOWN")
    assert detail["count"] == 0.4
    assert detail["remaining"] == 0.6


def test_a_complete_fill_reports_no_remainder():
    detail = parse_fill(order(fill="1.00", remaining="0.00", cost="0.70",
                              status="executed"), "DOWN")
    assert detail["count"] == 1.0
    assert detail["remaining"] == 0.0


def test_the_remainder_falls_back_to_initial_minus_filled():
    """If `remaining_count_fp` is absent the arithmetic still holds."""
    row = order(fill="0.40", remaining="0")
    row.pop("remaining_count_fp")
    assert parse_fill(row, "DOWN")["remaining"] == 0.6


# ----------------------------------- a partial fill keeps the order alive

def test_a_partial_fill_is_not_terminal(tmp_path):
    settings, store = make(tmp_path)
    runner = RecoveryAddRunner(settings, store)
    broker = PartialBroker([order()])
    run(runner, broker)
    run(runner, broker)
    row = store.open_add(WINDOW)
    assert row["state"] == AddState.PARTIAL
    assert row["filled_count"] == 0.4


def test_a_partially_filled_order_is_still_maintained(tmp_path):
    """`_step` used to return on EXECUTED, so the remainder was abandoned."""
    settings, store = make(tmp_path)
    runner = RecoveryAddRunner(settings, store)
    broker = PartialBroker([order()])
    run(runner, broker)
    run(runner, broker)
    assert store.open_add(WINDOW)["state"] == AddState.PARTIAL
    # Conditions fail: the remainder must be pulled.
    run(runner, broker, feats=features(side="UP"))
    assert broker.cancelled == ["ord-1"], "the remainder was cancelled"


def test_the_rest_of_the_fill_is_banked_and_charged_once(tmp_path):
    """The old `filled_count = 0` guard refused the second increment, so the
    remainder filled and was never recorded or charged."""
    settings, store = make(tmp_path)
    runner = RecoveryAddRunner(settings, store)
    broker = PartialBroker([
        order(),
        order(fill="1.00", remaining="0.00", cost="0.70", fee="0.01",
              status="executed"),
    ])
    run(runner, broker)
    run(runner, broker)
    assert store.open_add(WINDOW)["filled_count"] == 0.4
    part, _ = store.add_budget_committed()
    assert part == round(0.4 * 0.70 + 0.004, 6)

    run(runner, broker)
    row = store.open_add(WINDOW)
    assert row["state"] == AddState.EXECUTED
    assert row["filled_count"] == 1.0
    full, _ = store.add_budget_committed()
    assert full == round(1.0 * 0.70 + 0.01, 6), "the delta, not the whole order"


def test_the_same_partial_reported_twice_charges_once(tmp_path):
    settings, store = make(tmp_path)
    runner = RecoveryAddRunner(settings, store)
    broker = PartialBroker([order()])
    run(runner, broker)
    run(runner, broker)
    first, _ = store.add_budget_committed()
    run(runner, broker)
    second, _ = store.add_budget_committed()
    assert first == second


def test_the_reservation_is_held_while_a_remainder_is_working(tmp_path):
    """Releasing the whole claim on a partial hands the next order money this
    one still has committed at the broker."""
    settings, store = make(tmp_path)
    runner = RecoveryAddRunner(settings, store)
    broker = PartialBroker([order()])
    run(runner, broker)
    run(runner, broker)
    assert store.unreconciled_add_commitment() > 0


def test_cancelling_a_partial_leaves_us_holding_what_filled(tmp_path):
    """Writing CANCELLED over it would report a contract we own as never
    placed, and hide it from the add's own P&L."""
    settings, store = make(tmp_path)
    runner = RecoveryAddRunner(settings, store)
    broker = PartialBroker([order()])
    run(runner, broker)
    run(runner, broker)
    run(runner, broker, feats=features(side="UP"))
    row = store.open_add(WINDOW)
    assert row["state"] == AddState.EXECUTED
    assert row["filled_count"] == 0.4
    assert "flipped" in (row["cancel_reason"] or "")


def test_an_unconfirmed_remainder_is_not_made_terminal(tmp_path):
    """`remaining` unknown is not a confirmation that nothing is working."""
    settings, store = make(tmp_path)
    runner = RecoveryAddRunner(settings, store)
    row = order(fill="0.40")
    row.pop("remaining_count_fp")
    row.pop("initial_count_fp")
    broker = PartialBroker([row])
    run(runner, broker)
    run(runner, broker)
    assert store.open_add(WINDOW)["state"] == AddState.PARTIAL


def test_a_partial_is_offered_for_reconciliation(tmp_path):
    settings, store = make(tmp_path)
    runner = RecoveryAddRunner(settings, store)
    broker = PartialBroker([order()])
    run(runner, broker)
    run(runner, broker)
    pending = store.adds_needing_reconciliation()
    assert [r["state"] for r in pending] == [AddState.PARTIAL]


# -------------------------------------------------- exposure freshness

def test_a_stale_position_mark_defers_rather_than_approving(tmp_path):
    """The silent case: the cap applied to a figure nobody checked."""
    settings, store = make(tmp_path)
    store.set_setting("open_mark", 0.0, NOW - 600_000)
    runner = RecoveryAddRunner(settings, store)
    trader = FakeTrader()
    run(runner, trader)
    row = store.open_add(WINDOW)
    assert trader.placed == [], "never placed on an unverified exposure"
    assert row["state"] == AddState.DEFERRED
    assert "open position mark is" in row["cancel_reason"]


def test_a_mark_that_was_never_read_defers(tmp_path):
    settings, store = make(tmp_path)
    store.db.execute("DELETE FROM settings WHERE key='open_mark'")
    store.db.commit()
    runner = RecoveryAddRunner(settings, store)
    trader = FakeTrader()
    run(runner, trader)
    assert trader.placed == []
    assert store.open_add(WINDOW)["state"] == AddState.DEFERRED


def test_a_fresh_mark_places_normally(tmp_path):
    settings, store = make(tmp_path)
    store.set_setting("open_mark", 0.0, NOW - 30_000)
    runner = RecoveryAddRunner(settings, store)
    trader = FakeTrader()
    run(runner, trader)
    assert len(trader.placed) == 1


def test_the_freshness_bound_is_the_configured_one(tmp_path):
    settings, store = make(tmp_path, recovery_add_exposure_max_age_ms=5_000)
    store.set_setting("open_mark", 0.0, NOW - 10_000)
    runner = RecoveryAddRunner(settings, store)
    trader = FakeTrader()
    run(runner, trader)
    assert trader.placed == []
    assert store.open_add(WINDOW)["state"] == AddState.DEFERRED


def test_a_stale_mark_resolves_when_the_sweep_lands(tmp_path):
    """Deferred, not refused: the next sweep makes it answerable."""
    settings, store = make(tmp_path)
    store.set_setting("open_mark", 0.0, NOW - 600_000)
    runner = RecoveryAddRunner(settings, store)
    trader = FakeTrader()
    run(runner, trader)
    assert store.open_add(WINDOW)["state"] == AddState.DEFERRED
    store.set_setting("open_mark", 0.0, NOW)
    run(runner, trader)
    assert store.open_add(WINDOW)["state"] == AddState.PENDING
    assert len(trader.placed) == 1


def test_exposure_deferral_is_bounded_by_eligibility(tmp_path):
    settings, store = make(tmp_path)
    store.set_setting("open_mark", 0.0, NOW - 600_000)
    runner = RecoveryAddRunner(settings, store)
    trader = FakeTrader()
    for _ in range(3):
        run(runner, trader)
        assert store.open_add(WINDOW)["state"] == AddState.DEFERRED
    run(runner, trader, remaining=30)
    row = store.open_add(WINDOW)
    assert row["state"] == AddState.SKIPPED
    assert "deadline" in row["cancel_reason"]
    assert "still waiting on" in row["cancel_reason"]
    assert trader.placed == []


def test_unreconciled_submissions_count_toward_exposure(tmp_path):
    """An order accepted moments ago may not be in the resting list yet."""
    settings, store = make(tmp_path)
    assert store.unreconciled_add_commitment() == 0.0
    runner = RecoveryAddRunner(settings, store)
    run(runner, FakeTrader())
    assert store.open_add(WINDOW)["state"] == AddState.PENDING
    assert store.unreconciled_add_commitment() == 0.75


def test_a_terminal_row_commits_nothing(tmp_path):
    settings, store = make(tmp_path)
    runner = RecoveryAddRunner(settings, store)
    run(runner, FakeTrader())
    run(runner, FakeTrader(), feats=features(side="UP"))
    assert store.open_add(WINDOW)["state"] == AddState.CANCELLED
    assert store.unreconciled_add_commitment() == 0.0


def test_the_cap_and_its_arithmetic_are_unchanged(tmp_path):
    """The limit itself is untouched: only the freshness of its inputs."""
    settings, store = make(tmp_path)
    assert settings.recovery_add_test_budget == 30.0
    assert settings.recovery_add_max_contracts == 1
    assert settings.recovery_add_distance_floor == 10.0
    assert settings.recovery_add_dip == 0.02
    assert settings.recovery_add_min_seconds == 120
    trader = FakeTrader(resting=29.9)
    run(RecoveryAddRunner(settings, store), trader)
    row = store.open_add(WINDOW)
    assert trader.placed == []
    assert "cap" in row["cancel_reason"], row["cancel_reason"]
    assert row["state"] == AddState.SKIPPED, "a real cap breach is terminal"
