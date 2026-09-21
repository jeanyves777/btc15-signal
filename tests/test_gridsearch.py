"""The fast matcher must never disagree with the deployed rule logic."""

from dataclasses import replace

from btc15_signal.features import build_snapshots
from btc15_signal.gridsearch import (
    GridIndex,
    build_primary_candidates,
    build_reversion_candidates,
    match_primary,
    match_reversion,
    primary_grid,
    reversion_grid,
)
from btc15_signal.strategy import EntryRule, ReversionRule
from btc15_signal.validation import (
    candidates_to_trades,
    primary_trades,
    reversion_trades,
)
from tests.test_features import contract, klines, market

WINDOW = 900_000


def sample_snapshots(count: int = 60):
    """A spread of windows: some drift up, some down, some spike and retrace."""
    snapshots = []
    for number in range(count):
        open_ms = 1_700_000_000_000 - 1_700_000_000_000 % WINDOW + number * WINDOW
        mode = number % 3
        if mode == 0:
            prices = [100.0 + minute * 0.05 for minute in range(15)]
        elif mode == 1:
            prices = [100.0 - minute * 0.04 for minute in range(15)]
        else:
            prices = [
                100.0 + min(minute, 5) * 0.2 - max(0, minute - 5) * 0.12
                for minute in range(15)
            ]
        snapshots.extend(
            build_snapshots(
                [market(ticker=f"T{number}", open_ms=open_ms, result="yes" if mode else "no")],
                klines(prices, open_ms=open_ms),
                {f"T{number}": contract(open_ms=open_ms)},
            )
        )
    return snapshots


def test_primary_matcher_agrees_with_the_deployed_entry_rule():
    snapshots = sample_snapshots()
    candidates = build_primary_candidates(snapshots)
    for rule in (
        EntryRule(remaining_minutes=5, min_raw_probability=0.5, min_ask=0.1, max_ask=0.99),
        EntryRule(remaining_minutes=7, min_raw_probability=0.0, min_ask=0.0, max_ask=1.0),
        EntryRule(
            remaining_minutes=5,
            min_raw_probability=0.0,
            min_ask=0.0,
            max_ask=1.0,
            require_momentum_alignment=True,
        ),
        EntryRule(
            remaining_minutes=5,
            min_raw_probability=0.0,
            min_ask=0.0,
            max_ask=1.0,
            min_normalized_distance=1.5,
        ),
    ):
        fast = candidates_to_trades(candidates, match_primary(candidates, rule))
        reference = primary_trades(snapshots, rule)
        assert {(t.ticker, t.entry_minute, t.side, t.entry_price) for t in fast} == {
            (t.ticker, t.entry_minute, t.side, t.entry_price) for t in reference
        }, rule


def test_reversion_matcher_agrees_with_the_deployed_reversion_rule():
    snapshots = sample_snapshots()
    candidates = build_reversion_candidates(snapshots)
    for rule in (
        ReversionRule(min_entry_price=0.0, max_entry_price=1.0, min_spike_bps=0.0),
        ReversionRule(
            min_remaining_minutes=8,
            max_remaining_minutes=12,
            min_entry_price=0.0,
            max_entry_price=1.0,
            min_spike_bps=2.0,
            max_distance_bps=50.0,
        ),
    ):
        fast = candidates_to_trades(candidates, match_reversion(candidates, rule))
        reference = reversion_trades(snapshots, rule)
        assert {(t.ticker, t.entry_minute, t.side) for t in fast} == {
            (t.ticker, t.entry_minute, t.side) for t in reference
        }, rule


def test_reversion_takes_at_most_one_entry_per_market():
    candidates = build_reversion_candidates(sample_snapshots())
    rule = ReversionRule(min_entry_price=0.0, max_entry_price=1.0, min_spike_bps=0.0)
    matched = match_reversion(candidates, rule)
    tickers = [candidates[index].ticker for index in matched]
    assert len(tickers) == len(set(tickers))


def test_bitmask_scoring_matches_a_direct_pnl_computation():
    candidates = build_primary_candidates(sample_snapshots())
    index = GridIndex("primary", candidates, min_trades=1)
    assert len(index) > 0
    direct = max(
        sum(candidates[i].pnl(index.take_profits[position]) for i in matched) / len(matched)
        for position in range(len(index))
        if (matched := index.rule_trades(position, 0, len(candidates)))
    )
    masks = index.outcome_masks(*index.realised())
    assert abs(index.best_mean_pnl_under(masks, min_trades=1) - max(direct, 0.0)) < 1e-9


def test_permuting_outcomes_changes_the_score_but_not_the_match_sets():
    candidates = build_primary_candidates(sample_snapshots())
    index = GridIndex("primary", candidates, min_trades=1)
    before = [index.rule_trades(p, 0, len(candidates)) for p in range(len(index))]
    exits, wons = index.realised()
    index.best_mean_pnl_under(index.outcome_masks(exits, [not w for w in wons]), min_trades=1)
    after = [index.rule_trades(p, 0, len(candidates)) for p in range(len(index))]
    assert before == after


def test_slice_bounds_never_split_a_market_across_folds():
    """Reversion dedupe is only sound if every snapshot of a market stays together."""
    candidates = build_reversion_candidates(sample_snapshots())
    index = GridIndex("reversion", candidates, min_trades=1)
    midpoint = candidates[len(candidates) // 2].open_ms
    low, high = index.slice_bounds(0, midpoint)
    left = {candidates[i].ticker for i in range(low, high)}
    right = {candidates[i].ticker for i in range(high, len(candidates))}
    assert not (left & right)


def test_grids_are_non_trivial():
    assert len(primary_grid()) > 500
    assert len(reversion_grid()) > 200


def test_candidate_pnl_is_payoff_minus_price():
    candidates = build_primary_candidates(sample_snapshots())
    item = candidates[0]
    assert item.pnl() == (1.0 if item.won else 0.0) - item.entry_price
    assert replace(item, won=not item.won).pnl() != item.pnl()


def test_take_profit_replaces_settlement_when_the_exit_would_have_filled():
    """The correction that matters: a 30c entry sold at 50c earns 20c, not 70c."""
    base = build_primary_candidates(sample_snapshots())[0]
    item = replace(base, entry_price=0.30, won=True, max_exit=0.62)
    assert item.pnl() == 0.70  # held to expiry
    assert item.took_profit(0.50)
    assert abs(item.pnl(0.50) - 0.20) < 1e-9

    never = replace(item, max_exit=0.41)
    assert not never.took_profit(0.50)
    assert never.pnl(0.50) == 0.70  # offer never filled, so settlement decides

    loser = replace(item, won=False, max_exit=0.62)
    assert abs(loser.pnl(0.50) - 0.20) < 1e-9  # sold before it collapsed
    assert loser.pnl() == -0.30
