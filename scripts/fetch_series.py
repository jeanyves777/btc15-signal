"""Fetch settled markets and minute candles for ANY Kalshi 15-minute series.

Research only. Kept separate from `datasource.py`, which the live trading path
imports: a market this has never traded is exactly the wrong place to risk a
change to the code that places orders.

Writes the same schema as `data/market_data.db` so every measurement already
written against BTC runs unchanged against ETH or SOL.

    python scripts/fetch_series.py --series KXETH15M --days 30
"""

import argparse
import sqlite3
import sys
import time
from pathlib import Path

import httpx

BASE = "https://external-api.kalshi.com/trade-api/v2"


def get(client: httpx.Client, url: str, params: dict, tries: int = 6):
    """GET with backoff. Kalshi rate-limits a long backfill hard, and an
    unhandled 429 halfway through leaves a partial database that silently
    measures a shorter period than it claims to."""
    delay = 2.0
    for attempt in range(tries):
        response = client.get(url, params=params)
        if response.status_code != 429:
            response.raise_for_status()
            return response
        if attempt == tries - 1:
            response.raise_for_status()
        print(f"  rate limited, waiting {delay:.0f}s", flush=True)
        time.sleep(delay)
        delay *= 2
    raise RuntimeError("unreachable")


def _float(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _quote(candle: dict, group: str, field: str) -> float | None:
    block = candle.get(group) or {}
    raw = block.get(f"{field}_dollars", block.get(field))
    value = _float(raw)
    if value is None:
        return None
    # Kalshi returns cents on some fields and dollars on others.
    return value / 100 if value > 1.5 else value


def ensure_schema(db: sqlite3.Connection) -> None:
    db.execute("""
        CREATE TABLE IF NOT EXISTS markets (
            ticker TEXT PRIMARY KEY, open_ms INTEGER, close_ms INTEGER,
            floor_strike REAL, result TEXT, status TEXT, expiration_value REAL,
            settlement_ts INTEGER, volume REAL, open_interest REAL
        )""")
    db.execute("""
        CREATE TABLE IF NOT EXISTS contract_candles (
            ticker TEXT, end_period_ts INTEGER,
            yes_bid_open REAL, yes_bid_high REAL, yes_bid_low REAL, yes_bid_close REAL,
            yes_ask_open REAL, yes_ask_high REAL, yes_ask_low REAL, yes_ask_close REAL,
            price_open REAL, price_high REAL, price_low REAL, price_close REAL,
            price_mean REAL, volume REAL, open_interest REAL,
            PRIMARY KEY (ticker, end_period_ts)
        )""")
    db.commit()


def fetch_markets(series: str, days: int) -> list[tuple]:
    """Every settled market in the window, paged."""
    cutoff = int(time.time()) - days * 86_400
    rows, cursor = [], None
    with httpx.Client(timeout=60) as client:
        while True:
            params = {"series_ticker": series, "status": "settled", "limit": 200}
            if cursor:
                params["cursor"] = cursor
            payload = get(client, f"{BASE}/markets", params).json()
            time.sleep(0.4)  # stay under the limit rather than recover from it
            batch = payload.get("markets", [])
            if not batch:
                break
            oldest = None
            for m in batch:
                close = m.get("close_time") or m.get("close_ts")
                close_ms = _to_ms(close)
                if close_ms is None:
                    continue
                oldest = close_ms
                if close_ms // 1000 < cutoff:
                    continue
                rows.append((
                    m["ticker"], _to_ms(m.get("open_time")), close_ms,
                    _float(m.get("floor_strike")), m.get("result"), m.get("status"),
                    _float(m.get("expiration_value")), None,
                    _float(m.get("volume")) or 0.0,
                    _float(m.get("open_interest")) or 0.0,
                ))
            cursor = payload.get("cursor")
            print(f"  {len(rows)} settled so far...", flush=True)
            if not cursor or (oldest and oldest // 1000 < cutoff):
                break
    return rows


def _to_ms(value) -> int | None:
    if value is None:
        return None
    if isinstance(value, int | float):
        return int(value) * 1000 if value < 10**12 else int(value)
    try:
        from datetime import datetime

        return int(
            datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp() * 1000
        )
    except ValueError:
        return None


def fetch_candles(tickers: list[tuple], size: int = 20) -> list[tuple]:
    rows = []
    with httpx.Client(timeout=90) as client:
        for start in range(0, len(tickers), size):
            batch = tickers[start : start + size]
            lo = min(t[1] for t in batch if t[1]) // 1000
            hi = max(t[2] for t in batch if t[2]) // 1000
            try:
                payload = get(
                    client,
                    f"{BASE}/markets/candlesticks",
                    {
                        "market_tickers": ",".join(t[0] for t in batch),
                        "start_ts": lo, "end_ts": hi, "period_interval": 1,
                    },
                ).json()
                time.sleep(0.4)
            except httpx.HTTPError as exc:
                print(f"  batch failed: {type(exc).__name__}", flush=True)
                continue
            for entry in payload.get("markets", []):
                ticker = entry["market_ticker"]
                for candle in entry.get("candlesticks", []):
                    rows.append((
                        ticker, int(candle["end_period_ts"]),
                        _quote(candle, "yes_bid", "open"), _quote(candle, "yes_bid", "high"),
                        _quote(candle, "yes_bid", "low"), _quote(candle, "yes_bid", "close"),
                        _quote(candle, "yes_ask", "open"), _quote(candle, "yes_ask", "high"),
                        _quote(candle, "yes_ask", "low"), _quote(candle, "yes_ask", "close"),
                        _quote(candle, "price", "open"), _quote(candle, "price", "high"),
                        _quote(candle, "price", "low"), _quote(candle, "price", "close"),
                        _quote(candle, "price", "mean"),
                        _float(candle.get("volume_fp")) or 0.0,
                        _float(candle.get("open_interest_fp")) or 0.0,
                    ))
            print(f"  {start + len(batch)}/{len(tickers)} markets, "
                  f"{len(rows)} candles", flush=True)
    return rows


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--series", required=True)
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--out")
    args = parser.parse_args()

    out = args.out or f"data/market_data_{args.series.lower()}.db"
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(out)
    ensure_schema(db)

    print(f"fetching {args.series}, last {args.days} days -> {out}")
    markets = fetch_markets(args.series, args.days)
    print(f"{len(markets)} settled markets")
    if not markets:
        sys.exit("no settled markets returned")
    db.executemany(
        "INSERT OR REPLACE INTO markets VALUES (?,?,?,?,?,?,?,?,?,?)", markets
    )
    db.commit()

    candles = fetch_candles([(m[0], m[1], m[2]) for m in markets])
    db.executemany(
        "INSERT OR REPLACE INTO contract_candles VALUES ("
        + ",".join("?" * 17) + ")",
        candles,
    )
    db.commit()
    print(f"{len(candles)} candles stored")


if __name__ == "__main__":
    main()
