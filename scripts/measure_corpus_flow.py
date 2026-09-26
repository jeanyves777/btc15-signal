"""Does balanced taker flow hold on 68 days the live sample never saw?

WHAT IS BEING TESTED. On the bot's own 173 executed trades, flow that was
neither leaning for nor against us separated hard: |imbalance| < 0.10 gave
n=51, residual +0.082, a day-clustered interval excluding zero, and - unlike
the gates of section 66 - a MONOTONIC boundary sweep, +0.115 at 0.05 falling
to +0.013 at 0.20. Refusing everything else would have turned +3.80 realised
into +6.90 on 29% of the volume.

WHY THAT IS NOT ENOUGH. Five days, n=51, and the effect is absent in the first
two days and concentrated in the last three - which are exactly the days the
strategy changed underneath it. Sections 63, 64 and 66 all looked this good on
five days and all reversed. Section 67 was worse: it looked this good and was
an artifact of the fetch itself.

THIS SAMPLE is 612 corpus markets stratified across 68 days, 2026-07-15 to
09-20, nearly disjoint from the live trading days. Entry prices come from the
one-minute candle book, which is an APPROXIMATION - Kalshi sells no historical
depth, so queue position and true fill probability are not knowable here. That
is acceptable for a residual, because the candle ask IS the implied
probability being tested against; it would not be acceptable for any claim
about trades we did not take.

Every window carries its fetched bounds. A window not fully covered is
dropped, and a covered window with no trades is counted as genuinely quiet
rather than silently folded into a bucket.

No fees, per the operator's standing instruction.
"""
import json
import random
import sqlite3
import statistics
from collections import defaultdict
from datetime import datetime, timezone

ROOT = r"D:\Kalshi\btc15-signal"

tr = sqlite3.connect(f"file:{ROOT}/data/corpus_flow.db?mode=ro", uri=True)
tr.row_factory = sqlite3.Row
coverage = {r["ticker"]: (r["min_ms"], r["max_ms"], r["complete"])
            for r in tr.execute("SELECT ticker, min_ms, max_ms, complete "
                                "FROM trades_coverage")}
flow = defaultdict(list)
for r in tr.execute("SELECT ticker, created_ms, count, taker_side FROM trades "
                    " WHERE count IS NOT NULL AND created_ms IS NOT NULL"):
    flow[r["ticker"]].append((r["created_ms"], r["count"], r["taker_side"]))
for v in flow.values():
    v.sort()

targets = {t[0]: t for t in json.load(
    open(f"{ROOT}/data/corpus_targets.json"))}

rows, quiet, uncovered = [], 0, 0
for ticker, (lo_ms, hi_ms, ok) in coverage.items():
    t = targets.get(ticker)
    if t is None:
        continue
    _tk, decision_ms, ask, won, side, close_ms = t
    lo = decision_ms - 120_000
    if not ok or lo < lo_ms:
        uncovered += 1
        continue
    ours = "yes" if side == "UP" else "no"
    seg = [x for x in (flow.get(ticker) or []) if lo <= x[0] <= decision_ms]
    for_us = sum(c for _t, c, s in seg if s == ours)
    against = sum(c for _t, c, s in seg if s != ours)
    total = for_us + against
    if total == 0:
        quiet += 1
        continue
    rows.append({"ticker": ticker, "won": bool(won), "ask": ask,
                 "imb": (for_us - against) / total, "vol": total,
                 "day": datetime.fromtimestamp(
                     close_ms / 1000, timezone.utc).date()})

print(f"corpus markets with a covered 2-minute window: {len(rows)}")
print(f"  dropped as uncovered: {uncovered}   genuinely quiet: {quiet}")
days = sorted({r["day"] for r in rows})
print(f"  spanning {len(days)} days: {days[0]} .. {days[-1]}")


def score(sel, label):
    if len(sel) < 25:
        print(f"  {label:<24} n={len(sel):>4}  too few")
        return None
    byday = defaultdict(list)
    for r in sel:
        byday[r["day"]].append((1.0 if r["won"] else 0.0) - r["ask"])
    keys = list(byday)
    random.seed(13)
    draws = []
    for _ in range(8000):
        pick = [random.choice(keys) for _ in keys]
        vals = [x for k in pick for x in byday[k]]
        draws.append(sum(vals) / len(vals))
    draws.sort()
    mean = statistics.mean([x for v in byday.values() for x in v])
    w = sum(1 for r in sel if r["won"]) / len(sel)
    flag = "HOLDS" if draws[200] > 0 else ""
    print(f"  {label:<24}{len(sel):>6}{len(keys):>6}{w:>8.1%}{mean:>+10.4f}"
          f"  [{draws[200]:+.4f}, {draws[7799]:+.4f}] {flag}")
    return mean, draws[200]


print("\nBASELINE - everything the deployed gates admit")
print(f"  {'bucket':<24}{'n':>6}{'days':>6}{'win%':>8}{'residual':>10}"
      f"{'95% CI':>24}")
score(rows, "all")

print("\nTHE LIVE CLAIM, retested: balanced vs the rest")
score([r for r in rows if abs(r["imb"]) < 0.10], "balanced |imb| < 0.10")
score([r for r in rows if abs(r["imb"]) >= 0.10], "the rest (REFUSED)")

print("\nTHE BOUNDARY SWEEP - live was monotonic, +0.115 down to +0.013")
for w in (0.05, 0.08, 0.10, 0.12, 0.15, 0.20):
    score([r for r in rows if abs(r["imb"]) < w], f"|imb| < {w:.2f}")

print("\nSIGNED BUCKETS - is there a direction, or only a magnitude?")
for lo, hi, lab in ((-1.01, -0.5, "against us <-0.5"),
                    (-0.5, -0.1, "-0.5 to -0.1"),
                    (-0.1, 0.1, "balanced"),
                    (0.1, 0.5, "+0.1 to +0.5"),
                    (0.5, 1.01, "with us >+0.5")):
    score([r for r in rows if lo <= r["imb"] < hi], lab)

print("\nAnd the sanity check the live sample could not run:")
print(f"  mean imbalance {statistics.mean(r['imb'] for r in rows):+.3f}   "
      f"mean ask {statistics.mean(r['ask'] for r in rows):.3f}")
try:
    corr = statistics.correlation([abs(r["imb"]) for r in rows],
                                  [r["ask"] for r in rows])
    print(f"  corr(|imb|, ask) = {corr:+.3f}")
except statistics.StatisticsError:
    pass
