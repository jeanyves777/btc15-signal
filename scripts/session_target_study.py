"""A profit target PER SESSION instead of per day - on the RECORDED signals and
their RECORDED outcomes only (operator, 2026-09-30: "test 5% target per session").

Same inputs and sizing as scripts/target_study.py, so the two compare directly:
every BTC primary alert (strategy_alerts), its side and ask at the alert poll
(observations at observed_ms = created_at), its settled result (predictions.won);
contracts = floor(stake / ask), the system's own fee, held to the result.

Sessions are the system's own (sessions.SESSIONS, UTC): asia 00-07, europe 07-13,
us 13-21, late-us 21-24 - in New York time 20:00-03:00, 03:00-09:00, 09:00-17:00,
17:00-20:00. A session target stops that session's remaining signals; the next
session starts fresh. Days are New York days, as the live daily target uses.

    python scripts/session_target_study.py [rate ...]      # default 0.05
"""
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime
from zoneinfo import ZoneInfo

sys.path.insert(0, r"D:\Kalshi\btc15-signal\src")
from btc15_signal.sessions import session_of  # noqa: E402
from btc15_signal.validation import contracts_for_budget, kalshi_fee_charged  # noqa: E402

NY = ZoneInfo("America/New_York")
START = int(datetime(2026, 9, 22, 23, 0, tzinfo=NY).timestamp() * 1000)
con = sqlite3.connect(r"file:D:\Kalshi\btc15-signal\btc15.db?mode=ro", uri=True)
rows = con.execute("""
    SELECT a.window_open, o.side, o.our_ask, p.won, p.side
    FROM strategy_alerts a
    JOIN observations o ON o.window_open = a.window_open AND o.observed_ms = a.created_at
    LEFT JOIN predictions p ON p.window_open = a.window_open
    WHERE a.strategy = 'primary' AND a.window_open >= ?
    ORDER BY a.window_open""", (START,)).fetchall()

signals, dropped = [], defaultdict(int)
for wo, side, ask, won, pside in rows:
    if ask is None or not 0 < ask < 1:
        dropped["no ask at the alert"] += 1
        continue
    if won is None:
        dropped["not settled yet"] += 1
        continue
    if pside and pside != side:
        dropped["side mismatch"] += 1
        continue
    utc_day = datetime.fromtimestamp(wo / 1000, ZoneInfo("UTC")).strftime("%m-%d")
    signals.append({
        "wo": wo, "ask": float(ask), "won": int(won),
        "day": datetime.fromtimestamp(wo / 1000, NY).strftime("%m-%d"),
        "session": (utc_day, session_of(wo)),     # one session instance
    })

ACCOUNTS = [("You", 5.0, 104.77, 0.08), ("Wife", 2.0, 25.39, 0.15),
            ("Uncle George", 2.0, 19.03, 0.15)]
RATES = [float(a) for a in sys.argv[1:]] or [0.05]
days = sorted({s["day"] for s in signals})


def pnl_of(stake, ask, won):
    n = contracts_for_budget(stake, ask)
    return n * (won - ask) - kalshi_fee_charged(ask, n)


def run(stake, target, unit):
    """P&L per NY day with a target that resets on `unit` ('day' or 'session')."""
    by_day, acc, stopped = defaultdict(float), defaultdict(float), set()
    taken = skipped = hits = 0
    skipped_pnl = 0.0
    for s in signals:
        key = s["day"] if unit == "day" else s["session"]
        p = pnl_of(stake, s["ask"], s["won"])
        if key in stopped:
            skipped += 1
            skipped_pnl += p
            continue
        taken += 1
        acc[key] += p
        by_day[s["day"]] += p
        if target is not None and acc[key] + 1e-9 >= target:
            stopped.add(key)
            hits += 1
    return by_day, taken, skipped, skipped_pnl, hits


n_sessions = len({s["session"] for s in signals})
won = sum(s["won"] for s in signals)
print(f"signals used: {len(signals)} ({days[0]} to {days[-1]}, {len(days)} NY days, "
      f"{n_sessions} sessions); dropped: {dict(dropped)}")
print(f"won {won} / {len(signals)} ({100 * won / len(signals):.1f}%), held to the result, "
      "no cash-out, no cushion - as scripts/target_study.py\n")

for label, stake, cap, live_rate in ACCOUNTS:
    print(f"=== {label} (${stake:.0f} a signal, ${cap} account)")
    print(f"{'rule':<22} {'total':>8} {'hits':>9} {'trades':>7} {'skipped':>8} "
          f"{'skipped P&L':>12} {'worst day':>10} {'days < 0':>9}")
    plans = [("no target", None, "day"),
             (f"daily {live_rate:.0%} (live)", live_rate * cap, "day")]
    plans += [(f"{r:.0%} per session", r * cap, "session") for r in RATES]
    results = {}
    for name, target, unit in plans:
        by_day, taken, skipped, skipped_pnl, hits = run(stake, target, unit)
        results[name] = by_day
        units = len(days) if unit == "day" else n_sessions
        print(f"{name:<22} {sum(by_day.values()):>+8.2f} {hits:>4}/{units:<4} {taken:>7} "
              f"{skipped:>8} {skipped_pnl:>+12.2f} {min(by_day[d] for d in days):>+10.2f} "
              f"{sum(1 for d in days if by_day[d] < 0):>9}")
    print(f"{'  per NY day':<22} " + " ".join(f"{d:>6}" for d in days))
    for name in results:
        print(f"{'  ' + name:<22} " + " ".join(f"{results[name][d]:>+6.2f}" for d in days))
    print()

# Where the money is made, by session (no target, You at $5)
per = defaultdict(lambda: [0, 0, 0.0])
for s in signals:
    name = s["session"][1]
    per[name][0] += 1
    per[name][1] += s["won"]
    per[name][2] += pnl_of(5.0, s["ask"], s["won"])
print("By session, no target, You at $5:")
for name in ("asia", "europe", "us", "late-us"):
    n, w, p = per[name]
    print(f"  {name:<8} {n:>4} signals, won {100 * w / max(n, 1):5.1f}%, {p:>+7.2f}")
