"""Turn auto trading on or off for ONE instance, without Telegram.

WHY THIS HAS TO EXIST BEFORE SOL TRADES. `/auto off` is the kill switch, and it
arrives over Telegram - but only ONE instance may consume the command stream,
because `getUpdates` is destructive and a second poller silently steals messages
from the first, up to and including the kill switch. BTC is that listener; ETH,
gold, silver and SOL all run with `TELEGRAM_COMMANDS_ENABLED=false`.

That is correct for an instance that only ever SENDS alerts. It is not
acceptable for one placing real orders unattended: enabling auto trading on an
instance that cannot hear `/auto off` would leave the daily loss limit and
`Stop-Process` as the only ways to stop it. So the same switch is reachable
here, from the machine.

IT WRITES THE SAME ROW THE SERVICE READS. `main.auto_is_on` calls
`store.get_setting("auto_trade_enabled", default)` on every decision, and the
STORED value wins over the .env default - that is the existing design, so that
`/auto off` from a phone cannot be undone by a restart. Writing that row here
therefore takes effect within one poll, with no restart and no code path of its
own that could drift from the one the service trusts.

    python scripts/auto_switch.py --db sol15.db            # status
    python scripts/auto_switch.py --db sol15.db --off      # stop now
    python scripts/auto_switch.py --db sol15.db --on       # start

Turning it OFF is unconditional. Turning it ON refuses unless execution is
actually configured, for the same reason the Telegram command does: an "on"
that silently does nothing is worse than an error, because you would believe it
was trading.
"""

import argparse
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from btc15_signal.config import Settings  # noqa: E402


def read(db: str):
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        row = con.execute(
            "SELECT value, updated_at FROM settings "
            " WHERE key='auto_trade_enabled'").fetchone()
    except sqlite3.OperationalError:
        row = None
    finally:
        con.close()
    return row


def write(db: str, value: float) -> None:
    con = sqlite3.connect(db)
    try:
        # EXACTLY what `Store.set_setting` writes - same statement, same
        # three columns, same order - so this cannot drift from the row the
        # service reads. The column is `updated_at`, holding epoch ms.
        con.execute(
            "INSERT OR REPLACE INTO settings VALUES (?,?,?)",
            ("auto_trade_enabled", float(value), int(time.time() * 1000)))
        con.commit()
    finally:
        con.close()


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--db", required=True,
                   help="the instance database, e.g. sol15.db")
    group = p.add_mutually_exclusive_group()
    group.add_argument("--on", action="store_true")
    group.add_argument("--off", action="store_true")
    args = p.parse_args()

    path = ROOT / args.db
    if not path.exists():
        raise SystemExit(f"no such database: {path}")

    if args.off:
        write(str(path), 0.0)
        print(f"AUTO TRADING OFF for {args.db}. No further orders will be "
              f"placed; the service picks this up on its next decision.")
        return

    if args.on:
        settings = Settings()
        if not settings.execution_enabled:
            raise SystemExit(
                "refusing: EXECUTION_ENABLED is false, so an 'on' here would "
                "place no orders while reading as armed")
        if not (settings.kalshi_api_key_id
                and settings.kalshi_private_key_path):
            raise SystemExit(
                "refusing: no Kalshi API credentials configured, so an 'on' "
                "here would place no orders while reading as armed")
        write(str(path), 1.0)
        print(f"AUTO TRADING ON for {args.db}.")

    row = read(str(path))
    if row is None:
        print(f"{args.db}: no stored flag - the service falls back to the "
              f".env default for its instance")
    else:
        state = "ON" if row[0] else "OFF"
        when = time.strftime("%Y-%m-%d %H:%M:%S",
                             time.localtime((row[1] or 0) / 1000))
        print(f"{args.db}: auto trading {state} (stored, set {when})")


if __name__ == "__main__":
    main()
