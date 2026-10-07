"""A learning run may not fit one instrument's arms on another's rows.

FOUND LIVE, 2026-09-24. The ETH instance was started beside BTC and inherited
three things nobody had thought about:

  * `intelligence_policy_path` defaults to runtime/ - BTC's - and the
    learning runner WRITES it. ETH was about six hours from overwriting
    BTC's live policy.
  * it was meanwhile READING those BTC-fitted arms and applying them to ETH
    decisions.
  * the corpus itself, `data/brti_history.db` and `data/market_data.db`, is
    BTC, and those are function DEFAULTS rather than settings - so an ETH
    fit would have been built entirely from BTC rows.

None of it would have looked wrong. The arms are keyed on
distance-price-momentum, strings that exist for both instruments, so a
BTC-fitted arm applies cleanly to an ETH decision and produces a number.

The paths are now per-instance and learning is off for ETH, but both of
those are CONFIGURATION. This guard is the structural one: it compares the
instrument the ROWS describe against the instrument the process trades, and
refuses. Configuration can be forgotten; this cannot.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from btc15_signal.config import Settings  # noqa: E402
from btc15_signal.learning_runner import LearningRunner  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


class FakeStore:
    def __init__(self):
        import sqlite3
        self.db = sqlite3.connect(":memory:")


def runner(series):
    s = Settings(kalshi_series=series)
    try:
        return LearningRunner(s, FakeStore())
    except Exception:            # store shape differs; only the guard matters
        obj = LearningRunner.__new__(LearningRunner)
        obj.settings = s
        return obj


def rows(*tickers):
    return [{"ticker": t} for t in tickers]


# ------------------------------------------------------------- the refusal

def test_btc_rows_are_refused_for_an_eth_instance():
    why = runner("KXETH15M")._corpus_mismatch(
        rows("KXBTC15M-26SEP241300-00", "KXBTC15M-26SEP241315-15"))
    assert why
    assert "BTC" in why and "ETH" in why


def test_eth_rows_are_refused_for_a_btc_instance():
    why = runner("KXBTC15M")._corpus_mismatch(
        rows("KXETH15M-26SEP241300-00"))
    assert why
    assert "ETH" in why and "BTC" in why


def test_a_mixed_corpus_is_refused():
    """The dangerous shape: mostly right, quietly contaminated."""
    why = runner("KXBTC15M")._corpus_mismatch(
        rows(*(["KXBTC15M-26SEP2413"] * 500), "KXETH15M-26SEP2413"))
    assert why


# ------------------------------------------------------------- the allows

def test_matching_rows_are_allowed():
    assert runner("KXBTC15M")._corpus_mismatch(
        rows("KXBTC15M-26SEP241300-00")) == ""
    assert runner("KXETH15M")._corpus_mismatch(
        rows("KXETH15M-26SEP241300-00")) == ""


def test_the_hourly_ladder_counts_as_btc():
    """Different series, same instrument."""
    assert runner("KXBTC15M")._corpus_mismatch(
        rows("KXBTCD-26SEP2412")) == ""


def test_an_unrecognisable_corpus_is_not_refused():
    """A fresh, synthetic or fixture corpus has no instrument to disagree
    with. Refusing it would stop a first run for a reason that has nothing
    to do with instruments."""
    assert runner("KXBTC15M")._corpus_mismatch(rows("", "")) == ""
    assert runner("KXBTC15M")._corpus_mismatch([{}]) == ""
    assert runner("KXBTC15M")._corpus_mismatch([]) == ""


def test_an_unrecognisable_series_does_not_refuse_everything():
    """If the instance itself cannot be identified, the guard abstains
    rather than blocking every fit."""
    assert runner("KXAAAGASD")._corpus_mismatch(
        rows("KXBTC15M-26SEP241300-00")) == ""


# ------------------------------------------------- it is wired to the fit

def test_the_guard_runs_before_training():
    import inspect

    source = inspect.getsource(LearningRunner._train)
    assert "_corpus_mismatch" in source
    assert source.index("_corpus_mismatch") < source.index("learning.train(")


def test_a_refusal_aborts_rather_than_fitting():
    import inspect

    source = inspect.getsource(LearningRunner._train)
    at = source.index("mismatch = self._corpus_mismatch")
    assert "return outcome" in source[at:at + 260]


# --------------------------------------------- the ETH instance is isolated

def test_eth_has_its_own_policy_artefacts():
    """The runner WRITES the policy path. Sharing it meant ETH would
    overwrite BTC's live policy at its first scheduled fit."""
    launcher = (ROOT / "scripts" / "run_eth.ps1").read_text(encoding="utf-8")
    assert "runtime-eth/intelligence_policy.json" in launcher
    assert "runtime-eth/intelligence_candidates.json" in launcher


def test_eth_learns_only_because_it_now_has_its_own_corpus():
    """Learning was off while the only corpus was BTC's. It is on now, and
    the two facts must stay tied together: enabling it without pointing at
    an ETH corpus would hand `_corpus_mismatch` a fit to refuse every six
    hours, which is a loop that looks like learning and never learns."""
    launcher = (ROOT / "scripts" / "run_eth.ps1").read_text(encoding="utf-8")
    if 'LEARNING_ENABLED = "true"' in launcher:
        assert "data/brti_history_eth.db" in launcher
        assert "data/market_data_kxeth15m.db" in launcher
    else:
        assert 'LEARNING_ENABLED = "false"' in launcher


def test_btc_keeps_learning_and_its_own_paths():
    s = Settings()
    assert s.learning_enabled is True
    assert s.intelligence_policy_path == "runtime/intelligence_policy.json"
