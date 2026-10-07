"""The recovery trade, placed as a two-leg combo at BASE size.

OPERATOR'S DECISION, 2026-09-27: "replace the single recover into a Combo with
same base size, no more up scaling ... everything is kept just as design."

WHAT IS KEPT from the loss step, unchanged: it is armed by a losing bot market,
waits up to 5 settled markets for this instrument's own entry to come up with
its ask in 0.70-0.79, fires once per losing episode, and a loss on the
recovery trade itself arms nothing (FINDINGS 104). `main.loss_step_size`
still decides all of that.

WHAT CHANGES: the recovery entry is no longer the same market bought 2x base.
It is a combo of that entry plus a PARTNER - SOL for BTC and ETH, BTC for SOL -
taken on whatever side the partner's own signal showed at that instant,
qualified or not, but only when that side was priced 0.70-0.85 and the reading
is fresh (no later than the trigger, no older than 120s). Exactly the rule the
shadow ran (scripts/shadow_combo_recovery.py, FINDINGS 98/100).

SIZE: the base count - 2 contracts at base 2. Nothing is upsized, ever.

PRICE, VALIDATED BEFORE BUYING. Combos are made on request: an RFQ is sent and
makers quote. The cheapest quote is accepted if it is at or below the CHEAPER
LEG's ask (x `combo_max_price_ratio`, 1.00): a combo pays only if both legs
win, so it can never be worth more than its cheaper leg, and a quote above
that is refused whatever the direction. Same OR opposite direction is accepted
(operator, 2026-09-27). The first cut capped at the legs' PRODUCT - what the
shadow assumed - and that refuses nearly every same-direction pair, because
Kalshi prices the correlation in: live at 18:47 BTC-DOWN 0.64 + SOL-DOWN 0.54
(product 0.3456) was quoted 21 times, cheapest 0.428; the app at 18:52 sold
BTC 76 + SOL 88 at 0.719 against a product of 0.669.

ONE DROPPED CHECK, said out loud. The shadow also required the combo's win to
cover the whole loss ("net zero"). At base size that can never pass - 2
contracts at ~0.56 win ~0.88 against a 2-contract loss of ~1.60 - so keeping
it would mean the combo never fires. Base size was the operator's explicit
constraint, so the check goes.

WHEN THERE IS NO COMBO - no partner in band, no quote, a quote above the
product, a maker who does not confirm - the entry goes out exactly as it would
have: single-leg, BASE size. When the outcome is UNKNOWN (an accept whose
answer was lost), nothing else is sent: a second order on top of a combo that
may exist is how exposure doubles.

THE EXCHANGE PATH, as measured on this account (memory: btc15-combo-rfq-api):
POST the legs to the collection -> market_ticker; POST an RFQ sized in
contracts at the TOP level; poll quotes by our own user id; the maker's
`no_bid` is what they pay for NO, so BUYING the combo costs `1 - no_bid`, and
it is bought by accepting side "no" - every combo executed on this account
(13 accepted quotes, mixed-direction legs included) was bought that way;
acceptance is not a fill - wait for `executed`, then read the fill.
"""

from __future__ import annotations

import asyncio
import json
import math
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx

COLLECTION = "KXMVECROSSCATEGORY-R"
# Who partners whom. Operator: "SOL is the only one that combines with both",
# "SOL only combine with BTC".
#
# BTC HAS NO PARTNER SINCE 2026-09-28. Operator: "BTC AND GOLD ONLY I SAID" -
# only BTC and gold trade live, and a BTC combo's SOL leg was SOL exposure by
# another route. BTC's recovery is a single entry at base size; gold never had
# a partner (FINDINGS 108). ETH and SOL keep theirs, but both are in shadow.
PARTNERS = {"ETH": ("SOL",), "SOL": ("BTC",)}
# Each instance's own store, beside the running instance's database.
STORES = {"BTC": "btc15.db", "ETH": "eth15.db", "SOL": "sol15.db"}
# A quote side priced at this or below is the maker's "not quoting" placeholder.
NOT_QUOTING = 0.0011


async def _sleep(seconds: float) -> None:
    """Module-level so tests can make the waits instant."""
    await asyncio.sleep(seconds)


@dataclass(frozen=True)
class Leg:
    asset: str
    ticker: str
    side: str            # "UP" | "DOWN"
    ask: float           # the price of THIS side, when it was read
    decided_ms: int = 0

    @property
    def event_ticker(self) -> str:
        # KXBTC15M-26SEP271830-30 -> KXBTC15M-26SEP271830
        return self.ticker.rsplit("-", 1)[0]

    def selected(self) -> dict:
        return {"event_ticker": self.event_ticker, "market_ticker": self.ticker,
                "side": "yes" if self.side == "UP" else "no"}


@dataclass
class ComboResult:
    # bought   - a confirmed fill; the combo IS the recovery trade
    # none     - nothing was bought (no quote, too dear, maker did not confirm)
    # unknown  - money may have moved; do NOT send anything else this window
    outcome: str
    reason: str
    market_ticker: str = ""
    price: float = 0.0
    filled: float = 0.0
    fee: float = 0.0
    quote_id: str = ""
    rfq_id: str = ""
    product: float = 0.0
    limit: float = 0.0          # the most it would pay: the cheaper leg
    quotes_seen: list = field(default_factory=list)

    @property
    def bought(self) -> bool:
        return self.outcome == "bought"


def partner_now(root: Path, instrument: str, window_open: int, now_ms: int,
                band: tuple[float, float] = (0.70, 0.85),
                stale_s: float = 120.0) -> Leg | None:
    """The partner leg at this instant, or None. Never raises.

    Read from the partner instance's own `intelligence_decisions`, READ-ONLY:
    the latest reading for THIS window that is no later than now and no older
    than `stale_s`, on whatever side its signal showed, priced inside `band`.
    With more than one partner, the dearest in-band one - the strongest signal.
    """
    best = None
    for asset in PARTNERS.get(instrument, ()):
        path = Path(root) / STORES[asset]
        if not path.exists():
            continue
        try:
            con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2)
            try:
                row = con.execute(
                    "SELECT decided_ms, ticker, side, ask FROM intelligence_decisions "
                    " WHERE window_open = ? AND decided_ms <= ? AND ask IS NOT NULL "
                    " ORDER BY decided_ms DESC LIMIT 1",
                    (int(window_open), int(now_ms)),
                ).fetchone()
            finally:
                con.close()
        except sqlite3.Error:
            continue
        if not row:
            continue
        decided, ticker, side, ask = int(row[0]), row[1], row[2], float(row[3])
        if (now_ms - decided) / 1000.0 > stale_s:
            continue
        if not band[0] <= ask <= band[1] or side not in ("UP", "DOWN") or not ticker:
            continue
        leg = Leg(asset, ticker, side, ask, decided)
        if best is None or leg.ask > best.ask:
            best = leg
    return best


async def refresh(client, leg: Leg, band: tuple[float, float]) -> Leg | None:
    """The partner's price NOW, from its own market - the reading it was chosen
    on can be two minutes old, and the price cap is built from it. None if it
    has left the band or cannot be read. Never raises."""
    path = f"/markets/{leg.ticker}"
    try:
        r = await client.client.get(client.base_url + path,
                                    headers=client._headers("GET", path))
        if r.status_code >= 300:
            return None
        m = r.json().get("market") or {}
        ask = float(m.get("yes_ask_dollars" if leg.side == "UP"
                          else "no_ask_dollars") or 0)
    except (httpx.HTTPError, OSError, ValueError, TypeError):
        return None
    if not band[0] <= ask <= band[1]:
        return None
    return Leg(leg.asset, leg.ticker, leg.side, ask, leg.decided_ms)


def quote_price(quote: dict) -> float | None:
    """What BUYING the combo costs on this quote: 1 - the maker's NO bid."""
    try:
        no_bid = float(quote.get("no_bid_dollars") or 0)
    except (TypeError, ValueError):
        return None
    if no_bid <= NOT_QUOTING:
        return None
    price = round(1.0 - no_bid, 4)
    return price if 0.0 < price < 1.0 else None


async def _user_id(client) -> str | None:
    """The quotes endpoint filters by creator; any of our orders carries it."""
    cached = getattr(client, "_combo_user_id", None)
    if cached:
        return cached
    path = "/portfolio/orders"
    r = await client.client.get(client.base_url + path, params={"limit": 1},
                                headers=client._headers("GET", path))
    if r.status_code >= 300:
        return None
    orders = r.json().get("orders") or []
    uid = orders[0].get("user_id") if orders else None
    if uid:
        client._combo_user_id = uid
    return uid


async def create_market(client, legs: list[Leg]) -> str:
    """The combo market for these legs. No money moves. Raises on failure."""
    path = f"/multivariate_event_collections/{COLLECTION}"
    r = await client.client.post(client.base_url + path,
                                 json={"selected_markets": [l.selected() for l in legs]},
                                 headers=client._headers("POST", path))
    if r.status_code >= 300:
        raise RuntimeError(f"combo market {r.status_code}: {r.text[:200]}")
    ticker = r.json().get("market_ticker")
    if not ticker:
        raise RuntimeError(f"combo market: no market_ticker in {r.text[:200]}")
    return ticker


async def buy(client, legs: list[Leg], market_ticker: str, count: int, *,
              max_ratio: float = 1.0, wait_s: float = 25.0, poll_s: float = 2.0,
              confirm_s: float = 8.0, accept: bool = True,
              fund: bool = False) -> ComboResult:
    """RFQ -> validated quote -> accept -> confirmed fill. Never raises.

    `accept=False` stops before spending anything - used to verify the path
    against the live exchange.

    `fund=True` moves the shortfall into the combo market's shard BEFORE the
    RFQ, sized at the most it may pay (the cap) plus the fee allowance, so a
    good quote can be accepted at once rather than after a transfer. Combo
    markets live in shard 1, which nothing else funds: on 2026-09-27 the
    operator's held $0.28 against $103 in shard 0, so without this every combo
    accept on the main account would be refused.
    """
    product = round(math.prod(l.ask for l in legs), 6)
    # THE CAP IS THE CHEAPER LEG (operator, 2026-09-27: same or opposite
    # direction, the combo is accepted). A combo pays only if BOTH legs win,
    # so it can never be worth more than its cheaper leg; below that, the
    # cheapest quote is taken. The legs' PRODUCT is not the cap: Kalshi prices
    # correlation in, so a same-direction pair is quoted above it - 1.075x on
    # the app at 18:52 (BTC 76 + SOL 88 at 71.9c), 1.24x on an RFQ at 18:47.
    cap = min(l.ask for l in legs)
    limit = round(cap * max_ratio, 4)
    res = ComboResult("none", "", market_ticker=market_ticker, product=product,
                      limit=limit)
    accepted = False
    try:
        if fund and accept:
            try:
                ok, note = await client.ensure_funds(market_ticker, count, limit,
                                                     force=True)
                if note:
                    res.reason = f"funding: {note}"
            except Exception:  # noqa: BLE001 - funding never blocks the attempt
                pass
        uid = await _user_id(client)
        if not uid:
            res.reason = "cannot read our own user id - quotes need it"
            return res
        path = "/communications/rfqs"
        body = {"mve_collection_ticker": COLLECTION,
                "mve_selected_legs": [l.selected() for l in legs],
                "market_ticker": market_ticker,
                "contracts_fp": f"{count:.2f}"}
        r = await client.client.post(client.base_url + path, json=body,
                                     headers=client._headers("POST", path))
        if r.status_code >= 300:
            res.reason = f"RFQ refused {r.status_code}: {r.text[:160]}"
            return res
        payload = r.json()
        res.rfq_id = (payload.get("rfq") or payload).get("id") or ""

        chosen = None
        qpath = "/communications/quotes"
        deadline = time.monotonic() + wait_s
        while time.monotonic() < deadline:
            r = await client.client.get(
                client.base_url + qpath,
                params={"rfq_creator_user_id": uid, "limit": 100},
                headers=client._headers("GET", qpath))
            if r.status_code < 300:
                for q in r.json().get("quotes") or []:
                    if q.get("rfq_id") != res.rfq_id:
                        continue
                    if q.get("status") not in (None, "open", "active"):
                        continue
                    price = quote_price(q)
                    if price is None:
                        continue
                    if price not in res.quotes_seen:
                        res.quotes_seen.append(price)
                    if price <= limit and (chosen is None or price < chosen[0]):
                        chosen = (price, q)
            if chosen:
                break
            await _sleep(poll_s)
        if not chosen:
            seen = ", ".join(f"{p:.4f}" for p in sorted(res.quotes_seen)) or "none"
            res.reason = (f"no quote at or below {limit:.4f} (the cheaper "
                          f"leg; legs multiplied {product:.4f}); quotes seen: "
                          f"{seen}")
            return res

        price, quote = chosen
        res.price, res.quote_id = price, quote.get("id") or ""
        if not accept:
            res.reason = f"would buy {count} at {price:.4f} (limit {limit:.4f}) - not accepted"
            return res

        # Funding first, like every other order (a no-op unless auto_fund).
        try:
            await client.ensure_funds(market_ticker, count, price)
        except Exception:  # noqa: BLE001 - funding never blocks the attempt
            pass

        apath = f"/communications/quotes/{res.quote_id}/accept"
        accepted_ms = int(time.time() * 1000)
        try:
            r = await client.client.request(
                "PUT", client.base_url + apath, json={"accepted_side": "no"},
                headers=client._headers("PUT", apath))
        except (httpx.HTTPError, OSError) as exc:
            res.outcome = "unknown"
            res.reason = f"accept sent, answer lost ({type(exc).__name__})"
            return res
        if r.status_code >= 500:
            # A server error says nothing about whether it was processed.
            res.outcome = "unknown"
            res.reason = f"accept answered {r.status_code} - it may have gone through"
            return res
        if r.status_code >= 300:
            res.reason = f"accept refused {r.status_code}: {r.text[:160]}"
            return res
        accepted = True

        status = None
        deadline = time.monotonic() + confirm_s
        spath = f"{qpath}/{res.quote_id}"
        while time.monotonic() < deadline:
            await _sleep(1.0)
            try:
                rr = await client.client.get(client.base_url + spath,
                                             headers=client._headers("GET", spath))
            except (httpx.HTTPError, OSError):
                continue
            status = ((rr.json().get("quote") or {}) if rr.status_code < 300
                      else {}).get("status")
            if status in ("executed", "cancelled", "expired"):
                break
        # ONLY A MAKER WHO WALKED AWAY IS "NOTHING BOUGHT". Any other state at
        # the deadline - executed, accepted, confirmed, unanswered - may still
        # become a fill, and a single-leg order on top of it doubles the
        # position.
        if status in ("cancelled", "expired"):
            res.reason = f"accepted, maker did not confirm ({status}) - nothing bought"
            return res

        # THE BROKER DECIDES. Acceptance is not a fill. Only fills from this
        # accept onwards: the same combo market can already be held - the
        # other instance buys BTC+SOL too - and summing every fill on the
        # ticker would book that position as this one.
        filled, cost, fee = 0.0, 0.0, 0.0
        try:
            fpath = "/portfolio/fills"
            rr = await client.client.get(client.base_url + fpath,
                                         params={"ticker": market_ticker, "limit": 50},
                                         headers=client._headers("GET", fpath))
            for f in (rr.json().get("fills") or []) if rr.status_code < 300 else []:
                if _fill_ms(f) is not None and _fill_ms(f) < accepted_ms - 5_000:
                    continue
                n = float(f.get("count_fp") or f.get("count") or 0)
                filled += n
                cost += n * float(f.get("yes_price_dollars") or 0)
                fee += float(f.get("fee_cost") or 0)
        except (httpx.HTTPError, OSError, ValueError):
            pass
        if filled > 0:
            res.outcome = "bought"
            res.filled = filled
            res.price = round(cost / filled, 4) if cost > 0 else price
            res.fee = round(fee, 4)
            res.reason = (f"bought {filled:g} at {res.price:.4f} (cap "
                          f"{limit:.4f}, legs multiplied {product:.4f})")
            return res
        res.outcome = "unknown"
        res.reason = (f"quote {status or 'unanswered'} and no fill readable yet - "
                      f"it may still fill; treated as held and reconciled from "
                      f"the broker's fills")
        return res
    except (httpx.HTTPError, OSError, ValueError, KeyError, TypeError) as exc:
        # Before the accept nothing can have been bought; after it, anything
        # may have been.
        if accepted:
            res.outcome = "unknown"
        res.reason = f"{type(exc).__name__}: {exc}"[:200]
        return res


def _fill_ms(fill: dict) -> int | None:
    """A fill's own time in ms, from `created_time` (ISO) or `ts` (s)."""
    raw = fill.get("created_time")
    if raw:
        try:
            from datetime import datetime
            return int(datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
                       .timestamp() * 1000)
        except ValueError:
            return None
    ts = fill.get("ts")
    if isinstance(ts, (int, float)):
        return int(ts * 1000) if ts < 1e12 else int(ts)
    return None


def note_for(legs: list[Leg], result: ComboResult) -> str:
    """The proposal's result note: every leg, the price check, the outcome."""
    return json.dumps({
        "legs": [{"asset": l.asset, "ticker": l.ticker, "side": l.side,
                  "ask": l.ask} for l in legs],
        "product": result.product, "limit": result.limit, "price": result.price,
        "quotes_seen": result.quotes_seen, "rfq_id": result.rfq_id,
        "outcome": result.outcome, "reason": result.reason,
    })[:1000]
