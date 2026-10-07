"""Two New York days side by side, on the RECORDED signals and outcomes only
(operator, 2026-09-30: "compare to yesterday how the market did").

Every BTC primary alert, its side and ask at the alert poll, its settled result;
sized like live (You $5, mirrors $2), the system's own fee, held to the result
(no cash-out, no cushion) - as scripts/target_study.py. Each day also shows what
its daily target (8% You / 15% mirrors, from 00:00) would have done, and when.

    python scripts/day_compare.py 2026-09-29 2026-09-30
"""
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime
from zoneinfo import ZoneInfo

sys.path.insert(0, r"D:\Kalshi\btc15-signal\src")
from btc15_signal.validation import contracts_for_budget, kalshi_fee_charged  # noqa: E402

NY = ZoneInfo("America/New_York")
ROOT = r"D:\Kalshi\btc15-signal"
days = sys.argv[1:] or [datetime.now(NY).strftime("%Y-%m-%d")]
con = sqlite3.connect(rf"file:{ROOT}\btc15.db?mode=ro", uri=True)
guard = sqlite3.connect(rf"file:{ROOT}\runtime\daily_profit.db?mode=ro", uri=True)
ACCOUNTS = [("primary", "You", 5.0, 0.08), ("m1", "Wife", 2.0, 0.15), ("m2", "Uncle George", 2.0, 0.15)]


def pnl_of(stake, ask, won):
    n = contracts_for_budget(stake, ask)
    return n * (won - ask) - kalshi_fee_charged(ask, n)


def signals(day):
    start = int(datetime.fromisoformat(day).replace(tzinfo=NY).timestamp() * 1000)
    rows = con.execute("""
        SELECT a.window_open, o.side, o.our_ask, p.won, p.side
        FROM strategy_alerts a
        JOIN observations o ON o.window_open = a.window_open AND o.observed_ms = a.created_at
        LEFT JOIN predictions p ON p.window_open = a.window_open
        WHERE a.strategy = 'primary' AND a.window_open >= ? AND a.window_open < ?
        ORDER BY a.window_open""", (start, start + 86_400_000)).fetchall()
    return [(wo, float(ask), int(won)) for wo, side, ask, won, ps in rows
            if ask and 0 < ask < 1 and won is not None and (not ps or ps == side)]


def hour(ms):
    return datetime.fromtimestamp(ms / 1000, NY).strftime("%H")


data = {d: signals(d) for d in days}
last_hour = min(max((int(hour(s[0])) for s in data[d]), default=0) for d in days)

for day in days:
    sig = data[day]
    n = len(sig)
    if not n:
        print(f"=== {day}: no settled signals\n")
        continue
    won = sum(s[2] for s in sig)
    print(f"=== {day}: {n} settled signals, won {won} ({100 * won / n:.0f}%), "
          f"avg price {sum(s[1] for s in sig) / n:.2f}")
    for account, label, stake, rate in ACCOUNTS:
        opening = guard.execute("SELECT opening FROM profit_days WHERE account=? AND day=?",
                                (account, day)).fetchone()
        opening = opening[0] if opening else None
        total = run = 0.0
        low = 0.0
        hit_at = hit_pnl = None
        for wo, ask, w in sig:
            p = pnl_of(stake, ask, w)
            total += p
            run += p
            low = min(low, run)
            if opening and hit_at is None and run >= rate * opening - 1e-9:
                hit_at, hit_pnl = wo, run
        target = f"{rate:.0%} of {opening:.2f} = {rate * opening:.2f}" if opening else "no opening"
        hit = (f"hit {datetime.fromtimestamp(hit_at / 1000, NY):%H:%M} at {hit_pnl:+.2f}, rest of day "
               f"{total - hit_pnl:+.2f}") if hit_at else "not hit"
        print(f"   {label:<13} ${stake:.0f}: all signals {total:+7.2f} (lowest point {low:+.2f}) | "
              f"target {target}: {hit}")
    print()

print(f"By hour, You at $5 (won/signals, P&L){'':>4}" + "".join(f"{d:>22}" for d in days))
hours = sorted({hour(s[0]) for d in days for s in data[d]})
for h in hours:
    cells = []
    for d in days:
        hs = [s for s in data[d] if hour(s[0]) == h]
        cells.append(f"{sum(s[2] for s in hs)}/{len(hs)} {sum(pnl_of(5.0, s[1], s[2]) for s in hs):+7.2f}"
                     if hs else "-")
    print(f"   {h}:00{'':>32}" + "".join(f"{c:>22}" for c in cells))

print(f"\nSame hours on both days (00:00 to {last_hour:02d}:59), You at $5:")
for d in days:
    hs = [s for s in data[d] if int(hour(s[0])) <= last_hour]
    if hs:
        print(f"   {d}: {len(hs)} signals, won {100 * sum(s[2] for s in hs) / len(hs):.0f}%, "
              f"{sum(pnl_of(5.0, s[1], s[2]) for s in hs):+.2f}")
