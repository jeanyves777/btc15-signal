"""Check live connectivity without placing orders or displaying credentials."""

import asyncio
import time

import httpx

from btc15_signal.binance import BinanceClient
from btc15_signal.config import Settings
from btc15_signal.execution import KalshiExecutionClient
from btc15_signal.kalshi import KalshiClient


async def main() -> None:
    settings = Settings()
    kalshi = KalshiClient(settings.kalshi_base_url, settings.kalshi_series)
    binance = BinanceClient(
        settings.symbol, settings.spot_base_url, settings.futures_base_url
    )
    trader = None
    failed = False
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            for method, params in (
                ("getMe", {}),
                ("getChat", {"chat_id": settings.telegram_chat_id}),
            ):
                response = await client.get(
                    f"https://api.telegram.org/bot{settings.telegram_bot_token}/{method}",
                    params=params,
                )
                response.raise_for_status()
                if not response.json().get("ok"):
                    raise RuntimeError("Telegram check rejected")
                print(f"Telegram {method}: OK", flush=True)
        trader = KalshiExecutionClient(
            settings.kalshi_base_url,
            settings.kalshi_api_key_id,
            settings.kalshi_private_key_path,
        )
        path = "/portfolio/balance"
        response = await trader.client.get(
            trader.base_url + path, headers=trader._headers("GET", path)
        )
        response.raise_for_status()
        print("Kalshi authentication: OK (read-only balance check)", flush=True)
        market = await kalshi.active_market(int(time.time() * 1000))
        print(f"Kalshi live market: {market.ticker}", flush=True)
        await binance.snapshot(market.open_ms)
        print("Binance live snapshot: OK", flush=True)
    except Exception as exc:
        failed = True
        status = exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else "n/a"
        print(f"Connection check failed: {type(exc).__name__}; HTTP status={status}")
    finally:
        await kalshi.close()
        await binance.close()
        if trader:
            await trader.close()
    if failed:
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
