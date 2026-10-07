"""The local model reads the trade lifecycle and PROPOSES; the statistics DISPOSE.

The implementation lives in `btc15_signal.hypotheses` (moved there on
2026-09-28 so every learning run can call it). This is the by-hand entry point:
the same propose-and-test cycle, printed as a table.

    python scripts/lifecycle_hypotheses.py --asset BTC
    python scripts/lifecycle_hypotheses.py --asset BTC --offline   # no model
"""

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from btc15_signal import hypotheses as H  # noqa: E402

STORES = {
    "BTC": "btc15.db", "ETH": "eth15.db", "SOL": "sol15.db",
    "XRP": "xrp15.db", "NEAR": "near15.db",
    "GOLD": "gold15.db", "SILVER": "silver15.db",
}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--asset", default="BTC", choices=sorted(STORES))
    ap.add_argument("--url", default="http://127.0.0.1:8080/v1")
    ap.add_argument("--model", default="local")
    ap.add_argument("--timeout", type=float, default=600.0)
    ap.add_argument("--min-n", type=int, default=25)
    ap.add_argument("--min-days", type=int, default=3)
    ap.add_argument("--offline", action="store_true",
                    help="skip the model, score the control set only")
    args = ap.parse_args()

    result = H.run(ROOT / STORES[args.asset], args.asset, url=args.url,
                   model=args.model, timeout=args.timeout, min_n=args.min_n,
                   min_days=args.min_days, offline=args.offline)
    print("=" * 78)
    print(f"LIFECYCLE HYPOTHESES - {args.asset}: {result['windows']} alerted "
          f"windows over {result['days']} days")
    print("=" * 78)
    if result["model_error"]:
        print(f"  model: {result['model_error']}")
    else:
        print(f"  model proposed {result['proposed']} well-formed hypothesis(es)")
    print(f"\n  {'hypothesis':<26}{'n':>5}{'d':>3}{'inside':>9}{'outside':>9}"
          f"{'diff':>9}{'95% CI':>20}{'p':>7}")
    for s in sorted(result["scored"], key=lambda x: (not x["testable"],
                                                      -abs(x.get("diff") or 0))):
        if not s["testable"]:
            print(f"  {s['name'][:26]:<26}{s['n']:>5}{s['days']:>3}"
                  f"{('  ' + s.get('untestable', '')):>47}")
            continue
        mark = "  BH" if s.get("survives") else ""
        print(f"  {s['name'][:26]:<26}{s['n']:>5}{s['days']:>3}"
              f"{s['inside']:>+9.4f}{s['outside']:>+9.4f}{s['diff']:>+9.4f}"
              f"  [{s['lo']:+.4f},{s['hi']:+.4f}]{s['p']:>7.3f}{mark}")
    print("\n  NOTHING HERE IS ADOPTED. A survivor is a CANDIDATE for keying.")
    out = ROOT / "runtime-combo"
    out.mkdir(exist_ok=True)
    f = out / f"hypotheses_{args.asset.lower()}.json"
    f.write_text(json.dumps(result["scored"], indent=1, default=str),
                 encoding="utf-8")
    print(f"  recorded -> {f}")


if __name__ == "__main__":
    main()
