"""The active signal is Kalshi-only, and says so when it cannot be formed.

The operator's rule: do not substitute Binance when Kalshi data is
unavailable; record unavailable or stale inputs explicitly. A silent skip and
a fallback are the same defect wearing different clothes - both leave the
archive unable to say why a window produced nothing.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from btc15_signal import kalshi_signal as ks  # noqa: E402
from btc15_signal.brti import BRTIFeatures  # noqa: E402
from btc15_signal.kalshi_brti import KalshiBRTIRule  # noqa: E402

NOW = 1_000_000


def features(**over) -> BRTIFeatures:
    base = dict(
        event_ticker="E", ts_ms=NOW, target=100_000.0, value=100_150.0,
        signed_distance_bps=15.0, brti_momentum_bps=4.0,
        brti_volatility_bps=1.0, brti_normalized_distance=15.0,
        samples=900, span_ms=900_000, stale=False,
        settlement_projection=100_150.0,
        # The reversal gate shipped after this fixture was written. A
        # QUALIFYING setup is one whose move is still intact, so the default
        # is a stable path; tests that want a reversal pass it explicitly.
        brti_retrace=0.0,
    )
    base.update(over)
    return BRTIFeatures(**base)


class Contract:
    def __init__(self, target=100_000.0, yes_bid=0.78, yes_ask=0.80):
        self.target, self.yes_bid, self.yes_ask = target, yes_bid, yes_ask
        self.no_ask = round(1 - yes_bid, 4)
        self.no_bid = round(1 - yes_ask, 4)

    def ask(self, side):
        return self.yes_ask if side == "UP" else self.no_ask


_DEFAULT = object()


def inputs(f=_DEFAULT, c=None, now=NOW, stale_ms=15_000):
    """`f=None` must mean "no reference", not "use the default one".

    The first version of this helper wrote `f or features()`, which quietly
    substituted a good reference whenever the test asked for a missing one -
    the exact silent fallback the tests below exist to forbid, reproduced in
    the harness. A sentinel is the fix; `None` is now a real value.
    """
    return ks.signal_inputs(features() if f is _DEFAULT else f,
                            c or Contract(), now_ms=now, stale_limit_ms=stale_ms)


# ------------------------------------------------- no model on this path

def test_there_is_no_probability_model_on_the_kalshi_path():
    """The deployed model is Binance-weighted and three of its five terms do
    not exist here. Feeding it BRTI numbers would saturate its sigmoid and
    turn the confidence gate off while still rendering it as a tick."""
    pred = ks.prediction_from(features())
    assert pred.raw_probability is None
    assert not pred.model_available
    assert pred.bucket == -1, "no model means calibration is not consulted"


def test_the_side_comes_from_the_official_reference():
    assert ks.prediction_from(features(signed_distance_bps=15.0)).side == "UP"
    assert ks.prediction_from(features(signed_distance_bps=-15.0)).side == "DOWN"


def test_distance_is_the_reference_distance():
    pred = ks.prediction_from(features(signed_distance_bps=-12.5))
    assert pred.distance_bps == 12.5


# --------------------------------------- unavailable is named, not silent

def test_a_missing_reference_is_reported_not_substituted():
    out = inputs(f=None)
    assert isinstance(out, ks.Unavailable)
    assert "unavailable" in out.reason
    assert "binance" not in str(out).lower()


def test_a_stale_reference_is_refused():
    out = inputs(f=features(stale=True))
    assert isinstance(out, ks.Unavailable) and "stale" in out.reason


def test_a_reference_older_than_the_limit_is_refused():
    """`stale` is the publisher's flag; this is our own clock, because a feed
    can stop updating without ever setting a flag."""
    out = inputs(now=NOW + 60_000, stale_ms=15_000)
    assert isinstance(out, ks.Unavailable) and "stale" in out.reason
    assert "60000ms old" in str(out)


def test_a_reference_for_another_window_is_refused():
    out = inputs(c=Contract(target=101_000.0))
    assert isinstance(out, ks.Unavailable)
    assert "another window" in out.reason


def test_a_missing_kalshi_quote_is_refused():
    out = inputs(c=Contract(yes_bid=0.0, yes_ask=0.0))
    assert isinstance(out, ks.Unavailable) and "quote" in out.reason


def test_good_inputs_produce_a_prediction_and_a_kalshi_ask():
    pred, ask, feats = inputs()
    assert pred.side == "UP"
    assert ask == 0.80, "the yes ask from the Kalshi book"
    assert feats.brti_normalized_distance == 15.0


def test_a_down_side_prices_off_the_kalshi_no_ask():
    pred, ask, _ = inputs(f=features(signed_distance_bps=-15.0))
    assert pred.side == "DOWN"
    assert ask == round(1 - 0.78, 4), "1 - yes_bid, from the same book"


# ------------------------------------------------------------- the gates

def rule(**over) -> KalshiBRTIRule:
    return KalshiBRTIRule(**over)


def test_the_measured_distance_floor_gates():
    """10x, measured on BRTI (FINDINGS 43) - not the 1.5 that belongs to
    Binance raw volatility and would pass everything here."""
    ok, _, failed = ks.evaluate(rule(), features(), 0.80, 500)
    assert ok and failed == ()
    thin = features(brti_normalized_distance=4.0)
    ok, _, failed = ks.evaluate(rule(), thin, 0.80, 500)
    assert not ok and "BRTI distance" in failed


def test_the_price_band_still_gates():
    ok, _, failed = ks.evaluate(rule(), features(), 0.97, 500)
    assert not ok and "Decision ask" in failed


def test_a_stale_reference_fails_its_own_gate():
    ok, _, failed = ks.evaluate(rule(), features(stale=True), 0.80, 500)
    assert not ok and "Reference" in failed


def test_outside_the_entry_window_does_not_qualify():
    ok, _, _ = ks.evaluate(rule(), features(), 0.80, 900)
    assert not ok
    ok, _, _ = ks.evaluate(rule(), features(), 0.80, 60)
    assert not ok


def test_every_gate_reads_kalshi_only():
    _, facts, _ = ks.evaluate(rule(), features(), 0.80, 500)
    blob = repr(facts).lower()
    assert "binance" not in blob
    assert any("BRTI" in f["name"] for f in facts)
    assert len(facts) == 5, "ask, distance, momentum, stability, reference"


def test_the_module_imports_nothing_from_binance():
    import inspect

    source = inspect.getsource(ks)
    # The docstring explains WHY the Binance model is excluded, so look at
    # the code rather than the prose.
    code = source.split('"""', 2)[-1]
    assert "from .binance" not in code and "import binance" not in code
    assert "BinanceClient" not in code
