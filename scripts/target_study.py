"""Daily-target study on the RECORDED signals and their RECORDED outcomes only.

Every BTC primary alert (strategy_alerts), its side and ask at the alert poll
(observations at observed_ms = created_at), its settled result (predictions.won,
stamped from Kalshi's result). Sized like live: contracts = floor(stake / ask),
fee = the system's own kalshi_fee_charged. Held to the recorded result (no exit
replay). Per NY day, a target stops the rest of that day's signals.
"""
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime
from zoneinfo import ZoneInfo

sys.path.insert(0, r"D:\Kalshi\btc15-signal\src")
from btc15_signal.validation import contracts_for_budget, kalshi_fee_charged  # noqa: E402

NY = ZoneInfo("America/New_York")
START = int(datetime(2026, 9, 22, 23, 0, tzinfo=NY).timestamp() * 1000)
con = sqlite3.connect(r"file:D:\Kalshi\btc15-signal\btc15.db?mode=ro", uri=True)
rows = con.execute("""
    SELECT a.window_open, a.created_at, o.side, o.our_ask, p.won, p.side
    FROM strategy_alerts a
    JOIN observations o ON o.window_open = a.window_open AND o.observed_ms = a.created_at
    LEFT JOIN predictions p ON p.window_open = a.window_open
    WHERE a.strategy = 'primary' AND a.window_open >= ?
    ORDER BY a.window_open""", (START,)).fetchall()
signals, dropped = [], defaultdict(int)
for wo, _at, side, ask, won, pside in rows:
    if ask is None or not 0 < ask < 1:
        dropped["no ask at the alert"] += 1
        continue
    if won is None:
        dropped["not settled yet"] += 1
        continue
    if pside and pside != side:
        dropped["side mismatch"] += 1
        continue
    day = datetime.fromtimestamp(wo / 1000, NY).strftime("%m-%d")
    signals.append((wo, day, float(ask), int(won)))

ACCOUNTS = [("You ($5 on $104.77)", 5.0, 104.77),
            ("Wife ($2 on $25.39)", 2.0, 25.39),
            ("Uncle George ($2 on $19.03)", 2.0, 19.03)]
RATES = [None, 0.02, 0.03, 0.04, 0.05, 0.06, 0.08, 0.10, 0.15, 0.20]
days = sorted({d for _, d, _, _ in signals})


def pnl_of(stake, ask, won):
    n = contracts_for_budget(stake, ask)
    return n * (won - ask) - kalshi_fee_charged(ask, n)


print(f"signals used: {len(signals)} ({signals[0][1]} to {signals[-1][1]}, {len(days)} NY days); "
      f"dropped: {dict(dropped)}")
print(f"won {sum(s[3] for s in signals)} / {len(signals)} "
      f"({100 * sum(s[3] for s in signals) / len(signals):.1f}%), avg price "
      f"{sum(s[2] for s in signals) / len(signals):.3f}\n")

for label, stake, cap in ACCOUNTS:
    print(f"=== {label}")
    print(f"{'target':>7} {'total':>9} {'days hit':>9} {'trades':>7} {'skipped':>8} "
          f"{'skipped P&L':>12} {'worst day':>10}  per day")
    base_by_day = defaultdict(float)
    for _, d, ask, won in signals:
        base_by_day[d] += pnl_of(stake, ask, won)
    for rate in RATES:
        target = None if rate is None else rate * cap
        by_day, hit, taken, skipped, skipped_pnl = defaultdict(float), 0, 0, 0, 0.0
        for d in days:
            day_pnl, stopped = 0.0, False
            for _, sd, ask, won in signals:
                if sd != d:
                    continue
                p = pnl_of(stake, ask, won)
                if stopped:
                    skipped += 1
                    skipped_pnl += p
                    continue
                taken += 1
                day_pnl += p
                if target is not None and day_pnl + 1e-9 >= target:
                    stopped, hit = True, hit + 1
            by_day[d] = day_pnl
        total = sum(by_day.values())
        name = "none" if rate is None else f"{rate:.0%}"
        per_day = " ".join(f"{by_day[d]:+6.2f}" for d in days)
        print(f"{name:>7} {total:>+9.2f} {hit:>6}/{len(days)} {taken:>7} {skipped:>8} "
              f"{skipped_pnl:>+12.2f} {min(by_day.values()):>+10.2f}  {per_day}")
    print(f"{'days:':>7} {' ' * 60}{' '.join(f'{d:>6}' for d in days)}\n")

# After the day first reaches 3% (primary), what did the REST of that day make?
stake, cap = 5.0, 104.77
after = []
for d in days:
    day_pnl, reached = 0.0, False
    for _, sd, ask, won in signals:
        if sd != d:
            continue
        p = pnl_of(stake, ask, won)
        if reached:
            after.append((d, p))
        day_pnl += p
        if not reached and day_pnl >= 0.03 * cap:
            reached = True
n = len(after)
wins = sum(1 for _, p in after if p > 0)
print(f"AFTER reaching 3% (You, $5): {n} later signals, won {wins} ({100 * wins / max(n, 1):.0f}%), "
      f"total {sum(p for _, p in after):+.2f}, avg {sum(p for _, p in after) / max(n, 1):+.3f} per trade")
per = defaultdict(float)
for d, p in after:
    per[d] += p
print("  by day: " + " ".join(f"{d} {v:+.2f}" for d, v in sorted(per.items())))
