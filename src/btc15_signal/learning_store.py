"""Persistence for the learning loop. Three records, deliberately not one.

The operator asked for these to be separable, and they are separable because
they answer different questions and can disagree:

    learning_runs         WHEN TRAINING RAN and WHAT DATA IT USED. One row per
                          attempt, including the ones that failed and the ones
                          that produced nothing worth activating. A loop that
                          only records its successes cannot be distinguished
                          from one that is not running.
    policy_activations    WHEN A POLICY BECAME ACTIVE. Training and activation
                          are not the same event: most runs fit a policy that
                          is never activated, and an activation can be an
                          automatic rollback that no run produced.
    policy_withdrawals    WHEN AN ACTIVE ADJUSTMENT WAS TAKEN BACK, with the
                          forward evidence that condemned it, kept beside it.

`learning_state` carries the scheduler across restarts. It holds the watermark,
the due time and the last error - never a derived figure that could be
recomputed, because a cached number that drifts from its source is how this
system has previously reported healthy while doing nothing.

NOTHING HERE IS EVER DELETED. A withdrawn adjustment keeps its activation row;
a superseded policy keeps its file. Evidence is preserved even when it is
unflattering - especially then, because the run that looked good and was later
withdrawn is the only record that stops the next training run rediscovering it
as though for the first time.
"""

from __future__ import annotations

import json
import sqlite3

RUNNING = "running"
OK = "ok"
FAILED = "failed"
SKIPPED = "skipped"
INTERRUPTED = "interrupted"

# State keys. Named constants so a typo is an ImportError rather than a silent
# default - a scheduler that reads `last_sucess_ms` and gets None simply runs
# forever, which looks like enthusiasm rather than a bug.
WATERMARK = "settled_markets_at_last_training"
LAST_SUCCESS = "last_success_ms"
LAST_ATTEMPT = "last_attempt_ms"
NEXT_DUE = "next_due_ms"
LAST_ERROR = "last_error"
LAST_ERROR_MS = "last_error_ms"
FAILURES = "consecutive_failures"


class LearningStore:
    """Learning tables on the service's own connection.

    It takes the live `Store`'s connection rather than opening its own: one
    connection means one write lock and one transaction domain, and two handles
    onto the same SQLite file under a poll loop is how a `database is locked`
    appears an hour into an unattended night.
    """

    def __init__(self, db: sqlite3.Connection) -> None:
        self.db = db
        self._create()

    def _create(self) -> None:
        self.db.execute("""
            CREATE TABLE IF NOT EXISTS learning_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                trigger TEXT NOT NULL,
                started_ms INTEGER NOT NULL,
                finished_ms INTEGER,
                status TEXT NOT NULL,
                error TEXT,
                rows_used INTEGER,
                markets_used INTEGER,
                corpus_markets INTEGER,
                live_markets INTEGER,
                live_actual_fills INTEGER,
                excluded_unresolved INTEGER,
                excluded_duplicate INTEGER,
                excluded_incompatible INTEGER,
                data_start_ms INTEGER,
                data_end_ms INTEGER,
                training_cutoff_ms INTEGER,
                feature_version TEXT,
                feature_fingerprint TEXT,
                candidate_policy_version TEXT,
                arms_fitted INTEGER,
                arms_with_confidence INTEGER,
                candidates_examined INTEGER,
                promoted INTEGER,
                comparison TEXT,
                report TEXT,
                activated INTEGER NOT NULL DEFAULT 0,
                activation_reason TEXT,
                settled_markets INTEGER
            )
        """)
        self.db.execute(
            "CREATE INDEX IF NOT EXISTS learning_runs_time "
            "ON learning_runs(started_ms)"
        )
        self.db.execute("""
            CREATE TABLE IF NOT EXISTS policy_activations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                activated_ms INTEGER NOT NULL,
                policy_version TEXT NOT NULL,
                previous_version TEXT,
                run_id INTEGER,
                reason TEXT,
                kind TEXT NOT NULL DEFAULT 'training',
                vetoes_enabled INTEGER NOT NULL DEFAULT 0,
                admissions_enabled INTEGER NOT NULL DEFAULT 0,
                confidence_arms INTEGER NOT NULL DEFAULT 0,
                promoted_arms INTEGER NOT NULL DEFAULT 0,
                feature_version TEXT,
                feature_fingerprint TEXT,
                training_cutoff_ms INTEGER,
                data_end_ms INTEGER,
                rollback_path TEXT,
                superseded_ms INTEGER
            )
        """)
        self.db.execute("""
            CREATE TABLE IF NOT EXISTS policy_withdrawals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                withdrawn_ms INTEGER NOT NULL,
                policy_version TEXT NOT NULL,
                context_key TEXT NOT NULL,
                changes INTEGER,
                incremental REAL,
                reason TEXT
            )
        """)
        self.db.execute("""
            CREATE TABLE IF NOT EXISTS learning_state (
                key TEXT PRIMARY KEY,
                value TEXT,
                updated_ms INTEGER
            )
        """)
        self.db.commit()

    # ------------------------------------------------------------- state
    def get(self, key: str, default=None):
        row = self.db.execute(
            "SELECT value FROM learning_state WHERE key = ?", (key,)
        ).fetchone()
        if row is None or row[0] is None:
            return default
        try:
            return json.loads(row[0])
        except ValueError:
            return row[0]

    def set(self, key: str, value, now_ms: int) -> None:
        self.db.execute(
            "INSERT INTO learning_state (key, value, updated_ms) VALUES (?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, "
            "updated_ms=excluded.updated_ms",
            (key, json.dumps(value), now_ms),
        )
        self.db.commit()

    def state(self) -> dict:
        return {
            row[0]: _loads(row[1])
            for row in self.db.execute("SELECT key, value FROM learning_state")
        }

    # -------------------------------------------------------------- runs
    def start_run(self, *, trigger: str, now_ms: int,
                  settled_markets: int) -> int:
        cursor = self.db.execute(
            "INSERT INTO learning_runs (trigger, started_ms, status, "
            "settled_markets) VALUES (?,?,?,?)",
            (trigger, now_ms, RUNNING, settled_markets),
        )
        self.db.commit()
        self.set(LAST_ATTEMPT, now_ms, now_ms)
        return int(cursor.lastrowid)

    def finish_run(self, run_id: int, *, now_ms: int, status: str,
                   error: str = "", provenance: dict | None = None,
                   report: dict | None = None, comparison: dict | None = None,
                   candidate_version: str = "", activated: bool = False,
                   activation_reason: str = "") -> None:
        prov = provenance or {}
        rep = report or {}
        self.db.execute(
            "UPDATE learning_runs SET finished_ms=?, status=?, error=?, "
            "rows_used=?, markets_used=?, corpus_markets=?, live_markets=?, "
            "live_actual_fills=?, excluded_unresolved=?, excluded_duplicate=?, "
            "excluded_incompatible=?, data_start_ms=?, data_end_ms=?, "
            "training_cutoff_ms=?, feature_version=?, feature_fingerprint=?, "
            "candidate_policy_version=?, arms_fitted=?, arms_with_confidence=?, "
            "candidates_examined=?, promoted=?, comparison=?, report=?, "
            "activated=?, activation_reason=? WHERE id=?",
            (
                now_ms, status, error or None,
                prov.get("decisions"), prov.get("markets"),
                prov.get("corpus_markets"), prov.get("live_markets"),
                prov.get("live_actual_fills"), prov.get("excluded_unresolved"),
                prov.get("excluded_duplicate"), prov.get("excluded_incompatible"),
                prov.get("data_start_ms"), prov.get("data_end_ms"),
                rep.get("training_cutoff_ms"),
                prov.get("feature_version"), prov.get("feature_fingerprint"),
                candidate_version or None,
                rep.get("arms_fitted"), rep.get("arms_with_confidence"),
                rep.get("candidates_examined"), rep.get("promoted"),
                json.dumps(comparison) if comparison else None,
                json.dumps(rep) if rep else None,
                int(bool(activated)), activation_reason or None, run_id,
            ),
        )
        self.db.commit()

    def close_interrupted(self, now_ms: int) -> int:
        """Mark runs a previous process left open. Returns how many.

        A row still saying `running` after a restart did not finish - the
        process was killed mid-fit, or the machine went down. Leaving it as
        `running` would make `/learning` report a training in progress forever
        and stop the scheduler ever starting another.
        """
        cursor = self.db.execute(
            "UPDATE learning_runs SET status=?, finished_ms=?, "
            "error='process restarted while training' "
            "WHERE status=?", (INTERRUPTED, now_ms, RUNNING),
        )
        self.db.commit()
        return cursor.rowcount or 0

    def last_run(self, status: str | None = None) -> dict | None:
        sql = "SELECT * FROM learning_runs"
        args: tuple = ()
        if status:
            sql += " WHERE status = ?"
            args = (status,)
        sql += " ORDER BY started_ms DESC, id DESC LIMIT 1"
        return _one(self.db, sql, args)

    def recent_runs(self, limit: int = 10) -> list[dict]:
        return _many(
            self.db,
            "SELECT * FROM learning_runs ORDER BY started_ms DESC, id DESC "
            "LIMIT ?", (limit,),
        )

    def run_counts(self) -> dict:
        return {
            row[0]: row[1] for row in self.db.execute(
                "SELECT status, COUNT(*) FROM learning_runs GROUP BY status"
            )
        }

    # ------------------------------------------------------- activations
    def record_activation(self, *, now_ms: int, policy, previous_version: str,
                          run_id: int | None, reason: str, kind: str,
                          rollback_path: str, confidence_arms: int,
                          promoted_arms: int) -> int:
        self.db.execute(
            "UPDATE policy_activations SET superseded_ms=? "
            "WHERE superseded_ms IS NULL", (now_ms,),
        )
        cursor = self.db.execute(
            "INSERT INTO policy_activations (activated_ms, policy_version, "
            "previous_version, run_id, reason, kind, vetoes_enabled, "
            "admissions_enabled, confidence_arms, promoted_arms, "
            "feature_version, feature_fingerprint, training_cutoff_ms, "
            "data_end_ms, rollback_path) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                now_ms, policy.version, previous_version or None, run_id,
                reason, kind, int(bool(policy.vetoes_enabled)),
                int(bool(policy.admissions_enabled)), confidence_arms,
                promoted_arms, policy.feature_version,
                policy.feature_fingerprint, policy.training_cutoff_ms,
                policy.data_end_ms, rollback_path,
            ),
        )
        self.db.commit()
        return int(cursor.lastrowid)

    def active_activation(self) -> dict | None:
        return _one(
            self.db,
            "SELECT * FROM policy_activations WHERE superseded_ms IS NULL "
            "ORDER BY activated_ms DESC, id DESC LIMIT 1",
        )

    def activations(self, limit: int = 10) -> list[dict]:
        return _many(
            self.db,
            "SELECT * FROM policy_activations ORDER BY activated_ms DESC, "
            "id DESC LIMIT ?", (limit,),
        )

    # ------------------------------------------------------- withdrawals
    def record_withdrawals(self, *, now_ms: int, policy_version: str,
                           withdrawals) -> None:
        for item in withdrawals:
            self.db.execute(
                "INSERT INTO policy_withdrawals (withdrawn_ms, policy_version, "
                "context_key, changes, incremental, reason) VALUES (?,?,?,?,?,?)",
                (now_ms, policy_version, item.key, item.changes,
                 item.incremental, item.reason),
            )
        self.db.commit()

    def withdrawals(self, limit: int = 10) -> list[dict]:
        return _many(
            self.db,
            "SELECT * FROM policy_withdrawals ORDER BY withdrawn_ms DESC, "
            "id DESC LIMIT ?", (limit,),
        )

    # ------------------------------------------------------- the corpus
    def settled_markets(self) -> int:
        """Distinct settled markets currently available to learning.

        Counted from the table training actually reads, not from `settlements`.
        The two differ - a market can settle while its decision rows carry no
        usable context - and the watermark has to track what would change a
        fit, or the scheduler fires on data the fit cannot see and then reports
        "no new evidence" forever.
        """
        row = self.db.execute(
            "SELECT COUNT(DISTINCT window_open) FROM intelligence_decisions "
            "WHERE graded_ms IS NOT NULL AND won IS NOT NULL "
            "AND context_key LIKE '%bd%'"
        ).fetchone()
        return int(row[0] or 0) if row else 0

    def forward_scoreboard(self) -> dict[str, dict]:
        """Per context key: how many orders an arm CHANGED, and what it cost.

        Read from `candidate_evaluations`, which is the forward record: one row
        per market per candidate, graded at settlement. Only rows where the
        candidate actually disagreed with the rule count as changes - a
        candidate that "changed" a decision that already went its way changed
        nothing, and counting those would flatter every arm.
        """
        rows = _many(
            self.db,
            "SELECT context_key, "
            "COALESCE(SUM(would_change),0) AS changes, "
            "COALESCE(SUM(CASE WHEN would_change THEN candidate_pnl - "
            "baseline_pnl END),0) AS incremental, "
            "COALESCE(SUM(CASE WHEN would_change AND graded_ms IS NOT NULL "
            "THEN 1 ELSE 0 END),0) AS graded_changes, "
            "COUNT(DISTINCT window_open) AS markets, "
            "proposed_action "
            "FROM candidate_evaluations GROUP BY context_key, proposed_action",
        )
        out: dict[str, dict] = {}
        for row in rows:
            leg = "accept" if row["proposed_action"] == "veto" else "reject"
            key = f"{row['context_key']}|{leg}"
            out[key] = {
                "changes": int(row["graded_changes"] or 0),
                "incremental": float(row["incremental"] or 0.0),
                "markets": int(row["markets"] or 0),
            }
        return out


def _loads(value):
    try:
        return json.loads(value)
    except (ValueError, TypeError):
        return value


def _one(db: sqlite3.Connection, sql: str, args: tuple = ()) -> dict | None:
    rows = _many(db, sql, args)
    return rows[0] if rows else None


def _many(db: sqlite3.Connection, sql: str, args: tuple = ()) -> list[dict]:
    cursor = db.cursor()
    cursor.row_factory = sqlite3.Row
    return [dict(row) for row in cursor.execute(sql, args)]
