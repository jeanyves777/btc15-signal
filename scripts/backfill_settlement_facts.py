"""Fill in Kalshi's target and settling value for settled markets already stored.

WHAT THIS ADDS TO THE LIFECYCLE. The record said whether a trade won and what it
paid. It did not say BY HOW MUCH, and that margin is what separates an 83c
favourite settling comfortably from one that settled by a hair. Kalshi publishes
both numbers on the market object:

    floor_strike       the target price the signal message quotes
    expiration_value   where the settling BRTI actually finished

Measured across 143 BTC and 14 ETH executed trades before this existed, winners
finished a mean **18.3 bps** past the target and losers fell **6.4 / 6.5 bps**
short - the same figure in bps on two instruments 20x apart in price, which is
why it is stored raw and reported in bps rather than dollars.

FROM THE BROKER, not from the local reference archive. `settlement_reference.db`
holds `official_strike` and `official_expiration_value` too, and it would have
been quicker to read - but the archive is ours and the settlement is Kalshi's,
and where they disagree the broker is right by definition. The same rule already
governs P&L.

Resumable: `facts_synced_ms` marks a market as asked, so an interruption or a
rate limit costs only the markets still missing, and a re-run is free. The live
service enriches each new settlement as it arrives; this exists for history.

    python scripts/backfill_settlement_facts.py
    python scripts/backfill_settlement_facts.py --db eth15.db
"""

import argparse
import sys
import time
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from btc15_signal.store import Store  # noqa: E402

BASE = "https://external-api.kalshi.com/trade-api/v2"


def number(value):
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


def facts(client: httpx.Client, ticker: str, tries: int = 4) -> dict:
    """Kalshi's own account of how the market resolved. Public, unsigned."""
    for attempt in range(tries):
        try:
            response = client.get(f"{BASE}/markets/{ticker}")
        except httpx.HTTPError:
            time.sleep(1.0 * (attempt + 1))
            continue
        if response.status_code == 429:
            time.sleep(1.5 * (attempt + 1))
            continue
        if response.status_code != 200:
            return {}
        market = response.json().get("market") or {}
        return {
            "strike": number(market.get("floor_strike")),
            "expiration_value": number(market.get("expiration_value")),
        }
    return {}


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--db", default="btc15.db")
    p.add_argument("--batch", type=int, default=250)
    args = p.parse_args()

    store = Store(args.db)
    todo = store.settlements_missing_facts(args.batch)
    print(f"{args.db}: {len(todo)} settled markets missing target/value")
    if not todo:
        return

    filled = blank = 0
    with httpx.Client(timeout=30) as client:
        for index, ticker in enumerate(todo, 1):
            got = facts(client, ticker)
            store.record_settlement_facts(
                ticker, got.get("strike"), got.get("expiration_value"),
                int(time.time() * 1000),
            )
            if got.get("strike") is not None and \
                    got.get("expiration_value") is not None:
                filled += 1
            else:
                # Marked as asked regardless, so it is not retried forever.
                blank += 1
            if index % 50 == 0:
                print(f"  {index}/{len(todo)}", flush=True)

    remaining = len(store.settlements_missing_facts(10_000))
    print(f"enriched {filled}, no data for {blank}, {remaining} still missing")


if __name__ == "__main__":
    main()
