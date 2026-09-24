"""The learning corpus is per instrument, and reported as what was read.

`_load` took `brti_path` and `market_path` as FUNCTION DEFAULTS pointing at
BTC, and nothing above it passed them - so `combined_rows` always read BTC
whatever the instance traded. `_corpus_mismatch` refuses a fit on the wrong
instrument, but refusing is only half the job: a second instance also needs a
way to be RIGHT. These settings are that way.

Provenance matters as much as the paths. The sources were a hardcoded pair of
BTC filenames, so a run that read an ETH corpus would still have reported
having read BTC's. Provenance that names files nobody opened is worse than
none, because it is believed.
"""

import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from btc15_signal import feature_contract, learning_data  # noqa: E402
from btc15_signal.config import Settings  # noqa: E402
from btc15_signal.learning_runner import LearningRunner  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


# ------------------------------------------------------------- the setting

def test_the_corpus_is_a_setting_not_a_default():
    s = Settings()
    assert s.corpus_brti_path == "data/brti_history.db"
    assert s.corpus_market_path == "data/market_data.db"


def test_an_instance_can_override_it():
    s = Settings(corpus_brti_path="data/brti_history_eth.db",
                 corpus_market_path="data/market_data_kxeth15m.db")
    assert s.corpus_brti_path.endswith("_eth.db")


def test_combined_rows_accepts_the_paths():
    import inspect

    params = inspect.signature(learning_data.combined_rows).parameters
    assert "brti_path" in params
    assert "market_path" in params


def test_the_runner_passes_them_from_settings():
    """Threading them only as far as `combined_rows` would leave the runner
    still reading BTC - which is the defect, one layer up."""
    import inspect

    source = inspect.getsource(LearningRunner._train)
    assert "brti_path=self.settings.corpus_brti_path" in source
    assert "market_path=self.settings.corpus_market_path" in source


# ------------------------------------------------------------- provenance

def test_provenance_names_the_files_actually_read():
    """It was a hardcoded BTC pair, so an ETH run would have reported BTC."""
    rows, prov = learning_data.combined_rows(
        None, fingerprint=feature_contract.FINGERPRINT,
        brti_path="data/brti_history_eth.db",
        market_path="data/market_data_kxeth15m.db")
    assert prov.sources[0] == "data/brti_history_eth.db"
    assert prov.sources[1] == "data/market_data_kxeth15m.db"


def test_the_default_provenance_is_still_btc():
    rows, prov = learning_data.combined_rows(
        None, fingerprint=feature_contract.FINGERPRINT)
    assert prov.sources[0] == "data/brti_history.db"


# ------------------------------------------- the ETH corpus is real and ETH

def test_the_eth_corpus_exists():
    assert (ROOT / "data" / "brti_history_eth.db").exists()


def test_the_eth_corpus_contains_only_eth():
    """The guard would refuse it otherwise - but a corpus that is quietly
    half BTC is the shape that survives a guard keyed on 'any disagreement'
    only because someone built it carefully. Asserted directly."""
    db = sqlite3.connect(
        f"file:{ROOT / 'data' / 'brti_history_eth.db'}?mode=ro", uri=True)
    prefixes = {r[0] for r in db.execute(
        "SELECT DISTINCT substr(ticker, 1, 8) FROM brti_decision_points")}
    assert prefixes == {"KXETH15M"}, prefixes


def test_the_eth_corpus_satisfies_the_guard_for_eth_and_not_for_btc():
    rows, _ = learning_data.combined_rows(
        None, fingerprint=feature_contract.FINGERPRINT,
        brti_path="data/brti_history_eth.db",
        market_path="data/market_data_kxeth15m.db")
    assert rows, "the ETH corpus produced no policy rows"

    eth = LearningRunner.__new__(LearningRunner)
    eth.settings = Settings(kalshi_series="KXETH15M")
    assert eth._corpus_mismatch(rows) == ""

    btc = LearningRunner.__new__(LearningRunner)
    btc.settings = Settings(kalshi_series="KXBTC15M")
    assert btc._corpus_mismatch(rows)


def test_the_eth_launcher_points_at_the_eth_corpus():
    launcher = (ROOT / "scripts" / "run_eth.ps1").read_text(encoding="utf-8")
    assert "data/brti_history_eth.db" in launcher
    assert "data/market_data_kxeth15m.db" in launcher
    assert 'LEARNING_ENABLED = "true"' in launcher


def test_the_backfill_can_target_another_instrument():
    """It hardcoded both BTC paths, which is why no ETH corpus existed."""
    script = (ROOT / "scripts" / "backfill_brti.py").read_text(encoding="utf-8")
    assert "--market-db" in script
    assert "--out" in script
