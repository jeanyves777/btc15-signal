"""The ambiguous submission, the unacknowledged cancel, and the orphan.

THE CRASH BOUNDARY. The local row is written BEFORE the order is sent, so a
crash - or a dropped response - between the send and storing the broker's
`order_id` leaves a row saying PENDING with `placed_ms` set and `order_id`
NULL.

Every repair path was keyed on `order_id`: `adds_needing_reconciliation`
filtered `order_id IS NOT NULL`, `_maintain`'s fill check short-circuited on
it, and `_cancel` wrote CANCELLED WITHOUT SENDING ANYTHING. So an order that
was really resting at Kalshi became an orphan - never polled for a fill, never
cancelled, and if it filled, never banked and never charged to the budget. The
docstrings promised "`reconcile` resolves it against the broker"; for exactly
this row, it could not.

`client_order_id` is a pure function of (ticker, side, window), so it is the
one key that survives the crash, and Kalshi returns it on the order object.
That makes the order listing the way back to the `order_id`.

Nothing here relaxes the conservative cancellation rule: an unverifiable
crossing still pulls a resting order. What changes is that "I could not reach
Kalshi" stops being recorded as "the order is gone".
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from btc15_signal.recovery_add import AddState, client_order_id  # noqa: E402
from btc15_signal.recovery_add_runner import RecoveryAddRunner  # noqa: E402
from test_recovery_add_runner import (  # noqa: E402
    NOW,
    TICKER,
    WINDOW,
    FakeTrader,
    make,
    run,
)


class LosesTheResponse(FakeTrader):
    """Kalshi accepts the order; we never learn its id."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.listed = []

    async def place_resting_buy(self, **kwargs):
        self.placed.append(kwargs)
        raise RuntimeError("connection reset after the order was accepted")

    async def order_by_client_id(self, ticker, client_id):
        self.listed.append(client_id)
        if any(p["client_order_id"] == client_id for p in self.placed):
            return {"order_id": "ord-recovered", "client_order_id": client_id,
                    "status": "resting"}
        return None


def orphaned(tmp_path):
    """A runner and store left exactly at the crash boundary."""
    settings, store = make(tmp_path)
    runner = RecoveryAddRunner(settings, store)
    trader = LosesTheResponse()
    run(runner, trader)
    row = store.open_add(WINDOW)
    assert row["state"] == AddState.PENDING, "it believes it sent one"
    assert row["order_id"] is None, "and it does not know the id"
    assert row["placed_ms"] is not None, "but it knows it tried"
    return settings, store, runner, trader


def test_the_crash_boundary_leaves_a_row_with_no_order_id(tmp_path):
    orphaned(tmp_path)


def test_such_a_row_is_offered_for_reconciliation(tmp_path):
    """`order_id IS NOT NULL` excluded the one row that most needed it."""
    _s, store, _r, _t = orphaned(tmp_path)
    pending = store.adds_needing_reconciliation()
    assert len(pending) == 1
    assert pending[0]["order_id"] is None


def test_the_id_is_recovered_from_the_broker_by_our_own_id(tmp_path):
    _s, store, runner, trader = orphaned(tmp_path)
    asyncio.run(runner.reconcile(trader, NOW))
    assert trader.listed == [client_order_id(TICKER, "DOWN", WINDOW)]
    assert store.open_add(WINDOW)["order_id"] == "ord-recovered"


def test_an_ambiguous_row_is_never_cancelled_without_asking(tmp_path):
    _s, store, runner, trader = orphaned(tmp_path)
    asyncio.run(runner._cancel(trader, store.open_add(WINDOW), "deadline", NOW))
    assert trader.cancelled == ["ord-recovered"], "it asked, then cancelled"
    assert store.open_add(WINDOW)["state"] == AddState.CANCELLED


def test_a_submission_that_never_arrived_is_closed(tmp_path):
    """The listing was READ and our id is not in it: nothing is resting."""
    settings, store = make(tmp_path)
    runner = RecoveryAddRunner(settings, store)

    class NeverArrived(LosesTheResponse):
        async def order_by_client_id(self, ticker, client_id):
            self.listed.append(client_id)
            return None

    trader = NeverArrived()
    run(runner, trader)
    asyncio.run(runner.reconcile(trader, NOW))
    row = store.open_add(WINDOW)
    assert row["state"] == AddState.CANCELLED
    assert "never reached the exchange" in row["cancel_reason"]


def test_an_unreadable_listing_leaves_the_row_alone(tmp_path):
    """Unreadable is not absent. Concluding "no such order" from a failed
    read is how a live order gets written off."""
    settings, store = make(tmp_path)
    runner = RecoveryAddRunner(settings, store)

    class Unreadable(LosesTheResponse):
        async def order_by_client_id(self, ticker, client_id):
            raise LookupError("could not list orders: ReadTimeout")

    trader = Unreadable()
    run(runner, trader)
    asyncio.run(runner.reconcile(trader, NOW))
    assert store.open_add(WINDOW)["state"] == AddState.PENDING


def test_a_recovered_order_that_filled_is_banked_once(tmp_path):
    """The point of all of it: an orphan that fills still reaches the record."""
    settings, store = make(tmp_path)
    runner = RecoveryAddRunner(settings, store)

    class FilledWhileOrphaned(LosesTheResponse):
        def __init__(self):
            super().__init__(fill_on_status=True)

    trader = FilledWhileOrphaned()
    run(runner, trader)
    asyncio.run(runner.reconcile(trader, NOW))
    row = store.open_add(WINDOW)
    assert row["state"] == AddState.EXECUTED
    assert row["filled_count"] == 1.0
    _committed, fills = store.add_budget_committed()
    assert fills == 1, "charged to the lifetime budget exactly once"


def test_reconciling_twice_does_not_double_charge_the_budget(tmp_path):
    settings, store = make(tmp_path)
    runner = RecoveryAddRunner(settings, store)

    class FilledWhileOrphaned(LosesTheResponse):
        def __init__(self):
            super().__init__(fill_on_status=True)

    trader = FilledWhileOrphaned()
    run(runner, trader)
    asyncio.run(runner.reconcile(trader, NOW))
    asyncio.run(runner.reconcile(trader, NOW + 1000))
    _committed, fills = store.add_budget_committed()
    assert fills == 1


# ------------------------------------- a cancel that was not acknowledged

def test_an_unacknowledged_cancel_does_not_write_cancelled(tmp_path):
    """A 500 or a timeout left the order live at Kalshi under a row saying it
    was gone - and a CANCELLED row is never examined again."""
    settings, store = make(tmp_path)
    runner = RecoveryAddRunner(settings, store)

    class CancelFails(FakeTrader):
        async def cancel_order(self, order_id):
            self.cancelled.append(order_id)
            return (False, "HTTP 500: upstream error")

    trader = CancelFails()
    run(runner, trader)
    assert store.open_add(WINDOW)["state"] == AddState.PENDING
    asyncio.run(runner._cancel(trader, store.open_add(WINDOW), "deadline", NOW))
    assert store.open_add(WINDOW)["state"] == AddState.PENDING, \
        "left pending to retry, not written off"


def test_a_404_cancel_is_proof_the_order_is_gone(tmp_path):
    """The one unacknowledged case that IS evidence."""
    settings, store = make(tmp_path)
    runner = RecoveryAddRunner(settings, store)

    class AlreadyGone(FakeTrader):
        async def cancel_order(self, order_id):
            self.cancelled.append(order_id)
            return (False, "order not found (already filled, expired or cancelled)")

    trader = AlreadyGone()
    run(runner, trader)
    asyncio.run(runner._cancel(trader, store.open_add(WINDOW), "deadline", NOW))
    assert store.open_add(WINDOW)["state"] == AddState.CANCELLED


def test_a_cancel_that_lost_to_a_fill_still_banks_the_fill(tmp_path):
    """Unchanged behaviour, pinned: a contract we now hold is not written
    down as never placed."""
    settings, store = make(tmp_path)
    runner = RecoveryAddRunner(settings, store)
    trader = FakeTrader(fill_on_status=True, cancel_ok=False)
    run(runner, trader)
    asyncio.run(runner._cancel(trader, store.open_add(WINDOW), "deadline", NOW))
    row = store.open_add(WINDOW)
    assert row["state"] == AddState.EXECUTED
    assert row["filled_count"] == 1.0


def test_the_conservative_cancel_rule_is_unchanged(tmp_path):
    """An unverifiable crossing still pulls a resting order."""
    settings, store = make(tmp_path)
    runner = RecoveryAddRunner(settings, store)
    trader = FakeTrader()
    run(runner, trader)
    assert store.open_add(WINDOW)["state"] == AddState.PENDING
    run(runner, trader, crossed=None)
    assert store.open_add(WINDOW)["state"] == AddState.CANCELLED
    assert trader.cancelled == ["ord-1"]


# ------------------------------------ a deferred row always reaches an end

def test_a_deferred_row_is_closed_when_the_window_expires(tmp_path):
    settings, store = make(tmp_path)
    runner = RecoveryAddRunner(settings, store)
    run(runner, FakeTrader(), crossed=None)
    assert store.open_add(WINDOW)["state"] == AddState.DEFERRED
    assert store.close_stale_deferred_adds(WINDOW + 900_000) == 1
    row = store.open_add(WINDOW)
    assert row["state"] == AddState.SKIPPED
    assert "window closed" in row["cancel_reason"]


def test_a_deferred_row_is_closed_when_the_base_position_exits(tmp_path):
    """`step` is not called at all once the position is gone, so without the
    sweeper the row stays DEFERRED forever."""
    settings, store = make(tmp_path)
    runner = RecoveryAddRunner(settings, store)
    run(runner, FakeTrader(), crossed=None)
    store.db.execute("UPDATE trade_proposals SET status='exited'")
    store.db.commit()
    assert store.close_stale_deferred_adds(WINDOW + 60_000) == 1
    row = store.open_add(WINDOW)
    assert row["state"] == AddState.SKIPPED
    assert "base position closed" in row["cancel_reason"]


def test_the_sweeper_leaves_a_live_deferred_row_alone(tmp_path):
    """Still inside the window with the position held: still answerable."""
    settings, store = make(tmp_path)
    runner = RecoveryAddRunner(settings, store)
    run(runner, FakeTrader(), crossed=None)
    assert store.close_stale_deferred_adds(WINDOW + 60_000) == 0
    assert store.open_add(WINDOW)["state"] == AddState.DEFERRED


def test_the_sweeper_is_idempotent(tmp_path):
    settings, store = make(tmp_path)
    runner = RecoveryAddRunner(settings, store)
    run(runner, FakeTrader(), crossed=None)
    assert store.close_stale_deferred_adds(WINDOW + 900_000) == 1
    assert store.close_stale_deferred_adds(WINDOW + 900_001) == 0


def test_the_sweeper_never_touches_a_terminal_or_resting_row(tmp_path):
    _settings, store = make(tmp_path)
    for state in (AddState.PENDING, AddState.EXECUTED, AddState.CANCELLED,
                  AddState.SKIPPED):
        store.db.execute("DELETE FROM recovery_adds")
        store.db.execute(
            "INSERT INTO recovery_adds (client_order_id, window_open_ms, "
            "ticker, side, state, order_id, base_fill, limit_price, count, "
            "placed_ms, created_ms, updated_ms) "
            "VALUES ('c',?,?,'DOWN',?,'ord-1',0.77,0.75,1,1,0,0)",
            (WINDOW, TICKER, state),
        )
        store.db.commit()
        assert store.close_stale_deferred_adds(WINDOW + 900_000) == 0, state
        assert store.open_add(WINDOW)["state"] == state


def test_banking_a_fill_returns_true_and_does_not_raise(tmp_path):
    """`maker` was never bound in `_bank_if_filled`, so EVERY successful fill
    raised NameError AFTER the row and the funds had been written. The row
    survived - the write precedes the print - so a test that only checks the
    row passes while the caller sees an exception, the EXECUTED line is never
    logged, and the rest of the poll is abandoned. This asserts the return."""
    settings, store = make(tmp_path)
    runner = RecoveryAddRunner(settings, store)
    trader = FakeTrader(fill_on_status=True)
    run(runner, trader)
    banked = asyncio.run(
        runner._bank_if_filled(trader, store.open_add(WINDOW), NOW, False, None)
    )
    assert banked is True, "it must report the fill, not raise past the return"


def test_the_executed_line_names_the_liquidity_side(tmp_path, capsys):
    settings, store = make(tmp_path)
    runner = RecoveryAddRunner(settings, store)
    trader = FakeTrader(fill_on_status=True)
    run(runner, trader)
    asyncio.run(
        runner._bank_if_filled(trader, store.open_add(WINDOW), NOW, False, None)
    )
    out = capsys.readouterr().out
    assert "recovery add EXECUTED" in out
    assert ("maker" in out) or ("taker" in out)
