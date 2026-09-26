"""Fetch individual public trades for settled 15-minute markets.

WHY THIS IS WORTH HAVING when the book and the reference are already stored.
Everything the strategy currently reads is PRICE: BRTI gives distance,
momentum, volatility and the level measures; the candles give bid, ask and
traded price. None of it says who was actually transacting or which way they
leaned. A trade record does:

    taker_side          which outcome the AGGRESSOR bought
    taker_book_side     whether they lifted the ask or hit the bid
    count_fp            how much
    created_time        exactly when

That is order flow, and it is independent of everything already measured -
which matters more than another threshold on the same numbers.

THE BUG THIS FILE USED TO HAVE, recorded because it produced a finding that
survived a day-clustered interval and every confound check before it died.
The fetch paginated from the newest trade with a 25-page cap. A 15-minute BTC
market trades ~48 times a SECOND, so 25,000 trades is the last ~9 minutes and
the cap silently discarded everything earlier - including the minutes before
the bot's entry, which is the only period an entry decision can be measured
against. 137 of 172 markets hit the cap. Windows that fell off the end came
back EMPTY, and "no trades before entry" read as an illiquid market rather
than as missing data: 49 such markets won 55% against 94% for the rest, held
on all five days, and the day-clustered interval excluded zero. It was a
plotting of my own truncation against time-of-day.

The lesson is not "cap higher". It is that an absent measurement and a
measured absence must never arrive in the same shape - so this now fetches a
BOUNDED window with min_ts/max_ts and stores the bounds it actually covered,
letting a reader tell "quiet market" from "never fetched".

    python scripts/fetch_trades.py --tickers-from btc15.db --window-min 12
    python scripts/fetch_trades.py --series KXBTC15M --days 7
"""

import argparse
import sqlite3
import sys
import time
from pathlib import Path

import httpx

BASE = "https://external-api.kalshi.com/trade-api/v2"
DEFAULT_OUT = "data/trades.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS trades (
    trade_id TEXT PRIMARY KEY,
    ticker TEXT NOT NULL,
    created_ms INTEGER NOT NULL,
    count REAL,
    yes_price REAL,
    no_price REAL,
    taker_side TEXT,
    taker_book_side TEXT,
    is_block INTEGER
);
CREATE INDEX IF NOT EXISTS trades_ticker ON trades(ticker, created_ms);
CREATE TABLE IF NOT EXISTS trades_fetched (
    ticker TEXT PRIMARY KEY, fetched_at INTEGER NOT NULL, trades INTEGER NOT NULL
);
"""

# The bounds each market was actually fetched over. Without this a caller
# cannot distinguish a quiet window from one that was never requested, which
# is exactly the confusion that produced a false finding.
COVERAGE = """
CREATE TABLE IF NOT EXISTS trades_coverage (
    ticker TEXT PRIMARY KEY,
    min_ms INTEGER NOT NULL,
    max_ms INTEGER NOT NULL,
    complete INTEGER NOT NULL,
    trades INTEGER NOT NULL
);
"""


def get(client, url, params, tries=6):
    for attempt in range(tries):
        response = client.get(url, params=params)
        if response.status_code == 429:
            time.sleep(1.5 * (attempt + 1))
            continue
        response.raise_for_status()
        return response
    response.raise_for_status()
    return response


def to_ms(value) -> int | None:
    """Kalshi returns an ISO-8601 instant; keep it in the units everything
    else here uses rather than storing a second format nobody parses."""
    if not value:
        return None
    import datetime as dt
    try:
        return int(dt.datetime.fromisoformat(
            value.replace("Z", "+00:00")).timestamp() * 1000)
    except ValueError:
        return None


def _float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def tickers_for(series: str, days: int) -> list[tuple[str, int | None]]:
    cutoff = time.time() - days * 86400
    out, cursor = [], None
    with httpx.Client(timeout=30) as client:
        while True:
            params = {"series_ticker": series, "status": "settled",
                      "limit": 200}
            if cursor:
                params["cursor"] = cursor
            payload = get(client, f"{BASE}/markets", params).json()
            batch = payload.get("markets") or []
            if not batch:
                break
            stop = False
            for m in batch:
                closed = to_ms(m.get("close_time"))
                if closed and closed / 1000 < cutoff:
                    stop = True
                    continue
                out.append((m["ticker"], closed))
            cursor = payload.get("cursor")
            if stop or not cursor:
                break
    return out


def tickers_from_db(path: str) -> list[tuple[str, int | None]]:
    """The markets the bot actually traded, each with the instant it decided.

    The decision instant is what the window is anchored to: flow after the
    entry cannot inform the entry, and flow nine minutes of trading later is
    a different market state."""
    d = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    return [(r[0], r[1]) for r in d.execute(
        "SELECT p.ticker, MIN(p.created_at) FROM trade_proposals p "
        "JOIN settlements s ON s.ticker = p.ticker "
        "WHERE p.fill_price IS NOT NULL GROUP BY p.ticker")]


def fetch(targets: list[tuple[str, int | None]], out_path: str,
          window_min: float, max_pages: int) -> None:
    Path("data").mkdir(exist_ok=True)
    out = sqlite3.connect(out_path)
    out.executescript(SCHEMA)
    out.executescript(COVERAGE)
    done = {r[0] for r in out.execute("SELECT ticker FROM trades_coverage")}
    todo = [t for t in targets if t[0] not in done]
    print(f"{len(todo)} markets to fetch ({len(done)} already bounded) "
          f"-> {out_path}")

    written = failed = incomplete = 0
    with httpx.Client(timeout=30) as client:
        for index, (ticker, anchor_ms) in enumerate(todo, 1):
            # Anchor the window on the decision and reach BACK. An unknown
            # anchor gets no window rather than a silently wrong one.
            if anchor_ms is None:
                print(f"  {ticker}: no anchor instant, skipped")
                continue
            hi_ms = int(anchor_ms)
            lo_ms = hi_ms - int(window_min * 60_000)
            rows, cursor, pages = [], None, 0
            try:
                while pages < max_pages:
                    params = {"ticker": ticker, "limit": 1000,
                              "min_ts": lo_ms // 1000,
                              "max_ts": hi_ms // 1000 + 1}
                    if cursor:
                        params["cursor"] = cursor
                    payload = get(
                        client, f"{BASE}/markets/trades", params).json()
                    batch = payload.get("trades") or []
                    for t in batch:
                        rows.append((
                            t.get("trade_id"), ticker,
                            to_ms(t.get("created_time")),
                            _float(t.get("count_fp")),
                            _float(t.get("yes_price_dollars")),
                            _float(t.get("no_price_dollars")),
                            t.get("taker_side"), t.get("taker_book_side"),
                            1 if t.get("is_block_trade") else 0,
                        ))
                    cursor = payload.get("cursor")
                    pages += 1
                    if not cursor or not batch:
                        break
            except Exception as exc:  # noqa: BLE001 - one market is not fatal
                failed += 1
                if failed <= 5:
                    print(f"  {ticker}: {type(exc).__name__}", flush=True)
                continue
            # `complete` is false when the page budget ran out before the
            # cursor did, i.e. the window is only PARTLY covered. A reader
            # that ignores this flag can repeat the original mistake.
            complete = 0 if cursor else 1
            if not complete:
                incomplete += 1
            rows = [r for r in rows if r[0] and r[2]]
            if rows:
                out.executemany(
                    "INSERT OR REPLACE INTO trades VALUES (?,?,?,?,?,?,?,?,?)",
                    rows)
            out.execute("INSERT OR REPLACE INTO trades_fetched VALUES (?,?,?)",
                        (ticker, int(time.time() * 1000), len(rows)))
            out.execute(
                "INSERT OR REPLACE INTO trades_coverage VALUES (?,?,?,?,?)",
                (ticker, lo_ms, hi_ms, complete, len(rows)))
            out.commit()
            written += len(rows)
            if index % 25 == 0:
                print(f"  {index}/{len(todo)}  {written} trades", flush=True)

    total = out.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
    bounded = out.execute(
        "SELECT COUNT(*) FROM trades_coverage").fetchone()[0]
    out.close()
    print(f"wrote {written} this run; {total} trades, {bounded} markets "
          f"bounded ({failed} failures, {incomplete} windows only partly "
          f"covered)")


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--series", default="KXBTC15M")
    p.add_argument("--days", type=int, default=7)
    p.add_argument("--tickers-from", default=None,
                   help="a live database; fetches only markets the bot traded")
    p.add_argument("--window-min", type=float, default=12.0,
                   help="minutes of flow BEFORE the decision instant")
    p.add_argument("--max-pages", type=int, default=60,
                   help="page budget per market; exceeding it marks the "
                        "window incomplete rather than truncating in silence")
    p.add_argument("--out", default=DEFAULT_OUT)
    args = p.parse_args()

    if args.tickers_from:
        targets = tickers_from_db(args.tickers_from)
        print(f"{len(targets)} markets the bot actually traded")
    else:
        targets = tickers_for(args.series, args.days)
        print(f"{len(targets)} settled {args.series} markets in "
              f"{args.days} days")
    fetch(targets, args.out, args.window_min, args.max_pages)


if __name__ == "__main__":
    main()
