"""Where the edge lives by ENTRY PRICE, over the whole corpus.

`ask` and `won` are exchange facts - the price Kalshi quoted and the way the
market resolved - so they are valid regardless of which feed computed the
feature columns. That matters here: the corpus features are Binance-derived
(FINDINGS 42) and cannot be trusted for gate replay, but the price/outcome
relationship is the instrument's own.

One row per market at `remaining = 10` minutes, which is where the live
entries actually land.
"""
import sqlite3
import sys

sys.path.insert(0, r"D:\Kalshi\btc15-signal\src")
from btc15_signal.validation import kalshi_fee_charged  # noqa: E402

db = sqlite3.connect(r"file:D:\Kalshi\btc15-signal\data\cohort.db?mode=ro", uri=True)
db.row_factory = sqlite3.Row

BANDS = [(0.70, 0.75), (0.75, 0.80), (0.80, 0.85), (0.85, 0.90), (0.90, 0.94)]


def wilson(wins, n, z=1.96):
    if not n:
        return (0.0, 0.0)
    p = wins / n
    d = 1 + z * z / n
    c = p + z * z / (2 * n)
    m = z * ((p * (1 - p) / n + z * z / (4 * n * n)) ** 0.5)
    return ((c - m) / d, (c + m) / d)


def report(title, rows):
    print(f"\n=== {title} ===")
    print(f"{'band':>12} {'n':>6} {'win rate':>9} {'break-even':>11} "
          f"{'margin':>8} {'per contract':>13} {'95% CI':>22}")
    total_n = total_pnl = 0
    for lo, hi in BANDS:
        g = [r for r in rows if lo <= r["ask"] < hi]
        if len(g) < 30:
            continue
        wins = sum(1 for r in g if r["won"])
        n = len(g)
        wr = wins / n
        # Per-contract economics at THIS band's own average price.
        avg = sum(r["ask"] for r in g) / n
        fee = kalshi_fee_charged(avg, 1)
        win_amt, loss_amt = (1.0 - avg) - fee, -avg - fee
        be = (-loss_amt) / ((-loss_amt) + win_amt)
        pnl = sum(((1.0 - r["ask"]) if r["won"] else -r["ask"])
                  - kalshi_fee_charged(r["ask"], 1) for r in g)
        lo_ci, hi_ci = wilson(wins, n)
        edge_lo = lo_ci * win_amt + (1 - lo_ci) * loss_amt
        edge_hi = hi_ci * win_amt + (1 - hi_ci) * loss_amt
        print(f"{lo:.2f}-{hi:.2f} {n:>6} {wr:>9.1%} {be:>11.1%} "
              f"{wr - be:>+8.1%} {pnl / n:>+13.4f} "
              f"[{edge_lo:+.4f}, {edge_hi:+.4f}]")
        total_n += n
        total_pnl += pnl
    if total_n:
        print(f"{'TOTAL':>12} {total_n:>6} {'':>9} {'':>11} {'':>8} "
              f"{total_pnl / total_n:>+13.4f}")


rows = db.execute(
    "SELECT ask, won, session, normalized_distance, momentum_bps, side_is_up "
    "FROM cohort WHERE remaining = 10 AND ask >= 0.70 AND ask < 0.94"
).fetchall()
print(f"markets at remaining=10 inside the deployed band: {len(rows)}")
report("ALL markets in the band", rows)

aligned = [
    r for r in rows
    if r["normalized_distance"] is not None and r["momentum_bps"] is not None
    and (1 if r["side_is_up"] else -1) * r["momentum_bps"] > 0
]
report("momentum aligned (Binance-derived, indicative only)", aligned)

print("\n=== by session, whole band ===")
for name in ("asia", "europe", "us", "late-us"):
    g = [r for r in rows if r["session"] == name]
    if len(g) < 50:
        continue
    wins = sum(1 for r in g if r["won"])
    pnl = sum(((1.0 - r["ask"]) if r["won"] else -r["ask"])
              - kalshi_fee_charged(r["ask"], 1) for r in g)
    print(f"  {name:8} n={len(g):5} wr={wins/len(g):5.1%} "
          f"per contract {pnl/len(g):+.4f}")
