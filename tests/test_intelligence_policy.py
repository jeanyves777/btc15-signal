"""The adaptive layer, on the real decision path, with its limits enforced.

The operator's required behaviours, each as a test. The ones that matter most
are not "can it act" but "can it be stopped" - a learning layer that cannot be
bounded is a liability whatever its accuracy.
"""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from btc15_signal import feature_contract
from btc15_signal import intelligence_policy as intel  # noqa: E402
from btc15_signal import messages  # noqa: E402
from btc15_signal.adaptive import context_of  # noqa: E402
from btc15_signal.store import Store  # noqa: E402

NOW = int(time.time() * 1000)
# BRTI bands, because the policy under test declares `brti-1`. These were
# Binance keys (`dist3+`) under a brti-1 label - the same mislabelling the
# deployed artefact carried, reproduced in the fixtures. The runtime guard
# now catches it, so the fixtures had to become honest rather than the
# guard lenient.
KEY = "us · mid · bd10-15 · px70-85|accept"
REJECT_KEY = "us · mid · bd10-15 · px<70|reject"


def policy(**overrides) -> intel.Policy:
    base = intel.Policy(
        version="test-1", model_version="m1",
        feature_version=intel.FEATURE_VERSION, training_cutoff_ms=NOW - 1_000,
        min_evidence=100,
        arms={
            KEY: {"n": 400, "mean": -0.05, "low": -0.09, "high": -0.01,
                  "action": intel.VETO, "gate": None, "delta": -10},
            REJECT_KEY: {"n": 400, "mean": 0.05, "low": 0.01, "high": 0.09,
                         "action": intel.ADMIT, "gate": "contract price band",
                         "delta": 8},
        },
        vetoes_enabled=True, admissions_enabled=True,
        # The fit's DEFINITIONS, not just their name. Without this the
        # runtime contract check refuses the policy - which is the point of
        # the check, so the fixture declares it rather than the guard
        # relaxing to accommodate fixtures.
        feature_fingerprint=feature_contract.FINGERPRINT,
    )
    for k, v in overrides.items():
        setattr(base, k, v)
    return base


def decide(**overrides):
    kwargs = {
        "context_key": KEY, "base_qualified": True, "failed_gates": (),
        "ask": 0.80, "policy": policy(), "enabled": True, "features_ok": True,
        "now_ms": NOW, "max_age_ms": 30 * 86_400_000,
    }
    kwargs.update(overrides)
    return intel.decide(**kwargs)


# ------------------------------------------------------- it can act

def test_intelligence_can_veto_a_qualified_setup():
    v = decide()
    assert v.final_action == intel.VETO
    assert v.qualifies is False, "the final answer must refuse the trade"
    assert "adverse pattern" in v.reason


def test_a_learned_exception_admits_only_the_named_gate():
    admitted = decide(
        context_key=REJECT_KEY, base_qualified=False,
        failed_gates=("contract price band",), ask=0.60,
    )
    assert admitted.final_action == intel.ADMIT
    assert admitted.qualifies is True
    assert admitted.overrides_gate == "contract price band"

    # A setup refused for a DIFFERENT gate is not covered by this exception.
    other = decide(
        context_key=REJECT_KEY, base_qualified=False,
        failed_gates=("target distance",), ask=0.60,
    )
    assert other.final_action == intel.NEUTRAL
    assert other.qualifies is False

    # Nor is one refused for the named gate AND another.
    both = decide(
        context_key=REJECT_KEY, base_qualified=False,
        failed_gates=("contract price band", "target distance"), ask=0.60,
    )
    assert both.final_action == intel.NEUTRAL


def test_an_admission_cannot_fire_on_an_already_qualified_setup():
    v = decide(context_key=REJECT_KEY, base_qualified=True, failed_gates=())
    assert v.final_action == intel.NEUTRAL


def test_a_veto_cannot_fire_on_a_setup_that_was_already_refused():
    v = decide(base_qualified=False, failed_gates=("target distance",))
    assert v.final_action == intel.NEUTRAL


# ---------------------------------------------------- it can be stopped

def test_disabled_intelligence_preserves_baseline_behaviour():
    v = decide(enabled=False)
    assert v.final_action == intel.NEUTRAL
    assert v.qualifies is True, "the base verdict is untouched"
    assert "disabled" in v.reason


def test_policy_switches_gate_each_power_independently():
    v = decide(policy=policy(vetoes_enabled=False))
    assert v.final_action == intel.NEUTRAL
    v = decide(
        context_key=REJECT_KEY, base_qualified=False,
        failed_gates=("contract price band",),
        policy=policy(admissions_enabled=False),
    )
    assert v.final_action == intel.NEUTRAL


def test_missing_features_fall_back_explicitly():
    v = decide(features_ok=False)
    assert v.final_action == intel.NEUTRAL
    assert "features unavailable" in v.reason


def test_a_stale_policy_is_not_applied():
    old = policy(training_cutoff_ms=NOW - (400 * 86_400_000))
    v = decide(policy=old)
    assert v.final_action == intel.NEUTRAL
    assert "stale" in v.reason


def test_an_incompatible_feature_version_is_refused():
    """Declared `binance-0` over brti-1 keys. It is refused, and the reason
    names BOTH so the disagreement is diagnosable rather than just fatal."""
    v = decide(policy=policy(feature_version="binance-0"))
    assert v.final_action == intel.NEUTRAL
    assert "binance-0" in v.reason
    assert feature_contract.CONTRACT.version in v.reason


def test_a_consistently_labelled_but_unsupported_version_is_also_refused():
    """The pure version-mismatch path: the label and the keys agree with each
    other and both disagree with what this build computes. Nothing here is
    mislabelled, so the mislabelling guard must NOT be what catches it.

    THE VERSION IS DERIVED, not typed. This test was written with the literal
    `brti-2` standing in for "some future scheme"; the build then became
    `brti-2` and the fixture quietly turned into a MATCHING policy, so the
    guard under test stopped being reached and the failure surfaced somewhere
    else entirely. Suffixing the live version cannot collide with it.
    """
    unsupported_version = feature_contract.CONTRACT.version + "-unsupported"
    assert unsupported_version != intel.FEATURE_VERSION
    unsupported = intel.Policy(
        version="v9", model_version="m", feature_version=unsupported_version,
        arms={"us · mid · bd10-15 · px70-85|accept":
              {"n": 500, "mean": -0.05, "low": -0.09, "high": -0.01,
               "action": intel.VETO, "delta": -5}},
        vetoes_enabled=True, min_evidence=1,
    )
    assert not unsupported.mislabelled
    v = decide(policy=unsupported)
    assert v.final_action == intel.NEUTRAL
    assert unsupported_version in v.reason
    assert intel.FEATURE_VERSION in v.reason


def test_thin_evidence_is_neutral():
    thin = policy()
    thin.arms[KEY] = {**thin.arms[KEY], "n": 12}
    v = decide(policy=thin)
    assert v.final_action == intel.NEUTRAL
    assert "thin evidence" in v.reason


def test_an_unknown_context_is_neutral():
    v = decide(context_key="mars · ? · ? · ?|accept")
    assert v.final_action == intel.NEUTRAL
    assert "no evidence" in v.reason


def test_an_empty_policy_is_neutral():
    v = decide(policy=intel.Policy())
    assert v.final_action == intel.NEUTRAL
    assert v.qualifies is True


# --------------------------------------------- price changes re-evaluate

def test_an_approval_at_one_price_is_not_approval_at_a_worse_one():
    admitted = decide(
        context_key=REJECT_KEY, base_qualified=False,
        failed_gates=("contract price band",), ask=0.60,
    )
    assert admitted.final_action == intel.ADMIT
    withdrawn = intel.reprice(admitted, ask=0.97)
    assert withdrawn.final_action == intel.NEUTRAL
    assert "submission" in withdrawn.reason


def test_repricing_leaves_a_veto_alone():
    v = decide()
    assert intel.reprice(v, ask=0.99).final_action == intel.VETO


# ------------------------------------------------- sparse groups shrink

def test_a_sparse_group_shrinks_toward_the_broader_one():
    prior = 0.02
    bold = intel.shrink(0.50, n=5, prior=prior)
    settled = intel.shrink(0.50, n=5_000, prior=prior)
    assert bold < 0.10, "five observations must not produce a bold estimate"
    assert settled > 0.45, "a large sample speaks mostly for itself"


def test_shrinkage_of_nothing_is_the_prior():
    assert intel.shrink(9.9, n=0, prior=0.03) == 0.03


# ------------------------------------------------------------ recording

def test_every_decision_is_recorded_even_when_neutral(tmp_path):
    store = Store(str(tmp_path / "s.db"))
    store.record_intelligence({
        "window_open": 1, "decided_ms": NOW, "base_qualified": 1,
        "final_action": intel.NEUTRAL, "reason": "no evidence",
        "confidence_delta": 0, "policy_version": "test-1",
    })
    rows = store._dicts("SELECT * FROM intelligence_decisions")
    assert len(rows) == 1
    assert rows[0]["final_action"] == intel.NEUTRAL


def test_vetoed_and_rejected_signals_are_still_graded(tmp_path):
    """They are the counterfactuals. Grading only what traded would leave the
    evidence for 'did it help' permanently unavailable."""
    store = Store(str(tmp_path / "s.db"))
    for action in (intel.NEUTRAL, intel.VETO):
        store.record_intelligence({
            "window_open": 7, "decided_ms": NOW, "base_qualified": 1,
            "final_action": action, "confidence_delta": 0, "side": "UP",
        })
    store.grade_intelligence(7, "UP", pnl=0.2, now_ms=NOW + 1)
    rows = store._dicts("SELECT * FROM intelligence_decisions WHERE window_open=7")
    assert all(r["won"] == 1 for r in rows)
    assert all(r["graded_ms"] for r in rows)


def test_grading_is_not_applied_twice(tmp_path):
    store = Store(str(tmp_path / "s.db"))
    store.record_intelligence({
        "window_open": 9, "decided_ms": NOW, "base_qualified": 1,
        "final_action": intel.NEUTRAL, "confidence_delta": 0, "side": "UP",
    })
    store.grade_intelligence(9, "UP", 0.2, NOW + 1)
    store.grade_intelligence(9, "DOWN", -9.9, NOW + 2)
    row = store._dicts("SELECT * FROM intelligence_decisions WHERE window_open=9")[0]
    assert row["won"] == 1 and row["realised_pnl"] == 0.2


def test_a_losing_side_grades_as_a_loss(tmp_path):
    """The defect this pins: the settlement loop had only a market-level flag,
    and the one it could form - "did the winning side win?" - is true by
    construction. Every graded row came back a winner, 21 for 21, which is
    not a record any 15-minute strategy has ever had."""
    store = Store(str(tmp_path / "s.db"))
    for side in ("UP", "DOWN"):
        store.record_intelligence({
            "window_open": 11, "decided_ms": NOW, "base_qualified": 1,
            "final_action": intel.NEUTRAL, "confidence_delta": 0, "side": side,
        })
    store.grade_intelligence(11, "UP", 0.0, NOW + 1)
    graded = {r["side"]: r["won"] for r in store._dicts(
        "SELECT side, won FROM intelligence_decisions WHERE window_open=11")}
    assert graded == {"UP": 1, "DOWN": 0}


# ------------------------------------------------------ no hindsight

def test_the_context_key_uses_only_features_available_at_the_time():
    row = {"session": "us", "vol_regime": "mid", "normalized_distance": 3.4,
           "our_ask": 0.80, "won": 1, "rule_match": 1}
    key = str(context_of(row))
    assert "won" not in key and "1" not in key.replace("bd10-15", "")
    # Removing the outcome must not change the context.
    row.pop("won")
    assert str(context_of(row)) == key


# --------------------------------------------------------- the message

def test_telegram_stays_silent_when_nothing_changed():
    assert messages.policy_line(decide(enabled=False)) == ""


def test_telegram_explains_a_veto():
    text = messages.policy_line(decide())
    assert "Entry declined" in text
    assert "adverse pattern" in text


def test_telegram_explains_an_admission():
    v = decide(
        context_key=REJECT_KEY, base_qualified=False,
        failed_gates=("contract price band",), ask=0.60,
    )
    text = messages.policy_line(v)
    assert "Entry allowed" in text
    assert "contract price band" in text
