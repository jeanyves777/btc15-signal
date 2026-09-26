"""The learned layer is on and affecting trades. Being OFF must be visible.

THE OPERATOR'S INSTRUCTION, 2026-09-24: "make sure the intelligence is on and
should affect trades, never off."

It already is. `intelligence_mode` is "live", `intelligence_authorised` is
True, and the layer acts through the CONFIDENCE channel on every decision -
which is not cosmetic, because confidence decides execution at the HIGH
threshold. The ETH fill at 18:49 that day existed only because of it: score 80
plus `learned +7` reached 85 and executed. Graded on outcomes, BTC's deltas
order correctly - the population it marks -6 wins 66.8%, the one it marks +8
wins 83.2%, and it fills 154 of 155 at +8 against 53 of 241 at -6.

Vetoes and admissions are separately off, and that is the promotion bar doing
its job rather than a switch being down: no arm has cleared Holm-Bonferroni at
FWER 0.05 on the validation slice. Graded live, all three current veto arms
would have refused more winners than losers (7W/0L, 23W/6L, 7W/5L), so
enabling them is a decision about evidence, not about turning something on.

WHAT THESE TESTS PIN is the part that makes the instruction enforceable rather
than merely intended. Five conditions silently reduce every decision to NEUTRAL
while every switch still reads "on" - a missing artefact, a fingerprint
mismatch, stale evidence, a non-live mode, and every arm withdrawn. A layer
contributing +0 is indistinguishable from a layer that is gone, so "it is on"
could previously only be believed. `health()` makes it checkable and `compose`
makes it loud.
"""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from btc15_signal import feature_contract, surface  # noqa: E402
from btc15_signal import intelligence_policy as intel  # noqa: E402

NOW = 1_790_000_000_000


class S:
    intelligence_mode = "live"
    intelligence_enabled = True
    intelligence_authorised = True
    intelligence_max_policy_age_ms = 30 * 86_400_000


def policy(**over):
    base = dict(
        version="test-1",
        arms={"a": {"n": 500, "mean": 0.05, "low": 0.01, "high": 0.09,
                    "delta": 8, "action": intel.NEUTRAL}},
        feature_fingerprint=feature_contract.FINGERPRINT,
        data_end_ms=NOW - 3_600_000,
    )
    base.update(over)
    return intel.Policy(**base)


def test_a_working_layer_reports_ok():
    state = intel.health(policy(), S(), NOW)
    assert state["ok"], state["reason"]
    assert state["acting_arms"] == 1


# ------------------------------- each way it can go off, and each must show

def test_a_missing_artefact_is_not_silent():
    assert not intel.health(policy(arms={}), S(), NOW)["ok"]


def test_a_stale_policy_is_not_silent():
    old = policy(data_end_ms=NOW - 60 * 86_400_000)
    state = intel.health(old, S(), NOW)
    assert not state["ok"] and "old" in state["reason"]


def test_a_fingerprint_mismatch_is_not_silent():
    state = intel.health(policy(feature_fingerprint="deadbeef"), S(), NOW)
    assert not state["ok"] and "contract" in state["reason"]


def test_shadow_mode_is_not_silent():
    class Shadow(S):
        intelligence_mode = "shadow"
    state = intel.health(policy(), Shadow(), NOW)
    assert not state["ok"] and "live" in state["reason"]


def test_withdrawn_arms_leave_a_policy_that_does_nothing():
    """It loads, it has arms, and it cannot move a single decision."""
    dead = policy(arms={"a": {"n": 500, "delta": 0,
                              "action": intel.NEUTRAL}})
    state = intel.health(dead, S(), NOW)
    assert not state["ok"] and "neutral" in state["reason"]


def test_an_unauthorised_layer_is_not_silent():
    class NoAuth(S):
        intelligence_authorised = False
    assert not intel.health(policy(), NoAuth(), NOW)["ok"]


# ------------------------------------------------- and the operator sees it

def test_the_alert_is_rendered_when_the_layer_cannot_act():
    surface.set_intelligence_state(intel.health(policy(arms={}), S(), NOW))
    try:
        text = surface.compose(header="X", ticker="KXBTC15M-26SEP241000-00",
                               essentials=[], checks=[], status="")
        assert "Intelligence NOT acting" in text
        assert "fixed rules alone" in text
    finally:
        surface.set_intelligence_state(None)


def test_a_healthy_layer_does_not_nag():
    surface.set_intelligence_state(intel.health(policy(), S(), NOW))
    try:
        text = surface.compose(header="X", ticker="KXBTC15M-26SEP241000-00",
                               essentials=[], checks=[], status="")
        assert "Intelligence NOT acting" not in text
    finally:
        surface.set_intelligence_state(None)


def test_no_builder_can_omit_the_alert():
    """It is emitted by the single assembler, not by each message builder."""
    source = (Path(__file__).resolve().parents[1] / "src" / "btc15_signal"
              / "surface.py").read_text(encoding="utf-8")
    assert "alert = intelligence_alert()" in source


def test_the_service_publishes_health_on_every_policy_load():
    source = (Path(__file__).resolve().parents[1] / "src" / "btc15_signal"
              / "main.py").read_text(encoding="utf-8")
    assert "surface.set_intelligence_state(state)" in source
    assert "intel.health(pol, settings" in source


# ----------------------------------------------- the deployed configuration

def test_the_shipped_settings_keep_it_on():
    """If this fails, the layer is off in production, whatever else is true."""
    from btc15_signal.config import Settings
    s = Settings()
    assert s.intelligence_enabled is True
    assert s.intelligence_authorised is True
    assert s.intelligence_mode == "live"


def test_both_live_artefacts_are_healthy_and_separate():
    """ETH reading BTC's policy would apply BTC-fitted arms to ETH decisions,
    and the learning runner WRITES this path - so a shared one would also
    overwrite BTC's live policy at the first scheduled fit."""
    root = Path(__file__).resolve().parents[1]
    btc = root / "runtime" / "intelligence_policy.json"
    eth = root / "runtime-eth" / "intelligence_policy.json"
    if not (btc.exists() and eth.exists()):
        return
    now = int(time.time() * 1000)
    a = intel.health(intel.Policy.load(str(btc)), S(), now)
    b = intel.health(intel.Policy.load(str(eth)), S(), now)
    assert a["ok"], a["reason"]
    assert b["ok"], b["reason"]
    assert a["version"] != b["version"], "the two instances share one artefact"
