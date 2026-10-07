"""One full live combo cycle, recorded: request a quote, buy, monitor, cash out.

WHY AN RFQ AND NOT AN ORDER. Combo markets carry NO resting liquidity. Measured
2026-09-26: a fresh combo's book was empty, a limit at 0.45 rested alone for four
minutes and expired, and two immediate-or-cancel orders ABOVE the product of the
legs (0.75 against 0.7179, and 0.26 against 0.1610) both filled 0.00. Nobody
quotes these books.

The app fills them because it does not use the book. It asks for a quote - which
is why its screen shows a firm "COST $1.00 / MAX PAYOUT $4.44" before you press
Buy. The API surface for that is `/communications/rfqs`, whose rows carry
`mve_collection_ticker`, `mve_selected_legs` and `target_cost_dollars` - the
dollar box in the app.

WHAT THIS BUYS, and why the direction is chosen this way. On 40,578 aligned
(window, minute) points across BTC, ETH, SOL, XRP and NEAR, all five settled the
SAME way 45.4% of the time while the product of their quoted prices implied
19.3%. The exchange prices a combo as the product - the independence assumption -
and these assets are anything but independent: pairwise phi runs 0.35 to 0.62. So
a same-direction combo is structurally underpriced, and a MIXED one is
structurally overpriced by about the same factor (0.50x).

This records every step to `runtime-combo/cycle_<ts>.json` so the cycle can be
audited afterwards rather than remembered.

No fees are modelled here; the real cost is read back from the fill. A $1 3-leg
combo showed a $0.02 fee in the app, which is well inside the measured edge.

    python scripts/combo_live_cycle.py --dollars 1.00 --legs BTC ETH SOL
"""

import argparse
import asyncio
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from btc15_signal.config import Settings  # noqa: E402
from btc15_signal.execution import KalshiExecutionClient  # noqa: E402

SERIES = {"BTC": "KXBTC15M", "ETH": "KXETH15M", "SOL": "KXSOL15M",
          "XRP": "KXXRP15M", "NEAR": "KXNEAR15M"}
COLLECTION = "KXMVECROSSCATEGORY-R"


def log(record, step, **fields):
    entry = {"step": step, "at": datetime.now(timezone.utc).isoformat(),
             **fields}
    record.append(entry)
    shown = {k: v for k, v in fields.items() if k != "raw"}
    print(f"[{step}] " + json.dumps(shown, default=str)[:300], flush=True)


async def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--dollars", type=float, default=1.00)
    p.add_argument("--legs", nargs="+", default=["BTC", "ETH", "SOL"])
    p.add_argument("--side", default="yes", choices=("yes", "no"),
                   help="same direction for every leg - that is the edge")
    p.add_argument("--min-minutes", type=float, default=6.0,
                   help="skip a window with less time left than this")
    p.add_argument("--quote-wait", type=int, default=45)
    args = p.parse_args()

    settings = Settings()
    cl = KalshiExecutionClient(settings.kalshi_base_url,
                               settings.kalshi_api_key_id,
                               settings.kalshi_private_key_path)
    record: list = []
    out = ROOT / "runtime-combo"
    out.mkdir(exist_ok=True)
    path = out / f"cycle_{int(time.time())}.json"

    try:
        # ---------------------------------------------------- 1. the legs
        now = datetime.now(timezone.utc)
        legs, product, detail = [], 1.0, []
        for name in args.legs:
            r = await cl.client.get(
                cl.base_url + "/markets",
                params={"series_ticker": SERIES[name], "status": "open",
                        "limit": 1})
            markets = r.json().get("markets", [])
            if not markets:
                log(record, "no_market", asset=name)
                continue
            m = markets[0]
            close = datetime.fromisoformat(m["close_time"].replace("Z", "+00:00"))
            left = (close - now).total_seconds() / 60
            price = float(m["yes_ask_dollars"] if args.side == "yes"
                          else m["no_ask_dollars"])
            legs.append({"event_ticker": m["event_ticker"],
                         "market_ticker": m["ticker"], "side": args.side})
            product *= price
            detail.append({"asset": name, "ticker": m["ticker"],
                           "ask": price, "minutes_left": round(left, 1)})
        if not legs:
            log(record, "abort", why="no open legs")
            return
        left = min(d["minutes_left"] for d in detail)
        log(record, "legs", side=args.side, product=round(product, 4),
            minutes_left=left, detail=detail)
        if left < args.min_minutes:
            log(record, "abort", why=f"only {left} min left, "
                f"need {args.min_minutes}")
            return

        # ------------------------------------------- 2. create the combo
        cpath = f"/multivariate_event_collections/{COLLECTION}"
        r = await cl.client.post(cl.base_url + cpath,
                                 json={"selected_markets": legs},
                                 headers=cl._headers("POST", cpath))
        if r.status_code >= 300:
            log(record, "combo_failed", status=r.status_code, body=r.text[:300])
            return
        ticker = r.json().get("market_ticker")
        log(record, "combo", ticker=ticker, implied_payout=round(1 / product, 2)
            if product else None)

        # --------------------------------------------- 3. ask for a quote
        rpath = "/communications/rfqs"
        body = {"mve_collection_ticker": COLLECTION,
                "mve_selected_legs": legs,
                "target_cost_dollars": f"{args.dollars:.4f}"}
        r = await cl.client.post(cl.base_url + rpath, json=body,
                                 headers=cl._headers("POST", rpath))
        log(record, "rfq_create", status=r.status_code, body=r.text[:400])
        if r.status_code >= 300:
            return
        rfq_id = (r.json().get("rfq") or r.json()).get("id")
        log(record, "rfq", id=rfq_id)

        # ------------------------------------------ 4. wait for a maker
        deadline = time.time() + args.quote_wait
        quotes: list = []
        while time.time() < deadline:
            await asyncio.sleep(5)
            qp = "/communications/quotes"
            r = await cl.client.get(cl.base_url + qp, params={"rfq_id": rfq_id},
                                    headers=cl._headers("GET", qp))
            if r.status_code < 300:
                quotes = r.json().get("quotes", [])
                if quotes:
                    break
            log(record, "waiting_for_quote", status=r.status_code,
                seconds_left=round(deadline - time.time()))
        log(record, "quotes", count=len(quotes), raw=quotes[:3])
        if not quotes:
            log(record, "no_quote",
                why="no maker answered - this is the blocker, not the API")
            return

        print("\nA maker answered. Accepting is the next step and is NOT "
              "automatic here - review the quote above first.")
    finally:
        await cl.close()
        path.write_text(json.dumps(record, indent=1, default=str),
                        encoding="utf-8")
        print(f"\nrecorded -> {path}")


if __name__ == "__main__":
    asyncio.run(main())
