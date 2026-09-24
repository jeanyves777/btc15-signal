"""The reversal gate: refuse an entry standing on a move that already turned.

THE CASE, 2026-09-24 06:08. Target 83,234. BRTI ran to ~83,340 - comfortably
above - the position was taken UP at 51c, and BRTI then collapsed to 83,191.
The trade lost 0.62 with every deployed gate green.

None of the four could see it. Momentum reads POSITIVE because the five-minute
window still contains the run-up. Distance is large because BRTI is far from
the strike - it just happens to be far on its way back. The band has held. The
gates describe where the price IS and say nothing about whether the move that
put it there is still alive.

`brti_retrace` is how much of the recent advance has already been handed back,
measured in the direction the setup is taken. 0.0 is at the high-water mark;
1.0 is the whole move gone.

SHIPPED BLOCKING ON THE OPERATOR'S EXPLICIT INSTRUCTION, and the evidence is
recorded beside it rather than implied: 79 markets with a Kalshi BRTI path,
stable entries 85.5% against reversed 70-75%, separation +13.3% at this window
and threshold. Every band's interval overlaps every other and the separation
collapses at a 180-second window, so the mechanism is better supported than
the parameters. These tests pin the MECHANISM; the numbers are expected to
move when there is enough evidence to move them.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from btc15_signal import kalshi_signal  # noqa: E402
from btc15_signal.brti import features_from_series  # noqa: E402
from btc15_signal.config import Settings  # noqa: E402
from btc15_signal.kalshi_brti import KalshiBRTIRule  # noqa: E402

BASE = 1_790_000_000_000
TARGET = 83_234.0


def series(points):
    return [(BASE + i * 1000, v) for i, v in enumerate(points)]


def climbing(n=130, step=1.5, start=83_220.0):
    return series([start + i * step for i in range(n)])


def peaked(give_back, up_n=80, up_step=1.5, start=83_220.0, down_n=50):
    up = [start + i * up_step for i in range(up_n)]
    top = up[-1]
    return series(up + [top - i * give_back for i in range(down_n)])


def feats(pts, target=TARGET):
    return features_from_series("E", pts, target, pts[-1][0])


# ------------------------------------------------------- the measure itself

def test_a_move_at_its_high_has_given_nothing_back():
    assert feats(climbing()).brti_retrace == 0.0


def test_a_move_that_reversed_has_given_most_back():
    r = feats(peaked(1.6)).brti_retrace
    assert r is not None and r > 0.6


def test_a_small_pullback_is_not_a_reversal():
    r = feats(peaked(0.4)).brti_retrace
    assert r is not None and r < 0.3


def test_the_measure_is_bounded():
    for give in (0.1, 0.4, 1.0, 1.6, 4.0):
        r = feats(peaked(give)).brti_retrace
        if r is not None:
            assert 0.0 <= r <= 1.0


def test_too_few_samples_cannot_be_measured():
    assert feats(series([83_240.0, 83_241.0])).brti_retrace is None


def test_it_is_measured_in_the_direction_of_the_side():
    """A DOWN setup gives back an advance DOWNWARD, not upward."""
    down = series([83_240.0 - i * 1.5 for i in range(80)]
                  + [83_240.0 - 79 * 1.5 + i * 1.6 for i in range(50)])
    f = feats(down)
    assert f.side == "DOWN"
    assert f.brti_retrace is not None and f.brti_retrace > 0.6


# ------------------------------------------------------------- as a gate

def test_the_gate_refuses_a_reversed_entry():
    rule = KalshiBRTIRule()
    f = feats(peaked(1.6))
    facts = rule.check_facts(f, 0.80, 600)
    stability = next(x for x in facts if x["name"] == "BRTI stability")
    assert not stability["passed"]
    assert "reversed" in stability["fail_text"]


def test_the_gate_allows_a_stable_entry():
    rule = KalshiBRTIRule()
    facts = rule.check_facts(feats(peaked(0.4)), 0.80, 600)
    stability = next(x for x in facts if x["name"] == "BRTI stability")
    assert stability["passed"]


def test_an_unmeasurable_path_is_refused_not_waved_through():
    """"Cannot be checked" and "is fine" are different states."""
    rule = KalshiBRTIRule()
    f = feats(series([83_240.0, 83_241.0]))
    if f is None:
        return
    facts = rule.check_facts(f, 0.80, 600)
    stability = next(x for x in facts if x["name"] == "BRTI stability")
    assert not stability["passed"]


def test_the_gate_appears_in_the_rendered_checks():
    """The header word and the ticks under it read the same facts, so a new
    gate has to show up in the block or the operator cannot see it fire."""
    rule = KalshiBRTIRule()
    _ok, facts, _failed = kalshi_signal.evaluate(rule, feats(climbing()), 0.80, 600)
    assert [x["name"] for x in facts].count("BRTI stability") == 1


def test_a_reversal_alone_is_enough_to_disqualify():
    """It must be able to refuse a setup every other gate accepts."""
    rule = KalshiBRTIRule(min_brti_normalized_distance=0.0,
                          require_momentum_alignment=False)
    ok, _facts, failed = kalshi_signal.evaluate(rule, feats(peaked(1.6)), 0.80, 600)
    assert not ok
    assert "BRTI stability" in failed


def test_the_same_setup_passes_once_the_move_holds():
    rule = KalshiBRTIRule(min_brti_normalized_distance=0.0,
                          require_momentum_alignment=False)
    ok, _facts, failed = kalshi_signal.evaluate(rule, feats(climbing()), 0.80, 600)
    assert ok, failed


# --------------------------------------------------------- the settings

def test_the_shipped_threshold_is_the_measured_one():
    assert Settings().reversal_max_retrace == 0.60
    assert Settings().reversal_window_s == 120
    assert KalshiBRTIRule().max_brti_retrace == 0.60


def test_the_gate_only_ever_refuses():
    """It can never admit a setup the other gates rejected."""
    rule = KalshiBRTIRule(min_brti_normalized_distance=1e9)
    ok, _facts, failed = kalshi_signal.evaluate(rule, feats(climbing()), 0.80, 600)
    assert not ok
    assert "BRTI distance" in failed
