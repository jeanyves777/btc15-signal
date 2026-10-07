"""Does the chosen set still work in the LAST FEW DAYS, on the corpus itself?

WHY THIS IS ASKED SEPARATELY. The 500-market replay of recent markets scores
gold's admitted group at +0.0130 [-0.0071, +0.0323] over 8 days, while the same
set on the merged corpus scores +0.0925 [+0.0613, +0.1219] over 35 days. Those
intervals do not overlap, so one of two things is true and they have different
consequences:

  PERIOD   the edge is weaker in the last few days than over the 35. Then the
           corpus number is a historical average and the recent one is the
           relevant one.
  METHOD   the replay and the fit disagree for a reason that is not about time -
           a different market set, a different price lookup, a different
           estimator - and the discrepancy says nothing about the edge.

The two are separated by scoring the SAME corpus, with the SAME code, split at a
date. If the corpus also weakens in its last days, it is period. If the corpus
stays strong in exactly the days the replay calls weak, it is method, and the
replay is measuring something else.

No fees, per the operator's standing instruction.
"""

import argparse
import json
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import importlib.util

spec = importlib.util.spec_from_file_location(
    "fit", Path(__file__).resolve().parent / "fit_instrument_config.py")
fit = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fit)

ROOT = Path(__file__).resolve().parents[1]


def deployed_set(asset: str) -> dict:
    cfg = json.loads((ROOT / f"strategy_kalshi_{asset}.json").read_text(
        encoding="utf-8"))
    return {
        "price": (cfg["min_ask"], cfg["max_ask"]),
        "gap": (cfg.get("min_abs_distance_bps") or 0.0,
                cfg.get("max_abs_distance_bps") or 1e9),
        "mom": cfg.get("max_brti_momentum_bps") or 1e9,
        "accel": abs(cfg.get("max_brti_accel") or 1e9),
        "held": cfg.get("min_brti_held_s") or 0.0,
        "rej": cfg.get("min_brti_rejections") or 0,
        "win": (cfg["entry_from_seconds"], cfg["entry_to_seconds"]),
        "retrace": (cfg.get("max_brti_retrace", 1.01),
                    bool(cfg.get("require_measurable_retrace", False))),
    }


def report(label, rows, allrows):
    s = fit.score(rows, draws=6000)
    if s is None:
        print(f"    {label:<22} n={len(rows):<5} too few to score")
        return
    d = fit.delta(rows, allrows)
    dd = (f"{d['mu']:+.4f} [{d['lo']:+.4f},{d['hi']:+.4f}]" if d else "n/a")
    print(f"    {label:<22}{s['n']:>6}{s['days']:>6}{s['win']:>8.1%}"
          f"{s['ask']:>8.3f}{s['mu']:>+10.4f}  [{s['lo']:+.4f},{s['hi']:+.4f}]"
          f"   vs ungated {dd}")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--asset", required=True)
    p.add_argument("--brti", required=True)
    p.add_argument("--market", required=True)
    p.add_argument("--recent-days", type=int, default=8)
    args = p.parse_args()

    rows = fit.load(args.brti, args.market)
    if not rows:
        raise SystemExit("no priced rows")
    c = deployed_set(args.asset)
    last = max(r["day"] for r in rows)
    cut = last - timedelta(days=args.recent_days)

    print(f"{args.asset.upper()}  corpus {min(r['day'] for r in rows)} .. "
          f"{last}   split at {cut}")
    print(f"    {'slice':<22}{'n':>6}{'days':>6}{'win%':>8}{'ask':>8}"
          f"{'residual':>10}{'95% CI':>22}")

    for label, keep in (
            ("ALL, ungated", lambda r: True),
            ("ALL, deployed set", lambda r: fit.admits(r, c)),
            (f"last {args.recent_days}d, ungated", lambda r: r["day"] > cut),
            (f"last {args.recent_days}d, set",
             lambda r: r["day"] > cut and fit.admits(r, c)),
            (f"before, ungated", lambda r: r["day"] <= cut),
            (f"before, set", lambda r: r["day"] <= cut and fit.admits(r, c)),
    ):
        sel = [r for r in rows if keep(r)]
        pool = ([r for r in rows if r["day"] > cut] if "last" in label
                else [r for r in rows if r["day"] <= cut] if "before" in label
                else rows)
        report(label, sel, pool)

    # How much of the corpus even falls in the recent window? A stride-sampled
    # corpus can hold very few of the most recent markets, in which case the
    # recent slice is not a measurement of that period at all.
    recent = [r for r in rows if r["day"] > cut]
    tickers = len({r["ticker"] for r in recent})
    print(f"\n    the recent slice holds {len(recent)} points over {tickers} "
          f"markets - if that is a handful,")
    print(f"    the corpus cannot speak about this period and the replay is "
          f"the only evidence.")


if __name__ == "__main__":
    main()
