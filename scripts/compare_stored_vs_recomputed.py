"""Do the STORED corpus features match features recomputed now, market by market?

THE LAST AMBIGUITY. With the entry window applied to both, the corpus says gold's
deployed set scores +0.0900 [+0.0406, +0.1449] over the last 8 days while a
denser consecutive replay of the same days says +0.0069 [-0.0213, +0.0558]. Two
explanations remain and they have opposite consequences:

  SAMPLING   the corpus holds 148 of roughly 768 markets in those days, the
             replay 460. Different subsets of the same population, and the
             smaller one happens to be favourable. Then the corpus fit is
             sound but its interval is the honest width and the recent point
             estimate is unlucky.
  STALENESS  the stored features do not describe what `features_from_series`
             computes today - a definition changed after the backfill ran, and
             the fit was made on numbers the live rule no longer produces.
             Then the corpus fit means nothing at all.

They are separated by taking markets present in BOTH and comparing feature for
feature at the same decision minutes. Same ticker, same second, stored against
recomputed. If they agree, it is sampling. If they drift, the corpus is stale and
every threshold on it has to be refitted from scratch.

No fees, per the operator's standing instruction.
"""

import argparse
import asyncio
import sqlite3
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from btc15_signal.brti import KalshiBRTI, features_from_series  # noqa: E402
from btc15_signal.config import Settings  # noqa: E402

FIELDS = [
    ("signed_distance_bps", "signed_distance_bps"),
    ("brti_momentum_bps", "brti_momentum_bps"),
    ("brti_volatility_bps", "brti_volatility_bps"),
    ("brti_normalized_distance", "brti_normalized_distance"),
    ("brti_accel", "brti_accel"),
    ("brti_held_s", "brti_held_s"),
    ("brti_rejections", "brti_rejections"),
    ("brti_retrace", "brti_retrace"),
]


async def run(args) -> None:
    b = sqlite3.connect(f"file:{args.brti}?mode=ro", uri=True)
    b.row_factory = sqlite3.Row
    rows = [dict(r) for r in b.execute(
        "SELECT * FROM brti_decision_points "
        " WHERE result IN ('yes','no') ORDER BY close_ms DESC")]
    b.close()
    tickers, seen = [], set()
    for r in rows:
        if r["ticker"] not in seen:
            seen.add(r["ticker"])
            tickers.append(r["ticker"])
        if len(tickers) >= args.markets:
            break
    by_ticker = {}
    for r in rows:
        by_ticker.setdefault(r["ticker"], []).append(r)

    print(f"{args.asset.upper()}  comparing {len(tickers)} markets, "
          f"stored against recomputed")
    settings = Settings()
    client = KalshiBRTI(settings.kalshi_base_url, timeout=25)
    diffs = {name: [] for name, _ in FIELDS}
    compared = missing = 0
    try:
        for t in tickers:
            event = t.rsplit("-", 1)[0]
            try:
                series = await client.series(event)
            except Exception:  # noqa: BLE001
                missing += 1
                continue
            if not series:
                missing += 1
                continue
            series.sort()
            for r in by_ticker[t]:
                cutoff = r["close_ms"] - r["remaining_s"] * 1000
                upto = [(x, v) for x, v in series if x <= cutoff]
                if len(upto) < 120:
                    continue
                f = features_from_series(event, upto, r["target"], cutoff)
                if f is None:
                    continue
                compared += 1
                for name, attr in FIELDS:
                    stored, now = r[name], getattr(f, attr)
                    if stored is None or now is None:
                        if stored != now:
                            diffs[name].append(float("nan"))
                        continue
                    diffs[name].append(float(now) - float(stored))
    finally:
        await client.close()

    if not compared:
        raise SystemExit("nothing comparable")
    print(f"  {compared} decision points compared "
          f"({missing} markets no longer served)")
    print(f"    {'field':<28}{'n':>6}{'mean diff':>12}{'max |diff|':>12}"
          f"{'exact':>8}")
    verdict = "SAMPLING"
    for name, _ in FIELDS:
        vals = [d for d in diffs[name] if d == d]           # drop NaN
        nans = len(diffs[name]) - len(vals)
        if not vals:
            print(f"    {name:<28}{0:>6}   all None on both sides")
            continue
        worst = max(abs(v) for v in vals)
        exact = sum(1 for v in vals if abs(v) < 1e-9) / len(vals)
        flag = ""
        if worst > 0.01 and exact < 0.99:
            flag = "   <- DRIFT"
            verdict = "STALENESS"
        print(f"    {name:<28}{len(vals):>6}{statistics.mean(vals):>+12.5f}"
              f"{worst:>12.5f}{exact:>8.1%}{flag}"
              + (f"  ({nans} None/value mismatches)" if nans else ""))
    print(f"\n  VERDICT: {verdict}")
    if verdict == "SAMPLING":
        print("  The stored features match what the code computes today, so the")
        print("  corpus is not stale and the gap between the fit and the replay")
        print("  is two different subsets of the same days. The fit stands; its")
        print("  interval, not its point estimate, is the honest claim.")
    else:
        print("  The stored features do NOT match what the code computes today.")
        print("  Every threshold fitted on this corpus describes a quantity the")
        print("  live rule no longer produces and must be refitted.")


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--asset", required=True)
    p.add_argument("--brti", required=True)
    p.add_argument("--markets", type=int, default=40)
    asyncio.run(run(p.parse_args()))


if __name__ == "__main__":
    main()
