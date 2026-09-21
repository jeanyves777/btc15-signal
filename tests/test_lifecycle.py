from btc15_signal.datasource import ContractCandle
from btc15_signal.lifecycle import (
    build_lifecycle,
    lifecycle_intelligence,
    policy_summary,
    simulate_exit,
)

OPEN_MS = 1_700_000_000_000  # aligned to a minute; cutoffs are computed in seconds


def candle(minute: int, bid: tuple[float, float, float], ask: tuple[float, float, float]):
    """minute is minutes after the window open; bid/ask are (high, low, close)."""
    return ContractCandle(
        end_period_ts=OPEN_MS // 1000 + minute * 60,
        yes_bid_open=bid[2],
        yes_bid_high=bid[0],
        yes_bid_low=bid[1],
        yes_bid_close=bid[2],
        yes_ask_open=ask[2],
        yes_ask_high=ask[0],
        yes_ask_low=ask[1],
        yes_ask_close=ask[2],
        price_close=bid[2],
        volume=100.0,
        open_interest=1000.0,
    )


def up_lifecycle(settled_won: bool):
    """Long YES at 0.30; the bid peaks at 0.52 two minutes later, then collapses."""
    candles = [
        candle(11, (0.31, 0.29, 0.30), (0.33, 0.31, 0.32)),  # at entry, excluded
        candle(12, (0.44, 0.30, 0.42), (0.46, 0.32, 0.44)),
        candle(13, (0.52, 0.40, 0.45), (0.54, 0.42, 0.47)),
        candle(14, (0.20, 0.02, 0.03), (0.22, 0.04, 0.05)),
    ]
    return build_lifecycle(
        ticker="TEST",
        open_ms=OPEN_MS,
        side="UP",
        entry_minute=11,
        entry_price=0.30,
        settled_won=settled_won,
        candles=candles,
    )


def test_path_excludes_the_entry_candle_and_starts_after_it():
    life = up_lifecycle(settled_won=False)
    assert [point.minute for point in life.path] == [1, 2, 3]


def test_mfe_and_mae_use_the_exit_side_of_the_book():
    life = up_lifecycle(settled_won=False)
    assert round(life.mfe, 4) == 0.22  # 0.52 bid high - 0.30 entry
    assert round(life.mae, 4) == -0.28  # 0.02 bid low - 0.30 entry
    assert life.peak.minute == 2


def test_losing_trade_is_flagged_as_rescuable_with_the_minute_it_peaked():
    life = up_lifecycle(settled_won=False)
    assert life.settlement_pnl == -0.30
    assert life.rescuable_loss(0.05)
    assert life.ever_profitable_at[0.10] == 1
    assert life.ever_profitable_at[0.20] == 2
    assert life.ever_profitable_at[0.25] is None


def test_winning_trade_that_dipped_is_flagged_as_squandered():
    life = up_lifecycle(settled_won=True)
    assert life.settlement_pnl == 0.70
    assert life.squandered_win(0.10)
    assert not life.rescuable_loss()


def test_short_side_exits_at_the_no_bid():
    """A DOWN position is long NO, so it closes at 1 - yes_ask."""
    candles = [candle(6, (0.60, 0.40, 0.50), (0.62, 0.20, 0.55))]
    life = build_lifecycle(
        ticker="TEST",
        open_ms=OPEN_MS,
        side="DOWN",
        entry_minute=5,
        entry_price=0.40,
        settled_won=False,
        candles=candles,
    )
    point = life.path[0]
    assert point.best == 0.80  # 1 - yes_ask_low 0.20
    assert point.worst == 0.38  # 1 - yes_ask_high 0.62
    assert round(life.mfe, 4) == 0.40


def test_take_profit_exit_beats_holding_a_loser_to_expiry():
    life = up_lifecycle(settled_won=False)
    held = simulate_exit(life)
    taken = simulate_exit(life, take_profit=0.10)
    assert held.reason == "settlement"
    assert round(held.pnl, 4) == -0.30
    assert taken.reason == "take_profit"
    assert taken.minute == 1
    assert round(taken.pnl, 4) == 0.10


def test_stop_loss_is_assumed_to_trigger_before_a_take_profit_in_the_same_minute():
    """A minute candle cannot order the two touches, so assume the bad one first."""
    candles = [candle(12, (0.60, 0.10, 0.30), (0.62, 0.12, 0.32))]
    life = build_lifecycle(
        ticker="TEST",
        open_ms=OPEN_MS,
        side="UP",
        entry_minute=11,
        entry_price=0.30,
        settled_won=True,
        candles=candles,
    )
    result = simulate_exit(life, take_profit=0.10, stop_loss=0.10)
    assert result.reason == "stop_loss"
    assert round(result.pnl, 4) == -0.10


def test_settlement_is_used_when_no_policy_threshold_is_reached():
    life = up_lifecycle(settled_won=True)
    result = simulate_exit(life, take_profit=0.90)
    assert result.reason == "settlement"
    assert round(result.pnl, 4) == 0.70


def test_lifecycle_intelligence_counts_rescuable_losses():
    report = lifecycle_intelligence([up_lifecycle(False), up_lifecycle(True)])
    assert report["trades"] == 2
    assert report["losers"] == 1
    assert report["rescuable_losses"] == 1
    assert report["rescuable_share_of_losses"] == 1.0
    assert report["profit_ladder"]["0.20"]["losers_that_reached"] == 1


def test_policy_summary_aggregates_exit_reasons():
    summary = policy_summary([up_lifecycle(False), up_lifecycle(True)], take_profit=0.10)
    assert summary["trades"] == 2
    assert summary["exit_reasons"] == {"take_profit": 2}
    assert round(summary["total_pnl"], 4) == 0.20
