"""Microstructure recorder: Kalshi order book and Binance book/flow.

Nothing in the cached history has depth. Kalshi's candlesticks carry only OHLC
of the bid and ask, Binance klines carry no book at all, and both backtests
hard-code `bid_imbalance = 0.0` - so the one microstructure feature the live
model already consumes has never been validated against anything.

This process fixes the input side of that. It is deliberately a *separate*
process from the trading service: it only reads public endpoints, and if it
falls over or gets rate limited the service is untouched.

It stores the raw book alongside the venue's own quoted top-of-book in the same
row. The two should agree; recording both is what lets that be checked offline
instead of assumed. A mapping guessed today and found wrong in a month would
invalidate every feature built on it.
"""

import argparse
import json
import sqlite3
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import httpx

from .datasource import KALSHI_URL, SERIES, RateLimiter

BINANCE = "https://data-api.binance.vision"

SCHEMA = """
CREATE TABLE IF NOT EXISTS book_snapshots (
    captured_ms INTEGER NOT NULL,
    ticker TEXT NOT NULL,
    open_ms INTEGER NOT NULL,
    close_ms INTEGER NOT NULL,
    remaining_s INTEGER NOT NULL,
    strike REAL,
    -- Kalshi quoted top of book, straight from /markets
    yes_bid REAL, yes_ask REAL, yes_bid_size REAL, yes_ask_size REAL,
    volume REAL, open_interest REAL,
    -- raw depth, exactly as returned, so the mapping can be re-derived later
    book_yes TEXT, book_no TEXT,
    -- Binance top of book and depth summary
    btc_bid REAL, btc_ask REAL, btc_bid_qty REAL, btc_ask_qty REAL,
    depth_bid_qty REAL, depth_ask_qty REAL, depth_levels INTEGER,
    -- aggregate trade flow since the previous snapshot
    trade_count INTEGER, buy_volume REAL, sell_volume REAL, vwap REAL,
    -- when each venue call actually returned. The quote and the book come from
    -- two requests, and in a fast 15-minute market the drift between them is
    -- real: measured book-vs-quote offsets spread 1-6 cents purely from timing.
    -- Features must know how stale each half of a row is.
    quote_ms INTEGER, book_ms INTEGER, binance_ms INTEGER,
    PRIMARY KEY (ticker, captured_ms)
);
CREATE INDEX IF NOT EXISTS book_time ON book_snapshots(captured_ms);
"""


@dataclass(frozen=True)
class Snapshot:
    captured_ms: int
    ticker: str


def _num(value) -> float | None:
    if value is None:
        return None
    if isinstance(value, str):
        value = value.replace(",", "").strip()
        if not value:
            return None
    return float(value)


def _levels(raw) -> list[tuple[float, float]]:
    """Kalshi returns [[price, size], ...] as strings under *_fp keys."""
    out = []
    for entry in raw or []:
        if isinstance(entry, list | tuple) and len(entry) >= 2:
            price, size = _num(entry[0]), _num(entry[1])
            if price is not None and size is not None:
                out.append((price, size))
    return out


class Recorder:
    def __init__(self, path: str = "data/microstructure.db") -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path)
        self.db.executescript(SCHEMA)
        self.db.commit()
        self.limiter = RateLimiter(per_second=3.0)
        self.last_trade_id: int | None = None

    def capture(self, client: httpx.Client, kalshi_url: str) -> str | None:
        now_ms = int(time.time() * 1000)

        self.limiter.acquire()
        quote_ms = int(time.time() * 1000)
        markets = client.get(
            kalshi_url.rstrip("/") + "/markets",
            params={"series_ticker": SERIES, "status": "open", "limit": 20},
        ).json().get("markets", [])
        live = [
            m
            for m in markets
            if _iso(m["open_time"]) <= now_ms < _iso(m["close_time"])
            and m.get("floor_strike") is not None
        ]
        if not live:
            return None
        market = live[0]
        ticker = market["ticker"]

        self.limiter.acquire()
        book_ms = int(time.time() * 1000)
        book = (
            client.get(kalshi_url.rstrip("/") + f"/markets/{ticker}/orderbook",
                       params={"depth": 32})
            .json()
            .get("orderbook_fp", {})
        )
        yes_levels = _levels(book.get("yes_dollars"))
        no_levels = _levels(book.get("no_dollars"))

        binance_ms = int(time.time() * 1000)
        ticker_data = client.get(
            BINANCE + "/api/v3/ticker/bookTicker", params={"symbol": "BTCUSDT"}
        ).json()
        depth = client.get(
            BINANCE + "/api/v3/depth", params={"symbol": "BTCUSDT", "limit": 100}
        ).json()
        trades = client.get(
            BINANCE + "/api/v3/aggTrades", params={"symbol": "BTCUSDT", "limit": 500}
        ).json()

        fresh = [t for t in trades if self.last_trade_id is None or t["a"] > self.last_trade_id]
        if trades:
            self.last_trade_id = max(t["a"] for t in trades)
        buy = sum(float(t["q"]) for t in fresh if not t["m"])
        sell = sum(float(t["q"]) for t in fresh if t["m"])
        notional = sum(float(t["q"]) * float(t["p"]) for t in fresh)
        quantity = buy + sell

        self.db.execute(
            "INSERT OR REPLACE INTO book_snapshots VALUES "
            "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                now_ms,
                ticker,
                _iso(market["open_time"]),
                _iso(market["close_time"]),
                (_iso(market["close_time"]) - now_ms) // 1000,
                _num(market.get("floor_strike")),
                _num(market.get("yes_bid_dollars")),
                _num(market.get("yes_ask_dollars")),
                _num(market.get("yes_bid_size_fp")),
                _num(market.get("yes_ask_size_fp")),
                _num(market.get("volume_fp")),
                _num(market.get("open_interest_fp")),
                json.dumps(yes_levels),
                json.dumps(no_levels),
                _num(ticker_data.get("bidPrice")),
                _num(ticker_data.get("askPrice")),
                _num(ticker_data.get("bidQty")),
                _num(ticker_data.get("askQty")),
                sum(float(q) for _, q in depth.get("bids", [])),
                sum(float(q) for _, q in depth.get("asks", [])),
                len(depth.get("bids", [])),
                len(fresh),
                buy,
                sell,
                (notional / quantity) if quantity else None,
                quote_ms,
                book_ms,
                binance_ms,
            ),
        )
        self.db.commit()
        return ticker

    def coverage(self) -> dict:
        row = self.db.execute(
            "SELECT COUNT(*), COUNT(DISTINCT ticker), MIN(captured_ms), MAX(captured_ms) "
            "FROM book_snapshots"
        ).fetchone()
        return {
            "snapshots": row[0],
            "markets": row[1],
            "first": _stamp(row[2]),
            "last": _stamp(row[3]),
        }


def _iso(value: str) -> int:
    return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() * 1000)


def _stamp(value: int | None) -> str | None:
    return None if value is None else datetime.fromtimestamp(value / 1000, UTC).isoformat()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Record Kalshi and Binance microstructure")
    parser.add_argument("--db", default="data/microstructure.db")
    parser.add_argument("--every", type=float, default=15.0, help="seconds between snapshots")
    parser.add_argument("--kalshi-url", default=KALSHI_URL)
    parser.add_argument("--max-minutes", type=float, default=0, help="0 runs forever")
    return parser.parse_args()


def run() -> None:
    args = parse_args()
    recorder = Recorder(args.db)
    deadline = time.time() + args.max_minutes * 60 if args.max_minutes else None
    print(f"recording every {args.every:g}s into {args.db}", flush=True)
    errors = 0
    with httpx.Client(timeout=20) as client:
        while deadline is None or time.time() < deadline:
            started = time.time()
            try:
                ticker = recorder.capture(client, args.kalshi_url)
                errors = 0
                if ticker and recorder.coverage()["snapshots"] % 20 == 0:
                    print(f"  {recorder.coverage()}", flush=True)
            except Exception as error:  # noqa: BLE001 - a recorder must not die
                errors += 1
                print(f"  capture failed ({type(error).__name__}), backing off", flush=True)
                time.sleep(min(60.0, 2.0**errors))
            time.sleep(max(0.0, args.every - (time.time() - started)))
    print(f"done: {recorder.coverage()}", flush=True)


if __name__ == "__main__":
    run()


def measure_book_offset(db_path: str = "data/microstructure.db") -> dict:
    """Derive the book-to-quote relationship from recorded data, never assume it.

    Kalshi's `orderbook_fp` prices do not line up with the `yes_bid_dollars` /
    `yes_ask_dollars` the same endpoint reports for the top of book. Hardcoding
    a guessed correction would silently poison every depth feature built on it,
    and the error would be invisible because both numbers look plausible.

    Each recorded snapshot stores the raw book AND the venue's own quote, so the
    offset is measurable. This reports the distribution; a feature should only
    use it if it is tight and stable.
    """
    db = sqlite3.connect(db_path)
    rows = db.execute(
        "SELECT yes_bid, yes_ask, book_yes, book_no FROM book_snapshots "
        "WHERE yes_bid IS NOT NULL AND yes_ask IS NOT NULL"
    ).fetchall()
    bid_offsets, ask_offsets = [], []
    for yes_bid, yes_ask, raw_yes, raw_no in rows:
        yes = [p for p, _ in json.loads(raw_yes or "[]")]
        no = [p for p, _ in json.loads(raw_no or "[]")]
        if yes:
            bid_offsets.append(round(yes_bid - max(yes), 4))
        if no:
            ask_offsets.append(round(yes_ask - (1 - max(no)), 4))

    def describe(values: list[float]) -> dict:
        if not values:
            return {"n": 0}
        ordered = sorted(values)
        mode = max(set(values), key=values.count)
        return {
            "n": len(values),
            "median": ordered[len(ordered) // 2],
            "mode": mode,
            "mode_share": round(values.count(mode) / len(values), 4),
            "min": ordered[0],
            "max": ordered[-1],
        }

    gaps = [
        row[0]
        for row in db.execute(
            "SELECT book_ms - quote_ms FROM book_snapshots "
            "WHERE book_ms IS NOT NULL AND quote_ms IS NOT NULL"
        )
    ]
    return {
        "bid_offset": describe(bid_offsets),
        "ask_offset": describe(ask_offsets),
        "quote_to_book_gap_ms": describe([float(g) for g in gaps]),
        "note": (
            "A spread of offsets rather than a constant means the difference is "
            "drift between two requests, not a price convention. Do not correct "
            "for it; use the book for depth and the quote for the touch, and let "
            "the gap column say how comparable they are."
        ),
    }
