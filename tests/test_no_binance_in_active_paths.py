"""Binance is out of every ACTIVE path. These tests are the enforcement.

The operator's instruction: remove Binance from all active signal,
intelligence, training, evaluation and execution paths; use Kalshi market
data and Kalshi-provided BRTI; retire the Binance-trained policy and do not
feed it renamed BRTI features; keep old Binance records as labelled history,
excluded from active learning; and where Kalshi data is unavailable, record
that explicitly rather than falling back.

The failure these guard against is not an exception. It is a Binance number
arriving under a BRTI name and every downstream calculation proceeding
normally - which had already happened: the deployed `v1` policy declared
`feature_version: brti-1` over seven arms keyed on Binance bands.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from btc15_signal import feature_contract
from btc15_signal import intelligence_policy as intel  # noqa: E402
from btc15_signal.adaptive import brti_context_of  # noqa: E402

RUNTIME = Path(__file__).resolve().parents[1] / "runtime"


# ------------------------------------------- the mislabelling that happened

def binance_policy(**over) -> intel.Policy:
    base = {
        "version": "v1", "model_version": "m", "feature_version": "brti-1",
        "arms": {"asia · low · dist3+ · px<70|reject":
                 {"n": 500, "mean": 0.05, "low": 0.01, "high": 0.09,
                  "action": intel.ADMIT, "delta": 5}},
        "vetoes_enabled": True, "admissions_enabled": True, "min_evidence": 1,
        "feature_fingerprint": feature_contract.FINGERPRINT,
    }
    base.update(over)
    return intel.Policy(**base)


def test_provenance_is_read_from_the_keys_not_the_label():
    """The declared field is a claim; the arm keys are the evidence."""
    pol = binance_policy()
    assert pol.feature_version == "brti-1"        # what it says
    assert pol.keyed_feature_version == "binance-1"  # what it is
    assert pol.mislabelled


def test_a_binance_policy_wearing_a_brti_label_cannot_act():
    """This is the exact artefact that was deployed. With both action flags
    on it would have admitted trades using Binance-trained arms."""
    verdict = intel.decide(
        context_key="asia · low · dist3+ · px<70", base_qualified=False,
        failed_gates=(), ask=0.80, policy=binance_policy(),
    )
    assert verdict.final_action == intel.NEUTRAL
    assert "keyed on" in verdict.reason
    assert verdict.qualifies is False, "the base decision must stand unchanged"


def test_an_honestly_labelled_binance_policy_is_also_barred():
    """Relabelling it truthfully must not make it usable - it is retired."""
    verdict = intel.decide(
        context_key="asia · low · dist3+ · px<70", base_qualified=False,
        failed_gates=(), ask=0.80,
        policy=binance_policy(feature_version="binance-1"),
    )
    assert verdict.final_action == intel.NEUTRAL
    assert "retired" in verdict.reason


def test_a_brti_policy_is_not_barred_by_these_guards():
    """The guard must bar the retired instrument, not all intelligence."""
    pol = intel.Policy(
        version="v2", model_version="m", feature_version="brti-1",
        arms={"asia · low · bd10-15 · px<70":
              {"n": 500, "mean": 0.05, "low": 0.01, "high": 0.09,
               "action": intel.ADMIT, "gate": "target distance", "delta": 5}},
        vetoes_enabled=True, admissions_enabled=True, min_evidence=1,
        feature_fingerprint=feature_contract.FINGERPRINT,
    )
    assert pol.keyed_feature_version == "brti-1"
    assert not pol.mislabelled
    # An admission may only rescue a setup refused for the ONE gate it names,
    # so the call has to present exactly that refusal.
    verdict = intel.decide(
        context_key="asia · low · bd10-15 · px<70", base_qualified=False,
        failed_gates=("target distance",), ask=0.80, policy=pol,
    )
    assert verdict.final_action == intel.ADMIT


# ------------------------------------------------ the deployed artefact now

def test_the_deployed_policy_is_retired_and_honestly_labelled():
    path = RUNTIME / "intelligence_policy.json"
    if not path.exists():
        return
    pol = intel.Policy.load(path)
    assert not pol.mislabelled, "a policy must not misdeclare its features"
    if pol.keyed_feature_version == "binance-1":
        assert pol.feature_version in intel.RETIRED_FEATURE_VERSIONS
        assert not pol.vetoes_enabled and not pol.admissions_enabled
        verdict = intel.decide(
            context_key=next(iter(pol.arms), ""), base_qualified=True,
            failed_gates=(), ask=0.80, policy=pol,
        )
        assert verdict.final_action == intel.NEUTRAL


def test_frozen_candidates_are_keyed_on_brti():
    path = RUNTIME / "intelligence_candidates.json"
    if not path.exists():
        return
    data = json.loads(path.read_text())
    assert data.get("feature_version") == "brti-1"
    for candidate in data.get("candidates", []):
        assert not any(b in candidate["context"]
                       for b in intel.BINANCE_BAND_NAMES), candidate["context"]


# --------------------------------------------- the live context is Kalshi's

def test_the_brti_context_reads_only_brti_named_fields():
    """A Binance row passed here must not silently produce a plausible key.
    The BRTI fields carry BRTI names precisely so this cannot happen by
    autocomplete."""
    binance_row = {"session": "us", "vol_regime": "low",
                   "normalized_distance": 2.4, "our_ask": 0.62,
                   "momentum_5m_bps": 8.0, "volatility_5m_bps": 11.0}
    key = str(brti_context_of(binance_row))
    # Every Binance value is ignored; the distance falls to the empty default.
    assert "bd<5" in key
    assert not any(b in key for b in intel.BINANCE_BAND_NAMES)


def test_retired_versions_are_named_not_inferred():
    assert "binance-1" in intel.RETIRED_FEATURE_VERSIONS
    assert intel.FEATURE_VERSION not in intel.RETIRED_FEATURE_VERSIONS
