"""The learning loop as part of the running service. No script, no operator.

WHY THIS IS NOT A SCRIPT. A research script that a person remembers to run is
not a learning loop; it is a habit, and habits lapse exactly when the market
changes enough to matter. Everything here is driven off the service's own poll,
persists its state in the service's own database, and recovers from a restart
without anyone knowing there was one.

WHAT IT DOES, once per poll, cheaply, and never on the order path:

    1. is a training run due?      new settled markets past a threshold, or the
                                   scheduled interval elapsed, or no valid
                                   policy is active at all
    2. train, off the loop         in a worker thread with its own read-only
                                   connection, so a thirty-second fit cannot
                                   delay a fill by thirty seconds
    3. evaluate                    the new fit against the running one, on the
                                   same chronological validation slice, scored
                                   by the same `decide` the order path calls
    4. activate, atomically        only if it clears the bar; the previous
                                   valid artefact is kept as a rollback and the
                                   swap is an `os.replace`, so a crash mid-write
                                   cannot leave a half-written policy live
    5. withdraw                    active execution arms whose forward record
                                   has met the predefined deterioration bar

TRAINING ALWAYS RUNS; ACTIVATION IS EARNED. Most runs will fit a policy that is
never activated, and that is the loop working, not failing. The two are recorded
separately so "the system is learning" and "an adjustment is live" can never be
read off one another.

A FAILED RUN CHANGES NOTHING. The active artefact is only ever replaced by a
successful, validated fit; an exception anywhere in here leaves the last valid
Kalshi policy exactly where it was and writes the error where `/learning` shows
it.
"""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import time
import traceback
from pathlib import Path

from . import feature_contract, learning, learning_data
from .intelligence_policy import NEUTRAL, VETO, Policy
from .learning_store import (
    FAILED,
    FAILURES,
    LAST_ERROR,
    LAST_ERROR_MS,
    LAST_SUCCESS,
    NEXT_DUE,
    OK,
    SKIPPED,
    WATERMARK,
    LearningStore,
)

ROLLBACK_SUFFIX = ".rollback.json"
ARCHIVE_DIR = "policies"


class TrainingOutcome:
    """What the worker thread produced. Plain data; the main thread acts."""

    def __init__(self) -> None:
        self.ok = False
        self.error = ""
        self.policy: Policy | None = None
        self.report: dict = {}
        self.provenance: dict = {}
        self.comparison: dict = {}
        self.should_activate = False
        self.activation_reason = ""
        self.confidence_arms = 0
        self.promoted_arms = 0
        self.candidates: dict = {}


class LearningRunner:
    """Owns the schedule, the artefact files and the activation decision."""

    def __init__(self, settings, store, telegram=None,
                 on_activate=None) -> None:
        self.settings = settings
        self.store = store
        self.telegram = telegram
        # Called after an activation so the service drops its cached policy.
        # A runner that writes a new artefact and leaves the process reading
        # the old one in memory has trained nothing that anyone can observe.
        self.on_activate = on_activate
        self.learning = LearningStore(store.db)
        self.policy_path = Path(settings.intelligence_policy_path)
        self.rollback_path = Path(
            str(self.policy_path).replace(".json", "") + ROLLBACK_SUFFIX
        )
        self.archive_dir = self.policy_path.parent / ARCHIVE_DIR
        self._busy = False
        self._last_check_ms = 0
        self._task = None
        self.last_status = "not started"

    # ------------------------------------------------------------ startup
    def startup(self, now_ms: int) -> None:
        """Recover from whatever the last process was doing when it stopped."""
        interrupted = self.learning.close_interrupted(now_ms)
        if interrupted:
            print(
                f"learning: {interrupted} interrupted training run(s) from a "
                f"previous process closed out",
                flush=True,
            )
        active = Policy.load(self.policy_path)
        ok, why = learning.policy_is_valid(
            active, fingerprint=feature_contract.FINGERPRINT,
            feature_version=feature_contract.CONTRACT.version,
        )
        if ok:
            print(
                f"learning: active policy {active.version} is valid "
                f"({len(active.arms)} arms, vetoes={active.vetoes_enabled}, "
                f"admissions={active.admissions_enabled})",
                flush=True,
            )
        else:
            print(f"learning: active policy CANNOT ACT - {why}", flush=True)
            # RESTART RECOVERY. A valid rollback is restored immediately rather
            # than waiting for the next training run: the whole point of
            # keeping one is that the system is never left running on an
            # artefact that refuses every decision.
            if self._restore_rollback(now_ms, why):
                return
            print(
                "learning: no valid rollback available; a training run is due "
                "immediately",
                flush=True,
            )
            self.learning.set(NEXT_DUE, now_ms, now_ms)

    def _restore_rollback(self, now_ms: int, why: str) -> bool:
        rollback = Policy.load(self.rollback_path)
        ok, _ = learning.policy_is_valid(
            rollback, fingerprint=feature_contract.FINGERPRINT,
            feature_version=feature_contract.CONTRACT.version,
        )
        if not ok:
            return False
        previous = Policy.load(self.policy_path).version
        self._write_atomic(self.policy_path, rollback)
        self.learning.record_activation(
            now_ms=now_ms, policy=rollback, previous_version=previous,
            run_id=None,
            reason=f"restored on startup because the active artefact {why}",
            kind="rollback", rollback_path=str(self.rollback_path),
            confidence_arms=_confidence_arms(rollback),
            promoted_arms=_promoted_arms(rollback),
        )
        if self.on_activate:
            self.on_activate()
        print(
            f"learning: restored rollback policy {rollback.version}",
            flush=True,
        )
        return True

    # --------------------------------------------------------------- due
    def due(self, now_ms: int) -> tuple[bool, str]:
        """Should a run start now? Returns (due, trigger).

        Three independent triggers, checked in order of how much they mean:

          bootstrap   no valid policy is active. Nothing else matters; the
                      runtime is answering every decision with a refusal.
          settlements enough new settled markets to move a fit
          interval    the schedule elapsed, so a quiet market still refreshes
        """
        active = Policy.load(self.policy_path)
        ok, _ = learning.policy_is_valid(
            active, fingerprint=feature_contract.FINGERPRINT,
            feature_version=feature_contract.CONTRACT.version,
        )
        if not ok:
            return True, "bootstrap"
        settled = self.learning.settled_markets()
        watermark = int(self.learning.get(WATERMARK, 0) or 0)
        new = settled - watermark
        if new >= self.settings.learning_min_new_settlements:
            return True, "settlements"
        next_due = int(self.learning.get(NEXT_DUE, 0) or 0)
        if next_due and now_ms >= next_due:
            return True, "interval"
        if not next_due:
            # First run of a fresh install: schedule rather than fire, so a
            # restart loop cannot become a training loop.
            self.learning.set(
                NEXT_DUE, now_ms + self.settings.learning_interval_ms, now_ms
            )
        return False, ""

    def new_settlements(self) -> int:
        return max(
            0,
            self.learning.settled_markets()
            - int(self.learning.get(WATERMARK, 0) or 0),
        )

    # -------------------------------------------------------------- poll
    async def poll(self, now_ms: int) -> None:
        """Called once per service poll. Cheap, and never raises."""
        if not self.settings.learning_enabled or self._busy:
            return
        # The due check touches the database and reads a file; at a 10-second
        # poll that is 8,640 times a day for a decision that changes hourly.
        if now_ms - self._last_check_ms < self.settings.learning_check_ms:
            return
        self._last_check_ms = now_ms
        try:
            # WITHDRAWAL IS CHECKED ON THE POLL CADENCE, NOT ONLY AFTER A FIT.
            # It used to run only at the end of a training run, which meant an
            # active arm that started costing money could keep doing so until
            # the next scheduled refit - up to six hours. The check is a file
            # read and one aggregate query, so there is no reason to make the
            # protection wait on the thing it protects against.
            self._check_withdrawals(now_ms)
            is_due, trigger = self.due(now_ms)
            if not is_due:
                return
            # FIRE AND FORGET. `run` awaits a worker thread, and awaiting it
            # HERE would stall the poll loop for the length of the fit - about
            # eight seconds on the current corpus, against a ten-second poll.
            # Training must never be on the path of a decision, and "it runs in
            # a thread" is not enough on its own: what matters is that the poll
            # does not wait for the thread.
            #
            # `_busy` guards re-entry, and the reference is held so the task is
            # not garbage collected mid-fit - a detail that would otherwise show
            # up as a training run that silently vanishes under load.
            # `_busy` is set HERE as well as in `run`, so a second poll landing
            # before the task's first statement executes cannot start a second
            # fit against the same evidence.
            self._busy = True
            self._task = asyncio.create_task(self.run(trigger, now_ms))
        except Exception as exc:  # noqa: BLE001 - learning never stops trading
            print(f"learning poll failed: {exc!r}", flush=True)

    async def run(self, trigger: str, now_ms: int) -> None:
        """One complete cycle: train, evaluate, activate, withdraw, record."""
        self._busy = True
        self.last_status = "training"
        settled = self.learning.settled_markets()
        run_id = self.learning.start_run(
            trigger=trigger, now_ms=now_ms, settled_markets=settled,
        )
        print(
            f"learning: training run {run_id} started [{trigger}] over "
            f"{settled} settled markets",
            flush=True,
        )
        try:
            forward = self.learning.forward_scoreboard()
            # OFF THE LOOP. The fit reads ~38,000 BRTI points and bootstraps
            # every arm; on the poll thread that is a delayed fill.
            outcome = await asyncio.to_thread(
                self._train, forward, now_ms,
            )
            await self._finish(run_id, outcome, now_ms, settled)
        except Exception as exc:  # noqa: BLE001
            detail = f"{exc!r}"
            print(f"learning: run {run_id} FAILED {detail}", flush=True)
            traceback.print_exc()
            self._record_failure(run_id, detail, now_ms)
        finally:
            self._busy = False

    # ---------------------------------------------------------- training
    def _train(self, forward: dict, now_ms: int) -> TrainingOutcome:
        """Runs in a worker thread. Its own connection, read-only, no writes.

        SQLite handles belong to the thread that made them, and the service's
        handle is the poll loop's. This opens its own, read-only, so a training
        run can never take the write lock a settlement report is waiting on.
        """
        outcome = TrainingOutcome()
        db = None
        try:
            db = sqlite3.connect(
                f"file:{self.settings.database_path}?mode=ro", uri=True
            )
            rows, provenance = learning_data.combined_rows(
                db, fingerprint=feature_contract.FINGERPRINT,
            )
            outcome.provenance = provenance.payload()
            if not rows:
                outcome.error = "no Kalshi-native rows available"
                return outcome
            result = learning.train(
                rows,
                fingerprint=feature_contract.FINGERPRINT,
                feature_definitions=feature_contract.CONTRACT.payload(),
                feature_version=feature_contract.CONTRACT.version,
                provenance=outcome.provenance,
                slippage=self.settings.entry_slippage,
                forward=forward,
                now_ms=now_ms,
                min_evidence=self.settings.learning_min_evidence,
            )
            outcome.report = result.report.payload()
            if not result.ok:
                outcome.error = result.error
                return outcome
            new = result.policy
            current = Policy.load(self.policy_path)
            # THE VALIDATION SLICE, not the training slice and not the holdout.
            # Scoring a new fit on the data it was fitted to would prefer
            # whichever policy overfits hardest.
            _train_rows, validate_rows, _holdout = learning.chronological_split(
                rows
            )
            comparison = learning.compare(
                new, current, validate_rows,
                fingerprint=feature_contract.FINGERPRINT,
                feature_version=feature_contract.CONTRACT.version,
                slippage=self.settings.entry_slippage,
            )
            outcome.comparison = comparison.payload()
            should, reason = learning.activation_decision(
                new, current, comparison,
                fingerprint=feature_contract.FINGERPRINT,
                feature_version=feature_contract.CONTRACT.version,
                tolerance=self.settings.learning_regression_tolerance,
            )
            outcome.policy = new
            outcome.should_activate = should
            outcome.activation_reason = reason
            outcome.confidence_arms = _confidence_arms(new)
            outcome.promoted_arms = _promoted_arms(new)
            outcome.candidates = learning.candidate_payload(
                result, fingerprint=feature_contract.FINGERPRINT,
                feature_definitions=feature_contract.CONTRACT.payload(),
                feature_version=feature_contract.CONTRACT.version,
                now_ms=now_ms,
            )
            outcome.ok = True
            return outcome
        except Exception as exc:  # noqa: BLE001 - reported, never raised out
            outcome.error = f"{exc!r}\n{traceback.format_exc()}"
            return outcome
        finally:
            if db is not None:
                db.close()

    async def _finish(self, run_id: int, outcome: TrainingOutcome,
                      now_ms: int, settled: int) -> None:
        if not outcome.ok:
            self._record_failure(run_id, outcome.error or "training failed",
                                 now_ms, provenance=outcome.provenance,
                                 report=outcome.report)
            return
        policy = outcome.policy
        activated = False
        if outcome.should_activate:
            activated = self._activate(
                policy, run_id, outcome.activation_reason, now_ms,
                outcome.confidence_arms, outcome.promoted_arms,
            )
            if activated and outcome.candidates:
                # THE FORWARD SET IS REFRESHED WITH THE POLICY. A refit that
                # leaves the candidate artefact behind freezes the forward
                # evaluation on whatever the first run happened to find, so a
                # cell that becomes interesting later is never watched. The
                # ids are content-derived, so a cell keeps its name across
                # refits and an old prediction stays attributable to the
                # candidate that actually made it.
                self._write_candidates(outcome.candidates, now_ms)
        self.learning.finish_run(
            run_id, now_ms=now_ms, status=OK if activated else SKIPPED,
            provenance=outcome.provenance, report=outcome.report,
            comparison=outcome.comparison,
            candidate_version=policy.version if policy else "",
            activated=activated, activation_reason=outcome.activation_reason,
        )
        # THE WATERMARK MOVES ON A COMPLETED RUN, not on an activation. The run
        # consumed that evidence whether or not anything was promoted; leaving
        # the watermark behind would refire the same fit every poll forever.
        self.learning.set(WATERMARK, settled, now_ms)
        self.learning.set(LAST_SUCCESS, now_ms, now_ms)
        self.learning.set(FAILURES, 0, now_ms)
        self.learning.set(LAST_ERROR, "", now_ms)
        self.learning.set(
            NEXT_DUE, now_ms + self.settings.learning_interval_ms, now_ms
        )
        self.last_status = "idle"
        report = outcome.report or {}
        print(
            f"learning: run {run_id} {'ACTIVATED' if activated else 'completed'} "
            f"- {report.get('markets', 0)} markets, "
            f"{report.get('arms_fitted', 0)} arms, "
            f"{report.get('arms_with_confidence', 0)} with confidence, "
            f"{report.get('promoted', 0)} promoted. {outcome.activation_reason}",
            flush=True,
        )
        self._check_withdrawals(now_ms)
        if activated and self.telegram is not None:
            try:
                from . import messages

                await self.telegram.send(
                    messages.learning_activated(
                        policy=policy, report=report,
                        comparison=outcome.comparison,
                        reason=outcome.activation_reason,
                    )
                )
            except Exception as exc:  # noqa: BLE001 - reporting is never fatal
                print(f"learning activation report failed: {exc!r}", flush=True)

    def _record_failure(self, run_id: int, error: str, now_ms: int,
                        provenance: dict | None = None,
                        report: dict | None = None) -> None:
        """A failed run leaves the active policy untouched, and says so."""
        self.learning.finish_run(
            run_id, now_ms=now_ms, status=FAILED, error=error[:2000],
            provenance=provenance, report=report,
        )
        failures = int(self.learning.get(FAILURES, 0) or 0) + 1
        self.learning.set(FAILURES, failures, now_ms)
        self.learning.set(LAST_ERROR, error[:500], now_ms)
        self.learning.set(LAST_ERROR_MS, now_ms, now_ms)
        # BACK OFF, BUT DO NOT STOP. A run that fails every interval because a
        # database is locked would otherwise hammer it; one that fails because
        # the corpus is briefly unreadable should still recover by itself.
        backoff = min(
            self.settings.learning_interval_ms,
            self.settings.learning_retry_ms * (2 ** min(failures - 1, 4)),
        )
        self.learning.set(NEXT_DUE, now_ms + backoff, now_ms)
        self.last_status = "failed"
        print(
            f"learning: run {run_id} failed ({failures} consecutive); the last "
            f"valid Kalshi policy remains active; retry in {backoff // 60000}m",
            flush=True,
        )

    # -------------------------------------------------------- activation
    def _activate(self, policy: Policy, run_id: int, reason: str, now_ms: int,
                  confidence_arms: int, promoted_arms: int) -> bool:
        """Swap the artefact atomically, keeping a valid rollback behind it."""
        try:
            current = Policy.load(self.policy_path)
            ok, _ = learning.policy_is_valid(
                current, fingerprint=feature_contract.FINGERPRINT,
                feature_version=feature_contract.CONTRACT.version,
            )
            # A ROLLBACK MUST ITSELF BE VALID. Copying the retired artefact into
            # the rollback slot would mean the recovery path restores something
            # that cannot act - which is the failure it exists to undo.
            if ok:
                self._write_atomic(self.rollback_path, current)
            # The archive keeps every version that was ever live, so a decision
            # recorded under one can still be read back against it.
            self.archive_dir.mkdir(parents=True, exist_ok=True)
            self._write_atomic(
                self.archive_dir / f"{policy.version}.json", policy
            )
            self._write_atomic(self.policy_path, policy)
            self.learning.record_activation(
                now_ms=now_ms, policy=policy, previous_version=current.version,
                run_id=run_id, reason=reason, kind="training",
                rollback_path=str(self.rollback_path),
                confidence_arms=confidence_arms, promoted_arms=promoted_arms,
            )
            if self.on_activate:
                self.on_activate()
            return True
        except OSError as exc:
            print(f"learning: activation failed {exc!r}", flush=True)
            return False

    def _write_candidates(self, payload: dict, now_ms: int) -> None:
        """Freeze the new forward set beside the policy, atomically."""
        try:
            path = Path(self.settings.intelligence_candidates_path)
            self._write_json(path, payload)
            self.archive_dir.mkdir(parents=True, exist_ok=True)
            self._write_json(
                self.archive_dir / f"candidates-{payload['version']}.json",
                payload,
            )
            print(
                f"learning: candidates {payload['version']} frozen "
                f"({len(payload.get('candidates', []))} watched, forward "
                f"evaluation only - they control nothing)",
                flush=True,
            )
        except OSError as exc:
            print(f"learning: candidate write failed {exc!r}", flush=True)

    @classmethod
    def _write_atomic(cls, path: Path, policy: Policy) -> None:
        """Write beside, fsync, then rename over. Never a partial artefact.

        `os.replace` is atomic within a filesystem on Windows and POSIX alike,
        so a reader either sees the whole old file or the whole new one. Writing
        in place would let a crash leave a truncated JSON artefact live, and
        `Policy.load` answers a malformed file with an empty policy - which is
        an artefact that refuses every decision, deployed by a power cut.
        """
        cls._write_json(path, policy.__dict__)

    @staticmethod
    def _write_json(path: Path, payload: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix(path.suffix + f".tmp{os.getpid()}")
        blob = json.dumps(payload, indent=2, sort_keys=True)
        with open(temp, "w", encoding="utf-8") as handle:
            handle.write(blob)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)

    # ------------------------------------------------------- withdrawal
    def _check_withdrawals(self, now_ms: int) -> None:
        """Retire active execution arms that have met the deterioration bar."""
        try:
            policy = Policy.load(self.policy_path)
            if not policy.active:
                return
            forward = self.learning.forward_scoreboard()
            withdrawals = learning.deteriorated(
                policy, forward,
                min_changes=self.settings.learning_min_withdrawal_n,
            )
            if not withdrawals:
                return
            learning.withdraw(policy, withdrawals)
            self._write_atomic(self.policy_path, policy)
            self.learning.record_withdrawals(
                now_ms=now_ms, policy_version=policy.version,
                withdrawals=withdrawals,
            )
            if self.on_activate:
                self.on_activate()
            for item in withdrawals:
                print(
                    f"learning: WITHDREW {item.key} - {item.reason}", flush=True
                )
        except Exception as exc:  # noqa: BLE001
            print(f"learning withdrawal check failed: {exc!r}", flush=True)

    # ----------------------------------------------------------- reporting
    def snapshot(self, now_ms: int) -> dict:
        """Everything `/learning` needs, read fresh. Never cached.

        FOUR STATES, KEPT APART, because they are four different claims and the
        operator has been shown one and told another:

            running       the loop is alive and scheduled
            updating      a training run is in progress right now
            adjusting     at least one arm is re-rating displayed confidence
            authorised    an arm may actually change an order - which needs
                          BOTH evidence (a promoted arm) and the operator's two
                          switches. Evidence alone never gets here.
        """
        from . import intel_mode

        policy = Policy.load(self.policy_path)
        valid, why = learning.policy_is_valid(
            policy, fingerprint=feature_contract.FINGERPRINT,
            feature_version=feature_contract.CONTRACT.version,
        )
        mode, mode_why = intel_mode.resolve(
            self.settings.intelligence_mode,
            self.settings.intelligence_authorised,
        )
        state = self.learning.state()
        last_ok = self.learning.last_run(OK) or self.learning.last_run(SKIPPED)
        last_any = self.learning.last_run()
        confidence_arms = _confidence_arms(policy)
        promoted = _promoted_arms(policy)
        next_due = int(state.get(NEXT_DUE) or 0)
        new_settled = self.new_settlements()
        return {
            "enabled": bool(self.settings.learning_enabled),
            "running": bool(self.settings.learning_enabled),
            "updating": self._busy,
            # Confidence is label-only, so it rides on `intelligence_enabled`
            # rather than on the execution mode - the same gate the order path
            # applies, read from the same setting, so this line cannot claim an
            # adjustment the decision path is not actually making.
            "adjusting_confidence": bool(
                confidence_arms and valid and self.settings.intelligence_enabled
            ),
            "confidence_arms": confidence_arms if valid else 0,
            "authorised_to_execute": bool(
                promoted and valid and intel_mode.may_decide(mode)
                and (policy.vetoes_enabled or policy.admissions_enabled)
            ),
            "promoted_arms": promoted,
            "mode": mode,
            "mode_reason": mode_why,
            "policy_version": policy.version,
            "policy_valid": valid,
            "policy_invalid_reason": "" if valid else why,
            "feature_version": policy.feature_version,
            "feature_fingerprint": policy.feature_fingerprint,
            "arms": len(policy.arms),
            "training_cutoff_ms": policy.training_cutoff_ms,
            "data_end_ms": policy.data_end_ms,
            "vetoes_enabled": policy.vetoes_enabled,
            "admissions_enabled": policy.admissions_enabled,
            "last_success_ms": int(state.get(LAST_SUCCESS) or 0),
            "last_run": last_any,
            "last_complete_run": last_ok,
            "new_settled_markets": new_settled,
            "settled_markets": self.learning.settled_markets(),
            "trigger_threshold": self.settings.learning_min_new_settlements,
            "next_due_ms": next_due,
            "interval_ms": self.settings.learning_interval_ms,
            "last_error": state.get(LAST_ERROR) or "",
            "last_error_ms": int(state.get(LAST_ERROR_MS) or 0),
            "consecutive_failures": int(state.get(FAILURES) or 0),
            "activation": self.learning.active_activation(),
            "withdrawals": self.learning.withdrawals(3),
            "run_counts": self.learning.run_counts(),
            # AN INVALID POLICY HAS NO ACTIVE ADJUSTMENTS. Its arms still carry
            # deltas and actions in the file, and listing those as "active"
            # would report seven live confidence adjustments for an artefact
            # whose every answer is a refusal - which is the precise illusion
            # this whole exercise exists to remove.
            "active_adjustments": active_adjustments(policy) if valid else [],
            "now_ms": now_ms,
        }


def active_adjustments(policy: Policy) -> list[dict]:
    """The arms that are actually doing something, confidence or execution."""
    out = []
    for key, arm in policy.arms.items():
        delta = int(arm.get("delta") or 0)
        action = arm.get("action") or NEUTRAL
        if not delta and action == NEUTRAL:
            continue
        out.append({
            "context_key": key,
            "delta": delta,
            "action": action,
            "promoted": bool(arm.get("promoted")),
            "n": int(arm.get("n") or 0),
            "markets": int(arm.get("markets") or 0),
            "mean": float(arm.get("mean") or 0.0),
            "low": float(arm.get("low") or 0.0),
            "high": float(arm.get("high") or 0.0),
            "reason": arm.get("delta_reason") if delta else arm.get("action_reason"),
            "kind": "execution" if action != NEUTRAL else "confidence",
        })
    out.sort(key=lambda a: (a["kind"] != "execution", -abs(a["delta"])))
    return out


def _confidence_arms(policy: Policy) -> int:
    return sum(1 for a in policy.arms.values() if int(a.get("delta") or 0))


def _promoted_arms(policy: Policy) -> int:
    return sum(
        1 for a in policy.arms.values()
        if a.get("promoted") and a.get("action") in (VETO, "admit")
    )


def now_ms() -> int:
    return int(time.time() * 1000)
