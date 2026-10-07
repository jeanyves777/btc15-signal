"""The signals we ACTUALLY SENT, taken as combos. No replay, no reinvention.

WHAT THIS READS. Every instance's live store carries an `observations` row per
poll, and `alerted = 1` marks the windows where the Telegram signal went out.
The alert fires ONCE per window, at the first qualifying poll, and main.py
records the snapshot AT ALERT TIME on purpose - "so the stored contract price is
the one the alert actually quoted. Settling against a price from five minutes
earlier would score a trade nobody was offered." So the first alerted row in a
window IS the signal that reached the phone: its side, its quoted ask, and what
it settled at.

WHY NOT REPLAY THE RULE. An earlier version of this rebuilt the signal by running
`KalshiBRTIRule.check_facts` over the archive. That answers a different question -
what the CURRENT config would have done to HISTORICAL features - and it silently
excluded BTC and ETH entirely, because their corpora predate `brti_retrace` and
the deployed rule refuses an unmeasurable one. The alerts below were produced by
whatever config was live at the time, on live data, and were acted on. They are
the record; the replay was a reconstruction of it.

THE COMBO. For each 15-minute window, take every instrument that alerted. Two or
three of them is a combo: same window, each leg on the side that instrument's own
alert called. It wins only if EVERY leg settles that way.

PRICING. Cost is the PRODUCT of the legs' quoted asks, which is what the combo
orderbook charges (FINDINGS 80: a resting bid filled at 0.0500 against a product
of 0.0456). The app/RFQ path charges 1.75-2.42x the product, so `b/e` below - the
break-even multiple - is what decides whether the edge survives the venue.

Compared on RETURN PER DOLLAR STAKED, because a combo contract costs the product
while one contract of each leg costs the SUM; per-contract would flatter the
combo for being cheap. Paired within the same windows, day-clustered bootstrap,
Holm-Bonferroni across every basket tested. No fees, per standing instruction.

    python scripts/alert_combo.py
"""

import argparse
import itertools
import random
import sqlite3
import statistics
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

STORES = {
    "BTC": "btc15.db", "ETH": "eth15.db", "SOL": "sol15.db",
    "XRP": "xrp15.db", "NEAR": "near15.db",
    "GOLD": "gold15.db", "SILVER": "silver15.db",
}


def alerts_of(db_name: str) -> dict:
    """{window_open: signal} - the ALERT-TIME row, one per window.

    Opened read-only on purpose: the live Store runs DDL migrations on open, so
    a read-write handle on a running instrument's database is a real hazard.
    """
    path = ROOT / db_name
    if not path.exists():
        return {}
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    cols = {x[1] for x in con.execute("PRAGMA table_info(observations)")}
    if not {"alerted", "won", "our_ask", "side"} <= cols:
        con.close()
        return {}
    out = {}
    # The FIRST alerted row in each window is the moment the alert fired.
    for r in con.execute(
            "SELECT window_open, observed_ms, ticker, side, our_ask, won, "
            "       remaining_s "
            "  FROM observations "
            " WHERE alerted = 1 AND won IS NOT NULL AND our_ask IS NOT NULL "
            " ORDER BY window_open, observed_ms"):
        w = int(r["window_open"])
        if w in out:
            continue
        ask = float(r["our_ask"])
        if not 0.02 <= ask <= 0.98:
            continue
        out[w] = {
            "side": r["side"], "ask": ask, "won": bool(r["won"]),
            "ticker": r["ticker"], "remaining_s": r["remaining_s"],
            "day": datetime.fromtimestamp(w / 1000, timezone.utc).date(),
        }
    con.close()
    return out


def boot(vals, draws=4000, seed=7):
    if not vals:
        return (0.0, 0.0, 0.0)
    byday = defaultdict(list)
    for d, v in vals:
        byday[d].append(v)
    days = list(byday)
    rng = random.Random(seed)
    means = []
    for _ in range(draws):
        pool = [v for d in (rng.choice(days) for _ in days) for v in byday[d]]
        if pool:
            means.append(statistics.fmean(pool))
    means.sort()
    return (statistics.fmean([v for _, v in vals]),
            means[int(0.025 * len(means))], means[int(0.975 * len(means))])


def boot_p(vals, draws=4000, seed=13):
    """Two-sided day-clustered bootstrap p-value for mean != 0."""
    if not vals:
        return 1.0
    byday = defaultdict(list)
    for d, v in vals:
        byday[d].append(v)
    days = list(byday)
    obs = statistics.fmean([v for _, v in vals])
    rng = random.Random(seed)
    hits = 0
    for _ in range(draws):
        pool = [v for d in (rng.choice(days) for _ in days) for v in byday[d]]
        if not pool:
            continue
        if (statistics.fmean(pool) - obs) * (1 if obs > 0 else -1) <= -abs(obs):
            hits += 1
    return max((2.0 * hits) / draws, 1.0 / draws)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--legs", type=int, nargs="+", default=[2, 3])
    ap.add_argument("--min-n", type=int, default=20)
    ap.add_argument("--min-days", type=int, default=3,
                    help="a day-clustered bootstrap over ONE day resamples "
                         "that same day every draw, so its interval collapses "
                         "to a point and its p-value is 1/draws. Baskets under "
                         "this floor are reported as UNTESTABLE, not as wins.")
    args = ap.parse_args()

    print("=" * 80)
    print("THE ALERTS WE ACTUALLY SENT, TAKEN AS COMBOS")
    print("=" * 80)
    sig = {}
    for asset, db in STORES.items():
        a = alerts_of(db)
        if not a:
            print(f"  {asset:<7} no alerts recorded")
            continue
        sig[asset] = a
        wr = statistics.fmean([1.0 if v["won"] else 0.0 for v in a.values()])
        ask = statistics.fmean([v["ask"] for v in a.values()])
        days = sorted({v["day"] for v in a.values()})
        print(f"  {asset:<7}{len(a):>5} alerts  {len(days)} days "
              f"({days[0]}..{days[-1]})  win {wr:>6.1%}  "
              f"mean ask {ask:.3f}  residual {wr - ask:+.4f}")

    assets = sorted(sig)
    print("\n  residual = win rate - quoted ask = EV per contract, no fees.")
    print("  THIS is the single-leg edge a combo has to beat.")

    results = []
    for k in args.legs:
        for combo in itertools.combinations(assets, k):
            rows = []
            for w in sig[combo[0]]:
                legs = [sig[a].get(w) for a in combo]
                if any(x is None for x in legs):
                    continue
                cost = 1.0
                for x in legs:
                    cost *= x["ask"]
                stake = sum(x["ask"] for x in legs)
                allwin = all(x["won"] for x in legs)
                rows.append({
                    "day": legs[0]["day"], "cost": cost, "win": allwin,
                    "combo_roi": ((1.0 if allwin else 0.0) - cost) / cost,
                    "single_roi": sum((1.0 if x["won"] else 0.0) - x["ask"]
                                      for x in legs) / stake,
                })
            ndays = len({r["day"] for r in rows})
            if len(rows) < args.min_n:
                continue
            mu, lo, hi = boot([(r["day"], r["combo_roi"]) for r in rows])
            smu, _, _ = boot([(r["day"], r["single_roi"]) for r in rows])
            dmu, dlo, dhi = boot([(r["day"], r["combo_roi"] - r["single_roi"])
                                  for r in rows])
            pval = boot_p([(r["day"], r["combo_roi"] - r["single_roi"])
                           for r in rows])
            wr = statistics.fmean([1.0 if r["win"] else 0.0 for r in rows])
            cost = statistics.fmean([r["cost"] for r in rows])
            sd = statistics.pstdev([r["combo_roi"] for r in rows]) \
                if len(rows) > 1 else 0.0
            ssd = statistics.pstdev([r["single_roi"] for r in rows]) \
                if len(rows) > 1 else 0.0
            results.append({
                "testable": ndays >= args.min_days,
                "legs": k, "name": "+".join(combo), "n": len(rows),
                "winrate": wr, "cost": cost, "roi": mu, "single": smu,
                "diff": dmu, "diff_lo": dlo, "diff_hi": dhi, "p": pval,
                "days": len({r["day"] for r in rows}),
                "breakeven_x": (wr / cost) if cost else 0.0,
                "ir": (mu / sd) if sd else 0.0,
                "sir": (smu / ssd) if ssd else 0.0,
            })

    # Holm-Bonferroni over every basket tested. A search across baskets will
    # find positive intervals whether or not anything is there.
    testable = [r for r in results if r["testable"]]
    for r in results:
        r["holm"] = False
    m = len(testable)
    still = True
    for i, r in enumerate(sorted(testable, key=lambda x: x["p"])):
        r["holm"] = still and r["p"] <= 0.05 / (m - i)
        if not r["holm"]:
            still = False
    alive = [r for r in testable if r["holm"]]

    for k in args.legs:
        rows = [r for r in results if r["legs"] == k]
        print()
        print("=" * 80)
        print(f"  {k}-LEG COMBOS OF SIMULTANEOUS ALERTS")
        print("=" * 80)
        if not rows:
            print(f"  no basket reached {args.min_n} shared windows")
            continue
        print(f"  {'basket':<18}{'n':>4}{'d':>3}{'all-win':>9}{'cost':>7}"
              f"{'combo ROI':>11}{'singles':>9}{'diff':>9}"
              f"{'95% CI on diff':>20}{'b/e':>7}")
        for r in sorted(rows, key=lambda x: (-x["testable"], -x["diff"])):
            mark = ("  HOLM" if r["holm"]
                    else ("" if r["testable"] else "  UNTESTABLE"))
            print(f"  {r['name']:<18}{r['n']:>4}{r['days']:>3}"
                  f"{r['winrate']:>9.1%}{r['cost']:>7.3f}{r['roi']:>+11.1%}"
                  f"{r['single']:>+9.1%}{r['diff']:>+9.1%}"
                  f"  [{r['diff_lo']:+.1%},{r['diff_hi']:+.1%}]"
                  f"{r['breakeven_x']:>6.2f}x{mark}")

    print()
    print("  n/d       = shared windows / distinct days. A handful of days is a")
    print("              handful of days however many windows it contains.")
    print("  combo ROI = return per dollar staked, cost = product of the asks")
    print("  singles   = same windows, one contract of each leg, per dollar")
    print("  b/e       = most payable as a multiple of the product before the")
    print("              edge is gone. The app/RFQ path charges 1.75-2.42x the")
    print("              product, so anything under that dies on that venue.")
    print()
    untest = [r for r in results if not r["testable"]]
    print(f"  {len(alive)} of {m} TESTABLE baskets survive Holm-Bonferroni")
    print(f"  at FWER 0.05. {len(untest)} baskets span fewer than "
          f"{args.min_days} days and are")
    print("  marked UNTESTABLE: their intervals are points because the")
    print("  bootstrap has only one cluster to draw from. That is a symptom of")
    print("  no data, not of a strong result - do not read those rows as wins.")


if __name__ == "__main__":
    main()
