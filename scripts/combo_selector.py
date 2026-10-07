"""Which combo to buy, this window. Runs every 15 minutes.

CORRECTION, 2026-09-26: THE PREMISE BELOW IS WRONG FOR THE QUOTED PATH.
The exchange does NOT price a combo as the product of its legs. Read off the
operator's own app screens, and confirmed against live RFQ quotes:

    legs                product   copula   app/RFQ   vs product   vs copula
    0.63 0.62 0.74 up    0.2890   0.4732    0.5426      1.88x        1.15x
    0.60 0.55 0.68 up    0.2244   0.4132    0.5435      2.42x        1.32x

Kalshi ALREADY prices the correlation and adds a margin on top, so there is no
independence mispricing to harvest from a quote. The ranking below still finds
the combos whose PRODUCT is most out of line with the joint model - keep it for
that - but the "+690% edge" reading of those numbers was never real.

WHERE THE EDGE ACTUALLY LIVES: the combo's own ORDERBOOK, which does quote near
the product. A resting bid filled at 0.0500 where the product was 0.0456 and the
copula said 0.1219 - 41% of fair value. Same instrument, opposite side of the
edge, decided entirely by venue. See combo_rfq.py and measure_combo_markup.py.

THE ORIGINAL MEASUREMENT, still sound as a statement about the ASSETS, on 40,578
aligned (window, minute) points across BTC, ETH, SOL, XRP and NEAR. These assets
are not independent: pairwise phi runs 0.35 to 0.62, and all five settle the SAME
way 45.4% of the time where the product implies 19.3%.

So a SAME-DIRECTION combo is structurally underpriced and a MIXED one is
structurally overpriced by about the same factor. Split-half stable: 2.48x and
2.49x on disjoint halves.

    legs   same-direction lift   mixed
    2            1.50x           0.50x
    3            2.49x           0.50x
    4            4.33x           0.53x
    5            7.72x           0.55x

AND THE LIFT DECAYS THROUGH THE WINDOW, because the market learns which way the
complex is going. Early is where the mispricing lives:

    14 min left  5.47x      9 min  2.27x      3 min  1.58x

This ranks every same-direction combo available right now by expected value and
says which to buy. It does NOT place the order: combo books carry no resting
liquidity - a limit rested alone and expired, and immediate-or-cancel orders
above the product filled 0.00 - so execution happens in the app, which quotes
these directly.

No fees are modelled. A $1 three-leg combo showed a $0.02 app fee, ~2%, which is
inside the edge but not nothing.

DOGE AND BNB ARE UNMEASURED. They are quoted and combinable, but the lift table
was built on the other five, so a combo containing them is priced on an
assumption that crypto behaves alike rather than on their own history. Flagged
in the output rather than hidden.

    python scripts/combo_selector.py
    python scripts/combo_selector.py --dollars 1 --max-legs 4
"""

import argparse
import asyncio
import itertools
import sys
from datetime import datetime, timezone
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from btc15_signal.config import Settings  # noqa: E402

sys.path.insert(0, str(ROOT / "scripts"))
from combo_model import joint  # noqa: E402

# Fitted on 3,788 aligned windows so the model reproduces the observed 48.2%
# all-agree frequency (independence says 6.3%). See combo_model.py.
# CORRECTED 2026-09-26. Was 0.6944, fitted by matching an all-agree
# frequency over rows that counted each window ~11 times. Re-fitted by maximum
# likelihood on the 3,788 INDEPENDENT windows it gives 0.8070.
#
# TREAT ANY SINGLE RHO AS A ROUGH GUIDE, NOT A PRICE. Measured pairwise values
# run 0.2582 (BTC+NEAR) to 0.9700 (SOL+XRP) - a spread of 0.71 - so an
# equicorrelated model is misspecified for a mixed basket and will invent a
# leg-count gradient that is not there. See FINDINGS.md #87.
RHO = 0.8070

SERIES = {
    "BTC": "KXBTC15M", "ETH": "KXETH15M", "SOL": "KXSOL15M",
    "XRP": "KXXRP15M", "NEAR": "KXNEAR15M",
    # Quoted and combinable, but absent from the corpus the lift was measured
    # on. Treated as crypto-like, and marked in the output.
    "DOGE": "KXDOGE15M", "BNB": "KXBNB15M",
    "HYPE": "KXHYPE15M", "ZEC": "KXZEC15M",
}
UNMEASURED = {"DOGE", "BNB", "HYPE", "ZEC"}

def estimate(probs, directions) -> float:
    """True joint probability under the fitted one-factor model.

    THIS REPLACED A FLAT MULTIPLIER, which priced every combo of the same shape
    identically - +690% edge on all of them - so the ranking was really just
    "cheapest first" and it recommended ALL DOWN on a basket holding BNB at
    0.73-up. The correlation says assets move TOGETHER; it never said the
    market's direction call is wrong.
    """
    return joint(probs, directions, RHO)


async def quotes(settings) -> dict:
    out = {}
    now = datetime.now(timezone.utc)
    async with httpx.AsyncClient(timeout=25) as c:
        for name, series in SERIES.items():
            r = await c.get(settings.kalshi_base_url + "/markets",
                            params={"series_ticker": series, "status": "open",
                                    "limit": 1})
            markets = r.json().get("markets", []) if r.status_code == 200 else []
            if not markets:
                continue
            m = markets[0]
            close = datetime.fromisoformat(m["close_time"].replace("Z", "+00:00"))
            out[name] = {
                "ticker": m["ticker"],
                "yes": float(m["yes_ask_dollars"]),
                "no": float(m["no_ask_dollars"]),
                "minutes": (close - now).total_seconds() / 60,
            }
    return out


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--dollars", type=float, default=1.00)
    p.add_argument("--max-legs", type=int, default=5)
    p.add_argument("--min-legs", type=int, default=2)
    p.add_argument("--top", type=int, default=8)
    args = p.parse_args()

    settings = Settings()
    q = asyncio.run(quotes(settings))
    if len(q) < args.min_legs:
        raise SystemExit("not enough open crypto markets right now")

    minutes = min(v["minutes"] for v in q.values())
    print("=" * 74)
    print(f"COMBO SELECTOR   {len(q)} assets quoted, {minutes:.1f} minutes left")
    print("=" * 74)
    for name, v in sorted(q.items()):
        mark = "  (unmeasured)" if name in UNMEASURED else ""
        print(f"  {name:<5} up {v['yes']:.2f}   down {v['no']:.2f}{mark}")

    rows = []
    names = sorted(q)
    for k in range(args.min_legs, min(args.max_legs, len(names)) + 1):
        for combo in itertools.combinations(names, k):
            for direction in ("yes", "no"):
                cost = 1.0
                for a in combo:
                    cost *= q[a][direction]
                if cost <= 0.0005:
                    continue
                probs = [q[a][direction] for a in combo]
                # the model works in "probability it goes UP", so a DOWN leg
                # is 1 - its own ask
                ups = [p if direction == "yes" else 1.0 - p for p in probs]
                est = estimate(ups, [direction == "yes"] * k)
                rows.append({
                    "legs": combo, "dir": direction, "cost": cost,
                    "payout": 1 / cost, "est": est,
                    "edge": est / cost - 1.0,
                    "unmeasured": bool(set(combo) & UNMEASURED),
                })

    rows.sort(key=lambda r: -r["edge"])
    print(f"\n  ranked by expected value, ${args.dollars:.2f} stake")
    print(f"  {'direction':<10}{'legs':<28}{'cost':>7}{'payout':>9}"
          f"{'est win':>9}{'edge':>9}")
    for r in rows[:args.top]:
        label = "+".join(r["legs"]) + ("*" if r["unmeasured"] else "")
        word = "ALL UP" if r["dir"] == "yes" else "ALL DOWN"
        print(f"  {word:<10}{label:<28}{r['cost']:>7.3f}"
              f"{r['payout']:>8.1f}x{r['est']:>9.1%}{r['edge']:>+8.0%}")

    best = rows[0]
    print(f"\n  BUY: {'ALL UP' if best['dir']=='yes' else 'ALL DOWN'} on "
          f"{' + '.join(best['legs'])}")
    print(f"       ${args.dollars:.2f} pays ${args.dollars * best['payout']:.2f} "
          f"if right; the product prices it at {best['cost']:.1%} and the "
          f"measured frequency is about {best['est']:.0%}")
    if best["unmeasured"]:
        print("       * contains DOGE or BNB, which have no corpus here - the "
              "lift is assumed, not measured")
    print("\n  Buy it in the app: pick these markets, same side on every leg.")
    print("  Cash out early only if the value has run; a combo's price moves "
          "roughly as fast as all its legs at once.")


if __name__ == "__main__":
    main()
