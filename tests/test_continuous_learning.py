"""The learning loop, tested where it can actually go wrong.

These are not unit tests of arithmetic. Every one of them pins a failure this
system has either had or is one edit away from having:

  * a policy fitted under one set of feature definitions applied under another,
    which has happened twice here and both times looked healthy;
  * a market the model flipped inside graded as a win on both sides, which made
    a forward evaluation report a 100% hit rate on its own arithmetic;
  * a settlement arriving twice, or late, or a restart mid-fit, each of which
    can double-count evidence or wedge the scheduler;
  * a confidence adjustment that reaches the decision instead of the label;
  * an execution adjustment that reaches the decision without the operator;
  * a failed training run leaving no policy, or the retired one, live.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pytest  # noqa: E402

from btc15_signal import (  # noqa: E402
    feature_contract,
    intel_mode,
    learning,
    learning_data,
)
from btc15_signal import intelligence_policy as intel  # noqa: E402
from btc15_signal.learning_store import (  # noqa: E402
    FAILED,
    OK,
    RUNNING,
    WATERMARK,
    LearningStore,
)
from btc15_signal.store import Store  # noqa: E402

NOW = 1_790_000_000_000
CTX = "asia · mid · bd10-15 · px70-85"
DAY = 86_400_000


def rows_for(context: str, n: int, *, qualified: bool, win_rate: float,
             ask: float = 0.80, start_ms: int = NOW, side: str = "UP",
             spread_days: int = 10, model_points: int = 85) -> list[dict]:
    """`n` settled markets in one cell, spread over several days.

    Spread over days on purpose: the interval is bootstrapped by day, so a
    fixture confined to one afternoon produces a degenerate interval and every
    evidence bar refuses it - which would make these tests pass for the wrong
    reason.
    """
    out = []
    for i in range(n):
        out.append({
            "window_open": start_ms + (i % spread_days) * DAY + (i // spread_days) * 900_000,
            "ticker": f"T{i}",
            "our_ask": ask,
            "won": 1 if (i % 100) < int(win_rate * 100) else 0,
            "rule_match": 1 if qualified else 0,
            "failed_gates": None if qualified else "target distance",
            "context_key": context,
            "side": side,
            "model_points": model_points,
            "origin": "corpus",
            "fill_kind": "simulated",
            "fee_cost": None,
        })
    return out


def make_store(tmp_path) -> Store:
    return Store(str(tmp_path / "t.db"))


# ------------------------------------------------- feature parity / artefacts


def test_training_and_live_key_the_same_cell_from_the_same_function():
    """A corpus row and a live row in the same state must land in one cell.

    This is the parity that everything else rests on. The two legs reach the
    key by different routes - the corpus computes it from BRTI columns, the
    live leg reads back the key the order path already wrote - so if those ever
    disagree, training fits a pocket live never keys and the loop measures
    nothing while looking busy.
    """
    corpus_row = {
        "window_open": NOW, "our_ask": 0.80, "won": 1, "rule_match": 1,
        "session": "asia", "brti_volatility_bps": 0.9,
        "brti_normalized_distance": 12.0,
    }
    live_row = {
        "window_open": NOW, "our_ask": 0.80, "won": 1, "rule_match": 1,
        "context_key": CTX,
    }
    assert learning.context_key_of(corpus_row) == learning.context_key_of(live_row)
    assert learning.context_key_of(live_row) == f"{CTX}|accept"


def test_a_policy_from_different_definitions_is_refused_with_the_difference():
    """A fingerprint mismatch must name what differs, not just say no."""
    policy = intel.Policy(
        version="v9", feature_version="brti-1", arms={f"{CTX}|accept": {"n": 500}},
        feature_fingerprint="deadbeefdeadbeef",
        feature_definitions={**feature_contract.CONTRACT.payload(),
                             "momentum_window_s": 600},
    )
    ok, why = learning.policy_is_valid(
        policy, fingerprint=feature_contract.FINGERPRINT,
        feature_version="brti-1",
    )
    assert not ok
    verdict = intel.decide(
        context_key=f"{CTX}|accept", base_qualified=True, failed_gates=(),
        ask=0.8, policy=policy,
    )
    assert verdict.final_action == intel.NEUTRAL
    assert "feature contract mismatch" in verdict.reason
    assert "momentum_window_s" in verdict.reason


def test_a_binance_keyed_artefact_can_never_activate():
    """Provenance is read off the keys, not off the label it wears."""
    policy = intel.Policy(
        version="v1", feature_version="brti-1",
        arms={"us · high · dist3+ · px<70|reject": {"n": 500, "action": "veto"}},
        feature_fingerprint=feature_contract.FINGERPRINT,
        feature_definitions=feature_contract.CONTRACT.payload(),
    )
    ok, why = learning.policy_is_valid(
        policy, fingerprint=feature_contract.FINGERPRINT,
        feature_version="brti-1",
    )
    assert not ok
    assert "binance" in why


def test_live_rows_exclude_binance_keyed_and_unlabelled_history(tmp_path):
    """The live table holds pre-fix rows whose label says `brti-1` and whose
    key says otherwise. They are history, and they are counted as excluded."""
    store = make_store(tmp_path)
    for i, key in enumerate([
        f"{CTX}|accept",                          # good
        "asia · low · dist<1.5 · px<70|reject",   # Binance band
        "? · ? · dist<1.5 · px<70|reject",        # unlabelled
    ]):
        store.record_intelligence({
            "window_open": NOW + i * 900_000, "ticker": f"T{i}",
            "decided_ms": NOW + i * 900_000, "remaining_s": 600,
            "side": "UP", "ask": 0.8, "base_qualified": 1,
            "final_action": "neutral", "confidence_delta": 0,
            "context_key": key, "feature_version": "brti-1",
        })
    store.db.execute(
        "UPDATE intelligence_decisions SET won=1, graded_ms=?", (NOW,)
    )
    store.db.commit()
    rows, prov = learning_data.live_rows(
        store.db, fingerprint=feature_contract.FINGERPRINT
    )
    assert len(rows) == 1
    assert rows[0]["context_key"] == CTX
    assert prov.excluded_incompatible == 2


# ------------------------------------------------------ opposing signals


def test_opposing_signals_in_one_market_are_graded_on_their_own_sides(tmp_path):
    """A window the model flipped inside has one winner and one loser.

    The bug this pins graded both as winners, because the flag available at the
    settlement loop - "did the winning side win?" - is true by construction.
    """
    store = make_store(tmp_path)
    for side in ("UP", "DOWN"):
        store.record_intelligence({
            "window_open": NOW, "ticker": "T1", "decided_ms": NOW,
            "remaining_s": 600, "side": side, "ask": 0.8,
            "base_qualified": 1, "final_action": "neutral",
            "confidence_delta": 0, "context_key": f"{CTX}|accept",
            "feature_version": "brti-1",
        })
    store.grade_intelligence(NOW, "UP", 0.0, NOW + 1000)
    graded = store._dicts(
        "SELECT side, won FROM intelligence_decisions ORDER BY side"
    )
    assert {(r["side"], r["won"]) for r in graded} == {("UP", 1), ("DOWN", 0)}

    rows, _prov = learning_data.live_rows(
        store.db, fingerprint=feature_contract.FINGERPRINT
    )
    # TWO rows: one market, two opportunities. Collapsing them would lose the
    # loser, and every cell containing a flipped window would read too well.
    assert len(rows) == 2
    assert {r["side"]: r["won"] for r in rows} == {"UP": 1, "DOWN": 0}
    assert len({r["window_open"] for r in rows}) == 1


def test_repeated_polls_are_one_market_not_many(tmp_path):
    """Forty polls of one window are one opportunity."""
    store = make_store(tmp_path)
    for i in range(40):
        store.record_intelligence({
            "window_open": NOW, "ticker": "T1", "decided_ms": NOW + i * 10_000,
            "remaining_s": 660 - i * 10, "side": "UP", "ask": 0.8,
            "base_qualified": 1 if i >= 5 else 0,
            "final_action": "neutral", "confidence_delta": 0,
            "context_key": f"{CTX}|{'accept' if i >= 5 else 'reject'}",
            "feature_version": "brti-1",
        })
    store.grade_intelligence(NOW, "UP", 0.0, NOW + 1_000_000)
    rows, prov = learning_data.live_rows(
        store.db, fingerprint=feature_contract.FINGERPRINT
    )
    assert len(rows) == 1
    assert prov.live_markets == 1
    assert prov.excluded_duplicate == 39
    # The FIRST QUALIFYING poll represents the market - the bot alerts there
    # and stops - not the first poll looked at.
    assert rows[0]["rule_match"] == 1


# --------------------------------------------- duplicates, delays, restarts


def test_a_settlement_recorded_twice_does_not_double_count(tmp_path):
    """Grading is idempotent: the second sweep must find nothing to grade."""
    store = make_store(tmp_path)
    store.record_intelligence({
        "window_open": NOW, "ticker": "T1", "decided_ms": NOW,
        "remaining_s": 600, "side": "UP", "ask": 0.8, "base_qualified": 1,
        "final_action": "neutral", "confidence_delta": 0,
        "context_key": f"{CTX}|accept", "feature_version": "brti-1",
    })
    store.grade_intelligence(NOW, "UP", 0.25, NOW + 1000)
    store.grade_intelligence(NOW, "DOWN", -0.80, NOW + 2000)
    row = store._dicts("SELECT won, realised_pnl, graded_ms FROM "
                       "intelligence_decisions")[0]
    assert row["won"] == 1 and row["realised_pnl"] == 0.25
    assert row["graded_ms"] == NOW + 1000

    learn = LearningStore(store.db)
    assert learn.settled_markets() == 1


def test_a_delayed_settlement_is_excluded_until_it_resolves(tmp_path):
    """An unresolved market is not trained on, and is counted as held back."""
    store = make_store(tmp_path)
    for i in range(2):
        store.record_intelligence({
            "window_open": NOW + i * 900_000, "ticker": f"T{i}",
            "decided_ms": NOW, "remaining_s": 600, "side": "UP", "ask": 0.8,
            "base_qualified": 1, "final_action": "neutral",
            "confidence_delta": 0, "context_key": f"{CTX}|accept",
            "feature_version": "brti-1",
        })
    store.grade_intelligence(NOW, "UP", 0.2, NOW + 1000)   # only the first
    rows, prov = learning_data.live_rows(
        store.db, fingerprint=feature_contract.FINGERPRINT
    )
    assert len(rows) == 1
    assert prov.excluded_unresolved == 1
    learn = LearningStore(store.db)
    assert learn.settled_markets() == 1


def test_a_restart_mid_training_does_not_wedge_the_scheduler(tmp_path):
    """A run left `running` by a killed process is closed out on startup.

    Without this the scheduler sees a training already in progress forever and
    never starts another - a learning loop that has silently stopped while
    reporting that it is busy.
    """
    store = make_store(tmp_path)
    learn = LearningStore(store.db)
    run_id = learn.start_run(trigger="interval", now_ms=NOW,
                             settled_markets=10)
    assert learn.last_run()["status"] == RUNNING
    closed = learn.close_interrupted(NOW + 60_000)
    assert closed == 1
    row = learn.last_run()
    assert row["id"] == run_id
    assert row["status"] == "interrupted"
    assert "restarted" in row["error"]


def test_state_and_watermark_survive_a_new_store_object(tmp_path):
    """Scheduling is persisted, not held in memory."""
    path = str(tmp_path / "t.db")
    first = LearningStore(Store(path).db)
    first.set(WATERMARK, 137, NOW)
    second = LearningStore(Store(path).db)
    assert second.get(WATERMARK) == 137


def test_training_and_activation_are_separate_records(tmp_path):
    """Most runs activate nothing; the tables must be able to say so."""
    store = make_store(tmp_path)
    learn = LearningStore(store.db)
    run_id = learn.start_run(trigger="settlements", now_ms=NOW,
                             settled_markets=50)
    learn.finish_run(run_id, now_ms=NOW + 1000, status=OK,
                     report={"promoted": 0, "arms_fitted": 40},
                     activated=False)
    assert learn.last_run()["activated"] == 0
    assert learn.active_activation() is None

    policy = intel.Policy(version="p1", feature_version="brti-1",
                          arms={f"{CTX}|accept": {"n": 200}})
    learn.record_activation(
        now_ms=NOW + 2000, policy=policy, previous_version="p0",
        run_id=run_id, reason="first valid policy", kind="training",
        rollback_path="r.json", confidence_arms=1, promoted_arms=0,
    )
    active = learn.active_activation()
    assert active["policy_version"] == "p1"
    assert active["promoted_arms"] == 0
    # A second activation supersedes the first rather than replacing its row -
    # the history is the audit trail.
    learn.record_activation(
        now_ms=NOW + 3000, policy=intel.Policy(version="p2"),
        previous_version="p1", run_id=None, reason="newer fit",
        kind="training", rollback_path="r.json", confidence_arms=0,
        promoted_arms=0,
    )
    assert learn.active_activation()["policy_version"] == "p2"
    assert len(learn.activations()) == 2


# ------------------------------------------- confidence reaches the label


def test_a_confidence_arm_reaches_the_displayed_confidence():
    """The delta must arrive in the message the operator actually reads."""
    from btc15_signal import messages

    policy = intel.Policy(
        version="p1", feature_version="brti-1",
        feature_fingerprint=feature_contract.FINGERPRINT,
        feature_definitions=feature_contract.CONTRACT.payload(),
        arms={f"{CTX}|accept": {"n": 300, "mean": -0.05, "low": -0.09,
                                "high": -0.01, "delta": -10,
                                "action": "neutral"}},
    )
    verdict = intel.decide(
        context_key=f"{CTX}|accept", base_qualified=True, failed_gates=(),
        ask=0.80, policy=policy,
    )
    assert verdict.confidence_delta == -10
    assert verdict.changed
    line = messages.policy_line(verdict)
    assert "Confidence lowered" in line
    assert "n=300" in line
    assert intel.confidence_words(verdict.confidence_delta) == "lowered"


def test_confidence_cannot_admit_a_blocked_setup_or_change_size():
    """The whole of a confidence adjustment's authority is the label."""
    policy = intel.Policy(
        version="p1", feature_version="brti-1",
        feature_fingerprint=feature_contract.FINGERPRINT,
        feature_definitions=feature_contract.CONTRACT.payload(),
        arms={f"{CTX}|reject": {"n": 900, "mean": 0.20, "low": 0.10,
                                "high": 0.30, "delta": 12,
                                "action": "neutral"}},
    )
    verdict = intel.decide(
        context_key=f"{CTX}|reject", base_qualified=False,
        failed_gates=("target distance",), ask=0.80, policy=policy,
    )
    assert verdict.confidence_delta == 12
    # A large positive re-rating on a REFUSED setup still leaves it refused.
    assert verdict.final_action == intel.NEUTRAL
    assert verdict.qualifies is False
    for mode in intel_mode.MODES:
        assert intel_mode.may_change_size(mode) is False


# --------------------------------------- execution needs evidence AND leave


def test_a_validated_veto_reaches_the_decision_only_when_authorised():
    """Evidence and authority are two switches, and both must be on."""
    policy = intel.Policy(
        version="p1", feature_version="brti-1",
        feature_fingerprint=feature_contract.FINGERPRINT,
        feature_definitions=feature_contract.CONTRACT.payload(),
        vetoes_enabled=True,
        arms={f"{CTX}|accept": {"n": 400, "mean": -0.08, "low": -0.12,
                                "high": -0.04, "delta": -12, "action": "veto",
                                "promoted": True}},
    )
    verdict = intel.decide(
        context_key=f"{CTX}|accept", base_qualified=True, failed_gates=(),
        ask=0.80, policy=policy,
    )
    assert verdict.final_action == intel.VETO
    assert verdict.qualifies is False

    # SHADOW: the evidence stands, the effect does not.
    shadow = intel.authorise(verdict, may_confidence=False, may_veto=False,
                             may_admit=False)
    assert shadow.final_action == intel.NEUTRAL
    assert shadow.qualifies is True            # the base decision survives
    assert shadow.evidence_action == intel.VETO
    assert shadow.confidence_delta == 0
    assert shadow.evidence_delta == -12
    assert "veto not authorised" in shadow.authority

    # LIVE: both switches on, and only then does it bite.
    live = intel.authorise(verdict, may_confidence=True, may_veto=True,
                           may_admit=True)
    assert live.final_action == intel.VETO
    assert live.qualifies is False
    assert live.evidence_action == intel.VETO
    assert live.authority == ""


def test_an_admission_needs_the_single_gate_it_names():
    """An exception overrides ONE gate. It cannot rescue a setup that failed
    for other reasons as well, because those may not be strategy opinions."""
    arm = {"n": 400, "mean": 0.10, "low": 0.04, "high": 0.16, "delta": 12,
           "action": "admit", "gate": "target distance", "promoted": True}
    policy = intel.Policy(
        version="p1", feature_version="brti-1",
        feature_fingerprint=feature_contract.FINGERPRINT,
        feature_definitions=feature_contract.CONTRACT.payload(),
        admissions_enabled=True, arms={f"{CTX}|reject": arm},
    )
    one = intel.decide(
        context_key=f"{CTX}|reject", base_qualified=False,
        failed_gates=("target distance",), ask=0.80, policy=policy,
    )
    assert one.final_action == intel.ADMIT
    two = intel.decide(
        context_key=f"{CTX}|reject", base_qualified=False,
        failed_gates=("target distance", "contract price band"), ask=0.80,
        policy=policy,
    )
    assert two.final_action == intel.NEUTRAL
    assert two.qualifies is False


def test_modes_grant_confidence_veto_and_admission_separately():
    assert intel_mode.may_influence_confidence("assist") is True
    assert intel_mode.may_veto("assist") is False
    assert intel_mode.may_admit("assist") is False
    assert intel_mode.may_veto("live") is True
    assert intel_mode.may_admit("live") is True
    assert intel_mode.may_influence_confidence("shadow") is False
    # An unauthorised mode falls back to shadow and says why.
    mode, why = intel_mode.resolve("live", False)
    assert mode == "shadow" and "authorised" in why


# ------------------------------------------ missing data / failed training


def test_training_on_nothing_fails_explicitly_and_makes_no_policy():
    result = learning.train(
        [], fingerprint=feature_contract.FINGERPRINT,
        feature_definitions=feature_contract.CONTRACT.payload(),
        feature_version="brti-1",
    )
    assert result.ok is False
    assert "nothing to fit" in result.error
    assert result.policy.active is False


def test_a_split_too_small_to_validate_refuses_rather_than_guessing():
    rows = rows_for(CTX, 4, qualified=True, win_rate=0.75)
    result = learning.train(
        rows, fingerprint=feature_contract.FINGERPRINT,
        feature_definitions=feature_contract.CONTRACT.payload(),
        feature_version="brti-1",
    )
    assert result.ok is False
    assert "validate" in result.error


def test_a_failed_run_keeps_the_last_valid_policy_and_records_the_error(tmp_path):
    """The active artefact is only ever replaced by a successful fit."""
    from btc15_signal.config import Settings
    from btc15_signal.learning_runner import LearningRunner

    store = make_store(tmp_path)
    policy_path = tmp_path / "intelligence_policy.json"
    good = intel.Policy(
        version="good-1", model_version="m", feature_version="brti-1",
        training_cutoff_ms=NOW - DAY, data_end_ms=NOW,
        arms={f"{CTX}|accept": {"n": 300, "mean": 0.02, "low": 0.01,
                                "high": 0.03, "delta": 4, "action": "neutral"}},
        feature_fingerprint=feature_contract.FINGERPRINT,
        feature_definitions=feature_contract.CONTRACT.payload(),
    )
    good.save(policy_path)
    settings = Settings(intelligence_policy_path=str(policy_path),
                        database_path=str(tmp_path / "t.db"))
    runner = LearningRunner(settings, store)
    run_id = runner.learning.start_run(trigger="interval", now_ms=NOW,
                                       settled_markets=0)
    runner._record_failure(run_id, "corpus unreadable", NOW + 1000)

    row = runner.learning.last_run()
    assert row["status"] == FAILED
    assert "corpus unreadable" in row["error"]
    assert runner.learning.get("last_error") == "corpus unreadable"
    # Untouched.
    assert intel.Policy.load(policy_path).version == "good-1"
    snapshot = runner.snapshot(NOW + 2000)
    assert snapshot["policy_valid"] is True
    assert snapshot["policy_version"] == "good-1"
    assert snapshot["last_error"] == "corpus unreadable"
    # ...and the next attempt is scheduled, not abandoned.
    assert snapshot["next_due_ms"] > NOW + 1000


def test_a_missing_policy_file_produces_neutral_with_a_reason():
    policy = intel.Policy.load("does-not-exist.json")
    verdict = intel.decide(
        context_key=f"{CTX}|accept", base_qualified=True, failed_gates=(),
        ask=0.8, policy=policy,
    )
    assert verdict.final_action == intel.NEUTRAL
    assert verdict.reason == "no active policy"
    assert verdict.qualifies is True     # the base strategy is untouched


def test_an_unsupported_cell_is_neutral_with_a_specific_evidence_reason():
    """"Unsupported" must say what was missing, not just decline."""
    rows = (
        rows_for(CTX, 200, qualified=True, win_rate=0.70)
        + rows_for("us · low · bd5-10 · px70-85", 80, qualified=True,
                   win_rate=0.55, start_ms=NOW + 400 * DAY)
    )
    result = learning.train(
        rows, fingerprint=feature_contract.FINGERPRINT,
        feature_definitions=feature_contract.CONTRACT.payload(),
        feature_version="brti-1",
    )
    assert result.ok
    for arm in result.arms.values():
        if not arm.delta:
            assert arm.delta_reason, f"{arm.key} declined with no reason"
        if arm.action == intel.NEUTRAL:
            assert arm.action_reason, f"{arm.key} neutral with no reason"


# ------------------------------------------------------ promotion bar


def test_the_promotion_bar_is_not_cleared_by_a_thin_cell():
    rows = rows_for(CTX, 80, qualified=True, win_rate=0.20)
    result = learning.train(
        rows + rows_for("us · low · bd5-10 · px70-85", 120, qualified=True,
                        win_rate=0.95, start_ms=NOW + 400 * DAY),
        fingerprint=feature_contract.FINGERPRINT,
        feature_definitions=feature_contract.CONTRACT.payload(),
        feature_version="brti-1",
    )
    assert result.ok
    assert result.report.promoted == 0
    assert result.policy.vetoes_enabled is False
    assert result.policy.admissions_enabled is False


def test_a_policy_that_promotes_nothing_is_still_a_valid_usable_policy():
    """"Nothing qualified" must still produce an artefact that can act.

    This is the whole point of the exercise: the runtime answer becomes an
    evidence-backed neutral naming the cell, not a refusal naming a retired
    instrument.
    """
    # A cell that EARNS its price: 90% winners at 80c, so the accept leg is
    # positive and no veto is proposed. Nothing to promote, and the artefact
    # still has to be a usable one.
    rows = rows_for(CTX, 400, qualified=True, win_rate=0.90)
    result = learning.train(
        rows, fingerprint=feature_contract.FINGERPRINT,
        feature_definitions=feature_contract.CONTRACT.payload(),
        feature_version="brti-1",
    )
    assert result.ok
    assert result.report.promoted == 0
    ok, why = learning.policy_is_valid(
        result.policy, fingerprint=feature_contract.FINGERPRINT,
        feature_version="brti-1",
    )
    assert ok, why
    verdict = intel.decide(
        context_key=f"{CTX}|accept", base_qualified=True, failed_gates=(),
        ask=0.80, policy=result.policy,
    )
    assert verdict.final_action == intel.NEUTRAL
    assert "retired" not in verdict.reason
    assert verdict.evidence_n >= 120
    assert verdict.reason == "pattern supports the existing decision"


# ----------------------------------------------------------- withdrawal


def test_an_active_arm_is_withdrawn_when_its_forward_record_condemns_it():
    policy = intel.Policy(
        version="p1", feature_version="brti-1",
        feature_fingerprint=feature_contract.FINGERPRINT,
        feature_definitions=feature_contract.CONTRACT.payload(),
        vetoes_enabled=True,
        arms={f"{CTX}|accept": {"n": 400, "mean": -0.06, "low": -0.10,
                                "high": -0.02, "delta": -12, "action": "veto",
                                "promoted": True}},
    )
    # Not enough forward changes yet: bad luck is not deterioration.
    assert learning.deteriorated(policy, {f"{CTX}|accept": {
        "changes": 5, "incremental": -1.0}}) == []
    # Negative, but no worse than the bottom of its own interval.
    assert learning.deteriorated(policy, {f"{CTX}|accept": {
        "changes": 40, "incremental": -0.40}}) == []
    # Below the floor its promotion was granted on.
    out = learning.deteriorated(policy, {f"{CTX}|accept": {
        "changes": 40, "incremental": -8.0}})
    assert len(out) == 1
    learning.withdraw(policy, out)
    arm = policy.arms[f"{CTX}|accept"]
    assert arm["action"] == intel.NEUTRAL
    assert arm["promoted"] is False
    assert "withdrawn" in arm["action_reason"]
    assert policy.vetoes_enabled is False
    # The EVIDENCE is preserved, only the authority is taken away.
    assert arm["n"] == 400 and arm["mean"] == -0.06


# ------------------------------------------------- activation decision


def test_an_invalid_running_artefact_is_replaced_without_a_horse_race():
    """A retired policy must not be defended by a P&L comparison it cannot
    lose, because it never trades. This is exactly how it stayed deployed."""
    retired = intel.Policy(
        version="v1-retired", feature_version="binance-1",
        arms={"us · high · dist3+ · px<70|reject": {"n": 200}},
    )
    fresh = intel.Policy(
        version="kalshi-1", feature_version="brti-1",
        arms={f"{CTX}|accept": {"n": 300}},
        feature_fingerprint=feature_contract.FINGERPRINT,
        feature_definitions=feature_contract.CONTRACT.payload(),
    )
    comparison = learning.Comparison(current_valid=False, new_pnl=0.0,
                                     current_pnl=0.0)
    should, why = learning.activation_decision(
        fresh, retired, comparison, fingerprint=feature_contract.FINGERPRINT,
        feature_version="brti-1",
    )
    assert should is True
    assert "cannot act" in why


def test_a_worse_fit_does_not_replace_a_valid_running_policy():
    good = intel.Policy(
        version="p1", feature_version="brti-1",
        arms={f"{CTX}|accept": {"n": 300}},
        feature_fingerprint=feature_contract.FINGERPRINT,
        feature_definitions=feature_contract.CONTRACT.payload(),
    )
    fresh = intel.Policy(
        version="p2", feature_version="brti-1",
        arms={f"{CTX}|accept": {"n": 310}},
        feature_fingerprint=feature_contract.FINGERPRINT,
        feature_definitions=feature_contract.CONTRACT.payload(),
    )
    comparison = learning.Comparison(current_valid=True, new_pnl=1.0,
                                     current_pnl=2.0)
    should, why = learning.activation_decision(
        fresh, good, comparison, fingerprint=feature_contract.FINGERPRINT,
        feature_version="brti-1",
    )
    assert should is False
    assert "regression" in why


def test_an_invalid_new_fit_never_activates():
    bad = intel.Policy(version="p2", feature_version="brti-1",
                       arms={f"{CTX}|accept": {"n": 10}})   # no fingerprint
    should, why = learning.activation_decision(
        bad, intel.Policy(), learning.Comparison(current_valid=False),
        fingerprint=feature_contract.FINGERPRINT, feature_version="brti-1",
    )
    assert should is False
    assert "rejected" in why


def test_activation_is_atomic_and_keeps_a_valid_rollback(tmp_path):
    from btc15_signal.config import Settings
    from btc15_signal.learning_runner import LearningRunner

    store = make_store(tmp_path)
    policy_path = tmp_path / "intelligence_policy.json"
    first = intel.Policy(
        version="k1", feature_version="brti-1", data_end_ms=NOW,
        arms={f"{CTX}|accept": {"n": 300, "delta": 0, "action": "neutral"}},
        feature_fingerprint=feature_contract.FINGERPRINT,
        feature_definitions=feature_contract.CONTRACT.payload(),
    )
    first.save(policy_path)
    settings = Settings(intelligence_policy_path=str(policy_path),
                        database_path=str(tmp_path / "t.db"))
    runner = LearningRunner(settings, store)
    second = intel.Policy(
        version="k2", feature_version="brti-1", data_end_ms=NOW + DAY,
        arms={f"{CTX}|accept": {"n": 400, "delta": -6, "action": "neutral"}},
        feature_fingerprint=feature_contract.FINGERPRINT,
        feature_definitions=feature_contract.CONTRACT.payload(),
    )
    assert runner._activate(second, 1, "fresher fit", NOW, 1, 0) is True
    assert intel.Policy.load(policy_path).version == "k2"
    assert intel.Policy.load(runner.rollback_path).version == "k1"
    # Every version that was ever live stays readable.
    assert (tmp_path / "policies" / "k2.json").exists()
    assert runner.learning.active_activation()["policy_version"] == "k2"
    # No temporary file left behind by the atomic swap.
    assert not list(tmp_path.glob("*.tmp*"))


def test_startup_restores_a_valid_rollback_over_a_retired_artefact(tmp_path):
    """Restart recovery: the system is never left answering every decision
    with a refusal when a usable policy is sitting beside it."""
    from btc15_signal.config import Settings
    from btc15_signal.learning_runner import LearningRunner

    store = make_store(tmp_path)
    policy_path = tmp_path / "intelligence_policy.json"
    retired = intel.Policy(
        version="v1-retired", feature_version="binance-1",
        arms={"us · high · dist3+ · px<70|reject": {"n": 200}},
    )
    retired.save(policy_path)
    good = intel.Policy(
        version="k1", feature_version="brti-1", data_end_ms=NOW,
        arms={f"{CTX}|accept": {"n": 300, "delta": 0, "action": "neutral"}},
        feature_fingerprint=feature_contract.FINGERPRINT,
        feature_definitions=feature_contract.CONTRACT.payload(),
    )
    settings = Settings(intelligence_policy_path=str(policy_path),
                        database_path=str(tmp_path / "t.db"))
    runner = LearningRunner(settings, store)
    good.save(runner.rollback_path)

    reloaded = []
    runner.on_activate = lambda: reloaded.append(1)
    runner.startup(NOW)
    assert intel.Policy.load(policy_path).version == "k1"
    assert reloaded == [1]
    activation = runner.learning.active_activation()
    assert activation["kind"] == "rollback"
    assert "retired" in activation["reason"]


def test_bootstrap_is_due_when_the_running_artefact_cannot_act(tmp_path):
    from btc15_signal.config import Settings
    from btc15_signal.learning_runner import LearningRunner

    store = make_store(tmp_path)
    policy_path = tmp_path / "intelligence_policy.json"
    intel.Policy(
        version="v1-retired", feature_version="binance-1",
        arms={"us · high · dist3+ · px<70|reject": {"n": 200}},
    ).save(policy_path)
    settings = Settings(intelligence_policy_path=str(policy_path),
                        database_path=str(tmp_path / "t.db"))
    runner = LearningRunner(settings, store)
    due, trigger = runner.due(NOW)
    assert due is True
    assert trigger == "bootstrap"


def test_the_watermark_moves_on_a_completed_run_not_on_an_activation(tmp_path):
    """Otherwise a run that promotes nothing refires every poll forever."""
    from btc15_signal.config import Settings
    from btc15_signal.learning_runner import LearningRunner

    store = make_store(tmp_path)
    policy_path = tmp_path / "p.json"
    intel.Policy(
        version="k1", feature_version="brti-1", data_end_ms=NOW,
        arms={f"{CTX}|accept": {"n": 300, "delta": 0, "action": "neutral"}},
        feature_fingerprint=feature_contract.FINGERPRINT,
        feature_definitions=feature_contract.CONTRACT.payload(),
    ).save(policy_path)
    settings = Settings(intelligence_policy_path=str(policy_path),
                        database_path=str(tmp_path / "t.db"),
                        learning_min_new_settlements=5)
    runner = LearningRunner(settings, store)
    runner.learning.set(WATERMARK, 0, NOW)
    # No settled markets yet and the interval not elapsed: not due.
    runner.learning.set("next_due_ms", NOW + DAY, NOW)
    assert runner.due(NOW)[0] is False


# ------------------------------------------------- the /learning surface


def test_learning_message_distinguishes_the_four_states():
    from btc15_signal import messages

    state = {
        "running": True, "updating": False, "adjusting_confidence": True,
        "authorised_to_execute": False, "confidence_arms": 1,
        "promoted_arms": 0, "mode": "assist", "policy_valid": True,
        "policy_version": "kalshi-brti-1-123", "feature_version": "brti-1",
        "feature_fingerprint": "90a70cfa994e7a08", "arms": 128,
        "vetoes_enabled": False, "admissions_enabled": False,
        "last_success_ms": NOW - 3_600_000, "new_settled_markets": 3,
        "trigger_threshold": 24, "next_due_ms": NOW + 3_600_000,
        "interval_ms": 6 * 3_600_000, "last_error": "",
        "consecutive_failures": 0, "now_ms": NOW,
        "last_complete_run": {
            "status": "ok", "trigger": "settlements", "finished_ms": NOW - 3_600_000,
            "markets_used": 6472, "rows_used": 6489, "corpus_markets": 6428,
            "live_markets": 44, "live_actual_fills": 12, "arms_fitted": 128,
            "arms_with_confidence": 1, "promoted": 0, "candidates_examined": 1,
        },
        "active_adjustments": [{
            "context_key": f"{CTX}|accept", "delta": -12, "action": "neutral",
            "promoted": False, "n": 187, "markets": 187, "mean": -0.08,
            "low": -0.14, "high": -0.01, "reason": "interval clear of zero",
            "kind": "confidence",
        }],
        "withdrawals": [],
    }
    text = messages.learning(head="H", state=state, candidates=None, board=[])
    assert "Running" in text
    assert "Updating" in text
    assert "Adjusting confidence" in text
    assert "Authorised to affect execution" in text
    # The two that are NOT true must read as not true.
    assert "✅ <b>Running</b>" in text
    assert "— <b>Authorised to affect execution</b>" in text
    assert "✅ <b>Adjusting confidence</b>" in text
    # ...and the schedule and the data behind it are visible.
    assert "3</b> of 24" in text
    assert "6472" in text
    assert "kalshi-brti-1-123" in text


def test_learning_message_says_plainly_when_the_policy_cannot_act():
    from btc15_signal import messages

    state = {
        "running": True, "updating": False, "adjusting_confidence": False,
        "authorised_to_execute": False, "confidence_arms": 0,
        "promoted_arms": 0, "mode": "shadow", "policy_valid": False,
        "policy_version": "v1-retired",
        "policy_invalid_reason": "keyed on retired features binance",
        "feature_version": "binance-1", "feature_fingerprint": "",
        "arms": 7, "vetoes_enabled": False, "admissions_enabled": False,
        "last_success_ms": 0, "new_settled_markets": 0, "trigger_threshold": 24,
        "next_due_ms": NOW, "interval_ms": 6 * 3_600_000,
        "last_error": "", "consecutive_failures": 0, "now_ms": NOW,
        "last_complete_run": None, "active_adjustments": [], "withdrawals": [],
    }
    text = messages.learning(head="H", state=state, candidates=None, board=[])
    assert "cannot act" in text
    assert "retired" in text
    assert "no completed training run yet" in text


# ------------------------------------------------ evaluation faithfulness


def test_comparison_counts_markets_and_decisions_separately():
    rows = rows_for(CTX, 30, qualified=True, win_rate=0.70)
    # One window polled on both sides: 31 decisions, 30 markets.
    twin = dict(rows[0])
    twin["side"] = "DOWN"
    twin["won"] = 0
    rows.append(twin)
    out = learning.compare(
        intel.Policy(), intel.Policy(), rows,
        fingerprint=feature_contract.FINGERPRINT, feature_version="brti-1",
    )
    assert out.rows == 31
    assert out.markets == len({r["window_open"] for r in rows})
    assert out.markets < out.rows


def test_an_execution_is_worth_what_kalshi_paid_not_a_reconstruction():
    """An executed trade carries the exchange's own pnl per contract.

    A limit is permission to cross, never the price paid - so the recorded ask
    cannot price a trade that happened. An unexecuted row is a counterfactual
    and IS priced at the ask, with the published fee.
    """
    executed = {"our_ask": 0.80, "won": 1, "rule_match": 1,
                "fill_kind": "actual", "realised_pnl": 0.2354,
                "window_open": NOW}
    assert learning.reward_for(executed) == pytest.approx(0.2354)
    counterfactual = {"our_ask": 0.80, "won": 1, "rule_match": 1,
                      "fill_kind": "simulated", "window_open": NOW}
    assert learning.reward_for(counterfactual) == pytest.approx(
        1.0 - 0.80 - learning.kalshi_fee_charged(0.80, 1)
    )


def test_an_early_exit_is_scored_at_what_it_realised():
    """A DOWN position sold at 100c on a market that settled UP is a profit."""
    row = {"our_ask": 0.80, "won": 0, "rule_match": 1, "fill_kind": "actual",
           "realised_pnl": 0.18, "fee_cost": 0.01, "window_open": NOW}
    assert learning.reward_for(row) == 0.18


def test_a_refused_signal_is_priced_with_slippage():
    """It is a counterfactual - no order rested in that book."""
    row = {"our_ask": 0.80, "won": 1, "rule_match": 0, "fill_kind": "simulated",
           "fee_cost": None, "window_open": NOW}
    assert learning.reward_for(row, 0.02) < learning.reward_for(row, 0.0)


def test_winners_blocked_and_losers_admitted_are_counted_separately():
    rows = rows_for(CTX, 200, qualified=True, win_rate=0.75)
    result = learning.train(
        rows, fingerprint=feature_contract.FINGERPRINT,
        feature_definitions=feature_contract.CONTRACT.payload(),
        feature_version="brti-1",
    )
    arm = result.arms[f"{CTX}|accept"]
    assert arm.winners_blocked > 0
    assert arm.losers_blocked > 0
    assert arm.winners_blocked + arm.losers_blocked > 0
    payload = arm.payload()
    assert "winners_blocked" in payload and "losers_blocked" in payload


def test_provenance_travels_with_the_artefact_and_survives_a_reload(tmp_path):
    rows = rows_for(CTX, 300, qualified=True, win_rate=0.72)
    result = learning.train(
        rows, fingerprint=feature_contract.FINGERPRINT,
        feature_definitions=feature_contract.CONTRACT.payload(),
        feature_version="brti-1",
        provenance={"sources": ["data/brti_history.db"], "markets": 300},
    )
    path = tmp_path / "p.json"
    result.policy.save(path)
    reloaded = intel.Policy.load(path)
    assert reloaded.provenance["sources"] == ["data/brti_history.db"]
    assert reloaded.report["markets"] == 300
    assert reloaded.training_cutoff_ms > 0
    assert reloaded.feature_fingerprint == feature_contract.FINGERPRINT
    # The file itself is readable by a person, not just by us.
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    assert raw["provenance"]["markets"] == 300


# --------------------------------- confidence reaches the HEADER, not a line


def test_the_delta_moves_the_displayed_confidence_word():
    """A line saying "confidence lowered" beside a header still reading HIGH
    has not lowered confidence. The delta must move the word itself."""
    from btc15_signal.main import confidence_label

    facts = [{"passed": True}] * 4
    opened = NOW
    plain = confidence_label(facts, opened, None)
    lowered = confidence_label(facts, opened, None, -40)
    raised = confidence_label(facts, opened, None, 40)
    assert lowered != plain, "a large negative delta must change the label"
    assert {plain, lowered, raised} != {plain}
    # Clamped with everything else - no delta can drive it out of range.
    assert confidence_label(facts, opened, None, -999) in (
        "LOW", "MEDIUM", "HIGH"
    )
    assert confidence_label(facts, opened, None, 999) in (
        "LOW", "MEDIUM", "HIGH"
    )


def test_confidence_applies_in_shadow_mode_but_execution_does_not():
    """The deployed reality: the layer is in shadow, and a calibration must
    still be visible there. Requiring the execution switch to see a label
    would mean granting the power to trade on one to read it."""
    verdict = intel.Verdict(
        base_qualified=True, failed_gates=(), final_action=intel.VETO,
        reason="adverse", confidence_delta=-12, evidence_n=300,
    )
    applied = intel.authorise(
        verdict,
        may_confidence=True,                 # intelligence_enabled
        may_veto=intel_mode.may_veto("shadow"),
        may_admit=intel_mode.may_admit("shadow"),
    )
    assert applied.confidence_delta == -12       # the label moves
    assert applied.final_action == intel.NEUTRAL  # the order does not
    assert applied.qualifies is True
    assert applied.evidence_action == intel.VETO


def test_policy_line_is_silent_on_a_neutral_verdict():
    from btc15_signal import messages

    quiet = intel.Verdict(
        base_qualified=True, failed_gates=(), final_action=intel.NEUTRAL,
        reason="pattern supports the existing decision", evidence_n=300,
    )
    assert messages.policy_line(quiet) == ""


def test_the_intelligence_verdict_is_not_bound_to_a_reused_local_name():
    """`primary_signal` also keeps a local `verdict` holding a log STRING.

    Binding the intelligence Verdict to that name meant the alert sometimes
    rendered confidence from a `str` and raised - but only on the branches that
    reassign it, which is why it survived a full suite once. This pins the
    separation at the source, because the failure is a name, not a value.
    """
    import inspect

    from btc15_signal import main

    source = inspect.getsource(main.primary_signal)
    assert "intel_verdict = intelligence_verdict(" in source
    assert "verdict = intelligence_verdict(" not in source.replace(
        "intel_verdict = intelligence_verdict(", ""
    )
    # The display must read the intelligence verdict, never the log string.
    assert "intel_verdict.confidence_delta" in source
    assert "messages.policy_line(intel_verdict)" in source


def test_check_withdrawals_uses_real_forward_rows_and_persists(tmp_path):
    """The withdrawal path must read the FORWARD TABLE, not a passed-in dict.

    `deteriorated` is pure and easy to test with a synthetic scoreboard; the
    thing that can silently never fire is the wiring between it and the rows
    the service actually accumulates. So this builds real
    `candidate_evaluations` and drives the runner's own method.
    """
    from btc15_signal.config import Settings
    from btc15_signal.learning_runner import LearningRunner

    store = make_store(tmp_path)
    policy_path = tmp_path / "p.json"
    key = f"{CTX}|accept"
    intel.Policy(
        version="k1", feature_version="brti-1", data_end_ms=NOW,
        arms={key: {"n": 400, "mean": -0.06, "low": -0.10, "high": -0.02,
                    "delta": -12, "action": "veto", "promoted": True}},
        vetoes_enabled=True,
        feature_fingerprint=feature_contract.FINGERPRINT,
        feature_definitions=feature_contract.CONTRACT.payload(),
    ).save(policy_path)
    settings = Settings(intelligence_policy_path=str(policy_path),
                        database_path=str(tmp_path / "t.db"),
                        learning_min_withdrawal_n=20)
    runner = LearningRunner(settings, store)

    # 25 markets where the veto would have stood aside from a WINNER. Each is
    # its own window: repeated polls of one market would not be 25 markets.
    rows = []
    for i in range(25):
        rows.append({
            "window_open": NOW + i * 900_000, "decided_ms": NOW + i * 900_000,
            "ticker": f"T{i}", "candidate_id": "cabc1234",
            "candidate_version": "v1", "context_key": CTX,
            "proposed_action": "veto", "baseline_qualified": 1,
            "would_change": 1, "side": "UP", "ask": 0.80, "remaining_s": 600,
            "feature_version": "brti-1",
        })
    store.record_candidate_evaluations(rows)
    for i in range(25):
        store.grade_candidates(
            NOW + i * 900_000, "UP", NOW + 10_000_000,
            lambda ask, won: (1.0 if won else 0.0) - ask - 0.01,
        )
    board = runner.learning.forward_scoreboard()
    assert board[key]["changes"] == 25
    assert board[key]["incremental"] < 0          # it blocked 25 winners

    runner._check_withdrawals(NOW + 11_000_000)
    after = intel.Policy.load(policy_path)
    assert after.arms[key]["action"] == intel.NEUTRAL
    assert after.arms[key]["promoted"] is False
    assert after.vetoes_enabled is False
    assert len(runner.learning.withdrawals()) == 1
    # The measurement is kept; only the authority is taken away.
    assert after.arms[key]["n"] == 400


def test_forward_record_survives_a_candidate_id_change():
    """A cell that has carried two ids keeps one continuous forward record.

    Ids were positional (`c01` meant a different cell after every refit) and
    are now derived from the context. The CELL is the identity the rows are
    really about, so its history is accumulated across ids - without rewriting
    a single stored row.
    """
    from btc15_signal import messages
    from btc15_signal.candidates import Candidate, CandidateSet

    new_set = CandidateSet(
        version="brti-cand-2", feature_version="brti-1",
        feature_fingerprint=feature_contract.FINGERPRINT,
        candidates=(Candidate(
            candidate_id="cd2200ee5", context=f"{CTX}|accept",
            proposed_action="veto", train_n=143, train_mean=-0.004,
            promotes=False, delta=-1,
        ),),
    )
    board = [{                       # recorded under the OLD id
        "candidate_id": "c01", "candidate_version": "brti-cand-1",
        "proposed_action": "veto", "context_key": CTX, "seen": 7,
        "changes": 7, "graded": 7, "incremental": -0.8542,
    }]
    text = messages.learning(head="H", state=None, candidates=new_set,
                             board=board)
    assert "7 graded" in text
    assert "-0.8542" in text
    # ...and it must be labelled as forgone profit, not as a loss.
    assert "opportunity cost" in text
    assert "not a realised" in text


def test_an_invalid_policy_reports_no_active_adjustments(tmp_path):
    """Its arms still carry deltas in the file. None of them is doing anything.

    Reporting seven live confidence adjustments for an artefact whose every
    answer is a refusal is the exact illusion this work exists to remove, and
    it is one the state report produced on the first try.
    """
    from btc15_signal.config import Settings
    from btc15_signal.learning_runner import LearningRunner

    store = make_store(tmp_path)
    policy_path = tmp_path / "p.json"
    intel.Policy(
        version="v1-retired", feature_version="binance-1",
        arms={"us · high · dist3+ · px<70|reject": {
            "n": 200, "delta": -12, "action": "neutral"}},
    ).save(policy_path)
    settings = Settings(intelligence_policy_path=str(policy_path),
                        database_path=str(tmp_path / "t.db"))
    snap = LearningRunner(settings, store).snapshot(NOW)
    assert snap["policy_valid"] is False
    assert snap["active_adjustments"] == []
    assert snap["confidence_arms"] == 0
    assert snap["adjusting_confidence"] is False
    assert snap["authorised_to_execute"] is False


def test_poll_does_not_wait_for_the_fit(tmp_path):
    """Training must never sit on the path of a decision.

    `run` awaits a worker thread; awaiting THAT from `poll` would stall the
    ten-second poll loop for the length of the fit - about eight seconds on the
    current corpus. Running in a thread is not enough on its own. What matters
    is that the poll does not wait for the thread.
    """
    import asyncio

    from btc15_signal.config import Settings
    from btc15_signal.learning_runner import LearningRunner

    store = make_store(tmp_path)
    policy_path = tmp_path / "p.json"
    intel.Policy(
        version="v1-retired", feature_version="binance-1",
        arms={"us · high · dist3+ · px<70|reject": {"n": 200}},
    ).save(policy_path)
    settings = Settings(intelligence_policy_path=str(policy_path),
                        database_path=str(tmp_path / "t.db"))
    runner = LearningRunner(settings, store)

    started = asyncio.Event()
    finished = asyncio.Event()

    async def slow_run(trigger, now_ms):
        started.set()
        await asyncio.sleep(0.3)
        runner._busy = False
        finished.set()

    runner.run = slow_run

    async def drive():
        import time as _t
        t0 = _t.perf_counter()
        await runner.poll(NOW)
        elapsed = _t.perf_counter() - t0
        assert elapsed < 0.1, f"poll blocked for {elapsed:.3f}s"
        assert runner._busy is True          # guarded against a second start
        await runner.poll(NOW + 1000)        # would be throttled anyway
        await asyncio.wait_for(finished.wait(), timeout=2)
        assert started.is_set()

    asyncio.run(drive())


# ------------------------------------------- broker reconciliation, exactly


def _intel_row(store, *, window, ticker, side, ask, qualified=1):
    store.record_intelligence({
        "window_open": window, "ticker": ticker, "decided_ms": window,
        "remaining_s": 600, "side": side, "ask": ask,
        "base_qualified": qualified, "final_action": "neutral",
        "confidence_delta": 0, "context_key": f"{CTX}|accept",
        "feature_version": "brti-1",
    })


def _settlement(store, ticker, **kw):
    row = {"ticker": ticker, "yes_count": 0.0, "no_count": 0.0,
           "yes_cost": 0.0, "no_cost": 0.0, "revenue_cents": 0,
           "fee_cost": 0.0, "pnl": 0.0, "settled_ms": NOW, "synced_at": NOW,
           "window_ms": NOW, "event_ticker": None, "market_result": "yes"}
    row.update(kw)
    store.db.execute(
        "INSERT OR REPLACE INTO settlements ({}) VALUES ({})".format(
            ", ".join(row), ", ".join("?" * len(row))),
        tuple(row.values()),
    )
    store.db.commit()


def _fill(store, **kw):
    store.db.execute(
        "INSERT INTO fills ({}) VALUES ({})".format(
            ", ".join(kw), ", ".join("?" * len(kw))),
        tuple(kw.values()),
    )
    store.db.commit()


def test_an_execution_is_read_from_the_settlement_not_from_the_fills(tmp_path):
    """`fills.side` does not reliably name the leg we held.

    This account has entries booked `buy/yes` AND entries booked `sell/no`, and
    a cash-out of a YES position reported as `sell/no` carrying `yes_price`
    0.997. Any reading of our position from that field is a guess, and a guess
    about which side we were on inverts the trade. `/portfolio/settlements`
    states the counts, the costs and the money outright.
    """
    store = make_store(tmp_path)
    _intel_row(store, window=NOW, ticker="T1", side="DOWN", ask=0.77)
    _fill(store, fill_id="f1", ticker="T1", action="sell", side="no",
          count=2.0, yes_price=0.22, no_price=0.78, fee_cost=0.0241,
          filled_ms=NOW)
    _settlement(store, "T1", no_count=2.0, no_cost=1.56, revenue_cents=200,
                fee_cost=0.0241, pnl=0.4159, market_result="no")
    store.grade_intelligence(NOW, "DOWN", 0.0, NOW + 1000)
    rows, prov = learning_data.live_rows(
        store.db, fingerprint=feature_contract.FINGERPRINT
    )
    assert prov.live_actual_fills == 1
    row = rows[0]
    assert row["contracts"] == 2.0
    assert row["executed_price"] == pytest.approx(0.78)
    assert row["fee_cost"] == pytest.approx(0.0241 / 2)
    assert row["realised_pnl"] == pytest.approx(0.4159 / 2)
    # The reward IS the broker's money per contract. Nothing reconstructs it.
    assert learning.reward_for(row) == pytest.approx(0.20795)
    # The DECISION-time ask is preserved separately - a calibration has to be
    # measured against what the price predicted, not what we paid.
    assert row["our_ask"] == 0.77


def test_a_cashed_out_pair_is_one_position_not_two(tmp_path):
    """Kalshi books an early exit as buying the opposite side and nets the
    pair, so a cashed-out market shows BOTH counts. The leg we opened is the
    expensive one, and the size is the netted pair, not their sum."""
    store = make_store(tmp_path)
    _intel_row(store, window=NOW, ticker="T1", side="UP", ask=0.82)
    _fill(store, fill_id="f1", ticker="T1", action="buy", side="yes",
          count=2.0, yes_price=0.86, no_price=0.14, fee_cost=0.0169,
          filled_ms=NOW)
    _fill(store, fill_id="f2", ticker="T1", action="sell", side="no",
          count=2.0, yes_price=0.997, no_price=0.003, fee_cost=0.0005,
          filled_ms=NOW + 60_000)
    _settlement(store, "T1", yes_count=2.0, no_count=2.0, yes_cost=1.72,
                no_cost=0.006, fee_cost=0.0174, pnl=0.2566)
    store.grade_intelligence(NOW, "UP", 0.0, NOW + 1000)
    rows, _ = learning_data.live_rows(
        store.db, fingerprint=feature_contract.FINGERPRINT
    )
    row = rows[0]
    assert row["contracts"] == 2.0                  # not 4
    assert row["executed_price"] == pytest.approx(0.86)
    assert learning.reward_for(row) == pytest.approx(0.1283)


def test_the_side_we_held_comes_from_the_settlement_counts(tmp_path):
    """A decision on the side the broker did not settle is not an execution."""
    store = make_store(tmp_path)
    _intel_row(store, window=NOW, ticker="T1", side="UP", ask=0.60)
    _intel_row(store, window=NOW, ticker="T1", side="DOWN", ask=0.40)
    _fill(store, fill_id="f1", ticker="T1", action="buy", side="no",
          count=2.0, yes_price=0.60, no_price=0.40, fee_cost=0.02,
          filled_ms=NOW)
    _settlement(store, "T1", no_count=2.0, no_cost=0.80, revenue_cents=200,
                fee_cost=0.02, pnl=1.18, market_result="no")
    store.grade_intelligence(NOW, "DOWN", 0.0, NOW + 1000)
    rows, prov = learning_data.live_rows(
        store.db, fingerprint=feature_contract.FINGERPRINT
    )
    kinds = {r["side"]: r["fill_kind"] for r in rows}
    assert kinds == {"DOWN": "actual", "UP": "simulated"}
    assert prov.live_actual_fills == 1


def test_several_fills_are_one_execution(tmp_path):
    """19 of this account's market-sides have two to four buy fills - a base
    order plus recovery add-ons. They are ONE opportunity with one set of
    numbers, and the settlement row already totals them."""
    store = make_store(tmp_path)
    _intel_row(store, window=NOW, ticker="T1", side="UP", ask=0.70)
    for i in range(3):
        _fill(store, fill_id=f"f{i}", ticker="T1", action="buy",
              side="yes", count=1.0, yes_price=0.70, no_price=0.30,
              fee_cost=0.0147, filled_ms=NOW + i)
    _settlement(store, "T1", yes_count=3.0, yes_cost=2.10, revenue_cents=300,
                fee_cost=0.0441, pnl=0.8559)
    store.grade_intelligence(NOW, "UP", 0.0, NOW + 1000)
    rows, prov = learning_data.live_rows(
        store.db, fingerprint=feature_contract.FINGERPRINT
    )
    assert prov.live_actual_fills == 1          # one execution, not three
    row = rows[0]
    assert row["contracts"] == 3.0              # counted once, in full
    assert row["fill_count"] == 3               # and it took three fills
    assert row["fee_cost"] == pytest.approx(0.0441 / 3)
    assert learning.reward_for(row) == pytest.approx(0.8559 / 3)


def test_a_null_ticker_is_resolved_through_predictions_not_rewritten(tmp_path):
    """Every row written before 2026-09-23 has ticker NULL - it was read off
    the snapshot, which has no such attribute. `predictions` recorded the same
    window's ticker correctly throughout, so the join is recoverable from data
    already stored, at READ time, without touching the archive."""
    store = make_store(tmp_path)
    _intel_row(store, window=NOW, ticker=None, side="UP", ask=0.86)
    store.db.execute(
        "INSERT INTO predictions (window_open, created_at, target, "
        "entry_price, side, bucket, raw_probability, contract_ticker) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (NOW, NOW, 1.0, 0.86, "UP", -1, None, "T1"),
    )
    store.db.commit()
    _fill(store, fill_id="f1", ticker="T1", action="buy", side="yes",
          count=1.0, yes_price=0.86, no_price=0.14, fee_cost=0.0084,
          filled_ms=NOW)
    _settlement(store, "T1", yes_count=1.0, yes_cost=0.86, revenue_cents=100,
                fee_cost=0.0084, pnl=0.1316)
    store.grade_intelligence(NOW, "UP", 0.0, NOW + 1000)
    rows, prov = learning_data.live_rows(
        store.db, fingerprint=feature_contract.FINGERPRINT
    )
    assert prov.live_actual_fills == 1
    assert rows[0]["ticker"] == "T1"
    # The stored row is untouched.
    assert store._dicts(
        "SELECT ticker FROM intelligence_decisions"
    )[0]["ticker"] is None


def test_the_placeholder_realised_pnl_is_never_used_as_money(tmp_path):
    """`grade_intelligence` is called with a literal 0.0, so every graded row
    carries realised_pnl = 0.0 and not one of them means it. Reading it would
    score every real trade as break-even."""
    store = make_store(tmp_path)
    _intel_row(store, window=NOW, ticker="T1", side="UP", ask=0.86)
    store.grade_intelligence(NOW, "UP", 0.0, NOW + 1000)
    assert store._dicts(
        "SELECT realised_pnl FROM intelligence_decisions"
    )[0]["realised_pnl"] == 0.0
    rows, _ = learning_data.live_rows(
        store.db, fingerprint=feature_contract.FINGERPRINT
    )
    # No settlement row, so no execution and no claim of realised money.
    assert rows[0]["fill_kind"] == "simulated"
    assert rows[0]["realised_pnl"] is None
    assert learning.reward_for(rows[0]) == pytest.approx(
        1.0 - 0.86 - learning.kalshi_fee_charged(0.86, 1)
    )


def test_a_gate_string_is_not_iterated_character_by_character():
    """Both live callers hand `intelligence_verdict` a comma-joined STRING.

    The old expression tested `isinstance(failed_checks, list)` - a string is
    not - and fell through to `tuple(str(x) for x in failed_checks)`, which
    yields CHARACTERS. "BRTI distance" was archived as thirteen gates,
    `B, R, T, I, ...`. Nothing raised; the admission path, which may only
    rescue a setup whose failing gates are exactly the one it names, was simply
    dead by typo.
    """
    from btc15_signal.main import normalise_gates

    assert normalise_gates("BRTI distance") == ("BRTI distance",)
    assert normalise_gates("Decision ask, BRTI distance") == (
        "Decision ask", "BRTI distance",
    )
    assert normalise_gates([{"name": "momentum strength"}]) == (
        "momentum strength",
    )
    assert normalise_gates(("contract price band",)) == (
        "contract price band",
    )
    assert normalise_gates(None) == ()
    assert normalise_gates("") == ()
    # And the admission path must now be reachable with a real gate string.
    arm = {"n": 400, "mean": 0.10, "low": 0.04, "high": 0.16, "delta": 12,
           "action": "admit", "gate": "BRTI distance", "promoted": True}
    policy = intel.Policy(
        version="p1", feature_version="brti-1",
        feature_fingerprint=feature_contract.FINGERPRINT,
        feature_definitions=feature_contract.CONTRACT.payload(),
        admissions_enabled=True, arms={f"{CTX}|reject": arm},
    )
    verdict = intel.decide(
        context_key=f"{CTX}|reject", base_qualified=False,
        failed_gates=normalise_gates("BRTI distance"), ask=0.80, policy=policy,
    )
    assert verdict.final_action == intel.ADMIT


def test_a_thin_cell_records_the_count_it_names():
    """The reason said "thin evidence (n=53)" while `evidence_n` stayed 0.

    Any aggregate over the column read every below-bar cell as having no
    evidence at all, and "how close is this cell to the bar?" could only be
    answered by parsing English out of a text field.
    """
    policy = intel.Policy(
        version="p1", feature_version="brti-1",
        feature_fingerprint=feature_contract.FINGERPRINT,
        feature_definitions=feature_contract.CONTRACT.payload(),
        min_evidence=120,
        arms={f"{CTX}|accept": {"n": 53, "mean": -0.02, "delta": -4}},
    )
    verdict = intel.decide(
        context_key=f"{CTX}|accept", base_qualified=True, failed_gates=(),
        ask=0.80, policy=policy,
    )
    assert verdict.final_action == intel.NEUTRAL
    assert verdict.reason == "thin evidence (n=53)"
    assert verdict.evidence_n == 53
    assert verdict.confidence_delta == 0      # thin evidence adjusts nothing


# ------------------------- confidence is calibration, not profitability


def _fit_one(rows):
    return learning.train(
        rows, fingerprint=feature_contract.FINGERPRINT,
        feature_definitions=feature_contract.CONTRACT.payload(),
        feature_version="brti-1",
    )


def test_an_expensive_cell_that_wins_is_not_marked_low_confidence():
    """Negative profitability does not mean a bad directional call.

    A contract at 89c that wins 89% of the time loses money on every trade and
    is still exactly as likely to win as the market says. Sizing the confidence
    label off dollars showed "confidence lowered" on setups the market gets
    right nine times in ten - a statement about price dressed as a statement
    about the outcome.
    """
    rows = rows_for(CTX, 400, qualified=True, win_rate=0.90, ask=0.90)
    result = _fit_one(rows)
    arm = result.arms[f"{CTX}|accept"]
    assert arm.mean < 0, "this cell must genuinely lose money"
    assert abs(arm.calibration) < 0.03, "and its own score must be calibrated"
    assert arm.delta == 0
    assert "calibration" in arm.delta_reason


def test_the_market_comparison_is_measured_and_never_applied():
    """`observed win rate - ask` says whether KALSHI is right. It is worth
    knowing and it is kept, but it is not a statement about our model and it
    does not move our label."""
    rows = rows_for(CTX, 400, qualified=True, win_rate=0.90, ask=0.70)
    arm = _fit_one(rows).arms[f"{CTX}|accept"]
    # The market is badly wrong here: 70c on a cell that wins 90%.
    assert arm.market_calibration > 0.15
    # ...and our own score is perfectly calibrated to it, so nothing moves.
    assert abs(arm.calibration) < 0.02
    assert arm.delta == 0
    payload = arm.payload()
    assert payload["market_calibration"] == round(arm.market_calibration, 6)
    assert payload["calibration"] == round(arm.calibration, 6)


def test_confidence_must_hold_out_of_sample():
    """Evaluating every eligible cell does not remove the need to validate.

    A cell whose calibration error reverses on the validation slice is not a
    calibration error; it is the fitted period.
    """
    # One cell beats the pooled rate early and misses it late; a second cell
    # keeps the pool honest so the curve is not just this cell's own average.
    early = rows_for(CTX, 300, qualified=True, win_rate=0.95, ask=0.70,
                     start_ms=NOW, spread_days=30)
    late = rows_for(CTX, 220, qualified=True, win_rate=0.20, ask=0.70,
                    start_ms=NOW + 31 * DAY, spread_days=22)
    other = rows_for("us · low · bd5-10 · px<70|reject", 400, qualified=False,
                     win_rate=0.55, ask=0.55, start_ms=NOW + 500_000,
                     spread_days=50)
    result = _fit_one(early + late + other)
    arm = result.arms[f"{CTX}|accept"]
    assert arm.delta == 0
    assert "out of sample" in arm.delta_reason


def test_profit_still_drives_veto_and_admission_not_calibration():
    """The other side of the separation: execution is about money."""
    rows = rows_for(CTX, 400, qualified=True, win_rate=0.80, ask=0.80)
    result = _fit_one(rows)
    arm = result.arms[f"{CTX}|accept"]
    # Perfectly calibrated, so no confidence change...
    assert abs(arm.calibration) < 0.02
    assert arm.delta == 0
    # ...but it loses the fee every time, so the money points to a veto.
    assert arm.mean < 0
    assert arm.action == intel.VETO and arm.promoted is True


# --------------------------------------------- no market spans two splits


def test_every_row_of_a_market_stays_in_one_split():
    """A window the model flipped inside has an UP row and a DOWN row.

    Cutting the list at a ROW index can land them on opposite sides of the
    boundary, which puts the same market's outcome in both the fit and the
    check of the fit. It is invisible - both slices look the right size - and
    it flatters exactly the cells that contain flipped windows.
    """
    rows = []
    for i in range(300):
        window = NOW + i * 900_000
        rows.append({"window_open": window, "our_ask": 0.80, "won": 1,
                     "rule_match": 1, "context_key": CTX, "side": "UP"})
        if i % 3 == 0:                      # every third market flipped
            rows.append({"window_open": window, "our_ask": 0.20, "won": 0,
                         "rule_match": 1, "context_key": CTX, "side": "DOWN"})
    train, validate, holdout = learning.chronological_split(rows)
    assert len(train) + len(validate) + len(holdout) == len(rows)
    tw = {r["window_open"] for r in train}
    vw = {r["window_open"] for r in validate}
    hw = {r["window_open"] for r in holdout}
    assert not (tw & vw), "a market appears in both train and validate"
    assert not (vw & hw), "a market appears in both validate and holdout"
    assert not (tw & hw), "a market appears in both train and holdout"
    # ...and it is still chronological: every train market precedes every
    # validate market, which precedes every holdout market.
    assert max(tw) < min(vw) < max(vw) < min(hw)


def test_the_split_is_still_roughly_the_configured_proportions():
    rows = rows_for(CTX, 1000, qualified=True, win_rate=0.8, spread_days=40)
    train, validate, holdout = learning.chronological_split(rows)
    total = len(rows)
    assert 0.50 < len(train) / total < 0.60
    assert 0.20 < len(validate) / total < 0.30


# ---------------------------------------------- what source is running


def test_the_running_revision_identifies_itself_two_ways():
    """"432c388 plus fifteen changed files" is not a release identifier.

    Either answer alone can lie: `git rev-parse` reports a clean commit for a
    tree edited after it, and a content hash means nothing to someone holding
    the repository. So the process reports both.
    """
    from btc15_signal import revision

    rev = revision.REVISION
    assert set(rev) >= {"commit", "short", "branch", "dirty", "fingerprint"}
    assert len(rev["fingerprint"]) == 12
    assert isinstance(rev["dirty"], bool)
    # Resolved at import from the files the interpreter loaded, so it is stable
    # within the process even if the working tree moves underneath it.
    assert revision.REVISION is rev
    assert rev["fingerprint"] in revision.line()
    assert rev["short"] in revision.line()


def test_the_source_fingerprint_changes_with_the_source(tmp_path, monkeypatch):
    """A commit hash cannot catch an edit made after the commit. This can."""
    from btc15_signal import revision

    package = tmp_path / "pkg"
    package.mkdir()
    (package / "a.py").write_text("x = 1", encoding="utf-8")
    monkeypatch.setattr(revision, "PACKAGE", package)
    first = revision.source_fingerprint()
    (package / "a.py").write_text("x = 2", encoding="utf-8")
    assert revision.source_fingerprint() != first
    # ...and is stable when nothing changed.
    assert revision.source_fingerprint() == revision.source_fingerprint()


def test_the_learning_snapshot_carries_the_revision(tmp_path):
    from btc15_signal.config import Settings
    from btc15_signal.learning_runner import LearningRunner

    store = make_store(tmp_path)
    policy_path = tmp_path / "p.json"
    intel.Policy().save(policy_path)
    settings = Settings(intelligence_policy_path=str(policy_path),
                        database_path=str(tmp_path / "t.db"))
    snap = LearningRunner(settings, store).snapshot(NOW)
    assert "revision" in snap
    assert snap["revision"]["fingerprint"]


def test_a_policy_fitted_by_a_superseded_method_cannot_act(tmp_path):
    """`feature_version` says what the numbers ARE; `model_version` says what
    was DONE to them, and they fail the same way.

    A delta fitted when confidence was sized from dollars per contract does not
    mean what this build means by a delta - the field is the same integer
    either way, which is exactly why it needs a version rather than an
    inspection. Without this the corrected build would have kept applying the
    old method's deltas until the next scheduled refit.
    """
    stale = intel.Policy(
        version="kalshi-brti-1-old", model_version="arms-shrunk-2",
        feature_version="brti-1", data_end_ms=NOW,
        arms={f"{CTX}|accept": {"n": 300, "delta": -12, "action": "neutral"}},
        feature_fingerprint=feature_contract.FINGERPRINT,
        feature_definitions=feature_contract.CONTRACT.payload(),
    )
    ok, why = learning.policy_is_valid(
        stale, fingerprint=feature_contract.FINGERPRINT,
        feature_version="brti-1",
    )
    assert not ok
    assert "superseded method" in why and "arms-shrunk-2" in why

    # ...and a fresh fit carries the current method, so it passes.
    fresh = learning.train(
        rows_for(CTX, 400, qualified=True, win_rate=0.85, ask=0.80),
        fingerprint=feature_contract.FINGERPRINT,
        feature_definitions=feature_contract.CONTRACT.payload(),
        feature_version="brti-1",
    ).policy
    assert fresh.model_version == learning.MODEL_VERSION
    ok, why = learning.policy_is_valid(
        fresh, fingerprint=feature_contract.FINGERPRINT,
        feature_version="brti-1",
    )
    assert ok, why


def test_the_stale_method_triggers_an_automatic_refit(tmp_path):
    """The guard is not just a refusal: it makes a rebuild due."""
    from btc15_signal.config import Settings
    from btc15_signal.learning_runner import LearningRunner

    store = make_store(tmp_path)
    policy_path = tmp_path / "p.json"
    intel.Policy(
        version="kalshi-brti-1-old", model_version="arms-shrunk-2",
        feature_version="brti-1", data_end_ms=NOW,
        arms={f"{CTX}|accept": {"n": 300, "delta": -12, "action": "neutral"}},
        feature_fingerprint=feature_contract.FINGERPRINT,
        feature_definitions=feature_contract.CONTRACT.payload(),
    ).save(policy_path)
    settings = Settings(intelligence_policy_path=str(policy_path),
                        database_path=str(tmp_path / "t.db"))
    runner = LearningRunner(settings, store)
    assert runner.due(NOW) == (True, "bootstrap")
    # And meanwhile the stale deltas do not reach a decision.
    snap = runner.snapshot(NOW)
    assert snap["policy_valid"] is False
    assert snap["adjusting_confidence"] is False
    assert snap["active_adjustments"] == []


# ------------------------- attribution through broker order and fill ids


def test_the_opening_side_comes_from_fill_ORDER_not_from_which_leg_cost_more():
    """Buy YES at 0.80, watch it fall, cash out by buying NO at 0.85, and the
    NO leg is the expensive one. The old rule reported the position we exited
    INTO as the position we took. Over this account's 60 closed pairs it
    misattributed 6, including a -$1.75 loser."""
    fills = [
        {"fill_id": "f1", "order_id": "o1", "ticker": "T", "side": "yes",
         "action": "buy", "count": 2.0, "yes_price": 0.80, "no_price": 0.20,
         "fee_cost": 0.0224, "filled_ms": 100},
        {"fill_id": "f2", "order_id": "o2", "ticker": "T", "side": "no",
         "action": "sell", "count": 2.0, "yes_price": 0.15, "no_price": 0.85,
         "fee_cost": 0.0179, "filled_ms": 200},
    ]
    settlement = {"yes_count": 2.0, "no_count": 2.0, "yes_cost": 1.60,
                  "no_cost": 1.70, "fee_cost": 0.0403, "pnl": -0.1403}
    out = learning_data.attribute_execution(fills, settlement)
    assert out["resolved"], out["reason"]
    assert out["side"] == "UP"            # the FIRST fill, not the dearer leg
    assert out["entry_price"] == pytest.approx(0.80)
    assert out["exit_price"] == pytest.approx(0.85)
    assert out["pnl_per_contract"] == pytest.approx(-0.1403 / 2)
    assert out["fill_ids"] == ("f1", "f2")
    assert out["order_ids"] == ("o1", "o2")


def test_adds_are_linked_to_the_entry_and_priced_together():
    """A base order plus recovery add-ons is one position at a weighted price."""
    fills = [
        {"fill_id": "f1", "order_id": "o1", "ticker": "T", "side": "no",
         "action": "buy", "count": 2.0, "yes_price": 0.30, "no_price": 0.70,
         "fee_cost": 0.0294, "filled_ms": 100},
        {"fill_id": "f2", "order_id": "o2", "ticker": "T", "side": "no",
         "action": "buy", "count": 1.0, "yes_price": 0.20, "no_price": 0.80,
         "fee_cost": 0.0112, "filled_ms": 200},
    ]
    settlement = {"yes_count": 0.0, "no_count": 3.0, "yes_cost": 0.0,
                  "no_cost": 2.20, "fee_cost": 0.0406, "pnl": 0.7594}
    out = learning_data.attribute_execution(fills, settlement)
    assert out["resolved"], out["reason"]
    assert out["side"] == "DOWN"
    assert out["contracts"] == 3.0
    assert out["adds"] == 1
    assert out["entry_price"] == pytest.approx(2.20 / 3)
    assert out["pnl_per_contract"] == pytest.approx(0.7594 / 3, abs=1e-6)
    assert len(out["order_ids"]) == 2


def test_attribution_that_does_not_reconcile_is_refused():
    """The reconciliation is what makes this attribution and not a guess."""
    fills = [{"fill_id": "f1", "ticker": "T", "side": "yes", "action": "buy",
              "count": 2.0, "yes_price": 0.80, "no_price": 0.20,
              "fee_cost": 0.02, "filled_ms": 100}]
    settlement = {"yes_count": 5.0, "no_count": 0.0, "yes_cost": 4.0,
                  "no_cost": 0.0, "fee_cost": 0.05, "pnl": 1.0}
    out = learning_data.attribute_execution(fills, settlement)
    assert out["resolved"] is False
    assert "do not reconcile" in out["reason"]
    assert "yes_count" in out["reason"]


def test_a_settled_market_with_no_fills_is_unresolved():
    out = learning_data.attribute_execution(
        [], {"yes_count": 2.0, "no_count": 0.0, "yes_cost": 1.6,
             "no_cost": 0.0, "fee_cost": 0.02, "pnl": 0.38})
    assert out["resolved"] is False
    assert "no fill records" in out["reason"]


def test_unattributed_markets_are_excluded_from_execution_learning(tmp_path):
    """A trade we cannot attribute is not evidence about a decision - and it
    must not quietly become a counterfactual either, because something really
    did happen there."""
    store = make_store(tmp_path)
    _intel_row(store, window=NOW, ticker="T1", side="UP", ask=0.80)
    _settlement(store, "T1", yes_count=5.0, yes_cost=4.0, fee_cost=0.05,
                pnl=1.0)
    _fill(store, fill_id="f1", ticker="T1", action="buy", side="yes",
          count=2.0, yes_price=0.80, no_price=0.20, fee_cost=0.02,
          filled_ms=NOW)
    store.grade_intelligence(NOW, "UP", 0.0, NOW + 1000)
    rows, prov = learning_data.live_rows(
        store.db, fingerprint=feature_contract.FINGERPRINT
    )
    assert rows == []
    assert prov.excluded_unattributed == 1
    assert prov.live_actual_fills == 0


# ------------------- confidence calibrates the MODEL, not the market price


def test_the_reliability_curve_maps_the_models_score_to_a_frequency():
    rows = (
        rows_for(CTX, 200, qualified=True, win_rate=0.90, ask=0.80)
        + rows_for(CTX, 200, qualified=True, win_rate=0.50, ask=0.80,
                   start_ms=NOW + 300 * DAY)
    )
    for i, row in enumerate(rows):
        row["model_points"] = 95 if i < 200 else 35
    curve = learning.reliability_curve(rows)
    assert curve["n"] == 400
    assert curve["buckets"][9] > curve["buckets"][3]
    # A row with no recorded score predicts nothing.
    assert learning.predicted_probability(None, curve) is None
    assert learning.predicted_probability(95, curve) == curve["buckets"][9]


def test_confidence_follows_the_model_score_not_the_ask():
    """The two come apart, and only one of them is our model.

    Here the PRICE is right - 80c on a cell that wins 80% - while the model's
    own score says 0.50, because every one of these rows scored in the middle
    of its range. That is a miscalibrated confidence label on a correctly
    priced market, and it is exactly the case the ask can never surface.
    """
    # INTERLEAVED IN TIME, so the chronological split puts both cells in both
    # slices - otherwise the curve is fitted on one cell and checked on the
    # other, which measures the split rather than the calibration.
    strong = rows_for(CTX, 400, qualified=True, win_rate=0.80, ask=0.80,
                      model_points=45, spread_days=40)
    weak = rows_for("us · low · bd5-10 · px<70|reject", 400, qualified=False,
                    win_rate=0.20, ask=0.20, start_ms=NOW + 450_000,
                    model_points=45, spread_days=40)
    result = _fit_one(strong + weak)
    arm = result.arms[f"{CTX}|accept"]
    # The market is well priced here...
    assert abs(arm.market_calibration) < 0.03
    # ...and the model's own score is not.
    assert arm.calibration > 0.10
    assert arm.delta > 0, arm.delta_reason
    assert "model's own score" in arm.delta_reason
    # The market comparison is reported beside it and labelled as not applied.
    assert "not applied" in arm.delta_reason


def test_an_interval_spanning_zero_reports_insufficient_evidence():
    """Not "the score is correct". The distinction matters: one is a finding,
    the other is an absence of one."""
    # A cell that tracks the pooled rate: no measurable calibration error, and
    # a second cell so the pool is not simply this cell's own average.
    rows = rows_for(CTX, 300, qualified=True, win_rate=0.72, ask=0.72,
                    spread_days=30)
    rows += rows_for("us · low · bd5-10 · px<70|reject", 300, qualified=False,
                     win_rate=0.70, ask=0.70, start_ms=NOW + 450_000,
                     spread_days=30)
    arm = _fit_one(rows).arms[f"{CTX}|accept"]
    assert arm.delta == 0, arm.delta_reason
    assert "INSUFFICIENT EVIDENCE" in arm.delta_reason
    # It must NOT claim the score has been shown correct.
    assert "not a finding that the score is correct" in arm.delta_reason


def test_rows_without_a_recorded_score_cannot_calibrate_it():
    """Every live row written before the score was recorded has none. A
    calibration of predictions needs the predictions."""
    rows = rows_for(CTX, 400, qualified=True, win_rate=0.95, ask=0.70)
    for row in rows:
        row["model_points"] = None
    arm = _fit_one(rows).arms[f"{CTX}|accept"]
    assert arm.calibration_n == 0
    assert arm.delta == 0
    assert "recorded model confidence" in arm.delta_reason
    assert "INSUFFICIENT EVIDENCE" in arm.delta_reason


def test_the_curve_is_fitted_on_train_and_applied_to_validate():
    """Refitting per slice would let each one grade its own homework."""
    rows = rows_for(CTX, 600, qualified=True, win_rate=0.80, ask=0.80)
    for row in rows:
        row["model_points"] = 90
    train, validate, _hold = learning.chronological_split(rows)
    curve = learning.reliability_curve(train)
    train_arms = learning.fit_arms(train, lambda r: 0.0, curve=curve)
    validate_arms = learning.fit_arms(validate, lambda r: 0.0, curve=curve)
    key = f"{CTX}|accept"
    # Both slices are measured against the SAME predicted probability.
    assert train_arms[key].model_probability == pytest.approx(
        validate_arms[key].model_probability
    )


def test_the_ask_based_method_is_retired_and_cannot_act():
    stale = intel.Policy(
        version="v", model_version="arms-calibrated-1", feature_version="brti-1",
        arms={f"{CTX}|accept": {"n": 300, "delta": -12}},
        feature_fingerprint=feature_contract.FINGERPRINT,
        feature_definitions=feature_contract.CONTRACT.payload(),
    )
    ok, why = learning.policy_is_valid(
        stale, fingerprint=feature_contract.FINGERPRINT,
        feature_version="brti-1",
    )
    assert not ok
    assert "superseded method" in why


def test_the_model_score_is_computed_by_one_function_for_both_paths():
    """A calibration compares a PREDICTION with an OUTCOME. If the prediction
    is recomputed differently in training and live, the comparison measures the
    difference between the two implementations."""
    from btc15_signal.levels import confidence_points as level_points
    from btc15_signal.main import model_confidence_points
    from btc15_signal.regime import model_points

    facts = [{"passed": True}] * 3 + [{"passed": False}]
    opened = NOW
    # THE LEVEL TERM IS NOT ZERO under Kalshi-only: no protective level is
    # computed, so the live path calls `level_points(False)`, which is -6.
    # Passing 0 in the corpus scored every historical row six points above the
    # live one and turned the calibration into a comparison between two
    # implementations. That is why both sides call these functions rather than
    # agreeing by inspection.
    assert level_points(False) == -6
    assert model_confidence_points(facts, opened, None) == model_points(
        3, opened, level_points(False)
    )
    assert model_confidence_points(facts, opened, None) != model_points(
        3, opened, 0
    )
