"""Candidates are evaluated forward, and control nothing while they are.

The operator's point, and it is the one that makes a learning loop a loop:

    "No adjustment has earned promotion" is a valid result.
    "Therefore there is nothing to forward-test" is not.

So a candidate that fails the promotion bar is still recorded against every
eligible live signal - what the unchanged strategy decided, what the candidate
would have changed, and which was right once the market settled. These tests
pin that it happens, that it is honest, and that it cannot touch an order.
"""

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from btc15_signal.candidates import Candidate, CandidateSet, grade  # noqa: E402
from btc15_signal.intelligence_policy import ADMIT, VETO  # noqa: E402
from btc15_signal.store import Store  # noqa: E402

NOW = int(time.time() * 1000)
CTX = "asia · mid · bd10-15 · px70-85"


def reward(ask, won):
    from btc15_signal.validation import kalshi_fee_charged as fee
    return (1.0 if won else 0.0) - ask - fee(ask, 1)


def candidate_set(tmp_path, promotes=False) -> CandidateSet:
    artefact = {
        "version": "cand-test-1", "feature_version": "brti-1",
        "built_ms": NOW, "data_end_ms": NOW, "training_cutoff_ms": NOW - 1,
        "candidates": [
            {"candidate_id": "c01", "context": f"{CTX}|accept",
             "proposed_action": VETO, "train_n": 90, "train_mean": -0.04,
             "promotes": promotes, "delta": -8},
            {"candidate_id": "c02", "context": f"{CTX}|reject",
             "proposed_action": ADMIT, "train_n": 80, "train_mean": 0.03,
             "promotes": promotes, "delta": 6},
        ],
    }
    path = tmp_path / "cand.json"
    path.write_text(json.dumps(artefact))
    return CandidateSet.load(path)


# ------------------------------------------------- it evaluates forward

def test_a_candidate_that_failed_promotion_is_still_evaluated(tmp_path):
    cs = candidate_set(tmp_path, promotes=False)
    assert all(not c.promotes for c in cs.candidates)
    rows = cs.evaluate(context_key=CTX, qualified=True)
    assert rows, "a failed candidate must still be recorded forward"


def test_only_candidates_that_speak_to_the_context_are_recorded(tmp_path):
    cs = candidate_set(tmp_path)
    assert cs.evaluate(context_key="us · high · bd<5 · px94+", qualified=True) == []


def test_a_veto_only_bites_on_a_qualified_setup(tmp_path):
    cs = candidate_set(tmp_path)
    on_qualified = {r["candidate_id"]: r for r in
                    cs.evaluate(context_key=CTX, qualified=True)}
    assert on_qualified["c01"]["would_change"] == 1
    on_rejected = {r["candidate_id"]: r for r in
                   cs.evaluate(context_key=CTX, qualified=False)}
    assert on_rejected["c01"]["would_change"] == 0


def test_an_admission_only_bites_on_a_refused_setup(tmp_path):
    cs = candidate_set(tmp_path)
    on_rejected = {r["candidate_id"]: r for r in
                   cs.evaluate(context_key=CTX, qualified=False)}
    assert on_rejected["c02"]["would_change"] == 1
    on_qualified = {r["candidate_id"]: r for r in
                    cs.evaluate(context_key=CTX, qualified=True)}
    assert on_qualified["c02"]["would_change"] == 0


# ------------------------------------------------------- honest grading

def test_a_veto_that_avoided_a_loser_scores_positive():
    row = {"ask": 0.80, "baseline_qualified": 1, "would_change": 1,
           "proposed_action": VETO}
    baseline, candidate = grade(row, won=False, reward=reward)
    assert baseline < 0, "the rule took a loser"
    assert candidate == 0.0, "the veto stood aside"
    assert candidate - baseline > 0


def test_a_veto_that_blocked_a_winner_scores_negative():
    row = {"ask": 0.80, "baseline_qualified": 1, "would_change": 1,
           "proposed_action": VETO}
    baseline, candidate = grade(row, won=True, reward=reward)
    assert baseline > 0
    assert candidate == 0.0
    assert candidate - baseline < 0, "blocking a winner must cost it"


def test_an_admission_that_caught_a_winner_scores_positive():
    row = {"ask": 0.60, "baseline_qualified": 0, "would_change": 1,
           "proposed_action": ADMIT}
    baseline, candidate = grade(row, won=True, reward=reward)
    assert baseline == 0.0, "the rule stood aside, so it earned nothing"
    assert candidate > 0


def test_an_admission_that_caught_a_loser_scores_negative():
    row = {"ask": 0.60, "baseline_qualified": 0, "would_change": 1,
           "proposed_action": ADMIT}
    baseline, candidate = grade(row, won=False, reward=reward)
    assert baseline == 0.0
    assert candidate < 0, "the losses it admits count too"


def test_a_candidate_that_agreed_scores_exactly_the_baseline():
    row = {"ask": 0.80, "baseline_qualified": 1, "would_change": 0,
           "proposed_action": VETO}
    baseline, candidate = grade(row, won=True, reward=reward)
    assert baseline == candidate, "agreeing changes nothing, and scores nothing"


# ------------------------------------------------ recorded and graded

def test_predictions_are_recorded_once_per_market(tmp_path):
    store = Store(str(tmp_path / "s.db"))
    rows = [{
        "window_open": 100, "decided_ms": NOW, "candidate_id": "c01",
        "candidate_version": "v1", "context_key": CTX,
        "proposed_action": VETO, "baseline_qualified": 1, "would_change": 1,
        "side": "UP", "ask": 0.80,
    }]
    store.record_candidate_evaluations(rows)
    store.record_candidate_evaluations(rows)      # a second poll, same market
    got = store._dicts("SELECT * FROM candidate_evaluations")
    assert len(got) == 1, "repeated polls are not repeated predictions"


def test_grading_scores_baseline_and_candidate(tmp_path):
    store = Store(str(tmp_path / "s.db"))
    store.record_candidate_evaluations([{
        "window_open": 101, "decided_ms": NOW, "candidate_id": "c01",
        "candidate_version": "v1", "context_key": CTX,
        "proposed_action": VETO, "baseline_qualified": 1, "would_change": 1,
        "side": "UP", "ask": 0.80,
    }])
    store.grade_candidates(101, "DOWN", now_ms=NOW + 1, reward=reward)
    row = store._dicts("SELECT * FROM candidate_evaluations")[0]
    assert row["won"] == 0
    assert row["baseline_pnl"] < 0
    assert row["candidate_pnl"] == 0.0
    assert row["graded_ms"]


def test_the_scoreboard_counts_only_rows_the_candidate_changed(tmp_path):
    store = Store(str(tmp_path / "s.db"))
    for window, changed in ((201, 1), (202, 0)):
        store.record_candidate_evaluations([{
            "window_open": window, "decided_ms": NOW, "candidate_id": "c01",
            "candidate_version": "v1", "context_key": CTX,
            "proposed_action": VETO, "baseline_qualified": 1,
            "would_change": changed, "side": "UP", "ask": 0.80,
        }])
        store.grade_candidates(window, "DOWN", now_ms=NOW + 1, reward=reward)
    board = store.candidate_scoreboard()[0]
    assert board["seen"] == 2
    assert board["changes"] == 1
    assert board["incremental"] > 0, "only the changed row contributes"


def test_each_row_is_graded_on_its_own_side(tmp_path):
    """One window can hold rows on both sides once the model flips, and one
    boolean cannot be right for both. The market-level flag the settlement
    loop could form was true by construction, so every row graded as a win."""
    store = Store(str(tmp_path / "s.db"))
    for i, side in enumerate(("UP", "DOWN")):
        store.record_candidate_evaluations([{
            "window_open": 301, "decided_ms": NOW, "candidate_id": f"c{i}",
            "candidate_version": "v1", "context_key": CTX,
            "proposed_action": VETO, "baseline_qualified": 1,
            "would_change": 1, "side": side, "ask": 0.80,
        }])
    store.grade_candidates(301, "UP", now_ms=NOW + 1, reward=reward)
    graded = {r["side"]: r["won"] for r in store._dicts(
        "SELECT side, won FROM candidate_evaluations")}
    assert graded == {"UP": 1, "DOWN": 0}


def test_an_empty_artefact_is_inert(tmp_path):
    cs = CandidateSet.load(tmp_path / "missing.json")
    assert cs.version == "none"
    assert cs.evaluate(context_key=CTX, qualified=True) == []


def test_the_artefact_version_travels_with_every_prediction(tmp_path):
    """A candidate refitted between a prediction and its grading would make
    the record meaningless, so the version that made the call is stored."""
    cs = candidate_set(tmp_path)
    rows = cs.evaluate(context_key=CTX, qualified=True)
    assert all(r["candidate_version"] == "cand-test-1" for r in rows)
    assert all(r["feature_version"] == "brti-1" for r in rows)


def test_candidates_expose_no_way_to_change_an_order():
    """The structural guarantee: nothing in this module returns a decision."""
    public = {name for name in dir(Candidate) if not name.startswith("_")}
    assert "qualifies" not in public
    assert "final_action" not in public


# --------------------------------------- live and replay name the same cell

def test_the_live_key_and_the_training_key_come_from_one_function():
    """The defect this pins, and it is the second time in this shape.

    The live context was built with Binance distance bands (`dist1.5-3`)
    while candidates were frozen on BRTI bands (`bd5-10`). Nothing errors: the
    key formats, it just names a pocket that does not exist in the artefact,
    so every candidate matches nothing and the table stays empty forever
    while the logs say the layer is integrated.
    """
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
    from brti_dataset import brti_context

    from btc15_signal.adaptive import brti_context_of

    row = {"session": "us", "brti_volatility_bps": 0.3,
           "brti_normalized_distance": 7.5, "our_ask": 0.62}
    assert brti_context(row) == str(brti_context_of(row))
    assert str(brti_context_of(row)) == "us · low · bd5-10 · px<70"


def test_a_brti_row_is_not_a_binance_row():
    """Same market, two instruments, two cells. BRTI reads ~10-20 where
    Binance reads 2-4, so a Binance number passed to the BRTI bands lands in
    `bd<5` every time - which is why the two carry different key names."""
    from btc15_signal.adaptive import brti_context_of, context_of

    brti = {"session": "us", "brti_volatility_bps": 0.3,
            "brti_normalized_distance": 12.0, "our_ask": 0.62}
    binance = {"session": "us", "vol_regime": "low",
               "normalized_distance": 2.4, "our_ask": 0.62}
    assert str(brti_context_of(brti)) != str(context_of(binance))
    # and the BRTI function cannot read the Binance row by accident
    assert brti_context_of(binance).distance == "bd<5"


# ------------------------------------------- the live context row is guarded

class _F:
    def __init__(self, target=100.0, stale=False, vol=0.4, dist=12.0):
        self.target, self.stale = target, stale
        self.brti_volatility_bps, self.brti_normalized_distance = vol, dist


class _S:
    target = 100.0


def test_a_good_brti_row_labels_the_cell():
    from btc15_signal.main import brti_context_row

    row, why = brti_context_row(_S(), 0.62, 0, _F())
    assert why == ""
    assert row["brti_normalized_distance"] == 12.0


def test_missing_stale_or_foreign_brti_yields_no_context():
    """Each of these must return None, never a Binance-scale fallback: a
    mislabelled row is invisible, a missing one is not."""
    from btc15_signal.main import brti_context_row

    assert brti_context_row(_S(), 0.62, 0, None)[0] is None
    assert brti_context_row(_S(), 0.62, 0, _F(stale=True))[0] is None
    # the window rolled between the reference poll and this decision
    row, why = brti_context_row(_S(), 0.62, 0, _F(target=101.0))
    assert row is None and "another window" in why


# ------------------------------- one market is one opportunity, not six polls

def test_the_intelligence_summary_counts_markets_not_polls(tmp_path):
    """`intelligence_decisions` holds one row per POLL, on purpose, so a
    decision that changed mid-window is not lost. Any SUMMARY over it must
    still count markets: 2 settled markets once appeared as "39 rows, 39 won,
    100%", which is the live twin of the corpus scope error."""
    store = Store(str(tmp_path / "s.db"))
    for poll in range(6):
        store.record_intelligence({
            "window_open": 500, "decided_ms": NOW + poll, "base_qualified": 1,
            "final_action": "neutral", "confidence_delta": 0, "side": "UP",
        })
    store.grade_intelligence(500, "UP", 0.0, NOW + 10)
    summary = store.intelligence_summary()["neutral"]
    assert summary["n"] == 1, "six polls of one window are one opportunity"
    assert summary["wins"] == 1
    assert summary["graded"] == 1


def test_the_summary_separates_genuinely_distinct_markets(tmp_path):
    store = Store(str(tmp_path / "s.db"))
    for window, side in ((600, "UP"), (601, "DOWN")):
        for poll in range(3):
            store.record_intelligence({
                "window_open": window, "decided_ms": NOW + poll,
                "base_qualified": 1, "final_action": "neutral",
                "confidence_delta": 0, "side": side,
            })
        store.grade_intelligence(window, "UP", 0.0, NOW + 10)
    summary = store.intelligence_summary()["neutral"]
    assert summary["n"] == 2, "two windows are two opportunities"
    assert summary["wins"] == 1, "only the UP window won"
