"""Reconcile computed settlement averages against Kalshi's official value.

Two jobs, and the second is the correction the operator asked for.

1. RECONCILE. For every settled market, compare what we can compute against
   `expiration_value`, the official average of sixty BRTI prices in the final
   minute. Where the recorder has its own BRTI ticks it is graded directly;
   the HOLD/EXIT shadow model does not start until that error is reliably
   small, because a decision made on the wrong number is the mistake FINDINGS
   40 exists to record.

2. DECOMPOSE. Section 40 compared one Binance minute-close against a
   60-second average, which mixes the FEED difference with the TIME
   AGGREGATION and can attribute neither. Each is isolated by holding the
   other fixed:

       feed_basis_bps   60s Binance mean  vs  official 60s BRTI mean
                        -> both sides averaged the same way: FEED only
       aggregation_bps  Binance last tick vs  60s Binance mean
                        -> both sides the same feed: AVERAGING only

   Their sum is the number section 40 published.

Binance per-second bars are fetched once and cached in the recorder's own
database, so a rerun is free and a rate limit cannot half-write a row.

    python scripts/reconcile_settlement.py --limit 400
"""

import argparse
import asyncio
import sqlite3
import statistics as st
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from btc15_signal.config import Settings  # noqa: E402
from btc15_signal.reference import BinanceSeconds, basis_bps  # noqa: E402
from btc15_signal.reference_store import ReferenceStore  # noqa: E402


def corpus_markets(limit: int, stride: int) -> list[dict]:
    """Settled markets from the historical corpus, evenly spread over it."""
    path = Path("data/market_data.db")
    if not path.exists():
        return []
    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    rows = [
        dict(r) for r in db.execute(
            "SELECT ticker, open_ms, close_ms, floor_strike, expiration_value, result "
            "FROM markets WHERE result IN ('yes','no') "
            "AND expiration_value IS NOT NULL AND floor_strike IS NOT NULL "
            "ORDER BY open_ms"
        )
    ]
    sampled = rows[::stride] if stride > 1 else rows
    return sampled[:limit] if limit else sampled


async def reconcile(markets: list[dict], store: ReferenceStore, settings: Settings) -> int:
    binance = BinanceSeconds(settings.spot_base_url, settings.symbol)
    done = store.reconciled_tickers()
    written = 0
    try:
        for index, market in enumerate(markets, 1):
            ticker = market["ticker"]
            if ticker in done:
                continue
            close_ms, start = market["close_ms"], market["close_ms"] - 60_000
            bars = store.second_bars("binance_spot", start, close_ms)
            if len(bars) < 30:
                try:
                    bars = await binance.seconds(start, close_ms)
                    if bars:
                        store.save_second_bars("binance_spot", bars)
                except Exception as exc:  # noqa: BLE001
                    print(f"  {ticker}: 1s fetch failed ({type(exc).__name__})", flush=True)
                    continue
            if not bars:
                continue

            official = float(market["expiration_value"])
            strike = float(market["floor_strike"])
            mean = sum(p for _, p in bars) / len(bars)
            last = bars[-1][1]

            recorded = [
                (row["event_ms"] or row["received_ms"], row["raw_price"])
                for row in store.observations(market["open_ms"], "brti")
                if row["raw_price"] is not None
            ]
            in_window = [p for ms, p in recorded if start <= ms < close_ms]
            computed = sum(in_window) / len(in_window) if in_window else None

            store.record_reconciliation({
                "ticker": ticker,
                "window_open_ms": market["open_ms"],
                "close_ms": close_ms,
                "official_expiration_value": official,
                "official_strike": strike,
                "official_result": market["result"],
                "computed_brti_mean": computed,
                "computed_brti_count": len(in_window),
                "computed_brti_error": None if computed is None else computed - official,
                "computed_brti_error_bps": basis_bps(computed, official),
                "binance_60s_mean": mean,
                "binance_60s_count": len(bars),
                "binance_last": last,
                "feed_basis_bps": basis_bps(mean, official),
                "aggregation_bps": basis_bps(last, mean),
                "total_bps": basis_bps(last, official),
                "outcome_official": int(official >= strike),
                "outcome_binance_last": int(last >= strike),
                "outcome_binance_60s": int(mean >= strike),
                "reconciled_ms": int(time.time() * 1000),
                "note": None if in_window else "no BRTI ticks recorded",
            })
            written += 1
            if index % 50 == 0:
                print(f"  {index}/{len(markets)} ...", flush=True)
    finally:
        await binance.close()
    return written


def calibrate(store: ReferenceStore, window: int) -> int:
    """Fill the walk-forward basis correction, in close order.

    The correction applied to a market is the median feed basis of the
    `window` markets that settled BEFORE it - never including itself. That is
    the only form of this that means anything: a basis fitted on the market it
    is then scored against is the answer written down twice.
    """
    rows = list(store.db.execute(
        "SELECT ticker, close_ms, feed_basis_bps, binance_60s_mean, "
        "official_expiration_value, official_strike, outcome_official "
        "FROM settlement_reconciliation "
        "WHERE feed_basis_bps IS NOT NULL AND binance_60s_count >= 30 "
        "ORDER BY close_ms"
    ))
    history: list[float] = []
    written = 0
    for row in rows:
        if len(history) >= window:
            prior = sorted(history[-window:])
            adjustment = prior[len(prior) // 2]
            calibrated = row["binance_60s_mean"] / (1 + adjustment / 10_000)
            store.db.execute(
                "UPDATE settlement_reconciliation SET "
                "calibration_bps = :bps, calibration_window = :window, "
                "calibrated_binance = :price, calibrated_error_bps = :error, "
                "outcome_calibrated = :outcome WHERE ticker = :ticker",
                {
                    "bps": adjustment, "window": window, "price": calibrated,
                    "error": basis_bps(calibrated, row["official_expiration_value"]),
                    "outcome": int(calibrated >= row["official_strike"]),
                    "ticker": row["ticker"],
                },
            )
            written += 1
        history.append(row["feed_basis_bps"])
    store.db.commit()
    return written


def report(store: ReferenceStore) -> None:
    rows = list(store.db.execute(
        "SELECT * FROM settlement_reconciliation WHERE binance_60s_count >= 30"
    ))
    if not rows:
        print("nothing reconciled yet")
        return

    feed = [r["feed_basis_bps"] for r in rows if r["feed_basis_bps"] is not None]
    aggregation = [r["aggregation_bps"] for r in rows if r["aggregation_bps"] is not None]
    total = [r["total_bps"] for r in rows if r["total_bps"] is not None]

    def line(name: str, values: list[float]) -> None:
        absolute = sorted(abs(v) for v in values)
        n = len(absolute)
        print(f"  {name:<24} n={n:<6} median |{st.median(absolute):6.2f}| bps   "
              f"p90 {absolute[int(0.9 * n)]:6.2f}   mean signed {st.mean(values):+7.3f}")

    print(f"\nDECOMPOSITION on {len(rows)} settled markets")
    line("FEED (60s vs 60s)", feed)
    line("AGGREGATION (Binance)", aggregation)
    line("TOTAL (section 40)", total)

    # Outcome disagreement attributable to each component.
    last_flips = sum(
        1 for r in rows
        if r["outcome_binance_last"] is not None
        and r["outcome_binance_last"] != r["outcome_official"]
    )
    mean_flips = sum(
        1 for r in rows
        if r["outcome_binance_60s"] is not None
        and r["outcome_binance_60s"] != r["outcome_official"]
    )
    n = len(rows)
    print("\nOUTCOME DISAGREEMENT with the official settlement")
    print(f"  Binance last tick   {last_flips:>5} / {n}  ({last_flips / n:.2%})"
          f"   <- what section 40 measured")
    print(f"  Binance 60s mean    {mean_flips:>5} / {n}  ({mean_flips / n:.2%})"
          f"   <- averaging fixed, feed basis alone")
    recovered = last_flips - mean_flips
    print(f"  removed by averaging {recovered:>4}"
          f"  ({recovered / last_flips:.1%} of the disagreement)" if last_flips else "")

    calibrated = [r for r in rows if r["calibrated_error_bps"] is not None]
    if calibrated:
        residual = sorted(abs(r["calibrated_error_bps"]) for r in calibrated)
        cal_flips = sum(
            1 for r in calibrated if r["outcome_calibrated"] != r["outcome_official"]
        )
        raw_flips = sum(
            1 for r in calibrated if r["outcome_binance_60s"] != r["outcome_official"]
        )
        n_cal = len(calibrated)
        print(f"\nWALK-FORWARD BASIS CORRECTION (trailing "
              f"{calibrated[0]['calibration_window']}, earlier windows only)")
        print(f"  residual |error|    median {st.median(residual):.3f} bps   "
              f"p90 {residual[int(0.9 * n_cal)]:.3f}")
        print(f"  outcome flips       raw {raw_flips} ({raw_flips / n_cal:.2%})  ->  "
              f"calibrated {cal_flips} ({cal_flips / n_cal:.2%})")
        print("  Research only. No entry, exit or sizing path reads this.")

    graded = [r for r in rows if r["computed_brti_count"] and r["computed_brti_count"] >= 30]
    print("\nRECORDER vs OFFICIAL (own BRTI ticks)")
    if not graded:
        print("  0 markets have recorded BRTI ticks - the live feed is not entitled.")
        print("  Official 60s averages are still being captured from Kalshi, so the")
        print("  decomposition above is valid; only our own reproduction is pending.")
        return
    errors = sorted(abs(r["computed_brti_error_bps"]) for r in graded)
    print(f"  n={len(graded)}  median |{st.median(errors):.4f}| bps  "
          f"max |{errors[-1]:.4f}| bps")
    if errors[-1] < 1.0:
        print("  Reproduces official settlements. HOLD/EXIT shadow may begin.")
    else:
        print("  Does NOT yet reproduce official settlements. HOLD/EXIT stays off.")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=400,
                        help="markets to reconcile this run (0 = all)")
    parser.add_argument("--stride", type=int, default=8,
                        help="sample every Nth market, to span the whole corpus")
    parser.add_argument("--report-only", action="store_true")
    parser.add_argument("--calibration-window", type=int, default=20,
                        help="trailing markets the basis correction is fitted on")
    args = parser.parse_args()

    settings = Settings()
    store = ReferenceStore(settings.reference_database_path)
    try:
        if not args.report_only:
            markets = corpus_markets(args.limit, args.stride)
            print(f"reconciling {len(markets)} settled markets "
                  f"(stride {args.stride}) -> {settings.reference_database_path}")
            written = asyncio.run(reconcile(markets, store, settings))
            print(f"wrote {written} reconciliation rows")
        graded = calibrate(store, args.calibration_window)
        print(f"calibrated {graded} markets walk-forward "
              f"(trailing {args.calibration_window})")
        report(store)
    finally:
        store.close()


if __name__ == "__main__":
    main()
