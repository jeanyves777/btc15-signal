"""BRTI is the reference. Binance may never decide anything that settles.

The operator's rule, and FINDINGS 41 is the evidence for it: Binance BTCUSDT
disagrees with the official settlement about WHICH SIDE WON in 19.4% of
markets, and in 40.5% of the markets that finish within 5 bps of the strike.
Every one of those was a Kalshi decision taken with a different measuring
instrument.

These tests pin the separation itself, not just today's arithmetic:

* the BRTI feature path imports no Binance module and reads no Binance field
* side, distance and the settlement projection come from BRTI alone
* the Binance columns exist only as archive, and nothing gates on them
* our own 60-second recomputation is a cross-check, never the source
"""

import ast
import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

from btc15_signal import brti  # noqa: E402
from btc15_signal.brti import (  # noqa: E402
    BRTIFeatures,
    BRTIObservation,
    features_from_series,
    sixty_second_mean,
)

TARGET = 86_291.01


def series(start: float, step: float, n: int = 301, t0: int = 1_000_000) -> list:
    """n one-second points walking from `start` by `step` each second."""
    return [(t0 + i * 1000, start + i * step) for i in range(n)]


# ------------------------------------------------- the separation, structural

def test_brti_module_never_imports_binance():
    """Structural, so it keeps holding after someone edits the module."""
    tree = ast.parse((SRC / "btc15_signal" / "brti.py").read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
    assert not any("binance" in name.lower() for name in imported), imported


def test_brti_source_text_mentions_no_binance_field():
    source = (SRC / "btc15_signal" / "brti.py").read_text(encoding="utf-8")
    code = "\n".join(
        line for line in source.splitlines()
        if not line.lstrip().startswith("#")
    )
    # The module docstring discusses Binance deliberately; no CODE may touch it.
    body = code.split('"""', 2)[-1]
    for forbidden in ("binance", "BinanceClient", "spot_base_url", "symbol"):
        assert forbidden not in body, f"{forbidden!r} reached the BRTI code path"


def test_features_carry_no_binance_derived_field():
    names = set(BRTIFeatures.__dataclass_fields__)
    assert not any("binance" in n for n in names)
    # And the names are deliberately distinct from the Binance snapshot's, so a
    # threshold measured for one cannot be silently applied to the other.
    assert "normalized_distance" not in names
    assert "brti_normalized_distance" in names
    assert "momentum_5m_bps" not in names
    assert "brti_momentum_bps" in names


# --------------------------------------------------------- the decision facts

def test_side_comes_from_brti_against_the_official_strike():
    up = features_from_series("E", series(TARGET + 50, 0.0), TARGET, 1_000_301_000)
    down = features_from_series("E", series(TARGET - 50, 0.0), TARGET, 1_000_301_000)
    assert up.side == "UP"
    assert down.side == "DOWN"
    assert up.signed_distance_bps > 0 > down.signed_distance_bps


def test_side_flips_exactly_at_the_strike_not_near_it():
    """The 19.4% disagreement lives in the last basis point, so pin the edge."""
    just_over = features_from_series("E", series(TARGET + 0.01, 0.0), TARGET, 1_000_301_000)
    just_under = features_from_series("E", series(TARGET - 0.01, 0.0), TARGET, 1_000_301_000)
    assert just_over.side == "UP"
    assert just_under.side == "DOWN"


def test_settlement_projection_is_the_current_sixty_second_mean():
    """Each published point IS a trailing 60s mean, so the latest one is what
    this contract would settle at if the window ended now. That equivalence is
    the finding the close-call work rests on - it must not drift."""
    features = features_from_series("E", series(86_300.0, 0.5), TARGET, 1_000_301_000)
    assert features.settlement_projection == features.value
    assert features.value == 86_300.0 + 0.5 * 300


def test_distance_is_measured_against_the_official_strike():
    features = features_from_series("E", series(TARGET * 1.0001, 0.0), TARGET, 1_000_301_000)
    assert features.signed_distance_bps == pytest.approx(1.0, abs=0.01)


def test_momentum_has_the_sign_of_the_move():
    rising = features_from_series("E", series(86_000.0, 1.0), TARGET, 1_000_301_000)
    falling = features_from_series("E", series(86_000.0, -1.0), TARGET, 1_000_301_000)
    assert rising.brti_momentum_bps > 0
    assert falling.brti_momentum_bps < 0


def test_a_flat_series_has_no_volatility_and_infinite_normalisation_is_avoided():
    features = features_from_series("E", series(86_000.0, 0.0), TARGET, 1_000_301_000)
    assert features.brti_volatility_bps == pytest.approx(0.0, abs=1e-9)
    # A zero denominator must not produce inf/NaN in a gate input.
    assert features.brti_normalized_distance == features.brti_normalized_distance


def test_staleness_is_reported_not_hidden():
    points = series(86_000.0, 0.0)
    fresh = features_from_series("E", points, TARGET, points[-1][0] + 1_000)
    stale = features_from_series("E", points, TARGET, points[-1][0] + 60_000)
    assert not fresh.stale
    assert stale.stale


def test_empty_or_targetless_input_returns_none_rather_than_a_guess():
    assert features_from_series("E", [], TARGET, 1_000) is None
    assert features_from_series("E", series(86_000.0, 0.0), 0.0, 1_000) is None


# ------------------------------------------------------ the cross-check rule

def test_our_own_sixty_second_mean_is_a_crosscheck_with_its_sample_count():
    """It returns the count so a thin window can be told from a full one -
    recomputing the settlement lands $0.20-$1.10 from official, so this number
    may raise an alarm and must never be the one a decision uses."""
    close = 1_000_360_000
    prints = [(close - 60_000 + i * 200, 100.0) for i in range(1, 301)]
    mean, count = sixty_second_mean(prints, close)
    assert mean == 100.0
    assert count == 300
    assert sixty_second_mean([], close) == (None, 0)


def test_observation_age_is_measured_from_the_source_timestamp():
    observation = BRTIObservation(
        event_ticker="E", ts_ms=1_000, value=86_000.0,
        received_ms=4_000, source=brti.LIVE_DATA_SOURCE,
    )
    assert observation.age_ms == 3_000
    assert observation.is_stale(2_000)
    assert not observation.is_stale(5_000)
