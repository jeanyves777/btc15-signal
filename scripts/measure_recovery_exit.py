"""What the early stand-down would have done to the realised record.

The rule is the operator's and is deployed either way. This exists so the
decision has evidence beside it rather than instead of it.

WHAT CAN AND CANNOT BE MEASURED HERE. Standing down changes SIZE, and size
changes fills, so the true counterfactual P&L is not recoverable from a
ledger that only records what actually happened. What IS recoverable, and is
the thing the rule is actually about:

  * how often recovery would have stood down, and where
  * how many trades would have run at base instead of upsized
  * what those trades DID - because the rule's whole claim is that a loss
    arriving late in a recovery, at double size, costs more than the upsize
    ever won

A trade that lost while upsized is the case the rule is designed to avoid. A
trade that won while upsized is what it costs. Both are counted.

    python scripts/measure_recovery_exit.py
"""

import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from btc15_signal import recovery_exit  # noqa: E402
from btc15_signal.config import Settings  # noqa: E402

DEFICIT_CLEARED = 0.005


def replay(events, enabled: bool):
    """Walk the realised record, tracking the epoch exactly as the store does."""
    deficit = peak = 0.0
    wins = 0
    stood_down = False
    upsized_while_armed = []      # (ticker, amount) folded while recovery armed
    stand_downs = []
    for ticker, amount in events:
        armed = deficit > 0 and not stood_down
        if armed:
            upsized_while_armed.append((ticker, amount))
        if deficit > 0 and amount > 0:
            wins += 1
        deficit = max(0.0, deficit - amount)
        peak = max(peak, deficit)
        if deficit < DEFICIT_CLEARED:
            deficit = peak = 0.0
            wins, stood_down = 0, False
            continue
        if enabled and deficit > 0 and not stood_down:
            decision = recovery_exit.decide(peak, deficit, wins)
            if decision.stand_down:
                stood_down = True
                stand_downs.append((ticker, deficit, peak, wins))
    return upsized_while_armed, stand_downs


def main() -> None:
    settings = Settings()
    db = sqlite3.connect(f"file:{settings.database_path}?mode=ro", uri=True)
    events = [
        (row[0], float(row[1]))
        for row in db.execute(
            "SELECT ticker, amount FROM realised_events "
            "WHERE amount IS NOT NULL ORDER BY realised_ms, event_id"
        )
    ]
    if not events:
        print("no realised events")
        return
    print(f"{len(events)} realised events, "
          f"{sum(1 for _, a in events if a < 0)} of them losses\n")

    without, _ = replay(events, enabled=False)
    with_exit, stand_downs = replay(events, enabled=True)

    avoided = [e for e in without if e not in with_exit]
    print(f"{'':<34}{'trades armed':>14}{'their net':>12}")
    for label, group in (("recovery as it was", without),
                         ("with the early stand-down", with_exit)):
        total = sum(a for _, a in group)
        print(f"  {label:<32}{len(group):>14}{total:>+12.4f}")

    print(f"\nstand-downs that would have fired: {len(stand_downs)}")
    for ticker, deficit, peak, wins in stand_downs[:10]:
        fraction = recovery_exit.recovered_fraction(peak, deficit)
        print(f"   {ticker:<32} {fraction:.0%} of {peak:.2f} back "
              f"after {wins} win(s), {deficit:.2f} left")

    if avoided:
        losses = [a for _, a in avoided if a < 0]
        gains = [a for _, a in avoided if a > 0]
        print(f"\ntrades that would NOT have been upsized: {len(avoided)}")
        print(f"   of which losses {len(losses):>3}  totalling {sum(losses):+.4f}")
        print(f"   of which wins   {len(gains):>3}  totalling {sum(gains):+.4f}")
        print(f"   net on those trades          {sum(losses) + sum(gains):+.4f}")
        print("\n   A NEGATIVE net here is the case FOR the rule: those are")
        print("   the trades the upsize was riding, and the extra contract")
        print("   would have doubled that figure rather than the winning one.")
        print("   Sizes are not modelled - see the module docstring.")
    else:
        print("\nno trade would have changed size on this record.")


if __name__ == "__main__":
    main()
