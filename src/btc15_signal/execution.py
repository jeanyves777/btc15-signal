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

    async def execute_with_take_profit(self, proposal: TradeProposal) -> ExecutionResult:
        path = "/portfolio/events/orders"
        book_side, yes_price = event_order(proposal.side, proposal.entry_limit)
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
            return ExecutionResult("unfilled", 0, entry_id, None, "Entry limit did not fill")
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
