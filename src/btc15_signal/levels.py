"""Support and resistance: one definition, shared by the study and the alert.

`scripts/measure_levels.py` tested whether a level standing in the way of the
move that beats us is worth anything. It measured +0.0140/contract with a
directly-bootstrapped interval of [-0.0067, +0.0344], p=0.090 - the largest
single-feature effect in FINDINGS and still not established at n=15,369.

So this is DISPLAY ONLY. Nothing here may gate a trade. It exists because the
operator could see price band, momentum, distance and model in the alert and
not the one thing a chart makes obvious, and because a number you can watch
accumulate is how the next version of that measurement gets its sample.

The study and the alert import the same two functions on purpose. A level shown
in Telegram that was computed differently from the level that was measured is
worse than showing nothing: it would read as confirmation of a result that was
never about it.

LATENCY. Levels need about a day of one-minute bars; the live snapshot fetches
sixteen. That is a second network call, and on 2026-09-21 a 2-second gap
between deciding and submitting cost three orders (FINDINGS section 22). So the
tracker refreshes on its own slow clock, well off the order path, and the alert
only ever reads the cache.
"""

from dataclasses import dataclass

# Picked once, before the study was run, and deliberately not tuned: tuning
# them across a grid is what section 7 says produces rules that do not survive.
CONFIRM = 30        # minutes a pivot must survive on both sides to count
LOOKBACK_MIN = 1440  # a level older than 24h is not treated as live
REFRESH_S = 300      # how often the bar history is refetched
BARS = 1500          # 25h of one-minute bars, Binance's per-request maximum


@dataclass(frozen=True)
class Pivot:
    confirmed_ms: int
    price: float
    kind: str  # "resistance" | "support"


def confirmed_pivots(bars, confirm: int = CONFIRM) -> list[Pivot]:
    """Swing highs and lows, timestamped when they became KNOWABLE.

    `bars` is (open_time_ms, high, low), oldest first.

    A bar is a swing high if nothing within +-confirm traded higher. That fact
    is not available at the bar itself - it takes `confirm` more minutes to
    establish - so the pivot is stamped at the END of the forward window. Using
    the formation time instead would identify a level with the very bars that
    prove it held, which is the easiest way to manufacture an edge that is not
    there.
    """
    if len(bars) < 2 * confirm + 1:
        return []
    times = [b[0] for b in bars]
    highs = [b[1] for b in bars]
    lows = [b[2] for b in bars]
    out: list[Pivot] = []
    for i in range(confirm, len(bars) - confirm):
        window = slice(i - confirm, i + confirm + 1)
        confirmed = times[i + confirm]
        if highs[i] == max(highs[window]):
            out.append(Pivot(confirmed, highs[i], "resistance"))
        if lows[i] == min(lows[window]):
            out.append(Pivot(confirmed, lows[i], "support"))
    out.sort(key=lambda p: p.confirmed_ms)
    return out


def protective_level(
    pivots: list[Pivot],
    now_ms: int,
    price: float,
    target: float,
    lookback_min: int = LOOKBACK_MIN,
) -> Pivot | None:
    """The level SHIELDING us, or None.

    PROTECTIVE, not an obstacle. "Blocking" read both ways and the sign looked
    backwards because of it: this is a level standing between BTC and the
    target, so it blocks the move that would BEAT us, and having one is good
    news. Betting DOWN, price below target, the danger is a rally and only a
    resistance in between stands in its way; betting UP it is the mirror.

    The old name is kept as an alias below because a rename alone must not
    break a caller.

    We are betting the price stays on its side of the target, so we lose to a
    move THROUGH the target. Below the target the danger is a rally, and only a
    resistance in between stands in its way; above the target it is the mirror.
    Only pivots already confirmed at `now_ms`, and no older than the lookback,
    are eligible.
    """
    want = "resistance" if price < target else "support"
    low, high = (price, target) if price < target else (target, price)
    floor_ms = now_ms - lookback_min * 60_000
    best: Pivot | None = None
    for pivot in pivots:
        if pivot.confirmed_ms > now_ms or pivot.confirmed_ms < floor_ms:
            continue
        if pivot.kind != want or not low < pivot.price < high:
            continue
        # Whichever one has to break FIRST is the one nearest the price.
        if best is None or abs(pivot.price - price) < abs(best.price - price):
            best = pivot
    return best


class LevelTracker:
    """Caches confirmed pivots and refreshes them off the critical path."""

    def __init__(self, refresh_s: int = REFRESH_S) -> None:
        self.pivots: list[Pivot] = []
        self.refreshed_ms = 0
        self.refresh_s = refresh_s

    def due(self, now_ms: int) -> bool:
        return now_ms - self.refreshed_ms >= self.refresh_s * 1000

    async def maybe_refresh(self, client, now_ms: int) -> None:
        """Refetch the bar history if it is stale. NEVER raises.

        Called after the trading path, like the hourly shadow recorder. A
        Binance hiccup here must leave the previous levels in place and the
        trading loop untouched - a display feature cannot be allowed to stop
        the service or delay an order.
        """
        if not self.due(now_ms):
            return
        try:
            bars = await client.recent_bars(BARS)
        except Exception:  # noqa: BLE001 - a display cache, never fatal
            return
        if bars:
            self.pivots = confirmed_pivots(bars)
            self.refreshed_ms = now_ms

    def protecting(self, now_ms: int, price: float, target: float) -> Pivot | None:
        return protective_level(self.pivots, now_ms, price, target)

    # Old name, kept so a rename cannot break a caller.
    blocking = protecting

    def describe(self, now_ms: int, price: float, target: float) -> str | None:
        """One line for the alert, or None when there is nothing to say yet."""
        if not self.pivots:
            return None
        pivot = self.protecting(now_ms, price, target)
        if pivot is None:
            return "nothing shielding the target"
        gap_bps = abs(pivot.price - price) / max(price, 1e-9) * 10_000
        return (
            f"{pivot.kind} ${pivot.price:,.0f} shields the target "
            f"({gap_bps:.0f} bps away)"
        )

# --------------------------------------------------------------- confidence
# Measured over 71 days (`scripts/measure_levels.py`), deployed band:
#
#   a PROTECTIVE level shields the target  +0.0198 [+0.0049, +0.0341]  n=7370
#   nothing between price and target       +0.0059 [-0.0093, +0.0202]  n=7999
#   pooled                            +0.0126 [+0.0025, +0.0223]  n=15369
#   DIFFERENCE                        +0.0140 [-0.0067, +0.0344]  p=0.090
#
# The difference does not clear zero, which is why this is a confidence
# contributor and NOT a gate. It was briefly deployed as a gate on 2026-09-21
# and that was wrong: it refused about half of all qualifying setups on
# evidence that does not establish the effect.
PROTECTED_EDGE = 0.0198
UNPROTECTED_EDGE = 0.0059
POOLED_EDGE = 0.0126
DIFF_SE = 0.0105   # from the bootstrapped interval, (hi - lo) / (2 * 1.96)
SCALE = 0.05       # same unit as the regime lean, so the points are comparable
MAX_POINTS = 15


def _shrink() -> float:
    """How much of the measured difference survives its own uncertainty.

    James-Stein in one dimension: an effect no larger than its standard error
    is indistinguishable from noise and is pulled to nothing. At the measured
    +0.0140 against se 0.0105 this keeps a little under half.
    """
    effect = PROTECTED_EDGE - UNPROTECTED_EDGE
    if effect <= 0:
        return 0.0
    return max(0.0, 1.0 - (DIFF_SE ** 2) / (effect ** 2))


def confidence_points(has_protective_level: bool) -> int:
    """Level structure as confidence points. Never a gate, never a size.

    Same currency as the time-of-day lean, so the two adjustments add and a
    reader can compare them directly.
    """
    arm = PROTECTED_EDGE if has_protective_level else UNPROTECTED_EDGE
    shrunk = POOLED_EDGE + _shrink() * (arm - POOLED_EDGE)
    points = round((shrunk - POOLED_EDGE) / SCALE * 100)
    return int(max(-MAX_POINTS, min(MAX_POINTS, points)))


# Old name, kept so a rename cannot break a caller.
blocking_level = protective_level
