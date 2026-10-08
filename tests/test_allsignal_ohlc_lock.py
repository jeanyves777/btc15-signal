"""Skip BTC signals while a detected chop range is locked (study: reports/oct6_regime_deep)."""

import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from btc15_signal import main  # noqa: E402
from btc15_signal.config import Settings  # noqa: E402
from btc15_signal.ohlc_regime import range_lock  # noqa: E402

X = 1_790_600_400_000 + 240_000
HOURS = 8


def reference(tmp_path, price_at, name="ref.db"):
    path = tmp_path / name
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE brti_features (id INTEGER PRIMARY KEY, window_open_ms INTEGER, "
               "received_ms INTEGER, ts_ms INTEGER, brti_value REAL, stale INTEGER)")
    t = X - HOURS * 3_600_000
    while t <= X:
        db.execute("INSERT INTO brti_features (window_open_ms, received_ms, ts_ms, brti_value, "
                   "stale) VALUES (?,?,?,?,0)", (t // 900_000 * 900_000, t, t, price_at(t)))
        t += 12_000
    db.commit()
    db.close()
    return path


def flat(_t):
    return 100_000.0


def rising(t):
    return 100_000.0 + (t - (X - HOURS * 3_600_000)) / 1000 * 3


def settings(path, **kw):
    return Settings(kalshi_series="KXBTC15M", reference_database_path=str(path), **kw)


def test_a_flat_range_locks_and_the_signal_is_skipped(tmp_path):
    path = reference(tmp_path, flat)
    assert range_lock(path, X)["locked"] is True
    note = main.allsignal_ohlc_lock_skip(settings(path, allsignal_ohlc_lock_skip=True), X)
    assert note.startswith("chop range locked")


def test_a_trend_does_not_lock(tmp_path):
    path = reference(tmp_path, rising)
    assert range_lock(path, X)["locked"] is False
    assert main.allsignal_ohlc_lock_skip(settings(path, allsignal_ohlc_lock_skip=True), X) == ""


def test_off_by_default_other_instruments_and_missing_data_never_skip(tmp_path):
    path = reference(tmp_path, flat)
    assert Settings.model_fields["allsignal_ohlc_lock_skip"].default is False
    assert main.allsignal_ohlc_lock_skip(settings(path), X) == ""
    eth = Settings(kalshi_series="KXETH15M", reference_database_path=str(path),
                   allsignal_ohlc_lock_skip=True)
    assert main.allsignal_ohlc_lock_skip(eth, X) == ""
    missing = settings(tmp_path / "nope.db", allsignal_ohlc_lock_skip=True)
    assert main.allsignal_ohlc_lock_skip(missing, X) == ""
    assert not (tmp_path / "nope.db").exists(), "never creates a database"
    sparse = reference(tmp_path, flat, "sparse.db")
    db = sqlite3.connect(sparse)
    db.execute("DELETE FROM brti_features WHERE ts_ms < ?", (X - 2 * 3_600_000,))
    db.commit()
    db.close()
    assert range_lock(sparse, X)["locked"] is False, "unknown is never chop"


def test_an_error_lets_the_signal_through(tmp_path, monkeypatch):
    path = reference(tmp_path, flat)
    import btc15_signal.ohlc_regime as regime
    monkeypatch.setattr(regime, "range_lock", lambda *a, **k: 1 / 0)
    assert main.allsignal_ohlc_lock_skip(settings(path, allsignal_ohlc_lock_skip=True), X) == ""
