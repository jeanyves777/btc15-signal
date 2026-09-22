"""Every displayed check comes from ONE decision snapshot.

The defect this pins: momentum was computed twice, with two conventions. The
gates signed it FOR OUR SIDE - a DOWN bet with the market falling scores
+3.3 bps, because the market is moving our way - while the context block
printed the RAW market figure, -3.3 bps. The same alert then carried both, and
nothing on screen said which one the rule had actually used.

`EntryRule.check_facts` is now the single source. `check_detail` derives from
it, the alert renders from it, the fill report renders from it, and the
confidence label is scored from it.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from btc15_signal import messages  # noqa: E402
from btc15_signal.strategy import EntryRule  # noqa: E402


class Prediction:
    side = "DOWN"
    distance_bps = 8.8
    raw_probability = 1.0


class Snapshot:
    volatility_5m_bps = 2.0
    # NEGATIVE: the market is falling. For a DOWN bet that is momentum in our
    # favour, so every surface must show it as POSITIVE for our side.
    momentum_5m_bps = -3.3


RULE = EntryRule.load("strategy.json")


def facts():
    return RULE.check_facts(Prediction(), Snapshot(), 0.77)


def momentum_text(text: str) -> str:
    for line in text.splitlines():
        if "Momentum" in line:
            return line
    raise AssertionError(f"no momentum line in:\n{text}")


def test_momentum_is_signed_for_our_side_not_for_the_market():
    """A DOWN bet with the market falling is momentum IN FAVOUR."""
    momentum = next(f for f in facts() if f["name"] == "Momentum")
    assert momentum["value"] > 0, (
        "the market fell and we bet DOWN - that is momentum for us, not against"
    )
    assert "+3.3" in momentum["pass_text"]


def test_the_alert_and_the_fill_report_agree_about_every_number():
    """Two surfaces, one snapshot. They used to reformat it independently."""
    shared = facts()
    alert = messages.signal_alert(
        side="DOWN", ticker="T", ask=0.77, price=85_960.0, target=86_000.0,
        remaining=362, confidence="MEDIUM", facts=shared, executable=True,
    )
    fill = messages.order_filled(
        side="DOWN", ticker="T", contracts=1, paid=0.77,
        confidence="MEDIUM", facts=shared,
    )
    assert momentum_text(alert) == momentum_text(fill)
    for fact in shared:
        shown = fact["pass_text"] if fact["passed"] else fact["fail_text"]
        assert shown in alert and shown in fill


def test_check_detail_is_derived_from_check_facts_not_recomputed():
    """The archive and the alert cannot be allowed to drift apart."""
    triples = RULE.check_detail(Prediction(), Snapshot(), 0.77)
    for triple, fact in zip(triples, facts(), strict=True):
        name, passed, detail = triple
        assert name == fact["name"].lower()
        assert passed == fact["passed"]
        assert detail == (fact["pass_text"] if passed else fact["fail_text"])


def test_a_failed_gate_still_shows_its_measured_value():
    """The old paper alert printed only the NAMES of the gates that failed -
    "model confidence, contract price band, target distance, momentum
    strength" - which says a setup was refused four times without saying by how
    much any of them missed."""

    class Weak:
        side = "UP"
        distance_bps = 1.66      # 0.8x against a 1.5x floor
        raw_probability = 0.33   # against a 50% floor

    text = messages.signal_alert(
        side="UP", ticker="T", ask=0.44, price=85_972.0, target=85_957.7,
        remaining=360, confidence="LOW",
        facts=RULE.check_facts(Weak(), Snapshot(), 0.44),
        executable=False, verdict="NO ENTRY",
    )
    assert "0.8" in text and "volatility" in text, "the measured distance"
    assert "33%" in text, "the measured model score"
    assert "needs" in text, "and what it needed"


def test_the_four_checks_stay_visible_on_a_rejection():
    """They ARE the decision. Hiding them behind DETAILS on a refusal is what
    made a refusal unreadable."""
    for executable in (True, False):
        text = messages.signal_alert(
            side="DOWN", ticker="T", ask=0.77, price=85_960.0, target=86_000.0,
            remaining=362, confidence="MEDIUM", facts=facts(),
            executable=executable,
        )
        for name in ("Price", "Momentum", "Distance", "Model"):
            assert f"{name}:" in text, f"{name} missing when executable={executable}"
