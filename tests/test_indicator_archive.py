"""The two indicators are archived, so the thresholds can be re-measured.

The reversal gate shipped blocking on a threshold chosen from 79 markets
(FINDINGS 58), with the evidence against it recorded beside the evidence for
it. That is only revisitable if the rows say what the gate saw - a gate whose
input is not archived can never be re-measured, only re-argued.

Both indicators are stored on EVERY decision, qualified or not, because the
refused setups are exactly the counterfactual the threshold has to be judged
on. One gates and one does not; they are archived identically so they can be
compared against outcomes on equal footing.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from btc15_signal.store import Store  # noqa: E402

W = 1_790_193_600_000
NOW = 1_790_200_000_000


def columns(store):
    return {d[1] for d in store.db.execute(
        "PRAGMA table_info(intelligence_decisions)")}


def test_both_columns_exist(tmp_path):
    store = Store(str(tmp_path / "a.db"))
    cols = columns(store)
    assert "brti_retrace" in cols
    assert "brti_choppiness" in cols


def test_they_are_added_to_a_database_that_predates_them(tmp_path):
    """THE MIGRATION, not a CREATE TABLE. A bare `CREATE TABLE IF NOT EXISTS`
    adds nothing to a table that already exists, which is how a column has
    reached every fresh install and no live database before."""
    path = tmp_path / "old.db"
    store = Store(str(path))
    store.db.execute("ALTER TABLE intelligence_decisions DROP COLUMN brti_retrace")
    store.db.execute("ALTER TABLE intelligence_decisions DROP COLUMN brti_choppiness")
    store.db.commit()
    store.db.close()

    reopened = Store(str(path))
    cols = columns(reopened)
    assert "brti_retrace" in cols, "migration must add it to an existing table"
    assert "brti_choppiness" in cols


def test_a_decision_stores_both(tmp_path):
    store = Store(str(tmp_path / "b.db"))
    store.record_intelligence({
        "window_open": W, "ticker": "T", "signal_id": "s1",
        "decided_ms": NOW, "side": "UP", "ask": 0.80,
        "base_qualified": 1, "final_action": "neutral", "reason": "t",
        "confidence_delta": 0, "evidence_n": 10,
        "policy_version": "p", "feature_version": "brti-2",
        "brti_retrace": 0.42, "brti_choppiness": 0.91,
    })
    row = store._dicts(
        "SELECT brti_retrace, brti_choppiness FROM intelligence_decisions")[0]
    assert row["brti_retrace"] == 0.42
    assert row["brti_choppiness"] == 0.91


def test_an_unmeasurable_window_stores_null_not_zero(tmp_path):
    """Zero is a straight line and a fully intact move. Unknown is neither."""
    store = Store(str(tmp_path / "c.db"))
    store.record_intelligence({
        "window_open": W, "ticker": "T", "signal_id": "s1",
        "decided_ms": NOW, "side": "UP", "ask": 0.80,
        "base_qualified": 1, "final_action": "neutral", "reason": "t",
        "confidence_delta": 0, "evidence_n": 10,
        "policy_version": "p", "feature_version": "brti-2",
        "brti_retrace": None, "brti_choppiness": None,
    })
    row = store._dicts(
        "SELECT brti_retrace, brti_choppiness FROM intelligence_decisions")[0]
    assert row["brti_retrace"] is None
    assert row["brti_choppiness"] is None


def test_the_live_path_records_them():
    """Pinned: if the call site stops passing them the archive goes quiet and
    nothing fails, which is how `ticker` stayed NULL for 1,174 rows."""
    import inspect

    from btc15_signal import main

    # Anchored on the INTELLIGENCE recorder specifically. main.py has two
    # payloads carrying a session; looking at the first one found passes
    # against the observations row and proves nothing about this archive.
    source = inspect.getsource(main.intelligence_verdict)
    assert '"brti_retrace"' in source
    assert '"brti_choppiness"' in source
    assert 'record_intelligence' in source


def test_refused_setups_are_archived_too():
    """The counterfactual the threshold is judged on. `record_intelligence`
    is called before qualification is decided, not after."""
    import inspect

    from btc15_signal import main

    source = inspect.getsource(main.intelligence_verdict)
    assert "base_qualified" in source
    assert "record_intelligence" in source
