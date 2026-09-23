"""Price-reference clients for the settlement recorder.

Three sources, and the distinction between them is the entire point:

* ``brti.KalshiBRTI``  CF Benchmarks' Real Time Index, the thing the contract
                       actually settles on - served BY KALSHI and reached with
                       the credentials this system already holds. It lives in
                       `brti.py`. The CF-direct client that used to sit here
                       was deleted once FINDINGS 42 established that an outside
                       subscription was never needed; keeping a second path to
                       the same number would only invite the two to disagree.
* ``KalshiOfficial``   Kalshi's own published 60-second BRTI averages, read
                       from the API's ``floor_strike`` and ``expiration_value``.
                       Official, free, and available for every settled market.
* ``BinanceSeconds``   Per-second Binance spot, recorded ALONGSIDE so the feed
                       basis can be measured rather than guessed.

**No source substitutes for another.** If BRTI is unavailable the recorder
writes a gap and carries on; it does not quietly put Coinbase, Kraken or
Binance in the reference column. A basis measured against a stand-in is not a
basis, and a recorder that hides its own blind spots is worse than no recorder,
because the resulting table looks complete.

WHY KALSHI'S OWN FIELDS COUNT AS THE OFFICIAL REFERENCE, and why the app
display is never read. ``rules_primary`` states the settlement rule in full,
and the identity it implies - that window N's strike is window N-1's
settlement, both being the 60-second BRTI average at the same instant - holds
EXACTLY on 6,420 consecutive pairs in the corpus, max difference $0.0000, and
``expiration_value >= floor_strike`` reproduces all 6,435 settled results. Two
independent official numbers per window, from a documented API.
"""

import asyncio
from dataclasses import dataclass

import httpx


@dataclass(frozen=True)
class Observation:
    """One price from one source, with everything needed to judge it later."""

    source: str
    status: str  # ok | missing | stale | error
    raw_price: float | None = None
    event_ms: int | None = None
    received_ms: int = 0
    error: str | None = None

    @property
    def age_ms(self) -> int | None:
        if self.event_ms is None or not self.received_ms:
            return None
        return self.received_ms - self.event_ms

    def is_stale(self, limit_ms: int) -> bool:
        age = self.age_ms
        return age is not None and age > limit_ms


class BinanceSeconds:
    """Per-second Binance spot, recorded beside the reference - never as it.

    One-second klines are used rather than the book ticker because a 60-second
    mean has to be built from evenly spaced observations to be comparable with
    a 60-observation BRTI average; a poll every 10 seconds would compare a
    6-sample mean with a 60-sample one and call the difference basis.
    """

    def __init__(self, base_url: str, symbol: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.symbol = symbol
        self.client = httpx.AsyncClient(timeout=10)

    async def close(self) -> None:
        await self.client.aclose()

    async def seconds(self, start_ms: int, end_ms: int) -> list[tuple[int, float]]:
        """Closing price of each 1-second bar in [start_ms, end_ms)."""
        out: list[tuple[int, float]] = []
        cursor = start_ms
        while cursor < end_ms:
            response = await self.client.get(
                self.base_url + "/api/v3/klines",
                params={
                    "symbol": self.symbol, "interval": "1s",
                    "startTime": cursor, "endTime": end_ms - 1, "limit": 1000,
                },
            )
            response.raise_for_status()
            rows = response.json()
            if not rows:
                break
            out.extend((int(row[0]), float(row[4])) for row in rows)
            cursor = int(rows[-1][0]) + 1000
            if len(rows) < 1000:
                break
        return out

    async def latest(self, now_ms: int) -> Observation:
        try:
            response = await self.client.get(
                self.base_url + "/api/v3/ticker/bookTicker",
                params={"symbol": self.symbol},
            )
            response.raise_for_status()
            book = response.json()
            mid = (float(book["bidPrice"]) + float(book["askPrice"])) / 2
            return Observation(
                source="binance_spot", status="ok", raw_price=mid,
                event_ms=now_ms, received_ms=now_ms,
            )
        except (httpx.HTTPError, ValueError, KeyError) as exc:
            return Observation(
                source="binance_spot", status="error", received_ms=now_ms,
                error=f"{type(exc).__name__}: {exc}"[:200],
            )


class KalshiOfficial:
    """The official 60-second BRTI averages Kalshi publishes per market.

    ``floor_strike``      the average over the 60s before the window OPENED
    ``expiration_value``  the average over the 60s before it CLOSED

    Both are the settlement reference itself, not a display of it.
    """

    def __init__(self, base_url: str, series: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.series = series
        self.client = httpx.AsyncClient(timeout=15)

    async def close(self) -> None:
        await self.client.aclose()

    async def settled(
        self, limit: int = 200, cursor: str | None = None
    ) -> tuple[list[dict], str | None]:
        params = {"series_ticker": self.series, "status": "settled", "limit": limit}
        if cursor:
            params["cursor"] = cursor
        response = await self.client.get(self.base_url + "/markets", params=params)
        response.raise_for_status()
        body = response.json()
        return body.get("markets", []), body.get("cursor") or None

    async def market(self, ticker: str) -> dict | None:
        try:
            response = await self.client.get(self.base_url + f"/markets/{ticker}")
            response.raise_for_status()
            return response.json().get("market")
        except httpx.HTTPError:
            return None


def rolling_mean(
    samples: list[tuple[int, float]], now_ms: int, window_ms: int = 60_000
) -> tuple[float | None, int, int | None]:
    """(mean, count, span) over the samples inside the trailing window.

    Returned alongside its count and span on purpose. A 60-second mean built
    from four samples is not the same measurement as one built from sixty, and
    a column holding only the mean cannot tell the two apart afterwards.
    """
    inside = [(ms, price) for ms, price in samples if now_ms - ms <= window_ms]
    if not inside:
        return None, 0, None
    span = inside[-1][0] - inside[0][0] if len(inside) > 1 else 0
    return sum(price for _, price in inside) / len(inside), len(inside), span


def basis_bps(price: float | None, reference: float | None) -> float | None:
    if price is None or not reference:
        return None
    return (price / reference - 1) * 10_000


async def gather_quietly(*coros):
    """Run the source polls together; a failure in one is a value, not a raise."""
    return await asyncio.gather(*coros, return_exceptions=True)
