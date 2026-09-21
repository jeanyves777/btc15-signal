"""The verdict gate must depend on gross edge and nothing else."""

from dataclasses import replace

from btc15_signal.intelligence import MIN_OOS_TRADES, verdict_for
from btc15_signal.validation import EMPTY, Trade, evaluate

SIGNIFICANT = {"p_value": 0.001, "permutations": 500}
INSIGNIFICANT = {"p_value": 0.42, "permutations": 500}
NO_TEST = {"p_value": None, "permutations": 0}


def result(trades: int, edge: float, ci_low: float):
    return replace(
        EMPTY, trades=trades, edge_per_contract=edge, edge_ci_low=ci_low, edge_ci_high=edge + 0.05
    )


def test_confirmed_needs_a_positive_lower_bound_and_a_surviving_permutation_test():
    assert verdict_for(result(500, 0.03, 0.01), SIGNIFICANT) == "GROSS_EDGE_CONFIRMED"


def test_a_search_that_explains_the_edge_downgrades_the_verdict():
    assert (
        verdict_for(result(500, 0.03, 0.01), INSIGNIFICANT) == "NOT_SIGNIFICANT_AFTER_SEARCH"
    )


def test_positive_edge_whose_interval_straddles_zero_is_inconclusive():
    assert verdict_for(result(500, 0.02, -0.01), SIGNIFICANT) == "POSITIVE_BUT_INCONCLUSIVE"


def test_negative_edge_fails_outright():
    assert verdict_for(result(500, -0.04, -0.07), SIGNIFICANT) == "NO_GROSS_EDGE"


def test_too_few_trades_is_reported_as_missing_data_not_as_a_failure():
    assert verdict_for(result(MIN_OOS_TRADES - 1, 0.05, 0.02), SIGNIFICANT) == "INSUFFICIENT_DATA"


def test_a_missing_permutation_test_fails_closed():
    """A rule picked from thousands is not confirmed by a test that never ran."""
    assert verdict_for(result(500, 0.03, 0.01), NO_TEST) == "UNTESTED_AGAINST_SEARCH"
    assert verdict_for(result(500, 0.03, 0.01), {}) == "UNTESTED_AGAINST_SEARCH"


def test_fees_can_never_change_a_verdict():
    """The whole point: a strategy is judged before costs, so fees cannot fail it."""
    trades = [
        Trade(
            ticker=f"T{index}",
            open_ms=index * 900_000,
            side="UP",
            entry_minute=10,
            entry_price=0.50,
            won=index % 100 < 62,
        )
        for index in range(600)
    ]
    free = evaluate(trades, fee=0.0)
    charged = evaluate(trades, fee=0.25)  # absurd fee, far beyond any real schedule
    assert verdict_for(free, SIGNIFICANT) == verdict_for(charged, SIGNIFICANT)
    assert charged.fee_roi < 0 < charged.gross_roi
