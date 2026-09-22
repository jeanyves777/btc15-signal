"""Recalculate the entry gates on BRTI. Do not port the Binance thresholds.

FINDINGS 43: `min_normalized_distance = 1.5` and the 2.0-4.0x confidence band
were measured against Binance RAW volatility. A 60-second mean is a low-pass
filter, so the same markets read 15-22 on BRTI. Applying 1.5 there would pass
everything and size up on most of it, while the config still said "1.5" - the
gate would be off and nothing would say so.

So the thresholds are not translated, they are re-derived the way the originals
were: measure net edge across buckets of the BRTI quantity and find where the
edge actually is. Three questions, in order:

  1. SIDE     does BRTI disagree with Binance about who is winning at ENTRY,
              and when it does, which one is right? Section 41 measured 19.4%
              at SETTLEMENT, where margins are tenths of a basis point; entry
              is 6-11 minutes out and is a different number.
  2. DISTANCE what does the net edge look like across BRTI normalized
              distance, and is there a floor worth having?
  3. SIZING   is there a band where the edge is larger, as 2.0-4.0x was on
              Binance?

Prices and outcomes come from the Kalshi corpus; BRTI comes from
`data/brti_history.db` (see `scripts/backfill_brti.py`). Nothing here reads
Binance.

    python scripts/measure_brti_gates.py
"""

import argparse
import random
import sqlite3
import statistics as st
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from btc15_signal.config import Settings  # noqa: E402
from btc15_signal.strategy import EntryRule  # noqa: E402
from btc15_signal.validation import kalshi_fee_charged as fee  # noqa: E402


def net(price: float, won: bool) -> float:
    return (1.0 if won else 0.0) - price - fee(price)


def ci(values: list[float], draws: int = 4000, seed: int = 11):
    if len(values) < 2:
        return 0.0, 0.0
    rng = random.Random(seed)
    means = sorted(
        sum(rng.choice(values) for _ in values) / len(values) for _ in range(draws)
    )
    return means[int(0.025 * draws)], means[int(0.975 * draws) - 1]


def load(rule: EntryRule) -> list[dict]:
    """One trade per market: first decision minute that clears the price band.

    The BRTI side chooses the contract; the Kalshi book prices it. The corpus
    quote is the minute candle whose period ends at that decision second.
    """
    brti = sqlite3.connect("file:data/brti_history.db?mode=ro", uri=True)
    brti.row_factory = sqlite3.Row
    points: dict[str, list[dict]] = {}
    for row in brti.execute(
        "SELECT * FROM brti_decision_points ORDER BY ticker, remaining_s DESC"
    ):
        points.setdefault(row["ticker"], []).append(dict(row))
    if not points:
        return []

    market = sqlite3.connect("file:data/market_data.db?mode=ro", uri=True)
    market.row_factory = sqlite3.Row
    quotes: dict[tuple[str, int], tuple[float, float]] = {}
    for row in market.execute(
        "SELECT ticker, end_period_ts, yes_bid_close, yes_ask_close "
        "FROM contract_candles WHERE yes_bid_close IS NOT NULL "
        "AND yes_ask_close IS NOT NULL"
    ):
        quotes[(row["ticker"], row["end_period_ts"])] = (
            row["yes_bid_close"], row["yes_ask_close"]
        )

    trades = []
    for ticker, rows in points.items():
        for row in rows:
            end_ts = (row["close_ms"] - row["remaining_s"] * 1000) // 1000
            quote = quotes.get((ticker, end_ts))
            if quote is None:
                continue
            yes_bid, yes_ask = quote
            side = row["brti_side"]
            ask = yes_ask if side == "UP" else round(1 - yes_bid, 4)
            if not (rule.min_ask <= ask <= rule.max_ask) or not 0 < ask < 1:
                continue
            won = (row["result"] == "yes") if side == "UP" else (row["result"] == "no")
            trades.append({
                "ticker": ticker, "side": side, "ask": ask, "won": won,
                "remaining_s": row["remaining_s"],
                "distance": row["brti_normalized_distance"],
                "signed_bps": row["signed_distance_bps"],
                # Magnitude: the sign only says which side was chosen, and the
                # side is already fixed by the time distance is being gated.
                "abs_bps": abs(row["signed_distance_bps"]),
                "momentum": row["brti_momentum_bps"],
                "volatility": row["brti_volatility_bps"],
                "yes_bid": yes_bid, "yes_ask": yes_ask,
                "result": row["result"], "close_ms": row["close_ms"],
            })
            break
    return trades


def report_side(trades: list[dict]) -> None:
    """Where BRTI and Binance disagree about the side AT ENTRY, who is right?

    Binance's side is reconstructed from the corpus snapshot - the same
    convention `model.py` runs live: the sign of spot minus strike.
    """
    from compare_series import load as load_corpus  # noqa: PLC0415

    from btc15_signal.features import build_snapshots  # noqa: PLC0415

    markets, klines, candles = load_corpus("data/market_data.db")
    snaps: dict[tuple[str, int], object] = {}
    for snap in build_snapshots(markets, klines, candles):
        snaps[(snap.ticker, snap.remaining * 60)] = snap

    rule = EntryRule.load(Path("strategy.json"))
    compared = disagree = 0
    tradeable: list[tuple[dict, float, bool]] = []   # both rules would trade
    skipped: list[dict] = []                         # Binance would not trade
    for trade in trades:
        snap = snaps.get((trade["ticker"], trade["remaining_s"]))
        if snap is None or snap.yes_bid is None or snap.yes_ask is None:
            continue
        binance_side = "UP" if snap.signed_distance_bps >= 0 else "DOWN"
        compared += 1
        if binance_side == trade["side"]:
            continue
        disagree += 1
        binance_ask = (
            snap.yes_ask if binance_side == "UP" else round(1 - snap.yes_bid, 4)
        )
        binance_won = (
            (trade["result"] == "yes") if binance_side == "UP"
            else (trade["result"] == "no")
        )
        # THE TRAP THIS AVOIDS. When the two disagree the asks are complements,
        # so if BRTI's side sits in the 0.70-0.93 band, Binance's sits near
        # 0.07-0.30 and the DEPLOYED RULE WOULD NOT TRADE IT AT ALL. Scoring a
        # position the bot would never open, and calling the difference a gain,
        # is how a measurement becomes confidently wrong about money.
        if rule.min_ask <= binance_ask <= rule.max_ask:
            tradeable.append((trade, binance_ask, binance_won))
        else:
            skipped.append(trade)

    print("\n1. SIDE - BRTI against Binance, same market, same minute")
    print(f"   compared                {compared}")
    if not compared:
        return
    print(f"   different side chosen   {disagree} ({disagree / compared:.2%})")
    if not disagree:
        print("   (no disagreements in this sample)")
        return

    print(f"\n   a) Binance's side was OUTSIDE the band - it would not have "
          f"traded: {len(skipped)}")
    if skipped:
        values = [net(t["ask"], t["won"]) for t in skipped]
        lo, hi = ci(values)
        wins = sum(1 for t in skipped if t["won"])
        print("      these are trades BRTI FINDS and the deployed rule MISSES,")
        print("      not money Binance lost.")
        print(f"      BRTI on them: n={len(skipped)} {sum(values) / len(values):+.4f}/ct "
              f"[{lo:+.4f},{hi:+.4f}] total {sum(values):+.2f} win {wins / len(skipped):.1%}")

    print(f"\n   b) BOTH rules would have traded, opposite sides: {len(tradeable)}")
    if tradeable:
        brti_values = [net(t["ask"], t["won"]) for t, _, _ in tradeable]
        binance_values = [net(a, w) for _, a, w in tradeable]
        diffs = [b - a for b, a in zip(brti_values, binance_values, strict=True)]
        lo, hi = ci(diffs)
        brti_wins = sum(1 for t, _, _ in tradeable if t["won"])
        print(f"      BRTI    {sum(brti_values) / len(brti_values):+.4f}/ct  "
              f"win {brti_wins / len(tradeable):.1%}  total {sum(brti_values):+.2f}")
        print(f"      Binance {sum(binance_values) / len(binance_values):+.4f}/ct  "
              f"total {sum(binance_values):+.2f}")
        print(f"      BRTI - Binance, paired: {sum(diffs) / len(diffs):+.4f}/ct "
              f"[{lo:+.4f},{hi:+.4f}]  total {sum(diffs):+.2f}")
    else:
        print("      none - the two sides are complements, so when they "
              "disagree only one of them is ever in the band.")


def report_buckets(trades: list[dict], field: str, edges, label: str) -> None:
    print(f"\n   by {label}")
    print(f"   {'bucket':>14} {'n':>6} {'mean ask':>9} {'net/ct':>10} "
          f"{'95% CI':>22} {'win':>7}")
    bounds = [(-1e9, edges[0])] + list(zip(edges, edges[1:], strict=False)) + \
             [(edges[-1], 1e9)]
    for low, high in bounds:
        bucket = [t for t in trades if low <= t[field] < high]
        if len(bucket) < 40:
            continue
        values = [net(t["ask"], t["won"]) for t in bucket]
        mean = sum(values) / len(values)
        lo, hi = ci(values)
        name = (f"< {high:g}" if low < -1e8 else
                f">= {low:g}" if high > 1e8 else f"{low:g} - {high:g}")
        print(f"   {name:>14} {len(bucket):>6} "
              f"{st.mean([t['ask'] for t in bucket]):>9.3f} {mean:>+10.4f} "
              f"[{lo:>+8.4f},{hi:>+8.4f}] "
              f"{sum(1 for t in bucket if t['won']) / len(bucket):>7.1%}")


def report_floor(trades: list[dict], candidates) -> None:
    print("\n   a floor is only worth having if it BEATS taking everything")
    baseline = [net(t["ask"], t["won"]) for t in trades]
    base_mean = sum(baseline) / len(baseline)
    print(f"   {'floor':>8} {'kept':>6} {'net/ct':>10} {'vs no floor':>12} "
          f"{'95% CI of the difference':>28}")
    for floor in candidates:
        kept = [t for t in trades if t["distance"] >= floor]
        if len(kept) < 40:
            continue
        values = [net(t["ask"], t["won"]) for t in kept]
        mean = sum(values) / len(values)
        # Paired on the markets the floor KEEPS, against those same markets
        # taken without it - the difference is what the floor does, not what a
        # different population does.
        lo, hi = ci([v - base_mean for v in values])
        print(f"   {floor:>8.1f} {len(kept):>6} {mean:>+10.4f} "
              f"{mean - base_mean:>+12.4f} [{lo:>+11.4f},{hi:>+11.4f}]")


def report_robustness(trades: list[dict], floor: float, parts: int = 4) -> None:
    """Does the floor survive being cut into periods?

    FINDINGS 14 killed a positive sub-band that looked strong on the pooled
    sample and collapsed once it was sliced. A threshold picked by scanning
    buckets has had many chances to look good, so the question is not whether
    the best one is positive - it is whether it is positive everywhere.
    """
    ordered = sorted(trades, key=lambda t: t["close_ms"])
    size = len(ordered) // parts
    if size < 30:
        print(f"\n   too few trades to split into {parts} periods")
        return
    import datetime as dt

    print(f"\n   the floor >= {floor:g}, quarter by quarter")
    print(f"   {'period':>22} {'kept':>6} {'net/ct':>10} {'vs no floor':>12} "
          f"{'win':>7}")
    for index in range(parts):
        chunk = ordered[index * size: (index + 1) * size] if index < parts - 1 \
            else ordered[index * size:]
        kept = [t for t in chunk if t["distance"] >= floor]
        if len(kept) < 15:
            print(f"   {'period ' + str(index + 1):>22} {len(kept):>6}   "
                  f"too few to judge")
            continue
        base = [net(t["ask"], t["won"]) for t in chunk]
        values = [net(t["ask"], t["won"]) for t in kept]
        start = dt.datetime.utcfromtimestamp(chunk[0]["close_ms"] / 1000)
        end = dt.datetime.utcfromtimestamp(chunk[-1]["close_ms"] / 1000)
        label = f"{start:%m-%d}..{end:%m-%d}"
        print(f"   {label:>22} {len(kept):>6} "
              f"{sum(values) / len(values):>+10.4f} "
              f"{sum(values) / len(values) - sum(base) / len(base):>+12.4f} "
              f"{sum(1 for t in kept if t['won']) / len(kept):>7.1%}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--skip-side", action="store_true")
    parser.add_argument("--floor", type=float, default=10.0,
                        help="BRTI normalized-distance floor to stress-test")
    args = parser.parse_args()

    settings = Settings()  # noqa: F841 - kept for symmetry with sibling scripts
    rule = EntryRule.load(Path("strategy.json"))
    trades = load(rule)
    if not trades:
        print("no BRTI decision points - run scripts/backfill_brti.py first")
        return

    values = [net(t["ask"], t["won"]) for t in trades]
    lo, hi = ci(values)
    print(f"BRTI-native entries, band {rule.min_ask}-{rule.max_ask}")
    print(f"  n={len(trades)}  {sum(values) / len(values):+.4f}/ct "
          f"[{lo:+.4f},{hi:+.4f}]  total {sum(values):+.2f}  "
          f"win {sum(1 for t in trades if t['won']) / len(trades):.1%}")

    if not args.skip_side:
        report_side(trades)

    print("\n2. DISTANCE - where the edge actually is on BRTI")
    report_buckets(trades, "distance", (5, 10, 15, 20, 30),
                   "BRTI normalized distance")
    report_floor(trades, (5, 10, 15, 20, 25, 30))

    report_robustness(trades, args.floor)

    print("\n3. SIZING - is there a band with a larger edge?")
    report_buckets(trades, "abs_bps", (2, 5, 10, 20),
                   "|signed distance| bps (unnormalised)")


if __name__ == "__main__":
    main()
