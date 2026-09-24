"""Choppiness moves the confidence word. It is not a gate and must not become one.

THE CASE, 2026-09-24 06:45. Target 83,232.53. Over the hour BRTI crossed the
strike six times, printed four separate peaks and finished $11.74 away from
it. The outcome of that market is a coin toss however far the last print
happens to sit from the target, and no deployed gate can tell it from a clean
trend: distance measures WHERE the price is, momentum measures the last five
minutes, and neither asks whether the path got anywhere.

THE OPERATOR'S INSTRUCTION was explicit - choppiness influences CONFIDENCE
only. So it is measured as a feature, it is consumed by `confidence_label`,
and it appears in no `check_facts` list anywhere. There is no threshold for a
setup to fail on it. The tests at the bottom are the ones that matter: they
pin that it cannot refuse or admit anything, now or after a later edit.

THE MEASURE is net movement over distance travelled: 0.0 is a straight line,
1.0 is thrashing that ends where it began.
"""

import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import btc15_signal.main as main  # noqa: E402
from btc15_signal.brti import features_from_series  # noqa: E402
from btc15_signal.config import Settings  # noqa: E402
from btc15_signal.kalshi_brti import KalshiBRTIRule  # noqa: E402

BASE = 1_790_000_000_000
TARGET = 83_232.53
OPENED = 1_790_000_000_000


def path(values):
    return [(BASE + i * 1000, v) for i, v in enumerate(values)]


def feats(values):
    pts = path(values)
    return features_from_series("E", pts, TARGET, pts[-1][0])


TREND = [83_200 + i * 0.5 for i in range(900)]
CHOP = [83_230 + 60 * math.sin(i / 25.0) for i in range(900)]
WOBBLE = [83_200 + i * 0.4 + 12 * math.sin(i / 30.0) for i in range(900)]


# ------------------------------------------------------------ the measure

def test_a_clean_trend_is_not_choppy():
    assert feats(TREND).brti_choppiness < 0.05


def test_thrashing_that_goes_nowhere_is_choppy():
    assert feats(CHOP).brti_choppiness > 0.9


def test_a_trend_with_noise_on_it_is_still_a_trend():
    assert feats(WOBBLE).brti_choppiness < 0.2


def test_the_measure_is_bounded():
    for values in (TREND, CHOP, WOBBLE):
        c = feats(values).brti_choppiness
        assert 0.0 <= c <= 1.0


def test_too_few_samples_cannot_be_measured():
    assert feats([83_230.0, 83_231.0, 83_232.0]).brti_choppiness is None


# -------------------------------------------------- it moves the word only

def test_an_unmeasurable_window_costs_nothing():
    assert main.choppiness_points(None, 15) == 0


def test_a_straight_line_costs_nothing():
    assert main.choppiness_points(0.0, 15) == 0


def test_the_penalty_is_proportional_not_stepped():
    """A cliff would flip the word on a rounding change."""
    points = [main.choppiness_points(c, 15) for c in (0.0, 0.25, 0.5, 0.75, 1.0)]
    assert points == sorted(points, reverse=True)
    assert points[-1] == -15
    assert len(set(points)) == 5


def test_it_can_only_ever_lower_confidence():
    for c in (0.0, 0.1, 0.5, 0.9, 1.0, 5.0, -1.0):
        assert main.choppiness_points(c, 15) <= 0


def test_a_choppy_window_lowers_the_header():
    facts = [{"name": str(i), "passed": i < 3} for i in range(4)]
    clean = main.confidence_label(facts, OPENED, None, 0, 0)
    choppy = main.confidence_label(
        facts, OPENED, None, 0, main.choppiness_points(0.96, 15))
    assert clean == "HIGH"
    assert choppy == "MEDIUM"


def test_the_label_is_still_clamped():
    facts = [{"name": str(i), "passed": True} for i in range(4)]
    assert main.confidence_label(facts, OPENED, None, 0, -999) in (
        "LOW", "MEDIUM", "HIGH")


# ------------------------------- THE POINT: it is not a gate, and cannot be

def test_choppiness_appears_in_no_gate():
    """The gates are what refuse a setup. Choppiness is not among them."""
    rule = KalshiBRTIRule()
    names = [f["name"] for f in rule.check_facts(feats(CHOP), 0.80, 600)]
    assert not any("chop" in n.lower() for n in names), names


def test_the_choppiest_possible_window_refuses_nothing():
    """Everything else green: a coin-toss session still qualifies, and only
    the confidence word says so."""
    from btc15_signal import kalshi_signal

    rule = KalshiBRTIRule(min_brti_normalized_distance=0.0,
                          require_momentum_alignment=False,
                          max_brti_retrace=1.0)
    f = feats(CHOP)
    assert f.brti_choppiness > 0.9
    ok, _facts, failed = kalshi_signal.evaluate(rule, f, 0.80, 600)
    assert ok, f"choppiness must not disqualify: {failed}"


def test_no_rule_threshold_exists_for_choppiness():
    import dataclasses

    fields = {f.name for f in dataclasses.fields(KalshiBRTIRule)}
    assert not any("chop" in n for n in fields), fields


def test_choppiness_never_reaches_sizing():
    """Same standard the intelligence layer is held to."""
    import inspect

    source = inspect.getsource(main)
    at = source.index("choppiness_points(")
    window = source[at:at + 400]
    for forbidden in ("count", "contracts", "size", "budget"):
        assert forbidden not in window, f"{forbidden} near the choppiness call"


def test_the_shipped_settings_are_the_documented_ones():
    s = Settings()
    assert s.choppiness_window_s == 900
    assert s.choppiness_penalty == 15
