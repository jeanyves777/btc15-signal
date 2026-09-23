import base64
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse
from uuid import uuid4

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

from .store import TradeProposal

# The instrument's own definition, not settings: a Kalshi binary event contract
# settles at one dollar or nothing, and `revenue` is quoted in cents. They are
# named so that no dollar figure anywhere in the P&L path is a bare literal.
CONTRACT_FACE = 1.0
CENTS_PER_DOLLAR = 100.0


@dataclass(frozen=True)
class ExecutionResult:
    status: str
    filled_count: float
    entry_order_id: str
    take_profit_order_id: str | None
    note: str


def event_order(side: str, contract_price: float, exiting: bool = False) -> tuple[str, float]:
    if side not in {"UP", "DOWN"}:
        raise ValueError("side must be UP or DOWN")
    if not 0 < contract_price < 1:
        raise ValueError("contract price must be between 0 and 1")
    if side == "UP":
        return ("ask" if exiting else "bid"), contract_price
    return ("bid" if exiting else "ask"), 1 - contract_price


def parse_fill(order: dict | None, side: str) -> dict | None:
    """(count, price, fee, is_taker) from an order, or None if unfilled.

    THE PRICE IS THE COST DIVIDED BY THE COUNT, because that is what was
    actually paid. `yes_price_dollars` is the quote on the YES side, so on
    a DOWN position it reads 0.1700 for an order that filled at 0.8300 -
    the complement, silently, with no error anywhere.

    A PENDING ORDER RETURNS None. `initial_count_fp` is what was asked
    for; `fill_count_fp` is what happened, and only the second may ever be
    treated as a position.
    """
    if not order:
        return None

    def num(key: str) -> float:
        try:
            return float(order.get(key) or 0)
        except (TypeError, ValueError):
            return 0.0

    count = num("fill_count_fp")
    if count <= 0:
        count = max(0.0, num("initial_count_fp") - num("remaining_count_fp"))
    if count <= 0:
        return None
    maker_cost, taker_cost = (num("maker_fill_cost_dollars"),
                              num("taker_fill_cost_dollars"))
    cost = maker_cost + taker_cost
    fee = num("maker_fees_dollars") + num("taker_fees_dollars")
    if cost > 0:
        price = cost / count
    else:
        # No cost reported: fall back to the quote for OUR side, never the
        # other one.
        price = num("no_price_dollars" if side == "DOWN"
                    else "yes_price_dollars")
    return {
        "count": count,
        "price": round(price, 6),
        "fee": round(fee, 6),
        "is_taker": 1 if taker_cost > 0 else 0,
        "status": order.get("status"),
        "order_id": order.get("order_id"),
    }


class KalshiExecutionClient:
    def __init__(self, base_url: str, api_key_id: str, private_key_path: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key_id = api_key_id
        key_bytes = Path(private_key_path).expanduser().read_bytes()
        self.private_key = serialization.load_pem_private_key(key_bytes, password=None)
        self.client = httpx.AsyncClient(timeout=10)

    async def close(self) -> None:
        await self.client.aclose()

    def _headers(self, method: str, path: str) -> dict[str, str]:
        timestamp = str(int(time.time() * 1000))
        sign_path = urlparse(self.base_url + path).path
        message = f"{timestamp}{method.upper()}{sign_path}".encode()
        signature = self.private_key.sign(
            message,
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
            hashes.SHA256(),
        )
        return {
            "KALSHI-ACCESS-KEY": self.api_key_id,
            "KALSHI-ACCESS-TIMESTAMP": timestamp,
            "KALSHI-ACCESS-SIGNATURE": base64.b64encode(signature).decode(),
        }

    async def _post(self, path: str, payload: dict) -> dict:
        response = await self.client.post(
            self.base_url + path,
            json=payload,
            headers=self._headers("POST", path),
        )
        response.raise_for_status()
        return response.json()

    async def execute_with_take_profit(
        self, proposal: TradeProposal, slippage: float = 0.0,
        ceiling: float | None = None,
    ) -> ExecutionResult:
        """Buy, and optionally rest a take-profit behind the fill.

        `slippage` is how much MORE than the quoted price we are willing to pay.
        Without it the entry posts at exactly the touch, and an
        immediate-or-cancel order at the touch only fills if the resting size is
        still there microseconds later: 40% of the first live orders missed
        entirely. A limit still fills at the best available price, so the extra
        is paid only when the book actually moved - in the cases that would
        otherwise have been no trade at all.
        """
        path = "/portfolio/events/orders"
        # THE LIMIT IS THE CEILING, NOT THE PRICE.
        #
        # An immediate-or-cancel limit fills at the BEST AVAILABLE price and
        # never at the limit - our own fills prove it: limit 0.87 filled 0.84,
        # limit 0.80 filled 0.75, limit 0.81 filled 0.76. So the limit does not
        # decide what we pay; it decides how far we are willing to cross a book
        # that has moved since the price was read.
        #
        # Pricing it at `ask + 1c` meant 11% of orders were under-priced before
        # they left, and every one of those came back "no fill; the book moved".
        # A measured 5c allowance covered 100% of recorded drift, but "covered
        # every move so far" is not the same as "cannot miss", and a miss costs
        # an entire trade while crossing costs a few cents.
        #
        # So the entry crosses to the ceiling. Inside it we always pay the real
        # ask; outside it we do not trade at all - and above 0.95 no measured
        # bucket's interval excludes zero, so refusing there is the rule
        # working, not an execution failure. The ceiling is also the guard that
        # an unbounded chase lacked when 0.85 became 0.963 on 2026-09-21.
        limit = proposal.entry_limit + max(0.0, slippage)
        if ceiling is not None:
            # Cross exactly TO the ceiling - it is both the floor and the cap.
            # Flooring alone let a 0.93 ask send 0.98; capping alone left the
            # limit only as good as the drift estimate in `slippage`, and a
            # move larger than that estimate is a lost trade rather than a few
            # cents paid. The ceiling makes the limit independent of both.
            limit = ceiling
        limit = min(limit, 0.99)
        book_side, yes_price = event_order(proposal.side, limit)
        entry = await self._post(
            path,
            {
                "ticker": proposal.ticker,
                "client_order_id": str(uuid4()),
                "side": book_side,
                "count": f"{proposal.count:.2f}",
                "price": f"{yes_price:.4f}",
                "time_in_force": "immediate_or_cancel",
                "self_trade_prevention_type": "taker_at_cross",
                "cancel_order_on_pause": True,
                "reduce_only": False,
            },
        )
        filled = float(entry["fill_count"])
        entry_id = entry["order_id"]
        if filled <= 0:
            return ExecutionResult(
                "unfilled", 0, entry_id, None,
                f"No fill at {limit:.0%}; the book moved before the order landed",
            )
        if proposal.take_profit <= 0:
            return ExecutionResult(
                "filled", filled, entry_id, None, f"Filled {filled:g}; holding to settlement"
            )
        exit_side, exit_yes_price = event_order(proposal.side, proposal.take_profit, exiting=True)
        try:
            take_profit = await self._post(
                path,
                {
                    "ticker": proposal.ticker,
                    "client_order_id": str(uuid4()),
                    "side": exit_side,
                    "count": f"{filled:.2f}",
                    "price": f"{exit_yes_price:.4f}",
                    "time_in_force": "good_till_canceled",
                    "expiration_time": max(proposal.close_ms // 1000 - 5, int(time.time()) + 1),
                    "self_trade_prevention_type": "taker_at_cross",
                    "post_only": False,
                    "cancel_order_on_pause": True,
                    "reduce_only": True,
                },
            )
        except httpx.HTTPError:
            return ExecutionResult(
                "unprotected",
                filled,
                entry_id,
                None,
                f"URGENT: entry filled {filled:g}, but take-profit placement failed",
            )
        return ExecutionResult(
            "protected",
            filled,
            entry_id,
            take_profit["order_id"],
            f"Filled {filled:g}; take-profit order resting",
        )

    async def place_resting_buy(
        self,
        ticker: str,
        side: str,
        price: float,
        count: int,
        expiration_ts: int,
        client_order_id: str,
    ) -> dict:
        """A GTC limit BUY that waits at a price, with its own deadline.

        Used by the recovery add-on. Three things here are safety, not taste:

        * `client_order_id` is DETERMINISTIC, supplied by the caller. Kalshi
          rejects a duplicate, so a retry after a timeout - or a restart that
          re-evaluates the same position - cannot open a second contract. A
          random uuid4, which every other order path here uses, would make the
          ambiguous case (request sent, response lost) a duplicate order.
        * `expiration_ts` is explicit. Kalshi cancels resting orders shortly
          after close, but a cancel REQUEST is rejected once the market has
          closed, so an order without its own expiry can neither be relied on
          to die nor be killed by hand.
        * `post_only` is False deliberately. The add is allowed to cross if the
          book has already moved through the limit; refusing to be a taker
          would silently skip exactly the fast moves worth measuring.
        """
        # `/portfolio/events/orders` (CreateOrderV2), NOT `/portfolio/orders`.
        # The legacy path now answers 410 Gone, and the add-on used it: every
        # placement failed and no order ever reached the exchange, while the
        # record showed a local PENDING that was later cancelled on its
        # deadline. The V2 shape carries direction in `side` (bid buys, ask
        # sells) with fixed-point dollar prices - there is no `action` or
        # `type` field, and sending them is how the wrong shape went unnoticed.
        order_side, yes_price = event_order(side, price)
        return await self._post(
            "/portfolio/events/orders",
            {
                "ticker": ticker,
                "client_order_id": client_order_id,
                "side": order_side,
                "count": f"{count:.2f}",
                "price": f"{yes_price:.4f}",
                "time_in_force": "good_till_canceled",
                "expiration_time": expiration_ts,
                "self_trade_prevention_type": "taker_at_cross",
                "post_only": False,
                "cancel_order_on_pause": True,
            },
        )

    async def cancel_order(self, order_id: str) -> tuple[bool, str]:
        """(cancelled, note). Never raises.

        A cancel that loses the race to a fill is NOT an error - the order
        filled, which is a state the caller has to reconcile rather than a
        failure to report. A cancel after the market closed is rejected by
        Kalshi outright, which is why the order carries its own expiry.
        """
        # CancelOrderV2. The legacy `/portfolio/orders/{id}` is the same dead
        # family as the legacy create path.
        path = f"/portfolio/events/orders/{order_id}"
        try:
            response = await self.client.delete(
                self.base_url + path, headers=self._headers("DELETE", path)
            )
            if response.status_code in (200, 204):
                return True, "cancelled"
            if response.status_code == 404:
                return False, "order not found (already filled, expired or cancelled)"
            return False, f"HTTP {response.status_code}: {response.text[:120]}"
        except (httpx.HTTPError, OSError) as exc:
            return False, f"{type(exc).__name__}: {exc}"[:160]

    # The parser is a module function so a test double supplies an ORDER and
    # the real parser reads it, rather than every double implementing its own.
    parse_fill = staticmethod(parse_fill)

    async def order_status(self, order_id: str) -> dict | None:
        """The order as Kalshi sees it, for reconciling a cancel/fill race.

        `/portfolio/orders/{id}`, NOT `/portfolio/events/orders/{id}`. The
        create and cancel calls moved to the `events` family and this read was
        moved with them, but the read never existed there: it returns 404 for
        every order, so this returned None every time and the caller concluded
        "not filled" on orders that had filled. `/portfolio/orders/{id}` is a
        live GET and returns the order.
        """
        path = f"/portfolio/orders/{order_id}"
        try:
            response = await self.client.get(
                self.base_url + path, headers=self._headers("GET", path)
            )
            response.raise_for_status()
            return response.json().get("order")
        except (httpx.HTTPError, ValueError, KeyError):
            return None

    async def order_by_client_id(
        self, ticker: str, client_order_id: str
    ) -> dict | None:
        """The order OUR id owns, for a submission whose response was lost.

        THE AMBIGUOUS CASE. The local row is written before the order is sent,
        so a crash - or a dropped response - between the send and storing the
        broker's `order_id` leaves a row that says PENDING with no id. Every
        repair path here is keyed on `order_id`, and `fills` carries no client
        id, so that row could never be resolved: the order kept resting at
        Kalshi, unwatched, and a fill on it would never have been banked.

        `client_order_id` is a pure function of (ticker, side, window), so it
        is the one key that survives the crash. Kalshi returns it on the order
        object, which makes the listing the way back to the `order_id`.

        None means "no order is visible for that id", which the caller must
        treat as UNKNOWN rather than as "nothing was placed" - a listing we
        could not read looks identical to one with nothing in it, so the two
        are separated by raising nothing and returning None only after a read
        that actually succeeded. A failed read raises `LookupError`.
        """
        path = "/portfolio/orders"
        try:
            response = await self.client.get(
                self.base_url + path,
                params={"ticker": ticker, "limit": 200},
                headers=self._headers("GET", path),
            )
            response.raise_for_status()
            orders = response.json().get("orders") or []
        except (httpx.HTTPError, ValueError, KeyError) as exc:
            # UNREADABLE IS NOT EMPTY. Saying "not found" here would let the
            # caller conclude the order never existed and write it off.
            raise LookupError(f"could not list orders: {type(exc).__name__}") from exc
        for order in orders:
            if str(order.get("client_order_id") or "") == str(client_order_id):
                return order
        return None

    async def balance_dollars(self) -> float:
        """Cash on hand. Negative when it could not be read.

        An order is sized against what the account HAS, checked now - not
        against a running total of what it has spent. Money that settled back
        is spendable again, and a cap that cannot see that stops trading for
        lack of a number rather than lack of funds.
        """
        path = "/portfolio/balance"
        try:
            response = await self.client.get(
                self.base_url + path, headers=self._headers("GET", path)
            )
            response.raise_for_status()
            body = response.json()
            if "balance_dollars" in body:
                return float(body["balance_dollars"])
            return round(float(body.get("balance") or 0) / 100.0, 4)
        except (httpx.HTTPError, ValueError, KeyError, TypeError):
            return -1.0

    async def resting_exposure(self) -> tuple[int, float]:
        """(orders, dollars) committed to resting buys that have not filled.

        Exposure is filled positions PLUS working orders. Counting only fills
        is how a cap gets exceeded by the amount of whatever is resting.
        """
        path = "/portfolio/orders"
        try:
            response = await self.client.get(
                self.base_url + path, params={"status": "resting", "limit": 200},
                headers=self._headers("GET", path),
            )
            response.raise_for_status()
            orders = response.json().get("orders") or []
        except (httpx.HTTPError, ValueError, KeyError):
            # Unknown exposure is not zero exposure. Report it as a failure so
            # the caller can refuse to add rather than assume room.
            return -1, -1.0
        total = 0.0
        for order in orders:
            if order.get("action") != "buy":
                continue
            remaining = float(order.get("remaining_count") or 0)
            price = float(order.get("yes_price_dollars") or order.get("price") or 0)
            if order.get("side") == "no":
                price = 1 - price
            total += remaining * price
        return len(orders), round(total, 4)

    async def close_position(
        self, ticker: str, side: str, count: float, limit_price: float,
        floor: float | None = None,
    ) -> ExecutionResult:
        """Sell out of an open position at `limit_price` or better.

        Immediate-or-cancel, reduce-only. A reversal exit that quietly became a
        resting order would sit on the book after the thesis had already
        resolved either way, which is a different trade from the one intended;
        and reduce_only means a stale count can never open a new position on
        the opposite side.

        THE LIMIT IS A FLOOR, NOT A PRICE. A sell IOC fills at the best
        available BID and never at its own limit, so pricing the exit at the
        quoted bid made it as fragile as the entry was: on
        KXBTC15M-26SEP211700-00 the quote said 0.98, the order went out at
        0.98, and nothing filled. Crossing down to `floor` takes whatever real
        bid is there above it, which is the whole point of cashing out.

        A no-fill is still not an error - it means nobody was bidding above the
        floor, and the position rides to settlement as it would have anyway.
        """
        if floor is not None:
            limit_price = min(limit_price, floor)
        exit_side, yes_price = event_order(side, limit_price, exiting=True)
        order = await self._post(
            "/portfolio/events/orders",
            {
                "ticker": ticker,
                "client_order_id": str(uuid4()),
                "side": exit_side,
                "count": f"{count:.2f}",
                "price": f"{yes_price:.4f}",
                "time_in_force": "immediate_or_cancel",
                "self_trade_prevention_type": "taker_at_cross",
                "cancel_order_on_pause": True,
                "reduce_only": True,
            },
        )
        filled = float(order["fill_count"])
        if filled <= 0:
            return ExecutionResult(
                "exit-unfilled",
                0,
                order["order_id"],
                None,
                f"No bid at {limit_price:.0%}; holding to settlement",
            )
        return ExecutionResult(
            "exited",
            filled,
            order["order_id"],
            None,
            # NOT `limit_price` - that is the floor we were willing to cross
            # down to, never what we got. A sell IOC fills at the best
            # available bid, and it does: crossing to a 0.90 floor returned
            # 0.982, 0.998 and 0.997 on 2026-09-21 while this line called
            # every one of them "Sold 1 at 90%". The real price is read back
            # by `fill_detail` and lands on `trade_proposals.exit_price`.
            f"Sold {filled:g} (crossed to {limit_price:.0%}; "
            f"filled at the best bid)",
        )

    async def settlements(self, limit: int = 200) -> list[dict]:
        """Every settled market on the account, as the EXCHANGE accounts for it.

        This is the only honest source of realised P&L. Rebuilding it locally
        from `trade_proposals` got the sign wrong: it reported +1.06 on an
        account that was actually -1.62, because it priced unfilled rows at
        `entry_limit` (a limit is permission to cross, never what was paid),
        modelled the fee instead of reading it, and leaned on
        `predictions.won`, which is our own settlement guess rather than the
        exchange's. It also saw 47 of 88 settled markets, because a proposal
        that filled but never reached a terminal status is excluded by
        ACCOUNTED_SQL.

        Paginated: Kalshi caps a page at 200 and hands back a cursor.
        """
        return await self._paginate("/portfolio/settlements", "settlements", limit)

    async def fills(self, limit: int = 200) -> list[dict]:
        """Every execution on the account, as the broker recorded it.

        The trade COUNT has to come from here for the same reason the money
        does. Counting `trade_proposals` rows counts our intentions: it misses
        anything filled outside the bot, and it miscounts anything whose status
        never reached a terminal value - 71 proposals sat at `pending` on
        2026-09-21 alone.
        """
        return await self._paginate("/portfolio/fills", "fills", limit)

    async def _paginate(self, path: str, key: str, limit: int) -> list[dict]:
        """Kalshi caps a page at 200 and hands back a cursor."""
        out: list[dict] = []
        cursor = ""
        while True:
            query = f"{path}?limit={limit}" + (f"&cursor={cursor}" if cursor else "")
            response = await self.client.get(
                self.base_url + query, headers=self._headers("GET", path)
            )
            response.raise_for_status()
            page = response.json()
            rows = page.get(key) or []
            out.extend(rows)
            cursor = page.get("cursor") or ""
            if not cursor or not rows:
                return out

    async def open_mark(self) -> tuple[int, float, dict[str, float]]:
        """(open positions, unrealised dollars, per-ticker marks) at the bid.

        The per-ticker breakdown exists so a position that has already been
        banked can be dropped from the mark. One total cannot do that, and
        adding a stale total to a ledger that has already counted the sale is
        what made $0.55 appear, vanish and reappear across three messages on
        2026-09-22.

        The app's headline figure is TODAY'S realised P&L PLUS the open
        position marked to market - that is how "+$4.05 (+14.67%)" is built,
        and reproducing it takes both halves. Marking at the BID, not the ask
        or the midpoint, because the bid is what the position could actually be
        sold for right now.
        """
        response = await self.client.get(
            self.base_url + "/portfolio/positions?limit=200",
            headers=self._headers("GET", "/portfolio/positions"),
        )
        response.raise_for_status()
        positions = response.json().get("market_positions") or []
        count = 0
        total = 0.0
        per_ticker: dict[str, float] = {}
        for position in positions:
            size = float(position.get("position_fp") or 0)
            if not size:
                continue
            ticker = position.get("ticker")
            path = f"/markets/{ticker}"
            try:
                quote = await self.client.get(
                    self.base_url + path, headers=self._headers("GET", path)
                )
                quote.raise_for_status()
                market = quote.json().get("market", {})
            except (httpx.HTTPError, ValueError, KeyError):
                continue
            field = "yes_bid_dollars" if size > 0 else "no_bid_dollars"
            bid = float(market.get(field) or 0)
            cost = float(position.get("market_exposure_dollars") or 0)
            fees = float(position.get("fees_paid_dollars") or 0)
            mark = abs(size) * bid - cost - fees
            total += mark
            count += 1
            if ticker:
                per_ticker[ticker] = mark
        return count, total, per_ticker

    @staticmethod
    def market_open_ms(ticker: str) -> int | None:
        """The market's own time, parsed from its ticker. None if unrecognised.

        SETTLEMENT TIME IS NOT MARKET TIME. Kalshi settles these in batches
        hours after close - KXBTC15M-26SEP220445-45 settled at 08:45 UTC, four
        hours after its window - so bucketing realised P&L by `settled_time`
        files a trade under the wrong day and hands the daily loss floor the
        wrong window. The ticker carries the real one:

            KXBTC15M-26SEP220445-45   ->  2026-09-22 04:45 UTC
            KXBTCD-26SEP2207-T80099   ->  2026-09-22 07:00 UTC (hourly)
        """
        import re
        from datetime import UTC, datetime

        match = re.match(r"^[A-Z0-9]+-(\d{2})([A-Z]{3})(\d{2})(\d{2})(\d{2})?", ticker or "")
        if not match:
            return None
        year, mon, day, hour, minute = match.groups()
        months = ("JAN", "FEB", "MAR", "APR", "MAY", "JUN",
                  "JUL", "AUG", "SEP", "OCT", "NOV", "DEC")
        if mon not in months:
            return None
        try:
            stamp = datetime(
                2000 + int(year), months.index(mon) + 1, int(day),
                int(hour), int(minute or 0), tzinfo=UTC,
            )
        except ValueError:
            return None
        return int(stamp.timestamp() * 1000)

    @staticmethod
    def settlement_pnl(row: dict) -> float:
        """Realised dollars for one settled market, from Kalshi's fields only.

        `revenue` alone is NOT the payout. When a position is closed early the
        exit is booked as buying the OPPOSITE side, and Kalshi nets the
        offsetting pair immediately at $1 - outside `revenue`, which then reads
        0 even on a market that won. KXBTC15M-26SEP220615-15 is the proof:
        2 NO bought for $1.60, closed by buying 2 YES for $0.012,
        `market_result` "no", `revenue` 0. Scoring that by `revenue` books a
        $1.62 loss on a trade that made +$0.365.

        So the payout is the netted pairs plus whatever actually settled, and
        every other term - both costs and the fee - is read, never modelled.
        """
        yes_n = float(row.get("yes_count_fp") or 0)
        no_n = float(row.get("no_count_fp") or 0)
        pairs = min(yes_n, no_n)
        # NOTHING HERE IS A TUNING NUMBER. Every dollar term is read off the
        # row. The two constants are the instrument's definition, not choices:
        # CONTRACT_FACE is what a Kalshi binary settles at, and CENTS_PER_DOLLAR
        # is the unit `revenue` is quoted in. A netted pair is one YES and one
        # NO, so exactly one of them settles at face and the other at zero -
        # which is why the pair is worth face whatever the result turns out to
        # be, and why it can be counted without looking at `market_result`.
        payout = pairs * CONTRACT_FACE + float(row.get("revenue") or 0) / CENTS_PER_DOLLAR
        return (
            payout
            - float(row.get("yes_total_cost_dollars") or 0)
            - float(row.get("no_total_cost_dollars") or 0)
            - float(row.get("fee_cost") or 0)
        )

    async def fill_detail(self, order_id: str, side: str) -> tuple[float, float, float] | None:
        """(average price paid, contracts, fee) for one order, or None.

        The limit price is what we asked for; this is what we got. On the first
        live auto order the limit was 87c and the fill was 84c - three cents on
        a thirteen-cent gross profit, so scoring the trade at its limit would
        have understated it by nearly a quarter.

        Prices are read from the side actually bought: a DOWN position holds NO
        contracts, and its cost is the no price, not the yes price.
        """
        path = "/portfolio/fills"
        response = await self.client.get(
            self.base_url + path + "?limit=200", headers=self._headers("GET", path)
        )
        response.raise_for_status()
        field = "yes_price_dollars" if side == "UP" else "no_price_dollars"
        cost = count = fee = 0.0
        for fill in response.json().get("fills", []):
            if fill.get("order_id") != order_id:
                continue
            size = float(fill.get("count_fp", 0))
            cost += size * float(fill.get(field, 0))
            count += size
            fee += float(fill.get("fee_cost", 0))
        if count <= 0:
            return None
        return cost / count, count, fee
