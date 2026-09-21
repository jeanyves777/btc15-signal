"""Measure the PRE-REGISTERED hypotheses forward, on live signals only.

Section 14 found one positive sub-band result - 0.65-0.69 as its own band,
+0.0709 [+0.0209, +0.1203] over 284 historical markets - and deliberately did
not adopt it, because it is a grid-found rule and section 7 says what those are
worth. It was recorded "as a pre-registered hypothesis to measure FORWARD".
Nothing measured it forward. This does.

The historical study ends 2026-09-20 23:20 (last kline) and the live signal
record begins 2026-09-20 23:09, an eleven-minute overlap that is immaterial at
this resolution. Everything below is therefore genuinely out of sample.

THE TRAP THIS REPORT EXISTS TO AVOID. On a day when the market misprices
everything in one direction, every band beats its own price at once, and the
band you happen to be interested in looks vindicated. The operator's live feed
on 2026-09-21 showed a long run of declined sub-band signals settling as
winners, which reads as "the band is too tight". It is only evidence about the
band if the sub-band beat its prices by MORE than the rest of the tape did.
So the whole-tape excess is computed first, as a control, and every hypothesis
is scored against it rather than against zero.

The statistic throughout is EXCESS OVER THE MARKET'S OWN PRICE:

    excess = wins - sum(contract_price)

A contract at 0.70 that wins 70% of the time has zero excess and, after fee, is
a losing trade. This is the right measure precisely because it does not reward
a band for containing high-probability bets - the price already charged for
those.

    python scripts/forward_test.py
"""

import math
import random
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from btc15_signal.store import wilson_lower  # noqa: E402
from btc15_signal.validation import kalshi_fee_charged, trade_pnl  # noqa: E402

MIN_MINORITY = 3  # losses needed before a bootstrap interval means anything
TARGET = 0.0220  # the 15-minute edge these hypotheses are trying to beat

# Registered BEFORE seeing any of the live data below. Each carries the
# historical estimate it is being tested against, so a forward result that
# merely regresses to the mean is recognisable as such.
HYPOTHESES = [
    {
        "name": "0.65-0.69 as its own band",
        "lo": 0.65, "hi": 0.70,
        "prior": "+0.0709 [+0.0209, +0.1203], n=284, BH p=0.079 over 35 buckets",
        "verdict": "grid-found; NOT adopted; collapses without the settle rule",
    },
    {
        "name": "0.70-0.84 (what lowering the floor would add)",
        "lo": 0.70, "hi": 0.85,
        "prior": "min_ask 0.70 measured -0.0100 [-0.0240, +0.0035] vs deployed",
        "verdict": "predicted NO improvement; do not lower the bound",
    },
    {
        "name": "0.85-0.93 DEPLOYED band",
        "lo": 0.85, "hi": 0.931,
        "prior": "+0.0219 [+0.0090, +0.0347] with the settle rule",
        "verdict": "the incumbent; everything else is measured against this",
    },
]


def stats(rows: list[sqlite3.Row]) -> dict | None:
    """Wins, the price-implied expectation, and the gap between them."""
    if not rows:
        return None
    n = len(rows)
    wins = sum(r["won"] for r in rows)
    expected = sum(r["p"] for r in rows)
    variance = sum(r["p"] * (1 - r["p"]) for r in rows)
    excess = wins - expected
    price = sum(r["p"] for r in rows) / n
    return {
        "n": n, "wins": wins, "losses": n - wins, "price": price,
        "expected": expected, "excess": excess, "excess_per": excess / n,
        "z": excess / math.sqrt(variance) if variance > 0 else 0.0,
        "net_per": sum(trade_pnl(r["p"], bool(r["won"]), contracts=1)
                       for r in rows) / n,
        "net_total": sum(trade_pnl(r["p"], bool(r["won"]), contracts=1)
                         for r in rows),
        "breakeven": price + kalshi_fee_charged(price, 1),
        "wilson": wilson_lower(wins, n),
    }


def excess_gap(inside: list, outside: list, draws: int = 4000, seed: int = 11):
    """Bootstrap the difference in per-signal excess: band minus the rest.

    This is the number that separates "this band is special" from "today was
    special". Refused when either side is too one-sided to resample honestly -
    the section 18 lesson: a bootstrap cannot draw a loss it was never given.
    """
    if len(inside) < 2 or len(outside) < 2:
        return None
    if min(sum(1 for r in inside if not r["won"]),
           sum(1 for r in outside if not r["won"])) < MIN_MINORITY:
        return None
    rng = random.Random(seed)
    per = lambda s: sum(r["won"] - r["p"] for r in s) / len(s)  # noqa: E731
    diffs = sorted(
        per([rng.choice(inside) for _ in inside])
        - per([rng.choice(outside) for _ in outside])
        for _ in range(draws)
    )
    return diffs[int(0.025 * draws)], diffs[int(0.975 * draws)]


def main() -> None:
    db = sqlite3.connect("file:btc15.db?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    rows = db.execute(
        "SELECT contract_price p, won, window_open FROM predictions "
        "WHERE won IS NOT NULL AND contract_price IS NOT NULL"
    ).fetchall()

    print("=" * 78)
    print("FORWARD TEST - pre-registered hypotheses, live signals only")
    print("=" * 78)

    tape = stats(rows)
    if tape is None:
        print("\nNo settled live signals with a price. Nothing to measure.")
        return

    # --- THE CONTROL ------------------------------------------------------
    print("\n0. THE CONTROL - did the whole tape beat its own prices?")
    print("   Read this before any hypothesis below.")
    print(f"  settled live signals                     {tape['n']}")
    print(f"  wins                                     {tape['wins']}")
    print(f"  wins the PRICES implied                  {tape['expected']:.1f}")
    print(f"  excess                                   {tape['excess']:+.1f} "
          f"({tape['excess_per']:+.3f}/signal)")
    print(f"  z against fair pricing                   {tape['z']:+.2f}")
    if tape["z"] > 1.96:
        print("  => The ENTIRE tape beat its prices, not one band. On a day like")
        print("     this every band looks good, so a band is only interesting if")
        print("     it beat its prices by more than the tape did. That is the")
        print("     'vs rest of tape' line in each hypothesis below, and it is")
        print("     the only line that can distinguish the band from the day.")
    else:
        print("  => The tape as a whole is close to fairly priced, so a band")
        print("     that beats its prices is not merely riding a good day.")

    # --- THE HYPOTHESES ---------------------------------------------------
    print("\n1. THE HYPOTHESES")
    for hypothesis in HYPOTHESES:
        lo, hi = hypothesis["lo"], hypothesis["hi"]
        inside = [r for r in rows if lo <= r["p"] < hi]
        outside = [r for r in rows if not lo <= r["p"] < hi]
        print(f"\n  {hypothesis['name']}")
        print(f"    registered   {hypothesis['prior']}")
        print(f"    and          {hypothesis['verdict']}")
        block = stats(inside)
        if block is None or block["n"] < 2:
            got = 0 if block is None else block["n"]
            print(f"    FORWARD      {got} signal(s) - nothing measurable yet")
            continue
        print(f"    FORWARD      {block['wins']}/{block['n']} won at an average "
              f"price of {block['price']:.2f}")
        print(f"      excess over price            {block['excess']:+.1f} "
              f"({block['excess_per']:+.3f}/signal), z={block['z']:+.2f}")
        print(f"      net after fee                {block['net_per']:+.4f}/ct, "
              f"total {block['net_total']:+.4f}")
        print(f"      win rate 95% lower bound     {block['wilson']:.1%} "
              f"vs break-even {block['breakeven']:.1%}"
              + ("  CLEARS" if block["wilson"] > block["breakeven"]
                 else "  does NOT clear"))
        rest = stats(outside)
        gap = excess_gap(inside, outside)
        if rest:
            delta = block["excess_per"] - rest["excess_per"]
            interval = (f"[{gap[0]:+.3f}, {gap[1]:+.3f}]" if gap
                        else f"NO INTERVAL - {block['losses']} losses in band, "
                             f"{rest['losses']} outside")
            print(f"      vs rest of tape              {delta:+.3f}/signal "
                  f"{interval}")
            if gap and gap[0] > 0:
                print("      => beat the rest of the tape; the band, not the day")
            elif gap:
                print("      => indistinguishable from the rest of the tape")
            else:
                print("      => cannot separate the band from the day on this n")
        # Sample size from the THEORETICAL variance, never the observed spread.
        sd = math.sqrt(block["price"] * (1 - block["price"]))
        need = int(((1.96 + 0.84) * sd / TARGET) ** 2) + 1
        print(f"      to resolve {TARGET:+.4f} at 80% power   ~{need} signals "
              f"({need / max(block['n'], 1):.0f}x what is in hand)")

    # --- REGIME -----------------------------------------------------------
    # The operator's 2026-09-21 observation: the New York morning won on
    # everything and the losses started around 1pm local (17:00 UTC).
    # `scripts/measure_hour.py` tested that on 71 days as ONE pre-registered
    # comparison and found +0.0010/ct [-0.0272, +0.0305], p=0.542 - no
    # difference. Section 4 had already found every session interval
    # overlapping every other. It is registered HERE so the live record keeps
    # measuring it instead of the question being re-argued from single days.
    print("\n2. REGIME - hour of day, measured forward")
    print("   registered   17-20 UTC vs the rest: +0.0010/ct [-0.0272, +0.0305],")
    print("                p=0.542 over 71 days. No difference found.")
    print("   and          section 4: every session interval overlaps every other")
    afternoon = [
        r for r in rows
        if datetime.fromtimestamp(r["window_open"] / 1000, UTC).hour in (17, 18, 19, 20)
    ]
    other = [
        r for r in rows
        if datetime.fromtimestamp(r["window_open"] / 1000, UTC).hour not in (17, 18, 19, 20)
    ]
    for label, sel in (("17-20 UTC (1pm-5pm local)", afternoon),
                       ("every other hour", other)):
        block = stats(sel)
        if block is None or block["n"] < 2:
            print(f"    {label:<28} {0 if block is None else block['n']} "
                  f"signal(s) - nothing measurable yet")
            continue
        print(f"    {label:<28} {block['wins']}/{block['n']} won, "
              f"excess {block['excess']:+.1f} ({block['excess_per']:+.3f}/signal), "
              f"z={block['z']:+.2f}")
    if afternoon and other:
        gap = excess_gap(afternoon, other)
        a, b = stats(afternoon), stats(other)
        delta = a["excess_per"] - b["excess_per"]
        interval = (f"[{gap[0]:+.3f}, {gap[1]:+.3f}]" if gap
                    else f"NO INTERVAL - {a['losses']} and {b['losses']} losses")
        print(f"    afternoon minus the rest     {delta:+.3f}/signal {interval}")
        print("    A single day cannot answer this. The point of registering it")
        print("    is that the answer accumulates instead of being re-argued.")

    print("\n3. WHAT THIS DOES NOT SAY")
    print("  Nothing here licenses a band change. The live record is hours old,")
    print("  every hypothesis is short of the sample it needs by a factor of")
    print("  tens, and section 14 measured this same region over 68 days and")
    print("  found the bucket below the floor to be the flattest line in the")
    print("  table. A forward test is a commitment to keep looking, not a")
    print("  result. Re-run it daily; it only becomes evidence with time.")
    print("=" * 78)


if __name__ == "__main__":
    main()
