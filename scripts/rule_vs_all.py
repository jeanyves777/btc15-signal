"""Rule-based signals vs all signals, on the RECORDED signals and outcomes, under
the live sizing and daily targets (You $5 / 8%, mirrors $2 / 15%).

ALL  = every BTC primary alert (strategy_alerts) at its alert poll
       (observations at observed_ms = created_at), outcome predictions.won.
RULE = the main strategy's rule: the FIRST poll per window where it qualified
       (intelligence_decisions.base_qualified = 1), its side, ask and graded won.
Held to the recorded result, the system's own fee, grouped by New York day.
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


def day(ms):
    return datetime.fromtimestamp(ms / 1000, NY).strftime("%m-%d")


ALL = [(wo, day(wo), float(ask), int(won)) for wo, ask, won in con.execute("""
    SELECT a.window_open, o.our_ask, p.won
    FROM strategy_alerts a
    JOIN observations o ON o.window_open = a.window_open AND o.observed_ms = a.created_at
    JOIN predictions p ON p.window_open = a.window_open AND p.side = o.side
    WHERE a.strategy = 'primary' AND a.window_open >= ? AND p.won IS NOT NULL
      AND o.our_ask > 0 AND o.our_ask < 1
    ORDER BY a.window_open""", (START,))]

RULE = []
for wo, ask, won, rem in con.execute("""
    SELECT d.window_open, d.ask, d.won, d.remaining_s FROM intelligence_decisions d
    JOIN (SELECT window_open, MIN(decided_ms) AS first FROM intelligence_decisions
          WHERE base_qualified = 1 AND window_open >= ? GROUP BY window_open) f
      ON f.window_open = d.window_open AND f.first = d.decided_ms
    WHERE d.base_qualified = 1 AND d.won IS NOT NULL AND d.ask > 0 AND d.ask < 1
    ORDER BY d.window_open""", (START,)):
    RULE.append((wo, day(wo), float(ask), int(won)))

ACCOUNTS = [("You", 5.0, 104.77, 0.08), ("Wife", 2.0, 25.39, 0.15),
            ("Uncle George", 2.0, 19.03, 0.15)]
days = sorted({d for _, d, _, _ in ALL} | {d for _, d, _, _ in RULE})


def pnl(stake, ask, won):
    n = contracts_for_budget(stake, ask)
    return n * (won - ask) - kalshi_fee_charged(ask, n)


def run(signals, stake, cap, rate):
    target = None if rate is None else rate * cap
    by_day, taken, wins = defaultdict(float), 0, 0
    for d in days:
        total, stop = 0.0, False
        for _, sd, ask, won in signals:
            if sd != d or stop:
                continue
            p = pnl(stake, ask, won)
            total += p
            taken += 1
            wins += p > 0
            if target is not None and total + 1e-9 >= target:
                stop = True
        by_day[d] = total
    return by_day, taken, wins


for name, sig in (("ALL SIGNALS", ALL), ("RULE-BASED", RULE)):
    n = len(sig)
    print(f"{name}: {n} signals, won {sum(s[3] for s in sig)} "
          f"({100 * sum(s[3] for s in sig) / max(n, 1):.1f}%), avg price "
          f"{sum(s[2] for s in sig) / max(n, 1):.3f}")
print(f"days: {' '.join(days)}  (09-22 from 23:00; 09-29 to ~20:15)\n")

for label, stake, cap, rate in ACCOUNTS:
    print(f"=== {label}: ${stake:g} per signal, target {rate:.0%} of ${cap:.2f} = ${rate * cap:.2f}")
    for name, sig in (("ALL", ALL), ("RULE", RULE)):
        for r, tag in ((rate, f"{rate:.0%} target"), (None, "no target")):
            by_day, taken, wins = run(sig, stake, cap, r)
            total = sum(by_day.values())
            hit = sum(1 for d in days if r is not None and by_day[d] >= r * cap - 1e-9)
            print(f"  {name:<4} {tag:<10} total {total:+8.2f}  trades {taken:>4}  "
                  f"won {100 * wins / max(taken, 1):5.1f}%  days hit {hit}/{len(days)}  "
                  f"worst day {min(by_day.values()):+7.2f}  | "
                  + " ".join(f"{by_day[d]:+6.2f}" for d in days))
    print()
