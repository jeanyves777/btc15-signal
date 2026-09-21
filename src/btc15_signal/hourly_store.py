"""Archive for the hourly strike ladder. Shadow mode writes here and nowhere else.

A SEPARATE database file from the live 15-minute `btc15.db`, on purpose. The
trading service writes that one every 12 seconds with real positions in it; a
ladder snapshot is ~40 rows per poll, and a shadow experiment must not be able
to hold a write lock on, or corrupt, the database that knows what money is at
risk. Nothing in the 15-minute path imports this module.

Every row carries `mode`. Today that is always 'shadow'. If hourly trading is
ever switched on, live rows must be distinguishable from shadow rows forever
after - the same boundary lesson as the legacy null `signal_id` rows in the
15-minute archive, which cost a day of re-deriving which conclusions were
allowed to cross it.
"""

import sqlite3
from uuid import uuid4

from .features import _session


class HourlyStore:
    def __init__(self, path: str) -> None:
        self._db = sqlite3.connect(path)
        self._db.row_factory = sqlite3.Row
        # One snapshot of the whole ladder: the things that are true of the
        # chain rather than of any one rung.
        self._db.execute("""
            CREATE TABLE IF NOT EXISTS hourly_chains (
                chain_id TEXT NOT NULL,
                fetched_ms INTEGER NOT NULL,
                mode TEXT NOT NULL,
                session_id TEXT,
                open_ms INTEGER, close_ms INTEGER, remaining_s REAL,
                spot REAL, momentum_5m_bps REAL, volatility_5m_bps REAL,
                session TEXT,
                n_strikes INTEGER, n_quotable INTEGER, n_archived INTEGER,
                skipped_types INTEGER,
                integrity_ok INTEGER,
                arbitrage_pairs INTEGER, worst_arbitrage REAL,
                non_monotonic INTEGER, crossed_books INTEGER,
                stale_seconds REAL, reasons TEXT,
                state TEXT,
                PRIMARY KEY (chain_id, fetched_ms)
            )""")
        # One rung at one instant. Written only for rungs inside the archive
        # window; `n_strikes` vs `n_archived` on the chain row says how many
        # were left out, so the trimming is never silent.
        self._db.execute("""
            CREATE TABLE IF NOT EXISTS hourly_strikes (
                chain_id TEXT NOT NULL,
                fetched_ms INTEGER NOT NULL,
                ticker TEXT NOT NULL,
                strike REAL,
                yes_bid REAL, yes_ask REAL, no_bid REAL, no_ask REAL,
                yes_bid_size REAL, yes_ask_size REAL,
                volume REAL, open_interest REAL,
                spread REAL,
                distance_bps REAL, normalized_distance REAL,
                quotable INTEGER, updated_ms INTEGER,
                PRIMARY KEY (chain_id, fetched_ms, ticker)
            )""")
        # Filled in after the hour settles. One BRTI value decides all 188
        # rungs at once, so the outcome of every rung is derivable from this
        # single number and never needs to be fetched per contract.
        self._db.execute("""
            CREATE TABLE IF NOT EXISTS hourly_settlements (
                chain_id TEXT PRIMARY KEY,
                close_ms INTEGER,
                expiration_value REAL,
                settled_ms INTEGER
            )""")
        self._db.execute(
            "CREATE INDEX IF NOT EXISTS idx_strikes_chain "
            "ON hourly_strikes (chain_id, ticker)"
        )
        self._db.execute(
            "CREATE INDEX IF NOT EXISTS idx_chains_close ON hourly_chains (close_ms)"
        )
        self._db.commit()

    def close(self) -> None:
        self._db.close()

    def record_snapshot(
        self,
        chain,
        integrity,
        *,
        spot: float | None,
        momentum_5m_bps: float | None,
        volatility_5m_bps: float | None,
        archived,
        session_id: str,
        mode: str = "shadow",
        state: str = "WATCHING",
    ) -> int:
        """Write one chain row plus one row per archived rung. Returns rungs written.

        `INSERT OR IGNORE`: polling can revisit the same millisecond after a
        retry, and a duplicate snapshot must not raise inside the service loop.
        """
        self._db.execute(
            "INSERT OR IGNORE INTO hourly_chains VALUES ("
            + ",".join("?" * 23) + ")",
            (
                chain.chain_id, chain.fetched_ms, mode, session_id,
                chain.open_ms, chain.close_ms, chain.remaining_seconds,
                spot, momentum_5m_bps, volatility_5m_bps,
                _session(chain.open_ms),
                len(chain.strikes), integrity.quotable_count, len(archived),
                chain.skipped_types,
                1 if integrity.ok else 0,
                integrity.arbitrage_pairs, integrity.worst_arbitrage,
                integrity.non_monotonic, integrity.crossed_books,
                integrity.stale_seconds, "; ".join(integrity.reasons),
                state,
            ),
        )
        rows = []
        for rung in archived:
            distance_bps = (
                (rung.strike - spot) / spot * 10_000 if spot else None
            )
            normalized = (
                distance_bps / volatility_5m_bps
                if distance_bps is not None and volatility_5m_bps
                else None
            )
            rows.append((
                chain.chain_id, chain.fetched_ms, rung.ticker, rung.strike,
                rung.yes_bid, rung.yes_ask, rung.no_bid, rung.no_ask,
                rung.yes_bid_size, rung.yes_ask_size,
                rung.volume, rung.open_interest, rung.spread,
                distance_bps, normalized,
                1 if rung.quotable else 0, rung.updated_ms,
            ))
        self._db.executemany(
            "INSERT OR IGNORE INTO hourly_strikes VALUES ("
            + ",".join("?" * 17) + ")",
            rows,
        )
        self._db.commit()
        return len(rows)

    def record_settlement(
        self, chain_id: str, close_ms: int, expiration_value: float, settled_ms: int
    ) -> None:
        self._db.execute(
            "INSERT OR REPLACE INTO hourly_settlements VALUES (?,?,?,?)",
            (chain_id, close_ms, expiration_value, settled_ms),
        )
        self._db.commit()

    def unsettled_chains(self, now_ms: int) -> list[tuple[str, int]]:
        """Chains whose hour has ended but whose BRTI has not been recorded."""
        return [
            (r["chain_id"], r["close_ms"])
            for r in self._db.execute(
                "SELECT DISTINCT c.chain_id, c.close_ms FROM hourly_chains c "
                "LEFT JOIN hourly_settlements s ON s.chain_id = c.chain_id "
                "WHERE s.chain_id IS NULL AND c.close_ms < ? "
                "ORDER BY c.close_ms",
                (now_ms - 120_000,),
            )
        ]

    def coverage(self) -> dict:
        """What the archive actually holds. The honest denominator for any claim."""
        row = self._db.execute(
            "SELECT COUNT(*) AS snapshots, COUNT(DISTINCT chain_id) AS chains, "
            "SUM(integrity_ok) AS clean, MIN(fetched_ms) AS first_ms, "
            "MAX(fetched_ms) AS last_ms FROM hourly_chains"
        ).fetchone()
        strikes = self._db.execute(
            "SELECT COUNT(*) AS rows FROM hourly_strikes"
        ).fetchone()
        settled = self._db.execute(
            "SELECT COUNT(*) AS settled FROM hourly_settlements"
        ).fetchone()
        return {
            "snapshots": row["snapshots"] or 0,
            "chains": row["chains"] or 0,
            "clean_snapshots": row["clean"] or 0,
            "strike_rows": strikes["rows"] or 0,
            "settled_chains": settled["settled"] or 0,
            "first_ms": row["first_ms"],
            "last_ms": row["last_ms"],
        }


def new_session_id() -> str:
    return uuid4().hex
