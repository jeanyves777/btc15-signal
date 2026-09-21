"""Fetch Binance 1-minute klines into a series cache, for research.

The Kalshi candles say what the contract was priced at; the klines say what the
underlying was doing. The strategy's distance and momentum gates are computed
from the second, so a study using only contract prices measures the raw
favourite-longshot bias and not the rule that is actually deployed.

    python scripts/fetch_klines.py --symbol ETHUSDT --into data/market_data_kxeth15m.db
"""

import argparse
import sqlite3
import time
from pathlib import Path

import httpx

SPOT = "https://data-api.binance.vision/api/v3/klines"


def ensure_schema(db: sqlite3.Connection) -> None:
    db.execute("""
        CREATE TABLE IF NOT EXISTS klines (
            open_time INTEGER PRIMARY KEY, open REAL, high REAL, low REAL,
            close REAL, volume REAL, taker_buy_volume REAL, trades INTEGER
        )""")
    db.commit()


def fetch(symbol: str, start_ms: int, end_ms: int) -> list[tuple]:
    """Every minute in the span. Binance caps a page at 1000, so it pages."""
    rows, cursor = [], start_ms
    with httpx.Client(timeout=60) as client:
        while cursor < end_ms:
            response = client.get(
                SPOT,
                params={
                    "symbol": symbol, "interval": "1m",
                    "startTime": cursor, "endTime": end_ms, "limit": 1000,
                },
            )
            if response.status_code == 429:
                print("  rate limited, waiting 10s", flush=True)
                time.sleep(10)
                continue
            response.raise_for_status()
            batch = response.json()
            if not batch:
                break
            for k in batch:
                rows.append((
                    int(k[0]), float(k[1]), float(k[2]), float(k[3]), float(k[4]),
                    float(k[5]), float(k[9]), int(k[8]),
                ))
            cursor = int(batch[-1][0]) + 60_000
            if len(rows) % 20_000 < 1000:
                print(f"  {len(rows)} klines...", flush=True)
            time.sleep(0.15)
    return rows


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--into", required=True)
    args = parser.parse_args()

    Path(args.into).parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(args.into)
    ensure_schema(db)

    # Cover the markets already stored, plus four hours before the earliest so
    # the trailing-context window has history to work with.
    span = db.execute(
        "SELECT MIN(open_ms), MAX(close_ms) FROM markets WHERE result IN ('yes','no')"
    ).fetchone()
    if not span or not span[0]:
        raise SystemExit("no markets in that cache - fetch the series first")
    start, end = span[0] - 4 * 3_600_000, span[1]

    print(f"fetching {args.symbol} minutes covering the stored markets")
    rows = fetch(args.symbol, start, end)
    db.executemany("INSERT OR REPLACE INTO klines VALUES (?,?,?,?,?,?,?,?)", rows)
    db.commit()
    print(f"{len(rows)} klines stored in {args.into}")


if __name__ == "__main__":
    main()
