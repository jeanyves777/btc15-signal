from dataclasses import dataclass
from itertools import pairwise

import httpx


@dataclass(frozen=True)
class MarketSnapshot:
    price: float
    target: float
    bid_imbalance: float
    taker_imbalance: float
    momentum_5m_bps: float
    volatility_5m_bps: float
    futures_basis_bps: float
    spread_bps: float
    window_high: float = 0.0
    window_low: float = 0.0
    elapsed_minutes: int = 0


class BinanceClient:
    def __init__(self, symbol: str, spot_base_url: str, futures_base_url: str) -> None:
        self.symbol = symbol.upper()
        self.spot = spot_base_url.rstrip("/")
        self.futures = futures_base_url.rstrip("/")
        self.client = httpx.AsyncClient(timeout=8)

    async def close(self) -> None:
        await self.client.aclose()

    async def snapshot(self, window_open_ms: int) -> MarketSnapshot:
        params = {"symbol": self.symbol}
        ticker, depth, klines, trades = await self._parallel_requests(
            (self.spot + "/api/v3/ticker/bookTicker", params),
            (self.spot + "/api/v3/depth", {**params, "limit": 100}),
            (
                self.spot + "/api/v3/klines",
                {
                    **params,
                    "interval": "1m",
                    "startTime": window_open_ms,
                    "limit": 16,
                },
            ),
            (self.spot + "/api/v3/aggTrades", {**params, "limit": 500}),
        )
        bid = float(ticker["bidPrice"])
        ask = float(ticker["askPrice"])
        price = (bid + ask) / 2
        spread = (ask - bid) / price * 10_000
        target = self._window_open(klines, window_open_ms)
        bid_qty = sum(float(level[1]) for level in depth["bids"])
        ask_qty = sum(float(level[1]) for level in depth["asks"])
        bid_imbalance = (bid_qty - ask_qty) / max(bid_qty + ask_qty, 1e-9)
        buy_qty = sum(float(t["q"]) for t in trades if not t["m"])
        sell_qty = sum(float(t["q"]) for t in trades if t["m"])
        taker_imbalance = (buy_qty - sell_qty) / max(buy_qty + sell_qty, 1e-9)
        closes = [float(k[4]) for k in klines[-6:]]
        returns = [(b / a - 1) * 10_000 for a, b in pairwise(closes)]
        momentum = (closes[-1] / closes[0] - 1) * 10_000
        mean = sum(returns) / max(len(returns), 1)
        volatility = (sum((x - mean) ** 2 for x in returns) / max(len(returns), 1)) ** 0.5
        basis = await self._futures_basis(price)
        completed = [k for k in klines if int(k[6]) < int(ticker.get("closeTime", 0))]
        observed = completed or klines
        return MarketSnapshot(
            price,
            target,
            bid_imbalance,
            taker_imbalance,
            momentum,
            volatility,
            basis,
            spread,
            max(float(k[2]) for k in observed),
            min(float(k[3]) for k in observed),
            len(completed),
        )

    async def recent_bars(self, limit: int = 1500) -> list[tuple[int, float, float]]:
        """The last `limit` one-minute bars as (open_time, high, low).

        Separate from `snapshot`, which fetches sixteen bars for the current
        window only. Support and resistance need about a day, and that is a
        second request - so it is deliberately NOT part of the snapshot and is
        called on the level tracker's slow clock, off the order path.
        """
        response = await self.client.get(
            self.spot + "/api/v3/klines",
            params={"symbol": self.symbol, "interval": "1m", "limit": limit},
        )
        response.raise_for_status()
        return [(int(r[0]), float(r[2]), float(r[3])) for r in response.json()]

    async def close_at(self, window_open_ms: int) -> float:
        response = await self.client.get(
            self.spot + "/api/v3/klines",
            params={
                "symbol": self.symbol,
                "interval": "1m",
                "startTime": window_open_ms + 14 * 60 * 1000,
                "limit": 1,
            },
        )
        response.raise_for_status()
        rows = response.json()
        if not rows:
            raise RuntimeError("Missing settlement candle")
        return float(rows[0][4])

    async def _futures_basis(self, spot_price: float) -> float:
        try:
            response = await self.client.get(
                self.futures + "/fapi/v1/premiumIndex", params={"symbol": self.symbol}
            )
            response.raise_for_status()
            return (float(response.json()["markPrice"]) / spot_price - 1) * 10_000
        except httpx.HTTPError:
            return 0.0

    async def _parallel_requests(self, *requests: tuple[str, dict]) -> list:
        import asyncio

        async def get(url: str, params: dict):
            response = await self.client.get(url, params=params)
            response.raise_for_status()
            return response.json()

        return await asyncio.gather(*(get(url, params) for url, params in requests))

    @staticmethod
    def _window_open(klines: list, window_open_ms: int) -> float:
        eligible = [k for k in klines if int(k[0]) <= window_open_ms < int(k[6]) + 1]
        if not eligible:
            raise RuntimeError("Binance response did not contain the 15-minute window open")
        return float(eligible[-1][1])
