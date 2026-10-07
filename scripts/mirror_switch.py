"""Turn ONE mirror account on or off for ONE instrument, without Telegram.

Operator, 2026-09-28: "make the wife mirror account or any other mirror
account follow an on/off flag per asset ... now I want it to only trade BTC".

    python scripts/mirror_switch.py                                   # status
    python scripts/mirror_switch.py --mirror wife --asset GOLD --off
    python scripts/mirror_switch.py --mirror george --asset BTC --on

`wife` is m1; `george` (or `uncle`) is m2, Uncle George's account. Writes row mirror_<m1|m2>_enabled in that instrument's store,
which the service reads on every mirrored order (`main.mirror_on`): it takes
effect on the next entry with no restart, and survives one. OFF stops NEW
positions on that account for that instrument; a position it already holds is
still cashed out or closed. Nothing is copied at all unless the instrument is
also in MIRROR_INSTANCES (.env) - the status says so where it is not.
"""
import argparse
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from btc15_signal.config import Settings  # noqa: E402
from btc15_signal.shadow_summary import STORES  # noqa: E402

ALIASES = {"wife": "m1", "m1": "m1", "george": "m2", "uncle": "m2", "m2": "m2",
           "m3": "m3", "3": "m3"}
LABEL = {"m1": "wife", "m2": "m2", "m3": "m3"}  # replaced by the .env/config names below


def mirrors(settings) -> list[str]:
    """The mirror accounts configured in .env (a key id AND a key path)."""
    return [f"m{i}" for i in (1, 2, 3)
            if (getattr(settings, f"mirror_{i}_api_key_id", "") or "").strip()
            and (getattr(settings, f"mirror_{i}_private_key_path", "") or "").strip()]


def read_row(path: Path, key: str):
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        row = con.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    finally:
        con.close()
    return None if row is None else row[0]


def labels(settings) -> None:
    for i in (1, 2, 3):
        LABEL[f"m{i}"] = getattr(settings, f"mirror_{i}_label", "") or LABEL[f"m{i}"]


def status(root: Path, settings) -> None:
    listed = {x.strip().lower() for x in (settings.mirror_instances or "").split(",")
              if x.strip()}
    names = mirrors(settings)
    print(f"mirroring {'ON' if settings.mirror_enabled else 'OFF'} in .env; "
          f"MIRROR_INSTANCES={settings.mirror_instances or '(empty)'}; "
          f"accounts: {', '.join(f'{n} ({LABEL[n]})' for n in names) or 'none'}")
    for asset, db in STORES.items():
        path = root / db
        if not path.exists():
            continue
        instance = "btc" if asset == "BTC" else asset.lower()
        in_env = instance in listed or (
            asset == "BTC" and bool(listed & {"primary", "default"}))
        auto = read_row(path, "auto_trade_enabled")
        cells = []
        for n in names:
            v = read_row(path, f"mirror_{n}_enabled")
            state = "on" if v is None or v else "OFF"
            cells.append(f"{LABEL[n]} {state}{'' if v is not None else ' (default)'}")
        note = "" if in_env else "  <- not in MIRROR_INSTANCES: copies nothing"
        print(f"  {asset:6s} auto {'on ' if auto else 'off'} | {' | '.join(cells)}{note}")


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--mirror", choices=sorted(ALIASES))
    p.add_argument("--asset", choices=sorted(STORES))
    group = p.add_mutually_exclusive_group()
    group.add_argument("--on", action="store_true")
    group.add_argument("--off", action="store_true")
    p.add_argument("--root", default=str(ROOT), help=argparse.SUPPRESS)
    args = p.parse_args()
    root = Path(args.root)
    settings = Settings()
    labels(settings)
    if args.on or args.off:
        if not (args.mirror and args.asset):
            raise SystemExit("--mirror and --asset are required with --on/--off")
        path = root / STORES[args.asset]
        if not path.exists():
            raise SystemExit(f"no such database: {path}")
        name = ALIASES[args.mirror]
        con = sqlite3.connect(str(path), timeout=10)
        try:
            # The same statement Store.set_setting writes.
            con.execute("INSERT OR REPLACE INTO settings VALUES (?,?,?)",
                        (f"mirror_{name}_enabled", 1.0 if args.on else 0.0,
                         int(time.time() * 1000)))
            con.commit()
        finally:
            con.close()
        print(f"{LABEL[name]} ({name}) on {args.asset}: {'ON' if args.on else 'OFF'}")
    status(root, settings)


if __name__ == "__main__":
    main()
