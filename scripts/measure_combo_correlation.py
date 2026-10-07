"""Are 15-minute crypto outcomes independent? The whole combo question.

A Kalshi multi-market combo pays only if EVERY leg wins, and it is priced - as
the app's own cash-out figure shows - at the PRODUCT of the legs' current
prices. On 2026-09-26 a three-leg combo quoted 83% x 72% x 66% = 39.4% and its
cash-out was $1.70 of a $4.44 maximum, i.e. 38.3%. Product, less a small spread.

A product is the correct joint probability ONLY IF THE LEGS ARE INDEPENDENT.
These legs are not obviously independent: they are four or five crypto assets
over the SAME fifteen minutes, and this system has already watched BTC, ETH, SOL
and XRP all call UP in one window and all lose together.

Correlation cuts both ways and the direction decides everything:

  SAME-DIRECTION legs (all above, or all below) under POSITIVE correlation win
  together more often than independence implies. The product UNDERPRICES them,
  so the buyer is getting an edge.

  MIXED-DIRECTION legs (some above, some below) under positive correlation win
  together LESS often than the product implies. The product OVERPRICES them, so
  the buyer is paying for a coincidence.

The screenshot combo was MIXED: BNB above, NEAR above, SOL below.

WHAT `result` MEANS HERE. Each 15-minute market settles YES when the reference
finishes above `floor_strike`, and the strike is the window's own opening price,
so YES is simply "this asset rose over these fifteen minutes". That makes the
settled record a clean directional series, which is what this needs.

No fees, per the operator's standing instruction - noted because a real combo
pays fees on every leg and that is a separate, additive cost.

    python scripts/measure_combo_correlation.py
"""

import itertools
import sqlite3
import statistics
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

ASSETS = {
    "BTC": "market_data.db",
    "ETH": "market_data_kxeth15m.db",
    "SOL": "market_data_kxsol15m.db",
    "XRP": "market_data_kxxrp15m.db",
    "NEAR": "market_data_kxnear15m.db",
}


def outcomes():
    """{window_close_ms: {asset: True if it rose}} over settled markets."""
    by_window = defaultdict(dict)
    for asset, db in ASSETS.items():
        path = ROOT / "data" / db
        if not path.exists():
            print(f"  (no data for {asset} at {db})")
            continue
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        n = 0
        for close_ms, result in con.execute(
                "SELECT close_ms, result FROM markets "
                " WHERE result IN ('yes','no') AND floor_strike IS NOT NULL"):
            by_window[int(close_ms)][asset] = (result == "yes")
            n += 1
        con.close()
        print(f"  {asset:<5} {n} settled markets")
    return by_window


def main():
    print("=" * 76)
    print("ARE 15-MINUTE CRYPTO OUTCOMES INDEPENDENT?")
    print("=" * 76)
    by_window = outcomes()
    if not by_window:
        raise SystemExit("no data")

    # Only windows where every asset we want is present can be compared.
    assets = [a for a in ASSETS if any(a in v for v in by_window.values())]
    full = {w: v for w, v in by_window.items() if len(v) == len(assets)}
    print(f"\n{len(by_window)} windows seen, {len(full)} with ALL "
          f"{len(assets)} assets settled - those are the comparable ones")
    if len(full) < 100:
        print("  too few aligned windows to conclude anything")
        return
    days = len({datetime.fromtimestamp(w / 1000, timezone.utc).date()
                for w in full})
    print(f"  spanning {days} days")

    # ---------------------------------------------------------- marginals
    print("\nMARGINAL: how often each asset ROSE over its own window")
    marg = {}
    for a in assets:
        p = sum(1 for v in full.values() if v[a]) / len(full)
        marg[a] = p
        print(f"  {a:<5} {p:.1%}")

    # ------------------------------------------------------------- pairs
    print("\nPAIRS: observed agreement vs what independence predicts")
    print(f"  {'pair':<12}{'both rose':>11}{'independent':>13}"
          f"{'lift':>9}{'phi':>8}")
    for a, b in itertools.combinations(assets, 2):
        both = sum(1 for v in full.values() if v[a] and v[b]) / len(full)
        indep = marg[a] * marg[b]
        # phi coefficient: correlation of two binary variables
        n11 = sum(1 for v in full.values() if v[a] and v[b])
        n10 = sum(1 for v in full.values() if v[a] and not v[b])
        n01 = sum(1 for v in full.values() if not v[a] and v[b])
        n00 = sum(1 for v in full.values() if not v[a] and not v[b])
        den = ((n11 + n10) * (n01 + n00) * (n11 + n01) * (n10 + n00)) ** 0.5
        phi = ((n11 * n00 - n10 * n01) / den) if den else 0.0
        print(f"  {a + '+' + b:<12}{both:>11.1%}{indep:>13.1%}"
              f"{both / indep if indep else 0:>8.2f}x{phi:>8.2f}")

    # ------------------------------------------- three-leg combos, priced
    print("\nTHREE-LEG COMBOS: what the product implies, and what happened")
    print("  A combo pays only if every leg wins. 'product' is what Kalshi")
    print("  charges for; 'actual' is how often it really happened.")
    print(f"\n  {'legs':<22}{'direction':<11}{'product':>9}{'actual':>9}"
          f"{'edge':>9}{'n':>6}")

    same_lifts, mixed_lifts = [], []
    for trio in itertools.combinations(assets, 3):
        for directions in itertools.product([True, False], repeat=3):
            # price each leg at its marginal, as the book would
            product = 1.0
            for a, up in zip(trio, directions):
                product *= marg[a] if up else (1 - marg[a])
            actual = sum(
                1 for v in full.values()
                if all(v[a] == up for a, up in zip(trio, directions))
            ) / len(full)
            if product <= 0:
                continue
            same = len(set(directions)) == 1
            lift = actual / product
            (same_lifts if same else mixed_lifts).append(lift)
            # only print the extremes, or the table is 80 rows
            if abs(lift - 1) > 0.25:
                label = "+".join(trio)
                d = "".join("U" if u else "D" for u in directions)
                edge = actual * (1 / product) - 1.0
                print(f"  {label:<22}{d:<11}{product:>9.1%}{actual:>9.1%}"
                      f"{edge:>+8.0%}{len(full):>6}")

    print("\nTHE ANSWER, averaged over every three-leg combination:")
    if same_lifts:
        print(f"  SAME direction  (UUU / DDD) : actual / product = "
              f"{statistics.mean(same_lifts):.2f}x   over {len(same_lifts)} combos")
    if mixed_lifts:
        print(f"  MIXED direction             : actual / product = "
              f"{statistics.mean(mixed_lifts):.2f}x   over {len(mixed_lifts)} combos")
    print("\n  Above 1.00x means the combo happens MORE often than the price")
    print("  assumes, so the product underprices it. Below 1.00x means you are")
    print("  paying for a coincidence that is rarer than it looks.")


if __name__ == "__main__":
    main()
