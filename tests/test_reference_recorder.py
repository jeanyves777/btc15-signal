"""The settlement-reference recorder must be harmless, honest and versioned.

Three properties are pinned here, and each of them is a mistake this repo has
already made once:

* **Named-column inserts.** 93 shadow rows are permanently quarantined because
  a positional insert shifted every column (FINDINGS 38). A dict whose keys
  arrive in a different order must still land in the right columns.
* **No substitution.** If BRTI is unavailable the basis column stays NULL. The
  moment a stand-in feed is allowed into the reference column, the table stops
  measuring what it claims to measure and looks complete while doing it.
* **It cannot break trading.** Every entry point swallows its own errors, the
  same contract the hourly shadow runs under.
"""

import asyncio
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from btc15_signal.config import Settings  # noqa: E402
from btc15_signal.reference import (  # noqa: E402
    Observation,
    basis_bps,
    rolling_mean,
)
from btc15_signal.reference_shadow import ReferenceShadow  # noqa: E402
from btc15_signal.reference_store import (  # noqa: E402
    SCHEMA_VERSION,
    ReferenceStore,
)


class FakeContract:
    ticker = "KXBTC15M-26SEP221130-30"
    target = 86291.01
    open_ms = 1_790_000_000_000
    close_ms = 1_790_000_900_000


def settings_for(tmp_path: Path, **overrides) -> Settings:
    values = {
        "reference_database_path": str(tmp_path / "ref.db"),
        "reference_poll_seconds": 0,
        "cfb_api_key": "",
    }
    values.update(overrides)
    return Settings(**values)


# --------------------------------------------------------------- arithmetic

def test_rolling_mean_reports_its_own_sample_count():
    """A 60s mean over 4 samples is not the same measurement as over 60, and
    the row has to be able to say which it was."""
    samples = [(1_000, 100.0), (30_000, 200.0), (61_000, 300.0)]
    mean, count, span = rolling_mean(samples, now_ms=61_000, window_ms=60_000)
    assert count == 3
    assert span == 60_000
    assert mean == 200.0

    # At t=91,000 the window reaches back to 31,000, so only the last sample
    # is still inside it. The mean must follow the window, not the list.
    mean, count, _ = rolling_mean(samples, now_ms=91_000, window_ms=60_000)
    assert count == 1
    assert mean == 300.0


def test_rolling_mean_empty_is_none_not_zero():
    assert rolling_mean([], now_ms=1_000) == (None, 0, None)


def test_basis_bps_refuses_a_zero_reference():
    assert basis_bps(100.0, None) is None
    assert basis_bps(None, 100.0) is None
    assert basis_bps(100.0, 0) is None
    assert basis_bps(86_296.0, 86_291.0) == __import__("pytest").approx(0.579, abs=0.01)


def test_observation_age_and_staleness():
    fresh = Observation("brti", "ok", 1.0, event_ms=1_000, received_ms=1_500)
    assert fresh.age_ms == 500
    assert not fresh.is_stale(3_000)
    assert Observation("brti", "ok", 1.0, event_ms=1_000, received_ms=9_000).is_stale(3_000)
    # No event timestamp means age is unknown, which is not the same as fresh.
    assert Observation("brti", "ok", 1.0, received_ms=9_000).age_ms is None


# -------------------------------------------------------------------- store

def test_named_column_insert_survives_key_reordering(tmp_path):
    """The columns are taken from the dict keys, so order cannot shift them."""
    store = ReferenceStore(str(tmp_path / "ref.db"))
    store.record_observation({
        "received_ms": 1_000, "source": "brti", "status": "ok",
        "session_id": "s1", "raw_price": 86_291.01, "window_open_ms": 7,
    })
    row = store.db.execute("SELECT * FROM reference_observations").fetchone()
    assert row["source"] == "brti"
    assert row["raw_price"] == 86_291.01
    assert row["received_ms"] == 1_000
    assert row["window_open_ms"] == 7
    assert row["schema_version"] == SCHEMA_VERSION
    store.close()


def test_schema_version_is_recorded_and_migrates_forward(tmp_path):
    """A v1 file opened by v2 code gains the columns and says so by date."""
    path = tmp_path / "old.db"
    db = sqlite3.connect(path)
    db.executescript("""
        CREATE TABLE schema_version (
            version INTEGER NOT NULL, applied_ms INTEGER NOT NULL, note TEXT);
        CREATE TABLE settlement_reconciliation (
            ticker TEXT PRIMARY KEY, window_open_ms INTEGER NOT NULL,
            close_ms INTEGER NOT NULL, reconciled_ms INTEGER NOT NULL,
            schema_version INTEGER NOT NULL);
    """)
    db.execute("PRAGMA user_version = 1")
    db.commit()
    db.close()

    store = ReferenceStore(str(path))
    assert store.db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    columns = {r[1] for r in store.db.execute("PRAGMA table_info(settlement_reconciliation)")}
    assert "calibrated_error_bps" in columns
    assert "outcome_calibrated" in columns
    versions = [r["version"] for r in store.db.execute("SELECT version FROM schema_version")]
    assert 2 in versions
    store.close()


def test_migration_is_idempotent(tmp_path):
    path = str(tmp_path / "ref.db")
    ReferenceStore(path).close()
    store = ReferenceStore(path)  # second open must not re-apply or raise
    assert store.db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    store.close()


# ------------------------------------------------------------- no stand-ins

def test_missing_brti_leaves_the_basis_null(tmp_path):
    """Binance is recorded, but never promoted into the reference column."""
    shadow = ReferenceShadow(settings_for(tmp_path))
    shadow._record(
        Observation("binance_spot", "ok", 86_316.67, event_ms=1_000, received_ms=1_000),
        now_ms=1_000, window_open=7, ticker="T", target=86_291.01,
        remaining=48, reference_price=None,   # BRTI absent
    )
    row = shadow._store.db.execute(
        "SELECT * FROM reference_observations WHERE source = 'binance_spot'"
    ).fetchone()
    assert row["raw_price"] == 86_316.67          # recorded
    assert row["basis_bps"] is None               # but not re-based onto itself
    assert row["reference_source"] == "brti"      # the reference it is missing
    assert row["signed_distance_bps"] is not None
    asyncio.run(shadow.close())


def test_brti_between_windows_is_missing_not_substituted(tmp_path):
    """No event ticker means no official price. It must say so, not guess."""
    shadow = ReferenceShadow(settings_for(tmp_path))
    observation = asyncio.run(shadow._brti_observation(1_000, None))
    assert observation.status == "missing"
    assert observation.raw_price is None
    assert "event ticker" in (observation.error or "")
    asyncio.run(shadow.close())


def test_brti_event_ticker_is_derived_from_the_market_ticker(tmp_path):
    """KXBTC15M-26SEP221245-45 -> KXBTC15M-26SEP221245, the event it belongs to."""
    shadow = ReferenceShadow(settings_for(tmp_path))
    captured = {}

    async def fake_series(event):
        captured["event"] = event
        return [(1_000, 86_291.01), (2_000, 86_292.00)]

    shadow._brti.series = fake_series
    observation = asyncio.run(shadow._brti_observation(3_000, FakeContract()))
    assert captured["event"] == "KXBTC15M-26SEP221130"
    assert observation.status == "ok"
    assert observation.raw_price == 86_292.00
    assert observation.event_ms == 2_000
    asyncio.run(shadow.close())


def test_stale_value_is_flagged_and_not_priced(tmp_path):
    shadow = ReferenceShadow(settings_for(tmp_path, reference_stale_ms=1_000))
    shadow._record(
        Observation("brti", "ok", 86_291.0, event_ms=1_000, received_ms=50_000),
        now_ms=50_000, window_open=7, ticker="T", target=86_291.01,
        remaining=48, reference_price=None,
    )
    row = shadow._store.db.execute("SELECT * FROM reference_observations").fetchone()
    assert row["status"] == "stale"
    assert row["stale"] == 1
    assert row["raw_price"] is None   # a stale print is not a current price
    assert row["age_ms"] == 49_000
    asyncio.run(shadow.close())


def test_gaps_are_collapsed_into_one_row_per_run(tmp_path):
    """A feed that is off all day writes one gap row, not one per poll."""
    shadow = ReferenceShadow(settings_for(tmp_path))
    for tick in range(5):
        shadow._record(
            Observation("brti", "missing", received_ms=tick * 1_000,
                        error="not entitled"),
            now_ms=tick * 1_000, window_open=7, ticker="T", target=1.0,
            remaining=1, reference_price=None,
        )
    shadow._flush_gaps()
    gaps = list(shadow._store.db.execute("SELECT * FROM feed_gaps"))
    assert len(gaps) == 1
    assert gaps[0]["polls"] == 5
    assert gaps[0]["reason"] == "missing"
    assert gaps[0]["first_ms"] == 0 and gaps[0]["last_ms"] == 4_000
    asyncio.run(shadow.close())


# ----------------------------------------------------------- harmlessness

def test_poll_never_raises_when_everything_is_broken(tmp_path):
    """The contract the hourly shadow runs under: research is never fatal."""
    shadow = ReferenceShadow(settings_for(tmp_path))

    async def explode(*_args, **_kwargs):
        raise RuntimeError("feed down")

    shadow._brti.latest = explode
    # None under `kalshi_only`: the recorder no longer builds a Binance
    # client at all, because it was making a live request every poll. Break
    # it only when it exists, so this test covers BOTH configurations rather
    # than silently passing on one.
    if shadow._binance is not None:
        shadow._binance.latest = explode
    shadow._kalshi.settled = explode

    asyncio.run(shadow.poll(1_000, FakeContract()))     # must not raise
    asyncio.run(shadow.reconcile(1_000))                # must not raise
    asyncio.run(shadow.close())


def test_poll_survives_a_contract_that_is_none(tmp_path):
    """Between windows there is no market; the recorder still has to run,
    because the 60 seconds a market settles on straddle that boundary."""
    shadow = ReferenceShadow(settings_for(tmp_path))
    asyncio.run(shadow.poll(1_000, None))
    rows = list(shadow._store.db.execute("SELECT * FROM reference_observations"))
    assert rows, "the recorder must still write between windows"
    assert all(row["window_open_ms"] is None for row in rows)
    asyncio.run(shadow.close())


def test_recorder_writes_nothing_to_the_trading_database(tmp_path):
    """Its own file, its own locks. Nothing here can block an order."""
    settings = settings_for(tmp_path)
    assert settings.reference_database_path != settings.database_path
    shadow = ReferenceShadow(settings)
    assert not Path(settings.database_path).samefile(settings.reference_database_path) \
        if Path(settings.database_path).exists() else True
    asyncio.run(shadow.close())
