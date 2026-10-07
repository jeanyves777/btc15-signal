"""Turn the ALL-SIGNAL $1 strategy on or off for ONE instance, without Telegram.

The all-signal strategy (FINDINGS 111) buys every signal of a listed instrument
for $1, beside the main strategy. It also stops with /auto off (the kill switch
stops everything); this switch stops ONLY it, leaving the main strategy alone.
It writes the row `main.allsignal_on` reads on every signal, so it takes effect
within one poll and survives a restart.

    python scripts/allsignal_switch.py --db btc15.db            # status
    python scripts/allsignal_switch.py --db btc15.db --off
    python scripts/allsignal_switch.py --db gold15.db --on
"""
import argparse
import sqlite3
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
KEY = "allsignal_enabled"


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--db", required=True)
    group = p.add_mutually_exclusive_group()
    group.add_argument("--on", action="store_true")
    group.add_argument("--off", action="store_true")
    args = p.parse_args()
    path = ROOT / args.db
    if not path.exists():
        raise SystemExit(f"no such database: {path}")
    if args.on or args.off:
        con = sqlite3.connect(str(path), timeout=10)
        try:
            # The same statement Store.set_setting writes.
            con.execute("INSERT OR REPLACE INTO settings VALUES (?,?,?)",
                        (KEY, 1.0 if args.on else 0.0, int(time.time() * 1000)))
            con.commit()
        finally:
            con.close()
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        row = con.execute("SELECT value FROM settings WHERE key=?", (KEY,)).fetchone()
    finally:
        con.close()
    state = "ON (default)" if row is None else ("ON" if row[0] else "OFF")
    print(f"{args.db}: all-signal $1 strategy {state} - it also needs the "
          f"instrument on allsignal_instruments and auto trading on")


if __name__ == "__main__":
    main()
