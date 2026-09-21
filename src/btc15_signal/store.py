import math
import sqlite3
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
        self.db.execute("""
            CREATE TABLE IF NOT EXISTS strategy_alerts (
                strategy TEXT NOT NULL, window_open INTEGER NOT NULL,
                created_at INTEGER NOT NULL,
                PRIMARY KEY (strategy, window_open)
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
        ):
            if column not in observation_columns:
                self.db.execute(f"ALTER TABLE observations ADD COLUMN {column} {kind}")

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
            f"fee_paid, exit_price, exit_count FROM trade_proposals "
            f"WHERE window_open=? AND strategy='primary' AND status IN {ACCOUNTED_SQL}",
            (window_open,),
        ).fetchone()
        if not row:
            return None
        status, count, raw_fill, paid, fee, exit_price, exit_count = row
        return {
            "status": status,
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

    def settle_observations(self, window_open: int, won: bool, final_price: float) -> int:
        """Stamp every observation of a window with what actually happened.

        Done in one statement per window rather than per row, so the research
        archive settles at the same moment the trade does and can never drift
        out of step with `predictions`.
        """
        realised = None
        trade = self.trade_for_window(window_open)
        if trade:
            realised = position_pnl(
                paid=trade["paid"], count=trade["count"], entry_fee=trade["fee"],
                exit_price=trade["exit_price"], exit_count=trade["exit_count"], won=won,
            )
        cursor = self.db.execute(
            "UPDATE observations SET won=?, final_price=?, realised_pnl=? "
            "WHERE window_open=? AND won IS NULL",
            (1 if won else 0, final_price, realised, window_open),
        )
        self.db.commit()
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

    def realised_record(self) -> tuple[int, int, float]:
        """(trades, wins, dollars) over orders that were ACTUALLY PLACED.

        `scoreboard` answers "was the signal right?" across every signal,
        including the great majority that were never traded, by pricing each one
        at a hypothetical size. That is a useful measure of the strategy and a
        useless measure of the account: presenting it as money claims a P&L on
        trades nobody made, at a size nobody chose.

        This is the other question - "what did the account actually do?" - and
        it uses only real rows: the count that was filled, the price that was
        paid, and the fee that was charged. A position sold early is scored at
        its exit price, because that is what it realised; scoring it by who
        eventually won would credit back a loss already taken.
        """
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

        # One accounting path for both legs, shared with realised_record and the
        # dashboard. They each had their own copy and disagreed about the same
        # trade; a partially-sold position fell through every one of them.
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
