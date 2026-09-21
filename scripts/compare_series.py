"""Run the DEPLOYED rule against any series, using Kalshi + Binance minute data.

The earlier ETH/SOL comparison measured only the raw favourite-longshot bias:
buy the dearer side, hold to settlement. That is not the strategy. The deployed
rule also requires the strike to be a minimum number of volatility units away,
and volatility comes from the Binance klines - so a study without them measures
something the bot does not do.

    python scripts/compare_series.py
"""

import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from btc15_signal.features import (  # noqa: E402
    Candle,
    ContractCandle,
    Market,
    build_snapshots,
)
from btc15_signal.strategy import EntryRule  # noqa: E402
from btc15_signal.validation import (  # noqa: E402
    cluster_bootstrap,
    kalshi_fee_charged,
)

SERIES = [
    ("BTC", "data/market_data.db"),
    ("ETH", "data/market_data_kxeth15m.db"),
    ("SOL", "data/market_data_kxsol15m.db"),
]


def load(path: str):
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    markets = [
        Market(
            ticker=r["ticker"], open_ms=r["open_ms"], close_ms=r["close_ms"],
            floor_strike=r["floor_strike"], result=r["result"], status=r["status"],
            expiration_value=r["expiration_value"], volume=r["volume"] or 0.0,
        )
        for r in db.execute(
            "SELECT * FROM markets WHERE result IN ('yes','no') "
            "AND floor_strike IS NOT NULL AND open_ms IS NOT NULL"
        )
    ]
    klines = [
        Candle(
            open_time=r["open_time"], open=r["open"], high=r["high"], low=r["low"],
            close=r["close"], volume=r["volume"], taker_buy_volume=r["taker_buy_volume"],
        )
        for r in db.execute("SELECT * FROM klines ORDER BY open_time")
    ]
    candles: dict[str, list[ContractCandle]] = {}
    for r in db.execute("SELECT * FROM contract_candles ORDER BY ticker, end_period_ts"):
        candles.setdefault(r["ticker"], []).append(
            ContractCandle(
                end_period_ts=r["end_period_ts"],
                yes_bid_open=r["yes_bid_open"], yes_bid_high=r["yes_bid_high"],
                yes_bid_low=r["yes_bid_low"], yes_bid_close=r["yes_bid_close"],
                yes_ask_open=r["yes_ask_open"], yes_ask_high=r["yes_ask_high"],
                yes_ask_low=r["yes_ask_low"], yes_ask_close=r["yes_ask_close"],
                price_close=r["price_close"], volume=r["volume"] or 0.0,
                open_interest=r["open_interest"] or 0.0,
            )
        )
    return markets, klines, candles


def measure(label: str, path: str, rule: EntryRule) -> None:
    if not Path(path).exists():
        print(f"{label:>4}  no cache at {path}")
        return
    markets, klines, candles = load(path)
    if not klines:
        print(f"{label:>4}  no klines - run fetch_klines.py first")
        return

    snapshots = build_snapshots(markets, klines, candles)
    groups: dict[str, list[float]] = {}
    for snap in snapshots:
        # The deployed window is 6-11 minutes remaining.
        if not 6 <= snap.remaining <= 11:
            continue
        if snap.yes_ask is None or snap.yes_bid is None:
            continue
        up, down = snap.yes_ask, 1 - snap.yes_bid
        side_is_up = up >= down
        ask = max(up, down)
        if not 0 < ask < 1:
            continue
        won = (snap.result == "yes") if side_is_up else (snap.result == "no")

        # The deployed gates, computed from the Binance minute data.
        if not rule.min_ask <= ask <= rule.max_ask:
            continue
        vol = max(snap.volatility_5m_bps, 1.0)
        if abs(snap.signed_distance_bps) / vol < rule.min_normalized_distance:
            continue
        groups.setdefault(snap.ticker, []).append(
            (1.0 if won else 0.0) - ask - kalshi_fee_charged(ask, 1)
        )

    n = sum(len(v) for v in groups.values())
    if n < 100:
        print(f"{label:>4}  {len(snapshots):>7} snapshots, only {n} qualify - too thin")
        return
    (low, high), p = cluster_bootstrap(groups, 3000, 7)
    mean = sum(sum(v) for v in groups.values()) / n
    print(
        f"{label:>4}  {len(snapshots):>7} {n:>6} {len(groups):>7} {mean:>+9.4f} "
        f"[{low:+.4f}, {high:+.4f}] {p:>7.4f}  "
        f"{'TRADEABLE' if low > 0 else 'not demonstrated'}"
    )


def main() -> None:
    rule = EntryRule.load("strategy.json")
    print(
        f"deployed rule: {rule.min_ask}-{rule.max_ask}, "
        f"distance >= {rule.min_normalized_distance}x vol, 6-11 minutes left"
    )
    print()
    print(
        f"{'':>4}  {'snaps':>7} {'taken':>6} {'markets':>7} "
        f"{'NET edge':>9} {'95% CI':>22} {'p':>7}"
    )
    for label, path in SERIES:
        measure(label, path, rule)


if __name__ == "__main__":
    main()
