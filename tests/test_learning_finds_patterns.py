"""Can the learner actually FIND a winning pattern? Not just refuse bad ones.

THE GAP THIS CLOSES, identified by the operator 2026-09-25. There are 85 tests
in `test_continuous_learning.py` and every one of them tests REFUSAL - "the
promotion bar is not cleared by a thin cell", "a policy that promotes nothing
is still a valid usable policy". None plants a pattern and proves the learner
finds it.

That asymmetry matters more than a missing case, because **a learner hard-wired
to return "nothing promoted" would pass the entire existing suite**. Across
five live instruments, zero arms have ever been promoted. With only refusal
tests, "there is genuinely nothing there" and "it cannot find anything" are
indistinguishable - and the second would look exactly like discipline.

So these tests plant an edge that is unmistakably real, and fail if it is
missed. They are the counterweight to every test that checks the bar holds:
together they say the bar is in the right place, rather than merely high.

CONSTRUCTION. Rows are shaped as `learning.train` consumes them, split
chronologically at `TRAIN_FRACTION`, so a planted edge must survive the same
validation slice a real one does. The edge is placed in ONE context cell and
absent from the others, which is also the discrimination test: finding an edge
everywhere would be as wrong as finding it nowhere.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from btc15_signal import feature_contract, learning  # noqa: E402
from btc15_signal import feature_contract  # noqa: E402

FP = feature_contract.FINGERPRINT
DEFS = feature_contract.CONTRACT.payload()
VERSION = feature_contract.CONTRACT.version
BASE_MS = 1_790_000_000_000
# `context_key_of` appends the LEG to whatever context the row carries, so a
# row keyed "rich|accept" is stored as "rich|accept|accept". Asserting on the
# raw context would silently never match.
KEY = "rich|accept|accept"
DULL = "dull|accept|accept"


def row(i, context, won, ask=0.80, taken=True):
    """One graded decision, in the shape the fitter reads."""
    return {
        "window_open": BASE_MS + i * 900_000,
        "created_at": BASE_MS + i * 900_000,
        "ticker": f"KXBTC15M-T{i}",
        "side": "UP",
        # `our_ask` is the field the reward function reads; `fill_kind`
        # "actual" makes it use the realised figure, which is how a traded
        # row is priced.
        "our_ask": ask,
        "context_key": context,
        "rule_match": 1 if taken else 0,
        "won": 1 if won else 0,
        "fill_kind": "actual",
        "realised_pnl": (1 - ask) if won else -ask,
        "fee": 0.0,
        "feature_version": VERSION,
        "features_ok": 1,
        "graded_ms": BASE_MS + i * 900_000 + 900_000,
    }


def corpus(good_context, good_rate, dull_rate, n_each=400):
    """Two cells: one with a planted edge, one deliberately fairly priced."""
    rows = []
    for i in range(n_each):
        # The planted cell wins far more often than its 0.80 price implies.
        rows.append(row(i * 2, good_context, won=(i % 100) < good_rate))
        # The control cell wins exactly at its price - no edge to find.
        rows.append(row(i * 2 + 1, "dull|accept", won=(i % 100) < dull_rate))
    rows.sort(key=lambda r: r["window_open"])
    return rows


def train(rows):
    return learning.train(rows, fingerprint=FP, feature_definitions=DEFS,
                          feature_version=VERSION, now_ms=BASE_MS + 10**9)


# ------------------------------------------------- it must FIND a real edge

def test_a_planted_edge_is_actually_found():
    """The headline. A cell winning 95% at a 0.80 price is worth +0.15 a
    contract; if the learner cannot see that, it cannot see anything."""
    result = train(corpus("rich|accept", good_rate=95, dull_rate=80))
    assert result.ok, result.error
    arms = result.policy.arms
    assert KEY in arms, f"the planted cell is absent: {list(arms)}"
    rich = arms[KEY]
    assert rich.get("n", 0) >= learning.MIN_PROMOTION_N
    assert float(rich.get("mean", 0)) > 0.05, rich


def test_the_edge_is_found_in_the_RIGHT_cell_only():
    """Finding an edge everywhere would be as useless as finding it nowhere.
    The control cell is priced fairly and must not read as an opportunity."""
    result = train(corpus("rich|accept", good_rate=95, dull_rate=80))
    rich = float(result.policy.arms[KEY].get("mean", 0))
    dull = float(result.policy.arms.get(DULL, {}).get("mean", 0))
    assert rich > dull + 0.05, f"rich {rich:+.4f} vs dull {dull:+.4f}"


def test_the_measured_size_is_about_right():
    """95% at 0.80 is +0.15/contract. A learner that finds the cell but
    mis-sizes it by an order of magnitude cannot be acted on."""
    result = train(corpus("rich|accept", good_rate=95, dull_rate=80))
    mean = float(result.policy.arms[KEY]["mean"])
    assert 0.05 < mean < 0.30, mean


def test_a_negative_cell_is_found_as_negative():
    """The other direction. A cell winning 55% at 0.80 loses 0.25 a contract
    and must be seen as harmful, not merely as weak."""
    result = train(corpus("poor|accept", good_rate=55, dull_rate=80))
    poor = float(result.policy.arms["poor|accept|accept"]["mean"])
    assert poor < -0.05, poor


# ------------------------------- and it must still refuse what is not there

def test_no_edge_is_invented_when_none_exists():
    """Both cells priced fairly. Nothing should read as a large opportunity -
    this is the guard that the tests above have not simply lowered the bar."""
    result = train(corpus("flat|accept", good_rate=80, dull_rate=80))
    for key, arm in result.policy.arms.items():
        assert abs(float(arm.get("mean", 0))) < 0.05, (key, arm.get("mean"))


def test_a_thin_cell_is_not_promoted_however_good_it_looks():
    """A 100%-winning cell with too few rows must not clear the bar - the
    planted-edge tests must not have made the learner credulous."""
    rows = corpus("rich|accept", good_rate=95, dull_rate=80)
    rows += [row(99_000 + i, "tiny|accept", won=True) for i in range(8)]
    result = train(rows)
    tiny = result.policy.arms.get("tiny|accept|accept")
    if tiny is not None:
        assert not tiny.get("promoted"), tiny
        assert int(tiny.get("n", 0)) < learning.MIN_PROMOTION_N


# ------------------------------------------- the report says what it found

def test_the_report_counts_what_was_examined():
    """A run that says nothing about what it looked at cannot be audited."""
    result = train(corpus("rich|accept", good_rate=95, dull_rate=80))
    assert result.report.arms_fitted >= 2
    assert result.report.rows > 0


def test_a_learner_that_found_nothing_would_fail_these():
    """Stated explicitly, because it is the whole point: the existing suite
    is satisfied by a learner that always returns an empty policy. This one
    is not."""
    empty = learning.Policy()
    assert not empty.arms
    result = train(corpus("rich|accept", good_rate=95, dull_rate=80))
    assert result.policy.arms, "an empty policy must not pass"
    assert result.policy.arms.keys() != empty.arms.keys()
