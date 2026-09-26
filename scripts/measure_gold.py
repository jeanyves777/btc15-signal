"""Does the BTC/ETH 15-minute rule have anything to say about GOLD?

THE TRANSFER FAILS BEFORE IT IS TESTED, and the reason is worth stating first.
The deployed rule requires the reference to sit a minimum number of VOLATILITY
UNITS away from the strike - 10x in a calm market, 15x in a moving one. On the
gold corpus:

    GOLD   vol 5.67bp   normalised distance median 0.83   clears 10x:  0.6%
    BTC    vol 0.77bp   normalised distance median 6.92   clears 10x: 33.4%

Gold's per-second reference is roughly seven times noisier in bps than BRTI, so
the same dollar gap from the strike buys far fewer volatility units. The rule
would fire on about three of 584 markets. That is not a poor result, it is no
result, and re-running the deployed thresholds would only produce a number
computed on a handful of rows.

So this measures the question BEHIND the rule instead: at gold's own scale, is
a favourite whose reference has already moved away from the strike underpriced
the way BTC's is? Distance is therefore bucketed by PERCENTILE within gold, not
by BTC's absolute thresholds.

Scored on the calibration residual - win rate minus the implied probability the
ask already carries - because a win rate alone says nothing about whether the
price was wrong. Day-clustered, since points inside a day share a price path.
No fees, per the operator's standing instruction.
"""

import argparse
import random
import sqlite3
import statistics
from collections import defaultdict
from datetime import datetime, timezone


def load(brti_path: str, market_path: str, lo: float, hi: float):
    m = sqlite3.connect(f"file:{market_path}?mode=ro", uri=True)
    book = {}
    for ticker, ts, bid, ask in m.execute(
            "SELECT ticker, end_period_ts, yes_bid_close, yes_ask_close "
            "  FROM contract_candles"):
        if bid is None or ask is None:
            continue
        book[(ticker, ts * 1000 if ts < 1e11 else ts)] = (bid, ask)

    b = sqlite3.connect(f"file:{brti_path}?mode=ro", uri=True)
    b.row_factory = sqlite3.Row
    rows = []
    for r in b.execute(
            "SELECT ticker, remaining_s, close_ms, result, brti_side, "
            "       brti_normalized_distance d, brti_momentum_bps mo "
            "  FROM brti_decision_points "
            " WHERE result IN ('yes','no') "
            "   AND brti_normalized_distance IS NOT NULL"):
        ms = r["close_ms"] - r["remaining_s"] * 1000
        quote = book.get((r["ticker"], ms // 60000 * 60000))
        if quote is None:
            continue
        yes_bid, yes_ask = quote
        yes_ask = yes_ask / 100 if yes_ask > 1 else yes_ask
        no_ask = round(1 - (yes_bid / 100 if yes_bid > 1 else yes_bid), 4)
        side = r["brti_side"]
        ask = yes_ask if side == "UP" else no_ask
        if not lo <= ask <= hi:
            continue
        rows.append({
            "won": (r["result"] == "yes") == (side == "UP"),
            "ask": ask, "dist": r["d"] or 0.0,
            "mom": abs(r["mo"] or 0.0),
            "day": datetime.fromtimestamp(
                r["close_ms"] / 1000, timezone.utc).date(),
        })
    return rows


def score(sel, label, draws=8000, seed=13):
    if len(sel) < 40:
        print(f"  {label:<28} n={len(sel):>5}  too few")
        return
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
    mean = statistics.mean(flat)
    win = sum(1 for r in sel if r["won"]) / len(sel)
    ask = statistics.mean(r["ask"] for r in sel)
    lo, hi = out[int(draws * .025)], out[int(draws * .975)]
    flag = "  HOLDS" if lo > 0 else ""
    print(f"  {label:<28}{len(sel):>6}{len(days):>6}{win:>8.1%}{ask:>8.3f}"
          f"{mean:>+9.4f}  [{lo:+.4f}, {hi:+.4f}]{flag}")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--brti", default="data/brti_history_gold.db")
    p.add_argument("--market", default="data/market_data_kxgold15m.db")
    p.add_argument("--band", default="0.70,0.93")
    p.add_argument("--label", default="GOLD")
    args = p.parse_args()
    lo, hi = (float(x) for x in args.band.split(","))

    rows = load(args.brti, args.market, lo, hi)
    if not rows:
        raise SystemExit("no priced decision points")
    days = len({r["day"] for r in rows})
    print(f"{args.label}: {len(rows)} decision points in the "
          f"{lo:.2f}-{hi:.2f} band over {days} days\n")
    print(f"  {'bucket':<28}{'n':>6}{'days':>6}{'win%':>8}{'ask':>8}"
          f"{'residual':>9}{'95% CI':>22}")

    score(rows, "everything in the band")

    # Gold's OWN distance scale. Using BTC's absolute floors here would put
    # 99.4% of the corpus in one bucket and prove nothing.
    dists = sorted(r["dist"] for r in rows)
    cuts = [dists[int(len(dists) * q)] for q in (0.25, 0.5, 0.75, 0.9)]
    print(f"\n  distance percentiles (gold's own scale): "
          f"p25={cuts[0]:.2f} p50={cuts[1]:.2f} p75={cuts[2]:.2f} "
          f"p90={cuts[3]:.2f}")
    edges = [(-1e9, cuts[0], "distance p0-25"),
             (cuts[0], cuts[1], "distance p25-50"),
             (cuts[1], cuts[2], "distance p50-75"),
             (cuts[2], cuts[3], "distance p75-90"),
             (cuts[3], 1e9, "distance p90+ (farthest)")]
    for a, b, lab in edges:
        score([r for r in rows if a <= r["dist"] < b], lab)

    print()
    for a, b, lab in ((-1e9, 5.0, "calm  (|mom| < 5bp)"),
                      (5.0, 1e9, "moving(|mom| >= 5bp)")):
        score([r for r in rows if a <= r["mom"] < b], lab)


if __name__ == "__main__":
    main()
