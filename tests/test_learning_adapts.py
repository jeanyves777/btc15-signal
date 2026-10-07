"""The learning loop adopts what it learns (operator, 2026-09-28).

"Do so the system learns and adapts." Verified on the live record the same
morning: BTC and ETH had changed no live decision since 09-26, and ETH and SOL
refused every fresh fit - ETH since 09-27 02:17, SOL since 09-26 - because the
running policy was judged on the validation slice its own rule had been
PROMOTED on, and won by construction. So:

  * fresh fits are compared on the HOLDOUT, the newest slice, which neither
    policy was selected on;
  * each running rule the fresh fit did not re-promote gets ONE verdict
    (`learning.carry_forward`): kept on a supporting live record, let go on a
    condemning one, judged on the holdout when it has none - and nothing is
    carried out of an artefact that cannot act;
  * the forward record counts the poll where a candidate disagreed, and every
    acting rule stays on the watch list, so the live record can decide.

And the local model now runs inside every learning run (`hypotheses.run`),
proposing conditions that the statistics then test; nothing it says changes an
order. The review of 2026-09-28 found the test could not tell noise from an
edge on a few days of data - these tests pin the corrections (FINDINGS 107).
"""

import asyncio
import json
import sqlite3
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from btc15_signal import feature_contract  # noqa: E402
from btc15_signal import hypotheses as H  # noqa: E402
from btc15_signal import learning  # noqa: E402
from btc15_signal import messages  # noqa: E402
from btc15_signal.candidates import CandidateSet  # noqa: E402
from btc15_signal.intelligence_policy import NEUTRAL, VETO, Policy  # noqa: E402
from btc15_signal.store import Store  # noqa: E402

CTX = "bd<5 · px<70 · mom0-5"
KEY = f"{CTX}|accept"
OTHER = "bd5-10 · px70-85 · mom5+|accept"
FP = feature_contract.FINGERPRINT
FV = feature_contract.CONTRACT.version


def policy(arms, version="v", *, feature_version=FV, fingerprint=FP):
    p = Policy(version=version, arms=arms, model_version=learning.MODEL_VERSION,
               feature_version=feature_version, feature_fingerprint=fingerprint)
    p.vetoes_enabled = any(a.get("promoted") and a.get("action") == VETO
                           for a in arms.values())
    return p


def veto_arm(**extra):
    return {"n": 300, "mean": -0.05, "low": -0.09, "high": -0.01,
            "action": VETO, "promoted": True, "delta": -5, **extra}


def holdout(wins, losses, context=CTX, ask=0.60):
    """Rows the rule accepted, in the vetoed cell: `wins` winners, `losses` losers."""
    rows = []
    for i, won in enumerate([1] * wins + [0] * losses):
        rows.append({"window_open": 1_790_000_000_000 + i * 900_000,
                     "context_key": context, "rule_match": 1, "won": won,
                     "our_ask": ask, "side": "UP", "failed_gates": ""})
    return rows


def carry(new, current, forward, rows=(), min_changes=20):
    return learning.carry_forward(new, current, forward, list(rows),
                                  fingerprint=FP, feature_version=FV,
                                  min_changes=min_changes)


# ------------------------------------------------------------ carry forward

def test_a_live_rule_its_record_supports_is_carried_forward():
    """SOL, 09-28: the running veto had +1.30 over 10 live changes; the fresh
    fit did not re-promote it. It stays - its life is its live record."""
    current = policy({KEY: veto_arm()}, "old")
    new = policy({KEY: {"n": 310, "action": NEUTRAL, "promoted": False}}, "new")
    got = carry(new, current, {KEY: {"changes": 10, "incremental": 1.30}})
    assert got.carried == [KEY] and not got.dropped and not got.condemned
    assert new.arms[KEY]["promoted"] and new.arms[KEY]["action"] == VETO
    assert new.arms[KEY]["carried"] is True
    assert "kept from old: live record +1.3000" in new.arms[KEY]["carried_reason"]
    assert new.vetoes_enabled is True
    assert "kept 1 live rule(s)" in got.summary()


def test_nothing_is_carried_out_of_a_policy_that_cannot_act():
    """The review's HIGH finding. At a feature-contract bump the service
    WITHDRAWS the running policy's rules on load, then a bootstrap fit runs:
    carrying from that artefact resurrected a rule fitted under definitions
    this build does not compute - ETH's veto went through brti-2 -> 3 -> 4."""
    for stale in (policy({KEY: veto_arm()}, "old", feature_version="brti-0"),
                  policy({KEY: veto_arm()}, "old", fingerprint="0" * 16)):
        new = policy({}, "new")
        got = carry(new, stale, {KEY: {"changes": 10, "incremental": 1.30}})
        assert got.carried == [] and KEY not in new.arms
        assert new.vetoes_enabled is False


def test_a_live_rule_its_record_condemns_is_let_go():
    current = policy({KEY: veto_arm()}, "old")
    new = policy({}, "new")
    got = carry(new, current, {KEY: {"changes": 25, "incremental": -2.0}})
    assert got.condemned == [KEY] and got.carried == [] and KEY not in new.arms
    assert new.vetoes_enabled is False
    assert "let go 1 its live record condemned" in got.summary()


def test_a_condemned_rule_is_out_of_both_sides_of_the_comparison():
    """Otherwise the holdout could refuse the whole fresh fit for dropping it,
    and the rule its live record condemned would stay live (review, low)."""
    current = policy({KEY: veto_arm(), OTHER: veto_arm()}, "old")
    base = learning.without(current, [KEY])
    assert base.arms[KEY]["action"] == NEUTRAL and not base.arms[KEY]["promoted"]
    assert base.arms[OTHER]["action"] == VETO and base.vetoes_enabled
    assert current.arms[KEY]["action"] == VETO, "the running policy is untouched"


def test_no_live_verdict_is_judged_on_the_newest_slice_kept_when_it_helps():
    current = policy({KEY: veto_arm()}, "old")
    new = policy({}, "new")
    got = carry(new, current, {}, holdout(wins=3, losses=12))
    assert got.carried == [KEY]
    assert "no live verdict yet" in new.arms[KEY]["carried_reason"]
    assert "on the newest slice" in new.arms[KEY]["carried_reason"]


def test_no_live_verdict_and_the_newest_slice_disagrees_the_fresh_fit_stands():
    """ETH, 09-28: its veto had 0 live changes - every such setup fails the
    distance gate - and was being carried forever on that. The newest slice
    says blocking those setups costs money, so the fresh fit stands."""
    current = policy({OTHER: veto_arm()}, "old")
    new = policy({}, "new")
    rows = holdout(wins=12, losses=3, context=OTHER.split("|")[0])
    got = carry(new, current, {OTHER: {"changes": 0, "incremental": 0.0}}, rows)
    assert got.dropped == [OTHER] and OTHER not in new.arms
    assert "scored -" in got.reasons[OTHER]


def test_a_rule_that_changes_nothing_on_the_newest_slice_is_not_kept_on_faith():
    current = policy({OTHER: veto_arm()}, "old")
    new = policy({}, "new")
    got = carry(new, current, {}, holdout(wins=5, losses=5))   # a different cell
    assert got.dropped == [OTHER]
    assert "changed nothing" in got.reasons[OTHER]


def test_too_few_losing_changes_neither_condemn_nor_protect_it():
    current = policy({KEY: veto_arm()}, "old")
    new = policy({}, "new")
    got = carry(new, current, {KEY: {"changes": 5, "incremental": -2.0}},
                holdout(wins=12, losses=3))
    assert got.dropped == [KEY], "judged on the newest slice, which disagrees"


def test_a_rule_the_fresh_fit_re_promoted_is_not_overwritten():
    current = policy({KEY: veto_arm(low=-0.09)}, "old")
    fresh = veto_arm(low=-0.12)
    new = policy({KEY: fresh}, "new")
    got = carry(new, current, {})
    assert got.carried == [] and got.dropped == []
    assert new.arms[KEY]["low"] == -0.12, "the fresher evidence stands"


def test_neutral_and_confidence_arms_are_never_carried():
    current = policy({KEY: {"action": NEUTRAL, "promoted": False, "delta": 4}}, "old")
    new = policy({}, "new")
    assert carry(new, current, {}).carried == []


# --------------------------------------------------- compared on the holdout

def test_the_runner_decides_each_rule_then_compares_on_the_holdout():
    import inspect

    from btc15_signal import learning_runner

    src = " ".join(inspect.getsource(learning_runner.LearningRunner._train).split())
    split = src.index("_train_rows, _validate, holdout_rows = learning.chronological_split(")
    carry_at = src.index("carry = learning.carry_forward( new, current, forward, holdout_rows,")
    comp = src.index("comparison = learning.compare( new, "
                     "learning.without(current, carry.condemned), holdout_rows,")
    assert split < carry_at < comp
    assert "validate_rows," not in src[comp:comp + 120]
    assert "now_ms=now_ms, policy=new," in src, "acting rules stay watched"


def test_the_holdout_is_newer_than_the_validation_slice_it_replaces():
    rows = [{"window_open": w, "side": "UP"} for w in range(100)]
    train, validate, holdout_rows = learning.chronological_split(rows)
    assert max(r["window_open"] for r in validate) < min(r["window_open"] for r in holdout_rows)


# -------------------------------------------- the live record can decide

def test_every_acting_rule_stays_on_the_watch_list(tmp_path):
    """SOL's veto went unrecorded in 7 live windows because three refits had
    turned against the cell and it fell off the candidate list."""
    result = SimpleNamespace(arms={}, report=SimpleNamespace(
        data_end_ms=0, training_cutoff_ms=0))
    acting = policy({KEY: veto_arm(), OTHER: {"action": NEUTRAL, "promoted": False}})
    payload = learning.candidate_payload(
        result, fingerprint=FP, feature_definitions={}, feature_version=FV,
        now_ms=1_790_000_000_000, policy=acting)
    assert [c["context"] for c in payload["candidates"]] == [KEY]
    path = tmp_path / "candidates.json"
    path.write_text(json.dumps(payload))
    rows = CandidateSet.load(path).evaluate(context_key=CTX, qualified=True)
    assert len(rows) == 1 and rows[0]["would_change"] == 1


def test_the_forward_record_keeps_the_poll_where_the_candidate_disagreed(tmp_path):
    """It kept the first matching poll. A veto disagrees only on a poll the
    rule accepted - usually a later one - so SOL's record counted 8 of its 35
    live vetoes."""
    store = Store(str(tmp_path / "s.db"))

    def poll(ms, qualified, ask):
        store.record_candidate_evaluations([{
            "window_open": 500, "decided_ms": ms, "candidate_id": "c01",
            "candidate_version": "v1", "context_key": CTX,
            "proposed_action": VETO, "baseline_qualified": qualified,
            "would_change": qualified, "side": "UP", "ask": ask,
            "feature_version": FV,
        }])

    poll(1, 0, 0.55)             # context matches, rule not yet accepting
    poll(2, 1, 0.72)             # the rule accepts: the veto would bite here
    poll(3, 0, 0.60)             # and a later disagreement-free poll is ignored
    rows = store._dicts("SELECT * FROM candidate_evaluations")
    assert len(rows) == 1
    assert (rows[0]["decided_ms"], rows[0]["would_change"], rows[0]["ask"]) == (2, 1, 0.72)


def test_a_graded_forward_row_is_final(tmp_path):
    store = Store(str(tmp_path / "s.db"))
    row = {"window_open": 501, "decided_ms": 1, "candidate_id": "c01",
           "candidate_version": "v1", "context_key": CTX,
           "proposed_action": VETO, "baseline_qualified": 0,
           "would_change": 0, "side": "UP", "ask": 0.55}
    store.record_candidate_evaluations([row])
    store.db.execute("UPDATE candidate_evaluations SET graded_ms = 9")
    store.record_candidate_evaluations([{**row, "decided_ms": 2,
                                         "baseline_qualified": 1,
                                         "would_change": 1}])
    got = store._dicts("SELECT decided_ms, would_change FROM candidate_evaluations")
    assert got == [{"decided_ms": 1, "would_change": 0}]


# ------------------------------------------------------ the local model

def observations_db(path, n=160, days=10):
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE observations (window_open INTEGER, observed_ms INTEGER, "
                "won INTEGER, our_ask REAL, alerted INTEGER, spread_bps REAL, "
                "trade_count REAL, book_age_s REAL, remaining_s REAL, session TEXT)")
    day = 86_400_000
    for i in range(n):
        w = 1_790_000_000_000 + (i % days) * day + (i // days) * 900_000
        wide = i % 3 == 0
        con.execute("INSERT INTO observations VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (w, w + 1, 0 if wide else 1, 0.80, 1, 400 if wide else 100,
                     50, 5, 500, "us"))
    con.commit()
    con.close()


def test_a_model_proposal_is_tested_and_a_real_edge_survives(tmp_path):
    """The model proposes a filter; the statistics - not the model - decide."""
    db = tmp_path / "x.db"
    observations_db(db)

    def fake_ask(url, model, prompt, timeout):
        assert "spread_bps" in prompt and "our_ask" in prompt
        return json.dumps({"hypotheses": [
            {"name": "tight book", "why": "narrow spreads fill fairly",
             "where": [{"column": "spread_bps", "op": "<", "value": 200}]},
            {"name": "peeks at the answer", "why": "bad",
             "where": [{"column": "won", "op": "==", "value": 1}]},
        ]})

    r = H.run(db, "SOL", url="x", ask=fake_ask)
    assert r["model_ok"] and r["proposed"] == 1, "an outcome column is refused"
    names = [s["name"] for s in r["survivors"]]
    assert names == ["tight book"], "the real edge survives; controls are not reported"


def test_the_day_test_cannot_claim_more_than_the_days_allow():
    """The review's HIGH finding: the day bootstrap said p=1/3000 whenever
    every day agreed in sign - a coin does that a quarter of the time over 3
    days - and random filters 'survived' in 27-98% of runs."""
    three = [(d, 0.01 * (d + 1)) for d in range(3)]
    assert H.boot(three)[3] < 0.001, "the old floor, for the record"
    assert H.sign_flip_p(three) == 0.25
    assert H.sign_flip_p([(d, 0.02) for d in range(8)]) == 2 / 256
    assert H.sign_flip_p([(0, 0.02), (1, -0.02)]) == 1.0
    assert H.sign_flip_p([(d, 0.01) for d in range(20)]) < 0.001


def test_random_filters_rarely_survive(tmp_path):
    """A null simulation, as the review ran it: filters that pick rows at
    random, through the real scoring, on 8 days (BTC's history today; the old
    test reported noise on 27% of runs there). About 5% of runs at most."""
    import random

    rng = random.Random(7)
    rows = [{"day": d, "residual": rng.choice([0.2, -0.8]),
             "spread_bps": rng.random()} for d in range(8) for _ in range(40)]
    reported = 0
    for trial in range(20):
        props = [{"name": f"noise {k}", "why": "x",
                  "terms": [("spread_bps", "<", rng.uniform(0.3, 0.7))]}
                 for k in range(12)]
        reported += any(s["survives"] for s in H.score(rows, props))
    assert reported <= 1


def test_proposals_sharing_a_name_keep_their_own_verdicts():
    """The model names a proposal after its column, so names repeat; BH was
    keyed on the name and a p=0.69 proposal was reported as a survivor."""
    import random

    rng = random.Random(3)
    rows = [{"day": d, "spread_bps": i, "noise": rng.random(),
             "residual": 0.3 if i < 10 else rng.choice([0.2, -0.3])}
            for d in range(10) for i in range(40)]
    for order in ((0, 1), (1, 0)):
        edge = {"name": "same", "why": "x", "terms": [("spread_bps", "<", 10)]}
        noise = {"name": "same", "why": "x", "terms": [("noise", "<", 0.5)]}
        props = [(edge, noise)[i] for i in order]
        by_terms = {s["terms"][0][0]: s for s in H.score(rows, props)}
        assert by_terms["spread_bps"]["survives"] is True
        assert by_terms["noise"]["p"] > 0.05
        assert by_terms["noise"]["survives"] is False, f"order {order}"


def test_a_reply_cut_off_at_the_token_limit_keeps_its_complete_proposals(tmp_path):
    """SOL's reply ran out of tokens inside its 10th proposal on every observed
    run, and the whole reply parsed to NOTHING while the record said the model
    was fine and proposed 0."""
    whole = {"name": "tight book", "why": "w",
             "where": [{"column": "spread_bps", "op": "<", "value": 200}]}
    cut = ('{"hypotheses": [' + json.dumps(whole) + ", " + json.dumps(
        {**whole, "name": "thin", "where": [{"column": "trade_count", "op": ">",
                                             "value": 20}]})
           + ', {"name": "cut", "where": [{"column": "book_yes')
    got, note = H.parse_reply(cut)
    assert [p["name"] for p in got] == ["tight book", "thin"]
    assert note == "reply cut off; 2 complete proposal(s) recovered"
    assert H.parse_reply("not json at all") == ([], "reply was not valid JSON")

    db = tmp_path / "x.db"
    observations_db(db)
    r = H.run(db, "SOL", url="x", ask=lambda *a, **k: cut)
    assert r["model_ok"] and r["proposed"] == 2 and "cut off" in r["model_note"]
    r = H.run(db, "SOL", url="x", ask=lambda *a, **k: '{"hypotheses": [{"na')
    assert not r["model_ok"] and r["model_error"] == "reply was not valid JSON"


def test_an_unreachable_model_still_runs_the_controls(tmp_path):
    db = tmp_path / "x.db"
    observations_db(db)

    def down(*a, **k):
        raise OSError("connection refused")

    r = H.run(db, "SOL", url="x", ask=down)
    assert r["model_ok"] is False and "connection refused" in r["model_error"]
    assert any(s["name"] == "wide book" for s in r["scored"])
    assert r["survivors"] == []


def test_too_little_history_is_said_not_guessed(tmp_path):
    db = tmp_path / "x.db"
    observations_db(db, n=30)
    r = H.run(db, "SOL", url="x", ask=lambda *a, **k: "{}")
    assert r["scored"] == [] and "too few to test" in r["skipped"]


def test_one_instrument_per_window_by_the_clock_each_done_before_entries():
    """Offsets counted from each instance's own finish collided: on the real
    schedule BTC and XRP would have called the model in the same second every
    6 hours (review N3). Slots are owned by the clock now."""
    from btc15_signal.config import Settings
    from btc15_signal.learning_runner import SLOT_ORDER, hypotheses_delay_s

    timeout = Settings.model_fields["learning_hypotheses_timeout_s"].default
    assert 10 + timeout < 900 - 660, "a call ends before entries open"
    day = 86_400_000
    start = 1_790_000_000_000
    # Whenever each instance finishes - every minute across a day, per asset,
    # each finishing at its own arbitrary time - the slots never meet.
    for step in range(0, day, 60_000):
        slots = {}
        for k, asset in enumerate(SLOT_ORDER + ("NEW",)):
            finished = start + step + k * 37_000
            at = finished + hypotheses_delay_s(asset, finished) * 1000
            assert round(at) % 900_000 == 10_000, "10s after a window opens"
            assert 0 < at - finished <= 8 * 900_000 + 10_000
            slots.setdefault(round(at) // 900_000 % 8, set()).add(asset)
        assert all(len(v) == 1 for v in slots.values()), slots
    # The case the review replayed: BTC and XRP finishing in the same window.
    t = 1_790_591_400_000 + 120_000
    btc = t + hypotheses_delay_s("BTC", t) * 1000
    xrp = t + 30_000 + hypotheses_delay_s("XRP", t + 30_000) * 1000
    assert abs(btc - xrp) >= 900_000


def test_a_carried_rule_is_judged_against_a_policy_that_can_act():
    """Review N1: `new` got its enable flags only after the loop, so a rule
    carried earlier acted in the trial but not in the baseline, and its effect
    was credited to the rule being judged - a -2.75 veto read +6.50."""
    a_ctx, b_ctx = CTX, OTHER.split("|")[0]
    current = policy({KEY: veto_arm(), OTHER: veto_arm()}, "old")
    new = policy({}, "new")
    rows = (holdout(wins=2, losses=13, context=a_ctx)      # A is worth keeping
            + holdout(wins=12, losses=3, context=b_ctx))   # B costs money
    for i, r in enumerate(rows):
        r["window_open"] = 1_790_000_000_000 + i * 900_000
    got = carry(new, current, {KEY: {"changes": 10, "incremental": 1.0}}, rows)
    assert KEY in got.carried and got.dropped == [OTHER], got.reasons


def test_a_refused_fit_still_watches_the_running_rules():
    """Review N2: when activation is refused the running policy stays live,
    and a rule it acts on must stay on the watch list."""
    result = SimpleNamespace(arms={}, report=SimpleNamespace(
        data_end_ms=0, training_cutoff_ms=0))
    fresh = policy({OTHER: veto_arm()}, "new")
    running = policy({KEY: veto_arm()}, "old")
    payload = learning.candidate_payload(
        result, fingerprint=FP, feature_definitions={}, feature_version=FV,
        now_ms=1, policy=fresh, also=running)
    assert sorted(c["context"] for c in payload["candidates"]) == sorted([KEY, OTHER])


def test_a_carried_rule_keeps_its_execution_and_takes_fresh_confidence():
    """Review N4: the whole old arm was copied, so a carried rule replaced the
    fresh fit's confidence for the cell - the label the operator reads."""
    current = policy({KEY: veto_arm(delta=-12, probability=0.60, n=226)}, "old")
    new = policy({KEY: {"n": 244, "mean": 0.01, "low": -0.02, "high": 0.04,
                        "action": NEUTRAL, "promoted": False, "delta": 0,
                        "probability": 0.64}}, "new")
    carry(new, current, {KEY: {"changes": 11, "incremental": 0.95}})
    arm = new.arms[KEY]
    assert (arm["action"], arm["promoted"], arm["low"]) == (VETO, True, -0.09)
    assert (arm["delta"], arm["probability"], arm["n"]) == (0, 0.64, 244)


def test_the_model_call_cannot_hold_a_crashing_service():
    """asyncio.run waits for the default executor with no timeout on Python
    3.11, so a crash mid-call held the process and service.lock until the
    call returned. The call runs on a daemon thread instead."""
    from btc15_signal.learning_runner import _in_daemon_thread

    seen = {}

    def work(x):
        seen["daemon"] = threading.current_thread().daemon
        return x * 2

    def boom():
        raise ValueError("model")

    async def go():
        assert await _in_daemon_thread(work, 21) == 42
        try:
            await _in_daemon_thread(boom)
        except ValueError as exc:
            return str(exc)

    assert asyncio.run(go()) == "model" and seen["daemon"] is True


def test_the_runner_runs_the_model_after_every_completed_run(tmp_path, monkeypatch):
    from btc15_signal import learning_runner

    monkeypatch.setattr(learning_runner, "hypotheses_delay_s", lambda *a: 0)

    runner = learning_runner.LearningRunner.__new__(learning_runner.LearningRunner)
    runner.settings = SimpleNamespace(
        learning_hypotheses_enabled=True, kalshi_series="KXSOL15M",
        database_path=str(tmp_path / "none.db"),
        learning_hypotheses_url="x", learning_hypotheses_model="m",
        learning_hypotheses_timeout_s=1)
    runner.policy_path = tmp_path / "intelligence_policy.json"
    runner.telegram = None
    runner.store = None
    runner._hypotheses_busy = False
    runner._hypotheses_task = None

    async def go():
        runner._spawn_hypotheses(0)
        assert runner._hypotheses_busy is True
        runner._spawn_hypotheses(0)          # one at a time
        await runner._hypotheses_task
        assert runner._hypotheses_busy is False

    asyncio.run(go())
    saved = json.loads((tmp_path / "hypotheses.json").read_text())
    assert saved["asset"] == "SOL" and "too few" in saved["skipped"]
    assert (tmp_path / "hypotheses_history.jsonl").exists()

    import inspect
    finish = inspect.getsource(learning_runner.LearningRunner._finish)
    assert "self._spawn_hypotheses(now_ms)" in finish


def test_the_survivor_message_is_labelled_and_says_candidates_only():
    result = {"proposed": 3, "windows": 266, "days": 9, "survivors": [
        {"name": "tight book", "terms": [("spread_bps", "<", 200)],
         "diff": 0.21, "n": 80, "days": 9, "p": 0.004},
        {"name": "wide book", "terms": [["spread_bps", ">", 300]],
         "diff": -0.084, "n": 90, "days": 9, "p": 0.008}]}
    text = messages.hypotheses_message(asset="SOL", result=result,
                                       series="KXSOL15M")
    assert text.startswith("<b>SOL</b>")
    assert "2 of 3 proposal(s) survived" in text and "spread_bps &lt; 200" in text
    assert "beats the rest by 0.210/contract" in text
    assert "trails the rest by 0.084/contract - one to avoid" in text
    assert "by -0" not in text
    assert "Candidates only" in text and "treat these as leads" in text
