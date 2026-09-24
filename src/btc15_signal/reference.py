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
There is NO second price source. A ``BinanceSeconds`` reader used to sit here
recording per-second spot alongside, so the feed basis could be measured
rather than guessed. That measurement is finished (FINDINGS 41/43) and the
reader is deleted: a client that exists is a client that can be switched back
on, and this system has already shipped one Binance artefact that went on
answering live decisions after it was supposedly retired (FINDINGS 49). The
``binance_*`` columns remain so the rows already written stay readable.

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
