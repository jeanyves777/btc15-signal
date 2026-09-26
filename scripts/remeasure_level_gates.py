"""Re-measure the three level gates under brti-4's 45-minute lookback.

WHY THEY HAVE TO BE RE-MEASURED. `rejections`, `held_s` and `accel` are all
computed from `level_pts`, whose window moved from 900s to 2,700s on
2026-09-25. Their thresholds were fitted against the OLD definition, so they
are live and enforcing numbers that describe a quantity that no longer exists.

The rejection count is the clearest case. At 900s the lookback was the market's
own window and the strike IS that window's opening price, so every window began
with price on the strike, inside the threshold - a clean one-way move scored
exactly 1, its own departure. The deployed `>= 2` therefore refused the
cleanest setups, and over 19,305 brti-2 points the refused bucket led on every
measure: distance 8.33 against 4.58, held 241s against 212s, momentum 6.3
against 3.2, 75.6% wins against 66.9%.

At 2,700s a rejection means what the name says - price approached this level
and was turned back - so the ordering may well invert. That is the point of
measuring rather than assuming, in either direction.

WHAT THIS CANNOT DO. The brti-2 corpus cannot be recomputed: the backfill skips
markets it has already stored, and the raw per-second series is not retained at
that scale, so the 19,305 points cannot be re-derived under the new window.
This measures what `live_data` still serves, which is a few hundred markets -
enough to see a direction, not enough to set a threshold on its own. The
sample size is printed for that reason.

No fees, per the operator's standing instruction.
"""

import argparse
import random
import sqlite3
import statistics
from collections import defaultdict
from datetime import datetime, timezone


def load(brti: str, market: str, band=(0.70, 0.93)):
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
            "       brti_normalized_distance nd, brti_momentum_bps mom, "
            "       brti_accel, brti_held_s, brti_rejections "
            "  FROM brti_decision_points "
            " WHERE result IN ('yes','no') AND brti_rejections IS NOT NULL"):
        ms = r["close_ms"] - r["remaining_s"] * 1000
        q = book.get((r["ticker"], ms // 60000 * 60000))
        if q is None:
            continue
        yes_bid, yes_ask = q
        yes_ask = yes_ask / 100 if yes_ask > 1 else yes_ask
        no_ask = round(1 - (yes_bid / 100 if yes_bid > 1 else yes_bid), 4)
        ask = yes_ask if r["brti_side"] == "UP" else no_ask
        if not band[0] <= ask <= band[1]:
            continue
        mom = abs(r["mom"] or 0.0)
        floor = 10.0 if mom < 5.0 else 15.0
        if (r["nd"] or 0.0) < floor:
            continue
        rows.append({
            "won": (r["result"] == "yes") == (r["brti_side"] == "UP"),
            "ask": ask, "nd": r["nd"], "mom": mom,
            "accel": r["brti_accel"], "held": r["brti_held_s"],
            "rej": r["brti_rejections"],
            "day": datetime.fromtimestamp(
                r["close_ms"] / 1000, timezone.utc).date(),
        })
    return rows


def score(sel, draws=6000, seed=11, floor=50):
    if len(sel) < floor:
        return None
    byday = defaultdict(list)
    for r in sel:
        byday[r["day"]].append((1.0 if r["won"] else 0.0) - r["ask"])
    days = list(byday)
    if len(days) < 4:
        return None
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


def table(rows, key, edges, title, deployed):
    print(f"\n  {title}")
    print(f"    {'bucket':<20}{'n':>6}{'win%':>8}{'residual':>10}{'95% CI':>22}")
    for lo, hi, name in edges:
        sel = [r for r in rows
               if r.get(key) is not None and lo <= r[key] < hi]
        s = score(sel)
        mark = "  <- DEPLOYED refuses" if deployed(lo, hi) else ""
        if s is None:
            print(f"    {name:<20}n={len(sel):<5} too few{mark}")
            continue
        mu, l, h, n, win = s
        print(f"    {name:<20}{n:>6}{win:>8.1%}{mu:>+10.4f}"
              f"  [{l:+.4f}, {h:+.4f}]{'  HOLDS' if l > 0 else ''}{mark}")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--brti", default="data/brti_history_v4.db")
    p.add_argument("--market", default="data/market_data.db")
    p.add_argument("--old", default="data/brti_history.db")
    args = p.parse_args()

    new = load(args.brti, args.market)
    old = load(args.old, args.market)
    print(f"brti-4 (45-min lookback): {len(new)} points over "
          f"{len({r['day'] for r in new})} days")
    print(f"brti-2 (15-min lookback): {len(old)} points over "
          f"{len({r['day'] for r in old})} days   [for contrast only]")
    if not new:
        raise SystemExit("no brti-4 rows yet - has the backfill finished?")

    for label, rows in (("brti-2, 15-MIN (what the thresholds were fitted on)",
                         old),
                        ("brti-4, 45-MIN (what is running now)", new)):
        print(f"\n{'=' * 4} {label} {'=' * 4}")
        base = score(rows)
        if base:
            print(f"  baseline n={base[3]} win {base[4]:.1%} "
                  f"residual {base[0]:+.4f}")
        table(rows, "rej",
              [(0, 1, "0"), (1, 2, "1"), (2, 4, "2-3"), (4, 8, "4-7"),
               (8, 999, "8+")],
              "LEVEL TESTED (rejections) - deployed: >= 2",
              lambda lo, hi: hi <= 2)
        table(rows, "held",
              [(0, 60, "< 60s"), (60, 120, "60-120s"), (120, 240, "120-240s"),
               (240, 1e9, ">= 240s")],
              "LEVEL HELD - deployed: >= 120s",
              lambda lo, hi: hi <= 120)
        table(rows, "accel",
              [(-1e9, -5, "< -5"), (-5, 0, "-5 to 0"), (0, 10, "0 to +10"),
               (10, 1e9, ">= +10")],
              "MOVE STILL WORKING (accel) - deployed: >= -5",
              lambda lo, hi: hi <= -5)


if __name__ == "__main__":
    main()
