from btc15_signal.datasource import Candle, ContractCandle, Market
from btc15_signal.features import build_snapshots, group_klines
from btc15_signal.profittest import account, simulate
from btc15_signal.validation import kalshi_fee_observed as kalshi_fee

OPEN_MS = 1_700_000_000_000 - 1_700_000_000_000 % 900_000
TARGET = 100.0


def contract_candles(bids: dict[int, float], open_ms: int = OPEN_MS):
    """bids maps minute -> yes bid; the ask sits one cent above."""
    rows = []
    for minute in range(16):
        bid = bids.get(minute, 0.90)
        rows.append(
            ContractCandle(
                end_period_ts=open_ms // 1000 + minute * 60,
                yes_bid_open=bid,
                yes_bid_high=bid,
                yes_bid_low=bid,
                yes_bid_close=bid,
                yes_ask_open=bid + 0.01,
                yes_ask_high=bid + 0.01,
                yes_ask_low=bid + 0.01,
                yes_ask_close=bid + 0.01,
                price_close=bid,
                volume=500.0,
                open_interest=5000.0,
            )
        )
    return rows


def klines(prices: list[float], open_ms: int = OPEN_MS):
    return [
        Candle(open_ms + m * 60_000, p, p, p, p, 10.0, 6.0) for m, p in enumerate(prices)
    ]


def market(result: str = "yes"):
    return Market("T", OPEN_MS, OPEN_MS + 900_000, TARGET, result, "finalized", TARGET, 1000.0)


def build(prices, bids, result="yes"):
    rows = klines(prices)
    snaps = build_snapshots([market(result)], rows, {"T": contract_candles(bids)})
    return snaps, {"T": contract_candles(bids)}, group_klines(rows)


def test_entry_takes_the_first_qualifying_minute_in_the_window():
    """Above the strike all window; entry should fire at 10 min left, not 6."""
    snaps, candles, windows = build([101.0] * 15, {})
    trades = simulate(snaps, candles, windows, stake=10.0, enter_from=10, enter_to=6)
    assert len(trades) == 1
    assert trades[0].entry_minute == 5  # 15 - 10 remaining
    assert trades[0].side == "UP"


def test_only_one_trade_per_market():
    snaps, candles, windows = build([101.0] * 15, {})
    assert len(simulate(snaps, candles, windows, enter_from=10, enter_to=1)) == 1


def test_stake_buys_more_contracts_at_a_lower_price():
    snaps, candles, windows = build([101.0] * 15, {m: 0.86 for m in range(16)})
    trade = simulate(snaps, candles, windows, stake=10.0)[0]
    assert abs(trade.contracts - 10.0 / trade.entry_price) < 1e-3  # stored to 4dp
    assert trade.entry_fee == kalshi_fee(trade.entry_price, trade.contracts)


def test_hold_pays_settlement_and_charges_one_fee():
    snaps, candles, windows = build([101.0] * 15, {}, result="yes")
    trade = simulate(snaps, candles, windows, stake=10.0, policy="hold")[0]
    assert trade.exit_reason == "settlement"
    assert trade.exit_fee == 0.0  # settlement is free
    assert trade.gross > 0


def test_reversal_exit_fires_when_price_crosses_back_through_the_strike():
    """Up through minute 7, then below the strike: the thesis is dead."""
    prices = [101.0] * 8 + [99.0] * 7
    snaps, candles, windows = build(prices, {m: (0.90 if m < 9 else 0.30) for m in range(16)},
                                    result="no")
    held = simulate(snaps, candles, windows, stake=10.0, policy="hold")[0]
    exited = simulate(snaps, candles, windows, stake=10.0, policy="reversal")[0]
    assert held.exit_reason == "settlement"
    assert exited.exit_reason == "reversal"
    # Selling into the break beats riding it to zero.
    assert exited.net > held.net
    assert exited.exit_fee > 0  # closing early is a second execution


def test_reversal_exit_never_uses_a_future_price():
    """The exit may only act on prices already printed at the quote it sells at."""
    late = [101.0] * 14 + [99.0]  # crosses only in the final minute
    snaps, candles, windows = build(late, {}, result="no")
    trade = simulate(snaps, candles, windows, stake=10.0, policy="reversal")[0]
    # The cross is visible too late to trade on, so the position must settle.
    assert trade.exit_reason == "settlement"


def test_stop_exit_fires_when_the_bid_falls_far_enough():
    snaps, candles, windows = build([101.0] * 15, {m: (0.90 if m < 8 else 0.60) for m in range(16)})
    trade = simulate(snaps, candles, windows, stake=10.0, policy="stop", stop=0.15)[0]
    assert trade.exit_reason == "stop"
    assert trade.exit_price is not None


def test_account_reports_dollars_and_a_day_bootstrap_interval():
    snaps, candles, windows = build([101.0] * 15, {})
    trades = simulate(snaps, candles, windows, stake=10.0)
    summary = account(trades, 10.0)
    assert summary["trades"] == 1
    assert summary["total_staked"] == 10.0
    assert "net_ci_low" in summary and "net_ci_high" in summary
    assert summary["net_ci_low"] <= summary["net_profit"] <= summary["net_ci_high"]


def test_no_entry_when_the_price_is_outside_the_band():
    snaps, candles, windows = build([101.0] * 15, {m: 0.40 for m in range(16)})
    assert simulate(snaps, candles, windows, min_ask=0.85, max_ask=0.99) == []
