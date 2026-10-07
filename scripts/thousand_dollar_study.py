"""$1,000 ON THE PRIMARY, traded by this system (operator, 2026-10-01: "If I funded
$1,000 on the primary account and traded with this system, what would the daily profit
look like? And the risk, and the recommended trade size and risk on that thousand?").

Direct arithmetic on the RECORDED BTC signals and outcomes (as target_rules_live.py):
the live rule - 5 bps cushion after a loss, the 8% daily target with the follow rule
(the stake halves while the day stands at or above 8% of the opening, back to the base
below it) - with the stake set each midnight as a FRACTION of the opening (whole
dollars), compounding day to day. Held to the result (the cash-out has cost ~4c per
cash-out so far - FINDINGS 132 - not modelled), filled at the recorded price (depth is
checked separately).

    python scripts/thousand_dollar_study.py [start_balance]
"""
import math
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime
from zoneinfo import ZoneInfo

sys.path.insert(0, r"D:\Kalshi\btc15-signal\src")
from btc15_signal.validation import contracts_for_budget, kalshi_fee_charged  # noqa: E402

NY = ZoneInfo("America/New_York")
START = int(datetime(2026, 9, 22, 23, 0, tzinfo=NY).timestamp() * 1000)
NEED, MIN_LEFT_S, RATE = 5.0, 120, 0.08
B0 = float(sys.argv[1]) if len(sys.argv) > 1 else 1000.0

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
bids = defaultdict(list)       # (observed_ms, YES bid, NO bid = 1 - YES ask, s left)
for wo, ms, rside, ya, na, dist, rem, yb in con.execute(
        "SELECT window_open, observed_ms, side, yes_ask, no_ask, distance_bps, remaining_s, yes_bid "
        "FROM observations WHERE window_open >= ? ORDER BY window_open, observed_ms", (START,)):
    polls[wo].append((ms, rside, ya, na, dist, rem))
    bids[wo].append((ms, yb, (1 - ya) if ya else None, rem))
days = sorted({s["day"] for s in signals})
FULL = days[1:-1]          # 09-22 is its last hour, the last day is still running


def cushioned(s):
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


def cash_out_bid(s, paid):
    """The LIVE cash-out (main.allsignal_cash_out, settings in force 2026-10-01): at a
    recorded poll after the alert with >= 60 s left, the side's bid less 1c
    (exit_slippage) must be >= 0.90 and above the price paid, and either >= 0.98
    (cash_out_at_bid) or >= 90% of the way from the price paid to $1
    (cash_out_capture). Sold at the quoted bid (it fills at the best bid). None: held."""
    for ms, yb, nb, rem in bids[s["wo"]]:
        if ms <= s["at"]:
            continue
        if rem is not None and rem < 60:
            return None
        q = yb if s["side"] == "UP" else nb
        if q is None or q <= 0:
            continue
        disc = q - 0.01
        if disc < 0.90 or disc <= paid:
            continue
        if disc >= 0.98 or (disc - paid) >= 0.9 * (1 - paid):
            return float(q)
    return None


def run(fraction, target=True, b0=B0, cashout=False):
    """One account from b0. Returns per-day rows and the worst points."""
    bal = peak = b0
    worst_fall, lowest, out = 0.0, b0, []
    for d in days:
        opening = bal
        base = max(1.0, float(int(opening * fraction)))
        low = max(1.0, float(int(base / 2)))
        day, last_won, n, low_n, day_low = 0.0, None, 0, 0, 0.0
        for s in (x for x in signals if x["day"] == d):
            ask = s["ask"] if last_won is not False else cushioned(s)
            if ask is None:
                continue
            stake = low if (target and day + 1e-9 >= RATE * opening) else base
            low_n += stake == low and target and day + 1e-9 >= RATE * opening
            k = contracts_for_budget(stake, ask)
            sold = cash_out_bid(s, ask) if cashout else None
            if sold is None:
                day += k * (s["won"] - ask) - kalshi_fee_charged(ask, k)
            else:
                day += k * (sold - ask) - kalshi_fee_charged(ask, k) - kalshi_fee_charged(sold, k)
            last_won, n = bool(s["won"]), n + 1
            bal = opening + day
            day_low = min(day_low, day)
            peak = max(peak, bal)
            lowest = min(lowest, bal)
            worst_fall = max(worst_fall, (peak - bal) / peak)
        out.append(dict(day=d, opening=opening, base=base, low=low, pnl=day, n=n, low_n=low_n,
                        pct=day / opening, day_low=day_low))
    return out, worst_fall, lowest


def summary(name, fraction, target=True, cashout=False):
    out, fall, lowest = run(fraction, target, cashout=cashout)
    full = [r for r in out if r["day"] in FULL]
    pnls = sorted(r["pnl"] for r in full)
    pcts = [r["pct"] for r in full]
    final = out[-1]["opening"] + out[-1]["pnl"]
    m = sum(pcts) / len(pcts)
    sd = math.sqrt(sum((x - m) ** 2 for x in pcts) / (len(pcts) - 1))
    print(f"{name:<30}{out[1]['base']:>5.0f}{final:>10.2f}{final / B0 - 1:>+8.1%}"
          f"{m:>+8.2%}{pnls[len(pnls) // 2] / B0:>+8.2%}{min(pcts):>+8.2%}{max(pcts):>+8.2%}"
          f"{sum(1 for p in pcts if p > 0):>4}/{len(pcts)}{-fall:>+8.1%}{min(r['day_low'] for r in out) / B0:>+8.1%}"
          f"{sd:>7.2%}")
    return out


print(f"${B0:,.0f} on the primary: {len(signals)} recorded BTC signals {days[0]}..{days[-1]} "
      f"({len(FULL)} full NY days {FULL[0]}..{FULL[-1]}; {days[0]} and {days[-1]} partial).")
print("Live rule: 5 bps cushion after a loss; 8% daily target, stake halves while at/above "
      "it; stake = % of each midnight opening.\nHeld to the result, at the recorded price, "
      "net of the fee. Daily figures over the full days.\n")
print(f"{'stake rule':<30}{'$1st':>5}{'final':>10}{'total':>8}{'mean/d':>8}{'median':>8}"
      f"{'worst d':>8}{'best d':>8}{'pos':>6}{'max fall':>8}{'deepest':>8}{'sd/d':>7}")
detail = {}
for f in (0.01, 0.02, 0.03, 0.04, 0.056):
    detail[f] = summary(f"{f:.1%} of the opening", f)
for f in (0.02, 0.03):
    summary(f"{f:.0%} WITH the live cash-out", f, cashout=True)
summary("3.0%, NO daily target", 0.03, target=False)
summary("5.6%, NO daily target", 0.056, target=False)

for f in (0.02, 0.03):
    print(f"\nDay by day at {f:.0%} (stake = {f:.0%} of the opening, halved at/above the 8% target):")
    print(f"  {'day':<6}{'opening':>10}{'stake':>8}{'trades':>7}{'at half':>8}{'P&L':>10}{'%':>8}"
          f"{'lowest in day':>14}")
    for r in detail[f]:
        tag = "" if r["day"] in FULL else "  (partial)"
        print(f"  {r['day']:<6}{r['opening']:>10.2f}{r['base']:>5.0f}/{r['low']:<2.0f}{r['n']:>7}"
              f"{r['low_n']:>8}{r['pnl']:>+10.2f}{r['pct']:>+8.2%}{r['day_low']:>+14.2f}{tag}")
