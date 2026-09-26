"""A market is classified by its whole life, never by its first evaluation.

THE DEFECT THESE PIN, 2026-09-25. `predictions.qualified` is a snapshot taken
when the alert was written. Read as the market's verdict it said BTC had
qualified 4 signals and declined 52 that went on to score +7.7% - which reads
as a gate selecting the wrong setups, and was wrong. Nine of those "declined"
markets had been TRADED, 9W/0L at a 0.829 price, and BTC had filled 13 markets
rather than 4. Classified by whether a market was ever eligible the same data
says the opposite: +8.5% eligible against +4.4% never eligible.

The trading path was never at fault. Evaluation continues to the entry
deadline - a median of 27 polls per market - and of 55 BTC markets that failed
the distance check, evaluation continued past it in 55 and 11 later became
eligible, every one of which was filled. A failed gate rejects THAT MOMENT.

So these tests are about the reporting contract, and the one that matters most
is `test_a_market_declined_then_traded_is_never_never_eligible`: that is the
exact mistake, in the exact shape it occurred.

They also pin the separations that make an honest report possible - execution
apart from eligibility, because an instance with auto-execution off is a
SETTING and not a strategy failure; and pending apart from no-record, because a
vocabulary that cannot say "not settled yet" hides gaps instead of reporting
them.
"""

import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from btc15_signal import market_lifecycle as lc  # noqa: E402

TICKER = "KXBTC15M-26SEP250100-00"
# A proposal is only "waiting" while it is still inside its window.
FUTURE = int(time.time() * 1000) + 3_600_000
PAST = int(time.time() * 1000) - 3_600_000


def make(tmp_path, decisions, proposals=(), predictions=(),
         auto_enabled=None):
    """A minimal database shaped like the live one."""
    path = tmp_path / "t.db"
    db = sqlite3.connect(str(path))
    db.execute("""CREATE TABLE intelligence_decisions (
        ticker TEXT, decided_ms INTEGER, remaining_s INTEGER,
        base_qualified INTEGER, failed_gates TEXT, ask REAL)""")
    db.executemany("INSERT INTO intelligence_decisions VALUES (?,?,?,?,?,?)",
                   decisions)
    db.execute("""CREATE TABLE trade_proposals (
        ticker TEXT, status TEXT, fill_price REAL, count REAL,
        entry_order_id TEXT, result_note TEXT, expires_at INTEGER)""")
    db.executemany("INSERT INTO trade_proposals VALUES (?,?,?,?,?,?,?)",
                   [tuple(x) + (None,) * (7 - len(x)) for x in proposals])
    db.execute("""CREATE TABLE predictions (
        contract_ticker TEXT, won INTEGER, contract_price REAL,
        qualified INTEGER)""")
    db.executemany("INSERT INTO predictions VALUES (?,?,?,?)", predictions)
    db.execute("CREATE TABLE settings (key TEXT PRIMARY KEY, value REAL)")
    if auto_enabled is not None:
        db.execute("INSERT INTO settings VALUES ('auto_trade_enabled', ?)",
                   (1.0 if auto_enabled else 0.0,))
    db.commit()
    db.close()
    return str(path)


DECLINED_THEN_ELIGIBLE = [
    (TICKER, 1000, 700, 0, "BRTI distance", 0.80),
    (TICKER, 2000, 640, 0, "BRTI distance", 0.81),
    (TICKER, 3000, 580, 1, "", 0.83),      # became eligible here
    (TICKER, 4000, 520, 1, "", 0.84),
]


# ------------------------------------------------- the exact mistake, pinned

def test_a_market_declined_then_traded_is_never_never_eligible(tmp_path):
    """THE regression. Declined at the alert, eligible later, filled - it must
    never land in the bucket that says the rule refused it."""
    path = make(
        tmp_path, DECLINED_THEN_ELIGIBLE,
        proposals=[(TICKER, "filled", 0.83, 1.0, "oid-1", "")],
        predictions=[(TICKER, 1, 0.83, 0)],   # qualified_at_alert = False
    )
    life = lc.build(path)[TICKER]
    assert life.eligibility == lc.BECAME_ELIGIBLE
    assert life.ever_eligible
    assert life.traded
    assert life.qualified_at_alert is False, "the snapshot must be preserved"


def test_the_snapshot_is_kept_and_not_used_for_classification(tmp_path):
    """`qualified_at_alert` is a real fact about the alert. The defect was
    reading it as a fact about the market, so both must coexist."""
    path = make(tmp_path, DECLINED_THEN_ELIGIBLE,
                predictions=[(TICKER, 1, 0.83, 0)])
    life = lc.build(path)[TICKER]
    assert life.qualified_at_alert is False
    assert life.eligibility != lc.NEVER_ELIGIBLE


def test_a_market_that_never_passes_is_never_eligible(tmp_path):
    path = make(tmp_path, [
        (TICKER, 1000, 700, 0, "BRTI distance", 0.80),
        (TICKER, 2000, 600, 0, "BRTI distance", 0.81),
    ])
    assert lc.build(path)[TICKER].eligibility == lc.NEVER_ELIGIBLE


def test_eligible_at_the_first_poll_is_distinguished(tmp_path):
    path = make(tmp_path, [(TICKER, 1000, 700, 1, "", 0.80)])
    assert lc.build(path)[TICKER].eligibility == lc.ELIGIBLE_AT_FIRST


# --------------------------------------------- when eligibility began

def test_first_eligibility_time_and_transitions_are_recorded(tmp_path):
    path = make(tmp_path, DECLINED_THEN_ELIGIBLE)
    life = lc.build(path)[TICKER]
    assert life.first_eligible_ms == 3000
    assert life.first_eligible_remaining_s == 580
    assert life.eligible_evaluations == 2
    assert life.transitions == [(1000, False), (3000, True)]


def test_a_market_that_flips_back_records_both_transitions(tmp_path):
    """"Eligible once at second 612" and "eligible for nine minutes" are
    different facts and a single flag cannot tell them apart."""
    path = make(tmp_path, [
        (TICKER, 1000, 700, 0, "BRTI distance", 0.80),
        (TICKER, 2000, 640, 1, "", 0.82),
        (TICKER, 3000, 580, 0, "BRTI distance", 0.79),
    ])
    life = lc.build(path)[TICKER]
    assert [ok for _ms, ok in life.transitions] == [False, True, False]
    assert life.eligibility == lc.BECAME_ELIGIBLE


# ------------------------- execution is separate from eligibility

def test_automation_off_is_auto_disabled_not_awaiting_approval(tmp_path):
    """A proposal still inside its window on an instance recorded as having
    automation off. It is NOT "awaiting authorisation" - that would invent an
    outstanding request someone might go looking for - and it is not a
    strategy refusal either. The state names the setting; it does not claim
    the setting caused anything."""
    path = make(tmp_path, DECLINED_THEN_ELIGIBLE,
                proposals=[(TICKER, "pending", None, 1.0, None, "", FUTURE)],
                auto_enabled=False)
    life = lc.build(path)[TICKER]
    assert life.ever_eligible
    assert life.execution == lc.AUTO_DISABLED
    assert "at snapshot" in life.execution_reason
    assert life.execution != lc.AWAITING_AUTHORIZATION
    assert not life.traded


def test_a_live_proposal_with_automation_ON_is_not_called_awaiting(tmp_path):
    """Automation on and the order not yet sent is NOT an outstanding
    approval. Only a recorded, unexpired request could make it one."""
    path = make(tmp_path, DECLINED_THEN_ELIGIBLE,
                proposals=[(TICKER, "pending", None, 1.0, None, "", FUTURE)],
                auto_enabled=True)
    life = lc.build(path)[TICKER]
    # NOT awaiting_authorization: no approval request is recorded anywhere,
    # so that status cannot be evidenced.
    assert life.execution != lc.AWAITING_AUTHORIZATION
    assert life.authorization == lc.NOT_RECORDED


def test_the_two_waiting_states_are_never_the_same_value(tmp_path):
    assert lc.AUTO_DISABLED != lc.AWAITING_AUTHORIZATION


def test_an_unfilled_order_is_not_a_refusal_either(tmp_path):
    path = make(tmp_path, DECLINED_THEN_ELIGIBLE,
                proposals=[(TICKER, "unfilled", None, 1.0, "oid-2",
                            "book moved before the order landed")])
    life = lc.build(path)[TICKER]
    assert life.execution == lc.SUBMITTED_UNFILLED
    assert "book moved" in life.execution_reason
    assert life.order_ids == ["oid-2"]


def test_a_blocked_proposal_carries_its_reason(tmp_path):
    path = make(tmp_path, DECLINED_THEN_ELIGIBLE,
                proposals=[(TICKER, "rejected", None, 1.0, None,
                            "daily loss floor")])
    life = lc.build(path)[TICKER]
    assert life.execution == lc.BLOCKED
    assert "daily loss floor" in life.execution_reason


def test_an_exited_position_still_counts_as_traded(tmp_path):
    """Filled and then sold early is a fill."""
    path = make(tmp_path, DECLINED_THEN_ELIGIBLE,
                proposals=[(TICKER, "exited", 0.83, 1.0, "oid-3", "")])
    assert lc.build(path)[TICKER].traded


# ------------------------------- pending is not the same as missing

def test_pending_and_missing_outcomes_are_distinguished(tmp_path):
    """A vocabulary that cannot say "not settled yet" hides a gap rather than
    reporting it - which is how 16 eligible ETH markets came out as 15."""
    other = "KXBTC15M-26SEP250115-15"
    path = make(
        tmp_path,
        DECLINED_THEN_ELIGIBLE + [(other, 1000, 700, 1, "", 0.80)],
        predictions=[(TICKER, None, 0.83, 1)],   # row exists, not settled
    )
    lives = lc.build(path)
    assert lives[TICKER].outcome == lc.PENDING
    assert lives[other].outcome == lc.NO_RECORD


def test_the_decision_time_anchors_the_window(tmp_path):
    """Filtering eligibility on decision time and outcomes on window open is
    what produced the 16-against-15 mismatch. `build` uses decided_ms only."""
    path = make(tmp_path, DECLINED_THEN_ELIGIBLE)
    assert lc.build(path, since_ms=3500)[TICKER].evaluations == 1
    assert lc.build(path, since_ms=0)[TICKER].evaluations == 4


def test_training_labels_on_the_sequence_not_the_alert_snapshot():
    """The reporting was wrong; the TRAINING labels were not, and that
    distinction must not be lost. `learning_data` upgrades a market when a
    later poll qualifies, which is the became-eligible semantics."""
    source = (Path(__file__).resolve().parents[1] / "src" / "btc15_signal"
              / "learning_data.py").read_text(encoding="utf-8")
    assert 'not bool(existing.get("base_qualified")) and qualified' in source
    assert "predictions.qualified" not in source


def test_a_lapsed_proposal_is_expired_not_waiting(tmp_path):
    """Every resting proposal on all three live instances is past its expiry -
    328, 72 and 38, none with an order id. Calling those "awaiting
    authorisation" points somebody at approvals that do not exist."""
    path = make(tmp_path, DECLINED_THEN_ELIGIBLE,
                proposals=[(TICKER, "pending", None, 1.0, None, "", PAST)],
                auto_enabled=True)
    life = lc.build(path)[TICKER]
    assert life.execution == lc.EXPIRED
    # Automation was on, so nothing in the record explains the lapse. The
    # report says so rather than naming a cause it cannot evidence.
    assert life.execution_reason == lc.REASON_UNKNOWN


def test_an_expired_proposal_on_an_auto_off_instance_says_why(tmp_path):
    """Same state, different reason - and this reason IS evidenced: with
    automation off, nothing was ever going to place the order."""
    path = make(tmp_path, DECLINED_THEN_ELIGIBLE,
                proposals=[(TICKER, "pending", None, 1.0, None, "", PAST)],
                auto_enabled=False)
    life = lc.build(path)[TICKER]
    assert life.execution == lc.EXPIRED
    # The setting is reported in its own field. It is NOT offered as the
    # cause of the lapse: "automation was off" and "nothing was submitted"
    # are two observations, and no recorded blocker links them.
    assert life.execution_reason == lc.REASON_UNKNOWN
    assert life.automation_on_at_snapshot is False


def test_an_unexplained_lapse_says_reason_unknown(tmp_path):
    """Automation ON and the proposal lapsed. Nothing in the archive explains
    why, and there is no approval-request record to point at, so the report
    must say it does not know rather than invent an absent approver."""
    path = make(tmp_path, DECLINED_THEN_ELIGIBLE,
                proposals=[(TICKER, "pending", None, 1.0, None, "", PAST)],
                auto_enabled=True)
    life = lc.build(path)[TICKER]
    assert life.execution == lc.EXPIRED
    assert life.execution_reason == lc.REASON_UNKNOWN
    assert life.automation_on_at_snapshot is True


def test_automation_on_never_implies_an_approval_was_requested(tmp_path):
    """Automation being on means approval was not REQUIRED; it is not
    evidence one was asked for. And the absence of a record does not establish
    the absence of the event - without instrumentation, `not_recorded` is the
    strongest claim available in either direction."""
    path = make(tmp_path, DECLINED_THEN_ELIGIBLE,
                proposals=[(TICKER, "pending", None, 1.0, None, "", FUTURE)],
                auto_enabled=True)
    life = lc.build(path)[TICKER]
    assert life.authorization == lc.NOT_RECORDED
    assert life.execution != lc.AWAITING_AUTHORIZATION


def test_the_four_facts_are_separate_fields():
    """Status, automation setting, authorization and reason are distinct.
    Collapsing any two reintroduces the ambiguity this module removes."""
    life = lc.MarketLife(ticker="X")
    for field in ("execution", "automation_on_at_snapshot", "authorization",
                  "execution_reason"):
        assert hasattr(life, field)
    assert life.authorization == lc.NOT_RECORDED


def test_expired_is_not_traded():
    assert lc.EXPIRED not in (lc.FILLED, lc.PARTIALLY_FILLED)


def test_proposal_expiry_does_not_terminate_market_evaluation(tmp_path):
    """A lapsed proposal ends that ATTEMPT, not the market. Live: of 160 BTC,
    74 ETH and 31 GOLD markets whose entry window stayed open past a proposal
    expiry, evaluation continued in every one - and 52, 17 and 15 of them
    became eligible AFTER the expiry."""
    path = make(
        tmp_path,
        [(TICKER, 1000, 700, 0, "BRTI distance", 0.80),
         (TICKER, 2000, 640, 0, "BRTI distance", 0.81),
         # a proposal expired around here
         (TICKER, 3000, 580, 1, "", 0.83),
         (TICKER, 4000, 520, 1, "", 0.84)],
        proposals=[(TICKER, "pending", None, 1.0, None, "", PAST)],
        auto_enabled=False,
    )
    life = lc.build(path)[TICKER]
    assert life.evaluations == 4, "evaluation must not stop at the expiry"
    assert life.eligibility == lc.BECAME_ELIGIBLE
    assert life.first_eligible_ms == 3000


def test_no_automation_row_is_not_recorded_rather_than_off(tmp_path):
    """Gold carries no `auto_trade_enabled` row at all. Reading that as False
    turns an absence of record into a recorded setting."""
    path = make(tmp_path, DECLINED_THEN_ELIGIBLE,
                proposals=[(TICKER, "pending", None, 1.0, None, "", PAST)],
                auto_enabled=None)
    life = lc.build(path)[TICKER]
    assert life.automation_on_at_snapshot is None


def test_the_lapse_reason_is_never_attributed_to_the_setting(tmp_path):
    """Automation off and nothing submitted are two observed facts. Joining
    them with "so" is a causal claim no recorded blocker supports."""
    for enabled in (True, False, None):
        sub = tmp_path / f"a{enabled}"
        sub.mkdir()
        path = make(sub, DECLINED_THEN_ELIGIBLE,
                    proposals=[(TICKER, "pending", None, 1.0, None, "", PAST)],
                    auto_enabled=enabled)
        reason = lc.build(path)[TICKER].execution_reason
        assert "automation" not in reason.lower(), reason
        assert reason == lc.REASON_UNKNOWN


def test_a_recorded_note_IS_used_as_the_reason(tmp_path):
    """Evidence, when it exists, is reported. The rule is not "say nothing",
    it is "say only what a record supports"."""
    path = make(tmp_path, DECLINED_THEN_ELIGIBLE,
                proposals=[(TICKER, "pending", None, 1.0, None,
                            "daily loss floor reached", PAST)],
                auto_enabled=True)
    assert lc.build(path)[TICKER].execution_reason == "daily loss floor reached"
