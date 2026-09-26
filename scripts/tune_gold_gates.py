"""Set every GOLD gate from GOLD's own data. No zeros, no None, no BTC values.

SUPERSEDED, 2026-09-25. Use scripts/fit_instrument_config.py instead.
This tool reports one gate at a time, and choosing a threshold from its own
best bucket is exactly how silver and SOL came to hold seven individually
defensible numbers that, intersected, admitted 0.05% and 2.16% of their own
corpora and nothing at all live. The tables below are still useful for
SEEING a gradient; they must not be used to SET a threshold. FINDINGS 75.


THE DEFECT THIS ANSWERS, 2026-09-25. A gold alert read "Entry checks 8/8" on
KXGOLD15M-26SEP250945-45 while five of those eight gates were configured so
that no input could fail them - `accel >= -1e9`, `held >= 0s`,
`rejections >= 0`, `momentum >= 0.0`, `retrace <= 1.0`. Three real checks were
presented as eight passed protections. The operator's instruction is that none
or zero are not acceptable: every gate must carry a threshold measured on the
instrument it gates.

They were set to those values honestly - BTC's thresholds HURT on gold, and
this codebase's rule is that an unmeasured gate is worse than no gate. But
"disabled" and "measured" are not the only options: the third is to measure it
here, which is what this does.

METHOD. Each feature is bucketed inside the deployed gold gate (ask 0.65-0.75,
distance 1.5-6.0bp, 400-700s remaining) and scored on the calibration residual
- win rate minus the price the market quoted - day-clustered, because points
inside a day share a price path. The bucket table shows the DIRECTION before
any threshold is chosen, so a gate is never pointed the wrong way by assuming
it should work as it does on BTC.

A threshold is only proposed where the buckets are ordered and the interval
clears zero. Where they are not, this says so rather than picking the best
cell - that is the failure mode that produced four withdrawn findings in one
session.

No fees, per the operator's standing instruction.
"""

import argparse
import random
import sqlite3
import statistics
from collections import defaultdict
from datetime import datetime, timezone

BAND = (0.65, 0.75)
DIST = (1.5, 6.0)
REMAIN = (400, 700)


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
    extra = [c for c in ("brti_retrace", "brti_choppiness") if c in cols]
    select = ", ".join(["ticker", "remaining_s", "close_ms", "result",
                        "brti_side", "signed_distance_bps", "brti_accel",
                        "brti_held_s", "brti_rejections", "brti_momentum_bps",
                        "brti_volatility_bps"] + extra)
    rows = []
    for r in b.execute(f"SELECT {select} FROM brti_decision_points "
                       f" WHERE result IN ('yes','no') "
                       f"   AND signed_distance_bps IS NOT NULL"):
        ms = r["close_ms"] - r["remaining_s"] * 1000
        quote = book.get((r["ticker"], ms // 60000 * 60000))
        if quote is None:
            continue
        yes_bid, yes_ask = quote
        yes_ask = yes_ask / 100 if yes_ask > 1 else yes_ask
        no_ask = round(1 - (yes_bid / 100 if yes_bid > 1 else yes_bid), 4)
        ask = yes_ask if r["brti_side"] == "UP" else no_ask
        gap = abs(r["signed_distance_bps"])
        if not (BAND[0] <= ask <= BAND[1] and DIST[0] <= gap <= DIST[1]
                and REMAIN[0] <= r["remaining_s"] <= REMAIN[1]):
            continue
        row = {
            "won": (r["result"] == "yes") == (r["brti_side"] == "UP"),
            "ask": ask,
            "accel": r["brti_accel"],
            "held": r["brti_held_s"],
            "rej": r["brti_rejections"],
            "mom": abs(r["brti_momentum_bps"] or 0.0),
            "vol": r["brti_volatility_bps"],
            "day": datetime.fromtimestamp(
                r["close_ms"] / 1000, timezone.utc).date(),
        }
        for c in extra:
            row[c.replace("brti_", "")] = r[c]
        rows.append(row)
    return rows, extra


def score(sel, draws=6000, seed=11):
    if len(sel) < 60:
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


def buckets(rows, key, edges, label):
    print(f"\n  {label}")
    print(f"    {'bucket':<22}{'n':>6}{'win%':>8}{'residual':>10}"
          f"{'95% CI':>22}")
    seen = []
    for lo, hi, name in edges:
        sel = [r for r in rows
               if r.get(key) is not None and lo <= r[key] < hi]
        s = score(sel)
        if s is None:
            print(f"    {name:<22}n={len(sel):<5} too few")
            continue
        mu, l, h, n, win = s
        seen.append((name, mu, l))
        print(f"    {name:<22}{n:>6}{win:>8.1%}{mu:>+10.4f}"
              f"  [{l:+.4f}, {h:+.4f}]{'  HOLDS' if l > 0 else ''}")
    return seen


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--brti", default="data/brti_history_gold.db")
    p.add_argument("--market", default="data/market_data_kxgold15m.db")
    args = p.parse_args()
    rows, extra = load(args.brti, args.market)
    print(f"GOLD points inside the deployed gate: {len(rows)} over "
          f"{len({r['day'] for r in rows})} days")
    base = score(rows)
    if base:
        print(f"  baseline residual {base[0]:+.4f}  [{base[1]:+.4f}, "
              f"{base[2]:+.4f}]  win {base[4]:.1%}")

    buckets(rows, "accel",
            [(-1e9, -10, "accel < -10"), (-10, 0, "-10 to 0"),
             (0, 10, "0 to +10"), (10, 1e9, "accel >= +10")],
            "MOVE STILL WORKING (accel, bps) - currently -1e9 = disabled")
    buckets(rows, "held",
            [(0, 60, "held < 60s"), (60, 120, "60-120s"),
             (120, 240, "120-240s"), (240, 1e9, ">= 240s")],
            "LEVEL HELD (seconds on side) - currently 0 = disabled")
    buckets(rows, "rej",
            [(0, 2, "0-1 rejections"), (2, 5, "2-4"), (5, 10, "5-9"),
             (10, 1e9, "10+")],
            "LEVEL TESTED (rejections) - currently 0 = disabled")
    buckets(rows, "mom",
            [(0, 2, "|mom| < 2bp"), (2, 5, "2-5bp"), (5, 10, "5-10bp"),
             (10, 1e9, ">= 10bp")],
            "MOMENTUM (|bps|) - currently 0.0 = disabled")
    buckets(rows, "vol",
            [(0, 6, "vol < 6bp"), (6, 10, "6-10bp"), (10, 14, "10-14bp"),
             (14, 1e9, ">= 14bp")],
            "REFERENCE VOLATILITY (bps) - ungated; the alert's 11.5bp sat here")
    for c in extra:
        k = c.replace("brti_", "")
        if k == "retrace":
            buckets(rows, k, [(0, .25, "< 25% given back"), (.25, .5, "25-50%"),
                              (.5, .75, "50-75%"), (.75, 1.01, "75-100%")],
                    "GIVEBACK (retrace) - currently 1.0 = disabled")
        if k == "choppiness":
            buckets(rows, k, [(0, .5, "chop < 50%"), (.5, .75, "50-75%"),
                              (.75, .9, "75-90%"), (.9, 1.01, ">= 90%")],
                    "CHOPPINESS - currently not a gate at all")
    if not extra:
        print("\n  retrace and choppiness are NOT in the gold corpus - they "
              "cannot be\n  set from measurement until the backfill stores "
              "them.")


if __name__ == "__main__":
    main()
