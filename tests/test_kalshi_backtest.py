from btc15_signal.backtest import Candle
from btc15_signal.kalshi_backtest import (
    Candidate,
    apply_rule,
    build_candidates,
    metrics,
)
from btc15_signal.strategy import EntryRule


def test_fee_aware_metrics_and_flexible_rule():
    candidate = Candidate(0, "TEST", 5, "UP", 0.9, 0.70, 2.0, True, True, 0.30)
    rule = EntryRule(
        enabled=True,
        remaining_minutes=5,
        min_raw_probability=0.8,
        min_ask=0.6,
        max_ask=0.8,
        min_normalized_distance=1.0,
        require_momentum_alignment=True,
    )
    result = metrics(apply_rule([candidate], rule), 0.02)
    assert result.signals == 1
    assert result.wins == 1
    assert round(result.pnl_after_fee_buffer, 2) == 0.28


def test_candidates_use_exact_kalshi_target():
    binance = []
    for minute in range(15):
        price = 100 + minute * 0.1
        binance.append(Candle(minute * 60_000, price, price, price, price, 10, 6))
    market = {
        "ticker": "TEST",
        "open_time": "1970-01-01T00:00:00Z",
        "floor_strike": 102.0,
        "result": "no",
    }
    contract = {
        "TEST": [
            {
                "end_period_ts": minute * 60,
                "yes_ask": {"close_dollars": "0.20"},
                "yes_bid": {"close_dollars": "0.19"},
            }
            for minute in range(1, 16)
        ]
    }
    candidates = build_candidates(binance, [market], contract)
    five_minute = next(item for item in candidates if item.remaining_minutes == 5)
    assert five_minute.side == "DOWN"
    assert five_minute.won
