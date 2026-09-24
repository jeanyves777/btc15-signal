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
    # HOW MUCH OF THE RECENT MOVE HAS ALREADY BEEN HANDED BACK, 0.0 to 1.0,
    # measured in the direction the setup is taking. The deployed gates cannot
    # see this: momentum reads POSITIVE on a run-up that has already peaked,
    # because the five-minute window still contains the run. On 2026-09-24 a
    # market ran to 83,340 against a target of 83,234, the position was taken
    # UP, and BRTI then collapsed to 83,191. Every gate was green.
    #
    # 1.0 means the whole advance is gone. None means it could not be computed
    # - too few samples, or no advance to give back - and None is never read as
    # a pass.
    brti_retrace: float | None = None
    # HOW MUCH OF THE PATH WENT NOWHERE, 0.0 to 1.0. The ratio of net movement
    # to total distance travelled: 0.0 is a straight line, 1.0 is pure
    # thrashing that ends where it began. On 2026-09-24 06:45 BRTI crossed the
    # strike six times in an hour and finished $11.74 from it - a market whose
    # outcome is a coin toss however far the last print happens to be from the
    # target, and no deployed gate can tell it from a clean trend.
    #
    # IT IS NOT A GATE AND MUST NEVER BECOME ONE. The operator was explicit:
    # choppiness influences CONFIDENCE only. It is absent from `check_facts`
    # by construction, so there is no threshold for it to fail and no way for
    # it to refuse or admit a setup. It moves the header word; that is all the
    # authority it has.
    brti_choppiness: float | None = None
    # WILDER'S RSI ON THE SETTLEMENT REFERENCE. Operator, 2026-09-24: the
    # Kalshi price implies a direction, and taking that on trust has cost
    # validated signals - so direction wants a second opinion from the price
    # ACTION, not from the price alone.
    #
    # Above 50 the recent path has been rising, below 50 falling. Whether that
    # predicts anything is a MEASUREMENT and not an assumption: it is computed
    # and archived here, and gates nothing until the archive earns it. The same
    # discipline the reversal and choppiness indicators were held to, and the
    # reason both could be judged later instead of argued about.
    brti_rsi: float | None = None
    # THREE LEVEL-HOLDING MEASURES. Operator, 2026-09-24, after RSI failed on
    # the live archive: direction at a 15-minute horizon is already in the
    # price, so what is worth measuring is whether the STRIKE is being
    # defended - "we are mostly already in the money; will price stay above
    # the level we need it to stay".
    #
    # Measured on the bot's own 176 in-the-money executed trades:
    #
    #   accel     decaying (<0) trades lost -6.93 over 55 trades while
    #             winning 40 of them: they win often and lose big
    #   held_s    5-12 min on side won 91.5% at +7.8% residual, against
    #             79.1% and -3.2% for under 5 minutes
    #   rejections 2+ tests held won 88.1% at +6.8%, against 78.9% and
    #             -4.6% for a level tested only once
    #
    # brti_accel      momentum over the last 150s MINUS the prior 150s,
    #                 signed toward the winning side. Positive = the move
    #                 that built the cushion is still building.
    # brti_held_s     seconds price has been continuously on the winning side.
    # brti_rejections times price came inside 40% of the current gap and was
    #                 turned back without crossing.
    brti_accel: float | None = None
    brti_held_s: float | None = None
    brti_rejections: int | None = None

    @property
    def side(self) -> str:
        """Which side is winning, decided on the official reference alone."""
        return "UP" if self.signed_distance_bps >= 0 else "DOWN"


def wilder_rsi(values: list[float], period: int = 14) -> float | None:
    """Wilder's RSI over a price series, or None if it cannot be computed.

    The standard definition, smoothed the way Wilder specified rather than
    with a simple mean: the first average is a simple one over `period`
    changes and every later one carries (period-1)/period of the previous.
    A simple rolling mean is a DIFFERENT indicator that shares the name, and
    the difference is largest exactly where the series turns.

    Returns None below `period + 1` samples rather than a half-formed number.
    A flat series has no losses and no gains; it is 50, not 100, because
    "nothing moved" is neither strength nor weakness.
    """
    if len(values) < period + 1:
        return None
    gains, losses = [], []
    for earlier, later in zip(values, values[1:]):
        change = later - earlier
        gains.append(max(0.0, change))
        losses.append(max(0.0, -change))
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for gain, loss in zip(gains[period:], losses[period:]):
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period
    if avg_loss == 0 and avg_gain == 0:
        return 50.0
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def features_from_series(
    event_ticker: str,
    series: list[tuple[int, float]],
    target: float,
    now_ms: int,
    momentum_window_s: int = 300,
    volatility_window_s: int = 300,
    stale_limit_ms: int = 15_000,
    retrace_window_s: int = 120,
    choppiness_window_s: int = 900,
    rsi_window_s: int = 900,
    rsi_bucket_s: int = 60,
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

    # THE REVERSAL MEASURE, in the direction this setup would be taken.
    # `side` is whichever way BRTI already sits relative to the strike, which
    # is the side the strategy takes, so the advance is measured that way.
    side_up = signed >= 0
    seg = [v for t, v in ordered if t >= ts_ms - retrace_window_s * 1000]
    retrace = None
    if len(seg) >= 4:
        start = seg[0]
        if side_up:
            extreme = max(seg)
            advance, given = extreme - start, extreme - value
        else:
            extreme = min(seg)
            advance, given = start - extreme, value - extreme
        if advance > 0:
            # Clamped: a price now BETTER than the extreme is not a negative
            # retrace, it is simply no retrace at all.
            retrace = max(0.0, min(1.0, given / advance))

    # CHOPPINESS, over the whole window rather than the reversal's short one:
    # thrashing is a property of the session, not of the last two minutes.
    chop_seg = [v for t, v in ordered if t >= ts_ms - choppiness_window_s * 1000]
    choppiness = None
    if len(chop_seg) >= 10:
        travelled = sum(abs(chop_seg[i + 1] - chop_seg[i])
                        for i in range(len(chop_seg) - 1))
        net = abs(chop_seg[-1] - chop_seg[0])
        if travelled > 0:
            # 0.0 straight line, 1.0 ended where it started having moved a lot.
            choppiness = max(0.0, min(1.0, 1.0 - net / travelled))

    # RSI ON BUCKETED SAMPLES, not on the raw per-second series. BRTI arrives
    # roughly once a second, so 14 raw changes span 14 SECONDS - an indicator
    # of noise, not of the move. Bucketing to one close per minute over the
    # window makes the 14 periods 14 minutes, which is what RSI means
    # everywhere else and what the operator is reading off a chart.
    rsi = None
    rsi_points = [(t, v) for t, v in ordered
                  if t >= ts_ms - rsi_window_s * 1000]
    if rsi_points:
        buckets: dict[int, float] = {}
        for t, v in rsi_points:
            buckets[t // (rsi_bucket_s * 1000)] = v      # last in each bucket
        closes = [buckets[k] for k in sorted(buckets)]
        rsi = wilder_rsi(closes)

    # ---- THE THREE LEVEL-HOLDING MEASURES -------------------------------
    # All computed on the winning side's sign, so UP and DOWN read the same
    # way: positive `accel` means the move is still working FOR the position.
    sign = 1.0 if side_up else -1.0
    level_pts = [(t, v) for t, v in ordered if t >= ts_ms - 900_000]

    def _drift(lo_ms: int, hi_ms: int) -> float | None:
        seg2 = [v for t, v in level_pts if ts_ms - hi_ms <= t <= ts_ms - lo_ms]
        if len(seg2) < 10 or not seg2[0]:
            return None
        return (seg2[-1] / seg2[0] - 1) * 10_000

    recent, prior = _drift(0, 150_000), _drift(150_000, 300_000)
    accel = None if recent is None or prior is None else (recent - prior) * sign

    held_s = None
    rejections = None
    if len(level_pts) >= 60:
        # Distance from the strike, signed so positive is the winning side.
        edge = [(t, (v - target) * sign) for t, v in level_pts]
        gap = edge[-1][1]
        if gap > 0:
            held_s = 0.0
            for t, v in reversed(edge):
                if v <= 0:
                    break
                held_s = (ts_ms - t) / 1000.0
            # A level TESTED AND HELD is stronger evidence than one never
            # approached. Inside 40% of the current gap counts as a test; it
            # only counts as a rejection once price recovers back past it.
            threshold = gap * 0.4
            rejections, inside = 0, False
            for _t, v in edge:
                if not inside and 0 < v <= threshold:
                    inside = True
                elif inside and v > threshold:
                    rejections += 1
                    inside = False

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
        brti_retrace=retrace,
        brti_choppiness=choppiness,
        brti_rsi=rsi,
        brti_accel=accel,
        brti_held_s=held_s,
        brti_rejections=rejections,
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
