"""Evaluate an instance by what happened to each MARKET, not by one snapshot.

Replaces every report that read `predictions.qualified` as a market's verdict.
That flag is the alert-time snapshot and is preserved as such; reading it as
the classification made BTC look as though its gate selected the wrong setups,
when 9 of the markets it counted as "declined" had actually been traded, 9W/0L.

    python scripts/lifecycle_report.py --db btc15.db --since-ms 1790285639454

WHAT THE NUMBERS DO AND DO NOT SHOW. A residual here is win rate minus the
price the market was quoting - a calibration measure, not realised profit. It
excludes fees and assumes a fill at the quoted price, which is exactly the
addition counterfactual this system cannot verify. And separating eligible from
never-eligible shows the gate SELECTS; whether it is worth its cost in refused
volume is a different question that these sample sizes cannot answer.
"""

import argparse
import statistics
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from btc15_signal import market_lifecycle as lc  # noqa: E402


def norm(price):
    if price is None:
        return None
    return price / 100 if price > 1 else price


def scored(lives, keep):
    rows = [x for x in lives if keep(x) and x.outcome in (lc.WON, lc.LOST)
            and norm(x.contract_price) is not None]
    if not rows:
        return None
    win = sum(1 for x in rows if x.outcome == lc.WON) / len(rows)
    ask = statistics.mean(norm(x.contract_price) for x in rows)
    return len(rows), win, ask, win - ask


def line(label, s):
    if not s:
        return f"  {label:<26} -"
    n, win, ask, res = s
    return (f"  {label:<26} n={n:<4} {win:>6.1%}  price {ask:.3f}  "
            f"residual {res:>+6.1%}")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--db", default="btc15.db")
    p.add_argument("--since-ms", type=int, default=0)
    p.add_argument("--label", default="")
    args = p.parse_args()

    lives = list(lc.build(args.db, args.since_ms).values())
    if not lives:
        print(f"{args.db}: no decisions since the cutoff")
        return
    name = args.label or args.db
    when = (datetime.fromtimestamp(args.since_ms / 1000, timezone.utc)
            .strftime("%Y-%m-%d %H:%M UTC") if args.since_ms else "all time")
    # TIMESTAMP EVERY RUN. Counts move as markets settle and fills land -
    # gold's eligible set went 32 -> 33 and ETH's fills 14 -> 15 between two
    # runs an hour apart - and an untimestamped count invites reading that as
    # a reconciliation error rather than a later snapshot.
    taken = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    print(f"=== {name} - market lifecycle since {when} ===")
    print(f"  snapshot taken {taken}")
    print(f"  markets {len(lives)}   evaluations "
          f"{sum(x.evaluations for x in lives)}")
    # Reported as a SNAPSHOT of today's setting, because `settings` keeps one
    # current value with no history - the value at any decision instant is not
    # recoverable. None is "no row recorded", which is not the same as "off".
    autos = {x.automation_on_at_snapshot for x in lives}
    auto = ("on" if autos == {True} else "off" if autos == {False}
            else "not recorded" if autos == {None} else "mixed")
    auth = Counter(x.authorization for x in lives)
    print(f"  automation_on_at_snapshot: {auto}   authorization: "
          f"{', '.join(f'{k} {v}' for k, v in auth.most_common())}")
    print("  (no approval-request instrumentation exists, so authorization "
          "cannot be established either way)")

    print("\n  ELIGIBILITY (derived from every evaluation, not the alert)")
    for state in (lc.ELIGIBLE_AT_FIRST, lc.BECAME_ELIGIBLE, lc.NEVER_ELIGIBLE):
        sel = [x for x in lives if x.eligibility == state]
        extra = ""
        if state == lc.BECAME_ELIGIBLE and sel:
            rem = [x.first_eligible_remaining_s for x in sel
                   if x.first_eligible_remaining_s is not None]
            if rem:
                extra = (f"   first eligible with a median "
                         f"{statistics.median(rem):.0f}s left")
        print(f"    {state:<22} {len(sel):>4}{extra}")

    print("\n  EXECUTION (separate from eligibility, and from the strategy)")
    for state in (lc.FILLED, lc.PARTIALLY_FILLED, lc.SUBMITTED_UNFILLED,
                  lc.AWAITING_AUTHORIZATION, lc.AUTO_DISABLED,
                  lc.EXPIRED, lc.BLOCKED, lc.NO_PROPOSAL):
        sel = [x for x in lives if x.execution == state]
        if not sel:
            continue
        elig = sum(1 for x in sel if x.ever_eligible)
        reasons = Counter(x.execution_reason for x in sel if x.execution_reason)
        note = f"   {reasons.most_common(1)[0][0]}" if reasons else ""
        print(f"    {state:<22} {len(sel):>4}  (of which ever eligible "
              f"{elig}){note}")

    print("\n  OUTCOMES, split the four ways a snapshot cannot")
    print(line("never eligible",
               scored(lives, lambda x: not x.ever_eligible)))
    print(line("became eligible later",
               scored(lives, lambda x: x.eligibility == lc.BECAME_ELIGIBLE)))
    print(line("eligible, not executed",
               scored(lives, lambda x: x.ever_eligible and not x.traded)))
    print(line("traded", scored(lives, lambda x: x.traded)))
    print(line("  [all ever eligible]",
               scored(lives, lambda x: x.ever_eligible)))

    print("\n  THE SNAPSHOT THAT CAUSED THE ERROR, kept for comparison")
    mis = [x for x in lives if x.qualified_at_alert is False and x.traded]
    print(f"    qualified_at_alert=False but TRADED: {len(mis)}")
    if mis:
        s = scored(mis, lambda x: True)
        if s:
            print(line("      those markets", s))
    unk = [x for x in lives if x.qualified_at_alert is None]
    if unk:
        print(f"    no alert snapshot recorded: {len(unk)}")

    pend = [x for x in lives if x.outcome == lc.PENDING]
    norec = [x for x in lives if x.outcome == lc.NO_RECORD]
    print(f"\n  outcome pending {len(pend)}   no prediction row {len(norec)}"
          f"   (distinguished, never merged)")


if __name__ == "__main__":
    main()
