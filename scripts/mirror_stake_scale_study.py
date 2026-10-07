"""A SAFE STAKE SCALE for the mirrors, set from the balance at midnight (operator,
2026-10-01: "auto scale for the mirrored account as the account balance changes
every day at midnight, but a nice safe scale. Only my primary is controlled
manually on aggressive").

Direct arithmetic on the RECORDED signals and outcomes, exactly as
scripts/target_rules_live.py: the primary's live rule (follow, $6 / $3 at 8%,
5 bps after-loss cushion, held to the result, the system's own fee) picks the
entries; a mirror copies those entries in order at its own stake and pauses at
its own target = min(15% x opening, 7 wins at its stake), as live.

The stake for a day = the opening x FRACTION, in whole dollars, never below the
FLOOR and never above the CEILING; recomputed at each day's opening (compounding).

    python scripts/mirror_stake_scale_study.py
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
NEED, MIN_LEFT_S = 5.0, 120
P_HIGH, P_LOW, P_RATE, P_CAP = 6.0, 3.0, 0.08, 107.33
M_RATE, M_WINS, WIN_PRICE = 0.15, 7, 0.75

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
days = sorted({s["day"] for s in signals})


def pnl_of(stake, ask, won):
    n = contracts_for_budget(stake, ask)
    return n * (won - ask) - kalshi_fee_charged(ask, n)


def cushioned_entry(s):
    for ms, rside, ya, na, dist, rem in polls[s["wo"]]:
        if ms < s["at"]:
            continue
        if rem is not None and rem < MIN_LEFT_S:
            return None
        ask = ya if s["side"] == "UP" else na
        cushion = (dist or 0.0) if rside == s["side"] else -(dist or 0.0)
        if ask and 0 < ask < 1 and cushion >= NEED:
            return float(ask)
    return None


def primary_entries():
    """The live primary rule: follow + cushion. Returns {day: [(ask, won)]}."""
    out = {}
    for d in days:
        day, last_won, taken = 0.0, None, []
        for s in (x for x in signals if x["day"] == d):
            ask = s["ask"]
            if last_won is False:
                ask = cushioned_entry(s)
                if ask is None:
                    continue
            stake = P_LOW if day + 1e-9 >= P_RATE * P_CAP else P_HIGH
            day += pnl_of(stake, ask, s["won"])
            last_won = bool(s["won"])
            taken.append((ask, s["won"]))
        out[d] = taken
    return out


ENTRIES = primary_entries()


def win_value(stake):
    return contracts_for_budget(stake, WIN_PRICE) * (1 - WIN_PRICE)


def stake_for(opening, fraction, floor, ceiling):
    if fraction is None:
        return floor
    return max(floor, min(ceiling, float(int(opening * fraction))))


def run(b0, fraction=None, floor=2.0, ceiling=6.0):
    """A mirror from b0, compounding day to day. Returns the path and its worst points."""
    bal, peak, worst_dd, worst_day, lowest = b0, b0, 0.0, 0.0, b0
    stakes, hits = [], 0
    for d in days:
        opening = bal
        stake = stake_for(opening, fraction, floor, ceiling)
        target = min(M_RATE * opening, M_WINS * win_value(stake))
        stakes.append(stake)
        pnl = 0.0
        for ask, won in ENTRIES[d]:
            if pnl + 1e-9 >= target:
                break
            pnl += pnl_of(stake, ask, won)
            bal = opening + pnl
            lowest = min(lowest, bal)
            peak = max(peak, bal)
            worst_dd = max(worst_dd, (peak - bal) / peak)
        hits += pnl + 1e-9 >= target
        worst_day = min(worst_day, pnl / opening)
    return dict(final=bal, total=bal / b0 - 1, worst_day=worst_day, worst_dd=worst_dd,
                lowest=lowest, stakes=stakes, hits=hits)


# ---- the record in STAKES: what one stake's worth of risk has looked like ----
flat = [(a, w) for d in days for a, w in ENTRIES[d]]
wins = sum(w for _, w in flat)
streak = run_ = 0
for _, w in flat:
    run_ = 0 if w else run_ + 1
    streak = max(streak, run_)
print(f"{len(signals)} recorded BTC signals {days[0]}..{days[-1]} ({len(days)} NY days); the live "
      f"primary rule took {len(flat)}: {wins} won ({wins / len(flat):.1%}), mean ask "
      f"{sum(a for a, _ in flat) / len(flat):.3f}; longest losing run {streak}.")

# The worst stretch for a mirror at ONE fixed stake, measured in stakes: the
# deepest fall from a high on the running balance, the pause rule applied.
for stake in (2.0, 3.0):
    bal = peak = dd = 0.0
    for d in days:
        pnl = 0.0
        target = M_WINS * win_value(stake)        # the cap binds once the account is big
        for ask, won in ENTRIES[d]:
            if pnl + 1e-9 >= target:
                break
            p = pnl_of(stake, ask, won)
            pnl += p
            bal += p
            peak = max(peak, bal)
            dd = max(dd, peak - bal)
    print(f"  at ${stake:g}: deepest fall from a high {dd:.2f} = {dd / stake:.1f} stakes; "
          f"net {bal:+.2f} = {bal / stake:+.1f} stakes over {len(days)} days")
print()

print(f"{'start':>6} {'rule':<34} {'final':>8} {'total':>7} {'worst day':>9} {'max fall':>8} "
      f"{'lowest':>7} {'hit':>4}  stakes by day")
for b0 in (8.10, 23.16, 30.40, 100.0, 250.0):
    variants = [("fixed $2", None, 2.0, 6.0), ("fixed $3", None, 3.0, 6.0)]
    variants += [(f"{f:.0%} of opening, floor $2", f, 2.0, 6.0) for f in (0.02, 0.03, 0.04, 0.05)]
    variants += [(f"{f:.0%} of opening, floor $1", f, 1.0, 6.0) for f in (0.03, 0.05)]
    for name, f, floor, ceiling in variants:
        r = run(b0, f, floor, ceiling)
        print(f"{b0:>6.2f} {name:<34} {r['final']:>8.2f} {r['total']:>+7.1%} {r['worst_day']:>+9.1%} "
              f"{-r['worst_dd']:>+8.1%} {r['lowest']:>7.2f} {r['hits']:>4}  "
              + " ".join(f"{s:g}" for s in r["stakes"]))
    print()
