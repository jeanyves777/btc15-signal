"""Execution logging: every submitted order is written down, filled or not.

The point of the archive is the MISSES. A backtest credits a fill at the price
it saw; live, 9 of 21 orders filled. If only fills are recorded, the archive
reproduces the backtest's blind spot instead of correcting it.
"""

import pytest

from btc15_signal.execution import ExecutionResult
from btc15_signal.main import log_execution
from btc15_signal.store import Store, TradeProposal

# The REAL dataclasses, not stand-ins. A hand-rolled fake that carried a
# `created_at` the real `TradeProposal` does not have is exactly how an
# AttributeError reached the live order path: the test was more permissive
# than production. Anything these tests touch must exist on the real types.


def FakeProposal(proposal_id: str) -> TradeProposal:
    return TradeProposal(
        id=proposal_id, strategy="primary", window_open=1790001000000,
        ticker="KXBTC15M-26SEP211045-45", side="UP", entry_limit=0.87,
        take_profit=0.99, count=1.0, expires_at=0, close_ms=1790001900000,
        status="claimed",
    )


def FakeResult(filled_count: float, fill_price: float | None = None):
    # `fill_price` is accepted and ignored: ExecutionResult has no such field,
    # and the logger must not invent one.
    return ExecutionResult(
        status="filled" if filled_count else "unfilled",
        filled_count=filled_count, entry_order_id="o1",
        take_profit_order_id=None, note="",
    )


@pytest.fixture
def store(tmp_path):
    return Store(str(tmp_path / "t.db"))


def log(store, proposal_id, result, *, decision_ms=1790001278467,
        submitted_ms=1790001280867, acked_ms=1790001281177, attempt=1):
    """Seed the proposal row the logger reads its decision time from."""
    if decision_ms is not None:
        store.db.execute(
            "INSERT OR REPLACE INTO trade_proposals (id,strategy,window_open,"
            "ticker,side,entry_limit,take_profit,count,expires_at,close_ms,"
            "status,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (proposal_id, "primary", 1790001000000,
             "KXBTC15M-26SEP211045-45", "UP", 0.87, 0.99, 1.0, 0,
             1790001900000, "claimed", decision_ms),
        )
        store.db.commit()
    log_execution(
        store, claimed=FakeProposal(proposal_id), result=result,
        decision_ask=0.87, submitted_ms=submitted_ms, acked_ms=acked_ms,
        attempt=attempt,
    )


def row(store):
    cols = [c[1] for c in store.db.execute("PRAGMA table_info(executions)")]
    return dict(zip(cols, store.db.execute("SELECT * FROM executions").fetchone(),
                    strict=True))


def test_a_missed_order_is_recorded(store):
    log(store, "p1", FakeResult(filled_count=0.0))
    assert store.execution_report() == {
        "orders": 1, "filled": 0, "fill_rate": 0.0,
        "avg_decision_to_submit_ms": 2400.0, "avg_round_trip_ms": 310.0,
    }


def test_a_filled_order_is_recorded_as_filled(store):
    log(store, "p1", FakeResult(filled_count=1.0))
    r = row(store)
    assert r["filled"] == 1
    assert r["fill_count"] == pytest.approx(1.0)


def test_no_fill_price_is_invented(store):
    """ExecutionResult carries none; it is read back separately and joined."""
    log(store, "p1", FakeResult(filled_count=1.0))
    assert row(store)["fill_price"] is None


def test_the_logger_survives_a_proposal_missing_from_the_table(store):
    """The decision time is looked up, so a missing row must not raise."""
    log(store, "p1", FakeResult(0.0), decision_ms=None)
    assert row(store)["decision_to_submit_ms"] is None


def test_an_order_that_threw_is_still_recorded_as_a_miss(store):
    """result is None when the call raised. That row matters most."""
    log(store, "p1", None)
    r = row(store)
    assert r["filled"] == 0
    assert r["fill_count"] == 0.0


def test_the_staleness_of_the_decision_is_what_gets_measured(store):
    """decision -> submit is the free proxy for 'how far could the book move'."""
    log(store, "p1", FakeResult(0.0),
        decision_ms=1_000_000, submitted_ms=1_009_000, acked_ms=1_009_250)
    r = row(store)
    assert r["decision_to_submit_ms"] == 9000
    assert r["round_trip_ms"] == 250


def test_fill_rate_counts_misses_in_the_denominator(store):
    log(store, "p1", FakeResult(0.0))
    log(store, "p2", FakeResult(1.0, 0.9))
    log(store, "p3", FakeResult(0.0))
    r = store.execution_report()
    assert (r["orders"], r["filled"]) == (3, 1)
    assert r["fill_rate"] == pytest.approx(1 / 3)


def test_retries_keep_their_attempt_number(store):
    log(store, "p1", FakeResult(0.0), attempt=1)
    log(store, "p2", FakeResult(0.0), attempt=2)
    log(store, "p3", FakeResult(1.0, 0.96), attempt=3)
    got = store.db.execute(
        "SELECT attempt, filled FROM executions ORDER BY attempt"
    ).fetchall()
    assert [tuple(r) for r in got] == [(1, 0), (2, 0), (3, 1)]


def test_logging_never_raises_into_the_order_path(store):
    """Money has already moved by the time this runs. It must not throw."""
    class Broken:
        filled_count = property(lambda self: (_ for _ in ()).throw(RuntimeError("x")))
    log_execution(
        store, claimed=FakeProposal("p1"), result=Broken(), decision_ask=0.87,
        submitted_ms=2, acked_ms=3, attempt=1,
    )  # must not raise


def test_no_argument_is_evaluated_outside_the_loggers_own_guard(store):
    """The call site must not touch anything that can raise.

    The live bug: `decision_ms=claimed.created_at` was evaluated as an argument
    expression, so the AttributeError fired BEFORE log_execution was entered
    and its try/except was powerless. A proposal object with every attribute
    access booby-trapped must still not take the order path down.
    """
    class Hostile:
        def __getattr__(self, name):
            raise AttributeError(name)
    log_execution(
        store, claimed=Hostile(), result=FakeResult(0.0), decision_ask=0.87,
        submitted_ms=2, acked_ms=3, attempt=1,
    )  # must not raise


def test_an_empty_archive_reports_no_fill_rate_rather_than_zero(store):
    assert store.execution_report()["fill_rate"] is None
