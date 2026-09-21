"""Sweep the recovery overlay's parameters, and control for the sweep itself.

`scripts/measure_recovery.py` measured ONE configuration - 10 contracts at
0.90-0.93, 1-3 minutes left - and found +2.84 over 488 trades with a 95%
interval of [-0.2394, +0.2418]. This asks whether some other corner of the
parameter space does better.

IT ALMOST CERTAINLY WILL, and that is the problem this script is built around.
Section 7 of FINDINGS: "the best of 11,365 searched rules scored below the
MEDIAN best rule on shuffled data (p=0.99). Only pre-specified rules recover
the edge. Do not trust a rule that a search found."

So the sweep reports two things that matter more than the winner:

  * how many cells clear zero, against how many would clear by chance alone
  * what the BEST CELL looks like when the outcomes are shuffled - if a search
    over noise routinely produces a number as good as the real best, the real
    best is not evidence of anything

The live record is measured separately at the end, on the deployed
configuration only, because 14 losses cannot support a search.

    python scripts/optimize_recovery.py
"""

import math
import random
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from compare_series import load  # noqa: E402

from btc15_signal.config import Settings  # noqa: E402
from btc15_signal.features import build_snapshots  # noqa: E402
from btc15_signal.strategy import EntryRule  # noqa: E402

ASK_BANDS = [(0.85, 0.90), (0.88, 0.92), (0.90, 0.93), (0.92, 0.95),
             (0.94, 0.97), (0.90, 0.97)]
MINUTES = [(1, 2), (1, 3), (2, 4), (3, 5), (4, 6), (1, 5)]
SIZES = [5, 10, 20]
SHUFFLES = 200


def fee(price: float, contracts: float) -> float:
    return math.ceil(
        round(0.07 * contracts * price * (1 - price) * 10_000, 6)
    ) / 10_000


def prepare(rule: EntryRule):
    """Per market: the normal entry, and every candidate recovery quote."""
    markets, klines, candles = load("data/market_data.db")
    if not klines:
        return None
    by_market: dict[str, list] = {}
    for snap in build_snapshots(markets, klines, candles):
        by_market.setdefault(snap.ticker, []).append(snap)

    prepared = []
    for ticker, snaps in by_market.items():
        snaps.sort(key=lambda s: -s.remaining)
        if not snaps or snaps[0].result not in ("yes", "no"):
            continue
        normal = None
        quotes = []
        for snap in snaps:
            if snap.yes_ask is None or snap.yes_bid is None:
                continue
            up, down = snap.yes_ask, 1 - snap.yes_bid
            side_is_up = up >= down
            ask = max(up, down)
            if not 0 < ask < 1:
                continue
            vol = max(snap.volatility_5m_bps, 1.0)
            if abs(snap.signed_distance_bps) / vol < rule.min_normalized_distance:
                continue
            won = (snap.result == "yes") if side_is_up else (snap.result == "no")
            if normal is None and 6 <= snap.remaining <= 11 \
                    and rule.min_ask <= ask <= rule.max_ask:
                normal = (ask, won)
            quotes.append((snap.remaining, ask, won))
        prepared.append((snaps[0].open_ms, ticker, normal, quotes))
    prepared.sort(key=lambda row: row[0])
    return prepared


def sweep_cell(prepared, lo, hi, m_lo, m_hi, size, taker, flip=None):
    """One configuration. `flip` maps a market index to a forced outcome."""
    base = rec = 0.0
    rec_n = rec_wins = 0
    armed = False
    equity = peak = trough = 0.0
    pnl_log = []
    for index, (_open_ms, _t, normal, quotes) in enumerate(prepared):
        if armed:
            for remaining, ask, won in quotes:
                if not m_lo <= remaining <= m_hi or not lo <= ask <= hi:
                    continue
                if flip is not None:
                    won = flip(index, ask)
                charged = fee(ask, size) if taker else 0.0
                pnl = (size if won else 0.0) - size * ask - charged
                rec += pnl
                equity += pnl
                pnl_log.append(pnl)
                rec_n += 1
                rec_wins += int(won)
                armed = False
                break
        if normal is not None:
            ask, won = normal
            if flip is not None:
                won = flip(index, ask)
            charged = fee(ask, 1) if taker else 0.0
            pnl = (1.0 if won else 0.0) - ask - charged
            base += pnl
            equity += pnl
            if not won:
                armed = True
        peak = max(peak, equity)
        trough = min(trough, equity - peak)
    return {
        "rec": rec, "n": rec_n, "wins": rec_wins, "base": base,
        "total": base + rec, "dd": trough, "log": pnl_log,
    }


def ci(values, draws=2000, seed=7):
    if len(values) < 2:
        return 0.0, 0.0
    rng = random.Random(seed)
    means = sorted(
        sum(rng.choice(values) for _ in values) / len(values) for _ in range(draws)
    )
    return means[int(0.025 * draws)], means[int(0.975 * draws) - 1]


def main() -> None:
    rule = EntryRule.load("strategy.json")
    prepared = prepare(rule)
    if not prepared:
        print("no klines - run scripts/fetch_klines.py first")
        return

    print("=" * 78)
    print("RECOVERY OVERLAY - PARAMETER SWEEP, with a control for the sweep")
    print("=" * 78)
    cells = len(ASK_BANDS) * len(MINUTES) * len(SIZES)
    print(f"\n{len(prepared)} settled markets, {cells} configurations, taker fees.")

    results = []
    for lo, hi in ASK_BANDS:
        for m_lo, m_hi in MINUTES:
            for size in SIZES:
                r = sweep_cell(prepared, lo, hi, m_lo, m_hi, size, True)
                if r["n"] < 50:
                    continue
                low, high = ci(r["log"])
                results.append({
                    "band": f"{lo:.2f}-{hi:.2f}", "min": f"{m_lo}-{m_hi}",
                    "size": size, **r, "lo": low, "hi": high,
                })
    results.sort(key=lambda x: -x["rec"])

    print(f"\nTOP 10 BY RECOVERY P&L (of {len(results)} cells with n>=50)")
    print(f"  {'band':<12}{'min':<6}{'size':>5}{'n':>6}{'won':>7}"
          f"{'rec P&L':>10}{'per trade':>11}{'95% CI':>22}{'drawdown':>10}")
    for x in results[:10]:
        print(f"  {x['band']:<12}{x['min']:<6}{x['size']:>5}{x['n']:>6}"
              f"{x['wins'] / x['n']:>7.1%}{x['rec']:>10.2f}"
              f"{x['rec'] / x['n']:>+11.4f}"
              f"  [{x['lo']:+.3f}, {x['hi']:+.3f}]{x['dd']:>10.2f}")

    clears = [x for x in results if x["lo"] > 0]
    print(f"\n  cells whose interval CLEARS ZERO: {len(clears)} of {len(results)}")
    print(f"  expected by chance at 95%:        ~{len(results) * 0.025:.1f}")

    # --- THE CONTROL --------------------------------------------------------
    # Shuffle the outcomes so no edge can exist, re-run the WHOLE search, and
    # keep the best cell each time. If the real best sits inside this
    # distribution it is what a search over noise produces, not a finding.
    print("\nCONTROL: the same search run on SHUFFLED outcomes")
    print(f"  ({SHUFFLES} shuffles; each keeps only its own best cell)")
    best_real = results[0]["rec"] if results else 0.0

    # THE NULL HAS TO KEEP THE EDGE THE SYSTEM ALREADY HAS.
    # A first version drew `won = random() < ask` - each trade winning at
    # exactly its price. That is a fair-pricing null, and this whole strategy
    # exists because these markets win MORE than their price (section 1), so
    # the real data beat it decisively no matter which parameters were used.
    # It tested "is there any edge at all", not "did the search find one".
    #
    # This null instead draws each outcome from the EMPIRICAL win rate at that
    # ask, measured from the same data. The favourite-longshot edge survives
    # intact; only the question of WHICH market wins is destroyed. What is left
    # for the search to find is exactly the parameter structure - which is what
    # is on trial.
    buckets: dict[int, list[int]] = {}
    for _o, _t, _n, quotes in prepared:
        for _rem, ask, won in quotes:
            buckets.setdefault(int(ask * 100), []).append(int(won))
    rate = {
        key: sum(vals) / len(vals) for key, vals in buckets.items() if len(vals) >= 30
    }
    overall = sum(sum(v) for v in buckets.values()) / max(
        sum(len(v) for v in buckets.values()), 1
    )

    nulls = []
    for shuffle in range(SHUFFLES):
        rng = random.Random(1000 + shuffle)

        def flip(_index, ask, _rng=rng):
            return _rng.random() < rate.get(int(ask * 100), overall)

        best = None
        for lo, hi in ASK_BANDS:
            for m_lo, m_hi in MINUTES:
                r = sweep_cell(prepared, lo, hi, m_lo, m_hi, 10, True, flip=flip)
                if r["n"] >= 50 and (best is None or r["rec"] > best):
                    best = r["rec"]
        if best is not None:
            nulls.append(best)
    nulls.sort()
    if nulls:
        beat = sum(1 for x in nulls if x >= best_real) / len(nulls)
        print(f"  best real cell                {best_real:+.2f}")
        print(f"  best SHUFFLED cell, median    {nulls[len(nulls) // 2]:+.2f}")
        print(f"  best SHUFFLED cell, 95th      {nulls[int(0.95 * len(nulls))]:+.2f}")
        print(f"  shuffles matching the real best: {beat:.1%}")
        print("  => "
              + ("the real best is INSIDE what noise produces - the sweep found "
                 "nothing" if beat > 0.05 else
                 "the real best is beyond what noise produced here"))

    # --- THE LIVE RECORD ----------------------------------------------------
    print("\n" + "-" * 78)
    print("THE LIVE RECORD - deployed configuration only, no search")
    settings = Settings()
    db = sqlite3.connect(f"file:{settings.database_path}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    windows = [
        r["window_open"] for r in db.execute(
            "SELECT window_open FROM predictions WHERE won IS NOT NULL "
            "ORDER BY window_open"
        )
    ]
    outcome = {
        r["window_open"]: r["won"] for r in db.execute(
            "SELECT window_open, won FROM predictions WHERE won IS NOT NULL"
        )
    }
    armed = False
    fired = []
    for window in windows:
        if armed:
            row = db.execute(
                "SELECT our_ask, side FROM observations WHERE window_open=? "
                "AND remaining_s BETWEEN 60 AND 180 AND our_ask BETWEEN 0.90 AND 0.93 "
                "ORDER BY remaining_s DESC LIMIT 1",
                (window,),
            ).fetchone()
            if row:
                won = bool(outcome[window])
                pnl = (10 if won else 0.0) - 10 * row["our_ask"] - fee(row["our_ask"], 10)
                fired.append((window, row["our_ask"], won, pnl))
                armed = False
        if not outcome[window]:
            armed = True
    print(f"  settled live signals   {len(windows)}")
    print(f"  live losses            {sum(1 for w in windows if not outcome[w])}")
    print(f"  recovery would fire    {len(fired)} time(s)")
    for _window, ask, won, pnl in fired:
        print(f"    ask {ask:.2f}  {'WON ' if won else 'LOST'}  {pnl:+.2f}")
    if fired:
        print(f"  live overlay total     {sum(f[3] for f in fired):+.2f}")
    print("\n  A handful of fires cannot settle anything; it is reported so the")
    print("  live record and the backtest are looking at the same rule.")
    print("=" * 78)


if __name__ == "__main__":
    main()
