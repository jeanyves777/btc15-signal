"""Intelligence in the execution loop: what it may stop, and when it may not.

THE WIRING. `intelligence_verdict` runs BEFORE the auto trading block and an
authorised VETO sets `rule_match = False` (main.py). `rule_match` is the flag
the trading block is gated on, so a veto closes the block and no order is
submitted. That is the whole mechanism, and these tests hold it in place.

TWO INDEPENDENT THINGS must be true for a veto to bite, checked in two
different places on purpose:

    evidence   the policy was VALIDATED to veto in this cell (`decide`)
    authority  the OPERATOR granted the veto class (`authorise`, two switches)

Neither is lowered here. A policy that has not earned a veto cannot get one
from a switch, and a switch that is off cannot be overridden by evidence.

SESSION-AWARE EVIDENCE. The cell key is `distance · price · momentum` and is
deliberately NOT keyed on session - keying on it fragments cells below the
point where they can speak. But a pooled cell can be carried entirely by other
sessions, which is what happened on 2026-09-23: `bd10-15 · px70-85 · mom5+`
answered "pattern supports the existing decision" at n=431 while its `asia`
slice was 0W-2L, and two UP entries lost back to back on it. An execution
action now requires that THIS session is represented in the cell.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from btc15_signal import intel_mode  # noqa: E402
from btc15_signal import intelligence_policy as intel  # noqa: E402

KEY = "bd10-15 · px70-85 · mom5+|accept"


def policy(*, action="veto", vetoes=True, admissions=False, n=400,
           by_session=None, version="brti-2", fingerprint=None):
    from btc15_signal import feature_contract

    arm = {
        "action": action, "n": n, "markets": n, "days": 30,
        "mean": -0.05, "low": -0.09, "high": -0.01, "delta": 0,
        "probability": 0.62, "gate": None, "promoted": True,
    }
    if by_session is not None:
        arm["by_session"] = by_session
    return intel.Policy(
        version="test-1", model_version="arms-nested-1",
        feature_version=version,
        feature_fingerprint=(feature_contract.FINGERPRINT
                             if fingerprint is None else fingerprint),
        arms={KEY: arm}, vetoes_enabled=vetoes,
        admissions_enabled=admissions, min_evidence=40,
        training_cutoff_ms=0, data_end_ms=0,
    )


def verdict(pol, *, session="asia", base_qualified=True, mode="live",
            authorised=True):
    v = intel.decide(
        context_key=KEY, base_qualified=base_qualified, failed_gates=(),
        ask=0.80, policy=pol, enabled=True, features_ok=True,
        session=session,
    )
    effective, _why = intel_mode.resolve(mode, authorised)
    return intel.authorise(
        v, may_confidence=True,
        may_veto=intel_mode.may_veto(effective),
        may_admit=intel_mode.may_admit(effective),
    )


def submits(v) -> bool:
    """The execution loop, reduced to the line that matters.

    `main` does exactly this before the auto trading block:
        if verdict.final_action == VETO: rule_match = False
    and the block is `if rule.enabled and rule_match and auto_on ...`.
    """
    rule_match = True
    if v.final_action == intel.VETO:
        rule_match = False
    elif v.final_action == intel.ADMIT:
        rule_match = True
    return rule_match


# --------------------------------- an authorised veto prevents submission

def test_an_authorised_veto_prevents_submission():
    support = {"asia": {"n": 200, "wins": 90}}
    v = verdict(policy(by_session=support), mode="live", authorised=True)
    assert v.final_action == intel.VETO
    assert submits(v) is False, "a veto must close the trading block"


def test_the_same_veto_does_nothing_without_authority():
    """Evidence without the operator's switch changes no order."""
    support = {"asia": {"n": 200, "wins": 90}}
    pol = policy(by_session=support)
    for mode, auth in (("shadow", True), ("live", False), ("assist", True)):
        v = verdict(pol, mode=mode, authorised=auth)
        assert v.final_action == intel.NEUTRAL, (mode, auth)
        assert submits(v) is True
        assert v.evidence_action == intel.VETO, "the evidence is still recorded"


def test_authority_without_evidence_vetoes_nothing():
    """A switch cannot manufacture a veto the policy never earned."""
    support = {"asia": {"n": 200, "wins": 90}}
    v = verdict(policy(action="neutral", by_session=support),
                mode="live", authorised=True)
    assert v.final_action == intel.NEUTRAL
    assert submits(v) is True


def test_a_policy_with_vetoes_disabled_cannot_veto():
    support = {"asia": {"n": 200, "wins": 90}}
    v = verdict(policy(vetoes=False, by_session=support),
                mode="live", authorised=True)
    assert v.final_action == intel.NEUTRAL
    assert submits(v) is True


def test_a_veto_never_applies_to_a_setup_the_gates_already_refused():
    """A veto refuses; it has nothing to refuse if the rule already did."""
    support = {"asia": {"n": 200, "wins": 90}}
    v = verdict(policy(by_session=support), base_qualified=False,
                mode="live", authorised=True)
    assert v.final_action == intel.NEUTRAL


def test_no_mode_may_ever_change_size():
    for mode in intel_mode.MODES:
        assert intel_mode.may_change_size(mode) is False


# ------------------------------------------ session-aware evidence handling

def test_a_veto_is_withheld_when_this_session_is_unrepresented():
    """THE 2026-09-23 CASE. n=431 pooled, but asia held almost none of it."""
    thin = {"asia": {"n": 2, "wins": 0}, "us": {"n": 220, "wins": 190},
            "late-us": {"n": 209, "wins": 180}}
    v = verdict(policy(by_session=thin), session="asia",
                mode="live", authorised=True)
    assert v.final_action == intel.NEUTRAL
    assert "does not transfer" in v.reason
    assert "asia" in v.reason


def test_the_same_cell_still_acts_in_a_session_it_has_evidence_for():
    thin = {"asia": {"n": 2, "wins": 0}, "us": {"n": 220, "wins": 190}}
    v = verdict(policy(by_session=thin), session="us",
                mode="live", authorised=True)
    assert v.final_action == intel.VETO
    assert submits(v) is False


def test_session_support_is_counted_at_the_documented_floor():
    at = {"asia": {"n": intel.MIN_SESSION_EVIDENCE, "wins": 10}}
    below = {"asia": {"n": intel.MIN_SESSION_EVIDENCE - 1, "wins": 10}}
    assert verdict(policy(by_session=at), session="asia",
                   mode="live", authorised=True).final_action == intel.VETO
    assert verdict(policy(by_session=below), session="asia",
                   mode="live", authorised=True).final_action == intel.NEUTRAL


def test_the_session_check_never_creates_an_action():
    """It may only withhold. A neutral arm stays neutral however rich the
    session evidence is."""
    rich = {"asia": {"n": 5000, "wins": 4000}}
    v = verdict(policy(action="neutral", by_session=rich), session="asia",
                mode="live", authorised=True)
    assert v.final_action == intel.NEUTRAL
    assert submits(v) is True


# ------------------------- unsupported and incompatible policies fall back

def test_a_policy_without_per_session_evidence_withholds_execution():
    """The documented fallback: an arm that cannot answer the question does
    not get to act on it. Older artefacts keep working for confidence."""
    v = verdict(policy(by_session=None), session="asia",
                mode="live", authorised=True)
    assert v.final_action == intel.NEUTRAL
    assert "predates per-session evidence" in v.reason
    assert submits(v) is True


def test_a_caller_that_supplies_no_session_gets_the_old_contract():
    """The check can only speak when the session is known. Refusing every
    caller that predates the argument would disable the layer, not guard it -
    and the live path does supply it, pinned by a test below."""
    v = verdict(policy(by_session={"asia": {"n": 200, "wins": 90}}),
                session="", mode="live", authorised=True)
    assert v.final_action == intel.VETO
    assert submits(v) is False


def test_a_session_keyed_cell_needs_no_pooling_check():
    """`us · mid · ...` already names its session and cannot be carried by a
    different one, so it acts without per-session counts."""
    from btc15_signal import feature_contract

    arm = {"action": "veto", "n": 400, "markets": 400, "days": 30,
           "mean": -0.05, "low": -0.09, "high": -0.01, "delta": 0,
           "probability": 0.62, "gate": None, "promoted": True}
    key = "us · mid · bd10-15 · px70-85|accept"
    pol = intel.Policy(
        version="t", model_version="m", feature_version="brti-2",
        feature_fingerprint=feature_contract.FINGERPRINT,
        arms={key: arm}, vetoes_enabled=True, admissions_enabled=False,
        min_evidence=40, training_cutoff_ms=0, data_end_ms=0)
    v = intel.decide(context_key=key, base_qualified=True, failed_gates=(),
                     ask=0.80, policy=pol, session="us")
    assert v.final_action == intel.VETO


def test_an_incompatible_fingerprint_falls_back_to_the_base_strategy():
    pol = policy(by_session={"asia": {"n": 200, "wins": 90}},
                 fingerprint="deadbeefdeadbeef")
    v = verdict(pol, mode="live", authorised=True)
    assert v.final_action == intel.NEUTRAL
    assert submits(v) is True, "the base strategy decides, unchanged"


def test_a_retired_feature_family_falls_back():
    pol = policy(by_session={"asia": {"n": 200, "wins": 90}},
                 version="binance-1", fingerprint="deadbeefdeadbeef")
    v = verdict(pol, mode="live", authorised=True)
    assert v.final_action == intel.NEUTRAL
    assert submits(v) is True


def test_an_inactive_policy_falls_back():
    pol = intel.Policy(version="", model_version="", feature_version="",
                       feature_fingerprint="", arms={}, vetoes_enabled=True,
                       admissions_enabled=False, min_evidence=40,
                       training_cutoff_ms=0, data_end_ms=0)
    v = verdict(pol, mode="live", authorised=True)
    assert v.final_action == intel.NEUTRAL
    assert submits(v) is True


def test_thin_overall_evidence_still_falls_back():
    support = {"asia": {"n": 200, "wins": 90}}
    v = verdict(policy(n=5, by_session=support), mode="live", authorised=True)
    assert v.final_action == intel.NEUTRAL
    assert "thin evidence" in v.reason
    assert submits(v) is True


# --------------------------------------------- the wiring itself is pinned

def test_main_applies_the_veto_before_the_trading_block():
    """If these two ever drift apart the layer becomes decorative."""
    import inspect

    from btc15_signal import main

    src = inspect.getsource(main)
    veto_at = src.index("if intel_verdict.final_action == intel.VETO:")
    assert "rule_match = False" in src[veto_at:veto_at + 120]
    block_at = src.index("if rule.enabled and rule_match and auto_on")
    assert veto_at < block_at, "the verdict must be applied BEFORE the block"


def test_main_passes_the_session_to_decide():
    import inspect

    from btc15_signal import main

    src = inspect.getsource(main)
    assert "session=_session(opened)" in src
