"""BRTI from Kalshi: the price this contract actually settles on.

Every gate in this system has, until now, been computed on Binance BTCUSDT
spot while the contract settles on a 60-second average of CF Benchmarks' BRTI.
FINDINGS 41 measured what that costs: a median 6.23 bps feed basis, an outright
disagreement about which side won in **19.4%** of markets, and **40.5%** inside
5 bps of the strike. FINDINGS 42 established that Kalshi serves BRTI itself, to
our existing production credentials, so the mismatch is now a choice rather
than a constraint.

THREE SOURCES, ALL KALSHI, AND THEY ARE NOT INTERCHANGEABLE:

``/live_data/events/{event_ticker}``   (ordinary cost)
    One point per second, and each value is **already a trailing 60-second
    mean**. One call returns the whole window, so a poll is self-healing: a
    missed request costs freshness, never history. The value at the close
    instant IS ``expiration_value`` - verified exact on 12 of 12 markets.

``/cfbenchmarks/values?id=BRTI``       (50 read tokens)
    Raw per-second RTI prints, unsmoothed.

``/cfbenchmarks/history/values``       (50 read tokens)
    Raw history at 200 ms, for backfill and reconciliation.

WHICH ONE A DECISION SHOULD USE, AND WHY IT MATTERS.

Both ends of this contract are 60-second means: the strike is the mean over the
minute before the window opened, the settlement is the mean over the minute
before it closes. **Distance therefore has to be measured on the smoothed
series** - comparing a raw print against a 60-second mean compares two
different statistics and reintroduces, in miniature, exactly the error we are
removing.

Momentum and volatility are the opposite case. A 60-second mean is a low-pass
filter: measured on the same interval, the smoothed series shows about
**1.5x less** step-to-step volatility than the raw prints. So every threshold
calibrated on Binance raw volatility - ``min_normalized_distance = 1.5``, the
2.0-4.0x confidence band - describes a different quantity here and **cannot be
carried across**. They are recalculated, not copied, and until that measurement
exists this module is shadow-only.
"""

import statistics as st
from dataclasses import dataclass

import httpx

LIVE_DATA_SOURCE = "kalshi_live_data"
PASSTHROUGH_SOURCE = "kalshi_cfb_passthrough"


@dataclass(frozen=True)
class BRTIObservation:
    """One BRTI value, with everything needed to judge it later."""

    event_ticker: str
    ts_ms: int  # the instant the value describes
    value: float  # trailing 60-second mean at ts_ms
    received_ms: int
    source: str
    samples: int = 0  # points in the series this came from
    span_ms: int = 0

    @property
    def age_ms(self) -> int:
        return self.received_ms - self.ts_ms

    def is_stale(self, limit_ms: int) -> bool:
        return self.age_ms > limit_ms


@dataclass(frozen=True)
class BRTIFeatures:
    """The Kalshi-native replacements for the Binance-derived gate inputs.

    Named differently from the Binance fields on purpose. `normalized_distance`
    on a `MarketSnapshot` and `normalized_distance` here are NOT the same
    number, and a shared name would let one be compared against a threshold
    measured for the other - which is the whole class of error this exists to
    end.
    """

    event_ticker: str
    ts_ms: int
    target: float  # floor_strike: itself a 60-second BRTI mean
    value: float  # current trailing 60-second BRTI mean
    signed_distance_bps: float
    brti_momentum_bps: float
    brti_volatility_bps: float
    brti_normalized_distance: float
    samples: int
    span_ms: int
    stale: bool
    # What the contract would settle at if the window ended at `ts_ms`. It is
    # the same number as `value` - that is the point, and the reason it is
    # named separately is that this is the one a close-call decision wants.
    settlement_projection: float

    @property
    def side(self) -> str:
        """Which side is winning, decided on the official reference alone."""
        return "UP" if self.signed_distance_bps >= 0 else "DOWN"


def features_from_series(
    event_ticker: str,
    series: list[tuple[int, float]],
    target: float,
    now_ms: int,
    momentum_window_s: int = 300,
    volatility_window_s: int = 300,
    stale_limit_ms: int = 15_000,
) -> BRTIFeatures | None:
    """Gate inputs from a BRTI series. Returns None rather than guessing.

    The windows default to five minutes to mirror the Binance features they sit
    beside in the archive, so the two can be compared directly. That is a
    comparison choice, not a claim that five minutes is the right window for
    BRTI - the right window is an open measurement.
    """
    if not series or not target:
        return None
    ordered = sorted(series)
    ts_ms, value = ordered[-1]

    window = [v for t, v in ordered if ts_ms - momentum_window_s * 1000 <= t <= ts_ms]
    momentum = (value / window[0] - 1) * 10_000 if len(window) > 1 else 0.0

    vol_points = [
        v for t, v in ordered if ts_ms - volatility_window_s * 1000 <= t <= ts_ms
    ]
    if len(vol_points) > 2:
        returns = [
            (vol_points[i + 1] / vol_points[i] - 1) * 10_000
            for i in range(len(vol_points) - 1)
            if vol_points[i]
        ]
        # Scaled to the same window the Binance volatility describes, so the
        # two columns in the archive are at least dimensionally comparable.
        volatility = st.pstdev(returns) * (len(returns) ** 0.5) if returns else 0.0
    else:
        volatility = 0.0

    signed = (value / target - 1) * 10_000
    return BRTIFeatures(
        event_ticker=event_ticker,
        ts_ms=ts_ms,
        target=target,
        value=value,
        signed_distance_bps=signed,
        brti_momentum_bps=momentum,
        brti_volatility_bps=volatility,
        brti_normalized_distance=abs(signed) / max(volatility, 1e-9),
        samples=len(ordered),
        span_ms=ordered[-1][0] - ordered[0][0],
        stale=(now_ms - ts_ms) > stale_limit_ms,
        settlement_projection=value,
    )


class KalshiBRTI:
    """The per-second BRTI series for one event, from Kalshi's live data.

    Unauthenticated and ordinary cost, so it can be polled on the service beat.
    Each response carries the whole window, which is why this is preferred over
    the WebSocket channel for a first deployment: there is no sequence state to
    lose, no reconnect to get wrong, and a dropped poll costs freshness rather
    than a hole in the record. The `cfbenchmarks_value` channel is the upgrade
    when sub-second latency starts to matter, and it fits behind this same
    interface.
    """

    def __init__(self, base_url: str, timeout: float = 10.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.client = httpx.AsyncClient(timeout=timeout)

    async def close(self) -> None:
        await self.client.aclose()

    async def series(self, event_ticker: str) -> list[tuple[int, float]]:
        response = await self.client.get(
            self.base_url + f"/live_data/events/{event_ticker}"
        )
        response.raise_for_status()
        details = (response.json().get("live_data") or {}).get("details") or {}
        points = details.get("timeseries") or []
        return [
            (int(p["t"]), float(p["v"]))
            for p in points
            if p.get("t") is not None and p.get("v") is not None
        ]

    async def latest(self, event_ticker: str, now_ms: int) -> BRTIObservation | None:
        points = await self.series(event_ticker)
        if not points:
            return None
        points.sort()
        ts_ms, value = points[-1]
        return BRTIObservation(
            event_ticker=event_ticker, ts_ms=ts_ms, value=value,
            received_ms=now_ms, source=LIVE_DATA_SOURCE,
            samples=len(points), span_ms=points[-1][0] - points[0][0],
        )

    async def features(
        self, event_ticker: str, target: float, now_ms: int, **kwargs
    ) -> BRTIFeatures | None:
        return features_from_series(
            event_ticker, await self.series(event_ticker), target, now_ms, **kwargs
        )

    async def settlement_value(
        self, event_ticker: str, close_ms: int
    ) -> float | None:
        """The official settlement figure, read rather than recomputed.

        The value at the close instant IS `expiration_value` - exact on 12 of
        12 markets checked. Recomputing it from raw prints lands $0.20-$1.10
        away under every alignment tried, so our own arithmetic belongs in a
        cross-check, never in the number a decision uses.
        """
        points = await self.series(event_ticker)
        exact = [v for t, v in points if t == close_ms]
        if exact:
            return exact[0]
        before = [(t, v) for t, v in points if t <= close_ms]
        return max(before)[1] if before else None


class BRTIPassthrough:
    """Raw RTI prints through Kalshi's CF Benchmarks passthrough.

    Authenticated and **50 read tokens a call** against 10 for an ordinary
    request, so this is for backfill and reconciliation, never the poll loop.

    Signing gotcha, paid for once already: sign the path WITHOUT the query
    string, and do not re-prefix `/trade-api/v2` - `_headers` already does, and
    doing it twice returns INCORRECT_API_KEY_SIGNATURE.
    """

    def __init__(self, base_url: str, signer, timeout: float = 30.0) -> None:
        self.base_url = base_url.rstrip("/")
        self._signer = signer
        self.client = httpx.AsyncClient(timeout=timeout)

    async def close(self) -> None:
        await self.client.aclose()

    async def values(self, index_id: str = "BRTI") -> list[tuple[int, float]]:
        path = "/cfbenchmarks/values"
        response = await self.client.get(
            self.base_url + path, params={"id": index_id},
            headers=self._signer("GET", path),
        )
        response.raise_for_status()
        payload = (response.json().get("data") or {}).get("payload") or []
        return [(int(p["time"]), float(p["value"])) for p in payload]

    async def history(
        self, timestamp_iso: str, index_id: str = "BRTI", timespan: str = "HOUR"
    ) -> list[tuple[int, float]]:
        path = "/cfbenchmarks/history/values"
        response = await self.client.get(
            self.base_url + path,
            params={"id": index_id, "timespan": timespan, "timestamp": timestamp_iso},
            headers=self._signer("GET", path),
        )
        response.raise_for_status()
        payload = (response.json().get("data") or {}).get("payload") or []
        return [(int(p["time"]), float(p["value"])) for p in payload]


def sixty_second_mean(
    prints: list[tuple[int, float]], close_ms: int
) -> tuple[float | None, int]:
    """Our own final-minute average, for CROSS-CHECKING only.

    Kalshi publishes the official figure; this recomputation lands $0.20-$1.10
    from it and is therefore a disagreement alarm, not a source of truth
    (FINDINGS 42).
    """
    window = [v for t, v in prints if close_ms - 60_000 < t <= close_ms]
    return (st.mean(window) if window else None), len(window)
