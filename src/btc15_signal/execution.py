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
        self, proposal: TradeProposal, slippage: float = 0.0
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
        # 0.99 is the cap: event_order requires a price strictly inside (0, 1).
        limit = min(proposal.entry_limit + max(0.0, slippage), 0.99)
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

    async def close_position(
        self, ticker: str, side: str, count: float, limit_price: float
    ) -> ExecutionResult:
        """Sell out of an open position at `limit_price` or better.

        Immediate-or-cancel, reduce-only. A reversal exit that quietly became a
        resting order would sit on the book after the thesis had already
        resolved either way, which is a different trade from the one intended;
        and reduce_only means a stale count can never open a new position on
        the opposite side.

        A no-fill is not an error. It means nobody was bidding at that price,
        and the position simply rides to settlement - exactly what would have
        happened without this call.
        """
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
            f"Sold {filled:g} at {limit_price:.0%}",
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
