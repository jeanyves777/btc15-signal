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


# A deficit below half a cent is $0.00. The smallest thing this account can
# trade moves whole cents, so a residue under that is arithmetic, not money
# still to be won back.
DEFICIT_CLEARED = 0.005

# The recovery plan length, in trades. MUST match `Settings.recovery_steps`;
# it is duplicated here only so a caller that never sizes an order - a report,
# a script - does not have to carry the config. `test_intelligence.py` pins
# the two together.
DEFAULT_RECOVERY_STEPS = 4


@dataclass(frozen=True)
class RecoveryState:
    """The unrecovered deficit, in realised dollars after fees.

    `deficit` is money that has actually left the account and has not been won
    back. `active` is simply `deficit > 0` - recovery runs until the money is
    back, and stops the moment it is. `markets` counts the realised markets
    applied since the deficit was opened, and `opened_ms` when that was.

    `steps` is how many upsized trades the plan has left to win it back with.
    It is what the deficit is divided by to get the per-trade requirement, it
    decrements only when a trade actually takes the upsize, and a fresh loss
    puts it back to the full plan - see `consume_recovery_step`.
    """

    deficit: float
    active: bool
    markets: int
    opened_ms: int
    steps: int = 0

    def required_per_trade(self) -> float:
        """Dollars one upsized trade has to be able to win, net of fees.

        The divisor is floored at 1: a plan with no steps left still has a
        requirement, and it is the whole remaining deficit.
        """
        return self.deficit / max(1, self.steps)


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
@dataclass(frozen=True)
class LifetimeRecord:
    """Realised performance over the whole reconciled record.

    `complete` is False when the broker cannot show the account's entire
    history - Kalshi's settlements endpoint reaches back only so far, and this
    account has fills older than that. A total that silently omits earlier
    trading must not be called a lifetime, so the label carries the date it
    actually starts from.
    """

    markets: int
    winners: int
    dollars: float
    since_ms: int | None
    complete: bool

    @property
    def losers(self) -> int:
        return self.markets - self.winners

    def label(self) -> str:
        if self.complete or self.since_ms is None:
            return "Live lifetime"
        import datetime as _dt

        # `%-d` is a glibc extension and raises on Windows, where this runs.
        day = _dt.datetime.fromtimestamp(self.since_ms / 1000, tz=_dt.UTC)
        return f"Live since {day.day} {day:%b}"


@dataclass(frozen=True)
class MoneySnapshot:
    """One account, one instant. Every message renders from one of these.

    `realised` is money that is final. `open_mark` is the open position marked
    to the bid, which moves. `headline` is their sum, which is how the Kalshi
    app builds the figure the operator reads. Keeping the parts separate is
    what stops a count and a dollar figure being taken from different reads.
    """

    markets: int
    winners: int
    realised: float
    open_mark: float
    taken_ms: int
    has_mirror: bool = False
    # The lifetime figures come from the SAME read as today's, so a message
    # can never show a profit in one line that the other has not counted.
    lifetime: "LifetimeRecord | None" = None
    # And the session breakdown from the same rows as `markets`, so it sums.
    sessions: tuple = ()
    # Recovery state travels with the money, so every message can show
    # what it is doing without a second read that might disagree.
    recovery: object | None = None
    last_add: dict | None = None

    @property
    def losers(self) -> int:
        return self.markets - self.winners

    @property
    def headline(self) -> float:
        return round(self.realised + self.open_mark, 6)


def _capital(row: dict):
    """A capital_days row as a `capital.Capital`. Imported lazily so `store`
    does not depend on a module that depends on it."""
    from .capital import Capital

    return Capital(
        ny_day=row["ny_day"], reconciled_cash=row["reconciled_cash"],
        open_exposure=row["open_exposure"], base_contracts=row["base_contracts"],
        account_ceiling=row["account_ceiling"],
        reconciled_ms=row["reconciled_ms"],
    )


from .sessions import breakdown as session_breakdown  # noqa: E402

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
        # THE APPEND-ONLY REALISED LEDGER. One row per market whose money is
        # final, written the moment it becomes final and never removed.
        #
        # Every Telegram message used to read `settlements` + `open_mark`,
        # which are two different 60-second snapshots of the same account. On
        # 2026-09-22 that showed +$4.49 at the cash-out, +$3.95 in the
        # settlement recap four minutes later, and +$4.51 at the next signal -
        # the same $0.55 counted, dropped and recounted while no money moved.
        # The cause is a sync gap: a position sold early is gone from the
        # account but still carried in the stale mark, and its proceeds have
        # not yet landed in `settlements`.
        #
        # This table closes the gap. A cash-out writes its realised figure
        # immediately from the fill it already has; the broker sync writes the
        # settled figure when it arrives. `source` records which, so a later
        # disagreement between the two is visible rather than silent.
        self.db.execute("""
            CREATE TABLE IF NOT EXISTS daily_ledger (
                ticker TEXT PRIMARY KEY,
                window_ms INTEGER NOT NULL,
                pnl REAL NOT NULL,
                won INTEGER,
                source TEXT NOT NULL,
                first_ms INTEGER NOT NULL,
                updated_ms INTEGER NOT NULL,
                revisions INTEGER NOT NULL DEFAULT 0
            )
        """)
        # THE UNRECOVERED DEFICIT. One row, carried across restarts.
        #
        # Recovery ends on RECOVERED MONEY: realised, net of fees, and never on
        # a win count, a paper profit or an open position's mark. The figures
        # come from `daily_ledger` above, which is the only place that counts
        # an early cash-out exactly once and lets the exchange revise it.
        #
        # It is a stored running total rather than a replay because that is
        # what "another loss INCREASES the deficit" requires: the deficit is
        # path-dependent - it floors at zero the moment the money is back - so
        # it cannot be re-derived from a sum, and a restart mid-recovery has to
        # resume, not start again.
        #
        # NOT RESET AT MIDNIGHT. It is about money that is still missing, not
        # about a calendar. `auto_daily_loss_limit` remains the day's own stop.
        # If the operator wants a daily reset, this is the row to clear.
        self.db.execute("""
            CREATE TABLE IF NOT EXISTS recovery_deficit (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                deficit REAL NOT NULL,
                markets INTEGER NOT NULL DEFAULT 0,
                opened_ms INTEGER NOT NULL DEFAULT 0,
                updated_ms INTEGER NOT NULL DEFAULT 0,
                steps INTEGER NOT NULL DEFAULT 0
            )
        """)
        self._add_columns("recovery_deficit", {"steps": "INTEGER NOT NULL DEFAULT 0"})
        # WHAT HAS ALREADY BEEN APPLIED, per market, so no market can move the
        # deficit twice. It holds the realised figure that was folded in, not a
        # flag, so a row the exchange later revises moves the deficit by the
        # DELTA - the difference between the banked number and the settled one
        # - instead of being counted again in full.
        self._add_columns("daily_ledger", {"recovery_applied": "REAL"})
        # THE CONDITIONAL RECOVERY ADD-ON. One row per position that reached
        # BASE ENTERED, carrying the whole lifecycle: what was placed, what
        # filled, at what fee, with what queue ahead of it, and why it was
        # cancelled. `client_order_id` is UNIQUE, which is the database half
        # of the no-duplicate-contract guarantee - the API half is Kalshi
        # rejecting the same id, and neither is trusted alone.
        self.db.execute("""
            CREATE TABLE IF NOT EXISTS recovery_adds (
                client_order_id TEXT PRIMARY KEY,
                window_open_ms INTEGER NOT NULL,
                ticker TEXT NOT NULL,
                side TEXT NOT NULL,
                state TEXT NOT NULL,
                order_id TEXT,
                base_fill REAL,
                limit_price REAL,
                count INTEGER NOT NULL DEFAULT 1,
                deficit_at_placement REAL,
                required_at_placement REAL,
                conditions_at_placement TEXT,
                placed_ms INTEGER,
                expiration_ts INTEGER,
                filled_count REAL NOT NULL DEFAULT 0,
                fill_price REAL,
                fill_ms INTEGER,
                fee_paid REAL,
                is_taker INTEGER,
                queue_ahead REAL,
                book_depth REAL,
                conditions_at_fill TEXT,
                cancelled_ms INTEGER,
                cancel_reason TEXT,
                realised_pnl REAL,
                settled INTEGER NOT NULL DEFAULT 0,
                created_ms INTEGER NOT NULL,
                updated_ms INTEGER NOT NULL
            )
        """)
        self.db.execute(
            "CREATE INDEX IF NOT EXISTS recovery_adds_window "
            "ON recovery_adds(window_open_ms, state)"
        )
        # THE CUMULATIVE TEST BUDGET. One row, and it only ever goes UP.
        #
        # A cap that resets on a loss, a new day or a restart is not a cap - it
        # is an allowance that can be spent again every time the thing it is
        # protecting against happens. This counts every dollar the add-on has
        # ever actually had filled, for the life of the test, and the ceiling
        # is checked against that total PLUS whatever is resting right now.
        self.db.execute("""
            CREATE TABLE IF NOT EXISTS recovery_add_budget (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                committed REAL NOT NULL DEFAULT 0,
                fills INTEGER NOT NULL DEFAULT 0,
                started_ms INTEGER NOT NULL,
                updated_ms INTEGER NOT NULL
            )
        """)
        # REALISED EVENTS: the cash-flow sequence the deficit is folded from.
        #
        # `daily_ledger` is one row per MARKET and is right for reporting, but
        # wrong for a path-dependent figure. Two reasons, both measured on the
        # live database:
        #
        #   * `first_ms` is DISCOVERY time, not realisation time.
        #     KXBTC15M-26SEP221300-00 settled at 17:00:08 and was first banked
        #     at 17:15:25 - 917 seconds late. Ordering by it replays a delayed
        #     settlement 15 minutes after it actually happened.
        #   * a partial exit and the settlement of the remainder are two cash
        #     flows at two times. Aggregating them under one ticker loses that
        #     ordering entirely.
        #
        # `realised_ms` is the BROKER's timestamp for when the money became
        # real - `settlements.settled_ms` for a settlement, the exit fill's
        # `filled_ms` for a cash-out - and never ours.
        self.db.execute("""
            CREATE TABLE IF NOT EXISTS realised_events (
                event_id TEXT PRIMARY KEY,
                ticker TEXT NOT NULL,
                realised_ms INTEGER NOT NULL,
                amount REAL NOT NULL,
                source TEXT NOT NULL,
                window_ms INTEGER,
                applied REAL,
                recorded_ms INTEGER NOT NULL
            )
        """)
        self.db.execute(
            "CREATE INDEX IF NOT EXISTS realised_events_order "
            "ON realised_events(realised_ms, event_id)"
        )
        # THE DAILY CAPITAL REVIEW, keyed on the NEW YORK day - the exchange's
        # own reset boundary, not UTC. `reconciled_cash` is settled cash only:
        # sizing on an open position's mark would compound exposure exactly
        # when a position is winning and most likely to be given back.
        self.db.execute("""
            CREATE TABLE IF NOT EXISTS capital_days (
                ny_day TEXT PRIMARY KEY,
                reconciled_cash REAL NOT NULL,
                open_exposure REAL NOT NULL,
                base_contracts INTEGER NOT NULL,
                account_ceiling REAL NOT NULL,
                reconciled_ms INTEGER NOT NULL
            )
        """)
        # Funds claimed by an order this process has decided on but the broker
        # has not seen yet. The UNIQUE key is the atomicity: two orders in one
        # poll cannot both spend the same balance.
        self.db.execute("""
            CREATE TABLE IF NOT EXISTS fund_reservations (
                key TEXT PRIMARY KEY,
                amount REAL NOT NULL,
                created_ms INTEGER NOT NULL,
                state TEXT NOT NULL DEFAULT 'held'
            )
        """)
        # EVERY INTELLIGENCE DECISION, recorded BEFORE submission and whether
        # or not an order follows. This is what answers "what did intelligence
        # change, why, and did it help" - a question that cannot be answered
        # retrospectively from the orders alone, because the interesting cases
        # are the ones where no order exists.
        self.db.execute("""
            CREATE TABLE IF NOT EXISTS intelligence_decisions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                window_open INTEGER NOT NULL,
                ticker TEXT,
                signal_id TEXT,
                proposal_id TEXT,
                decided_ms INTEGER NOT NULL,
                remaining_s INTEGER,
                side TEXT,
                ask REAL,
                base_qualified INTEGER NOT NULL,
                failed_gates TEXT,
                final_action TEXT NOT NULL,
                overrides_gate TEXT,
                reason TEXT,
                confidence_delta INTEGER NOT NULL DEFAULT 0,
                calibrated_probability REAL,
                expected_net REAL,
                evidence_n INTEGER,
                uncertainty REAL,
                context_key TEXT,
                model_version TEXT,
                policy_version TEXT,
                feature_version TEXT,
                training_cutoff_ms INTEGER,
                features_ok INTEGER,
                -- filled in after the fact, so an adjustment can be graded
                order_id TEXT,
                filled INTEGER,
                won INTEGER,
                realised_pnl REAL,
                graded_ms INTEGER
            )
        """)
        self.db.execute(
            "CREATE INDEX IF NOT EXISTS intelligence_window "
            "ON intelligence_decisions(window_open, decided_ms)"
        )
        # `settings` holds REAL only. The open mark has to be carried per
        # ticker, not as one total, so a position that has already been banked
        # can be excluded from it - which is the whole fix.
        self.db.execute("""
            CREATE TABLE IF NOT EXISTS settings_text (
                key TEXT PRIMARY KEY, text_value TEXT, updated_at INTEGER
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

    def outstanding_loss(self) -> tuple[float, int]:
        """(dollars still to recover, markets applied since it was opened).

        The deficit only, for callers that want the number. `recovery_state`
        is the one sizing reads.
        """
        state = self.recovery_state()
        return state.deficit, state.markets

    def recovery_state(
        self, plan_steps: int = DEFAULT_RECOVERY_STEPS, now_ms: int | None = None
    ) -> RecoveryState:
        """The unrecovered deficit, brought up to date with the ledger.

        THE OPERATOR'S RULE, 2026-09-22: recovery starts on a realised net
        loss, tracks the deficit after fees, keeps sizing up until realised
        profit covers it, and stops the moment the deficit reaches zero. A
        further loss INCREASES the deficit; it never restarts or erases it.

        Recovery ends on RECOVERED MONEY, and nothing else - not a win count,
        not paper profit, not an open position's mark, not gross profit before
        fees. That is why the feed is `daily_ledger`: it is the append-only
        realised record, it counts an early cash-out exactly once, and the
        exchange may revise it but nothing may rebuild it locally.

        This supersedes the per-loss, replace-the-target rule of FINDINGS 39.
        That rule was chosen on a measurement - cumulative would have sat at $2
        for 93% of windows against 57%, with $6.68 outstanding and 3 recoveries
        completed against 11 - and the operator has decided against it with
        that measurement in view. The number to watch is how long the deficit
        stays open.

        Reading is also what applies new ledger rows, so a restart mid-recovery
        resumes from the stored row and folds in whatever settled while the
        service was down. A failure here returns the last stored deficit rather
        than raising: this runs in the order path.
        """
        if now_ms is None:
            now_ms = int(time.time() * 1000)
        try:
            return self.apply_realised_to_deficit(now_ms, plan_steps)
        except Exception as exc:  # noqa: BLE001 - sizing must not stop trading
            print(
                f"recovery deficit update failed: {type(exc).__name__}: {exc}",
                flush=True,
            )
            try:
                # ROLL BACK FIRST. The fold marks each ledger row applied and
                # writes the new deficit in one transaction; half of that left
                # open would be committed by the next unrelated write, and the
                # rows would be marked applied against a deficit that never saw
                # them - money missing from the deficit, silently.
                self.db.rollback()
                return self.stored_deficit()
            except Exception as inner:  # noqa: BLE001 - and still no raising
                print(
                    f"recovery deficit read failed: "
                    f"{type(inner).__name__}: {inner}",
                    flush=True,
                )
                return RecoveryState(0.0, False, 0, 0, 0)

    def recovery_deficit(
        self, plan_steps: int = DEFAULT_RECOVERY_STEPS, now_ms: int | None = None
    ) -> float:
        """Dollars of realised net loss still missing. 0.0 when nothing is owed.

        The named accessor for other code. It brings the deficit up to date
        with the ledger first, so it is never a stale read; use
        `stored_deficit()` for the persisted row without that update.
        """
        return self.recovery_state(plan_steps, now_ms).deficit

    def recovery_required_per_trade(
        self, plan_steps: int = DEFAULT_RECOVERY_STEPS, now_ms: int | None = None
    ) -> float:
        """The deficit divided by the steps left in the plan, floored at 1 step.

        What one upsized trade has to be able to win, net of fees, before the
        upsize is allowed to apply. 0.0 when there is no deficit.
        """
        return self.recovery_state(plan_steps, now_ms).required_per_trade()

    def recovery_is_active(
        self, plan_steps: int = DEFAULT_RECOVERY_STEPS, now_ms: int | None = None
    ) -> bool:
        """Is money still missing? Recovery runs on that alone."""
        return self.recovery_state(plan_steps, now_ms).active

    def stored_deficit(self) -> RecoveryState:
        """The persisted deficit, exactly as it was last written."""
        row = self.db.execute(
            "SELECT deficit, markets, opened_ms, steps FROM recovery_deficit "
            "WHERE id=1"
        ).fetchone()
        if row is None:
            return RecoveryState(0.0, False, 0, 0, 0)
        deficit = float(row[0] or 0.0)
        return RecoveryState(
            deficit, deficit > 0, int(row[1] or 0), int(row[2] or 0),
            int(row[3] or 0),
        )

    def _write_deficit(self, state: RecoveryState, now_ms: int) -> RecoveryState:
        self.db.execute(
            "INSERT INTO recovery_deficit (id, deficit, markets, opened_ms, "
            "updated_ms, steps) VALUES (1,?,?,?,?,?) ON CONFLICT(id) DO UPDATE "
            "SET deficit=excluded.deficit, markets=excluded.markets, "
            "opened_ms=excluded.opened_ms, updated_ms=excluded.updated_ms, "
            "steps=excluded.steps",
            (state.deficit, state.markets, state.opened_ms, now_ms, state.steps),
        )
        self.db.commit()
        return state

    def apply_realised_to_deficit(
        self, now_ms: int, plan_steps: int = DEFAULT_RECOVERY_STEPS
    ) -> RecoveryState:
        """Fold every realised figure into the deficit, each one exactly once.

        `recovery_applied` holds the amount already folded in for that market,
        so a row the exchange later revises moves the deficit by the DELTA
        between the two figures. A cash-out banked at +0.55 and settled at
        +0.54 costs the deficit one cent, not another 54.

        The three transitions the operator stated are one line of arithmetic:
        a loss is a negative delta and ADDS to the deficit, a profit subtracts
        from it, and it can never go below zero - which is the same thing as
        "as soon as it reaches $0.00, recovery is off". Profit from a BASE-size
        trade counts exactly as much as profit from an upsized one; the ledger
        does not record what size won the money back and it does not matter.

        A LOSS RESETS THE PLAN to `plan_steps`. The per-trade requirement is
        deficit/steps, so without this the divisor would shrink while the
        deficit grew, and the requirement would run away upwards - switching
        the upsize off exactly where the operator wants it on.

        Rows are applied in window order. The deficit is path-dependent, so
        that order is part of the answer; a settlement that arrives for an
        older market than one already applied is folded in where it lands.
        """
        state = self.stored_deficit()
        deficit, markets = state.deficit, state.markets
        opened_ms, steps = state.opened_ms, state.steps
        # FOLDED FROM `realised_events`, IN BROKER REALISATION ORDER.
        #
        # The deficit is path-dependent because profit stops reducing it at
        # zero, so the order is part of the answer and it has to be the order
        # the money actually moved in.
        #
        # Two earlier keys were both wrong. `window_ms` is the market's clock:
        # an early cash-out realises while its own window is still running and
        # can realise BEFORE a market that opened earlier, so a profit could be
        # applied to a debt that did not exist yet. `first_ms` is OUR discovery
        # time: a settlement credited at 17:00:08 was first banked at 17:15:25,
        # 917 seconds later, so a delayed settlement replayed in the wrong
        # place. `realised_ms` is the broker's own timestamp, and `event_id`
        # is the tie-breaker, so a duplicate sync and a restart produce the
        # same number - the deficit is a fact about the account, not about the
        # run that computed it.
        #
        # THE EPOCH IS COMPARED ON REALISATION, NOT ON THE MARKET'S TIME. A
        # position already open when recovery activates settles afterwards, so
        # its money becomes real afterwards and it COUNTS. Only events whose
        # money was already real before activation are excluded - those are
        # history, whenever the broker happens to deliver them.
        epoch = self.recovery_epoch_ms()
        for event_id, ticker, amount, applied, realised_ms in self.db.execute(
            "SELECT event_id, ticker, amount, COALESCE(applied, 0.0), realised_ms "
            "FROM realised_events ORDER BY realised_ms, event_id"
        ).fetchall():
            if epoch and realised_ms < epoch:
                if applied == 0.0:
                    self.db.execute(
                        "UPDATE realised_events SET applied = amount "
                        "WHERE event_id = ?",
                        (event_id,),
                    )
                continue
            pnl = amount
            realised = float(pnl or 0.0)
            delta = realised - float(applied or 0.0)
            if abs(delta) < 1e-9:
                continue
            if deficit > 0:
                markets += 1
            deficit = max(0.0, deficit - delta)
            if delta < 0:
                steps = int(plan_steps)      # a fresh loss, a fresh plan
            if deficit < DEFICIT_CLEARED:
                deficit, markets, opened_ms, steps = 0.0, 0, 0, 0
            elif not opened_ms:
                opened_ms = now_ms
            self.db.execute(
                "UPDATE realised_events SET applied=? WHERE event_id=?",
                (realised, event_id),
            )
            # Kept in step so the per-market view and the event view agree.
            self.db.execute(
                "UPDATE daily_ledger SET recovery_applied=pnl WHERE ticker=?",
                (ticker,),
            )
        return self._write_deficit(
            RecoveryState(deficit, deficit > 0, markets, opened_ms, steps), now_ms
        )

    def consume_recovery_step(
        self, now_ms: int, plan_steps: int = DEFAULT_RECOVERY_STEPS
    ) -> None:
        """One upsized trade is on the book. Never raises.

        Only a trade that actually TOOK the upsize spends a step: a qualifying
        trade that fell back to base size because its profit could not cover
        the per-trade share has changed nothing about the plan. That keeps the
        requirement roughly flat as the deficit falls - 1.64/4 = 0.41, then
        after a 0.47 recovery, 1.17/3 = 0.39 - instead of rising as the money
        comes back.

        Called on the post-order path, where a raising write is an outage: the
        contracts are already at the exchange by the time this runs.
        """
        try:
            # The current state, not the stored row: anything that settled
            # between the size decision and the fill belongs in the plan this
            # step is taken out of.
            state = self.recovery_state(plan_steps, now_ms)
            if not state.active:
                return
            self._write_deficit(
                RecoveryState(
                    state.deficit, state.active, state.markets,
                    state.opened_ms, max(0, state.steps - 1),
                ),
                now_ms,
            )
        except Exception as exc:  # noqa: BLE001 - bookkeeping must not stop trading
            print(
                f"consume_recovery_step failed: {type(exc).__name__}: {exc}",
                flush=True,
            )

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

    def save_details(self, key: str, body, now_ms: int) -> None:
        """Freeze the DETAILS text for one decision. NEVER raises.

        THIS SITS ON THE POST-ORDER PATH, so it is bookkeeping standing
        directly behind real money and it must not be able to stop the loop.
        It could: passing `decision_record`'s list of (label, value) pairs
        where a TEXT column was expected raised ProgrammingError one second
        after every fill, and the watchdog restarted the service roughly every
        fifteen minutes from 09:04 to 10:50 on 2026-09-22. The position was
        never at risk - only the order call may mark a proposal failed - but
        the service died each time, and a service that dies on a schedule
        eventually dies in a window that matters.

        `body` is coerced rather than type-checked, because the caller having
        the wrong type is exactly the case that must not propagate.
        """
        try:
            if not isinstance(body, str):
                if isinstance(body, (list, tuple)):
                    body = "\n".join(str(item) for item in body)
                else:
                    body = str(body)
            self.db.execute(
                "INSERT OR REPLACE INTO decision_details VALUES (?,?,?)",
                (str(key), body, int(now_ms)),
            )
            self.db.commit()
        except (sqlite3.Error, TypeError, ValueError) as exc:
            print(f"details not saved for {key}: {type(exc).__name__}: {exc}",
                  flush=True)

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

    def record_realised(
        self, ticker: str, window_ms: int, pnl: float, won: bool | None,
        source: str, now_ms: int, realised_ms: int | None = None,
    ) -> None:
        """Bank one market's final money, and emit its cash-flow event.

        `realised_ms` is the BROKER's timestamp for when the money became
        real - a settlement's `settled_ms`, an exit's `filled_ms`. It defaults
        to `now_ms` only because a caller that does not know cannot do better;
        every caller that does know passes it, because the deficit is folded
        in realisation order and our own discovery time has been observed 917
        seconds late.

        For an `exchange` event the amount recorded is the REMAINDER - the
        settlement total minus whatever cash-outs on the same market already
        banked - so a partial exit and the settlement of the rest are two
        events at two times that sum to the exchange's own figure.

        A row is never deleted and never silently replaced by a smaller
        figure that merely arrived later. The broker is still the authority -
        if its settled number differs from what the cash-out banked, the
        broker wins - but the change is counted in `revisions` and the source
        recorded, so a disagreement shows up instead of being absorbed.
        """
        self._emit_event(ticker, window_ms, pnl, source, now_ms, realised_ms)
        existing = self.db.execute(
            "SELECT pnl, source, revisions FROM daily_ledger WHERE ticker=?", (ticker,)
        ).fetchone()
        if existing is None:
            self.db.execute(
                "INSERT INTO daily_ledger (ticker, window_ms, pnl, won, source, "
                "first_ms, updated_ms, revisions) VALUES (?,?,?,?,?,?,?,0)",
                (ticker, window_ms, pnl, None if won is None else int(won),
                 source, now_ms, now_ms),
            )
            self.db.commit()
            return
        previous, previous_source, revisions = existing
        if source == previous_source or abs(previous - pnl) < 1e-9:
            return
        # Only the exchange may revise a figure the bot banked locally.
        if source != "exchange":
            return
        self.db.execute(
            "UPDATE daily_ledger SET pnl=?, won=?, source=?, updated_ms=?, "
            "revisions=? WHERE ticker=?",
            (pnl, None if won is None else int(won), source, now_ms,
             revisions + 1, ticker),
        )
        self.db.commit()
        print(
            f"ledger revision [{ticker}]: {previous_source} {previous:+.4f} -> "
            f"exchange {pnl:+.4f}",
            flush=True,
        )

    def sync_events_from_fills(self, now_ms: int) -> None:
        """One cash-flow event per SELL FILL, keyed on the broker's fill id.

        `cash_out:{ticker}` was not a unique identity. Two partial exits in the
        same market collapse onto one key, and the second silently overwrites
        the first - one of the two cash flows disappears from an order-
        sensitive fold, and the settlement remainder is then computed against
        the wrong banked total. The broker's `fill_id` is unique per execution
        and stable across redelivery, which is exactly what an event id has to
        be.

        Entry cost and fee are allocated to the portion sold, so a partial
        carries its own share and the remainder is left owing the rest.
        """
        floor = now_ms - self.LEDGER_SYNC_LOOKBACK_MS
        touched: set[str] = set()
        for fill in self._dicts(
            "SELECT fill_id, ticker, side, count, yes_price, no_price, fee_cost, "
            "filled_ms, window_ms FROM fills WHERE action = 'sell' AND filled_ms >= ?",
            (floor,),
        ):
            event_id = f"cash_out:{fill['fill_id']}"
            basis = self._entry_basis(fill["ticker"])
            if basis is None:
                continue
            entry_price, bought, entry_fee = basis
            sold = float(fill["count"] or 0)
            if sold <= 0:
                continue
            # A sell of the YES side exits an UP position at `yes_price`; a
            # sell of NO exits a DOWN position, whose price is the no side.
            exit_price = float(
                fill["no_price"] if fill["side"] == "no" else fill["yes_price"]
            )
            share = sold / bought if bought else 1.0
            amount = round(
                (exit_price - entry_price) * sold
                - entry_fee * share
                - float(fill["fee_cost"] or 0.0),
                6,
            )
            self.record_realised_event(
                event_id, fill["ticker"], int(fill["filled_ms"]), amount,
                "cash_out", now_ms, fill["window_ms"],
            )
            touched.add(fill["ticker"])

        # SUPERSEDE THE PROVISIONALS, PER TICKER, ALL OF THEM.
        #
        # One exit ORDER can produce several FILLS - a resting sell walked
        # through two price levels is two executions at two instants. The
        # provisional event was keyed on the moment the bot noticed, which
        # matches at most one of them, so matching provisional-to-fill by
        # timestamp would leave the rest behind and double-count the sale.
        # Once ANY broker fill exists for a market, every provisional for that
        # market is replaced by the real ones.
        for ticker in touched:
            # A provisional id is "cash_out:{ticker}:{ms}" - three parts. A
            # broker-backed one is "cash_out:{fill_id}" - two.
            provisionals = [
                event["event_id"] for event in self.settlement_events(ticker)
                if event["source"] != "exchange"
                and event["event_id"].count(":") >= 2
            ]
            if not provisionals:
                continue
            self.db.executemany(
                "DELETE FROM realised_events WHERE event_id = ?",
                [(event_id,) for event_id in provisionals],
            )
            self.db.commit()
            # THE AMOUNT OR THE TIMESTAMP HAS CHANGED, so the ordered fold is
            # no longer a valid continuation of the stored figure. Replay it.
            self.rebuild_deficit(now_ms)

    def _entry_basis(self, ticker: str) -> tuple[float, float, float] | None:
        """(price, contracts, fee) of the BUY side, from the broker's fills."""
        row = self.db.execute(
            "SELECT SUM(count), SUM(fee_cost), SUM(count * CASE WHEN side='no' "
            "THEN no_price ELSE yes_price END) FROM fills "
            "WHERE ticker = ? AND action = 'buy'",
            (ticker,),
        ).fetchone()
        if not row or not row[0]:
            return None
        bought = float(row[0])
        return round(float(row[2]) / bought, 6), bought, float(row[1] or 0.0)

    def _emit_event(
        self, ticker: str, window_ms: int, pnl: float, source: str,
        now_ms: int, realised_ms: int | None,
    ) -> None:
        """One cash-flow event per (source, market), at broker time.

        A settlement carries the REMAINDER after any cash-out on the same
        market, so the events sum to the exchange's total while each keeps its
        own timestamp. That is what lets a partial exit at 18:57 and the
        settlement of the rest at 19:00 replay as the two separate cash flows
        they were, instead of one aggregate at whichever time we noticed.
        """
        when = int(realised_ms if realised_ms is not None else now_ms)
        amount = float(pnl)
        if source != "exchange":
            # A PROVISIONAL exit event, keyed on the instant the cash flow
            # happened rather than on the ticker: two partial exits are two
            # instants and therefore two events, where `cash_out:{ticker}`
            # would have let the second overwrite the first. The broker's own
            # fill id supersedes this as soon as the fill syncs.
            self.record_realised_event(
                f"cash_out:{ticker}:{when}", ticker, when, amount,
                source, now_ms, window_ms,
            )
            return
        if source == "exchange":
            banked = sum(
                float(event["amount"])
                for event in self.settlement_events(ticker)
                if event["source"] != "exchange"
            )
            amount = round(amount - banked, 6)
            if abs(amount) < 1e-9 and banked:
                return
        self.record_realised_event(
            f"{source}:{ticker}", ticker, when, amount, source, now_ms, window_ms
        )

    # How far back a sync looks. Kalshi settles in batches and can credit a
    # market hours after its close, so the window has to be wider than the lag,
    # not wider than the day.
    LEDGER_SYNC_LOOKBACK_MS = 3 * 86_400_000

    def sync_ledger_from_settlements(self, now_ms: int) -> None:
        """Fold recent broker settlements into the ledger. The exchange wins.

        SELECTED ON THE SETTLEMENT TIMESTAMP, not the market's own day.

        The original filtered `COALESCE(window_ms, settled_ms) >= start of
        today`, which silently dropped every market that closed before midnight
        and settled after it. The 23:45 window carries a previous-day
        `window_ms`, so at 00:05 it fell outside the filter, never reached
        `daily_ledger`, and therefore could never open a recovery deficit - a
        loss that financed nothing and appeared nowhere.

        Looking back three days instead is safe because the fold is IDEMPOTENT:
        `daily_ledger` is keyed by ticker and `record_realised` refuses to
        re-apply a figure it already holds, so re-reading an old settlement
        writes nothing and moves no deficit. Re-reading is cheap; missing one
        is not.

        `window_ms` is still stored per row, because `ledger_today` reports by
        the MARKET's day - a 23:45 loss belongs to the day it was traded even
        when the money arrives the next morning.
        """
        floor = now_ms - self.LEDGER_SYNC_LOOKBACK_MS
        epoch = self.recovery_epoch_ms()
        for row in self.db.execute(
            "SELECT ticker, COALESCE(window_ms, settled_ms), pnl, settled_ms "
            "FROM settlements WHERE COALESCE(settled_ms, window_ms) >= ?",
            (floor,),
        ).fetchall():
            ticker, window_ms, pnl, settled_ms = row
            window_ms = int(window_ms)
            self.record_realised(
                ticker, window_ms, float(pnl), float(pnl) > 0, "exchange", now_ms,
                realised_ms=int(settled_ms) if settled_ms else None,
            )
            # A MARKET OLDER THAN THE RECOVERY EPOCH IS HISTORY, NOT A DEBT.
            #
            # Widening this lookback to catch the midnight settlements pulled
            # three days of finished markets into the ledger, all unapplied.
            # The fold then replayed them on top of the live figure and drove
            # the deficit from $0.85 to $10.90 - a requirement of $2.73 a
            # trade, which no two-contract position can ever satisfy, so the
            # add-on would have sat silently disabled while looking active.
            # Backfilled history is marked applied on arrival, so it can never
            # retroactively open a debt that was already settled or forgiven.
            if epoch and window_ms < epoch:
                self.db.execute(
                    "UPDATE daily_ledger SET recovery_applied = pnl "
                    "WHERE ticker = ? AND recovery_applied IS NULL",
                    (ticker,),
                )
        self.db.commit()
        # The deficit folds from events, not from this table.
        self.sync_events_from_settlements(now_ms)

    # ------------------------------------------------- capital and reservations

    def record_capital_day(self, capital) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO capital_days (ny_day, reconciled_cash, "
            "open_exposure, base_contracts, account_ceiling, reconciled_ms) "
            "VALUES (?,?,?,?,?,?)",
            (capital.ny_day, capital.reconciled_cash, capital.open_exposure,
             capital.base_contracts, capital.account_ceiling,
             capital.reconciled_ms),
        )
        self.db.commit()

    def capital_for_day(self, ny_day: str):
        rows = self._dicts(
            "SELECT * FROM capital_days WHERE ny_day = ?", (ny_day,)
        )
        return _capital(rows[0]) if rows else None

    def previous_capital_day(self, ny_day: str):
        rows = self._dicts(
            "SELECT * FROM capital_days WHERE ny_day < ? ORDER BY ny_day DESC "
            "LIMIT 1",
            (ny_day,),
        )
        return _capital(rows[0]) if rows else None

    def open_position_cost(self) -> float:
        """What open positions COST, not what they are marked at.

        Capital for sizing includes committed money at its cost basis, so a
        winning position cannot inflate tomorrow's tier on a gain that has not
        settled. `open_mark` is the mark and is deliberately not used here.
        """
        row = self.db.execute(
            f"SELECT COALESCE(SUM(count * COALESCE(fill_price, entry_limit)), 0) "
            f"FROM trade_proposals WHERE status IN {HELD_SQL}"
        ).fetchone()
        return round(float(row[0] or 0.0), 6)

    def reserve_funds(
        self, key: str, amount: float, now_ms: int, available: float | None = None
    ) -> bool:
        """Claim funds, checked against what is actually available. Atomic.

        A UNIQUE KEY ALONE DOES NOT PREVENT OVERSPENDING. It stops the SAME
        order reserving twice; it does nothing about two DIFFERENT orders - a
        base entry and a recovery add in the same poll - each reserving a
        different key against the same balance and together exceeding it. So
        the check and the insert happen in one IMMEDIATE transaction, and the
        sum of live reservations plus this request must fit inside
        `available`.

        `available` is the spendable figure the caller just read. Passing None
        keeps the old unchecked behaviour and is only for callers that have
        already done the arithmetic themselves.
        """
        amount = round(float(amount), 6)
        try:
            self.db.execute("BEGIN IMMEDIATE")
            if available is not None:
                held = self.db.execute(
                    "SELECT COALESCE(SUM(amount), 0) FROM fund_reservations "
                    "WHERE key != ?",
                    (key,),
                ).fetchone()[0]
                if round(float(held or 0.0) + amount, 6) > round(available, 6):
                    self.db.execute("ROLLBACK")
                    return False
            self.db.execute(
                "INSERT INTO fund_reservations (key, amount, created_ms, state) "
                "VALUES (?,?,?,'held')",
                (key, amount, now_ms),
            )
            self.db.execute("COMMIT")
            return True
        except sqlite3.IntegrityError:
            self.db.execute("ROLLBACK")
            return False

    def release_funds(self, key: str, reason: str = "released") -> None:
        """Free a reservation. Only call this once the outcome is KNOWN.

        Known means the broker has told us: cancelled, rejected, expired, or
        filled and therefore now counted as position exposure instead. A
        reservation released on a timer while its order is still resting hands
        the same dollars out twice.
        """
        self.db.execute(
            "DELETE FROM fund_reservations WHERE key = ?", (key,)
        )
        self.db.commit()
        if reason != "released":
            print(f"reservation released [{key}]: {reason}", flush=True)

    def reservation_keys(self) -> list[str]:
        return [
            row[0] for row in self.db.execute("SELECT key FROM fund_reservations")
        ]

    def reserved_funds(self, now_ms: int) -> float:
        """Dollars currently claimed by orders whose outcome is not yet known.

        NOTHING EXPIRES HERE. A reservation used to time out after five
        minutes, which is wrong in the exact case it matters: an order that is
        still resting, or one whose submission outcome is unknown, has not
        released its money and a clock cannot decide that it has. Releases are
        driven by reconciliation - `RecoveryAddRunner.reconcile` and the
        order path - so the only way funds come back is the broker saying so.
        """
        row = self.db.execute(
            "SELECT COALESCE(SUM(amount), 0) FROM fund_reservations"
        ).fetchone()
        return round(float(row[0] or 0.0), 6)

    def record_realised_event(
        self, event_id: str, ticker: str, realised_ms: int, amount: float,
        source: str, now_ms: int, window_ms: int | None = None,
    ) -> None:
        """One cash flow, at the BROKER's timestamp for it.

        Idempotent on `event_id`: a re-sync writes nothing. The amount may be
        revised (a cash-out banked at +0.55 that the exchange settles at
        +0.54), and the fold applies the delta.
        """
        existing = self.db.execute(
            "SELECT amount FROM realised_events WHERE event_id = ?", (event_id,)
        ).fetchone()
        if existing is None:
            self.db.execute(
                "INSERT INTO realised_events (event_id, ticker, realised_ms, "
                "amount, source, window_ms, applied, recorded_ms) "
                "VALUES (?,?,?,?,?,?,NULL,?)",
                (event_id, ticker, int(realised_ms), float(amount), source,
                 window_ms, now_ms),
            )
        elif abs(float(existing[0]) - float(amount)) > 1e-9:
            self.db.execute(
                "UPDATE realised_events SET amount = ?, realised_ms = ?, "
                "recorded_ms = ? WHERE event_id = ?",
                (float(amount), int(realised_ms), now_ms, event_id),
            )
        self.db.commit()

    def last_exit_fill_ms(self, ticker: str) -> int | None:
        """When the broker says the exit actually traded. None if unknown."""
        row = self.db.execute(
            "SELECT MAX(filled_ms) FROM fills WHERE ticker = ? AND action = 'sell'",
            (ticker,),
        ).fetchone()
        return int(row[0]) if row and row[0] else None

    def settlement_events(self, ticker: str) -> list[dict]:
        return self._dicts(
            "SELECT * FROM realised_events WHERE ticker = ? "
            "ORDER BY realised_ms, event_id",
            (ticker,),
        )

    def sync_events_from_settlements(self, now_ms: int) -> None:
        """Turn the broker's record into an ordered cash-flow sequence.

        A cash-out is a PROVISIONAL event at its fill time. The exchange's
        settlement is the total for that market, so its own event carries the
        REMAINDER - the total minus whatever the cash-outs already banked -
        at `settled_ms`. The events therefore sum to the exchange's figure
        while each keeps its own true timestamp, which is what a partial exit
        followed by a settlement actually looks like.
        """
        floor = now_ms - self.LEDGER_SYNC_LOOKBACK_MS
        for row in self.db.execute(
            "SELECT ticker, COALESCE(window_ms, settled_ms) AS wms, pnl, settled_ms "
            "FROM settlements WHERE COALESCE(settled_ms, window_ms) >= ?",
            (floor,),
        ).fetchall():
            ticker, window_ms, pnl, settled_ms = row
            if not settled_ms:
                continue
            banked = sum(
                float(e["amount"]) for e in self.settlement_events(ticker)
                if e["source"] != "exchange"
            )
            remainder = round(float(pnl) - banked, 6)
            if abs(remainder) < 1e-9 and banked:
                continue  # the cash-out already accounted for all of it
            self.record_realised_event(
                f"exchange:{ticker}", ticker, int(settled_ms), remainder,
                "exchange", now_ms, int(window_ms),
            )

    def position_entry_ms(self, window_open: int, ticker: str) -> int | None:
        """When the base position was actually BOUGHT, from the broker's fills.

        Not the window open, and not the proposal's creation time. "Crossed
        since entry" has to mean since the money went in: on 2026-09-22 the
        add for KXBTC15M-26SEP221500-00 was evaluated at 18:49:40 against a
        fill that happened at 18:49:42, while the crossing check looked all
        the way back to the 18:45 window open - so four minutes and forty-two
        seconds of BRTI history from BEFORE we held anything were allowed to
        veto the add.

        None when it cannot be established. The caller must treat that as
        unknown and refuse, never as "no crossing" and never as "crossed".
        """
        row = self.db.execute(
            "SELECT MIN(filled_ms) FROM fills WHERE ticker = ? AND action = 'buy'",
            (ticker,),
        ).fetchone()
        if row and row[0]:
            return int(row[0])
        # No broker fill yet - the position may be seconds old. The proposal's
        # own timestamp is a lower bound on when we could have been holding.
        row = self.db.execute(
            f"SELECT MIN(created_at) FROM trade_proposals WHERE window_open = ? "
            f"AND ticker = ? AND status IN {HELD_SQL}",
            (window_open, ticker),
        ).fetchone()
        return int(row[0]) if row and row[0] else None

    def recovery_epoch_ms(self) -> int:
        """When recovery accounting began. Markets older than this are history.

        Compared against the market's OWN time, not when the settlement was
        discovered. A market traded after the epoch counts even if the broker
        delivers its settlement tomorrow; a market traded before it never
        counts however late it arrives. The distinction matters because the
        sync deliberately looks back days to catch settlements that cross
        midnight.
        """
        return int(self.get_setting("recovery_epoch_ms", 0.0))

    def set_recovery_epoch(self, epoch_ms: int, now_ms: int) -> None:
        self.set_setting("recovery_epoch_ms", float(epoch_ms), now_ms)

    def day_start_ms(self, now_ms: int) -> int:
        """The start of the accounting day. NEW YORK, matching the exchange.

        THE WHOLE DAY MOVES TOGETHER. The capital review, the realised ledger,
        the trade counters and the daily loss floor were split across two
        calendars for a few hours on 2026-09-22 - the review on New York, the
        rest on UTC - which is worse than either alone: a loss counted in one
        day and a floor measured over another is a floor that does not bound
        what it claims to.

        Kalshi's own utilisation resets at midnight New York, so that is the
        boundary the account is actually kept on, but the exchange's reset does
        not by itself define OUR accounting fields - it is the reason to pick
        the same day for all of them, deliberately, rather than to inherit one
        field at a time.
        """
        from .capital import ny_day_start_ms

        return ny_day_start_ms(now_ms)

    def rebuild_deficit(self, now_ms: int, plan_steps: int | None = None):
        """Replay the deficit from zero over every event, in realisation order.

        The incremental fold is a valid continuation only while history is
        append-only. It is not, twice over: the exchange revises a figure a
        cash-out banked, and a provisional exit is replaced by the broker's
        real fills - which can be several, at different instants, summing to a
        different amount. Either changes an event the fold has already applied,
        and patching a path-dependent total in place after the fact is how a
        floored quantity silently diverges.

        So the whole sequence is replayed. It is deterministic - ordered by
        realisation time with the event id as tie-breaker - so a rebuild and a
        restart produce the same number, which is the property that makes the
        deficit a fact about the account rather than about the run.
        """
        steps = int(plan_steps or DEFAULT_RECOVERY_STEPS)
        self.db.execute("UPDATE realised_events SET applied = NULL")
        self.db.execute("DELETE FROM recovery_deficit")
        self.db.commit()
        return self.apply_realised_to_deficit(now_ms, steps)

    def migrate_day_boundary(self, now_ms: int) -> float:
        """Carry today's pre-New-York losses across the timezone change. Once.

        Returns the carried amount (<= 0). Idempotent: the marker is written
        with the day it applies to, so a restart cannot bank it twice.
        """
        from .capital import ny_day

        day = ny_day(now_ms)
        marker = self.db.execute(
            "SELECT text_value FROM settings_text WHERE key='day_boundary_migrated'"
        ).fetchone()
        if marker and marker[0] == day:
            return self.get_setting("day_boundary_carry", 0.0)
        utc_start = now_ms - (now_ms % 86_400_000)
        ny_start = self.day_start_ms(now_ms)
        carry = 0.0
        if ny_start > utc_start:
            _, _, carry = self.exchange_record(utc_start)
            _, _, after = self.exchange_record(ny_start)
            carry = round(min(0.0, carry - after), 6)
        self.set_setting("day_boundary_carry", carry, now_ms)
        self.set_setting_text("day_boundary_migrated", day, now_ms)
        if carry:
            print(
                f"day boundary moved to New York: carrying {carry:+.4f} of "
                f"losses already booked today so the floor is not refunded",
                flush=True,
            )
        return carry

    def day_boundary_carry(self, now_ms: int) -> float:
        """The carried loss, but only on the day the migration happened."""
        from .capital import ny_day

        marker = self.db.execute(
            "SELECT text_value FROM settings_text WHERE key='day_boundary_migrated'"
        ).fetchone()
        if not marker or marker[0] != ny_day(now_ms):
            return 0.0
        return self.get_setting("day_boundary_carry", 0.0)

    def ledger_today(self, now_ms: int | None = None) -> tuple[int, int, float]:
        """(markets, winners, dollars) realised today, from the ledger alone."""
        import time

        if now_ms is None:
            now_ms = int(time.time() * 1000)
        start = self.day_start_ms(now_ms)
        row = self.db.execute(
            "SELECT COUNT(*), COALESCE(SUM(pnl > 0), 0), COALESCE(SUM(pnl), 0) "
            "FROM daily_ledger WHERE window_ms >= ?",
            (start,),
        ).fetchone()
        return int(row[0] or 0), int(row[1] or 0), float(row[2] or 0.0)

    def ledger_tickers_today(self, now_ms: int | None = None) -> set[str]:
        import time

        if now_ms is None:
            now_ms = int(time.time() * 1000)
        start = self.day_start_ms(now_ms)
        return {
            row[0] for row in self.db.execute(
                "SELECT ticker FROM daily_ledger WHERE window_ms >= ?", (start,)
            )
        }

    def open_exposure(self) -> float:
        """Open positions marked to the bid, EXCLUDING anything already banked.

        This is the half of the headline that is allowed to move, and the
        exclusion is the fix: a position sold four minutes ago is still in the
        last 60-second mark, and adding that to a ledger which has already
        banked its proceeds counts the same dollar twice.
        """
        import json

        raw = self.db.execute(
            "SELECT text_value FROM settings_text WHERE key='open_mark_detail'"
        ).fetchone() if self._has_settings_text() else None
        if not raw:
            return self.get_setting("open_mark", 0.0)
        try:
            marks = json.loads(raw[0])
        except (ValueError, TypeError):
            return self.get_setting("open_mark", 0.0)
        banked = self.ledger_tickers_today()
        return sum(
            float(value) for ticker, value in marks.items() if ticker not in banked
        )

    def _has_settings_text(self) -> bool:
        return bool(self.db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='settings_text'"
        ).fetchone())

    def set_setting_text(self, key: str, value: str, now_ms: int) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO settings_text VALUES (?,?,?)",
            (key, value, now_ms),
        )
        self.db.commit()

    # ------------------------------------------- the conditional recovery add

    def add_budget_committed(self) -> tuple[float, int]:
        """(dollars, fills) the add-on has EVER had filled. Never decreases."""
        row = self.db.execute(
            "SELECT committed, fills FROM recovery_add_budget WHERE id = 1"
        ).fetchone()
        return (float(row[0]), int(row[1])) if row else (0.0, 0)

    def account_room(
        self, account_size: float, balance: float, exposure: float
    ) -> float:
        """Dollars this order may spend, against a $30 TESTING ACCOUNT.

        THE CORRECTED MODEL. The $30 was described as a testing account, and a
        lifetime purchase cap is not that: it counts money that came back.
        Three profitable $9 adds would exhaust a $30 "budget" while the
        account itself was larger than when it started, and recovery would
        stop for lack of a number rather than lack of funds.

        An account is a stock, not a running total. What limits an order is
        what is available RIGHT NOW:

            room = min(broker cash, account_size - open and resting exposure)

        Both terms matter. The broker balance stops us spending money we do
        not have; the account ceiling stops the test growing past the size it
        was authorised at, however well it goes. Cumulative spend is still
        recorded, because it says how much trading the test has done - but it
        does not gate anything.

        A balance or exposure that could not be read is passed as a negative
        number and yields no room: an unknown account is not an empty one, but
        it is certainly not a licence to spend.
        """
        if balance < 0 or exposure < 0:
            return -1.0
        return round(min(balance, account_size - exposure), 6)

    def add_budget_room(self, ceiling: float, resting: float) -> float:
        """DEPRECATED lifetime purchase-spend headroom. Not a gate.

        Kept so the cumulative figure stays visible in reports and tests, but
        `account_room` is what decides whether an order may be placed. See its
        docstring for why a lifetime cap was the wrong instrument.

        THREE DIFFERENT LIMITS, AND THIS IS ONLY ONE OF THEM. The $30 the
        operator authorised is a cumulative purchase-spend ceiling: every
        dollar the add-on has ever paid for contracts, plus whatever is
        resting unfilled right now. It is NOT a loss budget - a run of
        PROFITABLE adds exhausts it just as fast as a run of losing ones,
        because the money was spent either way and came back as settlement
        rather than as headroom.

        The other two are reported beside it by `add_budget_state` and are
        deliberately not merged into this number:

          purchase spend   cumulative, never decreases  <- the authorised cap
          current exposure resting orders, transient
          realised losses  what the add-on actually cost, can be negative

        Silently treating the cap as a loss budget would let it run far longer
        than authorised; treating it as pure exposure would let it reset on
        every fill. It is neither, so all three are kept apart.

        `resting` is passed in from the broker rather than inferred, because
        an order this process did not place - a manual one, or one left by a
        previous run - is still exposure.
        """
        committed, _ = self.add_budget_committed()
        return round(ceiling - committed - max(0.0, resting), 6)

    def add_budget_state(
        self, account_size: float, resting: float, balance: float | None = None
    ) -> dict:
        """The limits, named and kept apart so a report cannot conflate them.

        `account_room` is the one that gates an order. `lifetime_spend` is
        history and gates nothing.
        """
        spend, fills = self.add_budget_committed()
        summary = self.add_pnl_summary()
        held = self.get_setting("open_mark", 0.0)
        exposure = round(max(0.0, resting) + max(0.0, held), 6)
        return {
            "account_size": round(account_size, 6),
            "broker_balance": None if balance is None else round(balance, 6),
            "open_and_resting_exposure": exposure,
            "account_room": (
                None if balance is None
                else self.account_room(account_size, balance, exposure)
            ),
            "lifetime_spend": round(spend, 6),
            "lifetime_spend_fills": fills,
            "realised_add_pnl": round(float(summary.get("pnl") or 0.0), 6),
            "realised_add_fees": round(float(summary.get("fees") or 0.0), 6),
        }

    def _bump_add_budget(self, dollars: float, now_ms: int) -> None:
        self.db.execute(
            "INSERT INTO recovery_add_budget (id, committed, fills, started_ms, "
            "updated_ms) VALUES (1, ?, 1, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET committed = committed + excluded.committed, "
            "fills = fills + 1, updated_ms = excluded.updated_ms",
            (round(dollars, 6), now_ms, now_ms),
        )

    def _dicts(self, sql: str, args: tuple = ()) -> list[dict]:
        """Rows as dicts, via a cursor-local factory.

        The connection's `row_factory` is left alone on purpose: most of this
        class reads tuples by position, and flipping it globally - which
        `clean_shadow_rows` does - would change what those reads return.
        """
        cursor = self.db.cursor()
        cursor.row_factory = sqlite3.Row
        return [dict(row) for row in cursor.execute(sql, args)]

    def open_add(self, window_open_ms: int) -> dict | None:
        """The add for this position, whatever state it is in. One per position."""
        rows = self._dicts(
            "SELECT * FROM recovery_adds WHERE window_open_ms = ?", (window_open_ms,)
        )
        return rows[0] if rows else None

    def record_add(self, row: dict) -> bool:
        """Create the add record. False if one already exists for the position.

        The INSERT is what makes a restart safe: re-evaluating the same
        position produces the same `client_order_id`, the insert is refused,
        and no second order is placed.
        """
        row.setdefault("created_ms", row.get("updated_ms"))
        columns = ", ".join(row)
        placeholders = ", ".join(f":{name}" for name in row)
        try:
            self.db.execute(
                f"INSERT INTO recovery_adds ({columns}) VALUES ({placeholders})", row
            )
            self.db.commit()
            return True
        except sqlite3.IntegrityError:
            return False

    def update_add(self, client_order_id: str, fields: dict) -> None:
        if not fields:
            return
        fields = dict(fields)
        fields["client_order_id"] = client_order_id
        assignments = ", ".join(
            f"{name} = :{name}" for name in fields if name != "client_order_id"
        )
        self.db.execute(
            f"UPDATE recovery_adds SET {assignments} WHERE client_order_id = "
            ":client_order_id",
            fields,
        )
        self.db.commit()

    def record_add_fill(
        self, client_order_id: str, count: float, price: float, fee: float,
        now_ms: int, is_taker: int | None = None, conditions: str | None = None,
    ) -> None:
        """Bank a fill once, and charge it to the lifetime budget once.

        Guarded on `filled_count = 0` so a fill reported twice - by the order
        poll and again by the fills sync - cannot charge the budget twice.
        """
        row = self.db.execute(
            "SELECT filled_count FROM recovery_adds WHERE client_order_id = ?",
            (client_order_id,),
        ).fetchone()
        if row is None or float(row[0] or 0) > 0:
            return
        self.db.execute(
            "UPDATE recovery_adds SET state = ?, filled_count = ?, fill_price = ?, "
            "fill_ms = ?, fee_paid = ?, is_taker = ?, conditions_at_fill = ?, "
            "updated_ms = ? WHERE client_order_id = ?",
            ("RECOVERY ADD EXECUTED", count, price, now_ms, fee, is_taker,
             conditions, now_ms, client_order_id),
        )
        self._bump_add_budget(count * price + (fee or 0.0), now_ms)
        self.db.commit()

    def add_pnl_summary(self) -> dict:
        """The add-on's own P&L, kept apart from the base position's.

        The point of the live test is whether the SECOND contract pays. Mixing
        it into the account total would answer a different question.
        """
        rows = self._dicts(
            "SELECT COUNT(*) AS adds, "
            "COALESCE(SUM(filled_count > 0), 0) AS filled, "
            "COALESCE(SUM(CASE WHEN state = 'RECOVERY ADD CANCELLED' THEN 1 END), 0) "
            "  AS cancelled, "
            "COALESCE(SUM(CASE WHEN state = 'RECOVERY ADD SKIPPED' THEN 1 END), 0) "
            "  AS skipped, "
            "COALESCE(SUM(realised_pnl), 0) AS pnl, "
            "COALESCE(SUM(fee_paid), 0) AS fees "
            "FROM recovery_adds"
        )
        return rows[0] if rows else {}

    def adds_needing_reconciliation(self) -> list[dict]:
        """Adds left mid-flight by a restart: placed, not resolved."""
        return self._dicts(
            "SELECT * FROM recovery_adds WHERE state = 'RECOVERY ADD PENDING' "
            "AND order_id IS NOT NULL"
        )

    def unsettled_filled_adds(self) -> list[dict]:
        return self._dicts(
            "SELECT * FROM recovery_adds WHERE filled_count > 0 AND settled = 0"
        )

    def money_snapshot(self, now_ms: int | None = None) -> "MoneySnapshot":
        """Every money figure a message shows, computed ONCE, together.

        The counts and the dollars used to come from different reads. The
        headline added `open_exposure` while the W/L count came from the
        ledger alone, so a position that had settled but not yet synced was in
        the dollars and not in the count. On 2026-09-22 the settlement recap
        read "+$3.05 - 30W-5L" when the ledger held 30W-6L: the money was
        right, the record was a market short, and the two disagreed because
        they were two snapshots of an account taken a minute apart.

        One method, one instant, one set of numbers. `headline` is the figure
        the operator compares against the Kalshi app - realised plus the open
        position marked to the bid - and `realised` is the part that is final.
        They are both here so a caller can show either without recomputing a
        different account.
        """
        import time

        if now_ms is None:
            now_ms = int(time.time() * 1000)
        markets, winners, realised = self.ledger_today(now_ms)
        has_mirror = bool(markets)
        if not markets:
            # No ledger rows yet - fall back to the mirror, as before.
            markets, winners, realised = self.exchange_record(
                self.day_start_ms(now_ms)
            )
            has_mirror = bool(markets) or bool(self.exchange_record()[0])
            if not markets:
                realised = 0.0
        open_mark = self.open_exposure()
        return MoneySnapshot(
            markets=markets, winners=winners, realised=round(realised, 6),
            open_mark=round(open_mark, 6), taken_ms=now_ms, has_mirror=has_mirror,
            lifetime=self.lifetime_record(),
            sessions=tuple(session_breakdown(self.session_rows(now_ms))),
            recovery=self.stored_deficit(),
            last_add=self.last_add_decision(),
        )

    def session_rows(self, now_ms: int | None = None) -> list[tuple[int, float]]:
        """(window_ms, pnl) for every market settled in the current NY day.

        The same rows `ledger_today` counts, so the session breakdown built
        from them sums exactly to the day's total rather than being a second
        opinion about it.
        """
        import time

        if now_ms is None:
            now_ms = int(time.time() * 1000)
        start = self.day_start_ms(now_ms)
        return [
            (int(row[0]), float(row[1]))
            for row in self.db.execute(
                "SELECT COALESCE(window_ms, first_ms), pnl FROM daily_ledger "
                "WHERE COALESCE(window_ms, first_ms) >= ? ORDER BY 1",
                (start,),
            )
        ]

    def session_rows_for(self, session: str, now_ms: int) -> list[tuple[int, float]]:
        """Just one session's markets, within the current NY day."""
        from .sessions import session_of

        return [
            (ms, pnl) for ms, pnl in self.session_rows(now_ms)
            if session_of(ms) == session
        ]

    def session_reported(self, ny_day: str, session: str) -> bool:
        """Has this session's close already been reported today?

        Keyed on the day AND the session so a restart cannot repeat a report,
        and so a gap that straddles two closes still reports both once each.
        """
        row = self.db.execute(
            "SELECT 1 FROM settings_text WHERE key = ?",
            (f"session_reported:{ny_day}:{session}",),
        ).fetchone()
        return bool(row)

    def mark_session_reported(self, ny_day: str, session: str, now_ms: int) -> None:
        self.set_setting_text(
            f"session_reported:{ny_day}:{session}", str(now_ms), now_ms
        )

    def recovery_transition(self, now_ms: int, plan_steps: int | None = None):
        """(event, state) when recovery arms or clears, else (None, state).

        Recovery ran for nearly an hour on 2026-09-22 - armed by a -$0.86
        loss, two adds blocked, one order placed, then cleared - and NONE of
        it reached Telegram. The operator's question was "I did not see the
        recovery happening", and they were right: the word never appeared in
        any message. A subsystem that moves real money silently is one nobody
        can supervise.

        The previous state is persisted, so a restart mid-recovery does not
        re-announce something already reported.
        """
        state = self.recovery_state(plan_steps or DEFAULT_RECOVERY_STEPS, now_ms)
        row = self.db.execute(
            "SELECT text_value FROM settings_text WHERE key='recovery_announced'"
        ).fetchone()
        was_active = bool(row and row[0] == "active")
        if state.active == was_active:
            return None, state
        self.set_setting_text(
            "recovery_announced", "active" if state.active else "clear", now_ms
        )
        return ("armed" if state.active else "cleared"), state

    def last_realised_loss(self) -> tuple[str, float] | None:
        """The most recent losing market, to name what armed recovery."""
        row = self.db.execute(
            "SELECT ticker, pnl FROM daily_ledger WHERE pnl < 0 "
            "ORDER BY COALESCE(window_ms, first_ms) DESC LIMIT 1"
        ).fetchone()
        return (str(row[0]), float(row[1])) if row else None

    def last_add_decision(self, window_open_ms: int | None = None) -> dict | None:
        """The most recent add-on decision, for showing WHY it did or did not.

        Without this the recovery line can say a deficit is outstanding but
        not why nothing is being done about it - which is exactly the gap that
        made two razor-thin refusals (momentum -0.6 bps, distance 9.9x against
        a 10x floor) invisible until someone went looking in the database.
        """
        if window_open_ms is not None:
            rows = self._dicts(
                "SELECT * FROM recovery_adds WHERE window_open_ms = ?",
                (window_open_ms,),
            )
        else:
            rows = self._dicts(
                "SELECT * FROM recovery_adds ORDER BY created_ms DESC LIMIT 1"
            )
        return rows[0] if rows else None

    def record_intelligence(self, row: dict) -> None:
        """Log one decision. Never raises - it sits on the order path."""
        try:
            columns = ", ".join(row)
            placeholders = ", ".join(f":{name}" for name in row)
            self.db.execute(
                f"INSERT INTO intelligence_decisions ({columns}) "
                f"VALUES ({placeholders})",
                row,
            )
            self.db.commit()
        except sqlite3.Error as exc:
            print(f"intelligence record failed: {exc!r}", flush=True)

    def grade_intelligence(self, window_open: int, won: bool, pnl: float,
                           now_ms: int) -> None:
        """Attach the outcome to every decision taken on this market.

        A veto and a rejected signal are graded too, as counterfactuals - they
        are the evidence for whether the adjustment helped, and dropping them
        would leave only the cases that happened to trade.
        """
        self.db.execute(
            "UPDATE intelligence_decisions SET won=?, realised_pnl=?, graded_ms=? "
            "WHERE window_open=? AND graded_ms IS NULL",
            (int(won), pnl, now_ms, window_open),
        )
        self.db.commit()

    def intelligence_summary(self) -> dict:
        """Did it help? Counts by action, with outcomes where known."""
        rows = self._dicts(
            "SELECT final_action, COUNT(*) AS n, "
            "COALESCE(SUM(won), 0) AS wins, "
            "COALESCE(SUM(realised_pnl), 0) AS pnl, "
            "COALESCE(SUM(graded_ms IS NOT NULL), 0) AS graded "
            "FROM intelligence_decisions GROUP BY final_action"
        )
        return {r["final_action"]: r for r in rows}

    def lifetime_record(self) -> LifetimeRecord:
        """Realised performance across every reconciled market. One per market.

        `daily_ledger` is keyed by ticker, so a market counts ONCE however it
        was traded: base and recovery contracts on the same market are one
        position, and a partial fill or a partial exit does not make a second
        trade. The figure is the broker's own settled P&L, net of fees -
        paper results live in `scoreboard` and are labelled separately, and
        deposits, withdrawals and unrealised marks never enter here.
        """
        row = self.db.execute(
            "SELECT COUNT(*), COALESCE(SUM(pnl > 0), 0), COALESCE(SUM(pnl), 0), "
            "MIN(COALESCE(window_ms, first_ms)) FROM daily_ledger"
        ).fetchone()
        markets = int(row[0] or 0)
        since = int(row[3]) if row[3] else None
        return LifetimeRecord(
            markets=markets, winners=int(row[1] or 0),
            dollars=round(float(row[2] or 0.0), 6), since_ms=since,
            complete=self.history_is_complete(),
        )

    def history_is_complete(self) -> bool:
        """Does our record reach the account's first trade?

        Set by `scripts/verify_lifetime.py` after comparing against everything
        the broker will serve, including `/historical/fills`. Defaults to
        False: claiming a complete lifetime is a claim that has to be earned,
        and the honest fallback is to say which date the total starts from.
        """
        return bool(self.get_setting("history_complete", 0.0))

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
        snapshot = self.money_snapshot(now_ms)
        if snapshot.markets or snapshot.has_mirror:
            return snapshot.markets, snapshot.winners, snapshot.headline

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
        day_start = self.day_start_ms(now_ms)
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
        # THE TIMEZONE MIGRATION MUST NOT REFUND THE LOSS ALLOWANCE.
        #
        # Midnight New York is LATER in absolute time than midnight UTC, so
        # moving the accounting day shortens the window on the day of the
        # change, and every loss booked between the two midnights would fall
        # outside it. The floor would quietly hand back allowance on a day that
        # had already spent it - a stop-loss that resets when you change a
        # setting is not a stop-loss. The carry is computed once, at migration,
        # and only applies to the day the change happened.
        realised += self.day_boundary_carry(now_ms)

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
