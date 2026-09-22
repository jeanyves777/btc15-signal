import math
import sqlite3
import time
from dataclasses import dataclass
from uuid import uuid4


@dataclass(frozen=True)
class Calibration:
    samples: int
    wins: int
    observed_rate: float
    lower_bound: float


@dataclass(frozen=True)
class TradeProposal:
    id: str
    strategy: str
    window_open: int
    ticker: str
    side: str
    entry_limit: float
    take_profit: float
    count: int
    expires_at: int
    close_ms: int
    status: str


# Every status that means "contracts were bought and are ours".
#
# execute_with_take_profit returns 'filled' for a plain buy, but 'protected'
# when a take-profit order rests behind it and 'unprotected' when the entry
# filled and the take-profit placement failed. All three are real positions.
# Listing only 'filled' made the other two invisible to the daily loss floor
# and to the one-position guard - a live position the safety limits could not
# see. 'unprotected' is the most dangerous of the three to lose track of.
HELD_STATUSES = ("filled", "protected", "unprotected")
HELD_SQL = "('filled','protected','unprotected')"
ACCOUNTED_SQL = "('filled','protected','unprotected','exited')"


def _hour_utc(window_open: int) -> int:
    from datetime import UTC, datetime

    return datetime.fromtimestamp(window_open / 1000, UTC).hour


def position_pnl(
    *,
    paid: float,
    count: float,
    entry_fee: float | None,
    exit_price: float | None,
    exit_count: float | None,
    won: bool | None,
) -> float | None:
    """Realised dollars for one position, sold leg and held leg together.

    A reduce-only IOC exit can fill PARTIALLY: sell 10 of 22 and the other 12
    stay live to settlement. Scoring only the sold leg left those 12 outside
    the daily loss floor entirely - a reproduced case sat $15.11 down while the
    floor read $4.24 and happily kept trading.

    Returns None when the held leg has not settled yet, because a position that
    is still open has no realised figure to report.

    One function, three callers - the loss floor, the account record and the
    dashboard - because when they each had their own copy they disagreed about
    the same trade.
    """
    from .validation import kalshi_fee_charged

    sold = exit_count or 0.0
    held = max(0.0, (count or 0.0) - sold)
    fee_in = entry_fee if entry_fee is not None else kalshi_fee_charged(paid, count or 0.0)

    total = -fee_in
    if sold and exit_price is not None:
        total += sold * (exit_price - paid) - kalshi_fee_charged(exit_price, sold)
    if held:
        if won is None:
            return None  # still open; nothing realised yet
        total += held * ((1.0 if won else 0.0) - paid)
    return total


class Store:
    def _add_columns(self, table: str, columns: dict[str, str]) -> None:
        """Add any missing columns to an existing table, idempotently.

        Live databases predate most of these tables' current shapes, and
        `CREATE TABLE IF NOT EXISTS` will not widen one that already exists.
        Done by name rather than by rewriting the table so an upgrade cannot
        lose rows, and checked against `PRAGMA table_info` so re-running it is
        free.
        """
        existing = {row[1] for row in self.db.execute(f"PRAGMA table_info({table})")}
        for name, kind in columns.items():
            if name not in existing:
                self.db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {kind}")

    def __init__(self, path: str) -> None:
        self.db = sqlite3.connect(path)
        self.db.execute("""
            CREATE TABLE IF NOT EXISTS predictions (
                window_open INTEGER PRIMARY KEY, created_at INTEGER NOT NULL,
                target REAL NOT NULL, entry_price REAL NOT NULL, side TEXT NOT NULL,
                bucket INTEGER NOT NULL, raw_probability REAL NOT NULL,
                contract_ticker TEXT, final_price REAL, won INTEGER
            )
        """)
        columns = {row[1] for row in self.db.execute("PRAGMA table_info(predictions)")}
        if "contract_ticker" not in columns:
            self.db.execute("ALTER TABLE predictions ADD COLUMN contract_ticker TEXT")
        # The contract ask we were quoted, and whether the rule would actually
        # have entered. Without these a settlement report can say who won but
        # not what it would have paid, which is the part that matters.
        if "contract_price" not in columns:
            self.db.execute("ALTER TABLE predictions ADD COLUMN contract_price REAL")
        if "qualified" not in columns:
            self.db.execute("ALTER TABLE predictions ADD COLUMN qualified INTEGER DEFAULT 0")
        # WHICH gates rejected it, not merely that something did. Without this
        # a rejected signal is a dead end: you can see the rule said no and that
        # the market went our way anyway, but not whether the price floor, the
        # distance gate or the momentum gate was the one that cost you.
        if "failed_gates" not in columns:
            self.db.execute("ALTER TABLE predictions ADD COLUMN failed_gates TEXT")
        # What the similarity layer WOULD have decided, recorded and never
        # acted on. Promotion to an execution policy requires this table to
        # show its calls beating the deployed rule on realised P&L - which is
        # the only way to find out whether retrieval adds anything or merely
        # sounds clever.
        #
        # Two of these columns are easy to misread, so: `rule_qualified` is the
        # rule's verdict AT THIS ROW'S POLL, not the window's - the rule can
        # say no at the alert and yes two minutes later at the price we
        # actually bought, and one row cannot hold both. `traded` is the
        # opposite: a WINDOW-level fact, stamped on every row of the window by
        # `record_fill` once a real position exists. Group by window_open
        # before counting a head-to-head, or a traded window is counted twice.
        self.db.execute("""
            CREATE TABLE IF NOT EXISTS shadow_decisions (
                window_open INTEGER NOT NULL, created_at INTEGER NOT NULL,
                ticker TEXT, side TEXT, remaining_s INTEGER,
                ask REAL, cohort_n INTEGER, win_probability REAL,
                raw_win_rate REAL, net_edge_now REAL,
                enter_now_net REAL, wait_limit_net REAL, wait_real_net REAL,
                dip_price REAL, dip_rate REAL, dip_n INTEGER, mean_drift REAL,
                win_low REAL, win_high REAL, edge_low REAL, prior REAL,
                wait_limit_low REAL,
                ran_away_rate REAL, session TEXT, vol_regime TEXT,
                action TEXT, reason TEXT,
                rule_qualified INTEGER, traded INTEGER, won INTEGER,
                PRIMARY KEY (window_open, remaining_s)
            )
        """)
        # Added after the table shipped. Provenance and linkage: without them a
        # recorded recommendation cannot be tied to the observation it was made
        # from, or to the corpus and rule versions that produced it, so it can
        # never be re-derived or audited - and a graded number nobody can
        # reproduce is not evidence.
        self._add_columns("shadow_decisions", {
            "observation_id": "INTEGER", "signal_id": "TEXT", "market_id": "TEXT",
            "model_version": "TEXT", "feature_schema": "TEXT",
            "training_cutoff_ms": "INTEGER", "strategy_version": "TEXT",
            "fill_probability": "REAL", "pass_net": "REAL",
            "effective_n": "REAL", "neighbour_ids": "TEXT",
            "neighbour_scores": "TEXT", "regime_contribution": "REAL",
            "execution_risk": "REAL", "latency_ms": "REAL",
            "baseline_probability": "REAL", "mode": "TEXT",
            "realised_pnl": "REAL", "filled": "INTEGER",
            # PERMANENT. Set once on the rows the old positional insert
            # shifted, and never cleared: a quarantined row is evidence of the
            # bug, not evidence about the market, and the reason it is marked
            # rather than deleted is that deleting it would make the archive
            # look clean and the sample look merely small.
            "quarantined": "INTEGER DEFAULT 0",
            "quarantine_reason": "TEXT",
        })
        self.db.execute("""
            CREATE TABLE IF NOT EXISTS strategy_alerts (
                strategy TEXT NOT NULL, window_open INTEGER NOT NULL,
                created_at INTEGER NOT NULL,
                PRIMARY KEY (strategy, window_open)
            )
        """)
        # The exchange's own record of every settled market. Realised P&L is
        # READ from here, not rebuilt: see KalshiExecutionClient.settlements.
        self.db.execute("""
            CREATE TABLE IF NOT EXISTS settlements (
                ticker TEXT PRIMARY KEY, event_ticker TEXT, market_result TEXT,
                yes_count REAL, yes_cost REAL, no_count REAL, no_cost REAL,
                revenue_cents INTEGER, fee_cost REAL, pnl REAL,
                settled_ms INTEGER, synced_at INTEGER, window_ms INTEGER
            )
        """)
        # `window_ms` was added after the table shipped: settled_ms is hours
        # later than the market itself, so grouping by it files a trade under
        # the wrong day. See KalshiExecutionClient.market_open_ms.
        if "window_ms" not in {
            r[1] for r in self.db.execute("PRAGMA table_info(settlements)")
        }:
            self.db.execute("ALTER TABLE settlements ADD COLUMN window_ms INTEGER")
        # What the 📋 DETAILS button shows. Written at decision time and read
        # back verbatim, never recomputed: re-deriving the context when the
        # button is pressed would describe a market that has already moved,
        # and the whole point of the button is the audit trail of the moment
        # the call was made.
        self.db.execute("""
            CREATE TABLE IF NOT EXISTS decision_details (
                key TEXT PRIMARY KEY, body TEXT NOT NULL, created_at INTEGER NOT NULL
            )
        """)
        # The broker's own record of every execution. The trade COUNT comes
        # from here, not from `trade_proposals`, which records intentions.
        self.db.execute("""
            CREATE TABLE IF NOT EXISTS fills (
                fill_id TEXT PRIMARY KEY, ticker TEXT NOT NULL, order_id TEXT,
                action TEXT, side TEXT, count REAL, yes_price REAL, no_price REAL,
                fee_cost REAL, is_taker INTEGER, filled_ms INTEGER,
                window_ms INTEGER, synced_at INTEGER
            )
        """)
        self.db.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY, value REAL NOT NULL, updated_at INTEGER NOT NULL
            )
        """)
        self.db.execute("""
            CREATE TABLE IF NOT EXISTS reversion_signals (
                window_open INTEGER PRIMARY KEY, created_at INTEGER NOT NULL,
                ticker TEXT NOT NULL, side TEXT NOT NULL, entry_price REAL NOT NULL,
                take_profit REAL NOT NULL, key_level REAL, spike_bps REAL,
                rejection_bps REAL, remaining_s INTEGER, won INTEGER
            )
        """)
        self.db.execute("""
            CREATE TABLE IF NOT EXISTS trade_proposals (
                id TEXT PRIMARY KEY, strategy TEXT NOT NULL, window_open INTEGER NOT NULL,
                ticker TEXT NOT NULL, side TEXT NOT NULL, entry_limit REAL NOT NULL,
                take_profit REAL NOT NULL, count INTEGER NOT NULL, expires_at INTEGER NOT NULL,
                close_ms INTEGER NOT NULL, status TEXT NOT NULL, created_at INTEGER NOT NULL,
                entry_order_id TEXT, take_profit_order_id TEXT, result_note TEXT,
                attempt INTEGER NOT NULL DEFAULT 1,
                UNIQUE(strategy, window_open, attempt)
            )
        """)
        # Each entry ATTEMPT needs its own row and its own id, all linked to the
        # same window. The original UNIQUE(strategy, window_open) plus
        # INSERT OR IGNORE meant a second attempt was silently dropped - so the
        # retry path could never actually place an order, and would have failed
        # quietly rather than loudly. SQLite cannot drop an inline constraint,
        # so the table is rebuilt once.
        existing = {row[1] for row in self.db.execute("PRAGMA table_info(trade_proposals)")}
        if existing and "attempt" not in existing:
            self.db.executescript("""
                PRAGMA foreign_keys=off;
                BEGIN;
                CREATE TABLE trade_proposals_new (
                    id TEXT PRIMARY KEY, strategy TEXT NOT NULL,
                    window_open INTEGER NOT NULL, ticker TEXT NOT NULL,
                    side TEXT NOT NULL, entry_limit REAL NOT NULL,
                    take_profit REAL NOT NULL, count INTEGER NOT NULL,
                    expires_at INTEGER NOT NULL, close_ms INTEGER NOT NULL,
                    status TEXT NOT NULL, created_at INTEGER NOT NULL,
                    entry_order_id TEXT, take_profit_order_id TEXT, result_note TEXT,
                    exit_price REAL, exit_count REAL, fill_price REAL, fee_paid REAL,
                    attempt INTEGER NOT NULL DEFAULT 1,
                    UNIQUE(strategy, window_open, attempt)
                );
                INSERT INTO trade_proposals_new
                    (id,strategy,window_open,ticker,side,entry_limit,take_profit,count,
                     expires_at,close_ms,status,created_at,entry_order_id,
                     take_profit_order_id,result_note,exit_price,exit_count,
                     fill_price,fee_paid,attempt)
                SELECT id,strategy,window_open,ticker,side,entry_limit,take_profit,count,
                       expires_at,close_ms,status,created_at,entry_order_id,
                       take_profit_order_id,result_note,exit_price,exit_count,
                       fill_price,fee_paid,1
                FROM trade_proposals;
                DROP TABLE trade_proposals;
                ALTER TABLE trade_proposals_new RENAME TO trade_proposals;
                COMMIT;
                PRAGMA foreign_keys=on;
            """)

        # Every observation, not only the ones that alerted.
        #
        # `predictions` holds one row per window and only when something was
        # worth saying, which makes it useless for finding a strategy we do not
        # already have: the minutes we never alerted on are exactly where a
        # different edge would live. This keeps the full feature vector at every
        # poll inside the entry window, settled with the outcome afterwards, so
        # a future rule can be fitted and walk-forward tested on live data
        # rather than only on the historical dump.
        self.db.execute("""
            CREATE TABLE IF NOT EXISTS executions (
                proposal_id TEXT PRIMARY KEY,
                signal_id TEXT, session_id TEXT,
                ticker TEXT, side TEXT, window_open INTEGER, attempt INTEGER,
                decision_ask REAL, limit_submitted REAL,
                decision_ms INTEGER, submitted_ms INTEGER, acked_ms INTEGER,
                decision_to_submit_ms INTEGER, round_trip_ms INTEGER,
                filled INTEGER, fill_price REAL, fill_count REAL,
                ask_after REAL, timing TEXT
            )""")
        self.db.execute("""
            CREATE TABLE IF NOT EXISTS observations (
                window_open INTEGER NOT NULL, remaining_s INTEGER NOT NULL,
                observed_ms INTEGER NOT NULL, ticker TEXT,
                target REAL, btc REAL, side TEXT,
                raw_probability REAL, bucket INTEGER,
                our_ask REAL, yes_ask REAL, no_ask REAL,
                momentum_5m_bps REAL, volatility_5m_bps REAL,
                distance_bps REAL, normalized_distance REAL,
                spread_bps REAL, futures_basis_bps REAL, taker_imbalance REAL,
                window_high REAL, window_low REAL, elapsed_minutes REAL,
                rule_match INTEGER, failed_gates TEXT,
                won INTEGER, final_price REAL,
                PRIMARY KEY (window_open, remaining_s)
            )
        """)
        self.db.execute(
            "CREATE INDEX IF NOT EXISTS observations_unsettled "
            "ON observations(won, window_open)"
        )
        # Added after the first archive proved too thin: it covered only the
        # entry scan, so the path from entry to exit - the part that says
        # whether a position was ever in profit and when - was missing, and it
        # carried no order book and no regime label at all.
        observation_columns = {
            row[1] for row in self.db.execute("PRAGMA table_info(observations)")
        }
        for column, kind in (
            ("yes_bid", "REAL"), ("exit_bid", "REAL"),
            ("book_yes_depth", "REAL"), ("book_no_depth", "REAL"),
            ("book_yes_share", "REAL"), ("book_bid_size", "REAL"),
            ("book_ask_size", "REAL"), ("book_levels", "INTEGER"),
            ("depth_bid_qty", "REAL"), ("depth_ask_qty", "REAL"),
            ("trade_count", "INTEGER"), ("buy_volume", "REAL"),
            ("sell_volume", "REAL"), ("vwap", "REAL"),
            ("open_interest", "REAL"), ("volume", "REAL"),
            ("session", "TEXT"), ("weekday", "TEXT"), ("hour_utc", "INTEGER"),
            ("vol_regime", "TEXT"),
            ("holding", "INTEGER"), ("entry_paid", "REAL"), ("unrealised", "REAL"),
            ("book_age_s", "REAL"),
            # --- linkage. Without these the archive is a pile of rows rather
            # than reconstructable paths: you can see what happened at 07:41
            # but not which opportunity it belonged to, nor which service run
            # produced it, nor whether the trade that followed was the same one.
            ("session_id", "TEXT"),   # one service run; survives nothing, by design
            ("market_id", "TEXT"),    # the Kalshi ticker: globally unique
            ("signal_id", "TEXT"),    # one opportunity, stable across restarts
            ("proposal_id", "TEXT"),  # the order, once one exists
            # --- what we did and what came of it
            ("alerted", "INTEGER"),
            ("order_state", "TEXT"),  # none | proposed | filled | exited
            ("exit_price", "REAL"),
            ("exit_reason", "TEXT"),
            ("realised_pnl", "REAL"),
            # --- regime, archived so the hour question keeps being measured
            # rather than re-argued from single days. Recorded, never enforced:
            # time-of-day moves the confidence EXPLANATION and nothing else.
            ("regime_weight", "REAL"),
            ("confidence_adjustment", "INTEGER"),
        ):
            if column not in observation_columns:
                self.db.execute(f"ALTER TABLE observations ADD COLUMN {column} {kind}")

        # Where the time between reading a price and sending the order went.
        # Two guesses at that gap have already been wrong; this records it
        # instead of reasoning about it.
        execution_columns = {
            row[1] for row in self.db.execute("PRAGMA table_info(executions)")
        }
        if "timing" not in execution_columns:
            self.db.execute("ALTER TABLE executions ADD COLUMN timing TEXT")

        shadow_columns = {
            row[1] for row in self.db.execute("PRAGMA table_info(shadow_decisions)")
        }
        for column, kind in (
            ("dip_n", "INTEGER"), ("win_low", "REAL"), ("win_high", "REAL"),
            ("edge_low", "REAL"), ("prior", "REAL"), ("wait_limit_low", "REAL"),
        ):
            if shadow_columns and column not in shadow_columns:
                self.db.execute(
                    f"ALTER TABLE shadow_decisions ADD COLUMN {column} {kind}"
                )

        proposal_columns = {
            row[1] for row in self.db.execute("PRAGMA table_info(trade_proposals)")
        }
        # What an early exit actually realised. Without these a position sold on
        # a broken thesis has no recorded price, so its loss is invisible to the
        # daily loss floor - the one control that stops a bad night unattended.
        if "exit_price" not in proposal_columns:
            self.db.execute("ALTER TABLE trade_proposals ADD COLUMN exit_price REAL")
        if "exit_count" not in proposal_columns:
            self.db.execute("ALTER TABLE trade_proposals ADD COLUMN exit_count REAL")
        # The price actually paid and the fee actually charged, as opposed to
        # the limit we posted and the fee our formula predicts.
        if "fill_price" not in proposal_columns:
            self.db.execute("ALTER TABLE trade_proposals ADD COLUMN fill_price REAL")
        if "fee_paid" not in proposal_columns:
            self.db.execute("ALTER TABLE trade_proposals ADD COLUMN fee_paid REAL")
        self.db.commit()

    def proposal_created_at(self, proposal_id: str) -> int | None:
        """When the proposal was written, i.e. when the price was judged.

        Read from the row rather than the dataclass: `TradeProposal` does not
        carry `created_at`, and reaching for it raises in the order path.
        """
        row = self.db.execute(
            "SELECT created_at FROM trade_proposals WHERE id=?", (proposal_id,)
        ).fetchone()
        return row[0] if row else None

    def record_execution(self, row: dict) -> None:
        """One submitted order, with everything needed to judge execution.

        A backtest credits a fill at the price it saw. Live, 9 of 21 orders
        filled. Nothing measured from historical quotes can say which - the
        candles record what the market did, not what OUR order got - so the
        only way to learn the difference is to write down every attempt as it
        happens. That is what this is for.

        Deliberately does NOT read the book immediately before submitting.
        That would add a network round trip to the critical path and make the
        staleness it is trying to measure worse. `decision_to_submit_ms` -
        how long the price sat between being judged and being ordered on - is
        the free version of the same measurement. The post-failure book read
        IS taken, because by then the order has already missed and the read
        costs nothing.
        """
        self.db.execute(
            "INSERT OR REPLACE INTO executions VALUES ("
            + ",".join("?" * 19) + ")",
            (
                row.get("proposal_id"), row.get("signal_id"), row.get("session_id"),
                row.get("ticker"), row.get("side"), row.get("window_open"),
                row.get("attempt"),
                row.get("decision_ask"), row.get("limit_submitted"),
                row.get("decision_ms"), row.get("submitted_ms"), row.get("acked_ms"),
                row.get("decision_to_submit_ms"), row.get("round_trip_ms"),
                1 if row.get("filled") else 0,
                row.get("fill_price"), row.get("fill_count"),
                row.get("ask_after"), row.get("timing"),
            ),
        )
        self.db.commit()

    def execution_report(self) -> dict:
        """Fill rate and timing, which is the honest denominator for any edge."""
        row = self.db.execute(
            "SELECT COUNT(*) n, SUM(filled) filled, "
            "AVG(decision_to_submit_ms) avg_decide, AVG(round_trip_ms) avg_trip "
            "FROM executions"
        ).fetchone()
        return {
            "orders": row[0] or 0,
            "filled": row[1] or 0,
            "fill_rate": (row[1] / row[0]) if row[0] else None,
            "avg_decision_to_submit_ms": row[2],
            "avg_round_trip_ms": row[3],
        }

    def record(self, values: tuple) -> bool:
        # `failed_gates` was added later, so a caller may still pass the older
        # ten-field tuple. Padding here rather than demanding every call site
        # change keeps an older build from failing to record a signal at all -
        # the same class of break that once killed the live service.
        if len(values) == 10:
            values = (*values, None)
        cursor = self.db.execute(
            "INSERT OR IGNORE INTO predictions "
            "(window_open,created_at,target,entry_price,side,bucket,raw_probability,"
            "contract_ticker,contract_price,qualified,failed_gates) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            values,
        )
        self.db.commit()
        return cursor.rowcount == 1

    def pending_settlements(self, now_ms: int) -> list[tuple]:
        """(window_open, side, ticker, contract_price, qualified, target) awaiting a result."""
        return self.db.execute(
            "SELECT window_open,side,contract_ticker,contract_price,qualified,target "
            "FROM predictions "
            "WHERE won IS NULL AND contract_ticker IS NOT NULL AND window_open + 900000 <= ?",
            (now_ms,),
        ).fetchall()

    def scoreboard(
        self,
        contracts: float | None = None,
        cash: float | None = None,
        qualified_only: bool = False,
    ) -> tuple[int, int, float]:
        """(settled, wins, net dollars) sizing every scored signal the same way.

        The caller must say whether it means a contract count or a cash amount.
        They give identical ROI but very different dollars: at 64c a losing
        10-contract position costs $6.40, a losing $10 cash position costs $10.
        Defaulting silently to one of them misstates the money at risk.
        """
        from .validation import trade_pnl

        if contracts is None and cash is None:
            contracts = 1.0
        clause = " AND qualified=1" if qualified_only else ""
        rows = self.db.execute(
            "SELECT won, contract_price FROM predictions "
            f"WHERE won IS NOT NULL AND contract_price IS NOT NULL{clause}"
        ).fetchall()
        if not rows:
            return 0, 0, 0.0
        wins = sum(1 for won, _ in rows if won)
        pnl = sum(
            trade_pnl(price, bool(won), contracts=contracts, cash=cash)
            for won, price in rows
            if price
        )
        return len(rows), wins, pnl

    def trade_for_window(self, window_open: int) -> dict | None:
        """The real order behind a signal, or None if it was never traded.

        Most settled signals have no order at all. Settlement reporting needs to
        tell those apart from real ones, because a dollar figure attached to a
        trade nobody placed is a claim about money that never moved.
        """
        row = self.db.execute(
            f"SELECT status, count, fill_price, COALESCE(fill_price, entry_limit), "
            f"fee_paid, exit_price, exit_count, side FROM trade_proposals "
            f"WHERE window_open=? AND strategy='primary' AND status IN {ACCOUNTED_SQL}",
            (window_open,),
        ).fetchone()
        if not row:
            return None
        status, count, raw_fill, paid, fee, exit_price, exit_count, side = row
        return {
            "status": status,
            # The side actually HELD. `settle_observations` scores the realised
            # figure on this rather than on each observation's own side, and
            # reading a key that was never here crashed the service at every
            # settlement that followed a fill.
            "side": side,
            # False when the fill was never read back and `paid` is standing in
            # from the posted limit. A buy limit only ever fills at or below
            # itself, so an unconfirmed price always overstates the cost.
            "confirmed": raw_fill is not None,
            "count": count or 0.0,
            "paid": paid,
            "fee": fee,
            "exit_price": exit_price,
            "exit_count": exit_count,
            "exited": status == "exited" and exit_price is not None,
        }

    def band_streak_seconds(
        self, window_open: int, low: float, high: float, now_ms: int
    ) -> float:
        """How long the price has sat CONTINUOUSLY inside the band, in seconds.

        Measured from the archive rather than held in memory, so a restart does
        not reset it and hand a fresh entry to a price that has not settled.

        Entering on the first qualifying minute measured +0.0080/contract with a
        95% CI of [-0.0019, +0.0180] - indistinguishable from zero. Waiting for
        two minutes in the band measured +0.0219 [+0.0090, +0.0347]. The first
        qualifying minute is the worst moment to buy: the price is still moving,
        which is why it is both hard to fill and worth little.
        """
        rows = self.db.execute(
            "SELECT observed_ms, our_ask FROM observations "
            "WHERE window_open=? AND our_ask IS NOT NULL "
            "ORDER BY observed_ms DESC LIMIT 60",
            (window_open,),
        ).fetchall()
        if not rows:
            return 0.0
        earliest = None
        for observed_ms, ask in rows:
            if not low <= ask <= high:
                break  # the streak ended here
            earliest = observed_ms
        if earliest is None:
            return 0.0
        return max(0.0, (now_ms - earliest) / 1000)

    def lifecycle_by_signal(self, signal_id: str) -> list[dict]:
        """One opportunity's whole path, oldest first, however many runs it spans.

        Keyed on signal_id rather than window_open so a restart mid-window does
        not split the path in two: the id is derived from the ticker and is the
        same before and after.
        """
        cursor = self.db.execute(
            "SELECT * FROM observations WHERE signal_id=? ORDER BY observed_ms ASC",
            (signal_id,),
        )
        names = [d[0] for d in cursor.description]
        return [dict(zip(names, row, strict=True)) for row in cursor.fetchall()]

    def observe_full(self, row: dict) -> None:
        """Save one lifecycle observation from a dict of named columns.

        Named rather than positional because this row has grown past thirty
        fields and a positional tuple at that width is a silent-corruption
        waiting to happen: swap two of them and every later study is wrong with
        nothing to notice.

        Unknown keys are dropped rather than raising, so an older build writing
        a shorter row still records something useful instead of failing.
        """
        allowed = {r[1] for r in self.db.execute("PRAGMA table_info(observations)")}
        data = {k: v for k, v in row.items() if k in allowed}
        if not data:
            return
        columns = ",".join(data)
        marks = ",".join("?" * len(data))
        self.db.execute(
            f"INSERT OR REPLACE INTO observations ({columns}) VALUES ({marks})",
            tuple(data.values()),
        )
        self.db.commit()

    def lifecycle(self, window_open: int) -> list[dict]:
        """Every observation of one window, entry to expiry, newest last.

        This is the per-signal record: what the market, the book and our own
        position looked like at each poll, so a settled trade can be replayed
        rather than reduced to a single win or loss.
        """
        cursor = self.db.execute(
            "SELECT * FROM observations WHERE window_open=? ORDER BY remaining_s DESC",
            (window_open,),
        )
        names = [d[0] for d in cursor.description]
        return [dict(zip(names, row, strict=True)) for row in cursor.fetchall()]

    def observe(self, values: tuple) -> None:
        """Save one decision-time observation. Never raises on a duplicate."""
        self.db.execute(
            "INSERT OR IGNORE INTO observations "
            "(window_open,remaining_s,observed_ms,ticker,target,btc,side,"
            "raw_probability,bucket,our_ask,yes_ask,no_ask,momentum_5m_bps,"
            "volatility_5m_bps,distance_bps,normalized_distance,spread_bps,"
            "futures_basis_bps,taker_imbalance,window_high,window_low,"
            "elapsed_minutes,rule_match,failed_gates) "
            "VALUES (" + ",".join("?" * 24) + ")",
            values,
        )
        self.db.commit()

    def last_observed_btc(self, window_open: int) -> float | None:
        """The last BTC print seen in a window - the settlement price we saw.

        `final_price` was being fed the STRIKE, so all 2,383 settled rows
        recorded the target and the actual settlement price existed nowhere in
        the database.
        """
        row = self.db.execute(
            "SELECT btc FROM observations WHERE window_open=? AND btc IS NOT NULL "
            "ORDER BY remaining_s ASC LIMIT 1",
            (window_open,),
        ).fetchone()
        return float(row[0]) if row and row[0] is not None else None

    def settle_observations(
        self, window_open: int, winning_side: str, final_price: float
    ) -> int:
        """Stamp every observation of a window with what actually happened.

        PER ROW, on that row's OWN side. The model's side flips whenever BTC
        crosses the strike mid-window, so a single window holds both UP and
        DOWN rows. Stamping one boolean across all of them wrote the
        PREDICTION's outcome onto rows that had bet the other way: 432 of
        2,527 settled rows - 17.1% of the archive - carried an inverted `won`.
        Every live-tape study that read this column inherited that error.

        Takes the winning SIDE rather than a boolean for exactly that reason:
        a boolean cannot be correct for two different sides at once.
        """
        realised = None
        trade = self.trade_for_window(window_open)
        if trade:
            # The realised figure belongs to the position we actually held, so
            # it is scored on the TRADED side, not on each observation's side.
            realised = position_pnl(
                paid=trade["paid"], count=trade["count"], entry_fee=trade["fee"],
                exit_price=trade["exit_price"], exit_count=trade["exit_count"],
                won=trade["side"] == winning_side,
            )
        try:
            cursor = self.db.execute(
                "UPDATE observations SET won=CASE WHEN side=? THEN 1 ELSE 0 END, "
                "final_price=?, realised_pnl=? "
                "WHERE window_open=? AND won IS NULL",
                (winning_side, final_price, realised, window_open),
            )
            self.db.commit()
        except sqlite3.Error as exc:
            # This is ONE-SHOT: `won IS NULL` means a window that fails here is
            # never offered again, so the outcome is lost for good. It could
            # also take the service down with it - the cycle handler in main.py
            # catches httpx/RuntimeError/ValueError/OSError and NOT
            # sqlite3.Error - so the stamp now survives its own failure and
            # says which window it lost, rather than losing it in silence.
            print(
                f"settle: observations for window {window_open} were NOT "
                f"stamped: {type(exc).__name__}: {exc}",
                flush=True,
            )
            return 0
        # Stamped nothing although the window has rows: they already carry an
        # outcome, so this result went on the floor and, being one-shot, will
        # never be applied. That silence is how 432 inverted rows sat in the
        # archive unnoticed until someone went looking for them.
        if cursor.rowcount == 0 and self.db.execute(
            "SELECT 1 FROM observations WHERE window_open=? LIMIT 1",
            (window_open,),
        ).fetchone():
            print(
                f"settle: window {window_open} already carried an outcome; "
                f"the {winning_side} result was applied to no row",
                flush=True,
            )
        return cursor.rowcount

    # Rows written before the linkage columns existed carry no signal_id. They
    # are real observations and fine to keep, but a lifecycle cannot be
    # reconstructed from them and they must never be pooled with linked rows:
    # doing so would silently mix "this path" with "some rows from around then".
    LEGACY = "signal_id IS NULL"
    LINKED = "signal_id IS NOT NULL"

    def observation_coverage(self) -> dict:
        """How much research data exists, and how much of it is usable yet.

        Reports the legacy boundary explicitly rather than leaving it to be
        remembered. `research_from_ms` is the first fully linked observation;
        anything earlier can be described but not reconstructed.
        """
        total, settled, windows, first, last = self.db.execute(
            "SELECT COUNT(*), COALESCE(SUM(won IS NOT NULL),0), "
            "COUNT(DISTINCT window_open), MIN(observed_ms), MAX(observed_ms) "
            "FROM observations"
        ).fetchone()
        linked, linked_windows, boundary = self.db.execute(
            f"SELECT COUNT(*), COUNT(DISTINCT signal_id), MIN(observed_ms) "
            f"FROM observations WHERE {self.LINKED}"
        ).fetchone()
        return {
            "rows": total or 0,
            "settled": settled or 0,
            "windows": windows or 0,
            "first_ms": first,
            "last_ms": last,
            # Everything below is what analysis may actually use.
            "linked_rows": linked or 0,
            "linked_signals": linked_windows or 0,
            "legacy_rows": (total or 0) - (linked or 0),
            "research_from_ms": boundary,
        }

    def research_rows(self, settled_only: bool = True) -> list[dict]:
        """Observations fit for lifecycle analysis: linked, and optionally settled.

        The ONLY accessor analysis should use. Reading the table directly would
        quietly include pre-linkage rows, and a study that pools them is
        comparing reconstructable paths against loose rows from the same period.
        """
        clause = self.LINKED + (" AND won IS NOT NULL" if settled_only else "")
        cursor = self.db.execute(
            f"SELECT * FROM observations WHERE {clause} ORDER BY observed_ms ASC"
        )
        names = [d[0] for d in cursor.description]
        return [dict(zip(names, row, strict=True)) for row in cursor.fetchall()]

    def gate_study(self) -> dict[str, dict]:
        """Per-gate record of the signals each one rejected, and what happened.

        The question a rejected signal should answer is not "did it win" - most
        favourites win - but "was it priced well enough that taking it would
        have paid". So this reports edge per contract, which is payoff minus the
        price we would have paid, and is zero for a fairly priced market.

        A gate reported as SOLE is one that rejected the signal on its own: no
        other gate objected. Those are the only ones that tell you anything
        about that gate, because when three gates all say no you cannot
        attribute the outcome to any one of them.
        """
        out: dict[str, dict] = {}

        def bucket(key: str) -> dict:
            return out.setdefault(key, {"n": 0, "wins": 0, "edge": 0.0, "sole": 0})

        for price, won, qualified, gates in self.db.execute(
            "SELECT contract_price, won, qualified, failed_gates FROM predictions "
            "WHERE won IS NOT NULL AND contract_price IS NOT NULL"
        ):
            edge = (1.0 if won else 0.0) - price
            if qualified:
                b = bucket("(taken by the rule)")
                b["n"] += 1
                b["wins"] += 1 if won else 0
                b["edge"] += edge
                continue
            failed = [g.strip() for g in (gates or "").split(",") if g.strip()]
            if not failed:
                failed = ["(reason not recorded)"]
            for name in failed:
                b = bucket(name)
                b["n"] += 1
                b["wins"] += 1 if won else 0
                b["edge"] += edge
                if len(failed) == 1:
                    b["sole"] += 1
        return out

    def by_session(self) -> dict[str, dict]:
        """The record broken down by trading session and by hour of day.

        Derived from `window_open` rather than stored in a column, so it applies
        retroactively to every signal ever recorded and needs no migration.

        Sessions use the same boundaries as the backtest (`features._session`),
        so a live session figure can be compared directly against the measured
        one instead of against a differently-cut number.

        Measured over 22,560 historical entries in the 0.70-0.99 band, every
        session had a positive point estimate but the intervals overlap
        heavily - Asia +0.0093 [-0.0075, +0.0247] and US-afternoon
        +0.0116 [-0.0096, +0.0334] both straddle zero, while London, US-open
        and late-US do not. That is a reason to TRACK the split, not to trade
        on it: with intervals that wide, filtering by session would be fitting
        the sample rather than the market.
        """
        from .features import _session, _weekday
        from .validation import trade_pnl

        out: dict[str, dict] = {}

        def bucket(key: str) -> dict:
            return out.setdefault(
                key,
                {"signals": 0, "wins": 0, "paper": 0.0, "trades": 0, "real": 0.0},
            )

        traded = {
            row[0]: row[1:]
            for row in self.db.execute(
                f"SELECT window_open, count, COALESCE(fill_price, entry_limit), fee_paid, "
                f"exit_price, exit_count FROM trade_proposals "
                f"WHERE strategy='primary' AND status IN {ACCOUNTED_SQL}"
            )
        }

        for window, won, price in self.db.execute(
            "SELECT window_open, won, contract_price FROM predictions "
            "WHERE won IS NOT NULL AND contract_price IS NOT NULL"
        ):
            hour = _hour_utc(window)
            for key in (_session(window), f"{hour:02d}:00", _weekday(window)):
                b = bucket(key)
                b["signals"] += 1
                b["wins"] += 1 if won else 0
                b["paper"] += trade_pnl(price, bool(won), contracts=1)
                order = traded.get(window)
                if order:
                    count, paid, fee, exit_price, exit_count = order
                    pnl = position_pnl(
                        paid=paid, count=count, entry_fee=fee,
                        exit_price=exit_price, exit_count=exit_count, won=bool(won),
                    )
                    if pnl is not None:
                        b["trades"] += 1
                        b["real"] += pnl
        return out

    def ledger(self, limit: int = 12) -> list[dict]:
        """Every real trade in order, with the running total after each.

        The header shows only a total, so a jump from -1.53 to -1.03 looks like
        an unexplained +0.50 when it was in fact a +0.26 cash-out followed by a
        +0.24 settlement. A running balance makes each step account for itself.
        """
        rows = self.db.execute(
            f"SELECT t.created_at, t.ticker, t.side, t.count, "
            f"COALESCE(t.fill_price, t.entry_limit), t.fee_paid, t.exit_price, "
            f"t.exit_count, t.status, p.won FROM trade_proposals t "
            f"LEFT JOIN predictions p ON p.window_open = t.window_open "
            f"WHERE t.status IN {ACCOUNTED_SQL} ORDER BY t.created_at ASC"
        ).fetchall()
        out, running = [], 0.0
        for created, ticker, side, count, paid, fee, ex_px, ex_ct, _status, won in rows:
            pnl = position_pnl(
                paid=paid, count=count, entry_fee=fee, exit_price=ex_px,
                exit_count=ex_ct, won=None if won is None else bool(won),
            )
            if pnl is None:
                continue  # still open: nothing realised to add
            running += pnl
            out.append(
                {
                    "at": created,
                    "ticker": ticker,
                    "side": side,
                    "paid": paid,
                    "sold_at": ex_px,
                    "how": "sold early" if ex_px is not None else "settled",
                    "pnl": round(pnl, 4),
                    "running": round(running, 4),
                }
            )
        return out[-limit:]

    def entry_fee(self, proposal_id: str) -> float | None:
        """The fee Kalshi charged on the entry, or None if not read back yet."""
        row = self.db.execute(
            "SELECT fee_paid FROM trade_proposals WHERE id=?", (proposal_id,)
        ).fetchone()
        return float(row[0]) if row and row[0] is not None else None

    def record_settlements(self, rows: list[dict], now_ms: int) -> int:
        """Mirror Kalshi's settlement rows locally, so P&L survives an outage.

        Upsert by ticker: re-syncing the same settlement must not double-count
        it, and a settlement can be re-reported with corrected figures.
        """
        from datetime import datetime

        from .execution import KalshiExecutionClient

        written = 0
        for row in rows:
            ticker = row.get("ticker")
            if not ticker:
                continue
            stamp = row.get("settled_time") or ""
            try:
                settled_ms = int(
                    datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp() * 1000
                )
            except (ValueError, AttributeError):
                settled_ms = now_ms
            self.db.execute(
                "INSERT OR REPLACE INTO settlements VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    ticker, row.get("event_ticker"), row.get("market_result"),
                    float(row.get("yes_count_fp") or 0),
                    float(row.get("yes_total_cost_dollars") or 0),
                    float(row.get("no_count_fp") or 0),
                    float(row.get("no_total_cost_dollars") or 0),
                    int(row.get("revenue") or 0),
                    float(row.get("fee_cost") or 0),
                    KalshiExecutionClient.settlement_pnl(row),
                    settled_ms, now_ms,
                    KalshiExecutionClient.market_open_ms(ticker) or settled_ms,
                ),
            )
            written += 1
        self.db.commit()
        return written

    def quarantine_shifted_shadow_rows(self) -> int:
        """Mark every column-shifted legacy row, permanently. Returns the count.

        THE TELL IS A NUMERIC `session`. The old positional insert shifted
        every value one place once `dip_n` was appended to the table by a
        migration, so a row written after that point holds a probability where
        a session string belongs - "0.663797316526575" is not a trading
        session. `won` is NOT a usable tell: `settle_shadow` updates it by name
        after settlement, so it was being silently repaired on 95 of the 98
        rows while everything around it stayed wrong, which is exactly why this
        went unnoticed.

        Idempotent, and it only ever sets the flag. Nothing clears it.
        """
        marked = 0
        for row in self.db.execute(
            "SELECT rowid, session, won FROM shadow_decisions "
            "WHERE COALESCE(quarantined, 0) = 0"
        ).fetchall():
            rowid, session, won = row[0], row[1], row[2]
            shifted = False
            reason = ""
            if session is not None:
                try:
                    float(session)          # a session that parses as a number
                    shifted, reason = True, "numeric session (column shift)"
                except (TypeError, ValueError):
                    pass
            if not shifted and won is not None and won not in (0, 1):
                shifted, reason = True, "non-boolean won (column shift)"
            if shifted:
                self.db.execute(
                    "UPDATE shadow_decisions SET quarantined=1, quarantine_reason=? "
                    "WHERE rowid=?",
                    (reason, rowid),
                )
                marked += 1
        self.db.commit()
        return marked

    def clean_shadow_rows(self, since_ms: int | None = None) -> list[dict]:
        """Every shadow decision that is safe to analyse. Quarantine is absolute."""
        self.db.row_factory = sqlite3.Row
        where = "WHERE COALESCE(quarantined, 0) = 0"
        args: list = []
        if since_ms is not None:
            where += " AND created_at >= ?"
            args.append(since_ms)
        return [dict(r) for r in self.db.execute(
            f"SELECT * FROM shadow_decisions {where} ORDER BY created_at", args
        )]

    def save_details(self, key: str, body: str, now_ms: int) -> None:
        """Freeze the DETAILS text for one decision."""
        self.db.execute(
            "INSERT OR REPLACE INTO decision_details VALUES (?,?,?)",
            (key, body, now_ms),
        )
        self.db.commit()

    def details(self, key: str) -> str | None:
        row = self.db.execute(
            "SELECT body FROM decision_details WHERE key=?", (key,)
        ).fetchone()
        return row[0] if row else None

    def record_fills(self, rows: list[dict], now_ms: int) -> int:
        """Mirror the broker's executions. Upsert by `fill_id`."""
        from datetime import datetime

        from .execution import KalshiExecutionClient

        written = 0
        for row in rows:
            fill_id = row.get("fill_id") or row.get("trade_id")
            ticker = row.get("ticker") or row.get("market_ticker")
            if not fill_id or not ticker:
                continue
            stamp = row.get("created_time") or ""
            try:
                filled_ms = int(
                    datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp() * 1000
                )
            except (ValueError, AttributeError):
                filled_ms = int(row.get("ts") or 0) * 1000 or now_ms
            self.db.execute(
                "INSERT OR REPLACE INTO fills VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    fill_id, ticker, row.get("order_id"), row.get("action"),
                    row.get("side"), float(row.get("count_fp") or 0),
                    float(row.get("yes_price_dollars") or 0),
                    float(row.get("no_price_dollars") or 0),
                    float(row.get("fee_cost") or 0),
                    1 if row.get("is_taker") else 0,
                    filled_ms,
                    KalshiExecutionClient.market_open_ms(ticker) or filled_ms,
                    now_ms,
                ),
            )
            written += 1
        self.db.commit()
        return written

    def exchange_record(self, since_ms: int | None = None) -> tuple[int, int, float]:
        """(markets, winners, dollars) straight off the exchange's own numbers.

        Filtered on `window_ms` - the market's own time, parsed from the ticker
        - and never on `settled_ms`. Kalshi settles in batches hours after
        close, so a 04:45 market can settle at 08:45; filtering by settlement
        would have handed the daily loss floor a window that mixed one day's
        trades with another's.
        """
        where, args = "", []
        if since_ms is not None:
            where, args = " WHERE COALESCE(window_ms, settled_ms) >= ?", [since_ms]
        row = self.db.execute(
            "SELECT COUNT(*), SUM(pnl > 0), COALESCE(SUM(pnl), 0) "
            f"FROM settlements{where}",
            args,
        ).fetchone()
        return int(row[0] or 0), int(row[1] or 0), float(row[2] or 0.0)

    def executed_trades(self, since_ms: int | None = None) -> tuple[int, int]:
        """(fills, markets touched) from the broker's own execution record."""
        where, args = "", []
        if since_ms is not None:
            where, args = " WHERE COALESCE(window_ms, filled_ms) >= ?", [since_ms]
        row = self.db.execute(
            f"SELECT COUNT(*), COUNT(DISTINCT ticker) FROM fills{where}", args
        ).fetchone()
        return int(row[0] or 0), int(row[1] or 0)

    def realised_record(self) -> tuple[int, int, float]:
        """(trades, wins, dollars) over orders that were ACTUALLY PLACED.

        `scoreboard` answers "was the signal right?" across every signal,
        including the great majority that were never traded, by pricing each one
        at a hypothetical size. That is a useful measure of the strategy and a
        useless measure of the account: presenting it as money claims a P&L on
        trades nobody made, at a size nobody chose.

        This is the other question - "what did the account actually do?" - and
        it is READ FROM THE EXCHANGE, never rebuilt. The local reconstruction
        this replaced reported +1.06 on an account that was down -1.62: it
        priced rows at `entry_limit` when no fill price was stored, modelled
        the fee rather than reading the one charged, scored settlement by our
        own `predictions.won`, and saw only 47 of 88 settled markets because
        ACCOUNTED_SQL drops a proposal that filled but never reached a terminal
        status. Every one of those is a way to be confidently wrong about money.

        TODAY, not all time, because that is the figure the operator reads off
        the Kalshi app and compares against. The app's "+$4.05 (+14.67%)" is
        today's realised P&L plus the open position marked to the bid; an
        all-time total reported beside it looks like the bot is lying. The two
        differed by $5.47 on 2026-09-22: -1.40 all-time against +4.07 today.

        The day is the MARKET's day, from `window_ms`, never the settlement
        timestamp - Kalshi settles in batches hours late, and a 04:45 market
        settling at 08:45 would otherwise land on the wrong side of midnight.

        The fallback is the old local path, used only while the settlements
        mirror is still empty - a fresh database, or before the first sync.
        """
        import time

        now_ms = int(time.time() * 1000)
        markets, winners, dollars = self.exchange_record(now_ms - (now_ms % 86_400_000))
        if markets:
            return markets, winners, dollars + self.get_setting("open_mark", 0.0)
        if self.exchange_record()[0]:
            # The mirror is live, today simply has nothing settled in it yet.
            return 0, 0, self.get_setting("open_mark", 0.0)

        rows = self.db.execute(
            "SELECT t.count, COALESCE(t.fill_price, t.entry_limit), t.fee_paid, "
            "t.exit_price, t.exit_count, p.won FROM trade_proposals t "
            "LEFT JOIN predictions p ON p.window_open = t.window_open "
            f"WHERE t.status IN {ACCOUNTED_SQL}"
        ).fetchall()

        trades = wins = 0
        total = 0.0
        for count, paid, fee, exit_price, exit_count, won in rows:
            pnl = position_pnl(
                paid=paid,
                count=count,
                entry_fee=fee,
                exit_price=exit_price,
                exit_count=exit_count,
                won=None if won is None else bool(won),
            )
            if pnl is None:
                continue  # still open
            total += pnl
            trades += 1
            wins += 1 if pnl > 0 else 0
        return trades, wins, total

    def auto_state(self, now_ms: int) -> tuple[int, int, float, float, int]:
        """(trades_today, trades_last_hour, seconds_since_last, realised_today, open).

        Read from durable rows rather than in-memory counters, so a restart
        cannot quietly reset a limit that has already been breached.
        """
        day_start = now_ms - (now_ms % 86_400_000)
        # An exited trade still consumed a slot and still cost money, so it
        # counts against the daily and hourly caps exactly like a held one.
        traded = "('filled','protected','unprotected','executing','partial','exited')"
        today = self.db.execute(
            "SELECT COUNT(*), MAX(created_at) FROM trade_proposals "
            f"WHERE created_at >= ? AND status IN {traded}",
            (day_start,),
        ).fetchone()
        hour = self.db.execute(
            f"SELECT COUNT(*) FROM trade_proposals WHERE created_at >= ? AND status IN {traded}",
            (now_ms - 3_600_000,),
        ).fetchone()[0]
        last = today[1]
        since = (now_ms - last) / 1000 if last else 1e9

        # THE LIMIT TAKES THE WORSE OF THE TWO, DELIBERATELY.
        #
        # Reporting wants the exchange's truth, but a loss floor must never be
        # relaxed by a source that can be stale or empty: if the settlements
        # mirror has not synced - API down, fresh database, service just
        # started - reading it alone would return 0.00 and silently switch the
        # daily floor off on a day that had already lost money. So the local
        # reconstruction is still computed, and the floor uses whichever is
        # MORE negative. It can be early to stop, never late.
        _, _, exchange_today = self.exchange_record(day_start)

        realised = 0.0
        for count, paid, fee, exit_price, exit_count, won in self.db.execute(
            "SELECT t.count, COALESCE(t.fill_price, t.entry_limit), t.fee_paid, "
            "t.exit_price, t.exit_count, p.won FROM trade_proposals t "
            "LEFT JOIN predictions p ON p.window_open = t.window_open "
            f"WHERE t.status IN {ACCOUNTED_SQL} AND t.created_at >= ?",
            (day_start,),
        ).fetchall():
            pnl = position_pnl(
                paid=paid,
                count=count,
                entry_fee=fee,
                exit_price=exit_price,
                exit_count=exit_count,
                won=None if won is None else bool(won),
            )
            if pnl is not None:
                realised += pnl
        realised = min(realised, exchange_today)

        # 'filled' covers a position partially sold too - mark_exited only
        # promotes to 'exited' once the whole position is gone, so the remainder
        # still blocks a second order while it is genuinely at risk.
        open_now = self.db.execute(
            f"SELECT COUNT(*) FROM trade_proposals t "
            f"WHERE t.status IN {HELD_SQL} AND t.close_ms > ?",
            (now_ms,),
        ).fetchone()[0]
        return today[0], hour, since, realised, open_now

    def get_setting(self, key: str, default: float) -> float:
        """A setting changeable at runtime, so sizing needs no restart."""
        row = self.db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return float(row[0]) if row else default

    def set_setting(self, key: str, value: float, now_ms: int) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO settings VALUES (?,?,?)", (key, float(value), now_ms)
        )
        self.db.commit()

    def record_reversion(
        self,
        window_open: int,
        created_at: int,
        ticker: str,
        side: str,
        entry: float,
        take_profit: float,
        key_level: float,
        spike_bps: float,
        rejection_bps: float,
        remaining_s: int,
    ) -> bool:
        """Log a reversion setup without announcing it.

        The strategy is silent but not switched off: every setup it would have
        taken is still recorded, so its live record accumulates alongside the
        68-day backtest and the decision to keep it disabled stays evidence-led.
        """
        cursor = self.db.execute(
            "INSERT OR IGNORE INTO reversion_signals "
            "(window_open,created_at,ticker,side,entry_price,take_profit,key_level,"
            "spike_bps,rejection_bps,remaining_s) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                window_open,
                created_at,
                ticker,
                side,
                entry,
                take_profit,
                key_level,
                spike_bps,
                rejection_bps,
                remaining_s,
            ),
        )
        self.db.commit()
        return cursor.rowcount == 1

    def settle(self, window_open: int, side: str, result: str) -> None:
        won = int(result == ("yes" if side == "UP" else "no"))
        self.db.execute(
            "UPDATE predictions SET won=? WHERE window_open=?",
            (won, window_open),
        )
        self.db.commit()

    def open_position(self, window_open: int) -> tuple[str, float] | None:
        """The side and entry limit of a filled, unexited position in this window."""
        row = self.open_position_detail(window_open)
        return (row[0], row[1]) if row else None

    def open_position_detail(
        self, window_open: int
    ) -> tuple[str, float, float, str, int] | None:
        """(side, price paid, count, ticker, id) for an open primary position.

        The count and ticker are what an exit order needs. Reading them from the
        proposal row rather than from the exchange keeps the exit bounded by
        what this system actually bought: a position opened by hand elsewhere in
        the same market is not ours to close.

        The price is the FILL where one is known, not the posted limit - the
        limit is what we asked for and is always at or above what we paid.
        """
        row = self.db.execute(
            f"SELECT side, COALESCE(fill_price, entry_limit), count, ticker, id "
            f"FROM trade_proposals "
            f"WHERE window_open=? AND strategy='primary' AND status IN {HELD_SQL}",
            (window_open,),
        ).fetchone()
        return tuple(row) if row else None

    def calibration(self, bucket: int) -> Calibration:
        samples, wins = self.db.execute(
            "SELECT COUNT(*), COALESCE(SUM(won),0) FROM predictions "
            "WHERE bucket=? AND won IS NOT NULL",
            (bucket,),
        ).fetchone()
        rate = wins / samples if samples else 0.0
        return Calibration(samples, wins, rate, wilson_lower(wins, samples))

    def order_attempts(self, window_open: int) -> tuple[int, str | None]:
        """(attempts made, status of the most recent) for one window's orders.

        Used to decide whether a missed fill may be re-attempted. An order that
        never filled cost nothing and changed nothing, so the opportunity is
        still open - but only if the setup is still valid at the new price, and
        only a couple of times, so a runaway market cannot be chased forever.
        """
        rows = self.db.execute(
            "SELECT status FROM trade_proposals WHERE window_open=? AND strategy='primary' "
            "AND entry_order_id IS NOT NULL ORDER BY created_at DESC",
            (window_open,),
        ).fetchall()
        return len(rows), (rows[0][0] if rows else None)

    def record_decision_record(self, row: dict) -> None:
        """Everything that supported ONE decision, proposal or not.

        Keyed on the OBSERVATION, not the proposal. A PASS, a WAIT, a rejected
        setup and an order that never filled are all training data - arguably
        the most valuable, since they are the counterfactuals - and none of
        them has a proposal id. Keying on `proposal_id` meant the archive only
        ever contained the trades that worked out well enough to exist.

        Linked by signal_id (one opportunity, stable across restarts),
        market_id (the Kalshi ticker), observation_id (the exact poll) and
        strategy_version (what the rule was at the time), with proposal_id
        optional.

        NEVER raises: bookkeeping may not propagate into the path that trades.
        """
        try:
            self.db.execute("""
                CREATE TABLE IF NOT EXISTS decision_records (
                    observation_id TEXT PRIMARY KEY,
                    signal_id TEXT, market_id TEXT, proposal_id TEXT,
                    strategy_version TEXT,
                    window_open INTEGER, created_at INTEGER,
                    ticker TEXT, side TEXT, remaining_s INTEGER,
                    action TEXT, blocked_reason TEXT,
                    ask REAL, limit_submitted REAL, count REAL,
                    gates TEXT, settled_s REAL,
                    measured_edge REAL, fee REAL, net_edge REAL,
                    distance_dollars REAL, normalized_distance REAL,
                    volatility_bps REAL, momentum_bps REAL, spread_bps REAL,
                    session TEXT, hour_utc INTEGER, vol_regime TEXT,
                    regime_weight REAL, confidence_adjustment INTEGER,
                    protective_level REAL, level_adjustment INTEGER,
                    cohort_n INTEGER, cohort_win_probability REAL,
                    cohort_win_low REAL, cohort_win_high REAL,
                    cohort_action TEXT, cohort_reason TEXT,
                    fill_price REAL, filled INTEGER, won INTEGER
                )
            """)
            columns = [
                "observation_id", "signal_id", "market_id", "proposal_id",
                "strategy_version", "window_open", "created_at", "ticker",
                "side", "remaining_s", "action", "blocked_reason", "ask",
                "limit_submitted", "count", "gates", "settled_s",
                "measured_edge", "fee", "net_edge", "distance_dollars",
                "normalized_distance", "volatility_bps", "momentum_bps",
                "spread_bps", "session", "hour_utc", "vol_regime",
                "regime_weight", "confidence_adjustment", "protective_level",
                "level_adjustment", "cohort_n", "cohort_win_probability",
                "cohort_win_low", "cohort_win_high", "cohort_action",
                "cohort_reason", "fill_price", "filled", "won",
            ]
            self.db.execute(
                "INSERT OR REPLACE INTO decision_records VALUES ("
                + ",".join("?" * len(columns)) + ")",
                tuple(row.get(key) for key in columns),
            )
            self.db.commit()
        except sqlite3.Error:
            pass

    def settle_decision_records(self, window_open: int, winning_side: str) -> None:
        """Per row, on that row's own side - see `settle_observations`."""
        try:
            self.db.execute(
                "UPDATE decision_records "
                "SET won = CASE WHEN side=? THEN 1 ELSE 0 END "
                "WHERE window_open=?",
                (winning_side, window_open),
            )
            self.db.commit()
        except sqlite3.Error as exc:
            # See `settle_shadow`: a silent failure here leaves every DECLINED
            # and ENTERED record ungraded, so "what did refusing cost us" has
            # no answer and nothing reports that it has no answer.
            print(
                f"settle: decision records for window {window_open} were NOT "
                f"graded: {type(exc).__name__}: {exc}",
                flush=True,
            )

    def record_shadow_decision(self, row: dict) -> None:
        """Write down one similarity read. NEVER raises, never trades.

        Pure bookkeeping for a layer that is not allowed to act, so nothing it
        does may propagate into the loop that is.
        """
        # WRITTEN BY NAME, NEVER BY POSITION.
        #
        # This was `VALUES (?,?,...)` with a hand-counted `"?" * 30`, which
        # means every new column is a chance to shift every value one place
        # left and write a probability into a price. That is not hypothetical:
        # a blind edit of exactly this pattern put 30 values into the 24-column
        # observations insert on 2026-09-21 and broke `observe()` outright. By
        # name, an unknown key is caught here and a new column simply defaults.
        try:
            columns = [
                info[1] for info in self.db.execute(
                    "PRAGMA table_info(shadow_decisions)"
                )
            ]
            # `created_at` is NOT NULL, and a caller that forgets it used to
            # lose the row entirely to a swallowed IntegrityError - a decision
            # missing from the archive is a decision that cannot be graded,
            # which is the one failure this table exists to prevent.
            row = dict(row)
            row.setdefault("created_at", int(time.time() * 1000))
            present = [name for name in columns if name in row]
            self.db.execute(
                f"INSERT OR REPLACE INTO shadow_decisions "
                f"({','.join(present)}) VALUES ({','.join('?' * len(present))})",
                [row[name] for name in present],
            )
            self.db.commit()
        except sqlite3.Error as exc:
            # Still never raises - but no longer invisible. A row dropped here
            # is a window missing from the head-to-head, which is exactly the
            # failure that made the comparison look merely thin instead of
            # broken.
            print(
                f"shadow decision not recorded for "
                f"{row.get('window_open')}: {type(exc).__name__}: {exc}",
                flush=True,
            )

    def settle_shadow(self, window_open: int, winning_side: str) -> None:
        """Attach the outcome, scored on each ROW's own side.

        Same defect as `settle_observations`: a window holds rows on both
        sides once the model flips, and one boolean cannot be right for both.
        Safe today only because there happens to be one shadow row per window.
        """
        try:
            self.db.execute(
                "UPDATE shadow_decisions "
                "SET won = CASE WHEN side=? THEN 1 ELSE 0 END "
                "WHERE window_open=?",
                (winning_side, window_open),
            )
            self.db.commit()
        except sqlite3.Error as exc:
            # An unsettled shadow row can never be graded, and a shadow layer
            # that cannot be graded can never be promoted. Swallowing the error
            # meant the corpus could stop settling entirely without one line
            # anywhere saying so. Still never raises into the trading loop.
            print(
                f"settle: shadow rows for window {window_open} were NOT "
                f"graded: {type(exc).__name__}: {exc}",
                flush=True,
            )

    def record_alert(self, strategy: str, window_open: int, created_at: int) -> bool:
        cursor = self.db.execute(
            "INSERT OR IGNORE INTO strategy_alerts(strategy,window_open,created_at) VALUES(?,?,?)",
            (strategy, window_open, created_at),
        )
        self.db.commit()
        return cursor.rowcount == 1

    def create_proposal(
        self,
        strategy: str,
        window_open: int,
        ticker: str,
        side: str,
        entry_limit: float,
        take_profit: float,
        count: int,
        expires_at: int,
        close_ms: int,
        created_at: int,
    ) -> TradeProposal:
        proposal_id = uuid4().hex[:20]
        # Next attempt number for this window, so every try gets its own row and
        # its own id while staying joinable to the one signal.
        attempt = 1 + (
            self.db.execute(
                "SELECT COALESCE(MAX(attempt),0) FROM trade_proposals "
                "WHERE strategy=? AND window_open=?",
                (strategy, window_open),
            ).fetchone()[0]
        )
        self.db.execute(
            "INSERT OR IGNORE INTO trade_proposals "
            "(id,strategy,window_open,ticker,side,entry_limit,take_profit,count,expires_at,"
            "close_ms,status,created_at,attempt) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                proposal_id,
                strategy,
                window_open,
                ticker,
                side,
                entry_limit,
                take_profit,
                count,
                expires_at,
                close_ms,
                "pending",
                created_at,
                attempt,
            ),
        )
        self.db.commit()
        # BY ID. This selected on (strategy, window_open) with no attempt filter
        # and no LIMIT, which was correct only while a window could hold one
        # proposal. Once retries gave a window several, it returned whichever
        # row SQLite yielded first - attempt 1 - so a caller that had just
        # created attempt 2 at 82c was handed the manual attempt at 65c and
        # placed a real order against a price the rule had rejected.
        row = self.db.execute(
            "SELECT id,strategy,window_open,ticker,side,entry_limit,take_profit,count,"
            "expires_at,close_ms,status FROM trade_proposals WHERE id=?",
            (proposal_id,),
        ).fetchone()
        if row is None:
            # The insert was ignored on a uniqueness conflict. Say so rather
            # than hand back someone else's proposal.
            raise RuntimeError(
                f"proposal {proposal_id} was not stored for {strategy}/{window_open}"
            )
        return TradeProposal(*row)

    def proposal(self, proposal_id: str) -> TradeProposal | None:
        row = self.db.execute(
            "SELECT id,strategy,window_open,ticker,side,entry_limit,take_profit,count,"
            "expires_at,close_ms,status FROM trade_proposals WHERE id=?",
            (proposal_id,),
        ).fetchone()
        return TradeProposal(*row) if row else None

    def claim_proposal(self, proposal_id: str, now_ms: int) -> TradeProposal | None:
        cursor = self.db.execute(
            "UPDATE trade_proposals SET status='executing' "
            "WHERE id=? AND status='pending' AND expires_at>=?",
            (proposal_id, now_ms),
        )
        self.db.commit()
        return self.proposal(proposal_id) if cursor.rowcount == 1 else None

    def record_fill(
        self, proposal_id: str, window_open: int, price: float, count: float, fee: float
    ) -> None:
        """Overwrite the modelled entry with what the exchange actually did.

        Both rows are corrected: the proposal keeps the audit trail, and the
        prediction carries the price every scoreboard and settlement report is
        computed from. Leaving the prediction at its limit price would make
        every number this system reports subtly wrong in the same direction.
        """
        self.db.execute(
            "UPDATE trade_proposals SET fill_price=?,fee_paid=?,count=? WHERE id=?",
            (float(price), float(fee), count, proposal_id),
        )
        # Only a primary order may correct a prediction row. `predictions` is
        # written solely by primary_signal, so a reversion fill in the same
        # window would otherwise overwrite the primary signal's recorded price
        # with an unrelated one - and every scoreboard figure derives from it.
        strategy = self.db.execute(
            "SELECT strategy FROM trade_proposals WHERE id=?", (proposal_id,)
        ).fetchone()
        if strategy and strategy[0] == "primary":
            self.db.execute(
                "UPDATE predictions SET contract_price=? WHERE window_open=?",
                (float(price), window_open),
            )
            # `shadow_decisions.traded` was written as a literal 0 by its only
            # writer and updated by nobody, so "the rule traded it and the
            # shadow said don't" could not return a row however long the
            # service ran. This is the one point that proves a real position
            # exists, so it is where the window gets stamped. Its own
            # try/except: sqlite3.Error is not caught by the post-order handler
            # in primary_signal, and bookkeeping may not reach the loop after
            # money has moved.
            try:
                self.db.execute(
                    "UPDATE shadow_decisions SET traded=1 WHERE window_open=?",
                    (window_open,),
                )
            except sqlite3.Error as exc:
                print(
                    f"shadow traded stamp failed for window {window_open}: "
                    f"{type(exc).__name__}: {exc}",
                    flush=True,
                )
        self.db.commit()

    def mark_exited(self, proposal_id: str, price: float, count: float, note: str) -> None:
        """Record a position sold before settlement.

        Kept separate from finish_proposal because that method also writes the
        order-id columns, and passing None for them here would erase the entry
        order id - the only link back to what was actually bought.
        """
        row = self.db.execute(
            "SELECT count FROM trade_proposals WHERE id=?", (proposal_id,)
        ).fetchone()
        ordered = (row[0] if row else 0) or 0
        # Only a sale covering the whole position closes it. A partial fill
        # leaves contracts live, and marking those "exited" both hid their loss
        # from the daily floor and freed the one-position guard while they were
        # still at risk.
        status = "exited" if float(count) >= float(ordered) else "filled"
        self.db.execute(
            "UPDATE trade_proposals SET status=?,result_note=?,"
            "exit_price=?,exit_count=? WHERE id=?",
            (status, note, float(price), float(count), proposal_id),
        )
        self.db.commit()

    def finish_proposal(
        self,
        proposal_id: str,
        status: str,
        note: str,
        entry_order_id: str | None = None,
        take_profit_order_id: str | None = None,
    ) -> None:
        self.db.execute(
            "UPDATE trade_proposals SET status=?,result_note=?,entry_order_id=?,"
            "take_profit_order_id=? WHERE id=?",
            (status, note, entry_order_id, take_profit_order_id, proposal_id),
        )
        self.db.commit()

    def skip_proposal(self, proposal_id: str) -> bool:
        cursor = self.db.execute(
            "UPDATE trade_proposals SET status='skipped' WHERE id=? AND status='pending'",
            (proposal_id,),
        )
        self.db.commit()
        return cursor.rowcount == 1


def wilson_lower(wins: int, samples: int, z: float = 1.96) -> float:
    if samples == 0:
        return 0.0
    p = wins / samples
    denominator = 1 + z * z / samples
    center = p + z * z / (2 * samples)
    margin = z * math.sqrt((p * (1 - p) + z * z / (4 * samples)) / samples)
    return (center - margin) / denominator
