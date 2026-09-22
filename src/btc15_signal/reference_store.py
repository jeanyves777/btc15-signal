"""Storage for the settlement-reference recorder. Shadow only; nothing trades.

FINDINGS section 40 found that the bot decides using Binance spot while the
contract settles on something else entirely, and could not say how much of the
gap was the FEED and how much was the 60-second AVERAGING. This database exists
to separate those two, and it is kept apart from `btc15.db` on purpose: a
research archive that shares a file with the order path shares its locks and
its failure modes, and this one must never be able to interrupt trading.

WHAT THE CONTRACT ACTUALLY SETTLES ON, confirmed from Kalshi's own API rather
than assumed (`GET /series/KXBTC15M`, and `rules_primary` on every market):

    "If the simple average of the sixty seconds of CF Benchmarks' BRTI before
     12:15 PM EDT ... is at least the simple average of the sixty seconds of
     CF Benchmarks' BRTI before 12:00 PM EDT ... then the market resolves Yes."

So BOTH ends are 60-second BRTI averages, not spot prints, and the strike of
one window is the settlement of the previous one. That identity is exact on
6,420 consecutive pairs in the corpus - `floor_strike[N] == expiration_value[N-1]`
to the cent, every time - which is why Kalshi's published fields can be treated
as the official reference and the app display never needs to be read.

SCHEMA VERSIONING. `PRAGMA user_version` carries the number and `schema_version`
carries the history, so a column added later can be told from a column that was
always there and never populated. Every insert names its columns explicitly:
positional inserts are what silently shifted 93 shadow rows into permanent
quarantine (FINDINGS 38), and that must not happen twice.
"""

import sqlite3
import time
from pathlib import Path

SCHEMA_VERSION = 3

# Applied in order for any file below the current version. Version 2 adds the
# calibration columns: the decomposition showed the Binance-to-BRTI gap is
# almost entirely a slowly drifting FEED basis rather than the 60-second
# averaging, and a trailing median of prior windows removes most of it. That is
# a research result and it is stored, not acted on - no entry, exit or sizing
# path reads these columns.
MIGRATIONS: dict[int, tuple[str, ...]] = {
    2: (
        "ALTER TABLE settlement_reconciliation ADD COLUMN calibration_bps REAL",
        "ALTER TABLE settlement_reconciliation ADD COLUMN calibration_window INTEGER",
        "ALTER TABLE settlement_reconciliation ADD COLUMN calibrated_binance REAL",
        "ALTER TABLE settlement_reconciliation ADD COLUMN calibrated_error_bps REAL",
        "ALTER TABLE settlement_reconciliation ADD COLUMN outcome_calibrated INTEGER",
    ),
    # Version 3: BRTI-native gate inputs, recorded beside the Binance ones.
    # A separate table rather than more columns on `reference_observations`,
    # because these are one row per POLL describing the market, not one row per
    # source describing a price.
    3: (
        """
        CREATE TABLE IF NOT EXISTS brti_features (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            window_open_ms INTEGER,
            ticker TEXT,
            event_ticker TEXT,
            received_ms INTEGER NOT NULL,
            ts_ms INTEGER NOT NULL,
            remaining_s INTEGER,
            target REAL NOT NULL,
            brti_value REAL NOT NULL,
            signed_distance_bps REAL NOT NULL,
            brti_momentum_bps REAL,
            brti_volatility_bps REAL,
            brti_normalized_distance REAL,
            brti_side TEXT,
            settlement_projection REAL,
            samples INTEGER,
            span_ms INTEGER,
            stale INTEGER NOT NULL DEFAULT 0,
            -- The Binance view of the SAME instant, archived for comparison
            -- and for nothing else. No gate reads these.
            binance_price REAL,
            binance_signed_distance_bps REAL,
            binance_side TEXT,
            sides_agree INTEGER,
            schema_version INTEGER NOT NULL
        )
        """,
        "CREATE INDEX IF NOT EXISTS brti_features_window "
        "ON brti_features(window_open_ms, received_ms)",
    ),
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER NOT NULL,
    applied_ms INTEGER NOT NULL,
    note TEXT
);

-- One row per observation of one price source at one instant.
--
-- `raw_price` is stored exactly as the source published it, unrounded and
-- unadjusted. Anything derived - the basis, the distance, the rolling mean -
-- sits in its own column so a later correction to the arithmetic can be
-- recomputed without the original ever having been lost.
CREATE TABLE IF NOT EXISTS reference_observations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    window_open_ms INTEGER,
    ticker TEXT,
    source TEXT NOT NULL,          -- 'brti' | 'binance_spot'
    status TEXT NOT NULL,          -- 'ok' | 'missing' | 'stale' | 'error'
    raw_price REAL,                -- as published; NULL when not ok
    event_ms INTEGER,              -- the source's own timestamp for the value
    received_ms INTEGER NOT NULL,  -- when this process had it in hand
    age_ms INTEGER,                -- received_ms - event_ms
    stale INTEGER NOT NULL DEFAULT 0,
    target REAL,                   -- floor_strike, itself a 60s BRTI average
    signed_distance_bps REAL,      -- (raw_price / target - 1) * 10000
    rolling_60s_mean REAL,         -- mean of this source's own last 60s
    rolling_60s_count INTEGER,     -- how many observations that mean is over
    rolling_60s_span_ms INTEGER,   -- oldest to newest in that mean
    basis_bps REAL,                -- this source against the reference source
    reference_source TEXT,         -- which source basis_bps is measured against
    remaining_s INTEGER,
    error TEXT,
    schema_version INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS reference_obs_window
    ON reference_observations(window_open_ms, source, received_ms);
CREATE INDEX IF NOT EXISTS reference_obs_status
    ON reference_observations(status, received_ms);

-- One row per settled market: what we computed against what Kalshi published.
--
-- The decomposition lives here because it is the whole point of the recorder:
--   feed_basis_bps   a 60s Binance mean against the official 60s BRTI mean
--                    -> aggregation matched on both sides, so this is FEED
--   aggregation_bps  a Binance last print against that same Binance 60s mean
--                    -> feed matched on both sides, so this is AVERAGING
-- Adding them recovers the single number section 40 reported, which mixed the
-- two and could therefore attribute neither.
CREATE TABLE IF NOT EXISTS settlement_reconciliation (
    ticker TEXT PRIMARY KEY,
    window_open_ms INTEGER NOT NULL,
    close_ms INTEGER NOT NULL,
    official_expiration_value REAL,  -- Kalshi's published 60s BRTI average
    official_strike REAL,            -- also a 60s BRTI average, at window open
    official_result TEXT,
    computed_brti_mean REAL,         -- our own mean of recorded BRTI ticks
    computed_brti_count INTEGER,
    computed_brti_error REAL,        -- computed - official, in dollars
    computed_brti_error_bps REAL,
    binance_60s_mean REAL,
    binance_60s_count INTEGER,
    binance_last REAL,
    feed_basis_bps REAL,
    aggregation_bps REAL,
    total_bps REAL,
    -- Walk-forward basis correction: the trailing median of EARLIER windows
    -- only. Fitting on the market being scored would be the answer written
    -- down twice. Stored as evidence; nothing in the trading path reads it.
    calibration_bps REAL,
    calibration_window INTEGER,
    calibrated_binance REAL,
    calibrated_error_bps REAL,
    outcome_calibrated INTEGER,
    outcome_official INTEGER,
    outcome_binance_last INTEGER,
    outcome_binance_60s INTEGER,
    reconciled_ms INTEGER NOT NULL,
    note TEXT,
    schema_version INTEGER NOT NULL
);

-- Cache of per-second source data pulled for historical reconciliation, so a
-- rerun costs nothing and a rate limit never corrupts a partial result.
CREATE TABLE IF NOT EXISTS second_bars (
    source TEXT NOT NULL,
    open_ms INTEGER NOT NULL,
    close_price REAL NOT NULL,
    PRIMARY KEY (source, open_ms)
);

-- Every gap, named. A recorder that only writes when the feed works cannot be
-- told apart from a recorder that was switched off.
CREATE TABLE IF NOT EXISTS feed_gaps (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    source TEXT NOT NULL,
    reason TEXT NOT NULL,
    detail TEXT,
    first_ms INTEGER NOT NULL,
    last_ms INTEGER NOT NULL,
    polls INTEGER NOT NULL,
    schema_version INTEGER NOT NULL
);
"""


def _insert(db: sqlite3.Connection, table: str, row: dict) -> None:
    """Named-column insert. The column list comes from the dict keys, so a
    missing key is a missing column rather than a value landing in the wrong
    one - the failure that quarantined 93 shadow rows permanently."""
    columns = ", ".join(row)
    placeholders = ", ".join(f":{name}" for name in row)
    db.execute(f"INSERT INTO {table} ({columns}) VALUES ({placeholders})", row)


class ReferenceStore:
    def __init__(self, path: str) -> None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, timeout=30)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)
        self._migrate()
        self.db.commit()

    def _migrate(self) -> None:
        """Bring an existing file up to `SCHEMA_VERSION`, recording each step.

        A column added later has to be distinguishable from a column that was
        always there and never populated, otherwise a NULL is ambiguous between
        "the feed was down" and "this file predates the column". The version
        history answers that by date.
        """
        current = self.db.execute("PRAGMA user_version").fetchone()[0]
        if current >= SCHEMA_VERSION:
            return
        for version in range(current + 1, SCHEMA_VERSION + 1):
            for statement in MIGRATIONS.get(version, ()):
                try:
                    self.db.execute(statement)
                except sqlite3.OperationalError as exc:
                    # A fresh file created from SCHEMA already has the column.
                    if "duplicate column name" not in str(exc):
                        raise
            _insert(self.db, "schema_version", {
                "version": version,
                "applied_ms": int(time.time() * 1000),
                "note": f"migrated from user_version {current}",
            })
        self.db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    def close(self) -> None:
        self.db.close()

    # ------------------------------------------------------------ recording

    def record_observation(self, row: dict) -> None:
        row.setdefault("schema_version", SCHEMA_VERSION)
        _insert(self.db, "reference_observations", row)
        self.db.commit()

    def record_brti_features(self, row: dict) -> None:
        row.setdefault("schema_version", SCHEMA_VERSION)
        _insert(self.db, "brti_features", row)
        self.db.commit()

    def side_agreement(self) -> dict:
        """How often the two instruments disagree about who is winning.

        The number that justifies the whole migration, measured live rather
        than asserted: FINDINGS 41 found 19.4% at settlement on history.
        """
        row = self.db.execute(
            "SELECT COUNT(*) AS n, COALESCE(SUM(sides_agree = 0), 0) AS disagree "
            "FROM brti_features WHERE binance_side IS NOT NULL"
        ).fetchone()
        return dict(row) if row else {}

    def record_gap(self, row: dict) -> None:
        row.setdefault("schema_version", SCHEMA_VERSION)
        _insert(self.db, "feed_gaps", row)
        self.db.commit()

    def record_reconciliation(self, row: dict) -> None:
        row.setdefault("schema_version", SCHEMA_VERSION)
        row.setdefault("reconciled_ms", int(time.time() * 1000))
        columns = ", ".join(row)
        placeholders = ", ".join(f":{name}" for name in row)
        self.db.execute(
            f"INSERT OR REPLACE INTO settlement_reconciliation ({columns}) "
            f"VALUES ({placeholders})",
            row,
        )
        self.db.commit()

    # --------------------------------------------------------------- bars

    def save_second_bars(self, source: str, bars: list[tuple[int, float]]) -> None:
        self.db.executemany(
            "INSERT OR REPLACE INTO second_bars (source, open_ms, close_price) "
            "VALUES (:source, :open_ms, :close_price)",
            [{"source": source, "open_ms": ms, "close_price": price} for ms, price in bars],
        )
        self.db.commit()

    def second_bars(self, source: str, start_ms: int, end_ms: int) -> list[tuple[int, float]]:
        return [
            (row["open_ms"], row["close_price"])
            for row in self.db.execute(
                "SELECT open_ms, close_price FROM second_bars "
                "WHERE source = ? AND open_ms >= ? AND open_ms < ? ORDER BY open_ms",
                (source, start_ms, end_ms),
            )
        ]

    # ------------------------------------------------------------- reading

    def observations(self, window_open_ms: int, source: str) -> list[sqlite3.Row]:
        return list(self.db.execute(
            "SELECT * FROM reference_observations "
            "WHERE window_open_ms = ? AND source = ? AND status = 'ok' "
            "ORDER BY received_ms",
            (window_open_ms, source),
        ))

    def reconciled_tickers(self) -> set[str]:
        return {
            row["ticker"]
            for row in self.db.execute("SELECT ticker FROM settlement_reconciliation")
        }

    def coverage(self) -> dict:
        row = self.db.execute(
            "SELECT COUNT(*) AS n, "
            "SUM(status = 'ok') AS ok, "
            "SUM(status = 'missing') AS missing, "
            "SUM(status = 'stale') AS stale, "
            "SUM(status = 'error') AS errored "
            "FROM reference_observations"
        ).fetchone()
        return dict(row) if row else {}
