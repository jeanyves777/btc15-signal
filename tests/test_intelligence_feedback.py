"""The two things the live intelligence layer feeds itself on.

BOTH WERE WRONG IN THE SAME DIRECTION: a number that was always present, so
every surface looked healthy, and always meaningless.

1. `intelligence_decisions.realised_pnl` was a literal 0.0. The settlement
   loop passed a constant, so all 557 graded rows under the live policy
   carried 0.0 and not one of them meant it. Anything reading it scored every
   trade as break-even; `learning_data` had to route around it and recompute
   from broker fills.

2. `forward_scoreboard` pooled every candidate evaluation ever written,
   including rows produced under RETIRED feature definitions. A context key is
   only a label - `bd10-15 · px85-94` computed from brti-1 is not the same
   population as the identical string from brti-2 - so evidence from a dead
   feature set was gating promotion and withdrawal of live arms. That is the
   FINDINGS 49 class of mistake: a retired artefact still answering.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from btc15_signal.learning_store import LearningStore  # noqa: E402
from btc15_signal.store import Store  # noqa: E402
from btc15_signal.validation import kalshi_fee_charged  # noqa: E402

W = 1_790_193_600_000
NOW = 1_790_200_000_000


def decision(store, ticker, side, ask, window=W, rid=None):
    store.db.execute(
        "INSERT INTO intelligence_decisions (window_open, ticker, signal_id, "
        "decided_ms, side, ask, base_qualified, final_action, reason, "
        "confidence_delta, evidence_n, policy_version, feature_version) "
        "VALUES (?,?,?,?,?,?,1,'neutral','t',0,10,'p1','brti-2')",
        (window, ticker, rid or ticker, NOW - 1000, side, ask),
    )
    store.db.commit()


# ------------------------------------------- the money on a graded decision

def test_a_graded_decision_carries_a_real_figure(tmp_path):
    store = Store(str(tmp_path / "a.db"))
    decision(store, "T-UP", "UP", 0.80)
    store.grade_intelligence(W, "UP", None, NOW)
    row = store._dicts("SELECT won, realised_pnl FROM intelligence_decisions")[0]
    assert row["won"] == 1
    assert row["realised_pnl"] == round(1.0 - 0.80 - kalshi_fee_charged(0.80, 1), 6)
    assert row["realised_pnl"] != 0.0


def test_a_losing_decision_costs_what_it_paid(tmp_path):
    store = Store(str(tmp_path / "b.db"))
    decision(store, "T-DOWN", "DOWN", 0.80)
    store.grade_intelligence(W, "UP", None, NOW)
    row = store._dicts("SELECT won, realised_pnl FROM intelligence_decisions")[0]
    assert row["won"] == 0
    assert row["realised_pnl"] == round(-0.80 - kalshi_fee_charged(0.80, 1), 6)


def test_each_row_is_scored_on_its_own_side(tmp_path):
    """A market the model flipped inside has rows on both sides; one boolean
    for the market cannot be right for both."""
    store = Store(str(tmp_path / "c.db"))
    decision(store, "T", "UP", 0.80, rid="up")
    decision(store, "T", "DOWN", 0.20, rid="down")
    store.grade_intelligence(W, "UP", None, NOW)
    rows = {r["side"]: r for r in store._dicts(
        "SELECT side, won, realised_pnl FROM intelligence_decisions")}
    assert rows["UP"]["won"] == 1 and rows["UP"]["realised_pnl"] > 0
    assert rows["DOWN"]["won"] == 0 and rows["DOWN"]["realised_pnl"] < 0


def test_no_recorded_price_grades_null_not_zero(tmp_path):
    """"Unknown" and "break-even" are different claims."""
    store = Store(str(tmp_path / "d.db"))
    decision(store, "T", "UP", None)
    store.grade_intelligence(W, "UP", None, NOW)
    row = store._dicts("SELECT won, realised_pnl FROM intelligence_decisions")[0]
    assert row["won"] == 1
    assert row["realised_pnl"] is None


def test_an_explicit_figure_still_overrides(tmp_path):
    store = Store(str(tmp_path / "e.db"))
    decision(store, "T", "UP", 0.80)
    store.grade_intelligence(W, "UP", 1.2345, NOW)
    assert store._dicts(
        "SELECT realised_pnl FROM intelligence_decisions"
    )[0]["realised_pnl"] == 1.2345


def test_grading_is_not_repeated(tmp_path):
    store = Store(str(tmp_path / "f.db"))
    decision(store, "T", "UP", 0.80)
    store.grade_intelligence(W, "UP", None, NOW)
    first = store._dicts("SELECT realised_pnl, graded_ms FROM intelligence_decisions")[0]
    store.grade_intelligence(W, "DOWN", None, NOW + 5000)
    again = store._dicts("SELECT realised_pnl, graded_ms FROM intelligence_decisions")[0]
    assert again == first, "a graded row is never regraded"


def test_the_constant_zero_is_gone(tmp_path):
    """The exact defect: 557 rows, all 0.0, none of them meaning it."""
    store = Store(str(tmp_path / "g.db"))
    for i, (side, ask) in enumerate([("UP", 0.7), ("UP", 0.9), ("DOWN", 0.6)]):
        decision(store, f"T{i}", side, ask, window=W + i * 1000, rid=f"r{i}")
    for i in range(3):
        store.grade_intelligence(W + i * 1000, "UP", None, NOW)
    values = [r["realised_pnl"] for r in store._dicts(
        "SELECT realised_pnl FROM intelligence_decisions")]
    assert all(v is not None for v in values)
    assert len(set(values)) == 3, "three different prices, three different figures"


# ------------------------------- forward evidence, and the feature it is of

def evaluation(store, context, feature_version, change, baseline, candidate,
               window):
    store.db.execute(
        "INSERT INTO candidate_evaluations (window_open, ticker, decided_ms, "
        "candidate_id, candidate_version, context_key, proposed_action, "
        "baseline_qualified, would_change, side, ask, remaining_s, "
        "feature_version, won, baseline_pnl, candidate_pnl, graded_ms) "
        "VALUES (?,?,?,?,?,?, 'veto', 1, ?, 'UP', 0.8, 400, ?, 1, ?, ?, ?)",
        (window, f"T{window}", NOW, "c1", "v1", context, int(change),
         feature_version, baseline, candidate, NOW),
    )
    store.db.commit()


def board(store, version=""):
    ls = LearningStore.__new__(LearningStore)
    ls.db = store.db
    return ls.forward_scoreboard(version)


def test_evidence_from_a_retired_feature_set_is_excluded(tmp_path):
    store = Store(str(tmp_path / "h.db"))
    evaluation(store, "ctx-old", "brti-1", True, 0.5, 0.0, W)
    evaluation(store, "ctx-new", "brti-2", True, -0.5, 0.0, W + 1000)

    pooled = board(store)
    assert "ctx-old|accept" in pooled and "ctx-new|accept" in pooled

    current = board(store, "brti-2")
    assert "ctx-old|accept" not in current, "brti-1 evidence must not judge brti-2"
    assert "ctx-new|accept" in current


def test_the_same_context_label_is_not_pooled_across_versions(tmp_path):
    """The label is identical; the population is not."""
    store = Store(str(tmp_path / "i.db"))
    evaluation(store, "same", "brti-1", True, 1.0, 0.0, W)
    evaluation(store, "same", "brti-2", True, -1.0, 0.0, W + 1000)

    pooled = board(store)["same|accept"]
    assert pooled["changes"] == 2

    current = board(store, "brti-2")["same|accept"]
    assert current["changes"] == 1
    assert current["incremental"] == 1.0, "only the brti-2 row"


def test_an_unfiltered_call_still_returns_everything(tmp_path):
    """Callers that genuinely want every row keep the old behaviour."""
    store = Store(str(tmp_path / "j.db"))
    evaluation(store, "a", "brti-1", True, 0.5, 0.0, W)
    evaluation(store, "b", "brti-2", True, 0.5, 0.0, W + 1000)
    assert len(board(store)) == 2


def test_only_real_changes_count(tmp_path):
    """A candidate that agreed with the rule changed nothing."""
    store = Store(str(tmp_path / "k.db"))
    evaluation(store, "ctx", "brti-2", False, 0.5, 0.5, W)
    evaluation(store, "ctx", "brti-2", True, -0.5, 0.0, W + 1000)
    entry = board(store, "brti-2")["ctx|accept"]
    assert entry["changes"] == 1
    assert entry["incremental"] == 0.5


def test_the_runner_asks_for_the_running_contract(tmp_path):
    """Pinned: the gate must not silently go back to pooling."""
    import inspect

    from btc15_signal import learning_runner

    src = inspect.getsource(learning_runner)
    assert "forward_scoreboard(" in src
    assert "feature_contract.CONTRACT.version" in src
    assert "forward_scoreboard()" not in src, "an unfiltered call would pool"


# ------------------------------- joining a decision to the order it caused

def settle(store, window=W, winner="UP"):
    """Linking waits for settlement: before it, `filled` can still change."""
    store.grade_intelligence(window, winner, None, NOW)


def traded(store, window, order_id="ord-1"):
    store.db.execute(
        "INSERT INTO trade_proposals (id, strategy, window_open, ticker, side,"
        " entry_limit, take_profit, count, expires_at, close_ms, status,"
        " created_at, entry_order_id, fill_price) VALUES "
        "(?,'primary',?,?,'UP',0.8,0.99,2,0,0,'filled',0,?,0.79)",
        (f"p{window}", window, f"T{window}", order_id),
    )
    store.db.commit()


def test_a_traded_market_links_every_decision_row_to_its_order(tmp_path):
    """`order_id` and `filled` were declared and never written - NULL on all
    2,635 rows, so a query joining a decision to the order it caused got
    nothing and every executed trade looked simulated."""
    store = Store(str(tmp_path / "l1.db"))
    decision(store, "T", "UP", 0.80, rid="a")
    decision(store, "T", "UP", 0.81, rid="b")
    traded(store, W, "ord-xyz")
    settle(store)
    assert store.link_intelligence_orders() == 2
    rows = store._dicts("SELECT order_id, filled FROM intelligence_decisions")
    assert all(r["order_id"] == "ord-xyz" for r in rows)
    assert all(r["filled"] == 1 for r in rows)


def test_an_untraded_market_is_marked_not_filled(tmp_path):
    """Zero, not NULL. "We did not trade this" is a fact worth recording."""
    store = Store(str(tmp_path / "l2.db"))
    decision(store, "T", "UP", 0.80)
    settle(store)
    assert store.link_intelligence_orders() == 1
    row = store._dicts("SELECT order_id, filled FROM intelligence_decisions")[0]
    assert row["filled"] == 0
    assert row["order_id"] is None


def test_linking_is_idempotent(tmp_path):
    store = Store(str(tmp_path / "l3.db"))
    decision(store, "T", "UP", 0.80)
    traded(store, W)
    settle(store)
    assert store.link_intelligence_orders() == 1
    assert store.link_intelligence_orders() == 0, "already linked; stop"


def test_an_unfilled_proposal_does_not_count_as_traded(tmp_path):
    """`pending` is an intention, not an execution."""
    store = Store(str(tmp_path / "l4.db"))
    decision(store, "T", "UP", 0.80)
    store.db.execute(
        "INSERT INTO trade_proposals (id, strategy, window_open, ticker, side,"
        " entry_limit, take_profit, count, expires_at, close_ms, status,"
        " created_at, entry_order_id) VALUES "
        "('p','primary',?,?,'UP',0.8,0.99,2,0,0,'pending',0,'ord-1')",
        (W, "T"),
    )
    store.db.commit()
    settle(store)
    store.link_intelligence_orders()
    row = store._dicts("SELECT order_id, filled FROM intelligence_decisions")[0]
    assert row["filled"] == 0
    assert row["order_id"] is None


def test_each_window_links_to_its_own_order(tmp_path):
    store = Store(str(tmp_path / "l5.db"))
    decision(store, "T1", "UP", 0.80, window=W, rid="x")
    decision(store, "T2", "UP", 0.80, window=W + 900_000, rid="y")
    traded(store, W, "ord-first")
    traded(store, W + 900_000, "ord-second")
    settle(store, W)
    settle(store, W + 900_000)
    store.link_intelligence_orders()
    got = {r["ticker"]: r["order_id"] for r in store._dicts(
        "SELECT ticker, order_id FROM intelligence_decisions")}
    assert got == {"T1": "ord-first", "T2": "ord-second"}


def test_a_window_is_not_linked_before_it_settles(tmp_path):
    """The bug this caused: the linker ran on the 60-second sync BEFORE the
    order filled, wrote `filled = 0`, and never looked again because it only
    revisited NULLs. Two live windows carried a false 0 within hours."""
    store = Store(str(tmp_path / "l6.db"))
    decision(store, "T", "UP", 0.80)
    assert store.link_intelligence_orders() == 0, "ungraded: leave it alone"
    assert store._dicts(
        "SELECT filled FROM intelligence_decisions")[0]["filled"] is None


def test_a_false_zero_is_repaired(tmp_path):
    """Self-healing, so the two rows already wrong did not need a migration."""
    store = Store(str(tmp_path / "l7.db"))
    decision(store, "T", "UP", 0.80)
    settle(store)
    store.db.execute("UPDATE intelligence_decisions SET filled = 0")
    store.db.commit()
    traded(store, W, "ord-late")
    assert store.link_intelligence_orders() == 1
    row = store._dicts("SELECT order_id, filled FROM intelligence_decisions")[0]
    assert row["filled"] == 1
    assert row["order_id"] == "ord-late"
    assert store.link_intelligence_orders() == 0
