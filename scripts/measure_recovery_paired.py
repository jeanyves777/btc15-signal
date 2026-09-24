"""Does the DEPLOYED recovery upsize beat doing nothing? Paired, day-clustered.

This is the one comparison in the study that is not fitted: both arms take the
SAME markets in the same order, and differ only in how many contracts the
deployed rule buys when a deficit is open. So it can be paired market by
market, which removes all the variance the two arms share and leaves only the
rule's own contribution.

Clustered by DAY because the rule's state carries within a day - a loss in the
morning changes the size of every trade after it - so trades are not
independent draws and treating them as such would overstate the evidence.
"""
import random
import sqlite3
import statistics
import sys
from datetime import UTC, datetime

sys.path.insert(0, r"D:\Kalshi\btc15-signal\src")
from btc15_signal.validation import kalshi_fee_charged  # noqa: E402

db = sqlite3.connect(r"file:D:\Kalshi\btc15-signal\data\cohort.db?mode=ro", uri=True)
db.row_factory = sqlite3.Row
ROWS = db.execute(
    "SELECT open_ms, ask, won FROM cohort "
    "WHERE remaining = 10 AND ask >= 0.70 AND ask < 0.94 ORDER BY open_ms"
).fetchall()


def pnl(ask, won, count):
    return ((1.0 - ask) if won else -ask) * count - kalshi_fee_charged(ask, count)


def day_of(ms):
    return datetime.fromtimestamp(ms / 1000, UTC).strftime("%Y-%m-%d")


# Replay both arms together so the deficit state is exactly the deployed one.
per_day_a: dict[str, float] = {}
per_day_b: dict[str, float] = {}
deficit = 0.0
upsized = 0
for r in ROWS:
    d = day_of(r["open_ms"])
    count = 1
    if deficit > 0:
        share = deficit / 4
        best = 2 * (1.0 - r["ask"]) - kalshi_fee_charged(r["ask"], 2)
        if best >= share:
            count = 2
    if count == 2:
        upsized += 1
    a = pnl(r["ask"], r["won"], 1)
    b = pnl(r["ask"], r["won"], count)
    per_day_a[d] = per_day_a.get(d, 0.0) + a
    per_day_b[d] = per_day_b.get(d, 0.0) + b
    # The deficit follows the DEPLOYED arm, which is the rule under test.
    deficit = deficit + -b if b < 0 else max(0.0, deficit - b)

days = sorted(per_day_a)
diffs = [per_day_b[d] - per_day_a[d] for d in days]
print(f"markets {len(ROWS)}   days {len(days)}   upsized trades {upsized} "
      f"({upsized/len(ROWS):.1%})")
print(f"baseline total  {sum(per_day_a.values()):+.2f}")
print(f"deployed total  {sum(per_day_b.values()):+.2f}")
print(f"difference      {sum(diffs):+.2f}  over {len(days)} days")
print(f"mean per day    {statistics.mean(diffs):+.4f}")

random.seed(7)
boot = []
for _ in range(20000):
    sample = [diffs[random.randrange(len(diffs))] for _ in range(len(diffs))]
    boot.append(sum(sample))
boot.sort()
lo, hi = boot[int(0.025 * len(boot))], boot[int(0.975 * len(boot))]
print(f"\nday-clustered bootstrap 95% CI on the TOTAL difference: "
      f"[{lo:+.2f}, {hi:+.2f}]")
print("  ->", "CLEARS ZERO" if (lo > 0 or hi < 0) else "SPANS ZERO")

worse = sum(1 for v in diffs if v < 0)
print(f"\ndays the deployed rule did worse: {worse}/{len(days)}")
print(f"worst day under baseline : {min(per_day_a.values()):+.2f}")
print(f"worst day under deployed : {min(per_day_b.values()):+.2f}")
