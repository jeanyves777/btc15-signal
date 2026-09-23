"""Backfill every reachable settlement, and decide whether it IS a lifetime.

The operator's rule: verify the figures against the complete available broker
history, and if that history is incomplete, say "Since <date>" rather than
"Lifetime". A total that silently omits earlier trading is not a lifetime, and
labelling it one is the kind of small overstatement that makes every other
number suspect.

Completeness is decided by asking the broker two different questions:

  * `/portfolio/settlements` - what the live endpoint will serve
  * `/historical/fills`      - trading older than the live window

If the historical endpoint knows about fills before our earliest settlement,
the record does NOT reach the account's first trade, and the label becomes
"Live since <date>".

    python scripts/verify_lifetime.py
"""

import asyncio
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from btc15_signal.config import Settings  # noqa: E402
from btc15_signal.execution import KalshiExecutionClient  # noqa: E402
from btc15_signal.store import Store  # noqa: E402


def iso_to_ms(value: str) -> int:
    if not value:
        return 0
    return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() * 1000)


async def main() -> None:
    settings = Settings()
    store = Store(settings.database_path)
    client = KalshiExecutionClient(
        settings.kalshi_base_url, settings.kalshi_api_key_id,
        settings.kalshi_private_key_path,
    )
    now = int(time.time() * 1000)

    # 1. Mirror everything the live endpoint will serve, then fold it.
    rows = await client.settlements(limit=200)
    mirrored = store.record_settlements(rows, now)
    print(f"settlements from the broker: {len(rows)} ({mirrored} new)")

    # The ledger sync looks back a bounded window; for a lifetime figure the
    # whole mirror is folded, with the recovery epoch still protecting the
    # deficit from history (see `sync_ledger_from_settlements`).
    epoch = store.recovery_epoch_ms()
    for row in store.db.execute(
        "SELECT ticker, COALESCE(window_ms, settled_ms), pnl, settled_ms "
        "FROM settlements"
    ).fetchall():
        ticker, window_ms, pnl, settled_ms = row
        store.record_realised(
            ticker, int(window_ms), float(pnl), float(pnl) > 0, "exchange", now,
            realised_ms=int(settled_ms) if settled_ms else None,
        )
        if epoch and int(window_ms) < epoch:
            store.db.execute(
                "UPDATE daily_ledger SET recovery_applied = pnl "
                "WHERE ticker = ? AND recovery_applied IS NULL",
                (ticker,),
            )
    store.db.commit()

    # 2. Does anything predate our earliest record?
    earliest_ms = store.db.execute(
        "SELECT MIN(COALESCE(window_ms, first_ms)) FROM daily_ledger"
    ).fetchone()[0]
    older = None
    with httpx.Client(timeout=30) as http:
        path = "/historical/fills"
        try:
            response = http.get(
                settings.kalshi_base_url + path, params={"limit": 200},
                headers=client._headers("GET", path),
            )
            response.raise_for_status()
            times = [
                iso_to_ms(f.get("created_time", ""))
                for f in (response.json().get("fills") or [])
            ]
            times = [t for t in times if t]
            older = min(times) if times else None
        except httpx.HTTPError as exc:
            print(f"historical fills unavailable ({type(exc).__name__}) - "
                  f"treating the record as incomplete")

    complete = bool(earliest_ms) and (older is None or older >= earliest_ms)
    store.set_setting("history_complete", 1.0 if complete else 0.0, now)

    record = store.lifetime_record()
    fmt = lambda ms: datetime.fromtimestamp(ms / 1000, UTC).strftime("%Y-%m-%d %H:%M")  # noqa: E731
    print(f"\nledger markets      {record.markets}")
    print(f"earliest in record  {fmt(earliest_ms) if earliest_ms else '-'}")
    print(f"oldest broker fill  {fmt(older) if older else 'none found'}")
    print(f"complete history    {complete}")
    print(f"\nlabel               {record.label()}")
    print(f"realised            {record.dollars:+.4f} over {record.markets} markets "
          f"({record.winners}W-{record.losers}L)")
    await client.close()


if __name__ == "__main__":
    asyncio.run(main())
