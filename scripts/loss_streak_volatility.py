"""WERE THE 3-5 LOSSES IN A ROW HIGH-VOLATILITY DAYS? (operator, 2026-10-03: "check if
those were happening because of high volatility when we started noticing three to five
losses in a row").

The ACTUAL live $ trades (allsignal_trades, filled, graded), in order; a losing streak =
consecutive losses among taken trades within the New York day. For every trade, at its
alert (Kalshi only): the system's own volatility_5m_bps (observations), BRTI realised
volatility over the previous 15 min (std of ~12 s log returns, bps), and the 60-min BRTI
move (trend). A BRTI value counts at t only if ts_ms <= t and received_ms <= t.

    python scripts/loss_streak_volatility.py
"""
import bisect
import math
import sqlite3
from collections import defaultdict
from datetime import datetime
from zoneinfo import ZoneInfo

NY = ZoneInfo("America/New_York")
db = sqlite3.connect("file:D:/Kalshi/btc15-signal/btc15.db?mode=ro", uri=True)
ref = sqlite3.connect("file:D:/Kalshi/btc15-signal/runtime/settlement_reference.db?mode=ro", uri=True)

trades = db.execute("""
    SELECT t.window_open, t.side, t.won, COALESCE(t.fill_price, t.limit_price), t.stake,
           a.created_at, o.volatility_5m_bps
    FROM allsignal_trades t
    LEFT JOIN strategy_alerts a ON a.window_open = t.window_open AND a.strategy = 'primary'
    LEFT JOIN observations o ON o.window_open = a.window_open AND o.observed_ms = a.created_at
    WHERE t.status = 'filled' AND t.won IS NOT NULL ORDER BY t.window_open""").fetchall()

pts = ref.execute("SELECT MAX(ts_ms, received_ms), brti_value FROM brti_features "
                  "WHERE stale = 0 AND brti_value > 0 ORDER BY 1").fetchall()
T = [p[0] for p in pts]
V = [p[1] for p in pts]


def window(t0, t1):
    i, j = bisect.bisect_right(T, t0), bisect.bisect_right(T, t1)
    return V[i:j], T[i:j]


def rv15(t):
    vals, ts = window(t - 900_000, t)
    if len(vals) < 20:
        return None
    r = [math.log(b / a) * 1e4 for a, b in zip(vals, vals[1:])]
    m = sum(r) / len(r)
    return math.sqrt(sum((x - m) ** 2 for x in r) / (len(r) - 1))


def move60(t):
    i = bisect.bisect_right(T, t) - 1
    j = bisect.bisect_right(T, t - 3_600_000) - 1
    if i < 0 or j < 0 or t - T[i] > 60_000 or (t - 3_600_000) - T[j] > 60_000:
        return None
    return (V[i] / V[j] - 1) * 1e4


rows = []
for wo, side, won, px, stake, at, vol5 in trades:
    at = at or wo + 240_000
    rows.append(dict(wo=wo, day=datetime.fromtimestamp(wo / 1000, NY).strftime("%m-%d"),
                     side=side, won=int(won), px=float(px), vol5=vol5, rv=rv15(at), mv=move60(at)))

# losing streaks within each day
by_day = defaultdict(list)
for r in rows:
    by_day[r["day"]].append(r)
streaks = []
for d, rs in by_day.items():
    run = []
    for r in rs + [None]:
        if r is not None and not r["won"]:
            run.append(r)
            continue
        if len(run) >= 3:
            streaks.append(run)
            for x in run:
                x["streak"] = len(run)
        run = []


def pct(vals, v):
    s = sorted(x for x in vals if x is not None)
    return 100 * bisect.bisect_left(s, v) / len(s) if s and v is not None else None


all_rv = [r["rv"] for r in rows]
all_v5 = [r["vol5"] for r in rows]
all_mv = [abs(r["mv"]) for r in rows if r["mv"] is not None]


def avg(xs):
    xs = [x for x in xs if x is not None]
    return sum(xs) / len(xs) if xs else float("nan")


print(f"{len(rows)} live $ trades {rows[0]['day']}..{rows[-1]['day']}; {sum(1 for r in rows if not r['won'])} losses; "
      f"{len(streaks)} losing streaks of 3+ ({sum(len(s) for s in streaks)} losses)\n")
print(f"{'group':<26}{'n':>5}{'15m volatility':>16}{'5m vol (sys)':>14}{'|60m move|':>12}")
groups = [("losses in a 3+ streak", [r for r in rows if r.get("streak")]),
          ("other losses", [r for r in rows if not r["won"] and not r.get("streak")]),
          ("wins", [r for r in rows if r["won"]]),
          ("all trades", rows)]
for name, g in groups:
    print(f"{name:<26}{len(g):>5}{avg([r['rv'] for r in g]):>14.2f}bp{avg([r['vol5'] for r in g]):>12.2f}bp"
          f"{avg([abs(r['mv']) for r in g if r['mv'] is not None]):>10.1f}bp")

print("\nEach 3+ losing streak (percentile of volatility among ALL trades; 50 = typical):")
for s in streaks:
    first, last = s[0], s[-1]
    t0 = datetime.fromtimestamp(first["wo"] / 1000, NY).strftime("%m-%d %H:%M")
    t1 = datetime.fromtimestamp(last["wo"] / 1000, NY).strftime("%H:%M")
    rvp = avg([pct(all_rv, r["rv"]) for r in s]); v5p = avg([pct(all_v5, r["vol5"]) for r in s])
    mvp = avg([pct(all_mv, abs(r["mv"])) for r in s if r["mv"] is not None])
    sides = "".join(r["side"][0] for r in s)
    trend = avg([r["mv"] for r in s])
    print(f"  {t0}-{t1}  {len(s)} losses ({sides})  15m vol pct {rvp:4.0f}  5m vol pct {v5p:4.0f}  "
          f"|60m move| pct {mvp:4.0f}  60m move {trend:+6.1f} bp")

# loss rate by volatility third (all trades)
print("\nLoss rate by the 15-min volatility at the alert (all live trades):")
s = sorted(r["rv"] for r in rows if r["rv"] is not None)
cut = [s[len(s) // 3], s[2 * len(s) // 3]]
for name, lo, hi in (("low", -1, cut[0]), ("middle", cut[0], cut[1]), ("high", cut[1], 1e9)):
    g = [r for r in rows if r["rv"] is not None and lo <= r["rv"] < hi]
    rep = [b for a, b in zip(rows, rows[1:]) if a["day"] == b["day"] and not a["won"] and b["rv"] is not None and lo <= b["rv"] < hi]
    print(f"  {name:<7} ({lo if lo > 0 else 0:.2f}-{hi if hi < 1e8 else max(s):.2f} bp)  n {len(g):>3}  lost {sum(1 - r['won'] for r in g) / len(g):5.1%}  "
          f"priced {1 - sum(r['px'] for r in g) / len(g):5.1%}  after a loss lost {sum(1 - r['won'] for r in rep)}/{len(rep)}")

print("\nBy day: volatility vs losing streaks")
print(f"  {'day':<6}{'trades':>7}{'lost':>6}{'3+ streaks':>11}{'longest':>8}{'15m vol (avg)':>15}{'day range':>11}")
for d in sorted(by_day):
    rs = by_day[d]
    vals, _ = window(int(datetime.strptime(f"2026-{d}", "%Y-%m-%d").replace(tzinfo=NY).timestamp() * 1000),
                     int(datetime.strptime(f"2026-{d}", "%Y-%m-%d").replace(tzinfo=NY).timestamp() * 1000) + 86_400_000)
    rng = (max(vals) / min(vals) - 1) * 1e4 if vals else float("nan")
    run = best = 0
    for r in rs:
        run = 0 if r["won"] else run + 1
        best = max(best, run)
    print(f"  {d:<6}{len(rs):>7}{sum(1 - r['won'] for r in rs):>6}{sum(1 for x in streaks if x[0]['day'] == d):>11}{best:>8}"
          f"{avg([r['rv'] for r in rs]):>13.2f}bp{rng:>9.0f}bp")
