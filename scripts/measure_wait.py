"""Enter now, or wait for a better price? Measured, because nobody had.

The operator's question: "the distance is enough for a 70% win rate to enter
now - or wait until the last five minutes when the price retraces and gives us
a better level to enter for better profit."

That is a real question and the system currently has no opinion on it at all.
It enters at the FIRST qualifying minute (plus the settle timer) and has no
concept of a better entry later. Section 16 measured fixed entry WINDOWS - 6-10
minutes beats 5-11 beats 4-12 - but a fixed window is not a waiting policy: it
never says "this setup qualifies now, hold on for a cheaper ask".

The catch, stated before the numbers: a cheaper ask is not free. The price IS
the probability, so an ask that drifts from 0.88 to 0.80 has not handed us a
discount - it has told us the trade got less likely. Waiting pays only if the
ask falls further than the true odds did, which is an empirical question and
the reason this script exists rather than an argument.

POLICIES, all on the same markets, one entry each, deployed gates:

  now        enter at the first qualifying minute            (deployed)
  patient    wait while the ask keeps improving, take the best available
             before the window closes; fall back to the last qualifying minute
  last       always enter at the LAST qualifying minute in the window
  late       ignore the entry window; enter in the final five minutes if the
             rule still qualifies there

The side is fixed at the first qualifying minute and every later quote - and
the outcome - is read off that same contract. Without that, `patient` took
min(ask) across rows whose outcome came from whichever side was favourite at
that minute, so a market that flipped mid-window scored the cheap quote of one
contract against the other contract's result: not a waiting policy at all.

    python scripts/measure_wait.py
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


def qualifies(snap, rule: EntryRule, side_is_up: bool | None = None):
    """(ok, ask, side_is_up) under the deployed price and distance gates.

    `side_is_up` of None takes whichever side is the favourite at this minute;
    a fixed side prices and gates that contract only.
    """
    if snap.yes_ask is None or snap.yes_bid is None:
        return False, 0.0, False
    up, down = snap.yes_ask, 1 - snap.yes_bid
    if side_is_up is None:
        side_is_up = up >= down
    ask = up if side_is_up else down
    if not 0 < ask < 1 or not rule.min_ask <= ask <= rule.max_ask:
        return False, ask, side_is_up
    vol = max(snap.volatility_5m_bps, 1.0)
    if abs(snap.signed_distance_bps) / vol < rule.min_normalized_distance:
        return False, ask, side_is_up
    return True, ask, side_is_up


def net(ask: float, won: bool) -> float:
    return (1.0 if won else 0.0) - ask - kalshi_fee_charged(ask, 1)


def policies(snapshots, rule: EntryRule):
    by_market: dict[str, list] = {}
    for snap in snapshots:
        by_market.setdefault(snap.ticker, []).append(snap)

    out: dict[str, dict[str, list[float]]] = {
        name: {} for name in ("now", "patient", "last", "late")
    }
    improved = worse = same = 0
    drifts: list[float] = []
    for ticker, snaps in by_market.items():
        snaps.sort(key=lambda s: -s.remaining)
        side = None   # the contract, fixed at the first qualifying minute
        window = []   # qualifying minutes inside the deployed entry window
        late = []     # qualifying minutes in the final five
        for snap in snaps:
            in_window = 6 <= snap.remaining <= 11
            if not in_window and snap.remaining > 5:
                continue
            # Every arm has to price ONE contract. `patient` took
            # min(window, key=ask) over rows whose `won` came from whichever
            # side was favourite at that minute, so a market that flipped
            # mid-window let it buy the newly-cheap OTHER contract and score
            # it against the OTHER outcome - a side switch, not waiting. It
            # showed up as `last` winning 81.3% where `now` won 79.9% on the
            # same 5088 markets - impossible when the outcome is the market's.
            ok, ask, up = qualifies(snap, rule, side)
            if not ok:
                continue
            if side is None:
                side = up
            won = (snap.result == "yes") if side else (snap.result == "no")
            if in_window:
                window.append((snap.remaining, ask, won))
            if snap.remaining <= 5:
                late.append((snap.remaining, ask, won))
        if window:
            first = window[0]
            out["now"].setdefault(ticker, []).append(net(first[1], first[2]))
            # `patient` takes the cheapest ask the FIXED side still offered.
            best = min(window, key=lambda row: row[1])
            out["patient"].setdefault(ticker, []).append(net(best[1], best[2]))
            last = window[-1]
            out["last"].setdefault(ticker, []).append(net(last[1], last[2]))
            drifts.append(last[1] - first[1])
            if best[1] < first[1] - 1e-9:
                improved += 1
            elif last[1] > first[1] + 1e-9:
                worse += 1
            else:
                same += 1
        if late:
            first_late = late[0]
            out["late"].setdefault(ticker, []).append(
                net(first_late[1], first_late[2])
            )
    return out, improved, worse, same, drifts


def paired(a: dict, b: dict, samples: int = 3000, seed: int = 7):
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
    below = sum(1 for d in diffs if d <= 0)
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
    snaps = build_snapshots(markets, klines, candles)
    arms, improved, worse, same, drifts = policies(snaps, rule)

    print("=" * 78)
    print("ENTER NOW, OR WAIT FOR A BETTER PRICE?")
    print("=" * 78)
    print(f"\nBand {rule.min_ask:.2f}-{rule.max_ask:.2f}, distance "
          f">={rule.min_normalized_distance:.1f}x vol. One entry per market.")

    print("\n1. DOES THE ASK ACTUALLY IMPROVE WHILE YOU WAIT?")
    total = improved + worse + same
    if total:
        print(f"  markets where a cheaper ask appeared later  {improved} "
              f"({improved / total:.0%})")
        print(f"  markets where it only got dearer            {worse} "
              f"({worse / total:.0%})")
        print(f"  unchanged                                   {same} "
              f"({same / total:.0%})")
    if drifts:
        mean_drift = sum(drifts) / len(drifts)
        print(f"  mean ask drift, first to last qualifying    {mean_drift:+.4f}")
        print("  A cheaper ask is not a discount - the price IS the")
        print("  probability, so a falling ask says the trade got less likely.")

    print("\n2. THE POLICIES")
    print(f"  {'policy':<10}{'n':>7}{'won':>8}{'avg ask':>10}{'net/ct':>10}"
          f"  {'95% CI':>22}{'total':>9}")
    means = {}
    for name in ("now", "patient", "last", "late"):
        groups = arms[name]
        n = sum(len(v) for v in groups.values())
        if n < 100:
            print(f"  {name:<10}{n:>7}   too thin")
            continue
        tot = sum(sum(v) for v in groups.values())
        mean = tot / n
        means[name] = mean
        wins = sum(1 for v in groups.values() for x in v if x > 0)
        (lo, hi), _p = cluster_bootstrap(groups, 3000, 7)
        flag = "  <- DEPLOYED" if name == "now" else ""
        print(f"  {name:<10}{n:>7}{wins / n:>8.1%}"
              f"{'':>10}{mean:>+10.4f}  [{lo:+.4f}, {hi:+.4f}]{tot:>+9.2f}{flag}")

    print("\n3. THE TESTS - each waiting policy against entering now")
    for name in ("patient", "last", "late"):
        if name not in means:
            continue
        (lo, hi), p = paired(arms[name], arms["now"])
        verdict = ("BETTER" if lo > 0 else "WORSE" if hi < 0 else "no difference")
        print(f"  {name:<10} minus now   {means[name] - means['now']:+.4f}/ct  "
              f"[{lo:+.4f}, {hi:+.4f}]  p={p:.3f}  {verdict}")

    print("\n  `patient` is the optimistic bound on waiting: it is allowed to")
    print("  pick the CHEAPEST ask the side it entered ever showed, which no")
    print("  live rule could do without foresight. If even that does not beat")
    print("  entering now, no realistic waiting rule will.")
    print("=" * 78)


if __name__ == "__main__":
    main()
