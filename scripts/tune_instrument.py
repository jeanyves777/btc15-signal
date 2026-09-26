"""Derive an instrument's entry parameters from its OWN data. Nothing inherited.

SUPERSEDED, 2026-09-25. Use scripts/fit_instrument_config.py instead.
This tool reports one gate at a time, and choosing a threshold from its own
best bucket is exactly how silver and SOL came to hold seven individually
defensible numbers that, intersected, admitted 0.05% and 2.16% of their own
corpora and nothing at all live. The tables below are still useful for
SEEING a gradient; they must not be used to SET a threshold. FINDINGS 75.


WHY THIS EXISTS AS A TOOL. Gold's parameters were found by an ad-hoc sequence
of one-off queries, and the result was right but the process left five gates
set to values no input could fail - `accel >= -1e9`, `held >= 0s`,
`rejections >= 0`, `momentum >= 0.0`, `retrace <= 1.0`. An alert then read
"Entry checks 8/8" when only three checks could fail. Adding an instrument by
repeating that sequence by hand would reproduce the same gap.

So this runs the whole derivation in one place and, crucially, REPORTS THE
GATES IT CANNOT SET rather than leaving them at a permissive default that
looks deliberate.

THE ORDER MATTERS, and each step uses only this instrument's numbers:

  1. scale        how this reference behaves - volatility, distance, and how
                  much short-horizon noise survives to settlement. BTC's
                  `normalized_distance` means something different on an
                  instrument whose feed jitters differently, which is why
                  gold's read 0.83 where BTC's read 6.92 for equivalent setups.
  2. price band   scanned, not assumed. Gold's edge sits at 0.65-0.75, entirely
                  below where BTC's 0.70-0.93 begins.
  3. distance     in BOTH units - normalised and absolute bps - because which
                  one carries the edge is itself an instrument fact.
  4. timing       when in the window the edge exists.
  5. gates        every remaining feature, bucketed, so direction is read from
                  the data instead of copied. On gold `accel` and `momentum`
                  wanted CAPS where BTC wants floors.

Scored on the calibration residual - win rate minus the quoted price - day
clustered, because points inside a day share a price path. A threshold is
proposed only where buckets are ordered AND the interval clears zero.

No fees, per the operator's standing instruction.

    python scripts/tune_instrument.py --brti data/brti_history_sol.db \\
        --market data/market_data_kxsol15m_new.db --label SOL
"""

import argparse
import math
import random
import sqlite3
import statistics
from collections import defaultdict
from datetime import datetime, timezone


def load(brti: str, market: str):
    m = sqlite3.connect(f"file:{market}?mode=ro", uri=True)
    book = {}
    for ticker, ts, bid, ask in m.execute(
            "SELECT ticker, end_period_ts, yes_bid_close, yes_ask_close "
            "FROM contract_candles"):
        if bid is None or ask is None:
            continue
        book[(ticker, ts * 1000 if ts < 1e11 else ts)] = (bid, ask)

    b = sqlite3.connect(f"file:{brti}?mode=ro", uri=True)
    b.row_factory = sqlite3.Row
    cols = {r[1] for r in b.execute("PRAGMA table_info(brti_decision_points)")}
    optional = [c for c in ("brti_accel", "brti_held_s", "brti_rejections",
                            "brti_retrace", "brti_choppiness") if c in cols]
    base = ["ticker", "remaining_s", "close_ms", "result", "brti_side",
            "signed_distance_bps", "brti_normalized_distance",
            "brti_momentum_bps", "brti_volatility_bps", "brti_value",
            "expiration_value"]
    rows = []
    for r in b.execute(f"SELECT {', '.join(base + optional)} "
                       f"FROM brti_decision_points "
                       f"WHERE result IN ('yes','no') "
                       f"  AND signed_distance_bps IS NOT NULL"):
        ms = r["close_ms"] - r["remaining_s"] * 1000
        quote = book.get((r["ticker"], ms // 60000 * 60000))
        if quote is None:
            continue
        yes_bid, yes_ask = quote
        yes_ask = yes_ask / 100 if yes_ask > 1 else yes_ask
        no_ask = round(1 - (yes_bid / 100 if yes_bid > 1 else yes_bid), 4)
        ask = yes_ask if r["brti_side"] == "UP" else no_ask
        row = {
            "won": (r["result"] == "yes") == (r["brti_side"] == "UP"),
            "ask": ask,
            "gap": abs(r["signed_distance_bps"]),
            "nd": r["brti_normalized_distance"],
            "mom": abs(r["brti_momentum_bps"] or 0.0),
            "vol": r["brti_volatility_bps"],
            "rem": r["remaining_s"],
            "ref": r["brti_value"],
            "settle": r["expiration_value"],
            "day": datetime.fromtimestamp(
                r["close_ms"] / 1000, timezone.utc).date(),
        }
        for c in optional:
            row[c.replace("brti_", "")] = r[c]
        rows.append(row)
    return rows, [c.replace("brti_", "") for c in optional]


def score(sel, draws=6000, seed=11, floor=60):
    if len(sel) < floor:
        return None
    byday = defaultdict(list)
    for r in sel:
        byday[r["day"]].append((1.0 if r["won"] else 0.0) - r["ask"])
    days = list(byday)
    rng = random.Random(seed)
    out = []
    for _ in range(draws):
        pick = [rng.choice(days) for _ in days]
        vals = [x for d in pick for x in byday[d]]
        out.append(sum(vals) / len(vals))
    out.sort()
    flat = [x for v in byday.values() for x in v]
    return (statistics.mean(flat), out[150], out[5849], len(sel),
            sum(1 for r in sel if r["won"]) / len(sel))


def table(rows, key, edges, title, gate=None):
    print(f"\n  {title}")
    print(f"    {'bucket':<22}{'n':>6}{'win%':>8}{'residual':>10}{'95% CI':>22}")
    holds = []
    for lo, hi, name in edges:
        sel = [r for r in rows if r.get(key) is not None
               and lo <= r[key] < hi and (gate is None or gate(r))]
        s = score(sel)
        if s is None:
            print(f"    {name:<22}n={len(sel):<5} too few")
            continue
        mu, l, h, n, win = s
        if l > 0:
            holds.append((name, mu, lo, hi))
        print(f"    {name:<22}{n:>6}{win:>8.1%}{mu:>+10.4f}"
              f"  [{l:+.4f}, {h:+.4f}]{'  HOLDS' if l > 0 else ''}")
    return holds


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--brti", required=True)
    p.add_argument("--market", required=True)
    p.add_argument("--label", required=True)
    p.add_argument("--ref-brti", default="data/brti_history.db",
                   help="BTC, for scale comparison only - never for thresholds")
    args = p.parse_args()

    rows, optional = load(args.brti, args.market)
    if not rows:
        raise SystemExit(f"{args.label}: no priced decision points")
    days = len({r["day"] for r in rows})
    print(f"=== {args.label}: {len(rows)} priced decision points over "
          f"{days} days ===")

    # 1. SCALE
    print("\n  1. SCALE - how this reference behaves, against BTC for contrast")
    mv = [abs(r["settle"] - r["ref"]) / r["ref"] * 1e4
          for r in rows if r["settle"] and r["ref"]]
    print(f"    volatility_bps median      {statistics.median([r['vol'] for r in rows if r['vol']]):.2f}")
    print(f"    |distance| bps median      {statistics.median([r['gap'] for r in rows]):.2f}")
    print(f"    normalised distance median {statistics.median([r['nd'] for r in rows if r['nd'] is not None]):.2f}   (BTC's is ~6.9)")
    if mv:
        print(f"    |actual 15-min move| bps   {statistics.median(mv):.2f}")
        print(f"    distance / actual move     {statistics.median([r['gap'] for r in rows]) / statistics.median(mv):.2f}x   (BTC's is ~0.78x)")

    # 2. PRICE BAND - scanned
    holds = table(rows, "ask",
                  [(0.50, 0.60, "0.50-0.60"), (0.60, 0.70, "0.60-0.70"),
                   (0.70, 0.80, "0.70-0.80"), (0.80, 0.88, "0.80-0.88"),
                   (0.88, 0.96, "0.88-0.96")],
                  "2. PRICE BAND - scanned, not assumed")
    band = None
    if holds:
        band = (min(h[2] for h in holds), max(h[3] for h in holds))
        print(f"    -> buckets clearing zero span {band[0]:.2f}-{band[1]:.2f}")
    else:
        print("    -> NO price bucket clears zero. There is no band to ship.")
        return

    inband = lambda r: band[0] <= r["ask"] <= band[1]  # noqa: E731

    # 3. DISTANCE, in both units
    table(rows, "gap",
          [(0, 2, "< 2bp"), (2, 5, "2-5bp"), (5, 10, "5-10bp"),
           (10, 20, "10-20bp"), (20, 1e9, "20bp+")],
          "3a. DISTANCE, absolute bps", gate=inband)
    table(rows, "nd",
          [(0, 1, "< 1x vol"), (1, 3, "1-3x"), (3, 6, "3-6x"),
           (6, 10, "6-10x"), (10, 1e9, "10x+")],
          "3b. DISTANCE, normalised (x volatility)", gate=inband)

    # 4. TIMING
    table(rows, "rem",
          [(120, 250, "2-4 min left"), (250, 400, "4-6.7 min"),
           (400, 550, "6.7-9 min"), (550, 700, "9-11.6 min")],
          "4. TIME REMAINING", gate=inband)

    # 5. THE REMAINING GATES - direction read from the data
    print("\n  5. GATES - direction read from this instrument, not copied")
    table(rows, "vol", [(0, 4, "< 4bp"), (4, 8, "4-8bp"), (8, 12, "8-12bp"),
                        (12, 1e9, ">= 12bp")],
          "reference volatility", gate=inband)
    table(rows, "mom", [(0, 2, "< 2bp"), (2, 5, "2-5bp"), (5, 10, "5-10bp"),
                        (10, 1e9, ">= 10bp")],
          "momentum |bps|", gate=inband)
    for key, edges, title in (
            ("accel", [(-1e9, -10, "< -10"), (-10, 0, "-10 to 0"),
                       (0, 10, "0 to +10"), (10, 1e9, ">= +10")],
             "acceleration"),
            ("held_s", [(0, 60, "< 60s"), (60, 120, "60-120s"),
                        (120, 240, "120-240s"), (240, 1e9, ">= 240s")],
             "level held"),
            ("rejections", [(0, 2, "0-1"), (2, 5, "2-4"), (5, 10, "5-9"),
                            (10, 1e9, "10+")], "level tested"),
            ("retrace", [(0, .25, "< 25%"), (.25, .5, "25-50%"),
                         (.5, .75, "50-75%"), (.75, 1.01, "75-100%")],
             "giveback"),
            ("choppiness", [(0, .5, "< 50%"), (.5, .75, "50-75%"),
                            (.75, .9, "75-90%"), (.9, 1.01, ">= 90%")],
             "choppiness")):
        if key in optional:
            table(rows, key, edges, title, gate=inband)
        else:
            print(f"\n  {title}: NOT IN THIS CORPUS - cannot be set from "
                  f"measurement.\n    Shipping it at a permissive default "
                  f"would look deliberate and be inert.")


if __name__ == "__main__":
    main()
