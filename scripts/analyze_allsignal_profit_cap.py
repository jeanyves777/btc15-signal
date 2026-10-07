"""Replay today's profit-only cap over a saved, read-only broker snapshot.

Primary opening capital is archived. Mirror opening capital is an estimate,
conditional on no external cash flows today and flat snapshot holdings.
Uses actual sizes and final net market P&L at full exit or settlement.
"""
import json
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from btc15_signal.execution import KalshiExecutionClient as K
from analyze_daily_profit_cap import replay

NY = ZoneInfo("America/New_York")


def stamp(value):
    return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() * 1000)


def day(ms):
    return datetime.fromtimestamp(ms / 1000, NY).strftime("%Y-%m-%d")


def main():
    raw = json.loads((ROOT / "reports/allsignal_cap_broker_snapshot_2026-09-29.json").read_text())
    result = {}
    for name, account in raw["accounts"].items():
        fills = defaultdict(list)
        for fill in account["fills"]:
            fills[fill["ticker"]].append(fill)
        events = []
        for settlement in account["settlements"]:
            ticker = settlement["ticker"]
            ordered = sorted(fills.get(ticker, []), key=lambda f: f["created_time"])
            if not ordered:
                continue
            position = 0.0
            flat_at = None
            for fill in ordered:
                position += ((1 if fill["side"] == "yes" else -1)
                             * (1 if fill["action"] == "buy" else -1)
                             * float(fill["count_fp"]))
                if abs(position) < 1e-8:
                    flat_at = stamp(fill["created_time"])
            end = (flat_at if abs(position) < 1e-8 and flat_at
                   else stamp(settlement["settled_time"]))
            pnl = K.settlement_pnl(settlement)
            events.append((end, 0, ticker, pnl, int(pnl > 0)))
        for ticker, rows in fills.items():
            events.append((min(stamp(f["created_time"]) for f in rows), 1, ticker, None, None))
        events.sort()
        opening = (109.3504 if name == "primary" else account["cash"] - sum(
            e[3] for e in events if e[1] == 0 and day(e[0]) == "2026-09-29"))
        capital = {"2026-09-29": opening}
        result[name] = {
            "opening": opening,
            "opening_basis": "archived capital" if name == "primary" else "conditional estimate",
            "baseline": replay(events, capital),
            "targets": {str(p): replay(events, capital, p / 100) for p in (3, 4, 5)},
        }
    print(json.dumps({"asof": raw["asof"], "accounts": result}, indent=2))


if __name__ == "__main__":
    main()
