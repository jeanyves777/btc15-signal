"""The supported combo path: request a quote, price it, accept, verify the fill.

WHY AN RFQ AND NOT A BOOK ORDER. Combo books carry no resting liquidity. Measured
2026-09-26 on a fresh combo: the orderbook was literally
`{"yes_dollars":[],"no_dollars":[]}`, bid 0.00 / ask 1.00. An immediate-or-cancel
order needs a resting counterparty and finds none. The market is MADE ON REQUEST.

Two things work, and they are different trades:

  * resting a bid AT FAIR VALUE and waiting for a maker to take it. Filled $1.00
    of a BTC+SOL-down combo in 60 seconds, `is_taker: false`. Cheap, but you only
    get filled when someone wants the other side.
  * asking for a quote, which is what the app does. Supported, firm, immediate -
    and mostly ignored, see below.

WHAT 415 RFQs ON THIS ACCOUNT ACTUALLY SHOW. Only 10 were ever quoted - 2.4%.
The shape of the ask decides it:

    sized by contracts 4.44    3/5   60% quoted
    sized by contracts 1.84    3/6   50%
    sized by contracts 1111    0/37   0%
    target_cost_dollars $2     0/5    0%
    target_cost_dollars $5     0/1    0%
    target_cost_dollars $11    0/2    0%

    2 legs   0/137   0%        3 legs   8/157   5%        4-9 legs   0/30   0%

So this asks by CONTRACT COUNT, small, on THREE legs. A `target_cost_dollars` RFQ
- the dollar box in the app - has never once been quoted here, and neither has a
two-leg combo. That is measured, not assumed, and it is the opposite of what the
app screen suggests.

ACCEPTANCE IS NOT A FILL. A quote carries `accepted_ts` and then `confirmed_ts`;
the maker has ~3 seconds to confirm. Of 8 accepted quotes on this account, 6
confirmed and 2 went to `status: cancelled` - 75% follow-through. So this waits
for `executed` and then reads `/portfolio/fills`, and reports nothing as bought
until the broker says so.

PRICE DISCIPLINE. A quote is refused unless it beats the fitted one-factor fair
value by `--min-edge`. The maker chooses the price; without this check an RFQ is
a blank cheque. Fair value comes from combo_model.joint at the measured rho.

LIVE RESULT SO FAR: 0 for 6. Five executed RFQ combos on this account and one
resting-bid combo of mine have settled or closed for $0 against ~$4.67 staked.
The corpus edge is real (five assets settle alike 45.4% where the product implies
19.3%) but it has not shown up in live money yet. Sample of six.

No fees are modelled, per the operator's standing instruction; the real fee is
read back from the fill.

    python scripts/combo_rfq.py --contracts 4.44 --legs BTC ETH SOL --side no
    python scripts/combo_rfq.py --contracts 4.44 --accept        # may spend money
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

# Measured above: the only leg count makers answer, and the sizes they answer.
QUOTED_LEG_COUNT = 3


class Cycle:
    """Every step, written to disk as it happens so it can be audited later."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.steps: list = []

    def log(self, step: str, **fields):
        entry = {"step": step, "at": datetime.now(timezone.utc).isoformat(),
                 **fields}
        self.steps.append(entry)
        shown = {k: v for k, v in fields.items() if k != "raw"}
        print(f"[{step}] " + json.dumps(shown, default=str)[:280], flush=True)
        self.path.write_text(json.dumps(self.steps, indent=1, default=str),
                             encoding="utf-8")
        return entry


async def my_user_id(cl) -> str | None:
    """The quotes endpoint needs it; an order carries it."""
    path = "/portfolio/orders"
    r = await cl.client.get(cl.base_url + path, params={"limit": 1},
                            headers=cl._headers("GET", path))
    if r.status_code >= 300:
        return None
    orders = r.json().get("orders") or []
    return orders[0].get("user_id") if orders else None


async def legs_now(cl, settings, names, side):
    """Open 15-minute markets for each asset, same side on every leg."""
    now = datetime.now(timezone.utc)
    legs, product, detail = [], 1.0, []
    for name in names:
        r = await cl.client.get(cl.base_url + "/markets",
                                params={"series_ticker": SERIES[name],
                                        "status": "open", "limit": 1})
        markets = r.json().get("markets", []) if r.status_code < 300 else []
        if not markets:
            continue
        m = markets[0]
        if m.get("floor_strike") is None:
            continue
        close = datetime.fromisoformat(m["close_time"].replace("Z", "+00:00"))
        price = float(m["yes_ask_dollars"] if side == "yes"
                      else m["no_ask_dollars"])
        legs.append({"event_ticker": m["event_ticker"],
                     "market_ticker": m["ticker"], "side": side})
        product *= price
        detail.append({"asset": name, "ticker": m["ticker"], "ask": price,
                       "minutes_left": round((close - now).total_seconds() / 60, 1)})
    return legs, product, detail


def fair_value(detail, side) -> float:
    """What the legs are jointly worth under the fitted common factor.

    The exchange prices a combo as the PRODUCT of its legs - independence. These
    assets are not independent, so the joint probability is higher than the
    product for a same-direction combo. That gap is the whole trade.
    """
    ups = [d["ask"] if side == "yes" else 1.0 - d["ask"] for d in detail]
    return joint(ups, [side == "yes"] * len(ups), RHO)


async def wait_for_quote(cl, cycle, uid, rfq_id, seconds):
    """Poll until a maker answers this RFQ, or give up.

    The documented transport is the authenticated communications WebSocket. REST
    polling reaches the same rows and needs no extra dependency in a live money
    process; the cost is latency, which matters little against a 2.4% quote rate.
    """
    path = "/communications/quotes"
    deadline = time.time() + seconds
    polls = 0
    while time.time() < deadline:
        r = await cl.client.get(cl.base_url + path,
                                params={"rfq_creator_user_id": uid, "limit": 100},
                                headers=cl._headers("GET", path))
        polls += 1
        if r.status_code < 300:
            mine = [q for q in r.json().get("quotes", [])
                    if q.get("rfq_id") == rfq_id]
            if mine:
                cycle.log("quoted", count=len(mine), polls=polls, raw=mine)
                return mine
        await asyncio.sleep(2)
    cycle.log("no_quote", polls=polls, waited_s=seconds,
              why="no maker answered - 2.4% of RFQs on this account ever are")
    return []


def pick(quotes, side, fair, min_edge, cycle):
    """Cheapest quote we can BUY at that beats fair value by min_edge.

    WHAT A QUOTE'S NUMBERS MEAN, and the mistake worth not repeating. A quote
    carries `yes_bid_dollars` and `no_bid_dollars`: they are the maker's BIDS -
    what they would pay US. Buying the combo costs `1 - no_bid`, because buying
    yes is selling no. Reading `yes_bid` as the purchase price made every shape
    look like it cost 1.5-2.2 cents.

    Checked against a real app fill on this account: the executed quote showed
    no_bid 0.4750, so 0.5250 a contract, and 1.84 contracts filled for $0.97 -
    0.527 each. That is the arithmetic, from the broker.
    """
    best = None
    for q in quotes:
        try:
            other = float(q.get("no_bid_dollars") or 0)
        except (TypeError, ValueError):
            continue
        # 0.0010 is the placeholder for "not quoting that side".
        if other <= 0.0011:
            continue
        price = 1.0 - other
        if price <= 0.0 or price >= 1.0:
            continue
        edge = fair / price - 1.0 if price else 0.0
        cycle.log("candidate", quote_id=q.get("id"), price=price,
                  fair=round(fair, 4), edge=round(edge, 4),
                  good=edge >= min_edge)
        if edge >= min_edge and (best is None or price < best[0]):
            best = (price, q, edge)
    return best


async def accept(cl, cycle, quote_id, side):
    """PUT the accept, then wait for the maker's confirmation.

    Acceptance alone is not a fill: the maker has ~3 seconds to confirm, then a
    1 second execution timer. Measured follow-through on this account is 6 of 8.
    """
    path = f"/communications/quotes/{quote_id}/accept"
    body = {"accepted_side": side}
    r = await cl.client.request("PUT", cl.base_url + path, json=body,
                                headers=cl._headers("PUT", path))
    cycle.log("accept", status=r.status_code, body=r.text[:300])
    if r.status_code >= 300:
        return False
    qpath = "/communications/quotes"
    for _ in range(8):
        await asyncio.sleep(1)
        rr = await cl.client.get(cl.base_url + qpath + f"/{quote_id}",
                                 headers=cl._headers("GET", qpath + f"/{quote_id}"))
        q = (rr.json().get("quote") or {}) if rr.status_code < 300 else {}
        status = q.get("status")
        cycle.log("confirm_wait", status=status,
                  confirmed=bool(q.get("confirmed_ts")))
        if status == "executed":
            return True
        if status in ("cancelled", "expired"):
            cycle.log("maker_did_not_confirm", status=status,
                      why="acceptance is not a fill - this is the 2-in-8 case")
            return False
    return False


async def verify(cl, cycle, ticker):
    """The broker is the only source of truth about money."""
    out = {}
    for label, path in (("fills", "/portfolio/fills"),
                        ("positions", "/portfolio/positions")):
        r = await cl.client.get(cl.base_url + path, params={"ticker": ticker},
                                headers=cl._headers("GET", path))
        out[label] = r.json() if r.status_code < 300 else {"status": r.status_code}
    fills = out["fills"].get("fills", [])
    spent = sum(float(f.get("yes_price_dollars") or 0)
                * float(f.get("count_fp") or 0) for f in fills)
    fees = sum(float(f.get("fee_cost") or 0) for f in fills)
    cycle.log("BROKER", fills=len(fills), spent=round(spent, 4),
              fees=round(fees, 4), raw=out)
    return fills


async def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--contracts", type=float, default=4.44,
                   help="ask by CONTRACT COUNT - dollar targets are never quoted")
    p.add_argument("--legs", nargs="+", default=["BTC", "ETH", "SOL"])
    p.add_argument("--side", default="no", choices=("yes", "no"),
                   help="same direction on every leg - that is the edge")
    p.add_argument("--min-minutes", type=float, default=5.0)
    p.add_argument("--quote-wait", type=int, default=40)
    p.add_argument("--min-edge", type=float, default=0.10,
                   help="refuse a quote unless it beats fair value by this")
    p.add_argument("--accept", action="store_true",
                   help="actually accept a good quote. SPENDS REAL MONEY.")
    args = p.parse_args()

    settings = Settings()
    cl = KalshiExecutionClient(settings.kalshi_base_url,
                               settings.kalshi_api_key_id,
                               settings.kalshi_private_key_path)
    out = ROOT / "runtime-combo"
    out.mkdir(exist_ok=True)
    cycle = Cycle(out / f"rfq_{int(time.time())}.json")
    try:
        uid = await my_user_id(cl)
        if not uid:
            cycle.log("abort", why="cannot read own user_id, quotes need it")
            return
        cycle.log("account", user_id=uid, dry_run=not args.accept)

        legs, product, detail = await legs_now(cl, settings, args.legs, args.side)
        if len(legs) < 2:
            cycle.log("abort", why="need at least two open legs")
            return
        left = min(d["minutes_left"] for d in detail)
        fair = fair_value(detail, args.side)
        cycle.log("legs", side=args.side, product=round(product, 4),
                  fair_value=round(fair, 4),
                  model_lift=round(fair / product, 2) if product else None,
                  minutes_left=left, detail=detail)
        if left < args.min_minutes:
            cycle.log("abort", why=f"only {left} min left, need {args.min_minutes}")
            return
        if len(legs) != QUOTED_LEG_COUNT:
            cycle.log("warn_leg_count", legs=len(legs),
                      why=f"{QUOTED_LEG_COUNT} legs is the only count makers have "
                          f"answered here; 2-leg RFQs are 0 for 137")

        cpath = f"/multivariate_event_collections/{COLLECTION}"
        r = await cl.client.post(cl.base_url + cpath,
                                 json={"selected_markets": legs},
                                 headers=cl._headers("POST", cpath))
        if r.status_code >= 300:
            cycle.log("combo_failed", status=r.status_code, body=r.text[:300])
            return
        ticker = r.json().get("market_ticker")
        cycle.log("combo", ticker=ticker,
                  payout_x=round(1 / product, 1) if product else None)

        # THE MARKET TICKER IS REQUIRED and was what made this 404 for an hour.
        # The combo market must be created first; the RFQ then points at it.
        # Sizing must sit at the TOP level - nested inside an "rfq" wrapper the
        # server replies "Either contracts/contracts_fp or target_cost_dollars
        # must be provided" while ignoring the one you sent.
        rpath = "/communications/rfqs"
        body = {"mve_collection_ticker": COLLECTION,
                "mve_selected_legs": legs,
                "market_ticker": ticker,
                "contracts_fp": f"{args.contracts:.2f}"}
        r = await cl.client.post(cl.base_url + rpath, json=body,
                                 headers=cl._headers("POST", rpath))
        if r.status_code >= 300:
            cycle.log("rfq_failed", status=r.status_code, body=r.text[:300])
            return
        rfq = r.json().get("rfq") or r.json()
        rfq_id = rfq.get("id")
        cycle.log("rfq", id=rfq_id, contracts=args.contracts,
                  cost_if_filled=round(args.contracts * product, 4))

        quotes = await wait_for_quote(cl, cycle, uid, rfq_id, args.quote_wait)
        if not quotes:
            return
        best = pick(quotes, args.side, fair, args.min_edge, cycle)
        if not best:
            cycle.log("all_too_expensive", fair=round(fair, 4),
                      min_edge=args.min_edge,
                      why="the maker sets the price; refusing is the discipline")
            return
        price, quote, edge = best
        cycle.log("CHOSEN", quote_id=quote.get("id"), price=price,
                  fair=round(fair, 4), edge=round(edge, 4),
                  cost=round(price * args.contracts, 4))
        if not args.accept:
            cycle.log("dry_run", why="pass --accept to spend money on this")
            return
        if await accept(cl, cycle, quote.get("id"), args.side):
            await verify(cl, cycle, ticker)
        else:
            cycle.log("not_bought", why="no confirmed execution - spent nothing")
    finally:
        await cl.close()
        print(f"\nrecorded -> {cycle.path}")


if __name__ == "__main__":
    asyncio.run(main())
