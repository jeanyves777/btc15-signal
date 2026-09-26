"""Derive a gate set JOINTLY, with a volume floor. Never marginally.

SUPERSEDED, 2026-09-25. Use scripts/fit_instrument_config.py instead.
This tool reports one gate at a time, and choosing a threshold from its own
best bucket is exactly how silver and SOL came to hold seven individually
defensible numbers that, intersected, admitted 0.05% and 2.16% of their own
corpora and nothing at all live. The tables below are still useful for
SEEING a gradient; they must not be used to SET a threshold. FINDINGS 75.


THE MISTAKE THIS EXISTS TO PREVENT, made on 2026-09-25. Silver and SOL each got
seven thresholds, and every one was chosen from its own best bucket in
isolation - price 0.50-0.60, distance <=2bp, held >=60s, rejections >=10,
momentum <=10bp, accel -10..+10. Each was defensible alone. Stacked, they
admitted NOTHING: 423 and 422 live evaluations, zero qualified, every market
blocked. Gold, which carries two active gates, qualified 18.7%.

Seven marginal best-buckets intersected is an empty set, and the check that
would have caught it - does the COMBINATION admit a usable fraction - was never
run. So this evaluates candidate combinations rather than single gates, and
refuses to report one that does not clear a volume floor.

VOLUME IS A CONSTRAINT, NOT A PREFERENCE. A gate that admits 2% of setups
cannot be learned from: the arms never reach `min_evidence`, so the layer stays
inert and the configuration can never be corrected by evidence. A slightly
weaker edge that produces data beats a stronger one that produces none.

Scored on the calibration residual, day-clustered. No fees, per the operator's
standing instruction.
"""

import argparse
import itertools
import json
import random
import sqlite3
import statistics
from collections import defaultdict
from datetime import datetime, timezone


def load(brti, market):
    m = sqlite3.connect(f"file:{market}?mode=ro", uri=True)
    book = {}
    for t, ts, bid, ask in m.execute(
            "SELECT ticker, end_period_ts, yes_bid_close, yes_ask_close "
            "FROM contract_candles"):
        if bid is None or ask is None:
            continue
        book[(t, ts * 1000 if ts < 1e11 else ts)] = (bid, ask)
    b = sqlite3.connect(f"file:{brti}?mode=ro", uri=True)
    b.row_factory = sqlite3.Row
    rows = []
    for r in b.execute(
            "SELECT ticker, remaining_s, close_ms, result, brti_side, "
            "       signed_distance_bps sd, brti_momentum_bps mom, "
            "       brti_accel, brti_held_s, brti_rejections "
            "  FROM brti_decision_points "
            " WHERE result IN ('yes','no') AND signed_distance_bps IS NOT NULL"):
        ms = r["close_ms"] - r["remaining_s"] * 1000
        q = book.get((r["ticker"], ms // 60000 * 60000))
        if q is None:
            continue
        yes_bid, yes_ask = q
        yes_ask = yes_ask / 100 if yes_ask > 1 else yes_ask
        no_ask = round(1 - (yes_bid / 100 if yes_bid > 1 else yes_bid), 4)
        ask = yes_ask if r["brti_side"] == "UP" else no_ask
        rows.append({
            "won": (r["result"] == "yes") == (r["brti_side"] == "UP"),
            "ask": ask, "gap": abs(r["sd"]), "mom": abs(r["mom"] or 0.0),
            "accel": r["brti_accel"], "held": r["brti_held_s"],
            "rej": r["brti_rejections"], "rem": r["remaining_s"],
            "day": datetime.fromtimestamp(
                r["close_ms"] / 1000, timezone.utc).date(),
        })
    return rows


def score(sel, draws=3000, seed=11):
    if len(sel) < 60:
        return None
    byday = defaultdict(list)
    for r in sel:
        byday[r["day"]].append((1.0 if r["won"] else 0.0) - r["ask"])
    days = list(byday)
    if len(days) < 6:
        return None
    rng = random.Random(seed)
    out = []
    for _ in range(draws):
        pick = [rng.choice(days) for _ in days]
        vals = [x for d in pick for x in byday[d]]
        out.append(sum(vals) / len(vals))
    out.sort()
    flat = [x for v in byday.values() for x in v]
    return (statistics.mean(flat), out[75], out[2924], len(sel),
            sum(1 for r in sel if r["won"]) / len(sel))


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--brti", required=True)
    p.add_argument("--market", required=True)
    p.add_argument("--label", required=True)
    p.add_argument("--min-share", type=float, default=0.08,
                   help="a gate set admitting less than this cannot be learned")
    args = p.parse_args()

    rows = load(args.brti, args.market)
    if not rows:
        raise SystemExit(f"{args.label}: no priced rows")
    days = len({r["day"] for r in rows})
    print(f"=== {args.label}: {len(rows)} points over {days} days ===")
    base = score(rows)
    if base:
        print(f"  ungated: n={base[3]} win {base[4]:.1%} "
              f"residual {base[0]:+.4f}")

    # Candidate values per gate, INCLUDING "off". Off is a real choice: gold
    # runs two active gates and qualifies 18.7% where seven active gates
    # qualify nothing.
    prices = [(0.60, 0.80), (0.65, 0.85), (0.70, 0.90), (0.60, 0.90)]
    gaps = [(0.0, 99.0), (0.0, 6.0), (1.0, 8.0), (2.0, 99.0)]
    moms = [99.0, 10.0]
    rejs = [0, 2]

    print(f"\n  joint gate sets admitting >= {args.min_share:.0%} of points")
    print(f"    {'price':<14}{'gap bp':<12}{'mom<=':<8}{'rej>=':<7}"
          f"{'n':>6}{'share':>8}{'win%':>7}{'residual':>10}{'95% CI':>20}")
    found = []
    for (plo, phi), (glo, ghi), mcap, rmin in itertools.product(
            prices, gaps, moms, rejs):
        sel = [r for r in rows
               if plo <= r["ask"] <= phi and glo <= r["gap"] <= ghi
               and r["mom"] <= mcap
               and (r["rej"] is None or r["rej"] >= rmin)]
        share = len(sel) / len(rows)
        if share < args.min_share:
            continue
        s = score(sel)
        if not s:
            continue
        mu, lo, hi, n, win = s
        found.append((mu, lo, plo, phi, glo, ghi, mcap, rmin, n, share, win))
        print(f"    {f'{plo}-{phi}':<14}{f'{glo}-{ghi}':<12}{mcap:<8.0f}"
              f"{rmin:<7}{n:>6}{share:>8.1%}{win:>7.1%}{mu:>+10.4f}"
              f"  [{lo:+.4f},{hi:+.4f}]{'  HOLDS' if lo > 0 else ''}")

    holds = [f for f in found if f[1] > 0]
    print(f"\n  {len(found)} sets clear the volume floor, "
          f"{len(holds)} also clear zero")
    if holds:
        best = max(holds, key=lambda f: f[0])
        print(f"  strongest that also has volume: price {best[2]}-{best[3]}, "
              f"gap {best[4]}-{best[5]}bp, mom<={best[6]:.0f}, "
              f"rej>={best[7]}  -> n={best[8]} ({best[9]:.1%}), "
              f"residual {best[0]:+.4f}")
    else:
        print("  NOTHING clears both. Report that rather than picking the "
              "best cell -\n  a set chosen without an interval is how seven "
              "marginal winners became\n  a configuration that admits nothing.")


if __name__ == "__main__":
    main()
