"""Historical and live BRTI features must be the SAME computation.

Context parity - both sides calling `brti_context_of` - only fixes the band
labels. If the live path fed those labels a differently-computed volatility,
every label would still come from the agreed function and every one could
still be wrong.

`scripts/verify_feature_parity.py` settles it against the live database
(250/250 identical, worst 1.3e-10). These tests pin the properties that
recomputation relies on, so a change to either path fails here rather than
silently re-opening the gap.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from btc15_signal.brti import features_from_series  # noqa: E402
from btc15_signal.config import Settings  # noqa: E402


def series(n: int, end_ms: int = 1_000_000, start: float = 100_000.0):
    """One sample a second, the cadence both paths actually receive."""
    return [
        (end_ms - (n - 1 - i) * 1000, start + (i % 7) - 3)
        for i in range(n)
    ]


def test_only_the_trailing_window_enters_the_arithmetic():
    """The one real difference between the paths: live holds a rolling
    3,600-sample hour, the backfill's truncation gives 2,941-3,241. Both read
    only the trailing 300s, so the surplus must not change a single number."""
    long_series = series(3600)
    short_series = long_series[-1200:]          # the backfill's shorter tail
    a = features_from_series("E", long_series, 100_000.0, 1_000_000)
    b = features_from_series("E", short_series, 100_000.0, 1_000_000)
    assert a is not None and b is not None
    for field in ("brti_volatility_bps", "brti_momentum_bps",
                  "brti_normalized_distance", "signed_distance_bps", "value"):
        assert abs(getattr(a, field) - getattr(b, field)) < 1e-9, field


def test_the_lookbacks_are_the_defaults_both_paths_use():
    """Neither path passes a window, so a change to these defaults moves
    historical and live together - and a change at ONE call site would not."""
    import inspect

    params = inspect.signature(features_from_series).parameters
    assert params["momentum_window_s"].default == 300
    assert params["volatility_window_s"].default == 300


def test_the_backfill_decision_seconds_match_the_live_entry_window():
    """A corpus sampled outside the minutes the bot can act in would train on
    setups it never sees."""
    from backfill_brti import DECISION_SECONDS

    settings = Settings()
    assert max(DECISION_SECONDS) == settings.entry_from_seconds
    assert min(DECISION_SECONDS) == settings.entry_to_seconds
    assert all(
        settings.entry_to_seconds <= s <= settings.entry_from_seconds
        for s in DECISION_SECONDS
    )


def test_both_paths_read_the_same_series_source():
    """Same client, same endpoint. Two fetchers would be two feeds."""
    import inspect

    from btc15_signal import reference_shadow
    import backfill_brti

    for module in (reference_shadow, backfill_brti):
        source = inspect.getsource(module)
        assert "KalshiBRTI" in source
        assert "features_from_series" in source


def test_a_sparse_series_yields_no_features_rather_than_a_guess():
    assert features_from_series("E", [], 100_000.0, 1_000_000) is None
    assert features_from_series("E", series(5), 0.0, 1_000_000) is None


def test_normalized_distance_is_distance_over_volatility():
    """The definition the bands are calibrated against. If this inverted, the
    labels would still be produced by the agreed function."""
    feats = features_from_series("E", series(600), 99_990.0, 1_000_000)
    assert feats is not None
    expected = abs(feats.signed_distance_bps) / max(feats.brti_volatility_bps, 1e-9)
    assert abs(feats.brti_normalized_distance - expected) < 1e-9
