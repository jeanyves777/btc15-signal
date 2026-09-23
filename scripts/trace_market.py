"""Trace one market's complete lifecycle against the broker, by ID.

    .venv/Scripts/python.exe scripts/trace_market.py KXBTC15M-26SEP231615-15

READ-ONLY. It writes nothing, to the ledger or anywhere else. The point is to
check what a message SAID against what Kalshi RECORDS, not to make either one
agree with the other.

Written on 2026-09-23 after a recap read "Bought DOWN at 85c / Cost $1.72 /
Profit +$0.45" on a position that was really 2 @ 85c plus a recovery add of
1 @ 83c. Two contracts from 85c to 99.7c is 29.4c gross, so the message could
not produce its own profit figure - which is how the defect was caught, and
is worth being able to check in one command.

It prints, for one ticker:

  * every ORDER, with its status, resting time and fill counts;
  * every FILL, with its order id, so legs can be matched to orders;
  * the SETTLEMENT, which is the broker's own cost and result;
  * the local records beside them, and whether they agree.
"""
import asyncio
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from btc15_signal.config import Settings
from btc15_signal.execution import KalshiExecutionClient, parse_fill


def rule(title: str) -> None:
    print("=" * 72)
    print(title)
    print("=" * 72)


def local(settings: Settings, ticker: str) -> None:
    db = sqlite3.connect(f"file:{settings.database_path}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row

    rule("LOCAL: trade_proposals")
    for row in db.execute(
        "SELECT id, side, status, count, fill_price, fee_paid, exit_price, "
        "exit_count, entry_order_id FROM trade_proposals WHERE ticker = ? "
        "ORDER BY created_at", (ticker,),
    ):
        print(" ", dict(row))

    rule("LOCAL: recovery_adds")
    for row in db.execute(
        "SELECT client_order_id, state, order_id, limit_price, filled_count, "
        "fill_price, fee_paid, cancel_reason FROM recovery_adds "
        "WHERE ticker = ?", (ticker,),
    ):
        print(" ", dict(row))

    rule("LOCAL: fills")
    for row in db.execute(
        "SELECT order_id, action, side, count, yes_price, no_price, fee_cost, "
        "is_taker FROM fills WHERE ticker = ? ORDER BY filled_ms", (ticker,),
    ):
        print(" ", dict(row))

    rule("LOCAL: daily_ledger (the broker's P&L, never rebuilt here)")
    for row in db.execute(
        "SELECT pnl, won, source, revisions FROM daily_ledger WHERE ticker = ?",
        (ticker,),
    ):
        print(" ", dict(row))
    db.close()


async def broker(settings: Settings, ticker: str) -> None:
    client = KalshiExecutionClient(
        settings.kalshi_base_url, settings.kalshi_api_key_id,
        settings.kalshi_private_key_path,
    )
    try:
        rule("BROKER: orders")
        orders = await client._paginate("/portfolio/orders", "orders", 200)
        mine = [o for o in orders if o.get("ticker") == ticker]
        for order in sorted(mine, key=lambda r: str(r.get("created_time"))):
            full = await client.order_status(order["order_id"]) or order
            print(f"  {full.get('order_id')}  {full.get('status')}")
            print(f"     created {full.get('created_time')}")
            print(f"     updated {full.get('last_update_time')}")
            # A LONG GAP BETWEEN THEM IS A RESTING ORDER THAT FILLED LATE,
            # which is exactly the case a cancel can lose the race to.
            # THE ORDER'S OWN SIDE, not a guess. `outcome_side` says which
            # contract this order dealt in; labelling an exit that buys YES
            # as a DOWN leg is the kind of confusion this script exists to
            # remove rather than add.
            outcome = (full.get("outcome_side") or "").lower()
            side = "DOWN" if outcome == "no" else "UP"
            detail = parse_fill(full, side)
            if detail:
                print(f"     {outcome or '?'} leg: {detail}")
            else:
                print("     no fill")

        rule("BROKER: fills")
        fills = await client._paginate("/portfolio/fills", "fills", 300)
        for fill in sorted((f for f in fills if f.get("ticker") == ticker),
                           key=lambda r: str(r.get("created_time"))):
            print(f"  {fill.get('created_time')}  order {fill.get('order_id')}"
                  f"  {fill.get('action')}/{fill.get('side')}"
                  f"  taker={fill.get('is_taker')}")

        rule("BROKER: settlement")
        setts = await client._paginate(
            "/portfolio/settlements", "settlements", 200)
        for s in setts:
            if s.get("ticker") == ticker:
                print(json.dumps(s, indent=2, default=str))
                # The one line worth doing by hand.
                no_cost = float(s.get("no_total_cost_dollars") or 0)
                yes_cost = float(s.get("yes_total_cost_dollars") or 0)
                fee = float(s.get("fee_cost") or 0)
                print(f"\n  cost paid: ${no_cost + yes_cost + fee:.4f} "
                      f"(no {no_cost} + yes {yes_cost} + fees {fee})")
    finally:
        await client.close()


def main() -> None:
    if len(sys.argv) < 2:
        print(__doc__)
        raise SystemExit(2)
    ticker = sys.argv[1]
    settings = Settings()
    local(settings, ticker)
    asyncio.run(broker(settings, ticker))
    print()
    print("Check the message against these: quantity x price, plus fees, must")
    print("produce the P&L the recap claimed. If it cannot, the message is")
    print("missing a leg - the ledger is the broker's and is not the suspect.")


if __name__ == "__main__":
    main()
