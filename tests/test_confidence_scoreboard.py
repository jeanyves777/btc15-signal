"""Confidence arms are tracked and evaluated, and the data they need is captured.

THE GAP. The learned layer is authorised to do exactly one thing - move the
confidence label - and it does, on about 12% of live decisions. Nothing
anywhere recorded whether the direction it moved them was right.
`forward_scoreboard` tracks EXECUTION candidates (what a veto or an admission
would have changed); no arm has ever been promoted to execution, so it scored
an empty set while the only live behaviour went unmeasured.

THE MEASURE IS CALIBRATION, NOT P&L. A confidence arm claims a cell wins more
(or less) than its price implies, so it is judged on `won - ask`. Ranking
arms on P&L would retire a 0.90-priced cell winning 92% - a good arm - and
keep a 0.60-priced cell winning 55%.

THE CAPTURE GAPS, all columns declared on `intelligence_decisions` and never
written: `signal_id`, `proposal_id`, `fill_price`, `fee_cost` were NULL on
every row, so the archive could not tell a decision's ASK from the price the
account was actually charged - and `realised_pnl` was a counterfactual built
from the ask. `authority_granted` is new: `authority` records only what was
WITHHELD, so a neutral row could not be read as "the evidence said neutral"
or "the permission was never granted", which are opposite facts.
"""

import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from btc15_signal.learning_store import LearningStore  # noqa: E402
from btc15_signal.store import Store  # noqa: E402

W = 1_790_193_600_000
FV = "brti-2"


def decision(store, *, window, key, delta, ask, won, filled=1,
             graded=1, poll=0, fill_price=None):
    store.record_intelligence({
        "window_open": window, "ticker": f"T{window}", "signal_id": f"T{window}",
        "decided_ms": window + poll, "side": "UP", "ask": ask,
        "base_qualified": 1, "final_action": "neutral", "reason": "t",
        "confidence_delta": delta, "evidence_n": 50, "context_key": key,
        "policy_version": "p1", "feature_version": FV,
    })
    store.db.execute(
        "UPDATE intelligence_decisions SET won=?, filled=?, graded_ms=?, "
        "realised_pnl=?, fill_price=? WHERE window_open=? AND decided_ms=?",
        (won, filled, window + 900_000 if graded else None,
         (1.0 if won else 0.0) - ask, fill_price, window, window + poll),
    )
    store.db.commit()


def fresh(tmp_path, name="s.db"):
    store = Store(str(tmp_path / name))
    return store, LearningStore(store.db)


# --------------------------------------------------------- capture gaps

def test_the_never_written_columns_exist():
    """They were declared and never populated. The column existing was never
    the problem; this test is the floor for the ones below."""
    import sqlite3 as s3
    import tempfile

    store = Store(str(Path(tempfile.mkdtemp()) / "c.db"))
    cols = {d[1] for d in store.db.execute(
        "PRAGMA table_info(intelligence_decisions)")}
    for name in ("signal_id", "proposal_id", "fill_price", "fee_cost",
                 "authority_granted"):
        assert name in cols, name
    assert isinstance(store.db, s3.Connection)


def test_authority_granted_migrates_onto_an_existing_table(tmp_path):
    """CREATE TABLE IF NOT EXISTS adds nothing to a table that already
    exists - the way every column has reached a fresh install and no live
    database before."""
    path = tmp_path / "old.db"
    store = Store(str(path))
    store.db.execute(
        "ALTER TABLE intelligence_decisions DROP COLUMN authority_granted")
    store.db.commit()
    store.db.close()
    cols = {d[1] for d in Store(str(path)).db.execute(
        "PRAGMA table_info(intelligence_decisions)")}
    assert "authority_granted" in cols


def test_the_live_path_records_the_new_columns():
    """Pinned: if the call site stops passing them the archive goes quiet and
    nothing fails - which is how these columns stayed NULL for 3,493 rows."""
    import inspect

    from btc15_signal import main

    source = inspect.getsource(main.intelligence_verdict)
    assert '"signal_id"' in source
    assert '"authority_granted"' in source


def test_linking_writes_the_price_actually_paid(tmp_path):
    """`realised_pnl` is otherwise a counterfactual computed from the ask."""
    store, _ = fresh(tmp_path)
    decision(store, window=W, key="k", delta=5, ask=0.80, won=1,
             filled=None, graded=1)
    store.db.execute(
        "INSERT INTO trade_proposals (id, strategy, window_open, ticker, side, "
        "entry_limit, take_profit, count, expires_at, close_ms, status, "
        "created_at, entry_order_id, fill_price, fee_paid) "
        "VALUES (?,'primary',?,?,?,?,0,?,?,?,'filled',?,?,?,?)",
        ("prop-1", W, f"T{W}", "UP", 0.80, 2, W + 60_000, W + 900_000, W,
         "ord-1", 0.78, 0.021))
    store.db.commit()
    assert store.link_intelligence_orders() > 0
    row = store._dicts(
        "SELECT order_id, filled, proposal_id, fill_price, fee_cost "
        "FROM intelligence_decisions")[0]
    assert row["order_id"] == "ord-1"
    assert row["filled"] == 1
    assert row["proposal_id"] is not None
    assert row["fill_price"] == 0.78
    assert row["fee_cost"] == 0.021


# ------------------------------------------------------- the scoreboard

def test_an_arm_that_moved_the_right_way_agrees(tmp_path):
    """Raised confidence on a cell that beat its price."""
    store, learning = fresh(tmp_path)
    for i in range(10):
        decision(store, window=W + i * 900_000, key="good", delta=6,
                 ask=0.70, won=1)
    board = learning.confidence_scoreboard(FV)
    assert len(board) == 1
    assert board[0]["direction"] == "raised"
    assert board[0]["residual"] > 0
    assert board[0]["agrees"] is True


def test_an_arm_that_moved_the_wrong_way_does_not(tmp_path):
    """THE CASE THIS WAS BUILT FOR. On live data all three brti-2 arms raised
    or lowered against the outcome, and nothing reported it."""
    store, learning = fresh(tmp_path)
    for i in range(10):
        decision(store, window=W + i * 900_000, key="bad", delta=6,
                 ask=0.90, won=1 if i < 5 else 0)
    board = learning.confidence_scoreboard(FV)
    assert board[0]["residual"] < 0          # 50% against a 0.90 price
    assert board[0]["agrees"] is False


def test_it_counts_MARKETS_not_polls(tmp_path):
    """A decision row is written every ten seconds. Pooling polls counts one
    outcome dozens of times and shrinks every interval to nothing."""
    store, learning = fresh(tmp_path)
    for poll in range(25):
        decision(store, window=W, key="k", delta=6, ask=0.70, won=1,
                 poll=poll * 10_000)
    board = learning.confidence_scoreboard(FV)
    assert board[0]["markets"] == 1, board


def test_neutral_decisions_are_not_scored(tmp_path):
    """An arm is only answerable for the rows it moved."""
    store, learning = fresh(tmp_path)
    for i in range(6):
        decision(store, window=W + i * 900_000, key="k", delta=0,
                 ask=0.70, won=1)
    assert learning.confidence_scoreboard(FV) == []


def test_ungraded_markets_are_excluded(tmp_path):
    """Before settlement the answer can still change - the defect that wrote
    `filled = 0` on two windows whose orders had not filled yet."""
    store, learning = fresh(tmp_path)
    for i in range(6):
        decision(store, window=W + i * 900_000, key="k", delta=6,
                 ask=0.70, won=1, graded=0)
    assert learning.confidence_scoreboard(FV) == []


def test_another_feature_version_is_never_pooled(tmp_path):
    """`bd10-15 · px85-93` computed from brti-1 is not the same population as
    the identical string from brti-2 - the FINDINGS 49 class of mistake."""
    store, learning = fresh(tmp_path)
    for i in range(6):
        decision(store, window=W + i * 900_000, key="k", delta=6,
                 ask=0.70, won=1)
    store.db.execute(
        "UPDATE intelligence_decisions SET feature_version='brti-1'")
    store.db.commit()
    assert learning.confidence_scoreboard(FV) == []
    assert learning.confidence_scoreboard("brti-1") != []


def test_raised_and_lowered_are_scored_separately(tmp_path):
    """One cell can hold both legs and they make opposite claims."""
    store, learning = fresh(tmp_path)
    for i in range(6):
        decision(store, window=W + i * 900_000, key="k", delta=6,
                 ask=0.70, won=1)
    for i in range(6, 12):
        decision(store, window=W + i * 900_000, key="k", delta=-6,
                 ask=0.70, won=0)
    board = learning.confidence_scoreboard(FV)
    assert {r["direction"] for r in board} == {"raised", "lowered"}
    assert all(r["agrees"] for r in board)


def test_it_can_be_narrowed_to_one_policy(tmp_path):
    store, learning = fresh(tmp_path)
    for i in range(6):
        decision(store, window=W + i * 900_000, key="k", delta=6,
                 ask=0.70, won=1)
    store.db.execute(
        "UPDATE intelligence_decisions SET policy_version='p2' "
        "WHERE window_open >= ?", (W + 3 * 900_000,))
    store.db.commit()
    assert learning.confidence_scoreboard(FV, "p2")[0]["markets"] == 3
    assert learning.confidence_scoreboard(FV)[0]["markets"] == 6


def test_it_reports_and_never_acts():
    """Same standard the layer itself is held to: this measures, it does not
    promote, withdraw or size."""
    import inspect

    source = inspect.getsource(LearningStore.confidence_scoreboard)
    for forbidden in ("UPDATE", "INSERT", "DELETE", "promote", "withdraw"):
        assert forbidden not in source, forbidden
