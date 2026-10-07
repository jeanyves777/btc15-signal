"""Turn ONE strategy on or off for ONE instance, without Telegram.

    python scripts/strategy_switch.py --db btc15.db                         # status
    python scripts/strategy_switch.py --db btc15.db --strategy main --off   # main -> shadow
    python scripts/strategy_switch.py --db gold15.db --strategy allsignal --on

main       the main strategy (`main.main_strategy_on`, row main_enabled). Off, it
           places no new entry and its shadow keeps recording (every poll's rule
           verdict and graded outcome in intelligence_decisions; alerts,
           predictions and outcomes as always). decision_records is the order
           path's own audit, so it is empty while paused. Open positions are
           still managed to the close.
allsignal  the ALL-SIGNAL $1 strategy (`main.allsignal_on`, row allsignal_enabled).

Both also need auto trading on (scripts/auto_switch.py / Telegram /auto), which
stays the kill switch for everything. Writes the row the service reads on every
decision, so it takes effect within one poll and survives a restart.
"""
import argparse
import sqlite3
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
KEYS = {"main": "main_enabled", "allsignal": "allsignal_enabled"}


def read(path: Path) -> dict:
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        rows = dict(con.execute(
            "SELECT key, value FROM settings WHERE key IN (?,?,?)",
            (*KEYS.values(), "auto_trade_enabled")).fetchall())
    finally:
        con.close()
    return rows


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--db", required=True)
    p.add_argument("--strategy", choices=sorted(KEYS))
    group = p.add_mutually_exclusive_group()
    group.add_argument("--on", action="store_true")
    group.add_argument("--off", action="store_true")
    args = p.parse_args()
    path = ROOT / args.db
    if not path.exists():
        raise SystemExit(f"no such database: {path}")
    if args.on or args.off:
        if not args.strategy:
            raise SystemExit("--strategy main|allsignal is required with --on/--off")
        con = sqlite3.connect(str(path), timeout=10)
        try:
            # The same statement Store.set_setting writes.
            con.execute("INSERT OR REPLACE INTO settings VALUES (?,?,?)",
                        (KEYS[args.strategy], 1.0 if args.on else 0.0,
                         int(time.time() * 1000)))
            con.commit()
        finally:
            con.close()
    rows = read(path)

    def state(key, default=1.0):
        value = rows.get(key, default)
        return ("ON" if value else "OFF") + ("" if key in rows else " (default)")

    print(f"{args.db}: auto trading {state('auto_trade_enabled', 0.0)} | "
          f"main strategy {state('main_enabled')} | all-signal $1 {state('allsignal_enabled')}")


if __name__ == "__main__":
    main()
