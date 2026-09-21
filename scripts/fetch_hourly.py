"""Fetch settled KXBTCD hourly ladders for research. Read-only, no trading path.

Three phases, in order, because each needs the one before it:

  1. MARKETS  every settled rung. One `expiration_value` per event decides all
     188 rungs, so outcomes are derivable without fetching 188 results.
  2. KLINES   Binance minutes covering those events. Needed for spot, momentum
     and volatility at entry - the ladder alone cannot supply them.
  3. CANDLES  minute quotes, but ONLY for rungs near where spot actually was.
     Fetching all 188 rungs per event would be ~135k contracts over 30 days for
     data that is almost all pinned at 0.00/0.01 and carries no information.
     Which rungs are "near" is decided from the klines at the START of each
     hour, never from the settlement - selecting rungs by where the price
     ended up would leak the answer into the sample.

    python scripts/fetch_hourly.py --days 21
"""

import argparse
import sqlite3
import sys
import time
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from fetch_series import _float, _quote, _to_ms, get  # noqa: E402

BASE = "https://external-api.kalshi.com/trade-api/v2"
SERIES = "KXBTCD"


def ensure_schema(db: sqlite3.Connection) -> None:
    db.execute("""
        CREATE TABLE IF NOT EXISTS markets (
            ticker TEXT PRIMARY KEY, event_ticker TEXT, open_ms INTEGER,
            close_ms INTEGER, floor_strike REAL, result TEXT, status TEXT,
            expiration_value REAL, volume REAL, open_interest REAL
        )""")
    db.execute("""
        CREATE TABLE IF NOT EXISTS klines (
            open_time INTEGER PRIMARY KEY, open REAL, high REAL, low REAL,
            close REAL, volume REAL, taker_buy_volume REAL, trades INTEGER
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
    db.execute("CREATE INDEX IF NOT EXISTS idx_event ON markets (event_ticker)")
    db.commit()


def fetch_markets(db: sqlite3.Connection, days: int) -> int:
    cutoff = int(time.time()) - days * 86_400
    rows, cursor, pages = [], None, 0
    with httpx.Client(timeout=60) as client:
        while True:
            params = {"series_ticker": SERIES, "status": "settled", "limit": 1000}
            if cursor:
                params["cursor"] = cursor
            payload = get(client, f"{BASE}/markets", params).json()
            time.sleep(0.4)
            batch = payload.get("markets", [])
            if not batch:
                break
            oldest = None
            for m in batch:
                close_ms = _to_ms(m.get("close_time"))
                if close_ms is None:
                    continue
                oldest = close_ms
                if close_ms // 1000 < cutoff:
                    continue
                rows.append((
                    m["ticker"], m.get("event_ticker"), _to_ms(m.get("open_time")),
                    close_ms, _float(m.get("floor_strike")), m.get("result"),
                    m.get("status"), _float(m.get("expiration_value")),
                    _float(m.get("volume")) or 0.0,
                    _float(m.get("open_interest")) or 0.0,
                ))
            cursor = payload.get("cursor")
            pages += 1
            print(f"  page {pages}: {len(rows)} rungs kept", flush=True)
            if not cursor or (oldest and oldest // 1000 < cutoff):
                break
    db.executemany("INSERT OR REPLACE INTO markets VALUES (?,?,?,?,?,?,?,?,?,?)", rows)
    db.commit()
    return len(rows)


def fetch_klines(db: sqlite3.Connection, symbol: str = "BTCUSDT") -> int:
    span = db.execute(
        "SELECT MIN(open_ms), MAX(close_ms) FROM markets WHERE result IN ('yes','no')"
    ).fetchone()
    if not span or not span[0]:
        return 0
    start, end = span[0] - 4 * 3_600_000, span[1]
    rows, cursor = [], start
    with httpx.Client(timeout=60) as client:
        while cursor < end:
            response = client.get(
                "https://data-api.binance.vision/api/v3/klines",
                params={
                    "symbol": symbol, "interval": "1m",
                    "startTime": cursor, "endTime": end, "limit": 1000,
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
            time.sleep(0.15)
    db.executemany("INSERT OR REPLACE INTO klines VALUES (?,?,?,?,?,?,?,?)", rows)
    db.commit()
    return len(rows)


def near_spot_tickers(db: sqlite3.Connection, window: float) -> list[tuple]:
    """Rungs within `window` dollars of spot AT THE HOUR'S OPEN.

    Anchored to the open, not to the settlement. Choosing rungs by where the
    price finished would quietly select the sample on the outcome.
    """
    klines = {
        r[0] // 60_000: r[1]
        for r in db.execute("SELECT open_time, close FROM klines")
    }
    picked = []
    for event, open_ms, close_ms in db.execute(
        "SELECT event_ticker, MIN(open_ms), MIN(close_ms) FROM markets "
        "WHERE result IN ('yes','no') GROUP BY event_ticker"
    ):
        spot = klines.get(open_ms // 60_000)
        if spot is None:
            continue
        for ticker, strike in db.execute(
            "SELECT ticker, floor_strike FROM markets WHERE event_ticker = ? "
            "AND floor_strike IS NOT NULL",
            (event,),
        ):
            if abs(strike - spot) <= window:
                picked.append((ticker, open_ms, close_ms))
    return picked


def fetch_candles(db: sqlite3.Connection, tickers: list[tuple], size: int = 20) -> int:
    total = 0
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
            rows = []
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
            db.executemany(
                "INSERT OR REPLACE INTO contract_candles VALUES ("
                + ",".join("?" * 17) + ")",
                rows,
            )
            db.commit()
            total += len(rows)
            print(
                f"  {start + len(batch)}/{len(tickers)} rungs, {total} candles",
                flush=True,
            )
    return total


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=21)
    parser.add_argument("--window", type=float, default=1500.0)
    parser.add_argument("--out", default="data/market_data_kxbtcd.db")
    args = parser.parse_args()

    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(args.out)
    ensure_schema(db)

    print(f"1/3 settled {SERIES} rungs, last {args.days} days")
    print(f"    {fetch_markets(db, args.days)} rungs")
    events = db.execute(
        "SELECT COUNT(DISTINCT event_ticker) FROM markets WHERE result IN ('yes','no')"
    ).fetchone()[0]
    print(f"    {events} hourly events")

    print("2/3 Binance minutes")
    print(f"    {fetch_klines(db)} klines")

    print(f"3/3 candles for rungs within ${args.window:,.0f} of spot at the open")
    tickers = near_spot_tickers(db, args.window)
    print(f"    {len(tickers)} rungs selected "
          f"({len(tickers) / max(events, 1):.1f} per event)")
    print(f"    {fetch_candles(db, tickers)} candles stored in {args.out}")


if __name__ == "__main__":
    main()
