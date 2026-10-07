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
from btc15_signal import feature_contract  # noqa: E402


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


def test_the_feature_version_has_exactly_one_definition():
    """It drifted twice. `intelligence_policy` records the first - a constant
    left "at brti-1 when the contract moved to brti-2" - and on 2026-09-25
    `adaptive.SETUP_FEATURE_VERSION` stayed at brti-2 when the contract went to
    brti-3. `learning_data` filters rows on that exact value, so it silently
    discarded every row written under the running version: `live_actual_fills`
    read 0 out of rows that were all present.

    One definition, and the contract reads it.
    """
    from btc15_signal import feature_contract
    from btc15_signal.adaptive import SETUP_FEATURE_VERSION
    assert feature_contract.CONTRACT.version == SETUP_FEATURE_VERSION


def test_no_module_restates_the_version_as_a_literal():
    """A second literal is how it drifted both times. Comments may mention an
    old version; assignments may not."""
    import re
    from pathlib import Path
    src = Path(__file__).resolve().parents[1] / "src" / "btc15_signal"
    offenders = []
    for path in src.glob("*.py"):
        if path.name == "adaptive.py":
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            code = line.split("#", 1)[0]
            if re.search(r'=\s*["\']brti-\d["\']', code):
                offenders.append(f"{path.name}: {line.strip()}")
    assert not offenders, offenders


# --------------------------------------------------------------- the freeze

# FROZEN 2026-09-27 BY OPERATOR DECISION. These two literals are the ONLY
# place in the repo that restates the contract's identity, and they exist to
# make changing it deliberate rather than possible.
#
# WHAT A CHANGE COSTS, measured rather than asserted. `learning_data` excludes
# any live decision whose `feature_version` is not the current one - correctly,
# because a decision taken under other definitions describes a cell that does
# not exist under these. The consequence is that changing the contract DELETES
# THE LIVE EVIDENCE BASE. On BTC that was 6,395 of 8,982 decisions, 71%.
#
# It had happened four times in three days when this was frozen:
#
#     brti-1   0.7 days | brti-2  1.9 days | brti-3  34 MINUTES | brti-4  1.1 days
#
# leaving 177 qualified signals across seven live instruments, none older than
# 1.1 days, and silver with zero. Every tuning decision, every confidence arm
# and every hypothesis the local model can reason over is drawn from that
# window, so the contract moving is the single largest limit on the system
# learning anything at all. See FINDINGS 90.
#
# TO CHANGE IT ANYWAY - which is allowed, and sometimes right:
#   1. change the contract (or the arithmetic in brti.features_from_series),
#   2. give it a NEW version, update the literals below, APPEND the new pair
#      to CONTRACT_HISTORY and re-record GOLDEN_* - all in the same commit,
#   3. record in FINDINGS what was reset and why it was worth the reset.
# Step 2 is the whole mechanism. It cannot be satisfied by accident, and it
# puts this comment in front of whoever does it.
FROZEN_FINGERPRINT = "a641ab8e2a05aa44"
FROZEN_VERSION = "brti-4"
# The payload the fingerprint was taken over, so a failure can say WHICH
# field moved instead of only that the hash differs.
FROZEN_PAYLOAD = (
    '{"context_recorded": ["session", "vol_regime", "band_hold_s", '
    '"remaining_s", "momentum_45m", "volatility_45m"], '
    '"cutoff_rule": "t <= decision_ms", '
    '"distance_bands": [[0.0, 5.0, "bd<5"], [5.0, 10.0, "bd5-10"], '
    '[10.0, 15.0, "bd10-15"], [15.0, 999.0, "bd15+"]], '
    '"family": "brti", "key_dimensions": ["distance", "price", "momentum"], '
    '"level_window_s": 2700, '
    '"momentum_bands": [[-9000000000.0, 0.0, "mom<=0"], [0.0, 5.0, "mom0-5"], '
    '[5.0, 9000000000.0, "mom5+"]], '
    '"momentum_window_s": 300, '
    '"price_bands": [[0.0, 0.7, "px<70"], [0.7, 0.85, "px70-85"], '
    '[0.85, 0.9301, "px85-93"], [0.9301, 1.0, "px93+"]], '
    '"price_source": "kalshi:orderbook", "sampling_cadence_s": 1, '
    '"smoothing": "none added; BRTI is a published trailing 60s mean", '
    '"source": "kalshi:/live_data/events/{event} + /cfbenchmarks/values", '
    '"units": "bps; normalized_distance = |signed_bps| / volatility_bps", '
    '"version": "brti-4", '
    '"vol_bands": [[0.0, 0.5, "low"], [0.5, 1.5, "mid"], '
    '[1.5, 9000000000.0, "high"]], '
    '"volatility_window_s": 300}'
)
# APPEND-ONLY. Every (version, fingerprint) the system has run under. The
# version string is what `learning_data` filters evidence on, so a new
# fingerprint re-pinned under an OLD version would pool two arithmetics
# instead of resetting. One line per version, never edited.
CONTRACT_HISTORY = [
    ("brti-4", "a641ab8e2a05aa44"),
]


def _freeze_message(contract) -> str:
    import json
    moved = contract.differences(json.loads(FROZEN_PAYLOAD))
    return (
        f"THE FEATURE CONTRACT HAS CHANGED.\n"
        f"  frozen:  {FROZEN_FINGERPRINT}\n"
        f"  current: {contract.fingerprint()}\n"
        f"  moved:   {'; '.join(moved) or '(payload equal - hash function changed?)'}\n\n"
        f"This deletes every live decision recorded under the old contract - "
        f"71% of them last time - and resets the evidence every instrument "
        f"learns from to zero. If that is intended, follow the three steps "
        f"above FROZEN_FINGERPRINT in the same commit."
    )


def test_the_feature_contract_is_frozen():
    """The contract's identity is pinned, not merely self-consistent.

    Every other test here checks the MECHANISM - that a changed lookback
    changes the hash, that a mismatch is refused. All of them keep passing
    when the contract changes, because they compare the contract to itself.
    This one compares it to a literal, so a change has to be declared.
    """
    assert fc.FINGERPRINT == FROZEN_FINGERPRINT, _freeze_message(fc.CONTRACT)


def test_the_frozen_payload_is_the_one_that_was_hashed():
    """Three literals that could drift apart; this keeps them one fact."""
    import hashlib
    import json
    blob = json.dumps(json.loads(FROZEN_PAYLOAD), sort_keys=True,
                      separators=(",", ":"))
    assert hashlib.sha256(blob.encode()).hexdigest()[:16] == FROZEN_FINGERPRINT
    assert json.loads(FROZEN_PAYLOAD)["version"] == FROZEN_VERSION


def test_a_new_fingerprint_needs_a_new_version():
    """Re-pinning a changed contract under the same version name would let
    `learning_data` pool rows computed two different ways. The history makes
    that visible: each version appears once, each fingerprint once, and the
    frozen pair is the last line."""
    versions = [v for v, _ in CONTRACT_HISTORY]
    prints = [f for _, f in CONTRACT_HISTORY]
    assert len(set(versions)) == len(versions), "a version was reused"
    assert len(set(prints)) == len(prints), "a fingerprint was reused"
    assert CONTRACT_HISTORY[-1] == (FROZEN_VERSION, FROZEN_FINGERPRINT), (
        "the frozen pair must be the newest history line - append it, do not "
        "edit an old one")


def test_the_freeze_message_names_the_field_that_moved():
    """The message was dead code once: it called a method that does not exist,
    so the pin would have fired as an AttributeError and never printed the
    warning it exists for. Render it."""
    moved = fc.FeatureContract(
        momentum_window_s=fc.CONTRACT.momentum_window_s + 1)
    message = _freeze_message(moved)
    assert "momentum_window_s" in message
    assert moved.fingerprint() in message


def test_the_frozen_version_matches_the_contract():
    assert fc.CONTRACT.version == FROZEN_VERSION, (
        f"feature version moved from {FROZEN_VERSION} to "
        f"{fc.CONTRACT.version} without the freeze being updated"
    )


def test_the_freeze_would_actually_catch_a_change():
    """A guard nobody has seen fail is a guard nobody knows works.

    FINDINGS 92 records a gate whose thresholds no input could fail being
    counted as eight passed checks. So this builds a contract that differs in
    one lookback and asserts the pin rejects it.
    """
    # DERIVED FROM THE LIVE VALUE, never a literal. A hardcoded 301 passed
    # while the contract said 300 and then FAILED under a mutation test that
    # set the contract to 301 - the guard's own check collided with the thing
    # it was checking. Deriving the perturbation makes that impossible.
    moved = fc.FeatureContract(
        momentum_window_s=fc.CONTRACT.momentum_window_s + 1)
    assert moved.fingerprint() != fc.FINGERPRINT
    assert fc.compatible(moved.fingerprint()) is False


# ------------------------------------------------- the freeze covers the maths
#
# The fingerprint hashes the contract's DECLARATIONS. The numbers the gates
# and arms read are computed in brti.features_from_series, which never reads
# the contract: its windows are its own default arguments, and the volatility
# estimator (pstdev x sqrt(n)), the 1e-9 denominator floor, the rejection
# threshold and the 45-minute context are code, not fields. An adversarial
# probe on 2026-09-27 changed each of them - removing sqrt(n) moved 80% of
# brti-4 rows to another band, a 10x floor change took admission from 34% to
# 96%, a 3600s level window re-based held/rejections/accel - and the
# fingerprint stayed a641ab8e2a05aa44 with every test passing.
#
# So the arithmetic is pinned directly, on fixed series, to exact numbers.
# These do not describe what the features SHOULD be; they record what brti-4
# IS, so that changing it is a declared act like changing the contract.

GOLDEN_END_MS = 1_790_000_000_000


def _golden_wavy():
    """An hour of 1s prints: drift, a slow swing and a fast wobble, so the
    level lookback sees approaches and turn-backs and every feature is live."""
    import math
    out = []
    for i in range(3600):
        t = GOLDEN_END_MS - (3599 - i) * 1000
        v = 65_000 * (1 + 0.0009 * math.sin(i / 211.0)
                      + 0.00025 * math.sin(i / 17.0)
                      + 0.0000004 * i)
        out.append((t, round(v, 2)))
    return out


# Recorded from brti-4 on 2026-09-27. Re-record ONLY with a new version.
GOLDEN_WAVY = {
    "target": 65099.53,
    "value": 65021.25,
    "signed_distance_bps": -12.024664386978134,
    "brti_momentum_bps": -8.922957400292475,
    "brti_volatility_bps": 1.8088401865936568,
    "brti_normalized_distance": 6.647720719663218,
    "samples": 3600,
    "span_ms": 3599000,
    "brti_retrace": 0.0,
    "brti_choppiness": 0.8592074676085394,
    "brti_rsi": 40.682996020179395,
    "brti_accel": 6.238022430643531,
    "brti_held_s": 340.0,
    "brti_rejections": 5,
    "brti_momentum_45m_bps": 6.520580225453099,
    "brti_volatility_45m_bps": 5.637850941506642,
}

# A dead-flat series has zero volatility, so the normalized distance IS the
# denominator floor at work: |1.5387 bps| / 1e-9.
GOLDEN_FLAT = {
    "signed_distance_bps": 1.5386982612719535,
    "brti_volatility_bps": 0.0,
    "brti_normalized_distance": 1538698261.2719533,
    "brti_held_s": 599.0,
    "brti_rejections": 0,
}


def _assert_golden(got, expected):
    import pytest
    moved = []
    for name, want in expected.items():
        have = getattr(got, name)
        if have != pytest.approx(want, rel=1e-9, abs=1e-9):
            moved.append(f"{name}: brti-4 {want!r} -> now {have!r}")
    assert not moved, (
        "THE FEATURE ARITHMETIC HAS CHANGED under an unchanged contract.\n  "
        + "\n  ".join(moved)
        + "\nEvery keyed arm and gate reads these numbers. Treat this exactly "
          "like a contract change: new version, new golden values, FINDINGS.")


def test_the_feature_arithmetic_is_frozen():
    s = _golden_wavy()
    target = s[3600 - 900][1]
    assert target == GOLDEN_WAVY["target"], "the golden series itself moved"
    got = features_from_series("KXBTCD-GOLDEN", s, target, GOLDEN_END_MS)
    _assert_golden(got, GOLDEN_WAVY)


def test_the_distance_denominator_floor_is_frozen():
    flat = [(GOLDEN_END_MS - (599 - i) * 1000, 65_000.0) for i in range(600)]
    got = features_from_series("KXBTCD-FLAT", flat, 64_990.0, GOLDEN_END_MS)
    _assert_golden(got, GOLDEN_FLAT)


def test_the_golden_check_would_catch_a_changed_estimator():
    """Seen to fail: the same series with a different window must NOT match,
    or the golden numbers pin nothing."""
    s = _golden_wavy()
    got = features_from_series("KXBTCD-GOLDEN", s, s[2700][1], GOLDEN_END_MS,
                               level_window_s=fc.CONTRACT.level_window_s + 900)
    import pytest
    with pytest.raises(AssertionError, match="brti_rejections|_45m_bps"):
        _assert_golden(got, GOLDEN_WAVY)


def test_the_feature_function_defaults_are_the_contracts_windows():
    """brti.py states each window a SECOND time, as a default argument, and
    that default - not the contract field - is what the live path runs. Held
    equal here, derived from the contract, never typed as a literal."""
    import inspect
    params = inspect.signature(features_from_series).parameters
    for name in ("momentum_window_s", "volatility_window_s", "level_window_s"):
        assert params[name].default == getattr(fc.CONTRACT, name), (
            f"brti.features_from_series({name}={params[name].default}) but the "
            f"contract says {getattr(fc.CONTRACT, name)} - the live features "
            f"and the fingerprint now describe different windows")


def test_no_live_call_site_overrides_a_contracted_window():
    """The windows are parameters, so a caller could re-base the live features
    while the hash certifies the defaults. No module in src/ may pass one."""
    import re
    from pathlib import Path
    src = Path(__file__).resolve().parents[1] / "src" / "btc15_signal"
    pattern = re.compile(
        r"\b(momentum_window_s|volatility_window_s|level_window_s)\s*=")
    offenders = []
    for path in src.glob("*.py"):
        if path.name == "feature_contract.py":
            continue
        text = path.read_text(encoding="utf-8")
        if path.name == "brti.py":
            # The signature is the one place these names are assigned.
            start = text.index("def features_from_series(")
            end = text.index(") -> BRTIFeatures | None:", start)
            text = text[:start] + text[end:]
        for line in text.splitlines():
            code = line.split("#", 1)[0]
            if pattern.search(code):
                offenders.append(f"{path.name}: {line.strip()}")
    assert not offenders, offenders
