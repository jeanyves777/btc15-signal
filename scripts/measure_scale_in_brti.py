"""The operator's CONDITIONED scale-in, tested on BRTI.

`measure_scale_in.py` showed the unconditioned add is backwards: the resting
order fills on 97.5% of eventual losers and only 30% of eventual winners,
because a position that never gets cheaper wins 99.2%. This asks the fair
question about the design as actually specified - does requiring the evidence
to still hold turn a bad add into a good one?

The operator's conditions, as testable predicates at the moment the dipped
price is available:

  * Kalshi BRTI still supports the original direction   -> side unchanged
  * no target crossing since entry                      -> BRTI never crossed
  * momentum remains aligned                            -> direction * mom > 0
  * target distance has not collapsed                   -> distance >= floor
  * current ask meaningfully cheaper than the first fill -> the dip itself
  * reference data is fresh                             -> always true offline

Everything is computed from Kalshi: BRTI for the evidence, the Kalshi book for
the prices. Nothing reads Binance.

The test is the same one that decides every exit question in this archive: the
add is worth making only if the position wins MORE often than the dipped price
implies. A filter that merely picks better-looking losers does not help.

    python scripts/measure_scale_in_brti.py --fetch      # build the path cache
    python scripts/measure_scale_in_brti.py              # measure
"""

import argparse
import asyncio
import random
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from btc15_signal.brti import KalshiBRTI, features_from_series  # noqa: E402
from btc15_signal.config import Settings  # noqa: E402
from btc15_signal.strategy import EntryRule  # noqa: E402
from btc15_signal.validation import kalshi_fee_charged as fee  # noqa: E402

DB = "data/brti_history.db"
# Entry window plus the whole life of the position, one point a minute.
PATH_SECONDS = tuple(range(660, 59, -60))

SCHEMA = """
CREATE TABLE IF NOT EXISTS brti_path_points (
    ticker TEXT NOT NULL, remaining_s INTEGER NOT NULL,
    close_ms INTEGER NOT NULL, target REAL NOT NULL, result TEXT,
    brti_value REAL NOT NULL, signed_distance_bps REAL NOT NULL,
    brti_momentum_bps REAL, brti_normalized_distance REAL, brti_side TEXT,
    PRIMARY KEY (ticker, remaining_s)
);
CREATE TABLE IF NOT EXISTS brti_path_fetched (
    ticker TEXT PRIMARY KEY, fetched_at INTEGER NOT NULL
);
"""


def ci(values: list[float], draws: int = 4000, seed: int = 11):
    if len(values) < 2:
        return 0.0, 0.0
    rng = random.Random(seed)
    means = sorted(
        sum(rng.choice(values) for _ in values) / len(values) for _ in range(draws)
    )
    return means[int(0.025 * draws)], means[int(0.975 * draws) - 1]


def net(price: float, won: bool) -> float:
    return (1.0 if won else 0.0) - price - fee(price, 1)


async def fetch(limit: int, stride: int) -> None:
    settings = Settings()
    out = sqlite3.connect(DB)
    out.executescript(SCHEMA)
    done = {r[0] for r in out.execute("SELECT ticker FROM brti_path_fetched")}

    src = sqlite3.connect("file:data/market_data.db?mode=ro", uri=True)
    src.row_factory = sqlite3.Row
    rows = [
        dict(r) for r in src.execute(
            "SELECT ticker, close_ms, floor_strike, result FROM markets "
            "WHERE result IN ('yes','no') AND floor_strike IS NOT NULL "
            "ORDER BY open_ms"
        )
    ]
    rows = rows[::stride] if stride > 1 else rows
    todo = [r for r in rows if r["ticker"] not in done][:limit or None]
    print(f"{len(todo)} markets to fetch ({len(done)} cached)")

    client = KalshiBRTI(settings.kalshi_base_url, timeout=25)
    try:
        for index, market in enumerate(todo, 1):
            event = market["ticker"].rsplit("-", 1)[0]
            try:
                series = await client.series(event)
            except Exception:  # noqa: BLE001
                continue
            if not series:
                continue
            series.sort()
            batch = []
            for remaining in PATH_SECONDS:
                cutoff = market["close_ms"] - remaining * 1000
                upto = [(t, v) for t, v in series if t <= cutoff]
                if len(upto) < 120:
                    continue
                f = features_from_series(event, upto, market["floor_strike"], cutoff)
                if f is None:
                    continue
                batch.append((
                    market["ticker"], remaining, market["close_ms"],
                    market["floor_strike"], market["result"], f.value,
                    f.signed_distance_bps, f.brti_momentum_bps,
                    f.brti_normalized_distance, f.side,
                ))
            if batch:
                out.executemany(
                    "INSERT OR REPLACE INTO brti_path_points VALUES (?,?,?,?,?,?,?,?,?,?)",
                    batch,
                )
            out.execute(
                "INSERT OR REPLACE INTO brti_path_fetched VALUES (?,?)",
                (market["ticker"], int(time.time() * 1000)),
            )
            out.commit()
            if index % 100 == 0:
                print(f"  {index}/{len(todo)}", flush=True)
    finally:
        await client.close()
        out.close()


def measure(dip: float, distance_floor: float) -> None:
    rule = EntryRule.load(Path("strategy.json"))
    db = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    paths: dict[str, list[dict]] = {}
    for row in db.execute(
        "SELECT * FROM brti_path_points ORDER BY ticker, remaining_s DESC"
    ):
        paths.setdefault(row["ticker"], []).append(dict(row))
    if not paths:
        print("no path points - run with --fetch first")
        return

    market = sqlite3.connect("file:data/market_data.db?mode=ro", uri=True)
    market.row_factory = sqlite3.Row
    quotes: dict[tuple[str, int], tuple[float, float]] = {}
    for row in market.execute(
        "SELECT ticker, end_period_ts, yes_bid_close, yes_ask_close "
        "FROM contract_candles WHERE yes_bid_close IS NOT NULL "
        "AND yes_ask_close IS NOT NULL"
    ):
        quotes[(row["ticker"], row["end_period_ts"])] = (
            row["yes_bid_close"], row["yes_ask_close"]
        )

    def ask_at(ticker: str, close_ms: int, remaining: int, side: str):
        quote = quotes.get((ticker, (close_ms - remaining * 1000) // 1000))
        if quote is None:
            return None
        yes_bid, yes_ask = quote
        price = yes_ask if side == "UP" else round(1 - yes_bid, 4)
        return price if 0 < price < 1 else None

    plain, conditioned, blocked = [], [], []
    entries = 0
    for ticker, points in paths.items():
        entry = None
        for point in points:
            if not 360 <= point["remaining_s"] <= 660:
                continue
            price = ask_at(ticker, point["close_ms"], point["remaining_s"],
                           point["brti_side"])
            if price is None or not rule.min_ask <= price <= rule.max_ask:
                continue
            entry = (point, price)
            break
        if entry is None:
            continue
        point, fill = entry
        side, result = point["brti_side"], point["result"]
        won = (result == "yes") if side == "UP" else (result == "no")
        entries += 1
        direction = 1 if side == "UP" else -1

        crossed = False
        for later in points:
            if later["remaining_s"] >= point["remaining_s"]:
                continue
            # A crossing is permanent evidence, so once seen it stays seen.
            if later["brti_side"] != side:
                crossed = True
            price = ask_at(ticker, later["close_ms"], later["remaining_s"], side)
            if price is None or price > fill - dip:
                continue
            plain.append((price, won))
            ok = (
                not crossed
                and later["brti_side"] == side
                and direction * later["brti_momentum_bps"] > 0
                and later["brti_normalized_distance"] >= distance_floor
            )
            (conditioned if ok else blocked).append((price, won))
            break

    print(f"BRTI entries: {entries}   dip {dip * 100:.0f}c   "
          f"distance floor {distance_floor:g}")
    print(f"\n  {'cohort':>26} {'n':>6} {'add price':>10} {'win':>8} "
          f"{'edge vs price':>14} {'net/ct':>10} {'95% CI':>22}")
    for name, rows in (
        ("add fires, unconditioned", plain),
        ("add fires, CONDITIONS HOLD", conditioned),
        ("conditions block the add", blocked),
    ):
        if len(rows) < 25:
            print(f"  {name:>26} {len(rows):>6}   too few")
            continue
        mean_price = sum(p for p, _ in rows) / len(rows)
        win = sum(1 for _, w in rows if w) / len(rows)
        values = [net(p, w) for p, w in rows]
        low, high = ci(values)
        print(f"  {name:>26} {len(rows):>6} {mean_price:>10.3f} {win:>8.1%} "
              f"{win - mean_price:>+14.3f} {sum(values) / len(values):>+10.4f} "
              f"[{low:>+8.4f},{high:>+8.4f}]")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fetch", action="store_true")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--stride", type=int, default=3)
    parser.add_argument("--dip", type=float, default=0.10)
    parser.add_argument("--distance-floor", type=float, default=10.0)
    args = parser.parse_args()
    if args.fetch:
        asyncio.run(fetch(args.limit, args.stride))
    else:
        measure(args.dip, args.distance_floor)


if __name__ == "__main__":
    main()
