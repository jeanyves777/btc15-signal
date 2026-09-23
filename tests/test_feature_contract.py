"""One feature contract, enforced at runtime. A comment is not enforcement.

Two things have gone wrong in this system in exactly this shape, and neither
raised an exception:

  * live keyed contexts on Binance bands while candidates were frozen on
    BRTI bands - the key still formatted, it just named a pocket that no
    artefact contained, so forward evaluation would have recorded nothing
    forever while the logs said "integrated";
  * the deployed policy declared `feature_version: brti-1` over seven arms
    that were entirely Binance.

The fingerprint hashes the DEFINITIONS - source, units, cadence, smoothing,
lookbacks, cutoff rule and every band boundary - so a disagreement about what
a number means is a refusal at runtime rather than a comment someone has to
read.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from btc15_signal import feature_contract as fc  # noqa: E402
from btc15_signal import intelligence_policy as intel  # noqa: E402
from btc15_signal.adaptive import brti_context_of, setup_context_of  # noqa: E402
from btc15_signal.brti import features_from_series  # noqa: E402


def series(n: int, end_ms: int = 1_000_000, start: float = 100_000.0):
    return [(end_ms - (n - 1 - i) * 1000, start + (i % 7) - 3) for i in range(n)]


def setup_key(action: str | None = "accept", *, distance: float = 12.0,
              ask: float = 0.80, momentum: float = 7.0) -> str:
    """The key the live path builds, built by the live path's own function.

    Typing the key as a literal is how this file came to assert against a
    shape the keying function had already left behind: the arms moved to the
    setup - distance, price, momentum - while four call sites went on passing
    the session-first string, so `decide` found no arm and returned neutral
    for a reason that had nothing to do with the guard under test.
    """
    ctx = setup_context_of({
        "brti_normalized_distance": distance,
        "our_ask": ask,
        "brti_aligned_momentum_bps": momentum,
    })
    return f"{ctx}|{action}" if action else str(ctx)



def brti_policy(**over) -> intel.Policy:
    base = {
        "version": "v2", "model_version": "m",
        "feature_version": fc.CONTRACT.version,
        "arms": {setup_key():
                 {"n": 500, "mean": -0.05, "low": -0.09, "high": -0.01,
                  "action": intel.VETO, "delta": -5}},
        "vetoes_enabled": True, "min_evidence": 1,
        "feature_fingerprint": fc.FINGERPRINT,
    }
    base.update(over)
    return intel.Policy(**base)


# ------------------------------------------------------- the fingerprint

def test_the_fingerprint_is_stable_across_calls():
    assert fc.CONTRACT.fingerprint() == fc.CONTRACT.fingerprint() == fc.FINGERPRINT


def test_changing_a_lookback_changes_the_fingerprint():
    """A policy fitted under 300s and applied under 600s agrees on every
    NAME and is still arithmetic over a different quantity."""
    other = fc.FeatureContract(momentum_window_s=600)
    assert other.fingerprint() != fc.FINGERPRINT


def test_changing_a_band_boundary_changes_the_fingerprint():
    moved = fc.FeatureContract(
        distance_bands=((0.0, 6.0, "bd<5"), (6.0, 10.0, "bd5-10"),
                        (10.0, 15.0, "bd10-15"), (15.0, 999.0, "bd15+")))
    assert moved.fingerprint() != fc.FINGERPRINT


def test_changing_the_cutoff_rule_changes_the_fingerprint():
    assert fc.FeatureContract(cutoff_rule="t < decision_ms").fingerprint() \
        != fc.FINGERPRINT


def test_an_unknown_fingerprint_is_not_compatible():
    """Cannot be shown to match is the same as must not act."""
    assert not fc.compatible(None)
    assert not fc.compatible("")
    assert fc.compatible(fc.FINGERPRINT)


def test_the_contract_names_no_binance_endpoint():
    payload = repr(fc.CONTRACT.payload()).lower()
    assert "binance" not in payload
    assert "kalshi" in payload
    assert fc.CONTRACT.family == "brti"


# ---------------------------------------------- enforcement, not comments

def test_a_policy_without_a_fingerprint_cannot_act():
    verdict = intel.decide(
        context_key=setup_key(), base_qualified=True,
        failed_gates=(), ask=0.80, policy=brti_policy(feature_fingerprint=""),
    )
    assert verdict.final_action == intel.NEUTRAL
    assert "feature contract" in verdict.reason


def test_a_policy_with_a_stale_fingerprint_cannot_act():
    verdict = intel.decide(
        context_key=setup_key(), base_qualified=True,
        failed_gates=(), ask=0.80,
        policy=brti_policy(feature_fingerprint="0000000000000000"),
    )
    assert verdict.final_action == intel.NEUTRAL
    assert "feature contract" in verdict.reason


def test_the_mismatch_reason_names_a_concrete_difference():
    """A fingerprint says no; the operator needs to know which field."""
    stale_defs = fc.FeatureContract(momentum_window_s=600).payload()
    verdict = intel.decide(
        context_key=setup_key(), base_qualified=True,
        failed_gates=(), ask=0.80,
        policy=brti_policy(feature_fingerprint="0000000000000000",
                           feature_definitions=stale_defs),
    )
    assert "momentum_window_s" in verdict.reason


def test_a_matching_policy_is_allowed_to_act():
    """The guard must bar mismatches, not all intelligence."""
    verdict = intel.decide(
        context_key=setup_key(), base_qualified=True,
        failed_gates=(), ask=0.80, policy=brti_policy(),
    )
    assert verdict.final_action == intel.VETO


# -------------------------------- identical snapshots through both paths

def test_replay_and_live_agree_on_an_identical_snapshot():
    """Same series, same instant, same target - the two call sites must
    produce byte-identical features and the same context key."""
    points = series(900)
    target = 99_990.0
    live = features_from_series("E", points, target, points[-1][0])
    # The replay path truncates a longer series to the same instant, which is
    # what `backfill_brti.py` does.
    longer = points + [(points[-1][0] + i * 1000, 100_100.0) for i in range(1, 60)]
    replay_input = [(t, v) for t, v in longer if t <= points[-1][0]]
    replay = features_from_series("E", replay_input, target, points[-1][0])
    for field in ("brti_volatility_bps", "brti_momentum_bps",
                  "brti_normalized_distance", "signed_distance_bps", "value"):
        assert getattr(live, field) == getattr(replay, field), field
    row = {"session": "us", "brti_volatility_bps": live.brti_volatility_bps,
           "brti_normalized_distance": live.brti_normalized_distance,
           "our_ask": 0.80}
    assert str(brti_context_of(row)) == str(brti_context_of(dict(row)))


def test_future_samples_cannot_change_an_earlier_decision():
    """The cutoff is strict `t <= decision_ms`. If a later print could move
    an earlier decision, every replayed result would be unreproducible and
    every backtest would quietly contain hindsight."""
    points = series(900)
    cutoff = points[-1][0]
    before = features_from_series("E", points, 99_990.0, cutoff)
    # A violent move AFTER the decision instant.
    future = points + [(cutoff + i * 1000, 150_000.0) for i in range(1, 300)]
    truncated = [(t, v) for t, v in future if t <= cutoff]
    after = features_from_series("E", truncated, 99_990.0, cutoff)
    for field in ("brti_volatility_bps", "brti_momentum_bps",
                  "brti_normalized_distance", "value"):
        assert getattr(before, field) == getattr(after, field), field


def test_a_sample_exactly_at_the_cutoff_is_included():
    """`t <= decision_ms`, not `<`. Off-by-one here silently drops the most
    important observation in the window."""
    points = series(900)
    cutoff = points[-1][0]
    kept = [(t, v) for t, v in points if t <= cutoff]
    assert kept[-1][0] == cutoff
    assert features_from_series("E", kept, 99_990.0, cutoff).value == points[-1][1]


# ------------------------------------------------- the deployed artefacts

def test_the_deployed_candidates_carry_the_live_fingerprint():
    from btc15_signal.candidates import CandidateSet

    path = Path(__file__).resolve().parents[1] / "runtime" / "intelligence_candidates.json"
    if not path.exists():
        return
    cs = CandidateSet.load(path)
    assert cs.compatible, "frozen candidates must match the live contract"


def test_incompatible_candidates_evaluate_nothing():
    from btc15_signal.candidates import Candidate, CandidateSet

    cs = CandidateSet(
        version="v", feature_version=fc.CONTRACT.version,
        feature_fingerprint="stale",
        candidates=(Candidate(candidate_id="c01", context=setup_key(ask=0.50),
                              proposed_action=intel.VETO, train_n=500,
                              train_mean=-0.05, promotes=False),),
    )
    assert not cs.compatible
    assert cs.evaluate(context_key=setup_key(None, ask=0.50), qualified=True) == []
