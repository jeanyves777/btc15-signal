"""Populate the local market-data cache used by strategy validation."""

import argparse
import sys
from datetime import UTC, datetime, timedelta

from btc15_signal.datasource import DataCache


def main() -> None:
    parser = argparse.ArgumentParser(description="Sync Kalshi and Binance history into the cache")
    parser.add_argument("--days", type=int, default=70)
    parser.add_argument("--cache", default="data/market_data.db")
    parser.add_argument("--workers", type=int, default=6)
    parser.add_argument("--skip-candles", action="store_true")
    args = parser.parse_args()

    end = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
    start = end - timedelta(days=args.days)
    start_ms, end_ms = int(start.timestamp() * 1000), int(end.timestamp() * 1000)

    cache = DataCache(args.cache)
    print(f"Syncing {start:%Y-%m-%d} to {end:%Y-%m-%d} UTC", flush=True)

    added = cache.sync_markets(start_ms, end_ms)
    print(f"markets: +{added} rows", flush=True)

    added = cache.sync_klines(start_ms, end_ms)
    print(f"klines: +{added} rows", flush=True)

    if not args.skip_candles:
        tickers = [market.ticker for market in cache.markets(start_ms, end_ms)]
        print(f"contract candles for {len(tickers)} tickers", flush=True)
        added = cache.sync_contract_candles(tickers, workers=args.workers, progress=500)
        print(f"contract candles: +{added} rows", flush=True)

    print("coverage:", cache.coverage(), flush=True)


if __name__ == "__main__":
    sys.exit(main())
