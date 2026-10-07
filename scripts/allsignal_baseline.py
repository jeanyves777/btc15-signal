"""The account value each account had when the all-signal $1 strategy started.

Kalshi keeps no balance history, so it is REBUILT from the broker's own record:
value now (cash + open positions at cost) minus the settled P&L of every market
that opened at or after the start. Both accounts were flat at their start
(restarts at window opens after a broker flat check), so the value then was
cash. Writes the result to settings_text 'allsignal_start_balances' in the
given store, where the session summary reads it (FINDINGS 111).

    python scripts/allsignal_baseline.py                 # compute and print
    python scripts/allsignal_baseline.py --write         # ...and store it
"""
import argparse
import asyncio
import datetime as dt
import json
import sqlite3
import sys
import time
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from btc15_signal.config import Settings  # noqa: E402
from btc15_signal.execution import KalshiExecutionClient  # noqa: E402

NY = ZoneInfo("America/New_York")
# BOTH from 15:45: when the main strategy was paused and the $1 strategy ran
# ALONE on both accounts (14:15-15:45 still carried the old strategy's trades).
STARTS = {"You": dt.datetime(2026, 9, 28, 15, 45, tzinfo=NY),
          "Wife": dt.datetime(2026, 9, 28, 15, 45, tzinfo=NY)}


async def value_and_since(key: str, pem: str, start_ms: int):
    s = Settings()
    c = KalshiExecutionClient(s.kalshi_base_url, key, pem)
    try:
        cash = await c.balance_dollars()
        r = await c.client.get(c.base_url + "/portfolio/positions?limit=200",
                               headers=c._headers("GET", "/portfolio/positions"))
        r.raise_for_status()
        open_cost = sum(float(p.get("market_exposure_dollars") or 0)
                        for p in (r.json().get("market_positions") or [])
                        if float(p.get("position_fp") or 0) != 0)
        settled = 0.0
        n = 0
        for row in await c.settlements():
            opened = KalshiExecutionClient.market_open_ms(row.get("ticker"))
            if opened is not None and opened >= start_ms:
                settled += KalshiExecutionClient.settlement_pnl(row)
                n += 1
        return cash, open_cost, settled, n
    finally:
        await c.close()


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true")
    ap.add_argument("--db", default="btc15.db")
    args = ap.parse_args()
    s = Settings()
    accounts = {"You": (s.kalshi_api_key_id, s.kalshi_private_key_path),
                "Wife": (s.mirror_1_api_key_id, s.mirror_1_private_key_path)}
    out = {}
    for label, (key, pem) in accounts.items():
        start_ms = int(STARTS[label].timestamp() * 1000)
        cash, open_cost, settled, n = await value_and_since(key, pem, start_ms)
        now_value = cash + open_cost
        start_value = round(now_value - settled, 2)
        out[label] = {"value": start_value, "at_ms": start_ms}
        print(f"{label}: now ${now_value:.2f} (cash {cash:.2f} + open {open_cost:.2f}); "
              f"settled since {STARTS[label]:%H:%M} ET: {settled:+.2f} over {n} markets "
              f"-> started ${start_value:.2f}")
    if args.write:
        con = sqlite3.connect(str(ROOT / args.db), timeout=10)
        try:
            con.execute("INSERT OR REPLACE INTO settings_text VALUES (?,?,?)",
                        ("allsignal_start_balances", json.dumps(out),
                         int(time.time() * 1000)))
            con.commit()
        finally:
            con.close()
        print(f"stored in {args.db}: {json.dumps(out)}")


if __name__ == "__main__":
    asyncio.run(main())
