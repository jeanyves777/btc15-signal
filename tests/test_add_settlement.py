"""Closing the lifecycle on a filled recovery add, from the broker's record.

THE DEFECT, found 2026-09-23 while verifying something else: `settled` and
`realised_pnl` were declared on `recovery_adds`, `unsettled_filled_adds` was
written to find the backlog, and NOTHING EVER CALLED IT. All 7 filled adds sat
at `settled = 0` with a NULL P&L, so `add_pnl_summary` reported $0.00 for the
add-on however much it had made. The one number the live test exists to produce
had never been computed. It was +$1.4668 over those 7.

Two properties matter more than the arithmetic:

* **A settlement row is the evidence, never the clock.** A market that stopped
  trading has settled nothing.
* **It moves no money.** `daily_ledger` already holds the broker's P&L for the
  whole position and the deficit is already credited from it. The figure here
  is the add leg's own and is read only by reporting.
"""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from btc15_signal.store import Store  # noqa: E402

W = 1_790_193_600_000
TICKER = "KXBTC15M-26SEP231615-15"
NOW = int(time.time() * 1000)


def build(tmp_path, *, side="DOWN", fill=0.83, fee=0.0, filled=1.0,
          base_count=2.0, exit_count=None, exit_price=None,
          settlement="no", name="a.db"):
    store = Store(str(tmp_path / name))
    store.db.execute(
        "INSERT INTO trade_proposals (id, strategy, window_open, ticker, side,"
        " entry_limit, take_profit, count, expires_at, close_ms, status,"
        " created_at, fill_price, fee_paid, exit_price, exit_count)"
        " VALUES ('base','primary',?,?,?,0.86,0.99,?,0,0,?,0,0.85,0.0179,?,?)",
        (W, TICKER, side, base_count,
         "exited" if exit_count else "filled", exit_price, exit_count),
    )
    store.db.execute(
        "INSERT INTO recovery_adds (client_order_id, window_open_ms, ticker,"
        " side, state, order_id, base_fill, limit_price, count, placed_ms,"
        " filled_count, fill_price, fee_paid, settled, created_ms, updated_ms)"
        " VALUES ('coid',?,?,?,'RECOVERY ADD EXECUTED','ord-1',0.85,?,1,1,"
        "?,?,?,0,0,0)",
        (W, TICKER, side, fill, filled, fill, fee),
    )
    if settlement is not None:
        store.db.execute(
            "INSERT INTO settlements (ticker, event_ticker, market_result,"
            " yes_count, yes_cost, no_count, no_cost, revenue_cents, fee_cost,"
            " pnl, settled_ms, synced_at, window_ms)"
            " VALUES (?,?,?,0,0,3,2.53,100,0.0184,0.4456,?,?,?)",
            (TICKER, TICKER.rsplit("-", 1)[0], settlement, NOW, NOW, W),
        )
    store.db.commit()
    return store


def add_row(store):
    return store._dicts("SELECT * FROM recovery_adds")[0]


# ------------------------------------------- the evidence is the settlement

def test_a_filled_add_is_closed_from_the_brokers_settlement(tmp_path):
    store = build(tmp_path)
    assert len(store.unsettled_filled_adds()) == 1
    assert store.settle_filled_adds(NOW) == 1
    row = add_row(store)
    assert row["settled"] == 1
    # DOWN, market resolved no: the leg was right. 1 contract, cost 0.83.
    assert row["realised_pnl"] == 0.17
    assert store.unsettled_filled_adds() == []


def test_an_add_is_never_settled_without_a_settlement_row(tmp_path):
    """A market that stopped trading has settled nothing."""
    store = build(tmp_path, settlement=None)
    assert store.settle_filled_adds(NOW) == 0
    assert add_row(store)["settled"] == 0
    assert add_row(store)["realised_pnl"] is None


def test_a_result_we_cannot_read_leaves_the_add_open(tmp_path):
    """A void or an unrecognised result is not a verdict either."""
    for i, result in enumerate(("", "void", "unknown")):
        store = build(tmp_path, settlement=result, name=f"v{i}.db")
        assert store.settle_filled_adds(NOW) == 0, result
        assert add_row(store)["settled"] == 0


def test_a_losing_add_is_closed_at_its_full_cost(tmp_path):
    """DOWN held, market resolved yes: the contract paid nothing."""
    store = build(tmp_path, side="DOWN", fill=0.83, settlement="yes")
    assert store.settle_filled_adds(NOW) == 1
    assert add_row(store)["realised_pnl"] == -0.83


def test_the_brokers_own_fee_is_charged_once(tmp_path):
    store = build(tmp_path, fill=0.75, fee=0.0132, settlement="no")
    store.settle_filled_adds(NOW)
    assert add_row(store)["realised_pnl"] == round(1.0 - 0.75 - 0.0132, 6)


# --------------------------------------------------------- it is idempotent

def test_settling_twice_changes_nothing(tmp_path):
    store = build(tmp_path)
    assert store.settle_filled_adds(NOW) == 1
    first = add_row(store)["realised_pnl"]
    assert store.settle_filled_adds(NOW + 60_000) == 0
    assert add_row(store)["realised_pnl"] == first


# ------------------------------------------------------- it moves no money

def test_settling_an_add_does_not_touch_the_ledger_or_the_deficit(tmp_path):
    """The whole position was already banked once, by the exchange."""
    store = build(tmp_path)
    store.record_realised(TICKER, W, 0.4456, True, "exchange", NOW)
    before_ledger = store.db.execute(
        "SELECT ticker, pnl, won, source, recovery_applied FROM daily_ledger "
        "ORDER BY ticker"
    ).fetchall()
    before_deficit = store.db.execute("SELECT * FROM recovery_deficit").fetchall()
    before_budget = store.db.execute(
        "SELECT * FROM recovery_add_budget"
    ).fetchall()

    store.settle_filled_adds(NOW)

    assert store.db.execute(
        "SELECT ticker, pnl, won, source, recovery_applied FROM daily_ledger "
        "ORDER BY ticker"
    ).fetchall() == before_ledger
    assert store.db.execute(
        "SELECT * FROM recovery_deficit"
    ).fetchall() == before_deficit
    assert store.db.execute(
        "SELECT * FROM recovery_add_budget"
    ).fetchall() == before_budget


def test_the_add_pnl_summary_stops_reporting_zero(tmp_path):
    """What the live test is for: does the SECOND contract pay?"""
    store = build(tmp_path)
    assert store.add_pnl_summary()["pnl"] == 0
    store.settle_filled_adds(NOW)
    assert store.add_pnl_summary()["pnl"] == 0.17


# ------------------------------------------------ the one stated attribution

def test_an_exit_no_bigger_than_the_base_never_reaches_the_add(tmp_path):
    """All 7 real rows are this shape: the exit sold exactly the base."""
    store = build(tmp_path, base_count=2.0, exit_count=2.0, exit_price=0.997)
    store.settle_filled_adds(NOW)
    # The add ran to settlement, so it is worth $1 less what it cost - the
    # 99.7c sale belongs entirely to the base.
    assert add_row(store)["realised_pnl"] == 0.17


def test_an_exit_larger_than_the_base_reaches_the_add(tmp_path):
    """Base 2, sold 3: one of those was the add, at the exit price."""
    store = build(tmp_path, base_count=2.0, exit_count=3.0, exit_price=0.997)
    store.settle_filled_adds(NOW)
    realised = add_row(store)["realised_pnl"]
    assert realised is not None
    # Sold at 0.997 against a 0.83 fill, less the exit fee - NOT the $1.00 a
    # held contract would have returned.
    assert 0.16 < realised < 0.17


def test_an_unfilled_add_is_not_settled_at_all(tmp_path):
    """Only a leg that actually bought something has a P&L."""
    store = build(tmp_path)
    store.db.execute(
        "UPDATE recovery_adds SET filled_count = 0, state = "
        "'RECOVERY ADD SKIPPED', placed_ms = NULL, order_id = NULL"
    )
    store.db.commit()
    assert store.settle_filled_adds(NOW) == 0
    assert add_row(store)["settled"] == 0
