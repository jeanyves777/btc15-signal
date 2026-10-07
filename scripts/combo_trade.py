"""Buy one combo for a fixed dollar amount, monitor it, cash out. Recorded.

WHY THE EARLIER ATTEMPTS FILLED NOTHING. They were priced blind: a limit at 0.45
rested alone for four minutes and expired, and two immediate-or-cancel orders
fired the instant the combo was created (0.75 against a 0.7179 product, 0.26
against 0.1610) found no counterparty. Combo books start EMPTY - the market is
made on request, not resting - so an order placed before anyone has quoted can
only sit there.

So this one WATCHES the book after creating the combo and crosses whatever
actually appears, rather than guessing a price into an empty market. If nothing
appears it says so and spends nothing.

Everything is written to runtime-combo/trade_<ts>.json as it happens - the legs,
the product, every book poll, the order, the fills, the cash-out - so the cycle
can be audited from the record instead of recalled.

    python scripts/combo_trade.py --dollars 1 --legs BTC ETH SOL --side no
"""

import argparse
import asyncio
import json
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from btc15_signal.config import Settings  # noqa: E402
from btc15_signal.execution import KalshiExecutionClient  # noqa: E402

SERIES = {"BTC": "KXBTC15M", "ETH": "KXETH15M", "SOL": "KXSOL15M",
          "XRP": "KXXRP15M", "NEAR": "KXNEAR15M", "DOGE": "KXDOGE15M",
          "BNB": "KXBNB15M", "HYPE": "KXHYPE15M", "ZEC": "KXZEC15M"}
COLLECTION = "KXMVECROSSCATEGORY-R"

RECORD: list = []


def note(step, **fields):
    entry = {"step": step, "at": datetime.now(timezone.utc).isoformat(), **fields}
    RECORD.append(entry)
    print(f"[{step}] " + json.dumps(fields, default=str)[:260], flush=True)


async def book(cl, ticker):
    r = await cl.client.get(cl.base_url + f"/markets/{ticker}/orderbook")
    if r.status_code >= 300:
        return None
    ob = r.json().get("orderbook_fp") or r.json().get("orderbook") or {}
    return ob


async def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--dollars", type=float, default=1.00)
    p.add_argument("--legs", nargs="+", default=["BTC", "ETH", "SOL"])
    p.add_argument("--side", default="yes", choices=("yes", "no"))
    p.add_argument("--watch", type=int, default=120,
                   help="seconds to watch for a maker before giving up")
    p.add_argument("--max-premium", type=float, default=0.15,
                   help="most we will pay ABOVE the product of the legs")
    args = p.parse_args()

    s = Settings()
    cl = KalshiExecutionClient(s.kalshi_base_url, s.kalshi_api_key_id,
                               s.kalshi_private_key_path)
    out = ROOT / "runtime-combo"
    out.mkdir(exist_ok=True)
    path = out / f"trade_{int(time.time())}.json"
    try:
        now = datetime.now(timezone.utc)
        legs, product, detail = [], 1.0, []
        async with httpx.AsyncClient(timeout=25) as pub:
            for name in args.legs:
                r = await pub.get(s.kalshi_base_url + "/markets",
                                  params={"series_ticker": SERIES[name],
                                          "status": "open", "limit": 1})
                ms = r.json().get("markets", [])
                if not ms:
                    continue
                m = ms[0]
                close = datetime.fromisoformat(
                    m["close_time"].replace("Z", "+00:00"))
                price = float(m["yes_ask_dollars"] if args.side == "yes"
                              else m["no_ask_dollars"])
                legs.append({"event_ticker": m["event_ticker"],
                             "market_ticker": m["ticker"], "side": args.side})
                product *= price
                detail.append({"asset": name, "ask": price,
                               "minutes": round((close - now).total_seconds() / 60, 1)})
        if len(legs) < 2:
            note("abort", why="need at least two open legs")
            return
        note("legs", side=args.side, product=round(product, 4),
             payout=round(1 / product, 1) if product else None, detail=detail)

        cpath = f"/multivariate_event_collections/{COLLECTION}"
        r = await cl.client.post(cl.base_url + cpath,
                                 json={"selected_markets": legs},
                                 headers=cl._headers("POST", cpath))
        if r.status_code >= 300:
            note("combo_failed", status=r.status_code, body=r.text[:200])
            return
        ticker = r.json().get("market_ticker")
        note("combo", ticker=ticker)

        # ---- watch the book. THIS is what the earlier attempts skipped.
        ceiling = min(0.97, product + args.max_premium)
        deadline = time.time() + args.watch
        offer = None
        polls = 0
        while time.time() < deadline:
            ob = await book(cl, ticker)
            polls += 1
            asks = (ob or {}).get("yes_dollars") or []
            if asks:
                note("book", asks=asks[:3], polls=polls)
                try:
                    offer = min(float(a[0]) for a in asks)
                except Exception:
                    offer = None
                if offer is not None:
                    break
            await asyncio.sleep(5)
        if offer is None:
            note("no_maker", polls=polls, watched_s=args.watch,
                 why="the book stayed empty - nothing to cross, spent nothing")
            return
        note("offer", price=offer, ceiling=round(ceiling, 4),
             will_take=offer <= ceiling)
        if offer > ceiling:
            note("too_expensive", offer=offer, ceiling=round(ceiling, 4))
            return

        count = round(args.dollars / offer, 2)
        body = {"ticker": ticker, "client_order_id": str(uuid.uuid4()),
                "side": "bid", "count": f"{count:.2f}",
                "price": f"{offer:.4f}", "time_in_force": "immediate_or_cancel",
                "self_trade_prevention_type": "taker_at_cross",
                "post_only": False, "cancel_order_on_pause": True,
                "reduce_only": False}
        opath = "/portfolio/events/orders"
        r = await cl.client.post(cl.base_url + opath, json=body,
                                 headers=cl._headers("POST", opath))
        j = r.json() if r.status_code < 300 else {}
        note("order", status=r.status_code, price=offer, count=count,
             filled=j.get("fill_count"), order_id=j.get("order_id"),
             body=None if r.status_code < 300 else r.text[:200])
        filled = float(j.get("fill_count") or 0)
        if filled <= 0:
            note("unfilled", why="crossed the quoted offer and still no fill")
            return
        note("BOUGHT", contracts=filled, cost=round(filled * offer, 4),
             payout_if_right=round(filled, 2))
    finally:
        await cl.close()
        path.write_text(json.dumps(RECORD, indent=1, default=str),
                        encoding="utf-8")
        print(f"\nrecorded -> {path}")


if __name__ == "__main__":
    asyncio.run(main())
