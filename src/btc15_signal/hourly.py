"""BTC hourly-expiry chain: fetch, normalise, and prove a whole strike ladder.

The 15-minute series is ONE contract with one strike. The hourly series is a
ladder of ~188 "or above" thresholds $100 apart sharing a settlement time. That
difference is not cosmetic. A ladder lets you *pick* which strike to trade, and
picking the best-looking of 188 correlated estimates is the most reliable way
known to manufacture an edge that is not there - the same failure that made a
grid search over 11,365 rules score below the median shuffled result
(FINDINGS.md 7). Nothing in this module selects a strike. It reads the chain,
checks it is internally consistent, and hands it to the archive.

Kept out of `datasource.py` deliberately: that module is imported by the live
15-minute trading path, and a series that has never been traded is the wrong
place to risk a change to the code that places real orders.

Series note: the ladder is `KXBTCD`. The D does not mean daily - its events are
hourly (`KXBTCD-26SEP2112` opens 15:00 UTC, closes 16:00 UTC). `KXBTC` is the
same expiry expressed as ~186 mutually exclusive $100 brackets, which is a
different instrument and is not handled here.
"""

from dataclasses import dataclass
from datetime import datetime

import httpx

# A threshold contract pays YES if the settled BRTI is strictly above
# `floor_strike`. Kalshi titles it with the next cent up: floor_strike
# 89299.99 is shown as "$89,300 or above". Measure distance from the
# floor_strike, display the title.
THRESHOLD_TYPE = "greater"


@dataclass(frozen=True)
class Strike:
    """One rung of the ladder, at one instant."""

    ticker: str
    strike: float
    yes_bid: float
    yes_ask: float
    no_bid: float
    no_ask: float
    yes_bid_size: float
    yes_ask_size: float
    volume: float
    open_interest: float
    # Kalshi's market-record stamp. NOT a quote clock - see check_integrity.
    updated_ms: int | None

    @property
    def yes_mid(self) -> float | None:
        if self.yes_bid <= 0 and self.yes_ask >= 1:
            return None
        return (self.yes_bid + self.yes_ask) / 2

    @property
    def spread(self) -> float:
        return self.yes_ask - self.yes_bid

    @property
    def quotable(self) -> bool:
        """Is there a two-sided market that could actually be traded?

        A rung pinned at 0.00/0.01 or 0.99/1.00 is not a market, it is the
        exchange saying the outcome is settled in all but name. Most of the
        188 rungs are in that state at any moment.
        """
        return self.yes_bid > 0.0 and self.yes_ask < 1.0


@dataclass(frozen=True)
class Chain:
    """Every rung of one hourly expiry, at one instant."""

    chain_id: str
    open_ms: int
    close_ms: int
    fetched_ms: int
    strikes: tuple[Strike, ...]
    skipped_types: int

    @property
    def remaining_seconds(self) -> float:
        return (self.close_ms - self.fetched_ms) / 1000

    def quotable(self) -> tuple[Strike, ...]:
        return tuple(s for s in self.strikes if s.quotable)

    def near(self, spot: float, dollars: float) -> tuple[Strike, ...]:
        """Rungs within `dollars` of spot, in ladder order."""
        return tuple(s for s in self.strikes if abs(s.strike - spot) <= dollars)


@dataclass(frozen=True)
class Integrity:
    """Whether the chain is coherent enough to reason about.

    A failure here is not a reason to discard the snapshot - a chain that
    disagrees with itself is exactly the kind of thing the archive should keep.
    It is a reason not to TRADE it.
    """

    ok: bool
    arbitrage_pairs: int
    worst_arbitrage: float
    non_monotonic: int
    crossed_books: int
    stale_seconds: float
    quotable_count: int
    reasons: tuple[str, ...]


def _dollars(row: dict, field: str) -> float:
    raw = row.get(f"{field}_dollars", row.get(field))
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return 0.0
    # Kalshi returns cents on some fields and dollars on others.
    return value / 100 if value > 1.5 else value


def _number(row: dict, field: str) -> float:
    try:
        return float(row.get(field) or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _iso_ms(value) -> int | None:
    if not value:
        return None
    try:
        return int(
            datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp() * 1000
        )
    except ValueError:
        return None


def parse_chain(markets: list[dict], fetched_ms: int) -> Chain | None:
    """Build a Chain from raw Kalshi market rows sharing one event ticker.

    Rows that are not `greater` thresholds are counted and dropped rather than
    coerced. If Kalshi ever mixes bracket contracts into this event, the count
    makes it visible instead of letting a bracket be modelled as a threshold.
    """
    if not markets:
        return None
    chain_id = markets[0].get("event_ticker")
    open_ms = _iso_ms(markets[0].get("open_time"))
    close_ms = _iso_ms(markets[0].get("close_time"))
    if not chain_id or open_ms is None or close_ms is None:
        return None

    rungs, skipped = [], 0
    for row in markets:
        if row.get("strike_type") != THRESHOLD_TYPE:
            skipped += 1
            continue
        try:
            strike = float(row["floor_strike"])
        except (KeyError, TypeError, ValueError):
            skipped += 1
            continue
        rungs.append(
            Strike(
                ticker=row["ticker"],
                strike=strike,
                yes_bid=_dollars(row, "yes_bid"),
                yes_ask=_dollars(row, "yes_ask"),
                no_bid=_dollars(row, "no_bid"),
                no_ask=_dollars(row, "no_ask"),
                yes_bid_size=_number(row, "yes_bid_size_fp"),
                yes_ask_size=_number(row, "yes_ask_size_fp"),
                volume=_number(row, "volume_fp"),
                open_interest=_number(row, "open_interest_fp"),
                updated_ms=_iso_ms(row.get("updated_time")),
            )
        )
    if not rungs:
        return None
    rungs.sort(key=lambda s: s.strike)
    return Chain(
        chain_id=chain_id,
        open_ms=open_ms,
        close_ms=close_ms,
        fetched_ms=fetched_ms,
        strikes=tuple(rungs),
        skipped_types=skipped,
    )


def check_integrity(
    chain: Chain,
    spot: float | None,
    now_ms: int,
    quotes_age_s: float | None = None,
    max_stale_s: float = 180.0,
) -> Integrity:
    """Prove the ladder agrees with itself before anything reasons about it.

    P(BRTI > s) is non-increasing in s, so a higher rung can never be worth
    more than a lower one. Two strengths of that:

    - ARBITRAGE: `yes_bid(high) > yes_ask(low)`. Buy the low rung, sell the
      high one, collect a credit, and the payoff can never be negative - the
      low rung pays whenever the high one does. Free money does not exist, so
      this is bad data, a mid-fetch book change, or a rung that is quoted
      against a different underlying snapshot.
    - NON-MONOTONIC MIDS: softer. Wide spreads on thin rungs produce this
      routinely and it is not, on its own, a reason to distrust the chain.

    Only quotable rungs are compared. Comparing a rung pinned at 0.99/1.00
    against one pinned at 0.00/0.01 tests the exchange's rounding, not the
    market.

    STALENESS is the caller's measurement, not this function's. The obvious
    source - `updated_time` on the market record - does not track quotes: all
    188 rungs carry one of three timestamps stamped at chain open, and a
    measured 13 rungs changed their quotes inside 20 seconds while not one
    timestamp moved. Using it marks every snapshot stale. The only honest
    reading is how long it has been since any quote actually changed, which
    needs the previous snapshot, so the caller tracks it and passes
    `quotes_age_s`. None means not yet known (the first poll), which is not a
    failure.
    """
    live = chain.quotable()
    reasons: list[str] = []

    arb_pairs, worst_arb = 0, 0.0
    non_monotonic = 0
    for low, high in zip(live, live[1:], strict=False):
        if high.yes_bid > low.yes_ask > 0:
            arb_pairs += 1
            worst_arb = max(worst_arb, high.yes_bid - low.yes_ask)
        low_mid, high_mid = low.yes_mid, high.yes_mid
        if low_mid is not None and high_mid is not None and high_mid > low_mid:
            non_monotonic += 1

    crossed = sum(
        1 for s in chain.strikes if s.yes_bid > s.yes_ask or s.yes_bid + s.no_bid > 1.0
    )

    stale_s = quotes_age_s if quotes_age_s is not None else 0.0
    is_stale = quotes_age_s is not None and quotes_age_s > max_stale_s

    if arb_pairs:
        reasons.append(f"{arb_pairs} arbitrage pairs, worst {worst_arb:.3f}")
    if crossed:
        reasons.append(f"{crossed} crossed books")
    if is_stale:
        reasons.append(f"no quote moved in {stale_s:.0f}s")
    if not live:
        reasons.append("no quotable rung")
    if chain.skipped_types:
        reasons.append(f"{chain.skipped_types} non-threshold rows dropped")
    if spot is not None and live:
        lo, hi = live[0].strike, live[-1].strike
        # Spot far outside every quotable rung means the ladder and the
        # underlying are describing different moments.
        if not lo - 5000 <= spot <= hi + 5000:
            reasons.append(f"spot {spot:.0f} outside quotable band {lo:.0f}-{hi:.0f}")

    return Integrity(
        ok=not (arb_pairs or crossed or is_stale or not live),
        arbitrage_pairs=arb_pairs,
        worst_arbitrage=worst_arb,
        non_monotonic=non_monotonic,
        crossed_books=crossed,
        stale_seconds=stale_s,
        quotable_count=len(live),
        reasons=tuple(reasons),
    )


class HourlyChainClient:
    """Read-only Kalshi reader for the hourly ladder.

    Deliberately has no order-placing surface. Shadow mode cannot place an
    order by accident if the client it uses cannot express one.
    """

    def __init__(self, base_url: str, series: str = "KXBTCD") -> None:
        self._base = base_url.rstrip("/")
        self._series = series
        self._client = httpx.AsyncClient(timeout=30)

    async def close(self) -> None:
        await self._client.aclose()

    async def active_chain(self, now_ms: int) -> Chain | None:
        """The chain whose window contains `now_ms`, or None between windows.

        One request returns every open rung across every open expiry; they are
        grouped here rather than asking per event, because 188 rungs per event
        is already a large page and several events are open at once.
        """
        response = await self._client.get(
            f"{self._base}/markets",
            params={"series_ticker": self._series, "status": "open", "limit": 1000},
        )
        response.raise_for_status()
        grouped: dict[str, list[dict]] = {}
        for row in response.json().get("markets", []):
            event = row.get("event_ticker")
            if event:
                grouped.setdefault(event, []).append(row)

        best: Chain | None = None
        for rows in grouped.values():
            chain = parse_chain(rows, now_ms)
            if chain is None or not chain.open_ms <= now_ms < chain.close_ms:
                continue
            # Several events can be open; take the one closing soonest.
            if best is None or chain.close_ms < best.close_ms:
                best = chain
        return best
