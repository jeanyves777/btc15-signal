"""State the minimum detectable effect BEFORE measuring a candidate gate.

WHY THIS EXISTS. Five candidate gates were measured on ~5 days of the bot's own
executed trades in one session and all five died (FINDINGS 63, 64, 66, 68
reversed on the corpus; 67 was an artifact of its own data fetch). The cause was
not weak analysis. Each one was measured on a sample whose noise floor was
larger than the effect being claimed, and the arithmetic was fixed before any
of the analysis began:

    deployed-gate residual  +0.0375   (corpus, ~5.5k points over 68 days)
    per-DAY sd               0.061
    MDE at 5 days           +-0.0759   <- twice the entire edge

Every finding that reversed measured between +0.08 and +0.11. Those were not
weak effects that failed; they were the five-day noise floor read as signal. A
best-of-N sweep over five days returns a cell near +0.08 whether or not
anything is there, and a day-clustered bootstrap will report it precisely,
because the bootstrap correctly describes the precision of a quantity that
happens to be noise.

So the order of operations matters: compute what the sample CAN resolve, then
look at the result. A claimed effect below the MDE is not evidence however good
its interval looks. One far above the MDE is also a warning, since a genuine
improvement to a +0.038 edge is unlikely to be +0.11.

The clustering unit is the DAY, not the trade: trades inside a day share one
price path, so 170 trades over 5 days carry roughly the information of 5
observations, not 170. That is why the trade count barely moves the MDE.

    python scripts/mde.py                         # corpus, deployed gates
    python scripts/mde.py --days 5 --effect 0.08  # can 5 days see +0.08?
"""

import argparse
import math
import sqlite3
import statistics
from collections import defaultdict
from datetime import datetime, timezone

# 80% power, two-sided alpha 0.05: z(0.975) + z(0.80).
Z_SUM = 1.959963985 + 0.841621234


def deployed_residuals(brti_path: str, market_path: str,
                       lo: float, hi: float) -> dict:
    """Residuals (won - implied) at every corpus point the live gates admit.

    The price band and the momentum-scaled distance floor are the DEPLOYED
    ones, because the question is always "what can this sample resolve about a
    change to what is running", not about some other rule.
    """
    m = sqlite3.connect(f"file:{market_path}?mode=ro", uri=True)
    book = {}
    for ticker, ts, bid, ask in m.execute(
            "SELECT ticker, end_period_ts, yes_bid_close, yes_ask_close "
            "FROM contract_candles"):
        if ask is None or bid is None:
            continue
        book[(ticker, ts * 1000 if ts < 1e11 else ts)] = (bid, ask)

    b = sqlite3.connect(f"file:{brti_path}?mode=ro", uri=True)
    b.row_factory = sqlite3.Row
    byday = defaultdict(list)
    for r in b.execute(
            "SELECT ticker, remaining_s, close_ms, result, brti_side, "
            "       brti_normalized_distance d, brti_momentum_bps mo "
            "  FROM brti_decision_points WHERE result IN ('yes','no')"):
        ms = r["close_ms"] - r["remaining_s"] * 1000
        quote = book.get((r["ticker"], ms // 60000 * 60000))
        if quote is None:
            continue
        yes_bid, yes_ask = quote
        yes_ask = yes_ask / 100 if yes_ask > 1 else yes_ask
        no_ask = round(1 - (yes_bid / 100 if yes_bid > 1 else yes_bid), 4)
        ask = yes_ask if r["brti_side"] == "UP" else no_ask
        if not lo <= ask <= hi:
            continue
        dist = r["d"] or 0.0
        mom = abs(r["mo"] or 0.0)
        if dist < (10.0 if mom < 5.0 else 15.0):
            continue
        won = (r["result"] == "yes") == (r["brti_side"] == "UP")
        day = datetime.fromtimestamp(
            r["close_ms"] / 1000, timezone.utc).date()
        byday[day].append((1.0 if won else 0.0) - ask)
    return byday


def mde(sd_day: float, days: int) -> float:
    """Smallest effect this many DAYS can detect at 80% power."""
    return Z_SUM * sd_day / math.sqrt(max(days, 1))


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--brti", default="data/brti_history.db")
    p.add_argument("--market", default="data/market_data.db")
    p.add_argument("--band", default="0.70,0.93")
    p.add_argument("--days", type=int, default=None,
                   help="ask about one sample size")
    p.add_argument("--effect", type=float, default=None,
                   help="the effect a candidate claims, e.g. 0.08")
    args = p.parse_args()
    lo, hi = (float(x) for x in args.band.split(","))

    byday = deployed_residuals(args.brti, args.market, lo, hi)
    # A day with one or two points estimates its own mean too poorly to
    # contribute to a between-day spread.
    means = [statistics.mean(v) for v in byday.values() if len(v) >= 3]
    flat = [x for v in byday.values() for x in v]
    if len(means) < 3:
        raise SystemExit("not enough days with >=3 points")

    sd_day = statistics.stdev(means)
    print(f"deployed-gate residual: {statistics.mean(flat):+.4f}   "
          f"n={len(flat)}   days={len(byday)}")
    print(f"  per-TRADE sd {statistics.stdev(flat):.3f}   "
          f"per-DAY sd {sd_day:.3f}   (the day is the unit)")

    if args.days:
        value = mde(sd_day, args.days)
        print(f"\n{args.days} days can detect {value:+.4f} at 80% power")
        if args.effect:
            verdict = ("NOT EVIDENCE - below the noise floor"
                       if args.effect < value else
                       "resolvable, but check it is not implausibly large")
            print(f"  a claimed effect of {args.effect:+.4f}: {verdict}")
        return

    print("\nminimum detectable effect, 80% power, two-sided 5%:")
    for days in (5, 10, 20, 40, len(byday), 120):
        value = mde(sd_day, days)
        note = "  <- CANNOT see the deployed edge" if value > 0.0375 else ""
        print(f"  {days:>4} days   {value:+.4f}{note}")
    print(f"\n  to resolve HALF the deployed edge (+0.0188) needs "
          f"~{(Z_SUM * sd_day / 0.0188) ** 2:.0f} days")


if __name__ == "__main__":
    main()
