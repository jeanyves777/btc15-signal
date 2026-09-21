"""Measure the hourly ladder against PRE-REGISTERED rules. Research only.

The point of this script is what it refuses to do. A ladder offers ~24
correlated, quotable rungs at once, so "find the best rung" is a maximum over
correlated noise and will report an edge on data with none in it - the same
failure that made the best of 11,365 grid-searched rules score below the median
shuffled result (FINDINGS.md 7).

So every rule here picks its rung by a rule fixed IN ADVANCE, and picks it by
PRICE or by DISTANCE, never by estimated edge:

  NEAREST   the rung closest to spot. A coin flip, priced near 0.50, where the
            Kalshi fee peaks. Included as the control that should NOT work.
  FAVOURITE the rung whose dearer side is priced closest to a target (default
            0.89). This is the analogue of the deployed 15-minute rule. Picking
            by target price cannot leak the outcome, because the price is
            observable at entry and says nothing about which way it resolves.

Time is scaled properly. The 15-minute rule divides distance by 5-minute
volatility because its horizon barely varies (6-11 minutes). An hourly window
has 10-55 minutes left depending on when you look, so the expected move grows
as sqrt(time) and the normalised distance must divide by
`vol_5m * sqrt(remaining / 5)`. Using the unscaled figure would call a strike
"far" at 50 minutes out on the strength of a 5-minute number.

The sample is split in time: the earlier portion is for looking, the later
portion is touched once. Anything found by eye in the first is confirmed or
abandoned in the second - no re-slicing.

    python scripts/measure_hourly.py --db data/market_data_kxbtcd.db
"""

import argparse
import math
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from btc15_signal.validation import cluster_bootstrap, kalshi_fee_charged  # noqa: E402


def load(path: str):
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row

    events: dict[str, dict] = {}
    for r in db.execute(
        "SELECT event_ticker, ticker, floor_strike, result, expiration_value, "
        "open_ms, close_ms FROM markets WHERE result IN ('yes','no') "
        "AND floor_strike IS NOT NULL"
    ):
        ev = events.setdefault(
            r["event_ticker"],
            {
                "open_ms": r["open_ms"], "close_ms": r["close_ms"],
                "settle": r["expiration_value"], "rungs": {},
            },
        )
        ev["rungs"][r["ticker"]] = r["floor_strike"]

    klines = {
        r["open_time"] // 60_000: r["close"]
        for r in db.execute("SELECT open_time, close FROM klines")
    }

    quotes: dict[str, dict[int, tuple]] = defaultdict(dict)
    for r in db.execute(
        "SELECT ticker, end_period_ts, yes_bid_close, yes_ask_close "
        "FROM contract_candles"
    ):
        if r["yes_bid_close"] is None or r["yes_ask_close"] is None:
            continue
        quotes[r["ticker"]][int(r["end_period_ts"]) // 60] = (
            r["yes_bid_close"], r["yes_ask_close"]
        )
    return events, klines, quotes


def volatility_bps(klines: dict, minute: int, lookback: int = 5) -> float:
    """Realised 5-minute volatility of the underlying, in bps per minute."""
    moves = []
    for k in range(minute - lookback + 1, minute + 1):
        now, before = klines.get(k), klines.get(k - 1)
        if now and before:
            moves.append(abs(now / before - 1) * 10_000)
    return sum(moves) / len(moves) if moves else 0.0


def entries(events, klines, quotes, *, rule, target, lo_min, hi_min, min_norm):
    """One entry per event, at the first qualifying minute. Never re-enters."""
    out = []
    for event, data in sorted(events.items()):
        settle = data["settle"]
        if not settle or not data["close_ms"]:
            continue
        close_min = data["close_ms"] // 60_000
        chosen = None
        for minute in range(close_min - hi_min, close_min - lo_min + 1):
            remaining = close_min - minute
            spot = klines.get(minute)
            if not spot or remaining <= 0:
                continue
            vol = volatility_bps(klines, minute)
            if vol <= 0:
                continue
            # Expected move grows with the square root of time left.
            horizon = vol * math.sqrt(remaining / 5.0)

            live = []
            for ticker, strike in data["rungs"].items():
                q = quotes.get(ticker, {}).get(minute)
                if not q:
                    continue
                yes_bid, yes_ask = q
                if not (yes_bid > 0 and yes_ask < 1):
                    continue
                up, down = yes_ask, 1 - yes_bid
                side_up = up >= down
                ask = max(up, down)
                if not 0 < ask < 1:
                    continue
                live.append((ticker, strike, ask, side_up))
            if not live:
                continue

            # The rung is chosen HERE, by a rule fixed in advance, and by a
            # quantity observable at entry. Never by estimated edge.
            if rule == "nearest":
                pick = min(live, key=lambda r: abs(r[1] - spot))
            else:
                pick = min(live, key=lambda r: abs(r[2] - target))
            ticker, strike, ask, side_up = pick

            norm = abs((strike - spot) / spot * 10_000) / horizon
            if norm < min_norm:
                continue

            won = (settle > strike) if side_up else (settle <= strike)
            chosen = (
                event, remaining, ask, norm,
                (1.0 if won else 0.0) - ask - kalshi_fee_charged(ask, 1),
            )
            break
        if chosen:
            out.append(chosen)
    return out


def report(label: str, rows: list, min_n: int = 60) -> None:
    if len(rows) < min_n:
        print(f"{label:>34} {len(rows):>6}  too few")
        return
    groups: dict[str, list[float]] = defaultdict(list)
    for event, _remaining, _ask, _norm, net in rows:
        # Cluster by DAY: consecutive hours share the same trend and are not
        # independent draws, exactly as consecutive minutes are not inside a
        # 15-minute window.
        groups[event.split("-")[1][:7]].append(net)
    n = sum(len(v) for v in groups.values())
    (low, high), _p = cluster_bootstrap(groups, 3000, 7)
    mean = sum(sum(v) for v in groups.values()) / n
    wins = sum(1 for r in rows if r[4] > 0)
    print(
        f"{label:>34} {n:>6} {wins / n:>7.1%} {mean:>+9.4f} "
        f"[{low:+.4f}, {high:+.4f}]" + ("  <--" if low > 0 else "")
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default="data/market_data_kxbtcd.db")
    parser.add_argument("--target", type=float, default=0.89)
    parser.add_argument("--lo-min", type=int, default=10)
    parser.add_argument("--hi-min", type=int, default=50)
    parser.add_argument("--min-norm", type=float, default=0.0)
    parser.add_argument("--holdout-days", type=int, default=7)
    args = parser.parse_args()

    events, klines, quotes = load(args.db)
    print(f"{len(events)} settled hourly events, {len(quotes)} rungs with quotes")
    if not events or not quotes:
        sys.exit("nothing to measure - run scripts/fetch_hourly.py first")

    cutoff = sorted(e["close_ms"] for e in events.values())[
        -max(1, args.holdout_days * 24)
    ]
    explore = {k: v for k, v in events.items() if v["close_ms"] < cutoff}
    holdout = {k: v for k, v in events.items() if v["close_ms"] >= cutoff}
    print(f"explore: {len(explore)} events   holdout: {len(holdout)} events")
    print()
    print(f"{'':>34} {'n':>6} {'win':>7} {'NET edge':>9} {'95% CI':>22}")

    for name, rule in (("NEAREST (control)", "nearest"),
                       (f"FAVOURITE (ask ~ {args.target})", "favourite")):
        for portion, label in ((explore, "explore"), (holdout, "HOLDOUT")):
            rows = entries(
                portion, klines, quotes, rule=rule, target=args.target,
                lo_min=args.lo_min, hi_min=args.hi_min, min_norm=args.min_norm,
            )
            report(f"{name} - {label}", rows)
        print()


if __name__ == "__main__":
    main()
