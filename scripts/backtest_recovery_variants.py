"""Backtest recovery variants over the corpus, and ask what a bad day is.

THE QUESTION. The live record shows the strategy sitting exactly on its
break-even line (margin -0.07% over 191 markets), and a day can lose money at
an 83% win rate because the break-even RATE moves with the price paid. So:
does any recovery rule improve a bad day, or only move money between days?

THE CONTROL THAT MATTERS. FINDINGS 32 already established that flat size
scales exactly - twice the stake is twice the profit AND twice the drawdown.
So any recovery that only changes SIZE cannot create edge; it can only change
when the same edge is realised. A variant is only interesting if it changes
WHICH markets are taken.

Replayed one row per market at `remaining = 10`, in time order, inside the
deployed 0.70-0.93 band. `ask` and `won` are exchange facts and valid here;
the feature columns are Binance-derived (FINDINGS 42) and are used only where
marked indicative.
"""
import sqlite3
import statistics
import sys
from datetime import UTC, datetime

sys.path.insert(0, r"D:\Kalshi\btc15-signal\src")
from btc15_signal.validation import kalshi_fee_charged  # noqa: E402

db = sqlite3.connect(r"file:D:\Kalshi\btc15-signal\data\cohort.db?mode=ro", uri=True)
db.row_factory = sqlite3.Row

ROWS = db.execute(
    "SELECT ticker, open_ms, ask, won, session, normalized_distance, "
    "momentum_bps, side_is_up FROM cohort "
    "WHERE remaining = 10 AND ask >= 0.70 AND ask < 0.94 ORDER BY open_ms"
).fetchall()


def pnl_of(ask, won, count):
    gross = ((1.0 - ask) if won else -ask) * count
    return gross - kalshi_fee_charged(ask, count)


def day_of(ms):
    return datetime.fromtimestamp(ms / 1000, UTC).strftime("%Y-%m-%d")


def aligned(r):
    if r["momentum_bps"] is None:
        return False
    return (1 if r["side_is_up"] else -1) * r["momentum_bps"] > 0


# --------------------------------------------------------------- variants

def run(name, size_of, take_of):
    """Replay the whole corpus under one rule. Returns a summary dict."""
    equity = 0.0
    peak = 0.0
    max_dd = 0.0
    per_day: dict[str, float] = {}
    deficit = 0.0
    streak = 0
    taken = 0
    day_streak: dict[str, int] = {}
    for r in ROWS:
        d = day_of(r["open_ms"])
        state = {"deficit": deficit, "streak": streak,
                 "day_losses": day_streak.get(d, 0)}
        if not take_of(r, state):
            continue
        count = size_of(r, state)
        if count <= 0:
            continue
        p = pnl_of(r["ask"], r["won"], count)
        taken += 1
        equity += p
        per_day[d] = per_day.get(d, 0.0) + p
        peak = max(peak, equity)
        max_dd = min(max_dd, equity - peak)
        if p < 0:
            deficit += -p
            streak += 1
            day_streak[d] = day_streak.get(d, 0) + 1
        else:
            deficit = max(0.0, deficit - p)
            streak = 0
    days = sorted(per_day.values())
    return {
        "name": name, "taken": taken, "total": equity, "max_dd": max_dd,
        "days": len(per_day),
        "worst_day": days[0] if days else 0.0,
        "p10_day": days[max(0, int(len(days) * 0.10))] if days else 0.0,
        "median_day": statistics.median(days) if days else 0.0,
        "losing_days": sum(1 for v in days if v < 0),
        "per_trade": equity / taken if taken else 0.0,
    }


ALWAYS = lambda r, s: True          # noqa: E731
ONE = lambda r, s: 1                # noqa: E731
TWO = lambda r, s: 2                # noqa: E731


def deficit_size(r, s):
    """The DEPLOYED rule: upsize only if this trade can cover its share."""
    if s["deficit"] <= 0:
        return 1
    share = s["deficit"] / 4
    best = 2 * (1.0 - r["ask"]) - kalshi_fee_charged(r["ask"], 2)
    return 2 if best >= share else 1


def band_only(r, s):
    """Selective, not additive: after a loss take ONLY the band that has an
    interval clear of zero (0.85-0.90, momentum aligned). Indicative."""
    if s["deficit"] <= 0:
        return True
    return 0.85 <= r["ask"] < 0.90 and aligned(r)


def stop_after(n):
    def take(r, s):
        return s["day_losses"] < n
    return take


VARIANTS = [
    ("A baseline: flat 1", ONE, ALWAYS),
    ("B deployed: deficit upsize", deficit_size, ALWAYS),
    ("C control: flat 2", TWO, ALWAYS),
    ("D selective band after a loss", ONE, band_only),
    ("E stand down after 2 losses/day", ONE, stop_after(2)),
    ("F stand down after 3 losses/day", ONE, stop_after(3)),
    ("G selective + flat 2", TWO, band_only),
]

print(f"corpus: {len(ROWS)} markets in the band, "
      f"{len({day_of(r['open_ms']) for r in ROWS})} days\n")
print(f"{'variant':32} {'taken':>6} {'total':>9} {'per trade':>10} "
      f"{'max DD':>9} {'worst day':>10} {'p10 day':>9} {'losing days':>12}")
results = []
for name, size, take in VARIANTS:
    out = run(name, size, take)
    results.append(out)
    print(f"{out['name']:32} {out['taken']:>6} {out['total']:>+9.2f} "
          f"{out['per_trade']:>+10.4f} {out['max_dd']:>+9.2f} "
          f"{out['worst_day']:>+10.2f} {out['p10_day']:>+9.2f} "
          f"{out['losing_days']:>5}/{out['days']:<6}")

base = results[0]
print(f"\n=== against the baseline ({base['name']}) ===")
for out in results[1:]:
    scale = out["total"] / base["total"] if base["total"] else 0
    dd_scale = out["max_dd"] / base["max_dd"] if base["max_dd"] else 0
    print(f"  {out['name']:32} profit x{scale:5.2f}  drawdown x{dd_scale:5.2f}"
          f"  per-trade {out['per_trade'] - base['per_trade']:+.4f}")
