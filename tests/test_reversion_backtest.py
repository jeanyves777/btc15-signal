from btc15_signal.reversion_backtest import (
    ReversionCandidate,
    apply_reversion_rule,
    reversion_metrics,
)
from btc15_signal.strategy import ReversionRule


def candidate(future_prices: tuple[float, ...], won: bool = False) -> ReversionCandidate:
    return ReversionCandidate(0, "TEST", 11, "DOWN", 0.33, 15, 3, 12, won, future_prices)


def test_take_profit_is_used_before_settlement():
    trades = apply_reversion_rule([candidate((0.40, 0.51))], ReversionRule())
    assert len(trades) == 1
    assert trades[0].take_profit_hit
    assert round(trades[0].pnl_after_fee, 2) == 0.15


def test_unfilled_take_profit_falls_back_to_settlement_result():
    trades = apply_reversion_rule([candidate((0.40,), won=False)], ReversionRule())
    result = reversion_metrics(trades)
    assert not trades[0].take_profit_hit
    assert round(trades[0].pnl_after_fee, 2) == -0.35
    assert result.profitable == 0
