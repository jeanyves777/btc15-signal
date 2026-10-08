"""Trend-aware candidates vs the live rule, on the recorded BTC primary signals (Kalshi only).

Pre-registered (A-E), all tested and all reported; nothing added or tuned.
Trend = Kalshi BRTI (runtime/settlement_reference.db brti_features.brti_value), a value is
usable at time t only if ts_ms <= t AND received_ms <= t, and only if ts_ms >= t - 60 s.
Read-only databases. Binance columns are never selected.
"""
import bisect
import math
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime
from zoneinfo import ZoneInfo

sys.path.insert(0, r"D:\Kalshi\btc15-signal\src")
from btc15_signal.validation import contracts_for_budget, kalshi_fee_charged  # noqa: E402

NY = ZoneInfo("America/New_York")
START = 1790135100000
HIGH, LOW, TARGET, CAP = 6.0, 3.0, 0.08, 0.20
C_PREV, C_TODAY = 107.33, 119.67
TODAY = "10-02"
PREV_DAYS = ["09-23", "09-24", "09-25", "09-26", "09-27", "09-28", "09-29", "09-30", "10-01"]
FRESH = 60_000
EPS = 1e-9

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
           if ask is not None and 0 < ask < 1 and won is not None and (not ps or ps == side)]
polls = defaultdict(list)
for wo, ms, rside, ya, na, dist, rem in con.execute(
        "SELECT window_open, observed_ms, side, yes_ask, no_ask, distance_bps, remaining_s "
        "FROM observations WHERE window_open >= ? ORDER BY window_open, observed_ms", (START,)):
    polls[wo].append((ms, rside, ya, na, dist, rem))
con.close()

# ---- BRTI series (Kalshi) ----
ref = sqlite3.connect("file:D:/Kalshi/btc15-signal/runtime/settlement_reference.db?mode=ro", uri=True)
brti = ref.execute("SELECT ts_ms, received_ms, brti_value FROM brti_features "
                   "WHERE stale = 0 AND ts_ms >= ? ORDER BY ts_ms, received_ms",
                   (START - 26 * 3600_000,)).fetchall()
ref.close()
B_TS = [r[0] for r in brti]


def value_at(t):
    """Latest BRTI with ts_ms <= t and received_ms <= t, if within 60 s of t; else None."""
    i = bisect.bisect_right(B_TS, t) - 1
    while i >= 0 and brti[i][0] >= t - FRESH:
        if brti[i][1] <= t:
            return brti[i][2]
        i -= 1
    return None


def move_bps(t_now, t_then):
    a, b = value_at(t_now), value_at(t_then)
    if a is None or b is None:
        return None
    return (a / b - 1.0) * 1e4


def against(side, tr, T):
    if tr is None:
        return False
    return (side == "UP" and tr < -T) or (side == "DOWN" and tr > T)


for s in signals:
    X = s["at"]
    s["tr"] = {H: move_bps(X, X - H * 60_000) for H in (15, 30, 60)}
    d = datetime.fromtimestamp(s["wo"] / 1000, NY)
    midnight = int(d.replace(hour=0, minute=0, second=0, microsecond=0).timestamp() * 1000)
    s["dtr"] = move_bps(X, midnight)
    s["settle"] = s["wo"] + 900_000


def first_poll(s, need):
    """Ask at the first poll from the alert whose cushion on the signal's side >= need bps;
    None if a poll with < 120 s left comes first."""
    for ms, rside, ya, na, dist, rem in polls[s["wo"]]:
        if ms < s["at"]:
            continue
        if rem is not None and rem < 120:
            return None
        ask = ya if s["side"] == "UP" else na
        cushion = (dist or 0.0) if rside == s["side"] else -(dist or 0.0)
        if ask and 0 < ask < 1 and cushion >= need:
            return float(ask)
    return None


def fee1(p):
    return kalshi_fee_charged(p, 1)


# ---- rules ----
# A rule: (name, kind, param) ; kind in live/skip/half/confirm/cooldown
def flag_fn(H, T):
    return lambda s: against(s["side"], s["tr"][H], T)


def day_flag(T):
    return lambda s: against(s["side"], s["dtr"], T)


RULES = [("LIVE", "live", None, None)]
for H in (15, 30, 60):
    for T in (10, 20, 40):
        RULES.append((f"A skip H{H} T{T}", "skip", flag_fn(H, T), None))
RULES.append(("B half H60 T20", "half", flag_fn(60, 20), None))
for Xb in (10, 15, 20):
    RULES.append((f"C confirm H60 T20 X{Xb}", "confirm", flag_fn(60, 20), Xb))
for T in (50, 100):
    RULES.append((f"D day skip T{T}", "skip", day_flag(T), None))
RULES.append(("E side cooldown", "cooldown", None, None))


def cooldown_blocked(side, X, losses):
    """losses: list of (side, settle_ms) of TAKEN entries that lost, same day.
    A trigger at a loss settlement t when >= 2 same-side losses settled in [t-60m, t];
    the side is blocked for t <= X < t + 30m."""
    st = sorted(t for sd, t in losses if sd == side and t <= X)
    for j, t in enumerate(st):
        if X >= t + 30 * 60_000:
            continue
        n = sum(1 for u in st[:j + 1] if u >= t - 60 * 60_000)
        if n >= 2:
            return True
    return False


def run_day(sigs, C, kind, flag, param):
    pnl, last = 0.0, None
    losses = []
    dec = {}
    for s in sigs:
        if pnl + EPS >= CAP * C:
            dec[s["wo"]] = None
            continue
        f = flag(s) if flag else False
        if kind == "skip" and f:
            dec[s["wo"]] = None
            continue
        if kind == "cooldown" and cooldown_blocked(s["side"], s["at"], losses):
            dec[s["wo"]] = None
            continue
        need = 5.0 if last is False else None
        if kind == "confirm" and f:
            need = max(need or 0.0, float(param))
        ask = s["ask"] if need is None else first_poll(s, need)
        if ask is None:
            dec[s["wo"]] = None
            continue
        stake = LOW if pnl + EPS >= TARGET * C else HIGH
        if kind == "half" and f:
            stake = max(1.0, math.floor(stake / 2))
        n = contracts_for_budget(stake, ask)
        net = n * (s["won"] - ask) - kalshi_fee_charged(ask, n)
        pnl += net
        dec[s["wo"]] = (ask, n, net)
        last = bool(s["won"])
        if not s["won"]:
            losses.append((s["side"], s["settle"]))
    return pnl, dec


def run(rule, days, C):
    name, kind, flag, param = rule
    out = {}
    for d in days:
        sigs = [s for s in signals if s["day"] == d]
        out[d] = run_day(sigs, C, kind, flag, param)
    return out


def grp(items):
    """items: list of (won, price). -> n, win%, mean price, net/contract (c) and its SE (c)."""
    if not items:
        return "n=0"
    n = len(items)
    w = sum(x[0] for x in items) / n
    p = sum(x[1] for x in items) / n
    nets = [x[0] - x[1] - fee1(x[1]) for x in items]
    m = sum(nets) / n
    sd = math.sqrt(sum((v - m) ** 2 for v in nets) / (n - 1)) if n > 1 else float("nan")
    se = f"{sd / math.sqrt(n) * 100:.1f}c" if n > 1 else "n/a"
    return f"n={n} won {w:.0%} vs priced {p:.0%}, net/contract {m * 100:+.1f}c (SE {se})"


def msd(items):
    """mean net/contract and its standard error."""
    nets = [x[0] - x[1] - fee1(x[1]) for x in items]
    n = len(nets)
    if n == 0:
        return 0.0, 0.0
    m = sum(nets) / n
    sd = math.sqrt(sum((v - m) ** 2 for v in nets) / (n - 1)) if n > 1 else 0.0
    return m, sd / math.sqrt(n)


SIG = {s["wo"]: s for s in signals}
lines = []
P = lines.append

P("TREND FIX STUDY - BTC primary, Kalshi-only signals (window_open >= 1790135100000)")
P(f"Signals: PREVIOUS {sum(1 for s in signals if s['day'] in PREV_DAYS)} on 9 days "
  f"{PREV_DAYS[0]}..{PREV_DAYS[-1]} (C={C_PREV}); TODAY {sum(1 for s in signals if s['day'] == TODAY)} "
  f"settled on {TODAY} (C={C_TODAY}), last window "
  f"{datetime.fromtimestamp(max(s['wo'] for s in signals if s['day'] == TODAY) / 1000, NY):%H:%M} ET.")
unk = {H: sum(1 for s in signals if s["day"] in PREV_DAYS + [TODAY] and s["tr"][H] is None) for H in (15, 30, 60)}
unkd = sum(1 for s in signals if s["day"] in PREV_DAYS + [TODAY] and s["dtr"] is None)
P(f"Trend unknown (BRTI gap, treated as no-trend): H15 {unk[15]}, H30 {unk[30]}, H60 {unk[60]}, day {unkd} signals.")
P("Live rule replay: $6 below 8% of C, $3 at/above; stop at 20%; 5 bps cushion after a taken loss.")
P("Net per contract = won - price - fee(1 contract). SE = 1 standard error.")
for lab, days in (("PREVIOUS", PREV_DAYS), ("TODAY", [TODAY])):
    for sd_ in ("UP", "DOWN", None):
        it = [(s["won"], s["ask"]) for s in signals if s["day"] in days and (sd_ is None or s["side"] == sd_)]
        P(f"All signals {lab:<8} {sd_ or 'both':<5} {grp(it)}")
P("Changed signals: 'removed' = live took it, rule did not (at live price); 'added' = rule took it, "
  "live did not (at rule price); 'resized' = both took it, different price or size.")
P("")

results = {}
for rule in RULES:
    results[rule[0]] = (run(rule, PREV_DAYS, C_PREV), run(rule, [TODAY], C_TODAY))

live_prev, live_today = results["LIVE"]


def section(rule, res, live, days, label):
    name = rule[0]
    tot = sum(res[d][0] for d in days)
    ltot = sum(live[d][0] for d in days)
    taken = sum(1 for d in days for v in res[d][1].values() if v)
    per = " ".join(f"{d[3:] if len(days) > 1 else d}:{res[d][0]:+.2f}" for d in days)
    head = f"  {label:<8} total {tot:+8.2f}  vs live {tot - ltot:+7.2f}  entries {taken:>3}  per day {per}"
    out = [head]
    if name == "LIVE":
        return out, tot - ltot
    removed, added, resized = [], [], []
    dollars = {"removed": 0.0, "added": 0.0, "resized": 0.0}
    for d in days:
        for wo, lv in live[d][1].items():
            rv = res[d][1].get(wo)
            s = SIG[wo]
            if lv and not rv:
                removed.append((s["won"], lv[0]))
                dollars["removed"] -= lv[2]
            elif rv and not lv:
                added.append((s["won"], rv[0]))
                dollars["added"] += rv[2]
            elif lv and rv and (abs(lv[0] - rv[0]) > 1e-9 or lv[1] != rv[1]):
                resized.append((s["won"], lv[0], rv[0], lv[1], rv[1]))
                dollars["resized"] += rv[2] - lv[2]
    out.append(f"           removed {grp(removed)}; $ effect {dollars['removed']:+.2f}")
    if added:
        out.append(f"           added   {grp(added)}; $ effect {dollars['added']:+.2f}")
    if resized:
        n = len(resized)
        w = sum(x[0] for x in resized) / n
        lp = sum(x[1] for x in resized) / n
        rp = sum(x[2] for x in resized) / n
        lc = sum(x[3] for x in resized)
        rc = sum(x[4] for x in resized)
        ln = sum(x[0] - x[1] - fee1(x[1]) for x in resized) / n
        rn = sum(x[0] - x[2] - fee1(x[2]) for x in resized) / n
        out.append(f"           resized n={n} won {w:.0%}; live price {lp:.0%} net/contract {ln * 100:+.1f}c "
                   f"-> rule price {rp:.0%} net/contract {rn * 100:+.1f}c; contracts {lc}->{rc}; "
                   f"$ effect {dollars['resized']:+.2f}")
    return out, tot - ltot


summary = []
for rule in RULES:
    name, kind, flag, _ = rule
    rp, rt = results[name]
    P(f"== {name}")
    if flag is not None:
        for lab, days in (("PREVIOUS", PREV_DAYS), ("TODAY", [TODAY])):
            f_ = [(s["won"], s["ask"]) for s in signals if s["day"] in days and flag(s)]
            r_ = [(s["won"], s["ask"]) for s in signals if s["day"] in days and not flag(s)]
            mf, sf = msd(f_)
            mr, sr = msd(r_)
            P(f"  flagged (every signal, alert price) {lab:<8} {grp(f_)}")
            P(f"      rest {len(r_)} net/contract {mr * 100:+.1f}c; flagged minus rest "
              f"{(mf - mr) * 100:+.1f}c (SE {math.sqrt(sf ** 2 + sr ** 2) * 100:.1f}c)")
    o, dprev = section(rule, rp, live_prev, PREV_DAYS, "PREVIOUS")
    lines.extend(o)
    o, dtoday = section(rule, rt, live_today, [TODAY], "TODAY")
    lines.extend(o)
    if name != "LIVE":
        diffs = [rp[d][0] - live_prev[d][0] for d in PREV_DAYS]
        b = sum(1 for x in diffs if x > 0.005)
        w = sum(1 for x in diffs if x < -0.005)
        sm = len(diffs) - b - w
        P(f"  previous days vs live: better {b}, worse {w}, same {sm}   "
          + " ".join(f"{x:+.2f}" for x in diffs))
        summary.append((name, dprev, dtoday, b, w, sm))
    P("")

P("== SUMMARY (change vs live; previous 9 days | today)")
for name, dp, dt, b, w, sm in summary:
    P(f"  {name:<24} prev {dp:+7.2f} ({b}b/{w}w/{sm}s)   today {dt:+7.2f}")
P("")
good = [x for x in summary if x[2] > 0.005 and x[1] >= -0.005]
P("Improve TODAY and do not lose on PREVIOUS:")
if good:
    for name, dp, dt, b, w, sm in good:
        P(f"  {name}: today {dt:+.2f}, previous {dp:+.2f} ({b} better / {w} worse / {sm} same days)")
else:
    P("  none")
P("Today-only fixes (improve TODAY, lose on PREVIOUS) and their cost on PREVIOUS:")
bad = [x for x in summary if x[2] > 0.005 and x[1] < -0.005]
for name, dp, dt, b, w, sm in sorted(bad, key=lambda x: x[1]):
    P(f"  {name}: today {dt:+.2f}, previous {dp:+.2f} ({b} better / {w} worse / {sm} same days)")
if not bad:
    P("  none")
P("Do not improve TODAY:")
for name, dp, dt, b, w, sm in summary:
    if dt <= 0.005:
        P(f"  {name}: today {dt:+.2f}, previous {dp:+.2f}")

text = "\n".join(lines)
with open(r"C:\Users\admin\AppData\Local\Temp\claude\d--Kalshi\e370d92b-680f-40d2-b442-e1dbf63079e6"
          r"\scratchpad\trendfix\results.txt", "w", encoding="utf-8") as fh:
    fh.write(text + "\n")
print(text)
