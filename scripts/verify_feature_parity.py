"""Do the historical and live BRTI features agree on the SAME market?

Context parity - both sides calling `brti_context_of` - only guarantees the
band LABELS come from one function. It says nothing about the numbers fed to
them. If the live path computed volatility over a different lookback, at a
different cadence, or from a differently-smoothed series, every label would
still be produced by the agreed function and every one could still be wrong.

Distribution comparison cannot settle it either: live covers ~2 days and the
corpus 68, so a difference in medians is confounded with regime.

So this recomputes, market by market. For each feature row the LIVE path
stored, it fetches the series the way `backfill_brti.py` does, truncates to
the same instant, calls `features_from_series` with the same arguments, and
compares against the value the live path actually recorded. Identical inputs
through identical code must give identical outputs; any disagreement is a
real difference between the two paths.

    python scripts/verify_feature_parity.py [--limit 40]
"""

import argparse
import asyncio
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from btc15_signal.brti import KalshiBRTI, features_from_series  # noqa: E402
from btc15_signal.config import Settings  # noqa: E402

# The live table names the reference price `brti_value`; the dataclass calls
# it `value`. Same number, two column names.
FIELDS = {
    "brti_volatility_bps": "brti_volatility_bps",
    "brti_momentum_bps": "brti_momentum_bps",
    "brti_normalized_distance": "brti_normalized_distance",
    "signed_distance_bps": "signed_distance_bps",
    "brti_value": "value",
}
# Floating-point reassociation only. Anything larger is a real difference.
TOLERANCE = 1e-9


def live_rows(limit: int) -> list[dict]:
    db = sqlite3.connect("file:runtime/settlement_reference.db?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    return [
        dict(r) for r in db.execute(
            "SELECT * FROM brti_features WHERE remaining_s BETWEEN 360 AND 660 "
            "AND event_ticker IS NOT NULL AND target IS NOT NULL "
            "ORDER BY received_ms DESC LIMIT ?",
            (limit,),
        )
    ]


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=40)
    args = parser.parse_args()

    rows = live_rows(args.limit)
    if not rows:
        print("no live brti_features rows to check")
        return
    print(f"checking {len(rows)} live rows against a historical recomputation")

    settings = Settings()
    client = KalshiBRTI(settings.kalshi_base_url, timeout=25)
    cache: dict[str, list] = {}
    checked = agreed = 0
    worst: dict[str, float] = {f: 0.0 for f in FIELDS}
    unavailable = 0

    try:
        for row in rows:
            event = row["event_ticker"]
            if event not in cache:
                try:
                    cache[event] = sorted(await client.series(event))
                except Exception as exc:  # noqa: BLE001
                    print(f"  {event}: {type(exc).__name__}")
                    cache[event] = []
            series = cache[event]
            # The backfill's own truncation: every point at or before the
            # instant the live path computed its features.
            upto = [(t, v) for t, v in series if t <= row["ts_ms"]]
            if len(upto) < 120:
                unavailable += 1
                continue
            recomputed = features_from_series(
                event, upto, row["target"], row["ts_ms"],
            )
            if recomputed is None:
                unavailable += 1
                continue
            checked += 1
            deltas = {
                column: abs((getattr(recomputed, attr) or 0.0) - (row[column] or 0.0))
                for column, attr in FIELDS.items()
            }
            for f, d in deltas.items():
                worst[f] = max(worst[f], d)
            if max(deltas.values()) <= TOLERANCE:
                agreed += 1
            else:
                print(f"  MISMATCH {row['ticker']} @{row['remaining_s']}s: " + ", ".join(
                    f"{f} live={row[f]!r} recomputed={getattr(recomputed, FIELDS[f])!r}"
                    for f, d in deltas.items() if d > TOLERANCE
                ))
    finally:
        close = getattr(client, "close", None)
        if close:
            result = close()
            if asyncio.iscoroutine(result):
                await result

    print(f"\ncompared {checked} rows ({unavailable} without a usable series)")
    print(f"identical: {agreed}/{checked}")
    for f in FIELDS:
        print(f"  max |live - recomputed|  {f:<26} {worst[f]:.3e}")
    if checked and agreed == checked:
        print("\nPARITY: the two paths produce the same numbers on the same "
              "market. Formula, sampling, smoothing and lookback agree.")
    else:
        print("\nNOT AT PARITY - the difference above is real, not rounding.")


if __name__ == "__main__":
    asyncio.run(main())
