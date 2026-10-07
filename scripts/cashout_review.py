"""THE CASH-OUT, KEPT UNDER WATCH (operator, 2026-10-01: "when we do the 2000 signals
evaluation, we will have to verify if we still need to keep the early cash out or remove
it... so far it makes us lose about $4... make sure that we are not leaving money on the
table while it adds nothing"). On the 2,000-signal checklist (FINDINGS 127, 132).

For every $ trade the primary SOLD early (allsignal_trades.exit_*), compare the sale with
holding to settlement: same entry either way, so only the exit differs.
    sold     = contracts x sale price - sale fee
    held     = contracts x (1 if the side won else 0)       (no fee at settlement)
    cash-out = sold - held      < 0: a winner sold - money left on the table
                                > 0: a loser sold before it lost - money saved
Primary only; each mirror copies the primary's exits at its own size, so its figure
scales with its contracts.

    python scripts/cashout_review.py                  # everything recorded
    python scripts/cashout_review.py 2026-10-02       # the 2,000-signal window
"""
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime
from zoneinfo import ZoneInfo

NY = ZoneInfo("America/New_York")
since = sys.argv[1] if len(sys.argv) > 1 else "2000-01-01"
start = int(datetime.fromisoformat(since).replace(tzinfo=NY).timestamp() * 1000)

db = sqlite3.connect("file:D:/Kalshi/btc15-signal/btc15.db?mode=ro", uri=True)
rows = db.execute("""
    SELECT window_open, ticker, side, exit_price, exit_count, COALESCE(exit_fee, 0), won,
           COALESCE(stake, 0)
    FROM allsignal_trades
    WHERE exit_price IS NOT NULL AND exit_count > 0 AND won IS NOT NULL AND window_open >= ?
    ORDER BY window_open""", (start,)).fetchall()
pending = db.execute("SELECT COUNT(*) FROM allsignal_trades WHERE exit_price IS NOT NULL "
                     "AND won IS NULL AND window_open >= ?", (start,)).fetchone()[0]
held_total = db.execute("SELECT COUNT(*) FROM allsignal_trades WHERE status='filled' "
                        "AND won IS NOT NULL AND window_open >= ?", (start,)).fetchone()[0]

if not rows:
    print(f"no graded cash-outs since {since}")
    raise SystemExit

by_day = defaultdict(lambda: [0, 0.0])
winners, losers = [], []
for wo, ticker, side, price, n, fee, won, stake in rows:
    effect = n * (price - won) - fee
    (winners if won else losers).append((effect, price, n))
    d = datetime.fromtimestamp(wo / 1000, NY).strftime("%m-%d")
    by_day[d][0] += 1
    by_day[d][1] += effect

total = sum(e for e, _, _ in winners + losers)
print(f"CASH-OUTS since {since}: {len(rows)} of {held_total} filled $ trades sold early "
      f"(primary; {pending} not graded yet)")
print(f"  winners sold early  {len(winners):>4}  left on the table "
      f"{sum(e for e, _, _ in winners):+8.2f}  (mean sale {sum(p for _, p, _ in winners) / max(1, len(winners)):.3f})")
print(f"  losers sold early   {len(losers):>4}  saved              "
      f"{sum(e for e, _, _ in losers):+8.2f}" + (f"  (mean sale {sum(p for _, p, _ in losers) / len(losers):.3f})"
                                                if losers else ""))
print(f"  NET effect of the cash-out vs holding: {total:+.2f}  "
      f"({total / len(rows):+.4f} per cash-out)")
print("  by day:  " + "  ".join(f"{d} {n}x {v:+.2f}" for d, (n, v) in sorted(by_day.items())))
print("  The cash-out pays for itself only when a loser it sold early saves more than the "
      "winners it sold give up.")
