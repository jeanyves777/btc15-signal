from dataclasses import replace

from btc15_signal.datasource import Candle, ContractCandle, Market
from btc15_signal.features import build_snapshots, quote_at

OPEN_MS = 1_700_000_000_000 - 1_700_000_000_000 % 900_000


def klines(prices: list[float], open_ms: int = OPEN_MS) -> list[Candle]:
    return [
        Candle(open_ms + minute * 60_000, price, price, price, price, 10.0, 6.0)
        for minute, price in enumerate(prices)
    ]


def contract(open_ms: int = OPEN_MS) -> list[ContractCandle]:
    return [
        ContractCandle(
            end_period_ts=open_ms // 1000 + minute * 60,
            yes_bid_open=0.40,
            yes_bid_high=0.42,
            yes_bid_low=0.38,
            yes_bid_close=0.40 + minute / 1000,
            yes_ask_open=0.42,
            yes_ask_high=0.44,
            yes_ask_low=0.40,
            yes_ask_close=0.42 + minute / 1000,
            price_close=0.41,
            volume=50.0,
            open_interest=500.0,
        )
        for minute in range(16)
    ]


def market(**overrides) -> Market:
    base = {
        "ticker": "TEST",
        "open_ms": OPEN_MS,
        "close_ms": OPEN_MS + 900_000,
        "floor_strike": 100.0,
        "result": "yes",
        "status": "finalized",
        "expiration_value": 101.5,
        "volume": 1000.0,
    }
    return Market(**{**base, **overrides})


def test_quote_at_never_returns_a_candle_that_had_not_closed():
    candles = contract()
    cutoff = OPEN_MS // 1000 + 5 * 60
    chosen = quote_at(candles, cutoff)
    assert chosen.end_period_ts == cutoff  # the candle ending exactly at the cutoff is fair game
    assert quote_at(candles, OPEN_MS // 1000 - 1) is None


def test_features_ignore_everything_after_the_decision_minute():
    """The whole validation rests on this: change the future, keep the snapshot."""
    rising = [100.0 + minute * 0.1 for minute in range(15)]
    spiked = rising[:8] + [500.0] * 7  # violent move strictly after minute 8

    base = build_snapshots([market()], klines(rising), {"TEST": contract()})
    moved = build_snapshots([market()], klines(spiked), {"TEST": contract()})

    early_base = {snap.elapsed: snap for snap in base if snap.elapsed <= 8}
    early_moved = {snap.elapsed: snap for snap in moved if snap.elapsed <= 8}
    assert set(early_base) == set(early_moved)
    for elapsed, snap in early_base.items():
        other = early_moved[elapsed]
        assert snap.price == other.price
        assert snap.window_high == other.window_high
        assert snap.window_low == other.window_low
        assert snap.momentum_5m_bps == other.momentum_5m_bps
        assert snap.signed_distance_bps == other.signed_distance_bps


def test_window_high_and_low_only_cover_elapsed_minutes():
    prices = [100.0, 105.0, 95.0] + [100.0] * 12
    snaps = {snap.elapsed: snap for snap in build_snapshots(
        [market()], klines(prices), {"TEST": contract()}
    )}
    assert snaps[3].window_high == 105.0
    assert snaps[3].window_low == 95.0


def test_entry_price_maps_down_to_the_no_ask():
    snaps = build_snapshots(
        [market()], klines([100.0] * 15), {"TEST": contract()}
    )
    snap = next(item for item in snaps if item.elapsed == 5)
    assert snap.entry_price("UP") == snap.yes_ask
    assert snap.entry_price("DOWN") == round(1 - snap.yes_bid, 4)


def test_won_follows_the_side_and_is_none_for_unsettled_markets():
    snaps = build_snapshots([market(result="yes")], klines([100.0] * 15), {"TEST": contract()})
    snap = snaps[0]
    assert snap.won("UP") is True
    assert snap.won("DOWN") is False

    voided = build_snapshots([market(result="")], klines([100.0] * 15), {"TEST": contract()})
    assert voided[0].won("UP") is None


def test_incomplete_binance_window_is_skipped():
    """A window missing minutes cannot be scored consistently, so it is dropped."""
    assert build_snapshots([market()], klines([100.0] * 12), {"TEST": contract()}) == []


def test_oracle_basis_records_binance_versus_settlement_divergence():
    snaps = build_snapshots(
        [market(expiration_value=101.0)], klines([100.0] * 15), {"TEST": contract()}
    )
    snap = snaps[0]
    assert snap.binance_close == 100.0
    assert round(snap.oracle_basis_bps, 2) == round((100.0 / 101.0 - 1) * 10_000, 2)


def test_snapshots_are_produced_for_every_decision_minute():
    snaps = build_snapshots([market()], klines([100.0] * 15), {"TEST": contract()})
    assert [snap.elapsed for snap in snaps] == list(range(3, 15))


def test_missing_contract_candles_leave_prices_none_rather_than_guessing():
    snaps = build_snapshots([market()], klines([100.0] * 15), {"TEST": []})
    assert all(snap.yes_ask is None and snap.yes_bid is None for snap in snaps)
    assert all(snap.entry_price("UP") is None for snap in snaps)


def test_replace_on_result_is_enough_to_relabel_a_snapshot():
    """The permutation test relies on this - outcomes swap, features do not."""
    snap = build_snapshots([market()], klines([100.0] * 15), {"TEST": contract()})[0]
    flipped = replace(snap, result="no")
    assert flipped.price == snap.price
    assert flipped.won("UP") is False
