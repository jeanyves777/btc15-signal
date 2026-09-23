"""Mirror Kalshi's settlement record into the local database, and check it.

The service syncs this once a minute on its own. This script is for the
backfill - the history that settled before the mirror existed - and for
verifying, after any change to the money path, that what Telegram reports is
what the exchange actually did.

Realised P&L used to be rebuilt locally from `trade_proposals`. On 2026-09-22
that reconstruction reported **+1.06 on an account that was down -1.62**, and
counted 47 of 88 settled markets. Four separate reasons, each enough on its own:

  * `COALESCE(fill_price, entry_limit)` - a limit is permission to cross, never
    what was paid. Our own fills prove it: limit 0.87 filled 0.84.
  * the fee was modelled by formula instead of read from `fee_cost`
  * settlement came from `predictions.won`, our own guess, not the exchange's
  * ACCOUNTED_SQL excludes `pending`, and a proposal that fills but never
    reaches a terminal status stays `pending` for ever - 71 rows in one day

    python scripts/sync_settlements.py            # sync, then verify
    python scripts/sync_settlements.py --verify   # verify only
"""

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from btc15_signal.config import Settings  # noqa: E402
from btc15_signal.execution import KalshiExecutionClient  # noqa: E402
from btc15_signal.store import Store  # noqa: E402


async def main() -> None:
    verify_only = "--verify" in sys.argv
    settings = Settings()
    store = Store(settings.database_path)
    client = KalshiExecutionClient(
        settings.kalshi_base_url,
        settings.kalshi_api_key_id,
        settings.kalshi_private_key_path,
    )
    try:
        rows = await client.settlements()
        fills = await client.fills()
        print(f"Kalshi returned {len(rows)} settled markets and {len(fills)} fills.")
        if not verify_only:
            import time

            now = int(time.time() * 1000)
            print(f"mirrored {store.record_settlements(rows, now)} settlements, "
                  f"{store.record_fills(fills, now)} fills.")
            # The app's headline adds the open position marked to the bid, so
            # a verification run that skipped it would never match the screen.
            open_n, open_mark, per_ticker = await client.open_mark()
            store.set_setting("open_mark", open_mark, now)
            store.set_setting("open_positions", open_n, now)
            store.set_setting_text("open_mark_detail", json.dumps(per_ticker), now)
            store.sync_ledger_from_settlements(now)
            print(f"open positions: {open_n}, marked at {open_mark:+.4f}")

        live = sum(KalshiExecutionClient.settlement_pnl(r) for r in rows)
        markets, winners, dollars = store.exchange_record()
        print(f"\n  exchange, computed from the API   {live:+.4f}")
        print(f"  exchange, read from the mirror    {dollars:+.4f}"
              f"   ({winners}/{markets} markets in profit)")
        drift = abs(live - dollars)
        print(f"  drift                             {drift:.6f}"
              + ("   OK" if drift < 0.005 else "   MISMATCH"))

        trades, wins, reported = store.realised_record()
        executions, touched = store.executed_trades()
        print(f"\n  what Telegram will now report     {reported:+.4f}"
              f"   ({wins}/{trades})")
        print(f"  broker executions                 {executions} fills "
              f"across {touched} markets")

        print("\n  by MARKET day (window_ms, not settlement time):")
        for day, count, dollars in store.db.execute(
            "SELECT DATE(COALESCE(window_ms, settled_ms)/1000,'unixepoch'), "
            "COUNT(*), SUM(pnl) FROM settlements GROUP BY 1 ORDER BY 1"
        ):
            print(f"    {day}   {count:>3} markets   {dollars:+.4f}")

        balance = await client.client.get(
            client.base_url + "/portfolio/balance",
            headers=client._headers("GET", "/portfolio/balance"),
        )
        cash = float(balance.json()["balance_dollars"])
        print(f"  account balance                   ${cash:.4f}")
    finally:
        await client.close()


if __name__ == "__main__":
    asyncio.run(main())
