"""Does support/resistance predict a 15-minute settlement? Measured, not assumed.

The operator's observation: the Checks block shows price band, momentum,
distance and model, and says nothing about support or resistance. That is
accurate - `key_level` exists in this codebase ONLY in the reversion strategy
(`strategy.py`), where it is set to the current window's high or low. The
primary rule has no notion of a level at all.

Before adding a gate, measure it. Section 7: "Grid search cannot find this
edge. Only pre-specified rules recover it." So ONE hypothesis is specified here
in advance, in the direction the operator's intuition points, and it is tested
once.

THE HYPOTHESIS. We are betting the price stays on its current side of the
target. The way we lose is an adverse move THROUGH the target. If a real level
sits in the path of that move, it has to break before we lose:

  * betting DOWN (price below target) - a RESISTANCE between price and target
  * betting UP   (price above target) - a SUPPORT   between target and price

So: qualifying setups WITH a blocking level should settle better than
qualifying setups without one.

NO LOOKAHEAD. A swing high at time t is only a swing high once `CONFIRM`
minutes have passed without a higher high - so it is not knowable at t, only at
t + CONFIRM. Every level used at a decision is filtered on its CONFIRMATION
time, not its formation time. Getting this wrong is the single easiest way to
manufacture a level edge that does not exist, because the pivot that "held" is
identified using the very bars that prove it held.

    python scripts/measure_levels.py
"""

import random
import sys
from bisect import bisect_left, bisect_right
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from compare_series import load  # noqa: E402

from btc15_signal.features import build_snapshots  # noqa: E402
from btc15_signal.levels import (  # noqa: E402
    CONFIRM,
    LOOKBACK_MIN,
    blocking_level,
    confirmed_pivots,
)
from btc15_signal.strategy import EntryRule  # noqa: E402
from btc15_signal.validation import (  # noqa: E402
    cluster_bootstrap,
    kalshi_fee_charged,
)


def pivots(klines):
    """Confirmed pivots from historical Candle rows, via the shared definition.

    The study and the live alert MUST agree about what a level is. Keeping a
    second copy here would let the number shown in Telegram drift away from the
    number that was measured, which is worse than showing nothing: it would
    read as confirmation of a result that was never about it.
    """
    return confirmed_pivots([(k.open_time, k.high, k.low) for k in klines])


def blocking(levels, times, now_ms: int, price: float, target: float):
    """`levels.blocking_level`, narrowed by time first so this stays tractable.

    90k snapshots against 3k pivots is 280M comparisons done naively. Bisecting
    to the eligible confirmation window first cuts it to a few dozen per
    snapshot; the shared function then re-applies the same bounds, so the
    result is identical and the slice is only an optimisation.
    """
    lo = bisect_left(times, now_ms - LOOKBACK_MIN * 60_000)
    hi = bisect_right(times, now_ms)
    return blocking_level(levels[lo:hi], now_ms, price, target)


def difference_bootstrap(blocked, clear, samples: int = 3000, seed: int = 7):
    """CI and p for mean(blocked) - mean(clear), resampling whole MARKETS.

    Two overlapping one-sample intervals do NOT establish a difference, which
    is the exact point section 16 makes about the window study. The difference
    has to be resampled directly, and on shared clusters: one market
    contributes decision minutes to BOTH arms - a level can be in the way at
    minute 9 and gone by minute 7 - so the arms are not independent samples and
    differencing two separate bootstraps would understate the uncertainty.
    """
    keys = sorted(set(blocked) | set(clear))
    if len(keys) < 2:
        return (0.0, 0.0), 1.0
    rng = random.Random(seed)
    count = len(keys)
    diffs = []
    for _ in range(samples):
        sb = cb = sc = cc = 0.0
        for _ in range(count):
            key = keys[rng.randrange(count)]
            values = blocked.get(key, ())
            sb += sum(values)
            cb += len(values)
            values = clear.get(key, ())
            sc += sum(values)
            cc += len(values)
        if cb and cc:
            diffs.append(sb / cb - sc / cc)
    if len(diffs) < samples // 2:
        return (0.0, 0.0), 1.0
    diffs.sort()
    low = diffs[int(0.025 * len(diffs))]
    high = diffs[int(0.975 * len(diffs)) - 1]
    below = sum(1 for value in diffs if value <= 0)
    return (low, high), max(1.0 / len(diffs), below / len(diffs))


def report(label: str, groups: dict[str, list[float]]) -> None:
    n = sum(len(v) for v in groups.values())
    if n < 100:
        print(f"  {label:<34} n={n:<6} too thin to measure")
        return
    mean = sum(sum(v) for v in groups.values()) / n
    (low, high), p = cluster_bootstrap(groups, 3000, 7)
    wins = sum(1 for v in groups.values() for x in v if x > 0)
    print(f"  {label:<34} n={n:<6} {wins / n:>5.1%} won  {mean:>+8.4f} "
          f"[{low:+.4f}, {high:+.4f}]  p={p:.3f}")


def main() -> None:
    rule = EntryRule.load("strategy.json")
    markets, klines, candles = load("data/market_data.db")
    if not klines:
        print("no klines - run scripts/fetch_klines.py first")
        return
    print("=" * 78)
    print("SUPPORT / RESISTANCE - measured against 15-minute settlement")
    print("=" * 78)
    print(f"\nPivot definition: a bar whose high (low) is the extreme of "
          f"+-{CONFIRM} minutes,")
    print(f"counted only from the moment it is CONFIRMED, and dropped after "
          f"{LOOKBACK_MIN // 60}h.")

    levels = pivots(klines)
    times = [p.confirmed_ms for p in levels]
    print(f"Confirmed pivots over the history: {len(levels)}")

    snapshots = build_snapshots(markets, klines, candles)
    # Two arms of ONE pre-specified split, plus the pooled figure for context.
    blocked: dict[str, list[float]] = {}
    clear: dict[str, list[float]] = {}
    pooled: dict[str, list[float]] = {}
    for snap in snapshots:
        if not 6 <= snap.remaining <= 11:
            continue
        if snap.yes_ask is None or snap.yes_bid is None:
            continue
        up, down = snap.yes_ask, 1 - snap.yes_bid
        side_is_up = up >= down
        ask = max(up, down)
        if not 0 < ask < 1:
            continue
        # The deployed gates, so this measures the rule as it trades.
        if not rule.min_ask <= ask <= rule.max_ask:
            continue
        vol = max(snap.volatility_5m_bps, 1.0)
        if abs(snap.signed_distance_bps) / vol < rule.min_normalized_distance:
            continue
        won = (snap.result == "yes") if side_is_up else (snap.result == "no")
        net = (1.0 if won else 0.0) - ask - kalshi_fee_charged(ask, 1)
        now_ms = snap.open_ms + snap.elapsed * 60_000
        level = blocking(levels, times, now_ms, snap.price, snap.target)
        pooled.setdefault(snap.ticker, []).append(net)
        (blocked if level is not None else clear).setdefault(
            snap.ticker, []).append(net)

    print(f"\nDeployed rule, 6-11 minutes left, band "
          f"{rule.min_ask:.2f}-{rule.max_ask:.2f}:")
    report("ALL qualifying setups", pooled)
    print("\nThe pre-specified split:")
    report("a level BLOCKS the adverse move", blocked)
    print("    (resistance below the target on a DOWN bet, support above it on UP)")
    report("no level in the way", clear)

    nb = sum(len(v) for v in blocked.values())
    nc = sum(len(v) for v in clear.values())
    if nb >= 100 and nc >= 100:
        mb = sum(sum(v) for v in blocked.values()) / nb
        mc = sum(sum(v) for v in clear.values()) / nc
        (dlo, dhi), dp = difference_bootstrap(blocked, clear)
        print("")
        print("  THE ACTUAL TEST - the difference, bootstrapped directly:")
        print(f"  blocked - clear                    {mb - mc:+.4f}/contract "
              f"[{dlo:+.4f}, {dhi:+.4f}]  p={dp:.3f}")
        if dlo > 0:
            print("  => the interval EXCLUDES zero: a blocking level is")
            print("     associated with a better result, on this history.")
        else:
            print("  => the interval INCLUDES zero. The two arms look")
            print("     different but this data does not establish that they")
            print("     are, and the one-sample intervals above overlap.")
    print("\n" + "=" * 78)


if __name__ == "__main__":
    main()
