"""Does a 45-minute lookback beat the 15-minute one? Measured, not assumed.

THE DEFECT THAT PROMPTED IT (2026-09-25). `brti_rejections` counts times price
came inside 40% of the current gap and recovered. Its lookback is 900s - the
market's OWN window - and the strike IS that window's opening price, so every
window begins with price on the strike, inside the threshold. A clean one-way
move therefore scores exactly 1: its own departure. Reaching 2 requires the
move to have wobbled back.

The deployed gate demands `>= 2`, so it systematically refuses the cleanest
setups. On 19,305 corpus points the refused bucket is the best one on every
measure:

    rejections   n       distance  held_s  |mom|   win%
    1 (refused)  9041      8.33      241    6.3   75.6%
    2-3          9205      4.58      212    3.2   66.9%
    4+           1026      2.73      138    1.7   61.3%

corr(rejections, distance) = -0.319. The metric measures the opposite of what
its name implies.

THE OPERATOR'S INSTRUCTION is that any indicator should read 45 minutes of
price action rather than starting at the window open. The data supports it
structurally: every market's feed already carries 2,700s BEFORE its window
opens, and the current cut discards all of it. In one sampled market the strike
level had been touched for 103 seconds within 5bp before the window even began
- genuine level-test history that the 900s window cannot see.

WHAT THIS SCRIPT DOES. For a sample of settled markets it recomputes rejections
BOTH ways from the same raw feed and asks which one separates winners. It
changes nothing: the feature contract, the live rule and every threshold are
untouched until there is a measurement to justify moving them.

Both versions are also scored against the OUTCOME rather than against each
other, because "different" is not "better".
"""

import argparse
import random
import sqlite3
import statistics
from collections import defaultdict
from datetime import datetime, timezone

import httpx

BASE = "https://external-api.kalshi.com/trade-api/v2"
WINDOW_S = 900


def rejections(points, target, side_up, ts_ms, lookback_s):
    """The deployed algorithm, with the lookback as a parameter."""
    sign = 1.0 if side_up else -1.0
    pts = [(t, v) for t, v in points if t >= ts_ms - lookback_s * 1000
           and t <= ts_ms]
    if len(pts) < 60:
        return None
    edge = [(t, (v - target) * sign) for t, v in pts]
    gap = edge[-1][1]
    if gap <= 0:
        return None
    threshold = gap * 0.4
    count, inside = 0, False
    for _t, v in edge:
        if not inside and 0 < v <= threshold:
            inside = True
        elif inside and v > threshold:
            count += 1
            inside = False
    return count


def score(sel, key, draws=4000, seed=7):
    byday = defaultdict(list)
    for r in sel:
        byday[r["day"]].append((1.0 if r["won"] else 0.0) - r["ask"])
    days = list(byday)
    if len(days) < 4 or len(sel) < 40:
        return None
    rng = random.Random(seed)
    out = []
    for _ in range(draws):
        pick = [rng.choice(days) for _ in days]
        vals = [x for d in pick for x in byday[d]]
        out.append(sum(vals) / len(vals))
    out.sort()
    flat = [x for v in byday.values() for x in v]
    return (statistics.mean(flat), out[100], out[3899], len(sel),
            sum(1 for r in sel if r["won"]) / len(sel))


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--market", default="data/market_data.db")
    p.add_argument("--sample", type=int, default=400)
    p.add_argument("--remaining", type=int, default=600)
    args = p.parse_args()

    m = sqlite3.connect(f"file:{args.market}?mode=ro", uri=True)
    m.row_factory = sqlite3.Row
    book = {}
    for t, ts, bid, ask in m.execute(
            "SELECT ticker, end_period_ts, yes_bid_close, yes_ask_close "
            "FROM contract_candles"):
        if bid is None or ask is None:
            continue
        book[(t, ts * 1000 if ts < 1e11 else ts)] = (bid, ask)
    markets = [dict(r) for r in m.execute(
        "SELECT ticker, close_ms, floor_strike AS target, result FROM markets "
        " WHERE result IN ('yes','no') AND floor_strike IS NOT NULL "
        " ORDER BY close_ms DESC LIMIT ?", (args.sample,))]
    print(f"sampling {len(markets)} settled markets, decided at "
          f"{args.remaining}s remaining")

    rows = []
    with httpx.Client(timeout=30) as c:
        for i, mk in enumerate(markets, 1):
            event = mk["ticker"].rsplit("-", 1)[0]
            try:
                r = c.get(f"{BASE}/live_data/events/{event}")
                if r.status_code != 200:
                    continue
                ts = (r.json().get("live_data") or {}).get(
                    "details", {}).get("timeseries") or []
            except Exception:  # noqa: BLE001
                continue
            if len(ts) < 1200:
                continue
            pts = [(x["t"], x["v"]) for x in ts]
            decide_ms = mk["close_ms"] - args.remaining * 1000
            at = [v for t, v in pts if t <= decide_ms]
            if not at:
                continue
            value, target = at[-1], mk["target"]
            side_up = value >= target
            quote = book.get((mk["ticker"], decide_ms // 60000 * 60000))
            if quote is None:
                continue
            yes_bid, yes_ask = quote
            yes_ask = yes_ask / 100 if yes_ask > 1 else yes_ask
            no_ask = round(1 - (yes_bid / 100 if yes_bid > 1 else yes_bid), 4)
            ask = yes_ask if side_up else no_ask
            if not 0.70 <= ask <= 0.93:
                continue
            rows.append({
                "won": (mk["result"] == "yes") == side_up,
                "ask": ask,
                "r15": rejections(pts, target, side_up, decide_ms, 900),
                "r45": rejections(pts, target, side_up, decide_ms, 2700),
                "day": datetime.fromtimestamp(
                    mk["close_ms"] / 1000, timezone.utc).date(),
            })
            if i % 100 == 0:
                print(f"  {i}/{len(markets)}  usable {len(rows)}", flush=True)

    rows = [r for r in rows if r["r15"] is not None and r["r45"] is not None]
    print(f"\nusable decision points: {len(rows)} over "
          f"{len({r['day'] for r in rows})} days")
    if len(rows) < 100:
        print("too few to compare")
        return

    print(f"\n  median rejections   15-min lookback "
          f"{statistics.median(r['r15'] for r in rows):.0f}   "
          f"45-min {statistics.median(r['r45'] for r in rows):.0f}")

    for key, label in (("r15", "15-MINUTE lookback (deployed)"),
                       ("r45", "45-MINUTE lookback (proposed)")):
        print(f"\n  {label}")
        print(f"    {'bucket':<16}{'n':>6}{'win%':>8}{'residual':>10}"
              f"{'95% CI':>22}")
        for lo, hi, name in ((0, 1, "0"), (1, 2, "1"), (2, 4, "2-3"),
                             (4, 99, "4+")):
            s = score([r for r in rows if lo <= r[key] < hi], key)
            if s is None:
                print(f"    {name:<16}too few")
                continue
            mu, l, h, n, win = s
            print(f"    {name:<16}{n:>6}{win:>8.1%}{mu:>+10.4f}"
                  f"  [{l:+.4f}, {h:+.4f}]{'  HOLDS' if l > 0 else ''}")

    print("\n  THE DEPLOYED GATE, both ways: keep >= 2, refuse the rest")
    for key, label in (("r15", "15-min"), ("r45", "45-min")):
        keep = score([r for r in rows if r[key] >= 2], key)
        ref = score([r for r in rows if r[key] < 2], key)
        for s, what in ((keep, "kept"), (ref, "REFUSED")):
            if s:
                mu, l, h, n, win = s
                print(f"    {label} {what:<8}{n:>6}{win:>8.1%}{mu:>+10.4f}"
                      f"  [{l:+.4f}, {h:+.4f}]")


if __name__ == "__main__":
    main()
