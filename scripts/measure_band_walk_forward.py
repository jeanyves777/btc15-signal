"""Does the selective band survive out of sample, or did I fit it?

Variant D/G in the backtest picks the 0.85-0.90 aligned band - a band chosen
by looking at the whole corpus. That is selection on the data being scored,
and its interval [+0.0013, +0.0525] barely cleared zero before any
multiplicity correction across the ten cells examined.

So: choose the band on the FIRST half only, then score it on the second half,
which the choice never saw. A rule that survives that is worth proposing; one
that does not is a story about noise.
"""
import sqlite3
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

BANDS = [(0.70, 0.75), (0.75, 0.80), (0.80, 0.85), (0.85, 0.90), (0.90, 0.94)]


def aligned(r):
    if r["momentum_bps"] is None:
        return False
    return (1 if r["side_is_up"] else -1) * r["momentum_bps"] > 0


def edge(rows):
    if not rows:
        return 0.0, 0
    total = sum(((1.0 - r["ask"]) if r["won"] else -r["ask"])
                - kalshi_fee_charged(r["ask"], 1) for r in rows)
    return total / len(rows), len(rows)


def wilson(wins, n, z=1.96):
    p = wins / n
    d = 1 + z * z / n
    c = p + z * z / (2 * n)
    m = z * ((p * (1 - p) / n + z * z / (4 * n * n)) ** 0.5)
    return ((c - m) / d, (c + m) / d)


def ci_edge(rows):
    """Interval on per-contract edge, from the win-rate interval."""
    n = len(rows)
    if n < 30:
        return None
    wins = sum(1 for r in rows if r["won"])
    avg = sum(r["ask"] for r in rows) / n
    fee = kalshi_fee_charged(avg, 1)
    win_amt, loss_amt = (1.0 - avg) - fee, -avg - fee
    lo, hi = wilson(wins, n)
    return (lo * win_amt + (1 - lo) * loss_amt,
            hi * win_amt + (1 - hi) * loss_amt)


cut = len(ROWS) // 2
train, test = ROWS[:cut], ROWS[cut:]
d0 = datetime.fromtimestamp(train[0]["open_ms"] / 1000, UTC).date()
d1 = datetime.fromtimestamp(train[-1]["open_ms"] / 1000, UTC).date()
d2 = datetime.fromtimestamp(test[-1]["open_ms"] / 1000, UTC).date()
print(f"train {d0} -> {d1}  ({len(train)} markets)")
print(f"test  {d1} -> {d2}  ({len(test)} markets)\n")

print(f"{'band (aligned)':18} {'train edge':>12} {'train n':>8} "
      f"{'TEST edge':>11} {'test n':>8} {'test 95% CI':>24}")
best = None
for lo, hi in BANDS:
    tr = [r for r in train if lo <= r["ask"] < hi and aligned(r)]
    te = [r for r in test if lo <= r["ask"] < hi and aligned(r)]
    e_tr, n_tr = edge(tr)
    e_te, n_te = edge(te)
    ci = ci_edge(te)
    ci_s = f"[{ci[0]:+.4f}, {ci[1]:+.4f}]" if ci else "n<30"
    print(f"{lo:.2f}-{hi:.2f}          {e_tr:>+12.4f} {n_tr:>8} "
          f"{e_te:>+11.4f} {n_te:>8} {ci_s:>24}")
    if n_tr >= 100 and (best is None or e_tr > best[1]):
        best = ((lo, hi), e_tr)

print()
if best is None:
    print("no band had enough training evidence to choose")
    raise SystemExit
(lo, hi), e_tr = best
print(f"BAND CHOSEN ON TRAIN ONLY: {lo:.2f}-{hi:.2f} aligned "
      f"(train edge {e_tr:+.4f})")
te = [r for r in test if lo <= r["ask"] < hi and aligned(r)]
e_te, n_te = edge(te)
ci = ci_edge(te)
print(f"ITS OUT-OF-SAMPLE RESULT : {e_te:+.4f} over {n_te} markets")
if ci:
    print(f"                    95% CI: [{ci[0]:+.4f}, {ci[1]:+.4f}]"
          f"  -> {'clears zero' if ci[0] > 0 else 'SPANS ZERO'}")
base_e, base_n = edge(test)
print(f"\nbaseline on the same test half: {base_e:+.4f} over {base_n} markets")
print(f"difference: {e_te - base_e:+.4f} per contract")
