"""After a loss, would waiting inside the NEXT signal's window for a better entry
have helped? One New York day, recorded polls only.

Operator, 2026-09-29: "do we have regime data - volatility and price distance at
the time of entry - that can tell if waiting for a better price distance of the
market and getting in at 70 cents could have helped, on the next signal after
the loss; study on just that day."

For each signal that follows a loss on the day: the facts at the alert
(volatility_5m_bps, distance_bps = cushion from the target line on the signal's
side, price), then three waits on the SAME side inside that window, entering at
the recorded ask, never with under 120 s left:
  A  its ask <= 0.70;   B  cushion >= 5 bps;   C  both.
Outcome = Kalshi's result for the window (predictions). $5, fee included.

    python scripts/wait_after_loss_day.py 2026-09-26
"""
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from btc15_signal.validation import contracts_for_budget, kalshi_fee_charged  # noqa: E402

NY = ZoneInfo("America/New_York")
DAY = sys.argv[1] if len(sys.argv) > 1 else "2026-09-26"
a = int(datetime.fromisoformat(DAY).replace(tzinfo=NY).timestamp() * 1000)
con = sqlite3.connect(f"file:{ROOT / 'btc15.db'}?mode=ro", uri=True)
sigs = con.execute("""
    SELECT a.window_open, a.created_at, o.side, o.our_ask, o.distance_bps,
           o.volatility_5m_bps, p.won
    FROM strategy_alerts a
    JOIN observations o ON o.window_open = a.window_open AND o.observed_ms = a.created_at
    JOIN predictions p ON p.window_open = a.window_open AND p.side = o.side
    WHERE a.strategy = 'primary' AND a.window_open >= ? AND a.window_open < ? AND p.won IS NOT NULL
    ORDER BY a.window_open""", (a, a + 86_400_000)).fetchall()


def pnl(ask, won):
    n = contracts_for_budget(5.0, ask)
    return n * (won - ask) - kalshi_fee_charged(ask, n)


def wait(wo, created, side, cond):
    """First poll at/after the alert where cond(ask, cushion) holds on `side`."""
    for ms, rside, yes_ask, no_ask, dist, rem in con.execute(
            "SELECT observed_ms, side, yes_ask, no_ask, distance_bps, remaining_s "
            "FROM observations WHERE window_open = ? AND observed_ms >= ? "
            "ORDER BY observed_ms", (wo, created)):
        if rem is not None and rem < 120:
            return None
        ask = yes_ask if side == "UP" else no_ask
        cushion = (dist or 0.0) if rside == side else -(dist or 0.0)
        if ask and 0 < ask < 1 and cond(ask, cushion):
            return ask, ms
    return None


rows, prev_lost = [], False
for wo, created, side, ask, dist, vol, won in sigs:
    if prev_lost:
        t = datetime.fromtimestamp(wo / 1000, NY).strftime("%H:%M")
        alts = {}
        for key, cond in (("A <=70c", lambda k, c: k <= 0.70),
                          ("B cushion>=5", lambda k, c: c >= 5.0),
                          ("C both", lambda k, c: k <= 0.70 and c >= 5.0)):
            got = wait(wo, created, side, cond)
            alts[key] = None if got is None else (got[0], pnl(got[0], won),
                                                  (got[1] - created) / 1000)
        rows.append((t, side, ask, dist, vol, won, pnl(ask, won), alts))
    prev_lost = not won

print(f"{DAY}: {len(sigs)} signals; {len(rows)} came right after a loss\n")
print(f"{'time':<6}{'side':<5}{'price':>6}{'cushion':>9}{'vol':>6}{'result':>8}{'at alert':>10}   "
      f"{'A wait <=70c':<22}{'B cushion>=5bps':<22}{'C both'}")
tot = {"alert": 0.0, "A <=70c": 0.0, "B cushion>=5": 0.0, "C both": 0.0}
for t, side, ask, dist, vol, won, p, alts in rows:
    tot["alert"] += p
    cells = []
    for key in ("A <=70c", "B cushion>=5", "C both"):
        v = alts[key]
        if v is None:
            cells.append("no entry (skip)")
        else:
            tot[key] += v[1]
            cells.append(f"{v[0] * 100:.0f}c {v[1]:+.2f} +{v[2]:.0f}s")
    print(f"{t:<6}{side:<5}{ask * 100:>5.0f}c{dist or 0:>8.1f}b{vol or 0:>6.2f}{'WON' if won else 'LOST':>8}"
          f"{p:>+10.2f}   {cells[0]:<22}{cells[1]:<22}{cells[2]}")
print(f"\n{'total':<40}{tot['alert']:>+10.2f}   {tot['A <=70c']:<+22.2f}{tot['B cushion>=5']:<+22.2f}"
      f"{tot['C both']:+.2f}")
for label, grp in (("won", [r for r in rows if r[5]]), ("lost", [r for r in rows if not r[5]])):
    if grp:
        print(f"at the alert, the next signals that {label}: n {len(grp)}, avg price "
              f"{sum(r[2] for r in grp) / len(grp) * 100:.0f}c, avg cushion "
              f"{sum(r[3] or 0 for r in grp) / len(grp):.1f} bps, avg vol "
              f"{sum(r[4] or 0 for r in grp) / len(grp):.2f}")
