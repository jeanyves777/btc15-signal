"""Cached, resumable market-data source.

Every validation run reads from a local SQLite cache instead of re-hitting the
Kalshi and Binance APIs. That makes validation reproducible (the same bytes
produce the same verdict), fast enough to run walk-forward and permutation
tests, and explicit about which data a verdict was based on.
"""

import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx

KALSHI_URL = "https://api.elections.kalshi.com/trade-api/v2"
BINANCE_URL = "https://data-api.binance.vision"
SERIES = "KXBTC15M"
SYMBOL = "BTCUSDT"
TICKER_BATCH = 20

SCHEMA = """
CREATE TABLE IF NOT EXISTS markets (
    ticker TEXT PRIMARY KEY,
    open_ms INTEGER NOT NULL,
    close_ms INTEGER NOT NULL,
    floor_strike REAL NOT NULL,
    result TEXT,
    status TEXT,
    expiration_value REAL,
    settlement_ts INTEGER,
    volume REAL,
    open_interest REAL
);
CREATE INDEX IF NOT EXISTS markets_open ON markets(open_ms);

CREATE TABLE IF NOT EXISTS contract_candles (
    ticker TEXT NOT NULL,
    end_period_ts INTEGER NOT NULL,
    yes_bid_open REAL, yes_bid_high REAL, yes_bid_low REAL, yes_bid_close REAL,
    yes_ask_open REAL, yes_ask_high REAL, yes_ask_low REAL, yes_ask_close REAL,
    price_open REAL, price_high REAL, price_low REAL, price_close REAL, price_mean REAL,
    volume REAL, open_interest REAL,
    PRIMARY KEY (ticker, end_period_ts)
);

CREATE TABLE IF NOT EXISTS klines (
    open_time INTEGER PRIMARY KEY,
    open REAL, high REAL, low REAL, close REAL,
    volume REAL, taker_buy_volume REAL, trades INTEGER
);

CREATE TABLE IF NOT EXISTS fetched_days (
    kind TEXT NOT NULL,
    day TEXT NOT NULL,
    fetched_at INTEGER NOT NULL,
    rows INTEGER NOT NULL,
    PRIMARY KEY (kind, day)
);

CREATE TABLE IF NOT EXISTS candles_fetched (
    ticker TEXT PRIMARY KEY,
    fetched_at INTEGER NOT NULL,
    rows INTEGER NOT NULL
);
"""


@dataclass(frozen=True)
class Candle:
    open_time: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    taker_buy_volume: float


@dataclass(frozen=True)
class Market:
    ticker: str
    open_ms: int
    close_ms: int
    floor_strike: float
    result: str | None
    status: str | None
    expiration_value: float | None
    volume: float


@dataclass(frozen=True)
class ContractCandle:
    end_period_ts: int
    yes_bid_open: float | None
    yes_bid_high: float | None
    yes_bid_low: float | None
    yes_bid_close: float | None
    yes_ask_open: float | None
    yes_ask_high: float | None
    yes_ask_low: float | None
    yes_ask_close: float | None
    price_close: float | None
    volume: float
    open_interest: float


def iso_ms(value: str) -> int:
    return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() * 1000)


def _float(value) -> float | None:
    """Parse a Kalshi numeric field.

    Some fields (notably expiration_value) come back as display strings with
    thousands separators, e.g. '79,604.96'.
    """
    if value is None:
        return None
    if isinstance(value, str):
        value = value.replace(",", "").strip()
        if not value:
            return None
    return float(value)


def _quote(candle: dict, key: str, field: str) -> float | None:
    return _float(candle.get(key, {}).get(field + "_dollars"))


class RateLimiter:
    """Shared token bucket so concurrent workers stay under the API's ceiling."""

    def __init__(self, per_second: float) -> None:
        self.interval = 1.0 / per_second
        self.lock = threading.Lock()
        self.next_slot = 0.0

    def acquire(self) -> None:
        with self.lock:
            now = time.monotonic()
            wait = max(0.0, self.next_slot - now)
            self.next_slot = max(now, self.next_slot) + self.interval
        if wait:
            time.sleep(wait)

    def back_off(self, seconds: float) -> None:
        with self.lock:
            self.next_slot = max(self.next_slot, time.monotonic() + seconds)


_LIMITER = RateLimiter(per_second=4.0)


def _get(
    client: httpx.Client,
    url: str,
    params: dict,
    attempts: int = 9,
    limiter: RateLimiter | None = None,
) -> httpx.Response:
    limiter = limiter or _LIMITER
    last = None
    for attempt in range(attempts):
        limiter.acquire()
        try:
            response = client.get(url, params=params)
        except httpx.HTTPError as error:  # transient network failure
            last = error
            time.sleep(min(30.0, 0.5 * 2**attempt))
            continue
        if response.status_code < 500 and response.status_code != 429:
            response.raise_for_status()
            return response
        last = response
        delay = min(30.0, 0.5 * 2**attempt)
        if response.status_code == 429:
            header = response.headers.get("Retry-After")
            if header and header.isdigit():
                delay = max(delay, float(header))
            # Slow every worker down, not just this one.
            limiter.back_off(delay)
        time.sleep(delay)
    if isinstance(last, httpx.Response):
        last.raise_for_status()
    raise RuntimeError(f"request failed after {attempts} attempts: {last}")


class DataCache:
    """SQLite-backed cache of Kalshi markets, contract candles and Binance klines."""

    def __init__(self, path: str | Path = "data/market_data.db") -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, check_same_thread=False)
        self.db.executescript(SCHEMA)
        self.db.commit()
        self.lock = threading.Lock()

    # ---------------------------------------------------------------- markets

    def sync_markets(
        self, start_ms: int, end_ms: int, kalshi_url: str = KALSHI_URL, refresh: bool = False
    ) -> int:
        """Fetch settled markets one UTC day at a time.

        Uses the min_close_ts/max_close_ts filter rather than a cursor loop: the
        market list is not returned in open_time order, so a loop that stops the
        first time it sees an out-of-range market silently drops markets that
        appear on later pages.
        """
        added = 0
        with httpx.Client(timeout=30) as client:
            for day_start, day_end in _day_range(start_ms, end_ms):
                key = datetime.fromtimestamp(day_start / 1000, UTC).strftime("%Y-%m-%d")
                if not refresh and self._day_done("markets", key):
                    continue
                rows = self._fetch_markets_window(client, kalshi_url, day_start, day_end)
                with self.lock:
                    self.db.executemany(
                        "INSERT OR REPLACE INTO markets VALUES (?,?,?,?,?,?,?,?,?,?)", rows
                    )
                    self._mark_day("markets", key, len(rows))
                    self.db.commit()
                added += len(rows)
        return added

    def _fetch_markets_window(
        self, client: httpx.Client, kalshi_url: str, start_ms: int, end_ms: int
    ) -> list[tuple]:
        rows: dict[str, tuple] = {}
        cursor = None
        while True:
            params = {
                "series_ticker": SERIES,
                "status": "settled",
                "limit": 1000,
                "min_close_ts": start_ms // 1000,
                "max_close_ts": end_ms // 1000,
            }
            if cursor:
                params["cursor"] = cursor
            payload = _get(client, kalshi_url.rstrip("/") + "/markets", params).json()
            page = payload.get("markets", [])
            for item in page:
                if item.get("floor_strike") is None:
                    continue
                settlement = item.get("settlement_ts")
                rows[item["ticker"]] = (
                    item["ticker"],
                    iso_ms(item["open_time"]),
                    iso_ms(item["close_time"]),
                    float(item["floor_strike"]),
                    item.get("result"),
                    item.get("status"),
                    _float(item.get("expiration_value")),
                    iso_ms(settlement) if isinstance(settlement, str) else settlement,
                    _float(item.get("volume_fp")) or 0.0,
                    _float(item.get("open_interest_fp")) or 0.0,
                )
            cursor = payload.get("cursor")
            if not page or not cursor:
                break
        return list(rows.values())

    # ------------------------------------------------------- contract candles

    def sync_contract_candles(
        self,
        tickers: list[str],
        kalshi_url: str = KALSHI_URL,
        workers: int = 6,
        progress: int = 0,
    ) -> int:
        pending = [t for t in tickers if not self._candles_done(t)]
        if not pending:
            return 0
        windows = {
            row[0]: (row[1], row[2])
            for row in self.db.execute("SELECT ticker, open_ms, close_ms FROM markets")
        }
        batches = [
            pending[offset : offset + TICKER_BATCH]
            for offset in range(0, len(pending), TICKER_BATCH)
        ]
        total = 0
        done = 0
        failed = 0

        def fetch(batch: list[str]) -> tuple[list[tuple] | None, list[str]]:
            # One bad batch must not discard the progress of every other batch.
            try:
                return self._fetch_candle_batch(kalshi_url, batch, windows), batch
            except Exception as error:  # noqa: BLE001 - reported, then skipped
                print(f"  batch failed ({error.__class__.__name__}), will retry later", flush=True)
                return None, batch

        with ThreadPoolExecutor(max_workers=workers) as pool:
            for rows, batch in pool.map(fetch, batches):
                if rows is None:
                    failed += 1
                    done += len(batch)
                    continue
                stamp = int(time.time())
                counts: dict[str, int] = dict.fromkeys(batch, 0)
                for row in rows:
                    counts[row[0]] = counts.get(row[0], 0) + 1
                with self.lock:
                    self.db.executemany(
                        "INSERT OR REPLACE INTO contract_candles VALUES "
                        "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        rows,
                    )
                    self.db.executemany(
                        "INSERT OR REPLACE INTO candles_fetched VALUES (?,?,?)",
                        [(t, stamp, counts.get(t, 0)) for t in batch],
                    )
                    self.db.commit()
                total += len(rows)
                done += len(batch)
                if progress and done % progress < TICKER_BATCH:
                    print(f"  candles {done}/{len(pending)} tickers, {total} rows", flush=True)
        if failed:
            print(f"  {failed} batch(es) failed; rerun to fill the gaps", flush=True)
        return total

    def _fetch_candle_batch(
        self, kalshi_url: str, batch: list[str], windows: dict[str, tuple[int, int]]
    ) -> list[tuple]:
        spans = [windows[t] for t in batch if t in windows]
        if not spans:
            return []
        start = min(span[0] for span in spans) // 1000
        end = max(span[1] for span in spans) // 1000
        with httpx.Client(timeout=60) as client:
            payload = _get(
                client,
                kalshi_url.rstrip("/") + "/markets/candlesticks",
                {
                    "market_tickers": ",".join(batch),
                    "start_ts": start,
                    "end_ts": end,
                    "period_interval": 1,
                },
            ).json()
        rows = []
        for entry in payload.get("markets", []):
            ticker = entry["market_ticker"]
            for candle in entry.get("candlesticks", []):
                rows.append(
                    (
                        ticker,
                        int(candle["end_period_ts"]),
                        _quote(candle, "yes_bid", "open"),
                        _quote(candle, "yes_bid", "high"),
                        _quote(candle, "yes_bid", "low"),
                        _quote(candle, "yes_bid", "close"),
                        _quote(candle, "yes_ask", "open"),
                        _quote(candle, "yes_ask", "high"),
                        _quote(candle, "yes_ask", "low"),
                        _quote(candle, "yes_ask", "close"),
                        _quote(candle, "price", "open"),
                        _quote(candle, "price", "high"),
                        _quote(candle, "price", "low"),
                        _quote(candle, "price", "close"),
                        _quote(candle, "price", "mean"),
                        _float(candle.get("volume_fp")) or 0.0,
                        _float(candle.get("open_interest_fp")) or 0.0,
                    )
                )
        return rows

    # ----------------------------------------------------------------- klines

    def sync_klines(
        self, start_ms: int, end_ms: int, binance_url: str = BINANCE_URL, refresh: bool = False
    ) -> int:
        added = 0
        with httpx.Client(timeout=30) as client:
            for day_start, day_end in _day_range(start_ms, end_ms):
                key = datetime.fromtimestamp(day_start / 1000, UTC).strftime("%Y-%m-%d")
                if not refresh and self._day_done("klines", key):
                    continue
                rows = []
                cursor = day_start
                while cursor < day_end:
                    payload = _get(
                        client,
                        binance_url.rstrip("/") + "/api/v3/klines",
                        {
                            "symbol": SYMBOL,
                            "interval": "1m",
                            "startTime": cursor,
                            "endTime": day_end - 1,
                            "limit": 1000,
                        },
                    ).json()
                    if not payload:
                        break
                    rows.extend(
                        (
                            int(row[0]),
                            float(row[1]),
                            float(row[2]),
                            float(row[3]),
                            float(row[4]),
                            float(row[5]),
                            float(row[9]),
                            int(row[8]),
                        )
                        for row in payload
                    )
                    nxt = int(payload[-1][0]) + 60_000
                    if nxt <= cursor:
                        break
                    cursor = nxt
                with self.lock:
                    self.db.executemany(
                        "INSERT OR REPLACE INTO klines VALUES (?,?,?,?,?,?,?,?)", rows
                    )
                    self._mark_day("klines", key, len(rows))
                    self.db.commit()
                added += len(rows)
        return added

    # ------------------------------------------------------------------ reads

    def markets(self, start_ms: int = 0, end_ms: int = 1 << 62) -> list[Market]:
        rows = self.db.execute(
            "SELECT ticker,open_ms,close_ms,floor_strike,result,status,expiration_value,volume "
            "FROM markets WHERE open_ms >= ? AND open_ms < ? ORDER BY open_ms",
            (start_ms, end_ms),
        ).fetchall()
        return [Market(*row) for row in rows]

    def contract_candles(self, tickers: list[str] | None = None) -> dict[str, list[ContractCandle]]:
        wanted = set(tickers) if tickers is not None else None
        out: dict[str, list[ContractCandle]] = {}
        query = (
            "SELECT ticker,end_period_ts,yes_bid_open,yes_bid_high,yes_bid_low,yes_bid_close,"
            "yes_ask_open,yes_ask_high,yes_ask_low,yes_ask_close,price_close,volume,"
            "open_interest FROM contract_candles ORDER BY ticker, end_period_ts"
        )
        for row in self.db.execute(query):
            if wanted is not None and row[0] not in wanted:
                continue
            out.setdefault(row[0], []).append(ContractCandle(*row[1:]))
        return out

    def klines(self, start_ms: int = 0, end_ms: int = 1 << 62) -> list[Candle]:
        rows = self.db.execute(
            "SELECT open_time,open,high,low,close,volume,taker_buy_volume FROM klines "
            "WHERE open_time >= ? AND open_time < ? ORDER BY open_time",
            (start_ms, end_ms),
        ).fetchall()
        return [Candle(*row) for row in rows]

    def coverage(self) -> dict:
        row = self.db.execute("SELECT COUNT(*), MIN(open_ms), MAX(open_ms) FROM markets").fetchone()
        return {
            "markets": row[0],
            "market_start": _stamp(row[1]),
            "market_end": _stamp(row[2]),
            "tickers_with_candles": self.db.execute(
                "SELECT COUNT(*) FROM candles_fetched"
            ).fetchone()[0],
            "contract_candles": self.db.execute("SELECT COUNT(*) FROM contract_candles").fetchone()[
                0
            ],
            "klines": self.db.execute("SELECT COUNT(*) FROM klines").fetchone()[0],
        }

    # --------------------------------------------------------------- internal

    def _day_done(self, kind: str, day: str) -> bool:
        row = self.db.execute(
            "SELECT 1 FROM fetched_days WHERE kind=? AND day=?", (kind, day)
        ).fetchone()
        return row is not None

    def _mark_day(self, kind: str, day: str, rows: int) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO fetched_days VALUES (?,?,?,?)",
            (kind, day, int(time.time()), rows),
        )

    def _candles_done(self, ticker: str) -> bool:
        row = self.db.execute("SELECT 1 FROM candles_fetched WHERE ticker=?", (ticker,)).fetchone()
        return row is not None


def _day_range(start_ms: int, end_ms: int):
    day = datetime.fromtimestamp(start_ms / 1000, UTC).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    while day.timestamp() * 1000 < end_ms:
        nxt = day + timedelta(days=1)
        yield int(day.timestamp() * 1000), int(nxt.timestamp() * 1000)
        day = nxt


def _stamp(value: int | None) -> str | None:
    return None if value is None else datetime.fromtimestamp(value / 1000, UTC).isoformat()
