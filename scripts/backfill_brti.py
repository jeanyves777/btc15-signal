"""Backfill BRTI decision points so the gates can be measured on BRTI.

FINDINGS 43: the deployed thresholds were calibrated on Binance raw volatility
and describe a different quantity on a 60-second mean - `normalized_distance`
reads 15-22 on BRTI where Binance reads 2-4. They cannot be ported, so they
have to be re-measured, and that needs BRTI history at every decision minute.

`/live_data/events/{event_ticker}` serves it for settled events too: 3,601
points, one per second, ending exactly at the event close. That covers the
whole 15-minute window plus the 45 minutes before it, at ordinary request cost,
for every market in the corpus.

Only the DERIVED features are stored, not the raw series - 3,601 points across
6,435 markets is 23 million rows to answer a question about six decision
minutes each. Resumable: a market already stored is skipped, so a rate limit or
an interruption costs nothing.

    python scripts/backfill_brti.py --limit 1200 --stride 5
"""

import argparse
import asyncio
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from btc15_signal.brti import KalshiBRTI, features_from_series  # noqa: E402
from btc15_signal.config import Settings  # noqa: E402

OUT = "data/brti_history.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS brti_decision_points (
    ticker TEXT NOT NULL,
    remaining_s INTEGER NOT NULL,
    open_ms INTEGER NOT NULL,
    close_ms INTEGER NOT NULL,
    target REAL NOT NULL,
    expiration_value REAL,
    result TEXT,
    brti_value REAL NOT NULL,
    signed_distance_bps REAL NOT NULL,
    brti_momentum_bps REAL,
    brti_volatility_bps REAL,
    brti_normalized_distance REAL,
    brti_side TEXT,
    samples INTEGER,
    PRIMARY KEY (ticker, remaining_s)
);
CREATE TABLE IF NOT EXISTS brti_fetched (
    ticker TEXT PRIMARY KEY, fetched_at INTEGER NOT NULL, points INTEGER NOT NULL
);
"""

# The deployed entry window is 660s..360s remaining, sampled each minute.
DECISION_SECONDS = (660, 600, 540, 480, 420, 360)


def markets(limit: int, stride: int, done: set[str]) -> list[dict]:
    db = sqlite3.connect("file:data/market_data.db?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    rows = [
        dict(r) for r in db.execute(
            "SELECT ticker, open_ms, close_ms, floor_strike, expiration_value, result "
            "FROM markets WHERE result IN ('yes','no') AND floor_strike IS NOT NULL "
            "ORDER BY open_ms"
        )
    ]
    rows = rows[::stride] if stride > 1 else rows
    rows = [r for r in rows if r["ticker"] not in done]
    return rows[:limit] if limit else rows


async def run(args) -> None:
    settings = Settings()
    Path("data").mkdir(exist_ok=True)
    out = sqlite3.connect(OUT)
    out.executescript(SCHEMA)
    done = {r[0] for r in out.execute("SELECT ticker FROM brti_fetched")}

    todo = markets(args.limit, args.stride, done)
    print(f"{len(todo)} markets to fetch ({len(done)} already stored) -> {OUT}")

    client = KalshiBRTI(settings.kalshi_base_url, timeout=25)
    written = failed = 0
    try:
        for index, market in enumerate(todo, 1):
            event = market["ticker"].rsplit("-", 1)[0]
            try:
                series = await client.series(event)
            except Exception as exc:  # noqa: BLE001 - one bad event is not fatal
                failed += 1
                if failed <= 5:
                    print(f"  {event}: {type(exc).__name__}", flush=True)
                continue
            if not series:
                failed += 1
                continue
            series.sort()

            rows = []
            for remaining in DECISION_SECONDS:
                cutoff = market["close_ms"] - remaining * 1000
                upto = [(t, v) for t, v in series if t <= cutoff]
                if len(upto) < 120:
                    continue
                f = features_from_series(
                    event, upto, market["floor_strike"], cutoff,
                )
                if f is None:
                    continue
                rows.append({
                    "ticker": market["ticker"], "remaining_s": remaining,
                    "open_ms": market["open_ms"], "close_ms": market["close_ms"],
                    "target": market["floor_strike"],
                    "expiration_value": market["expiration_value"],
                    "result": market["result"],
                    "brti_value": f.value,
                    "signed_distance_bps": f.signed_distance_bps,
                    "brti_momentum_bps": f.brti_momentum_bps,
                    "brti_volatility_bps": f.brti_volatility_bps,
                    "brti_normalized_distance": f.brti_normalized_distance,
                    "brti_side": f.side,
                    "samples": f.samples,
                })
            if rows:
                out.executemany(
                    "INSERT OR REPLACE INTO brti_decision_points "
                    "(ticker, remaining_s, open_ms, close_ms, target, "
                    " expiration_value, result, brti_value, signed_distance_bps, "
                    " brti_momentum_bps, brti_volatility_bps, "
                    " brti_normalized_distance, brti_side, samples) "
                    "VALUES (:ticker, :remaining_s, :open_ms, :close_ms, :target, "
                    " :expiration_value, :result, :brti_value, :signed_distance_bps, "
                    " :brti_momentum_bps, :brti_volatility_bps, "
                    " :brti_normalized_distance, :brti_side, :samples)",
                    rows,
                )
            out.execute(
                "INSERT OR REPLACE INTO brti_fetched VALUES (?,?,?)",
                (market["ticker"], int(time.time() * 1000), len(series)),
            )
            out.commit()
            written += len(rows)
            if index % 100 == 0:
                print(f"  {index}/{len(todo)}  {written} decision points", flush=True)
    finally:
        await client.close()
        total = out.execute("SELECT COUNT(*) FROM brti_decision_points").fetchone()[0]
        markets_done = out.execute("SELECT COUNT(*) FROM brti_fetched").fetchone()[0]
        out.close()
    print(f"wrote {written} points this run; {total} total across "
          f"{markets_done} markets ({failed} fetch failures)")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=1200)
    parser.add_argument("--stride", type=int, default=5)
    asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    main()
