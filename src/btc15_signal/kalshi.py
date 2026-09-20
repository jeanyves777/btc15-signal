from dataclasses import dataclass
from datetime import datetime

import httpx


@dataclass(frozen=True)
class KalshiMarket:
    ticker: str
    target: float
    open_ms: int
    close_ms: int
    yes_ask: float
    no_ask: float

    def ask(self, side: str) -> float:
        return self.yes_ask if side == "UP" else self.no_ask


class KalshiClient:
    def __init__(self, base_url: str, series: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.series = series
        self.client = httpx.AsyncClient(timeout=10)

    async def close(self) -> None:
        await self.client.aclose()

    async def active_market(self, now_ms: int) -> KalshiMarket:
        response = await self.client.get(
            self.base_url + "/markets",
            params={"series_ticker": self.series, "status": "open", "limit": 100},
        )
        response.raise_for_status()
        matches = []
        for item in response.json().get("markets", []):
            opened = iso_ms(item["open_time"])
            closed = iso_ms(item["close_time"])
            if opened <= now_ms < closed and item.get("floor_strike") is not None:
                matches.append(
                    KalshiMarket(
                        ticker=item["ticker"],
                        target=float(item["floor_strike"]),
                        open_ms=opened,
                        close_ms=closed,
                        yes_ask=float(item["yes_ask_dollars"]),
                        no_ask=float(item["no_ask_dollars"]),
                    )
                )
        if len(matches) != 1:
            raise RuntimeError(f"Expected one active {self.series} market, found {len(matches)}")
        return matches[0]

    async def result(self, ticker: str) -> str | None:
        response = await self.client.get(self.base_url + f"/markets/{ticker}")
        response.raise_for_status()
        result = response.json()["market"].get("result")
        return result if result in {"yes", "no"} else None


def iso_ms(value: str) -> int:
    return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() * 1000)
