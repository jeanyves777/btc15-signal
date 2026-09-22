"""Quarantine the corrupted shadow rows, then prove the new ones are clean.

Two jobs, in order, because the second is only meaningful after the first:

  1. mark every column-shifted legacy row permanently. 93 of 98 rows were
     written by a positional insert whose tuple order stopped matching the
     table once `dip_n` was appended by a migration.
  2. check every row written SINCE the named-column fix against the invariants
     that corruption violates - a canary, run after a few windows have passed,
     that says whether the repair actually took in production rather than only
     in the test suite.

The canary matters more than it sounds. The original bug survived for 98 rows
because the only column anybody inspected - `won` - was being repaired in place
by `settle_shadow`'s named UPDATE while everything around it stayed wrong. A
fix verified by reading that same column would have looked fine too.

    python scripts/shadow_canary.py            # quarantine + check
    python scripts/shadow_canary.py --check    # check only
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from btc15_signal.config import Settings  # noqa: E402
from btc15_signal.store import Store  # noqa: E402

SESSIONS = {"asia", "eu", "us", "late-us", "off", "overnight", "unknown"}
REGIMES = {"low", "mid", "high", "unknown"}


def check(row: dict) -> list[str]:
    """Everything wrong with one row. Empty means it is structurally sound.

    Checks TYPES AND DOMAINS, not just presence: a shifted row is fully
    populated, it is simply populated with the neighbouring column's value, so
    "is not null" would pass every corrupted row in the archive.
    """
    problems = []
    session = row.get("session")
    if session is not None and session not in SESSIONS:
        problems.append(f"session={session!r} not a known session")
    regime = row.get("vol_regime")
    if regime is not None and regime not in REGIMES:
        problems.append(f"vol_regime={regime!r} not a known regime")
    won = row.get("won")
    if won is not None and won not in (0, 1):
        problems.append(f"won={won!r} is not boolean")
    for name in ("win_probability", "raw_win_rate", "win_low", "win_high", "prior"):
        value = row.get(name)
        if value is not None and not (0.0 <= float(value) <= 1.0):
            problems.append(f"{name}={value!r} outside [0,1]")
    action = row.get("action")
    if action is not None and action not in ("ENTER NOW", "WAIT", "PASS", "UNCERTAIN"):
        problems.append(f"action={action!r} unrecognised")
    n = row.get("cohort_n")
    if n is not None and not (0 <= int(n) <= 1000):
        problems.append(f"cohort_n={n!r} implausible")
    return problems


def main() -> None:
    settings = Settings()
    store = Store(settings.database_path)

    if "--check" not in sys.argv:
        marked = store.quarantine_shifted_shadow_rows()
        print(f"quarantined {marked} newly-detected shifted row(s)")

    total = store.db.execute("SELECT COUNT(*) FROM shadow_decisions").fetchone()[0]
    held = store.db.execute(
        "SELECT COUNT(*) FROM shadow_decisions WHERE COALESCE(quarantined,0)=1"
    ).fetchone()[0]
    clean = store.clean_shadow_rows()
    print(f"\narchive: {total} rows, {held} quarantined, {len(clean)} clean")

    # The canary: only rows the named insert wrote can vindicate the fix.
    suspect = [(r, check(r)) for r in clean]
    broken = [(r, p) for r, p in suspect if p]
    print(f"clean rows failing the structural checks: {len(broken)}")
    for row, problems in broken[:5]:
        print(f"  window {row.get('window_open')}: {'; '.join(problems)}")

    if not clean:
        print("\nNo clean rows yet. Let the service run a few windows and "
              "re-run with --check.")
    elif broken:
        print("\nCANARY FAILED - corruption is still being written. Do not "
              "trust any shadow analysis.")
    else:
        newest = max(r.get("created_at") or 0 for r in clean)
        print(f"\nCANARY PASSED - {len(clean)} clean rows, newest "
              f"{newest}. Rows written since the named-column fix are sound.")
        print("Note: a PASS on a handful of rows says the writer is correct, "
              "not that\nthere is enough evidence to promote anything.")


if __name__ == "__main__":
    main()
