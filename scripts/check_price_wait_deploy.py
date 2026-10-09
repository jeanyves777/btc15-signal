"""Read-only broker position/order check for a safe service reload. No orders sent."""
import asyncio
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from btc15_signal.config import Settings
from btc15_signal.execution import KalshiExecutionClient
from btc15_signal.mirror import targets_from_settings


async def main():
    settings = Settings(_env_file=ROOT / ".env")
    accounts = [("primary", settings.kalshi_api_key_id, settings.kalshi_private_key_path)]
    accounts += [(t.name, t.api_key_id, t.private_key_path) for t in targets_from_settings(settings)]
    result = []
    for name, key, path in accounts:
        client = KalshiExecutionClient(settings.kalshi_base_url, key, path)
        try:
            held, _mark, _tickers = await client.open_mark()
            orders, resting = await client.resting_exposure()
            result.append(dict(account=name, positions=held, resting_orders=orders,
                               readable=held >= 0 and orders >= 0 and resting >= 0,
                               checked_ms=int(time.time() * 1000)))
        except Exception as exc:
            result.append(dict(account=name, readable=False, error=type(exc).__name__))
        finally:
            await client.close()
    print(json.dumps(result))
    (ROOT / "runtime/price_wait_preflight.json").write_text(json.dumps(result, indent=2))
    return 0 if all(r['readable'] and r['positions'] == 0 and r['resting_orders'] == 0
                    for r in result) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
