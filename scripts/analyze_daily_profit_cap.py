"""Read-only paper replay of daily entry pauses over recorded rule signals.

One contract at the first qualified ask per instrument/market. Outcomes become
available at recorded graded_ms. Uses observed primary capital by day, not a
compounded simulated account; mirror opening capital is not archived.
"""
import json
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from btc15_signal.validation import kalshi_fee_charged

NY = ZoneInfo("America/New_York")
START = int(datetime(2026, 9, 24, tzinfo=NY).timestamp() * 1000)


def day(ms):
    return datetime.fromtimestamp(ms / 1000, NY).strftime("%Y-%m-%d")


def read_inputs():
    events = []
    for instrument in ("btc", "eth", "gold", "sol", "silver", "xrp", "near"):
        with sqlite3.connect(f"file:{ROOT / (instrument + '15.db')}?mode=ro", uri=True) as db:
            db.row_factory = sqlite3.Row
            rows = db.execute("""
                WITH ranked AS (
                    SELECT *, ROW_NUMBER() OVER (
                        PARTITION BY ticker ORDER BY decided_ms,id) AS rn
                    FROM intelligence_decisions
                    WHERE window_open>=? AND base_qualified=1)
                SELECT * FROM ranked WHERE rn=1
            """, (START,)).fetchall()
        for row in rows:
            if row["ask"] is None or not 0 < row["ask"] < 1:
                raise ValueError("Missing or invalid qualifying ask")
            key = row["ticker"]
            events.append((row["decided_ms"], 1, key, None, None))
            if row["won"] is not None:
                if row["graded_ms"] is None or row["graded_ms"] < row["decided_ms"]:
                    raise ValueError("Invalid grading chronology")
                pnl = row["won"] - row["ask"] - kalshi_fee_charged(row["ask"])
                events.append((row["graded_ms"], 0, key, pnl, row["won"]))
    with sqlite3.connect(f"file:{ROOT / 'btc15.db'}?mode=ro", uri=True) as db:
        capital = {r[0]: r[1] for r in db.execute(
            "SELECT ny_day,reconciled_cash FROM capital_days WHERE ny_day>='2026-09-25'")}
    return sorted(events), capital


def replay(events, capital, target=None, win_floor=0, min_settled=1):
    days = defaultdict(lambda: {"pnl": 0.0, "wins": 0, "settled": 0,
                               "skipped": 0, "stop": None})
    entered = set()
    closed = set()
    for ms, kind, key, pnl, won in events:
        d = day(ms)
        state = days[d]
        if kind == 1:
            if state["stop"] is None:
                entered.add(key)
            else:
                state["skipped"] += 1
        elif key in entered:
            closed.add(key)
            state["pnl"] += pnl
            state["wins"] += won
            state["settled"] += 1
            if (target is not None and d in capital and state["stop"] is None
                    and state["settled"] >= min_settled
                    and state["wins"] / state["settled"] >= win_floor
                    and state["pnl"] >= capital[d] * target):
                state["stop"] = {
                    "time": datetime.fromtimestamp(ms / 1000, NY).isoformat(),
                    "pnl": state["pnl"], "settled": state["settled"],
                    "win_rate": state["wins"] / state["settled"],
                }
    return {"days": dict(days), "open": len(entered - closed),
            "total_supported_days": sum(s["pnl"] for d, s in days.items() if d in capital)}


def main():
    events, capital = read_inputs()
    result = {"asof": datetime.now(NY).isoformat(), "opening_capital": capital,
              "baseline": replay(events, capital), "scenarios": {}}
    for pct in (3, 4, 5):
        for label, floor, minimum in (("profit_only", 0, 1), ("win80", .8, 1),
                                       ("win80_min5", .8, 5)):
            result["scenarios"][f"{pct}pct_{label}"] = replay(events, capital, pct / 100, floor, minimum)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
