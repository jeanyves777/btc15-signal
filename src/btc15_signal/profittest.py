"""Real-money profit test.

Everything else in this package reports edge per contract, which is the right
unit for deciding whether an edge exists but tells you nothing about what lands
in an account. This module answers the other question: stake $N on every
qualifying signal for 68 days, pay the real fees, and report the dollars.

Entry mirrors the live service: walk the window from `enter_from` minutes
remaining down to `enter_to`, take the first minute where the model's side is
quoted inside the price band, and buy `stake / ask` contracts.

Exit policies, all priced on the side of the book the position must actually
hit (long YES sells at the yes bid, long NO sells at the no bid = 1 - yes ask):

* `hold`      - settle at expiry. One fee, on entry.
* `reversal`  - the thesis was "BTC stays on this side of the strike". If it
                crosses back, the thesis is dead: sell at the next available
                bid. Two fees.
* `stop`      - sell if the bid falls `stop` below entry. Two fees.
* `reversal+stop` - whichever comes first.

Fees use Kalshi's published schedule, rounded up to the cent per ORDER, which
is what a real ticket is charged. Settlement is free.
"""

import argparse
import json
from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

from .datasource import DataCache
from .features import Snapshot, build_snapshots
from .model import predict
from .validation import as_market_snapshot
from .validation import kalshi_fee_observed as kalshi_fee


@dataclass(frozen=True)
class MoneyTrade:
    ticker: str
    open_ms: int
    side: str
    entry_minute: int  # minutes elapsed in the window
    entry_price: float
    contracts: float
    stake: float
    entry_fee: float
    exit_reason: str
    exit_minute: int | None
    exit_price: float | None
    exit_fee: float
    gross: float  # dollars returned minus dollars staked, before fees
    net: float  # after both fees

    @property
    def won(self) -> bool:
        return self.net > 0


def _bid_for(side: str, candle) -> float | None:
    """Price at which the position could be closed at this candle's close."""
    if side == "UP":
        return candle.yes_bid_close
    return None if candle.yes_ask_close is None else round(1 - candle.yes_ask_close, 4)


def simulate(
    snapshots: list[Snapshot],
    candles: dict,
    klines_by_window: dict,
    stake: float = 10.0,
    min_ask: float = 0.85,
    max_ask: float = 0.99,
    enter_from: int = 10,
    enter_to: int = 6,
    policy: str = "hold",
    stop: float = 0.15,
    slippage: float = 0.0,
) -> list[MoneyTrade]:
    by_market: dict[str, list[Snapshot]] = defaultdict(list)
    for snap in snapshots:
        by_market[snap.ticker].append(snap)

    trades: list[MoneyTrade] = []
    for ticker, rows in by_market.items():
        rows.sort(key=lambda item: item.elapsed)
        entry_snap = None
        for snap in rows:
            if not enter_to <= snap.remaining <= enter_from:
                continue
            side = predict(as_market_snapshot(snap)).side
            ask = snap.entry_price(side)
            if ask is None or not min_ask <= ask <= max_ask:
                continue
            # Slippage is paid on the price, not modelled by narrowing the band:
            # you still take the signal, you just get a worse fill.
            entry_snap, entry_side = snap, side
            entry_ask = min(0.99, round(ask + slippage, 4))
            break
        if entry_snap is None:
            continue
        won = entry_snap.won(entry_side)
        if won is None:
            continue

        contracts = stake / entry_ask
        entry_fee = kalshi_fee(entry_ask, contracts)
        cutoff = entry_snap.open_ms // 1000 + entry_snap.elapsed * 60
        target = entry_snap.target

        # minute-by-minute walk forward from entry to close
        quotes = {
            c.end_period_ts: c for c in candles.get(ticker, []) if c.end_period_ts > cutoff
        }
        prices = {
            row.open_time: row.close
            for row in klines_by_window.get(entry_snap.open_ms, [])
        }

        exit_reason, exit_minute, exit_price = "settlement", None, None
        if policy != "hold":
            for step in range(1, 15 - entry_snap.elapsed):
                minute = entry_snap.elapsed + step
                candle = quotes.get(entry_snap.open_ms // 1000 + minute * 60)
                if candle is None:
                    continue
                bid = _bid_for(entry_side, candle)
                if bid is None:
                    continue
                # A snapshot at minute m uses the close of the kline that OPENED
                # at m-1. Reading the kline opening at m would be the price one
                # minute into the future - enough to sell before the book
                # reprices, which invents the entire profit.
                spot = prices.get(entry_snap.open_ms + (minute - 1) * 60_000)
                reversed_side = spot is not None and (
                    (entry_side == "UP" and spot < target)
                    or (entry_side == "DOWN" and spot > target)
                )
                stopped = bid <= entry_ask - stop
                if "reversal" in policy and reversed_side:
                    exit_reason, exit_minute, exit_price = "reversal", step, bid
                    break
                if "stop" in policy and stopped:
                    exit_reason, exit_minute, exit_price = "stop", step, bid
                    break

        if exit_price is not None:
            gross = contracts * (exit_price - entry_ask)
            exit_fee = kalshi_fee(exit_price, contracts)
        else:
            gross = contracts * ((1.0 if won else 0.0) - entry_ask)
            exit_fee = 0.0

        trades.append(
            MoneyTrade(
                ticker=ticker,
                open_ms=entry_snap.open_ms,
                side=entry_side,
                entry_minute=entry_snap.elapsed,
                entry_price=entry_ask,
                contracts=round(contracts, 4),
                stake=stake,
                entry_fee=entry_fee,
                exit_reason=exit_reason,
                exit_minute=exit_minute,
                exit_price=exit_price,
                exit_fee=exit_fee,
                gross=round(gross, 4),
                net=round(gross - entry_fee - exit_fee, 4),
            )
        )
    return sorted(trades, key=lambda item: item.open_ms)


def account(trades: list[MoneyTrade], stake: float) -> dict:
    """What the account actually did, in dollars."""
    if not trades:
        return {"trades": 0}
    net = sum(item.net for item in trades)
    gross = sum(item.gross for item in trades)
    fees = sum(item.entry_fee + item.exit_fee for item in trades)
    wins = sum(1 for item in trades if item.net > 0)

    equity = peak = drawdown = 0.0
    for item in trades:
        equity += item.net
        peak = max(peak, equity)
        drawdown = max(drawdown, peak - equity)

    daily: dict[str, float] = defaultdict(float)
    for item in trades:
        daily[datetime.fromtimestamp(item.open_ms / 1000, UTC).strftime("%Y-%m-%d")] += item.net

    reasons: dict[str, int] = defaultdict(int)
    for item in trades:
        reasons[item.exit_reason] += 1

    # Resample whole DAYS. Trades inside a day share a market regime and the
    # same few price levels; resampling trades would pretend they are
    # independent and shrink the interval.
    import random as _random

    values = list(daily.values())
    rng = _random.Random(31)
    totals = []
    for _ in range(4000):
        total = sum(values[rng.randrange(len(values))] for _ in values)
        totals.append(total)
    totals.sort()
    low, high = totals[100], totals[3899]
    p_value = max(1 / 4000, sum(1 for value in totals if value <= 0) / 4000)

    days = len(daily)
    return {
        "net_ci_low": round(low, 2),
        "net_ci_high": round(high, 2),
        "p_value_vs_zero": round(p_value, 4),
        "trades": len(trades),
        "stake_per_trade": stake,
        "total_staked": round(stake * len(trades), 2),
        "win_rate": round(wins / len(trades), 4),
        "average_entry": round(sum(i.entry_price for i in trades) / len(trades), 4),
        "gross_profit": round(gross, 2),
        "total_fees": round(fees, 2),
        "net_profit": round(net, 2),
        "return_on_stake": round(net / (stake * len(trades)), 5),
        "profit_per_trade": round(net / len(trades), 4),
        "max_drawdown": round(drawdown, 2),
        "days": days,
        "profit_per_day": round(net / days, 2) if days else 0.0,
        "trades_per_day": round(len(trades) / days, 1) if days else 0.0,
        "positive_days": sum(1 for value in daily.values() if value > 0),
        "worst_day": round(min(daily.values()), 2),
        "best_day": round(max(daily.values()), 2),
        "exit_reasons": dict(reasons),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Real-money profit test for the BTC15 signal")
    parser.add_argument("--cache", default="data/market_data.db")
    parser.add_argument("--stake", type=float, default=10.0)
    parser.add_argument("--min-ask", type=float, default=0.85)
    parser.add_argument("--max-ask", type=float, default=0.99)
    parser.add_argument("--enter-from", type=int, default=10)
    parser.add_argument("--enter-to", type=int, default=6)
    parser.add_argument("--stop", type=float, default=0.15)
    parser.add_argument(
        "--slippage", type=float, default=0.0, help="dollars paid above the displayed ask"
    )
    parser.add_argument("--output", default="reports/profit-test.json")
    return parser.parse_args()


def run() -> None:
    args = parse_args()
    cache = DataCache(args.cache)
    markets = cache.markets()
    if not markets:
        raise SystemExit("cache is empty - run scripts/sync_data.py first")
    klines = cache.klines()
    candles = cache.contract_candles()
    snapshots = build_snapshots(markets, klines, candles)

    from .features import group_klines

    windows = group_klines(klines)

    results = {}
    for policy in ("hold", "reversal", "stop", "reversal+stop"):
        trades = simulate(
            snapshots,
            candles,
            windows,
            stake=args.stake,
            min_ask=args.min_ask,
            max_ask=args.max_ask,
            enter_from=args.enter_from,
            enter_to=args.enter_to,
            policy=policy,
            stop=args.stop,
            slippage=args.slippage,
        )
        results[policy] = {"summary": account(trades, args.stake)}
        if policy == "hold":
            results[policy]["sample"] = [asdict(item) for item in trades[:5]]

    period = (
        f"{datetime.fromtimestamp(markets[0].open_ms / 1000, UTC):%Y-%m-%d} to "
        f"{datetime.fromtimestamp(markets[-1].open_ms / 1000, UTC):%Y-%m-%d}"
    )
    report = {
        "generated_at": datetime.now(UTC).isoformat(),
        "period": period,
        "markets": len(markets),
        "settings": vars(args),
        "policies": results,
    }
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2, default=str) + "\n")

    print(f"\nREAL-MONEY TEST  ${args.stake:.0f} per signal, entry {args.enter_from}-"
          f"{args.enter_to} min out, ask {args.min_ask}-{args.max_ask}")
    print(f"{period}   {len(markets)} markets\n")
    header = (
        f"{'policy':<15} {'trades':>7} {'staked':>10} {'gross':>9} {'fees':>8} "
        f"{'NET':>9} {'ROI':>8} {'$/day':>8} {'maxDD':>8} {'win':>7}"
    )
    print(header)
    print("-" * len(header))
    for policy, data in results.items():
        s = data["summary"]
        if not s.get("trades"):
            continue
        print(
            f"{policy:<15} {s['trades']:>7} {s['total_staked']:>10,.0f} {s['gross_profit']:>9.2f} "
            f"{s['total_fees']:>8.2f} {s['net_profit']:>9.2f} {s['return_on_stake']:>7.2%} "
            f"{s['profit_per_day']:>8.2f} {s['max_drawdown']:>8.2f} {s['win_rate']:>6.1%}"
        )
    print()
    print(f"{'policy':<15} {'NET':>9}  {'95% CI on net (day bootstrap)':>32} {'p vs 0':>8}")
    print("-" * 68)
    for policy, data in results.items():
        s = data["summary"]
        if not s.get("trades"):
            continue
        print(
            f"{policy:<15} {s['net_profit']:>9.2f}  "
            f"{s['net_ci_low']:>14.2f} to {s['net_ci_high']:>14.2f} {s['p_value_vs_zero']:>8.4f}"
        )
    print(f"\nJSON: {output}")


if __name__ == "__main__":
    run()
