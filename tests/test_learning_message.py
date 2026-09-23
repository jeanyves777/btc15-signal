"""`/learning` reports what is watched separately from what may act.

The operator's requirement, in their words: "Report the live integration
separately from whether any candidate has earned permission to change orders."
So the message must never let a candidate that is merely being measured read
as one that is running.
"""

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from btc15_signal import messages  # noqa: E402
from btc15_signal.candidates import CandidateSet  # noqa: E402

NOW = int(time.time() * 1000)
CTX = "us · low · bd5-10 · px<70"


def artefact(tmp_path, promotes=False) -> CandidateSet:
    path = tmp_path / "c.json"
    path.write_text(json.dumps({
        "version": "brti-cand-1", "feature_version": "brti-1",
        "built_ms": NOW, "data_end_ms": NOW, "training_cutoff_ms": NOW - 1,
        "candidates": [{
            "candidate_id": "c01", "context": f"{CTX}|reject",
            "proposed_action": "admit", "train_n": 61, "train_mean": 0.0166,
            "promotes": promotes, "delta": 3,
        }],
    }))
    return CandidateSet.load(path)


def test_a_watched_candidate_is_not_described_as_running(tmp_path):
    out = messages.learning(head="H", candidates=artefact(tmp_path), board=[])
    assert "watching" in out
    assert "PROMOTED" not in out
    assert "<b>0</b> of <b>1</b> may change an order" in out


def test_a_promoted_candidate_says_so(tmp_path):
    out = messages.learning(
        head="H", candidates=artefact(tmp_path, promotes=True), board=[])
    assert "PROMOTED" in out
    assert "<b>1</b> of <b>1</b> may change an order" in out


def test_an_ungraded_candidate_shows_no_forward_number(tmp_path):
    """Zero graded rows means no forward evidence. Printing '+0.0000' there
    would read as a measured result of nothing, which is not the same thing."""
    out = messages.learning(head="H", candidates=artefact(tmp_path), board=[])
    assert "0 graded" in out
    assert "forward" not in out.split("💡")[0].split("may change")[0].split(
        "0 graded")[1]


def test_forward_evidence_is_reported_once_it_exists(tmp_path):
    out = messages.learning(
        head="H", candidates=artefact(tmp_path),
        board=[{"candidate_id": "c01", "graded": 12, "changes": 5,
                "incremental": 0.0431}],
    )
    assert "12 graded" in out and "5 it would change" in out
    assert "+0.0431" in out


def test_simulated_fills_are_labelled(tmp_path):
    """A rejected winner is evidence about direction, not proof a fill was
    available - the message has to say so where the number is shown."""
    out = messages.learning(head="H", candidates=artefact(tmp_path), board=[])
    assert "SIMULATED" in out


def test_no_candidates_says_nothing_is_watched(tmp_path):
    out = messages.learning(head="H", candidates=CandidateSet(), board=[])
    assert "nothing is being watched" in out
    assert "may change an order" not in out


def test_the_artefact_version_is_shown(tmp_path):
    """So a figure can always be traced to the candidate set that produced
    it, and a stale artefact cannot masquerade as the current one."""
    out = messages.learning(head="H", candidates=artefact(tmp_path), board=[])
    assert "brti-cand-1" in out and "brti-1" in out
