"""THE PRIMARY'S DAILY CAP (operator, 2026-10-02: "look for the max average profit target
so that we are not letting profit go up and down throughout the day and maybe not able to
recover on a bad afternoon... once reached after the first target, primary is done for
that day").

The live rule on the RECORDED BTC signals (direct arithmetic, as target_rules_live.py):
$6 below the 8% target, $3 at or above it (follow rule), 5 bps cushion after a loss,
held to the result - PLUS a cap: once the day's P&L reaches CAP% of the opening, no new
entry until midnight. CAP = none is today's live rule.

    python scripts/upper_target_sweep.py [capital]
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
CAP_BASE = float(sys.argv[1]) if len(sys.argv) > 1 else 107.33
HIGH, LOW, TARGET = 6.0, 3.0, 0.08

con = sqlite3.connect("file:D:/Kalshi/btc15-signal/btc15.db?mode=ro", uri=True)
rows = con.execute("""
    SELECT a.window_open, a.created_at, o.side, o.our_ask, p.won, p.side
    FROM strategy_alerts a
    JOIN observations o ON o.window_open = a.window_open AND o.observed_ms = a.created_at
    LEFT JOIN predictions p ON p.window_open = a.window_open
    WHERE a.strategy = 'primary' AND a.window_open >= ?
    ORDER BY a.window_open""", (START,)).fetchall()
signals = [dict(wo=wo, at=at, side=side, ask=float(ask), won=int(won),
                day=datetime.fromtimestamp(wo / 1000, NY).strftime("%m-%d"))
           for wo, at, side, ask, won, ps in rows
           if ask and 0 < ask < 1 and won is not None and (not ps or ps == side)]
polls = defaultdict(list)
for wo, ms, rside, ya, na, dist, rem in con.execute(
        "SELECT window_open, observed_ms, side, yes_ask, no_ask, distance_bps, remaining_s "
        "FROM observations WHERE window_open >= ? ORDER BY window_open, observed_ms", (START,)):
    polls[wo].append((ms, rside, ya, na, dist, rem))
today = datetime.now(NY).strftime("%m-%d")
days = sorted({s["day"] for s in signals if s["day"] != today and s["day"] != "09-22"})


def cushioned(s):
    for ms, rside, ya, na, dist, rem in polls[s["wo"]]:
        if ms < s["at"]:
            continue
        if rem is not None and rem < 120:
            return None
        ask = ya if s["side"] == "UP" else na
        cushion = (dist or 0.0) if rside == s["side"] else -(dist or 0.0)
        if ask and 0 < ask < 1 and cushion >= 5.0:
            return float(ask)
    return None


def day_run(d, cap):
    """(P&L, peak, time the cap was hit or None)."""
    pnl = peak = 0.0
    last, hit = None, None
    for s in (x for x in signals if x["day"] == d):
        if cap is not None and pnl + 1e-9 >= cap * CAP_BASE:
            hit = hit or s["wo"]
            break
        ask = s["ask"] if last is not False else cushioned(s)
        if ask is None:
            continue
        k = contracts_for_budget(LOW if pnl + 1e-9 >= TARGET * CAP_BASE else HIGH, ask)
        pnl += k * (s["won"] - ask) - kalshi_fee_charged(ask, k)
        peak = max(peak, pnl)
        last = bool(s["won"])
    return pnl, peak, hit


print(f"{len(days)} full days {days[0]}..{days[-1]}; capital {CAP_BASE} (8% target = "
      f"{TARGET * CAP_BASE:.2f}); $6 below it, $3 at/above it, cushion 5 bps.\n")
print(f"{'stop for the day at':<22}{'total':>9}{'per day':>9}{'worst':>8}{'losing':>8}{'days hit':>10}"
      f"   by day")
caps = [None, 0.08, 0.10, 0.12, 0.14, 0.16, 0.18, 0.20, 0.22, 0.25, 0.30]
res = {}
for cap in caps:
    r = [day_run(d, cap) for d in days]
    res[cap] = r
    tot = sum(x[0] for x in r)
    name = "no cap (LIVE)" if cap is None else f"{cap:.0%} (${cap * CAP_BASE:.2f})"
    print(f"{name:<22}{tot:>+9.2f}{tot / len(days):>+9.2f}{min(x[0] for x in r):>+8.2f}"
          f"{sum(1 for x in r if x[0] < 0):>8}{sum(1 for x in r if x[2]):>7}/{len(days)}   "
          + " ".join(f"{x[0]:+.1f}" for x in r))

print("\nLIVE (no cap) by day: how high the day went vs where it closed:")
for d, (p, pk, _) in zip(days, res[None]):
    print(f"  {d}  peak {pk:+7.2f} ({pk / CAP_BASE:+.1%})  closed {p:+7.2f}  gave back {pk - p:6.2f}")
