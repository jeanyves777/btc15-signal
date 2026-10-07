"""Win rate and net result by ENTRY PRICE on the recorded BTC signals (operator,
2026-10-01: "in our analysis earlier we found that signal below 70 cent had low win?").

FINDINGS 98's "below 0.70" result was about COMBO PARTNERS. This measures the $
strategy itself: every recorded signal, entered as the live primary rule enters it
(5 bps cushion after a loss, at that poll's ask), held to the result, the system's own
fee. Per $1 staked so price bands compare fairly. Direct arithmetic, nothing fitted.

    python scripts/entry_price_bands.py
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
NEED, MIN_LEFT_S = 5.0, 120
BANDS = [(0.0, 0.60), (0.60, 0.65), (0.65, 0.70), (0.70, 0.75), (0.75, 0.80), (0.80, 0.85),
         (0.85, 1.0)]

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


def per_dollar(ask, won, stake=6.0):
    """Net result per $1 staked, at the live $6 stake's contract count."""
    n = contracts_for_budget(stake, ask)
    cost = n * ask
    return (n * (won - ask) - kalshi_fee_charged(ask, n)) / cost, kalshi_fee_charged(ask, n) / n


def wilson(k, n, z=1.96):
    if n == 0:
        return (0.0, 0.0)
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return c - h, c + h


# THE LIVE ENTRIES: the cushion after a loss moves some entries to a later ask.
taken, last_won, day = [], None, None
for s in signals:
    if s["day"] != day:
        day, last_won = s["day"], None
    ask = s["ask"]
    if last_won is False:
        ask = cushioned(s)
        if ask is None:
            continue
    taken.append(dict(s, entry=ask))
    last_won = bool(s["won"])

days = sorted({t["day"] for t in taken})
print(f"{len(taken)} entries the live rule took ({len(signals)} signals, {days[0]}..{days[-1]}); "
      "held to the result, net of the fee, per $1 staked at the $6 contract count.\n")
print(f"{'entry price':<12}{'n':>5}{'won':>5}{'win %':>7}{'95% range':>15}{'break-even':>11}"
      f"{'net/$1':>8}{'net at $6':>10}   days positive")
for lo, hi in BANDS:
    b = [t for t in taken if lo <= t["entry"] < hi]
    if not b:
        continue
    k = sum(t["won"] for t in b)
    lo_ci, hi_ci = wilson(k, len(b))
    res = [per_dollar(t["entry"], t["won"]) for t in b]
    be = sum(t["entry"] + f for t, (_, f) in zip(b, res)) / len(b)
    net = sum(r for r, _ in res) / len(b)
    by_day = defaultdict(float)
    for t, (r, _) in zip(b, res):
        by_day[t["day"]] += r
    pos = sum(1 for v in by_day.values() if v > 0)
    name = f"< {hi:.2f}" if lo == 0 else (f">= {lo:.2f}" if hi == 1.0 else f"{lo:.2f}-{hi - 0.01:.2f}")
    print(f"{name:<12}{len(b):>5}{k:>5}{k / len(b):>7.1%}  [{lo_ci:.0%}, {hi_ci:.0%}]"
          f"{be:>11.3f}{net:>+8.3f}{net * 6 * len(b):>+10.2f}   {pos}/{len(by_day)}")

below = [t for t in taken if t["entry"] < 0.70]
above = [t for t in taken if t["entry"] >= 0.70]
for name, b in (("below 0.70", below), ("0.70 and up", above)):
    k = sum(t["won"] for t in b)
    net = sum(per_dollar(t["entry"], t["won"])[0] for t in b) / len(b)
    print(f"\n{name}: {len(b)} entries, {k} won ({k / len(b):.1%}), net {net:+.3f} per $1 "
          f"({net * 6 * len(b):+.2f} at $6)", end="")
print("\n\nBy day, below 0.70 (n, won, net at $6):")
for d in days:
    b = [t for t in below if t["day"] == d]
    if b:
        print(f"  {d}: {len(b):>3} {sum(t['won'] for t in b):>3} "
              f"{sum(per_dollar(t['entry'], t['won'])[0] for t in b) * 6:>+7.2f}")


# ---- THE SAME TWO GROUPS WITH THEIR NOISE: is "below 0.70" actually worse? ----
def mean_se(xs):
    m = sum(xs) / len(xs)
    sd = math.sqrt(sum((x - m) ** 2 for x in xs) / (len(xs) - 1))
    return m, sd / math.sqrt(len(xs))


rb = [per_dollar(t["entry"], t["won"])[0] for t in below]
ra = [per_dollar(t["entry"], t["won"])[0] for t in above]
(mb, sb), (ma, sa) = mean_se(rb), mean_se(ra)
diff, sd_ = ma - mb, math.sqrt(sa ** 2 + sb ** 2)
print(f"\nnet per $1: below 0.70 {mb:+.3f} [{mb - 1.96 * sb:+.3f}, {mb + 1.96 * sb:+.3f}]; "
      f"0.70+ {ma:+.3f} [{ma - 1.96 * sa:+.3f}, {ma + 1.96 * sa:+.3f}]; "
      f"difference {diff:+.3f} [{diff - 1.96 * sd_:+.3f}, {diff + 1.96 * sd_:+.3f}]")


# ---- UNDER THE LIVE RULES: the primary's follow rule ($6, $3 while >= 8%), the cushion ----
def first_poll(s, floor, need):
    """The first poll from the alert where the ask is >= floor and, after a loss, the
    price is >= 5 bps clear; None if 120 s left comes first."""
    for ms, rside, ya, na, dist, rem in polls[s["wo"]]:
        if ms < s["at"]:
            continue
        if rem is not None and rem < MIN_LEFT_S:
            return None
        ask = ya if s["side"] == "UP" else na
        cushion = (dist or 0.0) if rside == s["side"] else -(dist or 0.0)
        if ask and 0 < ask < 1 and ask >= floor and cushion >= need:
            return float(ask)
    return None


def live(mode, floor=0.70, cap=107.33):
    out = {}
    for d in days:
        day, last_won, n = 0.0, None, 0
        for s in (x for x in signals if x["day"] == d):
            need = NEED if last_won is False else -1e9
            if mode == "wait":
                ask = first_poll(s, floor, need)
            else:
                ask = s["ask"] if last_won is not False else cushioned(s)
                if ask is not None and mode == "skip" and ask < floor:
                    ask = None
            if ask is None:
                continue
            stake = 3.0 if day + 1e-9 >= 0.08 * cap else 6.0
            k = contracts_for_budget(stake, ask)
            day += k * (s["won"] - ask) - kalshi_fee_charged(ask, k)
            last_won, n = bool(s["won"]), n + 1
        out[d] = (day, n)
    return out


print("\nUNDER THE LIVE RULES (primary $6 / $3 while >= 8%, 5 bps after a loss), by day:")
runs = {"as live": live("none"), "skip below 0.70": live("skip"),
        "wait for >= 0.70": live("wait")}
print(f"{'day':<7}" + "".join(f"{k:>18}" for k in runs))
for d in days:
    print(f"{d:<7}" + "".join(f"{v[d][0]:>+12.2f} ({v[d][1]:>3})" for v in runs.values()))
print(f"{'total':<7}" + "".join(f"{sum(x[0] for x in v.values()):>+12.2f} "
                                f"({sum(x[1] for x in v.values()):>3})" for v in runs.values()))
base = runs["as live"]
for k in ("skip below 0.70", "wait for >= 0.70"):
    better = sum(1 for d in days if runs[k][d][0] > base[d][0] + 1e-9)
    worse = sum(1 for d in days if runs[k][d][0] < base[d][0] - 1e-9)
    print(f"{k}: better than live on {better} days, worse on {worse}; worst day "
          f"{min(x[0] for x in runs[k].values()):+.2f} vs {min(x[0] for x in base.values()):+.2f}")
