"""Trade lifecycle: trigger -> minute-by-minute path -> close.

Holding a Kalshi event contract to expiry throws away information. A position
that settles worthless may have been comfortably in profit minutes earlier.
This module reconstructs the full executable path of every triggered trade so
that question can be answered with numbers instead of intuition:

* MFE / MAE - the best and worst price the position could actually have been
  closed at, using the opposite side of the book rather than the mid.
* ``ever_profitable_at`` - the first minute the position could have been closed
  for at least N cents of profit.
* ``simulate_exit`` - what a take-profit, stop-loss, trailing stop or timed exit
  would have produced on that same path, so early-exit policies can be compared
  against hold-to-expiry on identical trades.

Exit prices use the side of the book the position must actually hit:
long YES exits at the yes bid, long NO exits at the no bid (1 - yes ask).
"""

from dataclasses import dataclass, field

from .datasource import ContractCandle

PROFIT_LADDER = (0.01, 0.02, 0.03, 0.05, 0.08, 0.10, 0.15, 0.20, 0.25)


@dataclass(frozen=True)
class PathPoint:
    minute: int  # minutes after entry
    end_ts: int
    best: float  # best price the position could have been closed at
    worst: float
    close: float
    volume: float


@dataclass(frozen=True)
class Lifecycle:
    ticker: str
    open_ms: int
    side: str
    entry_minute: int  # minutes elapsed in the 15-minute window
    entry_price: float
    settled_won: bool | None
    path: tuple[PathPoint, ...] = ()
    ever_profitable_at: dict[float, int | None] = field(default_factory=dict)

    # --------------------------------------------------------------- summary

    @property
    def settlement_pnl(self) -> float:
        if self.settled_won is None:
            return 0.0
        return (1 - self.entry_price) if self.settled_won else -self.entry_price

    @property
    def mfe(self) -> float:
        """Maximum favourable excursion, in dollars per contract."""
        return max((point.best - self.entry_price for point in self.path), default=0.0)

    @property
    def mae(self) -> float:
        return min((point.worst - self.entry_price for point in self.path), default=0.0)

    @property
    def peak(self) -> PathPoint | None:
        return max(self.path, key=lambda point: point.best) if self.path else None

    @property
    def trough(self) -> PathPoint | None:
        return min(self.path, key=lambda point: point.worst) if self.path else None

    @property
    def final_close(self) -> float | None:
        return self.path[-1].close if self.path else None

    def was_ever_winner(self, threshold: float = 0.02) -> bool:
        return self.mfe >= threshold

    def rescuable_loss(self, threshold: float = 0.02) -> bool:
        """Settled as a loser, but could have been closed for a profit earlier."""
        return self.settled_won is False and self.mfe >= threshold

    def squandered_win(self, threshold: float = 0.02) -> bool:
        """Settled as a winner, but was deeply underwater on the way - stop-loss risk."""
        return self.settled_won is True and self.mae <= -threshold


@dataclass(frozen=True)
class ExitResult:
    reason: str  # take_profit | stop_loss | trailing | timed | settlement | no_data
    minute: int | None
    price: float | None
    pnl: float


def exit_quotes(candle: ContractCandle, side: str) -> tuple[float, float, float] | None:
    """(best, worst, close) price at which this position could be closed."""
    if side == "UP":
        high, low, close = candle.yes_bid_high, candle.yes_bid_low, candle.yes_bid_close
        if high is None or low is None or close is None:
            return None
        return high, low, close
    high, low, close = candle.yes_ask_low, candle.yes_ask_high, candle.yes_ask_close
    if high is None or low is None or close is None:
        return None
    return round(1 - high, 4), round(1 - low, 4), round(1 - close, 4)


def build_lifecycle(
    ticker: str,
    open_ms: int,
    side: str,
    entry_minute: int,
    entry_price: float,
    settled_won: bool | None,
    candles: list[ContractCandle],
    close_ms: int | None = None,
) -> Lifecycle:
    cutoff = open_ms // 1000 + entry_minute * 60
    limit = close_ms // 1000 if close_ms else None
    path = []
    for candle in candles:
        if candle.end_period_ts <= cutoff:
            continue
        if limit is not None and candle.end_period_ts > limit:
            continue
        quotes = exit_quotes(candle, side)
        if quotes is None:
            continue
        best, worst, close = quotes
        path.append(
            PathPoint(
                minute=(candle.end_period_ts - cutoff) // 60,
                end_ts=candle.end_period_ts,
                best=best,
                worst=worst,
                close=close,
                volume=candle.volume,
            )
        )
    path.sort(key=lambda point: point.end_ts)

    reached: dict[float, int | None] = {}
    for threshold in PROFIT_LADDER:
        hit = next(
            (point.minute for point in path if point.best - entry_price >= threshold), None
        )
        reached[threshold] = hit

    return Lifecycle(
        ticker=ticker,
        open_ms=open_ms,
        side=side,
        entry_minute=entry_minute,
        entry_price=entry_price,
        settled_won=settled_won,
        path=tuple(path),
        ever_profitable_at=reached,
    )


def simulate_exit(
    lifecycle: Lifecycle,
    take_profit: float | None = None,
    stop_loss: float | None = None,
    trail: float | None = None,
    time_exit: int | None = None,
) -> ExitResult:
    """Replay one exit policy over an already-built lifecycle path.

    Thresholds are in dollars of profit/loss per contract relative to entry.

    Two conventions, both chosen to avoid flattering the policy:

    * Where a single minute touches both the take-profit and the stop, the stop
      is assumed to have been hit first. Minute candles cannot order the two
      touches, so the unfavourable ordering is used.
    * Each order fills at exactly its own trigger price, never at the minute's
      extreme. Filling a take-profit at the period high would invent profit that
      required perfect timing; filling a stop at the period low would invent a
      loss no resting stop actually takes. Symmetry keeps the comparison between
      policies honest even though real fills scatter around both.
    """
    entry = lifecycle.entry_price
    peak = entry

    for point in lifecycle.path:
        if stop_loss is not None and point.worst - entry <= -stop_loss:
            price = entry - stop_loss
            return ExitResult("stop_loss", point.minute, price, -stop_loss)
        if trail is not None and peak - point.worst >= trail and point.minute > 0:
            price = peak - trail
            return ExitResult("trailing", point.minute, price, price - entry)
        if take_profit is not None and point.best - entry >= take_profit:
            price = entry + take_profit
            return ExitResult("take_profit", point.minute, price, take_profit)
        if time_exit is not None and point.minute >= time_exit:
            return ExitResult("timed", point.minute, point.close, point.close - entry)
        peak = max(peak, point.best)

    if lifecycle.settled_won is None:
        return ExitResult("no_data", None, None, 0.0)
    return ExitResult("settlement", None, None, lifecycle.settlement_pnl)


def policy_summary(lifecycles: list[Lifecycle], bootstrap: int = 1000, **policy) -> dict:
    """Aggregate one exit policy across a set of trades.

    The ROI interval matters more here than anywhere else: exit policies are
    compared many at a time, so the best-looking row in a table of ten is partly
    a selection artefact. Without an interval it is impossible to tell a real
    improvement from the spread of the same trades re-sliced.
    """
    from .validation import block_bootstrap  # local import keeps module deps acyclic

    results = [simulate_exit(item, **policy) for item in lifecycles]
    usable = [
        (life, result)
        for life, result in zip(lifecycles, results, strict=True)
        if result.reason != "no_data"
    ]
    if not usable:
        return {"trades": 0}
    pnls = [result.pnl for _, result in usable]
    costs = [life.entry_price for life, _ in usable]
    pnl, cost = sum(pnls), sum(costs)
    wins = sum(1 for value in pnls if value > 0)
    reasons: dict[str, int] = {}
    for _, result in usable:
        reasons[result.reason] = reasons.get(result.reason, 0) + 1
    _, (roi_low, roi_high) = block_bootstrap(pnls, costs, bootstrap, seed=13)

    # Paired against holding the *same* trades to expiry. Comparing two separate
    # intervals would throw away the pairing and badly understate the evidence,
    # because both policies share every trade and most of their variance.
    deltas = [
        result.pnl - life.settlement_pnl
        for life, result in usable
        if life.settled_won is not None
    ]
    (delta_low, delta_high), _ = block_bootstrap(deltas, [1.0] * len(deltas), bootstrap, seed=17)
    mean_delta = sum(deltas) / len(deltas) if deltas else 0.0

    return {
        "trades": len(usable),
        "win_rate": wins / len(usable),
        "total_pnl": round(pnl, 4),
        "roi": round(pnl / cost, 6) if cost else 0.0,
        "roi_ci_low": round(roi_low, 6),
        "roi_ci_high": round(roi_high, 6),
        "average_entry": round(cost / len(usable), 4),
        "vs_hold_per_trade": round(mean_delta, 5),
        "vs_hold_ci_low": round(delta_low, 5),
        "vs_hold_ci_high": round(delta_high, 5),
        "beats_hold": bool(delta_low > 0),
        "exit_reasons": reasons,
    }


def lifecycle_intelligence(lifecycles: list[Lifecycle], threshold: float = 0.02) -> dict:
    """The 'was this loss ever a win?' report."""
    settled = [item for item in lifecycles if item.settled_won is not None and item.path]
    if not settled:
        return {"trades": 0}
    losers = [item for item in settled if not item.settled_won]
    winners = [item for item in settled if item.settled_won]
    rescuable = [item for item in losers if item.rescuable_loss(threshold)]
    squandered = [item for item in winners if item.squandered_win(threshold)]

    ladder = {}
    for level in PROFIT_LADDER:
        reached_losers = [item for item in losers if item.mfe >= level]
        ladder[f"{level:.2f}"] = {
            "losers_that_reached": len(reached_losers),
            "share_of_losers": round(len(reached_losers) / len(losers), 4) if losers else 0.0,
            "recoverable_dollars": round(sum(level for _ in reached_losers), 2),
            "median_minute_reached": _median(
                [
                    item.ever_profitable_at.get(level)
                    for item in reached_losers
                    if item.ever_profitable_at.get(level) is not None
                ]
            ),
        }

    return {
        "trades": len(settled),
        "winners": len(winners),
        "losers": len(losers),
        "rescuable_losses": len(rescuable),
        "rescuable_share_of_losses": round(len(rescuable) / len(losers), 4) if losers else 0.0,
        "squandered_wins": len(squandered),
        "squandered_share_of_wins": round(len(squandered) / len(winners), 4) if winners else 0.0,
        "median_loser_mfe": _median([item.mfe for item in losers]),
        "median_winner_mae": _median([item.mae for item in winners]),
        "hold_to_expiry_pnl": round(sum(item.settlement_pnl for item in settled), 2),
        "profit_ladder": ladder,
    }


def _median(values: list) -> float | None:
    clean = sorted(value for value in values if value is not None)
    if not clean:
        return None
    middle = len(clean) // 2
    if len(clean) % 2:
        return round(float(clean[middle]), 4)
    return round((clean[middle - 1] + clean[middle]) / 2, 4)
