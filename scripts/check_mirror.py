"""Verify each configured mirror account BEFORE any order is forwarded to it.

Read-only: it authenticates, reads the balance and counts resting orders. It
places nothing and cancels nothing, so it is safe to run against a live account
at any time, including while the service is trading.

    .venv/Scripts/python.exe scripts/check_mirror.py

A mirror that fails here would have failed silently at the first trade, with one
line in runtime/mirror.jsonl and no position on the account. Run it after adding
a key, and again after changing one.
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from btc15_signal.config import Settings  # noqa: E402
from btc15_signal.execution import KalshiExecutionClient  # noqa: E402
from btc15_signal.mirror import (  # noqa: E402
    current_instance,
    mirror_allowed,
    targets_from_settings,
)


async def check(target, base_url: str) -> bool:
    print(f"\n[{target.name}]  key {target.api_key_id[:8]}...")
    print(f"  key file   : {target.private_key_path}")
    if not Path(target.private_key_path).expanduser().exists():
        print("  FAIL       : key file does not exist at that path")
        return False
    try:
        client = KalshiExecutionClient(
            base_url, target.api_key_id, target.private_key_path
        )
    except (OSError, ValueError) as exc:
        print(f"  FAIL       : key could not be loaded - {exc}")
        return False
    try:
        balance = await client.balance_dollars()
        orders, resting = await client.resting_exposure()
    except Exception as exc:  # noqa: BLE001
        print(f"  FAIL       : authenticated call failed - {exc}")
        await client.close()
        return False
    await client.close()

    if balance <= 0:
        # balance_dollars returns 0.0 on a read failure as well as on an empty
        # account, so this is a warning rather than a verdict.
        print("  balance    : $0.00 (empty account, or the read failed)")
    else:
        print(f"  balance    : ${balance:,.2f}")
    print(f"  resting    : {orders} order(s), ${resting:,.2f} committed")

    budget = (f"${target.base_budget:.2f}/contract"
              if target.base_budget > 0 else "no budget cap")
    print(f"  base       : {target.base_contracts} contract(s), {budget}")
    print(f"  upsize     : {target.add_contracts or 'follows the primary'}")
    print(f"  ceiling    : {target.max_contracts or 'none'}")

    # What one base entry would actually cost on this account, at a price in
    # the deployed band, so an underfunded account is obvious now.
    worst = target.entry_count(0.93) * 0.93
    if balance > 0 and worst > balance:
        print(f"  WARNING    : a base entry costs up to ${worst:.2f} at 0.93 "
              f"but the account holds ${balance:.2f}")
    else:
        print(f"  base entry costs up to ${worst:.2f} at 0.93")
    print("  OK")
    return True


async def main() -> int:
    settings = Settings()
    targets = targets_from_settings(settings)

    instance = current_instance()
    allowed, why = mirror_allowed(settings)
    print(f"instance         = {instance}"
          "   (set BTC15_INSTANCE to check another)")
    print(f"MIRROR_INSTANCES = {settings.mirror_instances or '(empty)'}")
    print(f"MIRROR_ENABLED   = {settings.mirror_enabled}")
    print(f"DRY_RUN          = {settings.dry_run}")
    print(f"would forward    = {'YES' if allowed else 'NO - ' + why}")
    if not targets:
        print("\nNo mirror credentials configured. Set MIRROR_1_API_KEY_ID and "
              "MIRROR_1_PRIVATE_KEY_PATH in .env")
        return 1

    results = [await check(t, settings.kalshi_base_url) for t in targets]
    ok = sum(results)
    print(f"\n{ok}/{len(results)} mirror account(s) usable.")
    return 0 if ok == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
