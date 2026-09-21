import random
from dataclasses import replace

from btc15_signal.validation import (
    Trade,
    benjamini_hochberg,
    block_bootstrap,
    evaluate,
    kalshi_fee,
    regime_breakdown,
    wilson_lower,
)


def trade(price: float, won: bool, open_ms: int = 0, **regimes) -> Trade:
    return Trade(
        ticker=f"T{open_ms}",
        open_ms=open_ms,
        side=regimes.pop("side", "UP"),
        entry_minute=10,
        entry_price=price,
        won=won,
        **regimes,
    )


def test_edge_is_outcome_minus_price_so_a_fair_price_scores_zero():
    """Buying at the true probability must score zero edge, not a positive win rate."""
    trades = [trade(0.70, won=index < 70, open_ms=index * 900_000) for index in range(100)]
    result = evaluate(trades, bootstrap=0)
    assert result.win_rate == 0.70
    assert abs(result.edge_per_contract) < 1e-9
    assert abs(result.gross_roi) < 1e-9


def test_high_win_rate_at_a_high_price_is_still_a_losing_strategy():
    """An 84% win rate bought at 90c loses money. Win rate alone cannot validate."""
    trades = [trade(0.90, won=index < 84, open_ms=index * 900_000) for index in range(100)]
    result = evaluate(trades, bootstrap=0)
    assert result.win_rate == 0.84
    assert result.edge_per_contract < 0
    assert result.gross_roi < 0


def test_genuine_edge_is_positive_and_scales_with_mispricing():
    trades = [trade(0.50, won=index < 60, open_ms=index * 900_000) for index in range(100)]
    result = evaluate(trades, bootstrap=0)
    assert round(result.edge_per_contract, 4) == 0.10
    assert round(result.gross_roi, 4) == 0.20


def test_fee_fields_never_affect_gross_fields():
    trades = [trade(0.50, won=index < 60, open_ms=index * 900_000) for index in range(100)]
    free = evaluate(trades, fee=0.0, bootstrap=0)
    charged = evaluate(trades, fee=0.02, bootstrap=0)
    assert free.gross_pnl == charged.gross_pnl
    assert free.edge_per_contract == charged.edge_per_contract
    assert charged.fee_pnl < free.fee_pnl
    assert round(free.fee_pnl - charged.fee_pnl, 4) == 2.0  # 100 trades x 2c


def test_kalshi_fee_peaks_at_the_midpoint():
    assert kalshi_fee(0.50) >= kalshi_fee(0.30)
    assert kalshi_fee(0.50) >= kalshi_fee(0.90)
    assert kalshi_fee(0.50) == 0.02


def test_block_bootstrap_interval_brackets_the_mean():
    rng = random.Random(1)
    values = [rng.gauss(0.05, 0.4) for _ in range(400)]
    costs = [0.5] * 400
    (low, high), _ = block_bootstrap(values, costs, samples=400, seed=3)
    mean = sum(values) / len(values)
    assert low < mean < high


def test_block_bootstrap_is_deterministic_for_a_fixed_seed():
    values = [0.1, -0.2, 0.3, -0.1] * 25
    costs = [0.5] * 100
    first = block_bootstrap(values, costs, samples=200, seed=11)
    second = block_bootstrap(values, costs, samples=200, seed=11)
    assert first == second


def test_wilson_lower_bound_is_below_the_point_estimate():
    assert 0 < wilson_lower(84, 100) < 0.84
    assert wilson_lower(0, 0) == 0.0


def test_benjamini_hochberg_rejects_noise_but_keeps_a_strong_signal():
    keep = benjamini_hochberg([0.0001, 0.40, 0.55, 0.62, 0.80], alpha=0.10)
    assert keep[0] is True
    assert not any(keep[1:])


def test_benjamini_hochberg_discards_a_lone_marginal_p_among_many():
    """One p=0.04 among 20 tests is what noise looks like, and must not survive."""
    keep = benjamini_hochberg([0.04] + [0.5] * 19, alpha=0.10)
    assert not any(keep)


def test_regime_breakdown_labels_small_or_noisy_buckets_as_noise():
    trades = [
        trade(0.50, won=index % 2 == 0, open_ms=index * 900_000, session="us")
        for index in range(60)
    ] + [
        trade(0.50, won=index % 2 == 0, open_ms=(index + 60) * 900_000, session="asia")
        for index in range(60)
    ]
    rows = regime_breakdown(trades, dimensions=("session",))
    assert {row["bucket"] for row in rows} == {"us", "asia"}
    assert all(row["verdict"] == "noise" for row in rows)


def test_regime_breakdown_skips_buckets_below_the_minimum_sample():
    trades = [trade(0.5, won=True, open_ms=i * 900_000, session="us") for i in range(5)]
    assert regime_breakdown(trades, dimensions=("session",)) == []


def test_kalshi_fee_matches_a_real_fill():
    """An observed $0.18 fee on a $10 stake pins the rate at 7%.

    Spending S dollars at price P buys S/P contracts, so the fee collapses to
    0.07 * S * (1 - P) - independent of P except through (1 - P). At S = $10
    that is $0.18 for P in [0.743, 0.757], i.e. a fill around 75c.
    """
    from btc15_signal.validation import kalshi_fee

    assert kalshi_fee(0.75, 10 / 0.75) == 0.18
    assert kalshi_fee(0.90, 10 / 0.90) == 0.07
    assert kalshi_fee(0.50, 10 / 0.50) == 0.35
    # the collapsed form
    for price in (0.55, 0.75, 0.90, 0.96):
        assert abs(0.07 * (10 / price) * price * (1 - price) - 0.70 * (1 - price)) < 1e-12


def test_fee_rounding_is_per_order_not_per_contract():
    """Ceiling each contract to a cent overstates the cost several-fold."""
    from btc15_signal.validation import kalshi_fee, kalshi_fee_rate

    per_order = kalshi_fee(0.96, 100) / 100
    assert round(kalshi_fee_rate(0.96), 5) == 0.00269
    assert per_order < 0.004
    assert kalshi_fee(0.96, 1) == 0.01  # the single-contract order really does pay a cent


def test_an_early_exit_pays_two_fees_but_settlement_pays_one():
    trades = [
        trade(0.50, won=True, open_ms=i * 900_000) for i in range(50)
    ]
    held = evaluate(trades, fee=0.01, bootstrap=0)
    exited = evaluate(
        [replace(t, took_profit=True) for t in trades], fee=0.01, bootstrap=0
    )
    assert round(held.gross_pnl - held.fee_pnl, 4) == 0.50   # 50 trades x 1c
    assert round(exited.gross_pnl - exited.fee_pnl, 4) == 1.00  # x2 executions


def test_the_charged_fee_matches_real_kalshi_fills():
    """Two live fills pin the rounding: ceil at four decimal places.

    1 contract at 84c was debited $0.0095 against a raw 0.9408c, and a $10
    stake at 74c was charged 18c against a raw 18.1818c. Cent-flooring gives
    $0.00 for the first, cent-ceiling gives $0.01 - only 4dp ceiling fits both.
    """
    from btc15_signal.validation import kalshi_fee_charged, kalshi_fee_observed

    assert kalshi_fee_charged(0.84, 1) == 0.0095
    assert round(kalshi_fee_charged(0.74, 13.5), 4) == 0.1819

    # At one contract the difference is the whole fee, not a rounding detail.
    assert kalshi_fee_observed(0.84, 1) == 0.0
    assert kalshi_fee_charged(0.84, 1) > 0


def test_the_charged_fee_is_never_below_the_raw_formula():
    from btc15_signal.validation import FEE_RATE, kalshi_fee_charged

    for price in (0.05, 0.5, 0.84, 0.9, 0.95, 0.99):
        for count in (1, 2, 13.5, 100):
            raw = FEE_RATE * count * price * (1 - price)
            charged = kalshi_fee_charged(price, count)
            assert charged >= raw - 1e-12
            assert charged - raw < 0.0001  # never more than a hundredth of a cent over


def test_a_one_dollar_trade_is_not_fee_free():
    """The size this system actually trades overnight."""
    from btc15_signal.validation import kalshi_fee_charged

    fee = kalshi_fee_charged(0.87, 1)
    assert 0.007 < fee < 0.011
    # Against roughly 13c of gross profit if it wins, that is a real bite.
    assert fee / (1 - 0.87) > 0.05
