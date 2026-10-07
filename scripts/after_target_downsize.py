"""Instead of pausing at the daily target, keep trading at a LOWER base until
midnight - on the RECORDED signals and outcomes only (operator, 2026-09-30:
"after we hit the 8% at $5 base then we move to lower base for all remaining of
the day at $2 base until midnight and back to $5 again").

Same inputs, sizing and fixed account sizes as scripts/target_study.py: every
BTC primary alert, its side and ask at the alert poll, its settled result
(predictions.won); contracts = floor(stake / ask), the system's own fee, held
to the result (no cash-out, no cushion). The switch happens once the day's
P&L reaches the target, on the next signal; it holds until 00:00 New York.

    python scripts/after_target_downsize.py [high] [low] [rate] [capital]
    default: 5 2 0.08 104.77  (You);  mirrors analogue: 2 1 0.15 25.39
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
args = [float(a) for a in sys.argv[1:]]
HIGH, LOW, RATE, CAP = (args + [5.0, 2.0, 0.08, 104.77][len(args):])[:4]
TARGET = RATE * CAP

con = sqlite3.connect(r"file:D:\Kalshi\btc15-signal\btc15.db?mode=ro", uri=True)
rows = con.execute("""
    SELECT a.window_open, o.side, o.our_ask, p.won, p.side
    FROM strategy_alerts a
    JOIN observations o ON o.window_open = a.window_open AND o.observed_ms = a.created_at
    LEFT JOIN predictions p ON p.window_open = a.window_open
    WHERE a.strategy = 'primary' AND a.window_open >= ?
    ORDER BY a.window_open""", (START,)).fetchall()
signals = [(wo, datetime.fromtimestamp(wo / 1000, NY).strftime("%m-%d"), float(ask), int(won))
           for wo, side, ask, won, ps in rows
           if ask and 0 < ask < 1 and won is not None and (not ps or ps == side)]
days = sorted({d for _, d, _, _ in signals})


def pnl_of(stake, ask, won):
    n = contracts_for_budget(stake, ask)
    return n * (won - ask) - kalshi_fee_charged(ask, n)


def run(after):
    """after: 'trade' (same stake), 'pause', or 'lower'. Per day: total, the part
    after the target, switch time, lowest point of the day."""
    out = {}
    for d in days:
        day, reached, after_pnl, at, low, n_after = 0.0, False, 0.0, None, 0.0, 0
        for wo, sd, ask, won in signals:
            if sd != d:
                continue
            if reached and after == "pause":
                continue
            stake = LOW if (reached and after == "lower") else HIGH
            p = pnl_of(stake, ask, won)
            day += p
            low = min(low, day)
            if reached:
                after_pnl += p
                n_after += 1
            elif day + 1e-9 >= TARGET:
                reached, at = True, datetime.fromtimestamp(wo / 1000, NY).strftime("%H:%M")
        out[d] = (day, after_pnl, at, low, n_after)
    return out


plans = [("no target", "trade"), (f"pause at {RATE:.0%} (live)", "pause"),
         (f"${LOW:g} after {RATE:.0%}", "lower")]
res = {name: run(mode) for name, mode in plans}
won = sum(s[3] for s in signals)
print(f"{len(signals)} signals, {days[0]} to {days[-1]} ({len(days)} NY days), won "
      f"{100 * won / len(signals):.1f}%; ${HIGH:g} base, target {RATE:.0%} of {CAP} = {TARGET:.2f}; "
      "held to the result")
print(f"\n{'rule':<20} {'total':>8} {'worst day':>10} {'losing days':>12} {'lowest point':>13}")
for name, _ in plans:
    r = res[name]
    print(f"{name:<20} {sum(v[0] for v in r.values()):>+8.2f} {min(v[0] for v in r.values()):>+10.2f} "
          f"{sum(1 for v in r.values() if v[0] < 0):>12} {min(v[3] for v in r.values()):>+13.2f}")

lower = res[f"${LOW:g} after {RATE:.0%}"]
pause = res[f"pause at {RATE:.0%} (live)"]
print(f"\n{'day':<6} {'hit at':>7} {'pause':>8} {'$' + format(LOW, 'g') + ' after':>9} "
      f"{'= the $' + format(LOW, 'g') + ' part':>14} {'signals':>8} {'no target':>10}")
for d in days:
    day, after_pnl, at, _low, n_after = lower[d]
    print(f"{d:<6} {at or '-':>7} {pause[d][0]:>+8.2f} {day:>+9.2f} {after_pnl:>+14.2f} {n_after:>8} "
          f"{res['no target'][d][0]:>+10.2f}")
parts = [lower[d][1] for d in days if lower[d][2]]
print(f"\nthe ${LOW:g} part: {sum(parts):+.2f} over {len(parts)} days it ran, "
      f"positive on {sum(1 for p in parts if p > 0)}, negative on {sum(1 for p in parts if p < 0)}")
