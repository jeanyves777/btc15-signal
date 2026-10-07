"""What the signals did AFTER each account reached its daily target - on the
RECORDED signals and outcomes only (operator, 2026-09-30: "after we had reached
our target how did the market do up to now").

The pause time is each account's own, read from runtime/daily_profit.db. Every
BTC primary alert after it (strategy_alerts), its side and ask at the alert poll
(observations at observed_ms = created_at), its settled result (predictions.won);
sized like live (contracts = floor(stake / ask)), the system's own fee, held to
the result (no cash-out) - as scripts/target_study.py.

    python scripts/after_target_day.py [YYYY-MM-DD]      # default: today (New York)
"""
import sqlite3
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

sys.path.insert(0, r"D:\Kalshi\btc15-signal\src")
from btc15_signal.validation import contracts_for_budget, kalshi_fee_charged  # noqa: E402

NY = ZoneInfo("America/New_York")
ROOT = r"D:\Kalshi\btc15-signal"
day = sys.argv[1] if len(sys.argv) > 1 else datetime.now(NY).strftime("%Y-%m-%d")
STAKES = {"primary": 5.0, "m1": 2.0, "m2": 2.0}

guard = sqlite3.connect(rf"file:{ROOT}\runtime\daily_profit.db?mode=ro", uri=True)
accounts = guard.execute(
    "SELECT account, label, opening, target, pnl, paused_ms FROM profit_days WHERE day = ? "
    "ORDER BY CASE account WHEN 'primary' THEN 0 WHEN 'm1' THEN 1 ELSE 2 END", (day,)).fetchall()
if not accounts:
    sys.exit(f"no daily-target rows for {day}")

con = sqlite3.connect(rf"file:{ROOT}\btc15.db?mode=ro", uri=True)
day_start = int(datetime.fromisoformat(day).replace(tzinfo=NY).timestamp() * 1000)
day_end = day_start + 86_400_000
rows = con.execute("""
    SELECT a.window_open, a.created_at, o.side, o.our_ask, p.won, p.side
    FROM strategy_alerts a
    JOIN observations o ON o.window_open = a.window_open AND o.observed_ms = a.created_at
    LEFT JOIN predictions p ON p.window_open = a.window_open
    WHERE a.strategy = 'primary' AND a.window_open >= ? AND a.window_open < ?
    ORDER BY a.window_open""", (day_start, day_end)).fetchall()


def pnl_of(stake, ask, won):
    n = contracts_for_budget(stake, ask)
    return n * (won - ask) - kalshi_fee_charged(ask, n)


def hm(ms):
    return datetime.fromtimestamp(ms / 1000, NY).strftime("%H:%M")


for account, label, opening, target, pnl, paused_ms in accounts:
    label = {"primary": "You"}.get(account, label)
    stake = STAKES[account]
    if not paused_ms:
        print(f"=== {label}: target not reached on {day} (+{pnl:.2f} of {target:.2f})\n")
        continue
    after = [r for r in rows if r[1] > paused_ms]
    settled = [r for r in after if r[3] and 0 < r[3] < 1 and r[4] is not None
               and (not r[5] or r[5] == r[2])]
    pending = len(after) - len(settled)
    run, peak, low, total, wins = 0.0, 0.0, 0.0, 0.0, 0
    by_hour: dict[str, list] = {}
    for wo, _at, _side, ask, won, _ps in settled:
        p = pnl_of(stake, ask, int(won))
        total += p
        run += p
        peak, low = max(peak, run), min(low, run)
        wins += int(won)
        h = by_hour.setdefault(datetime.fromtimestamp(wo / 1000, NY).strftime("%H"), [0, 0, 0.0])
        h[0] += 1
        h[1] += int(won)
        h[2] += p
    n = len(settled)
    print(f"=== {label} (${stake:.0f} a signal): paused {hm(paused_ms)} at +{pnl:.2f} "
          f"(target {target:.2f} on {opening:.2f})")
    if not n:
        print("   no settled signals since the pause\n")
        continue
    avg_ask = sum(r[3] for r in settled) / n
    print(f"   {n} signals since, won {wins} ({100 * wins / n:.0f}%), avg price {avg_ask:.2f}"
          f"{f', {pending} not settled yet' if pending else ''}")
    print(f"   had it kept trading: {total:+.2f}  (best point {peak:+.2f}, worst point {low:+.2f})")
    print(f"   the day would stand at {pnl + total:+.2f} instead of {pnl:+.2f}")
    print("   by hour: " + "  ".join(f"{h}:00 {v[1]}/{v[0]} {v[2]:+.2f}"
                                    for h, v in sorted(by_hour.items())))
    print()
