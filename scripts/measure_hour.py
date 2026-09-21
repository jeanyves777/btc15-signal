"""Hour of day, measured at hourly resolution and as ONE pre-registered test.

Section 4 measured five sessions in 4-hour blocks and found every interval
overlapping every other, so it forbade session data from reaching the trade
path. Section 8 re-tested the New York session against volatility and again
adopted nothing, closing with an instruction: "Re-test as a single
pre-registered comparison; do not re-slice."

This is that comparison. The operator's observation on 2026-09-21, in local
time (UTC-4): the New York morning won on everything, and from about 1pm the
losses started. 1pm local is **17:00 UTC**, which is exactly where section 4's
weakest US block begins - `US pm 17-20`, +0.0116 [-0.0096, +0.0334].

THE HYPOTHESIS, fixed before running: entries taken at 17:00-20:59 UTC do worse
than entries taken at any other hour. One test, one difference, bootstrapped
directly on shared market clusters. The hourly table is printed for context
BELOW the test, deliberately, so the test is not chosen from the table.

    python scripts/measure_hour.py
"""

import random
import sys
from datetime import UTC, datetime
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

SUSPECT_HOURS = (17, 18, 19, 20)  # 1pm-5pm for an operator at UTC-4


def entries(snapshots, rule: EntryRule):
    """One entry per market, deployed gates, first qualifying minute."""
    by_market: dict[str, list] = {}
    for snap in snapshots:
        by_market.setdefault(snap.ticker, []).append(snap)
    out = []
    for ticker, snaps in by_market.items():
        snaps.sort(key=lambda s: -s.remaining)
        for snap in snaps:
            if not 6 <= snap.remaining <= 11:
                continue
            if snap.yes_ask is None or snap.yes_bid is None:
                continue
            up, down = snap.yes_ask, 1 - snap.yes_bid
            side_is_up = up >= down
            ask = max(up, down)
            if not 0 < ask < 1 or not rule.min_ask <= ask <= rule.max_ask:
                continue
            vol = max(snap.volatility_5m_bps, 1.0)
            if abs(snap.signed_distance_bps) / vol < rule.min_normalized_distance:
                continue
            won = (snap.result == "yes") if side_is_up else (snap.result == "no")
            hour = datetime.fromtimestamp(snap.open_ms / 1000, UTC).hour
            out.append((ticker, hour, (1.0 if won else 0.0) - ask
                        - kalshi_fee_charged(ask, 1)))
            break
    return out


def difference(a: dict, b: dict, samples: int = 3000, seed: int = 7):
    """mean(a) - mean(b), resampling whole markets, on shared clusters."""
    keys = sorted(set(a) | set(b))
    if len(keys) < 2:
        return (0.0, 0.0), 1.0
    rng = random.Random(seed)
    count = len(keys)
    diffs = []
    for _ in range(samples):
        sa = ca = sb = cb = 0.0
        for _ in range(count):
            key = keys[rng.randrange(count)]
            va, vb = a.get(key, ()), b.get(key, ())
            sa += sum(va)
            ca += len(va)
            sb += sum(vb)
            cb += len(vb)
        if ca and cb:
            diffs.append(sa / ca - sb / cb)
    if not diffs:
        return (0.0, 0.0), 1.0
    diffs.sort()
    below = sum(1 for d in diffs if d >= 0)
    return (
        (diffs[int(0.025 * len(diffs))], diffs[int(0.975 * len(diffs)) - 1]),
        max(1.0 / len(diffs), below / len(diffs)),
    )


def main() -> None:
    rule = EntryRule.load("strategy.json")
    markets, klines, candles = load("data/market_data.db")
    if not klines:
        print("no klines - run scripts/fetch_klines.py first")
        return
    rows = entries(build_snapshots(markets, klines, candles), rule)
    if not rows:
        print("no qualifying entries")
        return

    print("=" * 78)
    print("HOUR OF DAY - one pre-registered test, then the table")
    print("=" * 78)
    print(f"\nBand {rule.min_ask:.2f}-{rule.max_ask:.2f}, 6-11 minutes left, "
          f"one entry per market, n={len(rows)}.")

    suspect: dict[str, list[float]] = {}
    rest: dict[str, list[float]] = {}
    for ticker, hour, net in rows:
        (suspect if hour in SUSPECT_HOURS else rest).setdefault(
            ticker, []).append(net)
    ns = sum(len(v) for v in suspect.values())
    nr = sum(len(v) for v in rest.values())
    ms = sum(sum(v) for v in suspect.values()) / max(ns, 1)
    mr = sum(sum(v) for v in rest.values()) / max(nr, 1)
    (lo, hi), p = difference(suspect, rest)

    print("\n1. THE TEST - 17:00-20:59 UTC (1pm-5pm local) vs every other hour")
    print(f"  17-20 UTC        n={ns:<6} {ms:+.4f}/ct")
    print(f"  all other hours  n={nr:<6} {mr:+.4f}/ct")
    print(f"  DIFFERENCE       {ms - mr:+.4f}/ct  [{lo:+.4f}, {hi:+.4f}]  p={p:.3f}")
    if hi < 0:
        print("  => the afternoon is WORSE and the interval excludes zero.")
    elif lo > 0:
        print("  => the afternoon is BETTER and the interval excludes zero.")
    else:
        print("  => the interval INCLUDES zero. The afternoon is not shown to")
        print("     differ, whatever the point estimate reads.")

    print("\n2. THE TABLE - context only. The test above was fixed first, so")
    print("   that nothing here could have chosen it.")
    print(f"  {'hour':>5}{'n':>7}{'won':>8}{'net/ct':>10}  {'95% CI':>22}")
    for hour in range(24):
        groups: dict[str, list[float]] = {}
        for ticker, h, net in rows:
            if h == hour:
                groups.setdefault(ticker, []).append(net)
        n = sum(len(v) for v in groups.values())
        if n < 60:
            print(f"  {hour:>5}{n:>7}   too thin")
            continue
        mean = sum(sum(v) for v in groups.values()) / n
        wins = sum(1 for v in groups.values() for x in v if x > 0)
        (clo, chi), _ = cluster_bootstrap(groups, 2000, 7)
        mark = "  <-" if hour in SUSPECT_HOURS else ""
        print(f"  {hour:>5}{n:>7}{wins / n:>8.1%}{mean:>+10.4f}  "
              f"[{clo:+.4f}, {chi:+.4f}]{mark}")

    print("\n" + "=" * 78)
    print("Section 4 forbade session data in the trade path because every")
    print("interval overlapped every other. Line 1 is the only thing that can")
    print("overturn that, and it is the only line here that was specified")
    print("before the data was looked at.")
    print("=" * 78)


if __name__ == "__main__":
    main()
