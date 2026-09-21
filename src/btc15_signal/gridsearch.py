"""Fast rule matching for grid search, walk-forward and permutation testing.

Each strategy is scored on **the exit it actually defines**, not on holding to
settlement. The reversion strategy buys at 30-35c to sell at a take-profit; its
profit is therefore `take_profit - entry` whenever the resting offer would have
filled, and settlement only decides the trades where it never did. Scoring such
a strategy at expiry measures something nobody intends to trade. The primary
non-return strategy genuinely does hold to settlement, so for it the two agree.

Per contract:

    payoff = take_profit           if the exit would have filled before close
           = 1 if the market settled the strategy's way, else 0
    pnl    = payoff - entry_price

Under an efficient market the contract price is a martingale, so *any* stopping
rule has zero expected P&L. Mean P&L per contract therefore keeps zero as its
null hypothesis even once early exits are allowed, which is what lets the same
permutation machinery work for both strategies.

Three things make the search cheap enough to permute hundreds of times:

* Candidates are built once - one `predict()` or `setup()` call per snapshot
  instead of one per snapshot per rule.
* Match sets are cached by the fields that actually filter. Varying only the
  take-profit does not change which markets a rule trades, so those rules share
  one match set.
* Each match set is stored as an integer bitmask, making a re-score under
  permuted outcomes an AND and a popcount per rule rather than a full re-scan.
"""

from array import array
from bisect import bisect_left
from dataclasses import dataclass

from .binance import MarketSnapshot
from .datasource import ContractCandle
from .features import Snapshot
from .lifecycle import exit_quotes
from .model import predict
from .strategy import EntryRule, ReversionRule

NEVER = -1.0  # no post-entry quote was ever available, so no exit could fill

# Decision minutes the non-return strategy may trigger at. The deployed rule fires
# at five minutes left, but the market's mispricing of favourites is widest earlier
# in the window, so the search has to be able to see those minutes at all.
PRIMARY_MINUTES = (3, 4, 5, 6, 7, 8, 9, 10, 11, 12)


@dataclass(frozen=True)
class Candidate:
    """A tradeable decision point, with every feature any rule might filter on."""

    ticker: str
    open_ms: int
    elapsed: int
    remaining: int
    side: str
    entry_price: float
    won: bool
    # best price the position could have been closed at any time after entry
    max_exit: float = NEVER
    # primary-strategy features
    raw_probability: float = 0.0
    normalized_distance: float = 0.0
    momentum_aligned: bool = False
    signed_momentum_bps: float = 0.0
    # reversion-strategy features
    spike_bps: float = 0.0
    rejection_bps: float = 0.0
    distance_bps: float = 0.0
    # regime labels
    session: str = ""
    vol_regime: str = ""
    trend_regime: str = ""
    liquidity_regime: str = ""
    distance_regime: str = ""

    def took_profit(self, take_profit: float | None) -> bool:
        return take_profit is not None and self.max_exit >= take_profit

    def payoff(self, take_profit: float | None = None) -> float:
        if self.took_profit(take_profit):
            return take_profit
        return 1.0 if self.won else 0.0

    def pnl(self, take_profit: float | None = None) -> float:
        return self.payoff(take_profit) - self.entry_price


def take_profit_of(rule) -> float | None:
    """Reversion rules carry a take-profit; the non-return rule holds to expiry."""
    return getattr(rule, "take_profit_price", None)


def _as_market_snapshot(snap: Snapshot) -> MarketSnapshot:
    return MarketSnapshot(
        price=snap.price,
        target=snap.target,
        bid_imbalance=0.0,
        taker_imbalance=snap.taker_imbalance,
        momentum_5m_bps=snap.momentum_5m_bps,
        volatility_5m_bps=snap.volatility_5m_bps,
        futures_basis_bps=0.0,
        spread_bps=0.0,
        window_high=snap.window_high,
        window_low=snap.window_low,
        elapsed_minutes=snap.elapsed,
    )


def _regimes(snap: Snapshot) -> dict:
    return {
        "session": snap.session,
        "vol_regime": snap.vol_regime,
        "trend_regime": snap.trend_regime,
        "liquidity_regime": snap.liquidity_regime,
        "distance_regime": snap.distance_regime,
    }


def max_exit_after(
    candles: list[ContractCandle],
    open_ms: int,
    elapsed: int,
    side: str,
    close_ms: int | None = None,
) -> float:
    """Best price a position opened at `elapsed` could later have been closed at.

    Uses the side of the book the position must actually hit, and only candles
    that had not yet closed at entry, so a take-profit can never be filled by a
    price that existed before the trade.
    """
    cutoff = open_ms // 1000 + elapsed * 60
    limit = close_ms // 1000 if close_ms else None
    best = NEVER
    for candle in candles:
        if candle.end_period_ts <= cutoff:
            continue
        if limit is not None and candle.end_period_ts > limit:
            continue
        quotes = exit_quotes(candle, side)
        if quotes and quotes[0] > best:
            best = quotes[0]
    return best


def build_primary_candidates(
    snapshots: list[Snapshot],
    contract_candles: dict[str, list[ContractCandle]] | None = None,
    closes: dict[str, int] | None = None,
) -> list[Candidate]:
    contract_candles = contract_candles or {}
    closes = closes or {}
    candidates = []
    for snap in sorted(snapshots, key=lambda item: (item.open_ms, item.elapsed)):
        if not PRIMARY_MINUTES[0] <= snap.remaining <= PRIMARY_MINUTES[-1]:
            continue
        market = _as_market_snapshot(snap)
        prediction = predict(market)
        entry = snap.entry_price(prediction.side)
        won = snap.won(prediction.side)
        if entry is None or won is None or not 0 < entry < 1:
            continue
        direction = 1 if prediction.side == "UP" else -1
        candidates.append(
            Candidate(
                ticker=snap.ticker,
                open_ms=snap.open_ms,
                elapsed=snap.elapsed,
                remaining=snap.remaining,
                side=prediction.side,
                entry_price=entry,
                won=won,
                max_exit=max_exit_after(
                    contract_candles.get(snap.ticker, []),
                    snap.open_ms,
                    snap.elapsed,
                    prediction.side,
                    closes.get(snap.ticker),
                ),
                raw_probability=prediction.raw_probability,
                normalized_distance=prediction.distance_bps / max(snap.volatility_5m_bps, 1.0),
                momentum_aligned=direction * snap.momentum_5m_bps > 0,
                signed_momentum_bps=direction * snap.momentum_5m_bps,
                **_regimes(snap),
            )
        )
    return candidates


def build_reversion_candidates(
    snapshots: list[Snapshot],
    contract_candles: dict[str, list[ContractCandle]] | None = None,
    closes: dict[str, int] | None = None,
) -> list[Candidate]:
    """Setup geometry is threshold-independent, so it is computed once here."""
    contract_candles = contract_candles or {}
    closes = closes or {}
    permissive = ReversionRule(
        min_remaining_minutes=1,
        max_remaining_minutes=14,
        min_spike_bps=0.0,
        min_rejection_bps=0.0,
        max_rejection_bps=1e9,
        max_distance_bps=1e9,
    )
    candidates = []
    for snap in sorted(snapshots, key=lambda item: (item.open_ms, item.elapsed)):
        if not 6 <= snap.remaining <= 14:
            continue
        setup = permissive.setup(_as_market_snapshot(snap), snap.remaining)
        if setup is None:
            continue
        entry = snap.entry_price(setup.side)
        won = snap.won(setup.side)
        if entry is None or won is None or not 0 < entry < 1:
            continue
        candidates.append(
            Candidate(
                ticker=snap.ticker,
                open_ms=snap.open_ms,
                elapsed=snap.elapsed,
                remaining=snap.remaining,
                side=setup.side,
                entry_price=entry,
                won=won,
                max_exit=max_exit_after(
                    contract_candles.get(snap.ticker, []),
                    snap.open_ms,
                    snap.elapsed,
                    setup.side,
                    closes.get(snap.ticker),
                ),
                spike_bps=setup.spike_bps,
                rejection_bps=setup.rejection_bps,
                distance_bps=setup.distance_bps,
                **_regimes(snap),
            )
        )
    return candidates


def match_primary(
    candidates: list[Candidate], rule: EntryRule, buckets: dict[int, list[int]] | None = None
) -> list[int]:
    # A primary rule only ever matches one decision minute, so scanning the whole
    # candidate list per rule wastes 90% of the work once ten minutes are in play.
    pool = (
        buckets.get(rule.remaining_minutes, [])
        if buckets is not None
        else range(len(candidates))
    )
    return [
        index
        for index in pool
        # The bucket already fixes the minute, but the check stays so the
        # bucketless path cannot silently match every minute at once.
        if (item := candidates[index]).remaining == rule.remaining_minutes
        and item.raw_probability >= rule.min_raw_probability
        and rule.min_ask <= item.entry_price <= rule.max_ask
        and item.normalized_distance >= rule.min_normalized_distance
        and (not rule.require_momentum_alignment or item.momentum_aligned)
        and item.signed_momentum_bps >= rule.min_momentum_bps
    ]


def match_reversion(candidates: list[Candidate], rule: ReversionRule) -> list[int]:
    """At most one entry per market: the earliest decision minute that qualifies.

    Candidates arrive sorted by (open_ms, elapsed), so the first match for a
    ticker is the earliest one, which is what the live service would take.
    """
    matched = []
    seen: set[str] = set()
    for index, item in enumerate(candidates):
        if item.ticker in seen:
            continue
        if (
            rule.min_remaining_minutes <= item.remaining <= rule.max_remaining_minutes
            and rule.min_entry_price <= item.entry_price <= rule.max_entry_price
            and item.spike_bps >= rule.min_spike_bps
            and rule.min_rejection_bps <= item.rejection_bps <= rule.max_rejection_bps
            and item.distance_bps <= rule.max_distance_bps
        ):
            seen.add(item.ticker)
            matched.append(index)
    return matched


def match_key(strategy: str, rule) -> tuple:
    """The rule fields that decide *which* trades are taken.

    The take-profit changes what a trade earns, never which markets qualify, so
    rules differing only in take-profit reuse one match set.
    """
    if strategy == "reversion":
        return (
            rule.min_remaining_minutes,
            rule.max_remaining_minutes,
            rule.min_entry_price,
            rule.max_entry_price,
            rule.min_spike_bps,
            rule.min_rejection_bps,
            rule.max_rejection_bps,
            rule.max_distance_bps,
        )
    return (
        rule.remaining_minutes,
        rule.min_raw_probability,
        rule.min_ask,
        rule.max_ask,
        rule.min_normalized_distance,
        rule.require_momentum_alignment,
        rule.min_momentum_bps,
    )


BUILDERS = {"primary": build_primary_candidates, "reversion": build_reversion_candidates}
MATCHERS = {"primary": match_primary, "reversion": match_reversion}


def primary_grid() -> list[EntryRule]:
    rules = []
    for remaining in PRIMARY_MINUTES:
        for raw in (0.50, 0.60, 0.70, 0.80, 0.90):
            for distance in (0.0, 0.5, 1.0, 2.0, 3.0, 4.0):
                for min_ask in (0.30, 0.45, 0.60, 0.75, 0.85, 0.90):
                    for max_ask in (0.70, 0.85, 0.95, 0.99):
                        if min_ask >= max_ask:
                            continue
                        for alignment in (False, True):
                            rules.append(
                                EntryRule(
                                    remaining_minutes=remaining,
                                    min_raw_probability=raw,
                                    min_ask=min_ask,
                                    max_ask=max_ask,
                                    min_normalized_distance=distance,
                                    require_momentum_alignment=alignment,
                                    fee_buffer=0.0,
                                )
                            )
    return rules


def reversion_grid() -> list[ReversionRule]:
    return [
        ReversionRule(
            min_remaining_minutes=low,
            max_remaining_minutes=high,
            min_entry_price=min_price,
            max_entry_price=max_price,
            min_spike_bps=spike,
            min_rejection_bps=rejection,
            max_rejection_bps=max_rejection,
            max_distance_bps=distance,
            take_profit_price=take_profit,
            fee_buffer=0.0,
        )
        for low, high in ((10, 12), (8, 12), (6, 14))
        for min_price, max_price in ((0.20, 0.35), (0.30, 0.45), (0.35, 0.55))
        for spike in (4.0, 8.0, 12.0, 16.0)
        for rejection in (0.5, 1.0, 2.0, 4.0)
        for max_rejection in (8.0, 12.0, 20.0)
        if rejection < max_rejection
        for distance in (15.0, 25.0, 35.0, 50.0)
        for take_profit in (0.40, 0.45, 0.50, 0.55, 0.60)
        # A take-profit inside the entry band would "fill" at or below the price
        # paid, booking a loss and calling it an exit.
        if take_profit > max_price
    ]


GRIDS = {"primary": primary_grid, "reversion": reversion_grid}


def pack_mask(indices, width: int) -> int:
    """Build a bitmask from set indices via a byte buffer.

    Accumulating with ``mask |= 1 << index`` allocates a fresh multi-kilobyte
    integer per set bit, which turns mask construction into the slowest part of
    the whole run. Writing bytes and converting once is O(bits) instead.
    """
    buffer = bytearray((width + 7) // 8)
    for index in indices:
        buffer[index >> 3] |= 1 << (index & 7)
    return int.from_bytes(buffer, "little")


class GridIndex:
    """Precomputed match sets and P&L prefixes for a whole rule grid."""

    def __init__(
        self,
        strategy: str,
        candidates: list[Candidate],
        min_trades: int = 1,
        progress: int = 0,
    ) -> None:
        self.strategy = strategy
        self.candidates = candidates
        self.width = len(candidates)
        self.open_times = [item.open_ms for item in candidates]
        matcher = MATCHERS[strategy]

        self.rules: list = []
        self.take_profits: list[float | None] = []
        self.indices: list[array] = []
        self.masks: list[int] = []
        self.price_prefix: list[list[float]] = []
        self.pnl_prefix: list[list[float]] = []
        self.pnl_sq_prefix: list[list[float]] = []

        buckets: dict[int, list[int]] | None = None
        if strategy == "primary":
            buckets = {}
            for index, item in enumerate(candidates):
                buckets.setdefault(item.remaining, []).append(index)

        cache: dict[tuple, tuple[array, int]] = {}
        grid = GRIDS[strategy]()
        for number, rule in enumerate(grid, start=1):
            key = match_key(strategy, rule)
            if key not in cache:
                found = (
                    matcher(candidates, rule, buckets)
                    if buckets is not None
                    else matcher(candidates, rule)
                )
                cache[key] = (array("i", found), pack_mask(found, self.width))
            matched, mask = cache[key]
            if len(matched) >= min_trades:
                take_profit = take_profit_of(rule)
                prices = [0.0]
                pnls = [0.0]
                squares = [0.0]
                for index in matched:
                    item = candidates[index]
                    value = item.pnl(take_profit)
                    prices.append(prices[-1] + item.entry_price)
                    pnls.append(pnls[-1] + value)
                    squares.append(squares[-1] + value * value)
                self.rules.append(rule)
                self.take_profits.append(take_profit)
                self.indices.append(matched)
                self.masks.append(mask)
                self.price_prefix.append(prices)
                self.pnl_prefix.append(pnls)
                self.pnl_sq_prefix.append(squares)
            if progress and number % progress == 0:
                print(f"    indexed {number}/{len(grid)} rules", flush=True)

        self.tp_levels = sorted({tp for tp in self.take_profits if tp is not None})
        if any(tp is None for tp in self.take_profits):
            self.tp_levels.append(None)

    def __len__(self) -> int:
        return len(self.rules)

    def slice_bounds(self, start_ms: int, end_ms: int) -> tuple[int, int]:
        """Candidate index range covering [start_ms, end_ms)."""
        return bisect_left(self.open_times, start_ms), bisect_left(self.open_times, end_ms)

    def rule_span(self, position: int, low: int, high: int) -> tuple[int, int]:
        """Positions within this rule's own matched list covering [low, high)."""
        matched = self.indices[position]
        return bisect_left(matched, low), bisect_left(matched, high)

    def rule_trades(self, position: int, low: int, high: int) -> list[int]:
        """Candidate indices for one rule, restricted to a candidate-index window."""
        start, end = self.rule_span(position, low, high)
        return list(self.indices[position][start:end])

    def window_stats(self, position: int, low: int, high: int) -> tuple[int, float, float]:
        """(trades, mean P&L per contract, total cost) for one rule, in O(log n).

        Prefix sums along each rule's own matched list mean a walk-forward fold
        never has to re-scan or even materialise the trades it did not pick.
        """
        start, end = self.rule_span(position, low, high)
        count = end - start
        if count <= 0:
            return 0, 0.0, 0.0
        pnls = self.pnl_prefix[position]
        prices = self.price_prefix[position]
        return count, (pnls[end] - pnls[start]) / count, prices[end] - prices[start]

    def window_lower_bound(self, position: int, low: int, high: int, z: float = 1.0) -> float:
        """Mean P&L minus z standard errors, computed from prefix sums.

        Ranking a grid by its best point estimate is how a search picks a rule
        that traded 60 times and got lucky over one that traded 4,000 times with
        a real but modest edge. Ranking by a lower bound prices that difference
        in, so a broad stable effect is no longer invisible to the selector.
        """
        start, end = self.rule_span(position, low, high)
        count = end - start
        if count < 2:
            return 0.0
        pnls = self.pnl_prefix[position]
        squares = self.pnl_sq_prefix[position]
        total = pnls[end] - pnls[start]
        mean = total / count
        variance = max((squares[end] - squares[start]) / count - mean * mean, 0.0)
        return mean - z * (variance / count) ** 0.5

    def outcome_masks(self, max_exits: list[float], wons: list[bool]) -> dict:
        """Per take-profit level, the masks needed to score any rule.

        Returns `{take_profit: (took_profit_mask, settled_winner_mask)}` where the
        second mask covers only trades whose exit never filled, since those are
        the ones settlement still decides.
        """
        masks = {}
        for level in self.tp_levels:
            if level is None:
                masks[level] = (
                    0,
                    pack_mask((i for i, won in enumerate(wons) if won), self.width),
                )
                continue
            masks[level] = (
                pack_mask(
                    (i for i, value in enumerate(max_exits) if value >= level), self.width
                ),
                pack_mask(
                    (
                        i
                        for i, (value, won) in enumerate(zip(max_exits, wons, strict=True))
                        if won and value < level
                    ),
                    self.width,
                ),
            )
        return masks

    def best_mean_pnl_under(self, masks: dict, min_trades: int) -> float:
        """Highest mean P&L per contract any rule achieves under these outcomes.

        Used by the permutation test: outcomes move, match sets do not.
        """
        best = 0.0
        for position, mask in enumerate(self.masks):
            prices = self.price_prefix[position]
            count = len(prices) - 1
            if count < min_trades:
                continue
            level = self.take_profits[position]
            took_profit, settled_winner = masks[level]
            payout = float((mask & settled_winner).bit_count())
            if level is not None:
                payout += level * (mask & took_profit).bit_count()
            value = (payout - prices[-1]) / count
            if value > best:
                best = value
        return best

    def realised(self) -> tuple[list[float], list[bool]]:
        return (
            [item.max_exit for item in self.candidates],
            [item.won for item in self.candidates],
        )
