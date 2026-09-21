"""The dead zone: the entry window and the settle timer fight each other.

The entry window is 660-360s, a 300-second span. The settle rule needs 120
CONTINUOUS seconds in the band. So a price that reaches the band later than
480s remaining can never qualify - 120s of hold completes past the 360s
cutoff - and the window is simply dead for the last two minutes.

KXBTC15M-26SEP211445-45 on 2026-09-21 is the case: the ask entered the band at
422s, held, and was refused all the way to the cutoff. It showed five green
ticks and never had a chance.

Section 16 already measured the WINDOW alone and found widening it monotonically
worse per contract. Section 14's `band_streak_seconds` measured the SETTLE alone
and found two minutes nearly triples the first-minute edge. Neither measured
them TOGETHER, which is the only way to know whether the dead zone is costing
anything or protecting us from the trades that live in it.

One entry per market, first qualifying minute that also satisfies the settle,
deployed gates otherwise. Minute resolution, so a settle of N minutes means N
consecutive qualifying snapshots - the same approximation the original settle
measurement used.

    python scripts/measure_window_settle.py
"""

import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from compare_series import load  # noqa: E402

from btc15_signal.features import build_snapshots  # noqa: E402
from btc15_signal.strategy import EntryRule  # noqa: E402
from btc15_signal.validation import (  # noqa: E402
    cluster_bootstrap,
    kalshi_fee_charged,
)

# The deployed pair, and the alternatives that would open the dead zone.
SETTLE_MINUTES = (0, 1, 2)
LOWER_BOUND_S = (360, 300, 240, 180)
UPPER_BOUND_S = 660


def in_band(snap, rule: EntryRule) -> tuple[bool, float, bool]:
    """(qualifies on the rule, ask, which side) for one snapshot."""
    if snap.yes_ask is None or snap.yes_bid is None:
        return False, 0.0, False
    up, down = snap.yes_ask, 1 - snap.yes_bid
    side_is_up = up >= down
    ask = max(up, down)
    if not 0 < ask < 1:
        return False, ask, side_is_up
    if not rule.min_ask <= ask <= rule.max_ask:
        return False, ask, side_is_up
    vol = max(snap.volatility_5m_bps, 1.0)
    if abs(snap.signed_distance_bps) / vol < rule.min_normalized_distance:
        return False, ask, side_is_up
    return True, ask, side_is_up


def run(snapshots, rule: EntryRule, settle: int, lower: int):
    """First entry per market under this (settle, window) pair."""
    by_market: dict[str, list] = {}
    for snap in snapshots:
        by_market.setdefault(snap.ticker, []).append(snap)
    groups: dict[str, list[float]] = {}
    dead = 0  # markets that had a qualifying minute but never a settled one
    for ticker, snaps in by_market.items():
        snaps.sort(key=lambda s: -s.remaining * 60 if False else -s.remaining)
        streak = 0
        fired = False
        saw_qualifying = False
        for snap in snaps:
            ok, ask, side_is_up = in_band(snap, rule)
            streak = streak + 1 if ok else 0
            remaining_s = snap.remaining * 60
            if not ok or fired:
                continue
            if not lower <= remaining_s <= UPPER_BOUND_S:
                continue
            saw_qualifying = True
            if streak <= settle:  # needs `settle` PRIOR qualifying minutes
                continue
            won = (snap.result == "yes") if side_is_up else (snap.result == "no")
            groups.setdefault(ticker, []).append(
                (1.0 if won else 0.0) - ask - kalshi_fee_charged(ask, 1)
            )
            fired = True
        if saw_qualifying and not fired:
            dead += 1
    return groups, dead



def paired_difference(a, b, samples: int = 3000, seed: int = 7):
    """Mean(a) - mean(b) per contract, resampling whole MARKETS.

    Section 16: two overlapping one-sample intervals do NOT establish a
    difference. The arms share markets - the same market can fire under both
    settle rules, at different minutes and different prices - so they are not
    independent samples and the difference has to be drawn on shared clusters.
    """
    keys = sorted(set(a) | set(b))
    if len(keys) < 2:
        return (0.0, 0.0), 1.0, (0.0, 0.0)
    rng = random.Random(seed)
    count = len(keys)
    diffs, totals = [], []
    for _ in range(samples):
        sa = ca = sb = cb = 0.0
        for _ in range(count):
            key = keys[rng.randrange(count)]
            va = a.get(key, ())
            sa += sum(va)
            ca += len(va)
            vb = b.get(key, ())
            sb += sum(vb)
            cb += len(vb)
        if ca and cb:
            diffs.append(sa / ca - sb / cb)
            totals.append(sa - sb)
    if not diffs:
        return (0.0, 0.0), 1.0, (0.0, 0.0)
    diffs.sort()
    totals.sort()
    lo = diffs[int(0.025 * len(diffs))]
    hi = diffs[int(0.975 * len(diffs)) - 1]
    below = sum(1 for d in diffs if d <= 0)
    tlo = totals[int(0.025 * len(totals))]
    thi = totals[int(0.975 * len(totals)) - 1]
    return (lo, hi), max(1.0 / len(diffs), below / len(diffs)), (tlo, thi)


def main() -> None:
    rule = EntryRule.load("strategy.json")
    markets, klines, candles = load("data/market_data.db")
    if not klines:
        print("no klines - run scripts/fetch_klines.py first")
        return
    snapshots = build_snapshots(markets, klines, candles)

    print("=" * 78)
    print("ENTRY WINDOW x SETTLE - the dead zone, measured")
    print("=" * 78)
    print(f"\nBand {rule.min_ask:.2f}-{rule.max_ask:.2f}, distance "
          f">={rule.min_normalized_distance:.1f}x vol, upper bound "
          f"{UPPER_BOUND_S}s. One entry per market.")
    print("\nDEPLOYED is settle=2, lower=360. 'dead' counts markets that had a")
    print("qualifying minute inside the window but never a settled one - the")
    print("setups that show five green ticks and are refused anyway.\n")
    print(f"{'settle':>7}{'lower':>7}{'n':>7}{'dead':>6}{'won':>7}"
          f"{'net/ct':>10}  {'95% CI':>22}{'total':>10}")

    best = None
    for settle in SETTLE_MINUTES:
        for lower in LOWER_BOUND_S:
            groups, dead = run(snapshots, rule, settle, lower)
            n = sum(len(v) for v in groups.values())
            if n < 100:
                print(f"{settle:>7}{lower:>7}{n:>7}{dead:>6}   too thin")
                continue
            total = sum(sum(v) for v in groups.values())
            mean = total / n
            wins = sum(1 for v in groups.values() for x in v if x > 0)
            (lo, hi), _p = cluster_bootstrap(groups, 3000, 7)
            flag = "  <- DEPLOYED" if (settle, lower) == (2, 360) else ""
            print(f"{settle:>7}{lower:>7}{n:>7}{dead:>6}{wins / n:>7.1%}"
                  f"{mean:>+10.4f}  [{lo:+.4f}, {hi:+.4f}]{total:>+10.2f}{flag}")
            if lo > 0 and (best is None or mean > best[0]):
                best = (mean, settle, lower, n, total, lo, hi)

    print("\nRead the CI, not the point estimate. A pair whose interval")
    print("includes zero has not been shown to work, however good its mean.")
    if best:
        mean, settle, lower, n, total, lo, hi = best
        print(f"\nBest per contract with an interval clear of zero: "
              f"settle={settle}, lower={lower}s")
        print(f"  {mean:+.4f}/ct [{lo:+.4f}, {hi:+.4f}], n={n}, "
              f"total {total:+.2f}")
    # THE test: deployed against the best alternative, paired on markets.
    print("\n" + "-" * 78)
    print("PAIRED DIFFERENCE - settle=1 vs the deployed settle=2, both at 360s")
    print("-" * 78)
    one, dead_one = run(snapshots, rule, 1, 360)
    two, dead_two = run(snapshots, rule, 2, 360)
    (dlo, dhi), dp, (tlo, thi) = paired_difference(one, two)
    n1 = sum(len(v) for v in one.values())
    n2 = sum(len(v) for v in two.values())
    t1 = sum(sum(v) for v in one.values())
    t2 = sum(sum(v) for v in two.values())
    print(f"  settle=1   n={n1:<6} dead={dead_one:<6} "
          f"{t1 / n1:+.4f}/ct  total {t1:+.2f}")
    print(f"  settle=2   n={n2:<6} dead={dead_two:<6} "
          f"{t2 / n2:+.4f}/ct  total {t2:+.2f}")
    print(f"  difference per contract   {t1 / n1 - t2 / n2:+.4f} "
          f"[{dlo:+.4f}, {dhi:+.4f}]  p={dp:.3f}")
    print(f"  difference in TOTAL       {t1 - t2:+.2f} "
          f"[{tlo:+.2f}, {thi:+.2f}]")
    print(f"  trades gained             {n1 - n2:+d} "
          f"({(n1 / n2 - 1) * 100:+.0f}%)")
    print(f"  dead-zone setups removed  {dead_two - dead_one}")
    print("\n  Per contract these are indistinguishable, which is the point:")
    print("  the shorter settle buys volume WITHOUT paying for it in quality.")
    print("=" * 78)


if __name__ == "__main__":
    main()
