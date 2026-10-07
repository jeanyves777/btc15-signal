"""What does Kalshi actually CHARGE for a combo? Same direction vs one opposite.

THE PREMISE THAT TURNED OUT TO BE FALSE. Every earlier script here assumed the
exchange prices a combo as the PRODUCT of its legs - the independence
assumption - and that a same-direction combo is therefore structurally
underpriced, because correlated assets agree far more often than the product
implies. Read from the operator's own app screens on 2026-09-26:

    legs shown            product   copula   APP price   app/product   app/copula
    0.63 0.62 0.74 up     0.2890    0.4732    0.5426        1.88x         1.15x
    0.60 0.55 0.68 up     0.2244    0.4132    0.5435        2.42x         1.32x
    0.61 0.62 0.82 up     0.3101    0.4798    0.5435        1.75x         1.13x

The app charges 1.75-2.42x the product. Kalshi ALREADY prices the correlation,
and adds 13-32% on top. There is no independence mispricing to harvest there.

BUT THE ORDERBOOK IS A DIFFERENT VENUE. A resting bid on the combo's own book
filled at 0.0500 where the product was 0.0456 and the copula said 0.1219 - 41%
of fair value. Same instrument, opposite side of the edge. So WHERE you buy
decides whether the combo is cheap or dear, and that is the finding.

WHAT THIS MEASURES. For one live window it prices the same three legs four ways
- all-up, all-down, and the two one-opposite variants the operator asked about -
against the product and the fitted copula, and asks the exchange for a real
quote on each. Leverage comes from a low joint probability, and an opposite leg
is the cheapest way to buy one; the question is what the exchange charges for it.

No fees are modelled, per the operator's standing instruction.

    python scripts/measure_combo_markup.py --legs BTC ETH ZEC
"""

import argparse
import asyncio
import itertools
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from btc15_signal.config import Settings  # noqa: E402
from btc15_signal.execution import KalshiExecutionClient  # noqa: E402
from combo_model import joint  # noqa: E402

SERIES = {"BTC": "KXBTC15M", "ETH": "KXETH15M", "SOL": "KXSOL15M",
          "XRP": "KXXRP15M", "NEAR": "KXNEAR15M", "DOGE": "KXDOGE15M",
          "BNB": "KXBNB15M", "HYPE": "KXHYPE15M", "ZEC": "KXZEC15M"}
COLLECTION = "KXMVECROSSCATEGORY-R"
# CORRECTED 2026-09-26. Was 0.6944, fitted by matching an all-agree
# frequency over rows that counted each window ~11 times. Re-fitted by maximum
# likelihood on the 3,788 INDEPENDENT windows it gives 0.8070.
#
# TREAT ANY SINGLE RHO AS A ROUGH GUIDE, NOT A PRICE. Measured pairwise values
# run 0.2582 (BTC+NEAR) to 0.9700 (SOL+XRP) - a spread of 0.71 - so an
# equicorrelated model is misspecified for a mixed basket and will invent a
# leg-count gradient that is not there. See FINDINGS.md #87.
RHO = 0.8070


async def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--legs", nargs="+", default=["BTC", "ETH", "ZEC"])
    p.add_argument("--dollars", type=float, default=1.00)
    p.add_argument("--quote-wait", type=int, default=25)
    args = p.parse_args()

    s = Settings()
    cl = KalshiExecutionClient(s.kalshi_base_url, s.kalshi_api_key_id,
                               s.kalshi_private_key_path)
    record = {"at": datetime.now(timezone.utc).isoformat(), "rows": []}
    try:
        path = "/portfolio/orders"
        r = await cl.client.get(cl.base_url + path, params={"limit": 1},
                                headers=cl._headers("GET", path))
        uid = (r.json().get("orders") or [{}])[0].get("user_id")

        mk = {}
        for name in args.legs:
            r = await cl.client.get(cl.base_url + "/markets",
                                    params={"series_ticker": SERIES[name],
                                            "status": "open", "limit": 1})
            ms = r.json().get("markets", [])
            if ms and ms[0].get("floor_strike") is not None:
                mk[name] = ms[0]
        names = [n for n in args.legs if n in mk]
        if len(names) < 3:
            print("need three open legs")
            return
        close = datetime.fromisoformat(
            mk[names[0]]["close_time"].replace("Z", "+00:00"))
        left = (close - datetime.now(timezone.utc)).total_seconds() / 60
        print("=" * 78)
        print(f"COMBO MARKUP   {'+'.join(names)}   {left:.1f} min left")
        print("=" * 78)
        for n in names:
            print(f"  {n:<5} up {float(mk[n]['yes_ask_dollars']):.2f}   "
                  f"down {float(mk[n]['no_ask_dollars']):.2f}")

        # all-up, all-down, and every one-opposite variant
        shapes = []
        for ups in itertools.product([True, False], repeat=len(names)):
            n_up = sum(ups)
            if n_up in (0, len(names)):
                label = "ALL UP" if n_up else "ALL DOWN"
            elif n_up in (1, len(names) - 1):
                # Both cases are "one leg against the other two", but they are
                # DIFFERENT shapes - say which way the odd leg points, or the
                # table prints one label for two distinct trades.
                if n_up == 1:
                    label = f"one opposite ({names[ups.index(True)]} up)"
                else:
                    label = f"one opposite ({names[ups.index(False)]} down)"
            else:
                continue
            shapes.append((label, ups))

        print(f"\n  {'shape':<24}{'product':>9}{'copula':>9}{'payout':>9}"
              f"{'quoted':>9}{'q/copula':>10}")
        for label, ups in shapes:
            legs, product = [], 1.0
            for n, up in zip(names, ups):
                m = mk[n]
                side = "yes" if up else "no"
                product *= float(m["yes_ask_dollars"] if up
                                 else m["no_ask_dollars"])
                legs.append({"event_ticker": m["event_ticker"],
                             "market_ticker": m["ticker"], "side": side})
            probs = [float(mk[n]["yes_ask_dollars"]) for n in names]
            cop = joint(probs, list(ups), RHO)

            cpath = f"/multivariate_event_collections/{COLLECTION}"
            r = await cl.client.post(cl.base_url + cpath,
                                     json={"selected_markets": legs},
                                     headers=cl._headers("POST", cpath))
            ticker = r.json().get("market_ticker") if r.status_code < 300 else None
            quoted = None
            if ticker:
                rpath = "/communications/rfqs"
                body = {"mve_collection_ticker": COLLECTION,
                        "mve_selected_legs": legs, "market_ticker": ticker,
                        "target_cost_dollars": f"{args.dollars:.4f}"}
                rr = await cl.client.post(cl.base_url + rpath, json=body,
                                          headers=cl._headers("POST", rpath))
                rfq_id = rr.json().get("id") if rr.status_code < 300 else None
                if rfq_id and uid:
                    deadline = time.time() + args.quote_wait
                    qp = "/communications/quotes"
                    while time.time() < deadline and quoted is None:
                        await asyncio.sleep(2)
                        q = await cl.client.get(
                            cl.base_url + qp,
                            params={"rfq_creator_user_id": uid, "limit": 100},
                            headers=cl._headers("GET", qp))
                        if q.status_code >= 300:
                            continue
                        for row in q.json().get("quotes", []):
                            if row.get("rfq_id") != rfq_id:
                                continue
                            # BUYING costs 1 - the maker's no_bid. Their
                            # yes_bid is what they would PAY US, not our
                            # cost. Confirmed against a real app fill:
                            # no_bid 0.4750 -> 0.5250, filled at 0.527.
                            nb = float(row.get("no_bid_dollars") or 0)
                            if nb > 0.0011:
                                quoted = 1.0 - nb
                                break
            row = {"shape": label, "product": round(product, 4),
                   "copula": round(cop, 4),
                   "payout_x": round(1 / product, 1) if product else None,
                   "quoted": quoted, "ticker": ticker}
            record["rows"].append(row)
            qs = f"{quoted:.4f}" if quoted else "   -"
            qr = f"{quoted / cop:.2f}x" if quoted and cop else "     -"
            print(f"  {label:<24}{product:>9.4f}{cop:>9.4f}"
                  f"{1 / product if product else 0:>8.1f}x{qs:>9}{qr:>10}")

        print("\n  payout is 1/product - the leverage. An opposite leg drops the")
        print("  joint probability, which is where the big multiple comes from.")
        print("  copula is what the legs are jointly worth at the fitted rho.")
        print("  A quote ABOVE copula is the exchange's margin, and it is the")
        print("  reason the app path loses: measured 1.13-1.32x fair value.")
    finally:
        await cl.close()
        out = ROOT / "runtime-combo"
        out.mkdir(exist_ok=True)
        f = out / f"markup_{int(time.time())}.json"
        f.write_text(json.dumps(record, indent=1, default=str), encoding="utf-8")
        print(f"\nrecorded -> {f}")


if __name__ == "__main__":
    asyncio.run(main())
