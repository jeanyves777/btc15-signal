"""A gold-specific tuner: correct the volatility horizon, then re-derive the gates.

SUPERSEDED, 2026-09-25. Use scripts/fit_instrument_config.py instead.
This tool reports one gate at a time, and choosing a threshold from its own
best bucket is exactly how silver and SOL came to hold seven individually
defensible numbers that, intersected, admitted 0.05% and 2.16% of their own
corpora and nothing at all live. The tables below are still useful for
SEEING a gradient; they must not be used to SET a threshold. FINDINGS 75.


WHY GOLD LOOKED UNTRADEABLE, AND WHY THAT WAS A MEASUREMENT ERROR.

The deployed rule requires the reference to sit 10-15 VOLATILITY UNITS from the
strike. On gold only 0.6% of setups cleared 10x, against BTC's 33.4%, and the
median normalised distance was 0.83 against BTC's 6.92. Read naively that says
gold never moves far enough from its strike to be worth trading.

It says nothing of the kind. The denominator is wrong. Measuring how much of
the short-horizon volatility actually SURVIVES to settlement:

    ratio = realised move to settlement / (short-window vol x sqrt(horizon))

    GOLD  median 0.039      BTC  median 0.311

Gold's per-second feed is genuinely noisy - 80% distinct values, 1.5% flat
ticks, so this is not quantisation - but almost all of that noise MEAN-REVERTS
within the window. BTC's persists eight times more. So the same formula divides
gold's distance by a volatility that is ~8x too large relative to how BTC is
treated, and the normalised distance is deflated by exactly that factor.

    0.83 x (0.311 / 0.039) = 6.6, against BTC's 6.92

The two instruments were comparable all along; the measurement was not. This is
the third instrument to show that a threshold in "volatility units" is still
instrument-specific - but here the fix is not a new threshold, it is a
volatility that matches the horizon being traded.

WHAT THIS SCRIPT DOES
  1. estimates gold's persistence ratio from its own corpus;
  2. rebuilds normalised distance on a horizon-corrected volatility;
  3. sweeps distance floors on that corrected scale, day-clustered;
  4. scores gold-specific context that crypto does not have - the London and
     COMEX sessions, which are when gold actually trades.

No fees, per the operator's standing instruction.
"""

import argparse
import math
import random
import sqlite3
import statistics
from collections import defaultdict
from datetime import datetime, timezone


def persistence(brti_path: str) -> float:
    """How much of the short-window volatility survives to settlement."""
    d = sqlite3.connect(f"file:{brti_path}?mode=ro", uri=True)
    d.row_factory = sqlite3.Row
    ratios = []
    for r in d.execute(
            "SELECT brti_volatility_bps v, expiration_value ev, "
            "       brti_value bv, remaining_s "
            "  FROM brti_decision_points "
            " WHERE brti_volatility_bps > 0 AND expiration_value IS NOT NULL "
            "   AND brti_value > 0 AND remaining_s BETWEEN 300 AND 900"):
        pred = r["v"] * math.sqrt(r["remaining_s"])
        if pred <= 0:
            continue
        moved = abs(r["ev"] - r["bv"]) / r["bv"] * 1e4
        ratios.append(moved / pred)
    return statistics.median(ratios) if ratios else 0.0


def load(brti_path, market_path, lo, hi, scale):
    m = sqlite3.connect(f"file:{market_path}?mode=ro", uri=True)
    book = {}
    for ticker, ts, bid, ask in m.execute(
            "SELECT ticker, end_period_ts, yes_bid_close, yes_ask_close "
            "FROM contract_candles"):
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
        q = book.get((r["ticker"], ms // 60000 * 60000))
        if q is None:
            continue
        yes_bid, yes_ask = q
        yes_ask = yes_ask / 100 if yes_ask > 1 else yes_ask
        no_ask = round(1 - (yes_bid / 100 if yes_bid > 1 else yes_bid), 4)
        ask = yes_ask if r["brti_side"] == "UP" else no_ask
        if not lo <= ask <= hi:
            continue
        when = datetime.fromtimestamp(r["close_ms"] / 1000, timezone.utc)
        rows.append({
            "won": (r["result"] == "yes") == (r["brti_side"] == "UP"),
            "ask": ask,
            # THE CORRECTION. Distance in units of the volatility that actually
            # reaches settlement, rather than the volatility of the last few
            # seconds of feed noise.
            "dist": (r["d"] or 0.0) * scale,
            "raw": r["d"] or 0.0,
            "mom": abs(r["mo"] or 0.0),
            "hour": when.hour,
            "day": when.date(),
        })
    return rows


def score(sel, label, draws=8000, seed=13, quiet=False):
    if len(sel) < 40:
        if not quiet:
            print(f"  {label:<26} n={len(sel):>5}  too few")
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
    mean = statistics.mean(flat)
    win = sum(1 for r in sel if r["won"]) / len(sel)
    lo, hi = out[int(draws * .025)], out[int(draws * .975)]
    if not quiet:
        flag = "  HOLDS" if lo > 0 else ""
        print(f"  {label:<26}{len(sel):>6}{len(days):>6}{win:>8.1%}"
              f"{mean:>+9.4f}  [{lo:+.4f}, {hi:+.4f}]{flag}")
    return mean, lo, hi, len(sel)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--brti", default="data/brti_history_gold.db")
    p.add_argument("--market", default="data/market_data_kxgold15m.db")
    p.add_argument("--ref-brti", default="data/brti_history.db",
                   help="the instrument the thresholds were calibrated on")
    p.add_argument("--band", default="0.70,0.93")
    args = p.parse_args()
    lo, hi = (float(x) for x in args.band.split(","))

    g, b = persistence(args.brti), persistence(args.ref_brti)
    scale = (b / g) if g > 0 else 1.0
    print("VOLATILITY PERSISTENCE (share of short-window vol reaching settlement)")
    print(f"  gold {g:.4f}   reference {b:.4f}   -> correction x{scale:.2f}\n")

    rows = load(args.brti, args.market, lo, hi, scale)
    print(f"{len(rows)} gold decision points in the band over "
          f"{len({r['day'] for r in rows})} days")
    raw = statistics.median([r["raw"] for r in rows])
    cor = statistics.median([r["dist"] for r in rows])
    print(f"  median normalised distance: raw {raw:.2f} -> corrected "
          f"{cor:.2f}   (BTC's is 6.92)\n")

    print(f"  {'bucket':<26}{'n':>6}{'days':>6}{'win%':>8}"
          f"{'residual':>9}{'95% CI':>22}")
    score(rows, "everything in the band")

    print("\n  DISTANCE FLOOR on the corrected scale")
    for floor in (2, 4, 6, 8, 10, 12, 15):
        score([r for r in rows if r["dist"] >= floor], f"corrected >= {floor}x")

    print("\n  GOLD SESSIONS (UTC) - crypto has no equivalent of these")
    for a, z, lab in ((0, 7, "Asia 00-07"), (7, 12, "London 07-12"),
                      (12, 17, "COMEX 12-17"), (17, 24, "US pm 17-24")):
        score([r for r in rows if a <= r["hour"] < z], lab)

    print("\n  DISTANCE x SESSION (corrected >= 6x)")
    far = [r for r in rows if r["dist"] >= 6]
    for a, z, lab in ((0, 7, "Asia"), (7, 12, "London"),
                      (12, 17, "COMEX"), (17, 24, "US pm")):
        score([r for r in far if a <= r["hour"] < z], f"{lab} + far")


if __name__ == "__main__":
    main()
