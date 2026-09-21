"""Intelligence ledger - the durable memory of the validation system.

Backtests that only print a summary throw away the evidence. This ledger keeps
every decision snapshot, every simulated trade, the full trigger-to-close price
path of each trade, and every regime finding, so that results accumulate across
runs instead of being recomputed and discarded. Later runs can ask questions the
earlier run did not think to ask.
"""

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from .features import Snapshot
from .lifecycle import Lifecycle

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    strategy TEXT NOT NULL,
    fee_per_contract REAL NOT NULL,
    data_start TEXT, data_end TEXT,
    markets INTEGER, snapshots INTEGER,
    params TEXT, verdict TEXT, summary TEXT
);

CREATE TABLE IF NOT EXISTS snapshots (
    ticker TEXT NOT NULL,
    elapsed INTEGER NOT NULL,
    open_ms INTEGER NOT NULL,
    target REAL, price REAL,
    signed_distance_bps REAL, momentum_5m_bps REAL, volatility_5m_bps REAL,
    normalized_distance REAL, taker_imbalance REAL,
    window_high REAL, window_low REAL, window_range_bps REAL,
    trailing_vol_bps REAL, trailing_trend_bps REAL,
    yes_bid REAL, yes_ask REAL, spread REAL,
    contract_volume REAL, open_interest REAL,
    result TEXT, expiration_value REAL, settle_distance_bps REAL,
    binance_close REAL, oracle_basis_bps REAL,
    session TEXT, weekday TEXT, vol_regime TEXT, trend_regime TEXT,
    liquidity_regime TEXT, distance_regime TEXT,
    PRIMARY KEY (ticker, elapsed)
);
CREATE INDEX IF NOT EXISTS snapshots_open ON snapshots(open_ms);

CREATE TABLE IF NOT EXISTS trades (
    run_id TEXT NOT NULL,
    ticker TEXT NOT NULL,
    strategy TEXT NOT NULL,
    segment TEXT,
    open_ms INTEGER NOT NULL,
    side TEXT NOT NULL,
    entry_minute INTEGER NOT NULL,
    entry_price REAL NOT NULL,
    settled_won INTEGER,
    settlement_pnl REAL,
    mfe REAL, mae REAL,
    peak_price REAL, peak_minute INTEGER,
    trough_price REAL, trough_minute INTEGER,
    final_close REAL,
    rescuable_loss INTEGER, squandered_win INTEGER,
    first_minute_up_2c INTEGER, first_minute_up_5c INTEGER, first_minute_up_10c INTEGER,
    session TEXT, vol_regime TEXT, trend_regime TEXT,
    liquidity_regime TEXT, distance_regime TEXT,
    PRIMARY KEY (run_id, strategy, ticker, entry_minute)
);
CREATE INDEX IF NOT EXISTS trades_run ON trades(run_id);

CREATE TABLE IF NOT EXISTS trade_path (
    run_id TEXT NOT NULL,
    strategy TEXT NOT NULL,
    ticker TEXT NOT NULL,
    entry_minute INTEGER NOT NULL,
    minute INTEGER NOT NULL,
    best REAL, worst REAL, close REAL, volume REAL,
    PRIMARY KEY (run_id, strategy, ticker, entry_minute, minute)
);

CREATE TABLE IF NOT EXISTS policy_results (
    run_id TEXT NOT NULL,
    policy TEXT NOT NULL,
    trades INTEGER, win_rate REAL, total_pnl REAL, roi REAL,
    average_entry REAL, detail TEXT,
    PRIMARY KEY (run_id, policy)
);

CREATE TABLE IF NOT EXISTS findings (
    run_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    dimension TEXT NOT NULL,
    bucket TEXT NOT NULL,
    trades INTEGER, win_rate REAL, roi REAL,
    lower_95 REAL, upper_95 REAL, p_value REAL,
    verdict TEXT, detail TEXT,
    PRIMARY KEY (run_id, kind, dimension, bucket)
);
"""

_SNAPSHOT_COLUMNS = [
    "ticker",
    "elapsed",
    "open_ms",
    "target",
    "price",
    "signed_distance_bps",
    "momentum_5m_bps",
    "volatility_5m_bps",
    "normalized_distance",
    "taker_imbalance",
    "window_high",
    "window_low",
    "window_range_bps",
    "trailing_vol_bps",
    "trailing_trend_bps",
    "yes_bid",
    "yes_ask",
    "spread",
    "contract_volume",
    "open_interest",
    "result",
    "expiration_value",
    "settle_distance_bps",
    "binance_close",
    "oracle_basis_bps",
    "session",
    "weekday",
    "vol_regime",
    "trend_regime",
    "liquidity_regime",
    "distance_regime",
]


@dataclass(frozen=True)
class RunHandle:
    run_id: str
    created_at: str


class Ledger:
    def __init__(self, path: str | Path = "data/intelligence.db") -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path)
        self.db.executescript(SCHEMA)
        self.db.commit()

    # ------------------------------------------------------------------ runs

    def start_run(
        self,
        run_id: str,
        strategy: str,
        fee: float,
        data_start: str | None,
        data_end: str | None,
        markets: int,
        snapshots: int,
        params: dict,
    ) -> RunHandle:
        created = datetime.now(UTC).isoformat()
        self.db.execute(
            "INSERT OR REPLACE INTO runs "
            "(run_id,created_at,strategy,fee_per_contract,data_start,data_end,markets,"
            "snapshots,params,verdict,summary) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                run_id,
                created,
                strategy,
                fee,
                data_start,
                data_end,
                markets,
                snapshots,
                json.dumps(params, sort_keys=True),
                None,
                None,
            ),
        )
        self.db.commit()
        return RunHandle(run_id, created)

    def finish_run(self, run_id: str, verdict: str, summary: dict) -> None:
        self.db.execute(
            "UPDATE runs SET verdict=?, summary=? WHERE run_id=?",
            (verdict, json.dumps(summary, sort_keys=True, default=str), run_id),
        )
        self.db.commit()

    def runs(self, limit: int = 20) -> list[dict]:
        rows = self.db.execute(
            "SELECT run_id,created_at,strategy,fee_per_contract,data_start,data_end,"
            "markets,snapshots,verdict FROM runs ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        keys = [
            "run_id",
            "created_at",
            "strategy",
            "fee",
            "data_start",
            "data_end",
            "markets",
            "snapshots",
            "verdict",
        ]
        return [dict(zip(keys, row, strict=True)) for row in rows]

    # ------------------------------------------------------------- snapshots

    def record_snapshots(self, snapshots: list[Snapshot]) -> int:
        rows = [tuple(getattr(item, name) for name in _SNAPSHOT_COLUMNS) for item in snapshots]
        placeholders = ",".join("?" * len(_SNAPSHOT_COLUMNS))
        self.db.executemany(f"INSERT OR REPLACE INTO snapshots VALUES ({placeholders})", rows)
        self.db.commit()
        return len(rows)

    # ---------------------------------------------------------------- trades

    def record_trades(
        self,
        run_id: str,
        strategy: str,
        entries: list[tuple[Lifecycle, Snapshot, str]],
        store_path: bool = True,
    ) -> int:
        trade_rows = []
        path_rows = []
        for life, snap, segment in entries:
            peak, trough = life.peak, life.trough
            trade_rows.append(
                (
                    run_id,
                    life.ticker,
                    strategy,
                    segment,
                    life.open_ms,
                    life.side,
                    life.entry_minute,
                    life.entry_price,
                    None if life.settled_won is None else int(life.settled_won),
                    round(life.settlement_pnl, 4),
                    round(life.mfe, 4),
                    round(life.mae, 4),
                    peak.best if peak else None,
                    peak.minute if peak else None,
                    trough.worst if trough else None,
                    trough.minute if trough else None,
                    life.final_close,
                    int(life.rescuable_loss()),
                    int(life.squandered_win()),
                    life.ever_profitable_at.get(0.02),
                    life.ever_profitable_at.get(0.05),
                    life.ever_profitable_at.get(0.10),
                    snap.session,
                    snap.vol_regime,
                    snap.trend_regime,
                    snap.liquidity_regime,
                    snap.distance_regime,
                )
            )
            if store_path:
                path_rows.extend(
                    (
                        run_id,
                        strategy,
                        life.ticker,
                        life.entry_minute,
                        point.minute,
                        point.best,
                        point.worst,
                        point.close,
                        point.volume,
                    )
                    for point in life.path
                )
        self.db.executemany(
            "INSERT OR REPLACE INTO trades VALUES (" + ",".join("?" * 27) + ")", trade_rows
        )
        if path_rows:
            self.db.executemany(
                "INSERT OR REPLACE INTO trade_path VALUES (?,?,?,?,?,?,?,?,?)", path_rows
            )
        self.db.commit()
        return len(trade_rows)

    # -------------------------------------------------------------- analysis

    def record_policy(self, run_id: str, policy: str, summary: dict) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO policy_results VALUES (?,?,?,?,?,?,?,?)",
            (
                run_id,
                policy,
                summary.get("trades", 0),
                summary.get("win_rate"),
                summary.get("total_pnl"),
                summary.get("roi"),
                summary.get("average_entry"),
                json.dumps(summary.get("exit_reasons", {}), sort_keys=True),
            ),
        )
        self.db.commit()

    @staticmethod
    def _stat(stats: dict, *names):
        """First present key among `names`.

        Regime rows and hypothesis rows describe the same quantities under
        different names (roi/gross_roi, lower_95/edge_ci_low). Reading only one
        spelling silently stored NULL for every hypothesis row and made the
        findings query crash on format.
        """
        for name in names:
            if stats.get(name) is not None:
                return stats[name]
        return None

    def record_finding(self, run_id: str, kind: str, dimension: str, bucket: str, stats: dict):
        self.db.execute(
            "INSERT OR REPLACE INTO findings VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                run_id,
                kind,
                dimension,
                bucket,
                self._stat(stats, "trades"),
                self._stat(stats, "win_rate"),
                self._stat(stats, "roi", "gross_roi"),
                self._stat(stats, "lower_95", "edge_ci_low"),
                self._stat(stats, "upper_95", "edge_ci_high"),
                self._stat(stats, "p_value"),
                stats.get("verdict"),
                json.dumps(stats.get("detail", {}), sort_keys=True, default=str),
            ),
        )
        self.db.commit()

    def findings(self, run_id: str | None = None, kind: str | None = None) -> list[dict]:
        clauses, params = [], []
        if run_id:
            clauses.append("run_id=?")
            params.append(run_id)
        if kind:
            clauses.append("kind=?")
            params.append(kind)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        rows = self.db.execute(
            "SELECT run_id,kind,dimension,bucket,trades,win_rate,roi,lower_95,upper_95,"
            f"p_value,verdict,detail FROM findings{where} ORDER BY kind, dimension, bucket",
            params,
        ).fetchall()
        keys = [
            "run_id",
            "kind",
            "dimension",
            "bucket",
            "trades",
            "win_rate",
            "roi",
            "lower_95",
            "upper_95",
            "p_value",
            "verdict",
            "detail",
        ]
        return [dict(zip(keys, row, strict=True)) for row in rows]

    def trade_rows(self, run_id: str) -> list[dict]:
        cursor = self.db.execute("SELECT * FROM trades WHERE run_id=?", (run_id,))
        keys = [column[0] for column in cursor.description]
        return [dict(zip(keys, row, strict=True)) for row in cursor.fetchall()]
