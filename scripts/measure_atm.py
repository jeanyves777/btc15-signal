"""Backtest the at-the-money strategy: price action picks the side, enter at 45-50c.

The operator's specification (2026-09-25):

  * 15-minute expiry markets
  * use PRICE ACTION on the underlying to decide the direction
  * enter only when Kalshi offers the chosen side at 45-50c - a coin flip

The question is not whether price action "works" in general but whether it
knows something the 45-50c price does not. Section 36 says no model beat the
price over the whole book; this asks the narrower question at the one place
the market says it has no opinion. The bar is high for a structural reason:
the fee 0.07*P*(1-P) PEAKS at 50c (~1.75c), so at a 47.5c ask a rule must win
~49.3% just to break even.

Method:
  * every settled market with 1-minute Kalshi candles and 1-minute underlying
    klines; decision at each candle close, features from klines that had
    CLOSED by then (no look-ahead)
  * each rule: the FIRST minute in the window where the signal names a side
    and that side's ask (YES ask, or 1 - YES bid for NO) is in [0.45, 0.50];
    one trade per market per rule
  * net of `kalshi_fee_charged` at one contract, held to settlement
  * 95% CI by bootstrap clustered by DAY (trades in a day share a path)
  * every rule is scored with its mirror (reversal) and against controls that
    take a side at 45-50c with no information; Holm across the whole family
  * replication: first vs second half of days, then ETH and SOL untouched

Caveat carried to every number: the klines are Binance spot, not BRTI. Live
is Kalshi-only, so anything that survived here would need re-measuring on BRTI
before it could trade. Fills assume the candle-close ask was takeable for one
contract; the book is not historical (see kalshi-historical-data-limits).

    python scripts/measure_atm.py [--asset btc|eth|sol] [--lo 0.45] [--hi 0.50]
"""

import argparse
import math
import random
import sqlite3
import sys
from bisect import bisect_right
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from btc15_signal.validation import kalshi_fee_charged  # noqa: E402

DBS = {"btc": "market_data.db", "eth": "market_data_kxeth15m.db", "sol": "market_data_kxsol15m.db"}
BOOT = 2000


def load(asset):
    c = sqlite3.connect(f"file:{ROOT / 'data' / DBS[asset]}?mode=ro", uri=True)
    kl = c.execute("SELECT open_time, open, high, low, close FROM klines ORDER BY open_time").fetchall()
    k_end = [r[0] + 60_000 for r in kl]          # a kline is known once it closes
    markets = c.execute(
        "SELECT ticker, open_ms, close_ms, floor_strike, result FROM markets "
        "WHERE result IN ('yes','no') AND floor_strike > 0").fetchall()
    candles = defaultdict(list)
    for t, ts, b, a in c.execute(
            "SELECT ticker, end_period_ts, yes_bid_close, yes_ask_close FROM contract_candles "
            "ORDER BY ticker, end_period_ts"):
        candles[t].append((ts * 1000, b, a))
    return kl, k_end, markets, candles


def features(kl, k_end, t_ms, open_ms, strike, mids):
    """Price action known at t_ms. Returns None where history is missing."""
    i = bisect_right(k_end, t_ms) - 1            # last closed kline
    if i < 240:
        return None
    if k_end[i] < t_ms - 120_000:                # gap in klines
        return None
    px = kl[i][4]
    ret = lambda n: (px / kl[i - n][4] - 1) * 1e4
    j = bisect_right(k_end, open_ms) - 1          # price at window open
    if j < 0 or k_end[j] < open_ms - 120_000:
        return None
    streak = 0
    sign = 1 if kl[i][4] >= kl[i][1] else -1
    for k in range(i, i - 10, -1):
        s = 1 if kl[k][4] >= kl[k][1] else -1
        if s != sign:
            break
        streak += sign
    hi15 = max(r[2] for r in kl[i - 14:i + 1]); lo15 = min(r[3] for r in kl[i - 14:i + 1])
    hi60 = max(r[2] for r in kl[i - 59:i + 1]); lo60 = min(r[3] for r in kl[i - 59:i + 1])
    ema = lambda n: _ema([r[4] for r in kl[i - 4 * n:i + 1]], n)
    f = {
        "r1": ret(1), "r3": ret(3), "r5": ret(5), "r15": ret(15),
        "r60": ret(60), "r240": ret(240),
        "window_move": (px / kl[j][4] - 1) * 1e4,
        "vs_strike": (px / strike - 1) * 1e4,
        "streak": streak,
        "range15": (px - lo15) / (hi15 - lo15) - 0.5 if hi15 > lo15 else 0.0,
        "range60": (px - lo60) / (hi60 - lo60) - 0.5 if hi60 > lo60 else 0.0,
        "ema5_20": (ema(5) / ema(20) - 1) * 1e4,
        "kalshi_mid_1m": (mids[-1] - mids[-2]) if len(mids) >= 2 else None,
        "kalshi_mid_3m": (mids[-1] - mids[-4]) if len(mids) >= 4 else None,
    }
    return f


def _ema(xs, n):
    a = 2 / (n + 1); e = xs[0]
    for x in xs[1:]:
        e = a * x + (1 - a) * e
    return e


# Each rule: feature, threshold. UP when feature > thr, DOWN when < -thr.
# "cont" follows the move, "rev" fades it - both are scored.
RULES = [
    ("r1", 0), ("r1", 3), ("r3", 0), ("r3", 5), ("r5", 0), ("r5", 8),
    ("r15", 0), ("r15", 10), ("r60", 0), ("r60", 20), ("r240", 0), ("r240", 40),
    ("window_move", 0), ("window_move", 3), ("vs_strike", 0), ("vs_strike", 2),
    ("streak", 2), ("streak", 3), ("range15", 0.3), ("range60", 0.3),
    ("ema5_20", 0), ("ema5_20", 3), ("kalshi_mid_1m", 0.03), ("kalshi_mid_3m", 0.05),
]


def build(asset, lo, hi):
    kl, k_end, markets, candles = load(asset)
    rows = []   # per market: list of (minute_elapsed, yes_ask, no_ask, features)
    for ticker, open_ms, close_ms, strike, result in markets:
        cs = [c for c in candles.get(ticker, []) if open_ms < c[0] <= close_ms - 60_000]
        if not cs:
            continue
        pts, mids = [], []
        for t_ms, b, a in cs:
            if b is None or a is None or a <= 0 or b >= 1 or a - b > 0.05:
                mids.append(mids[-1] if mids else 0.5); continue
            mids.append((a + b) / 2)
            f = features(kl, k_end, t_ms, open_ms, strike, mids)
            if f is None:
                continue
            pts.append(((t_ms - open_ms) // 60_000, a, round(1 - b, 4), f))
        if pts:
            rows.append((ticker, open_ms // 86_400_000, result == "yes", pts))
    return rows


def trades_for(rows, pick, lo, hi, minutes):
    """pick(f) -> +1 UP, -1 DOWN, 0 no signal. First qualifying minute per market."""
    out = []
    for ticker, day, up_won, pts in rows:
        for m, ya, na, f in pts:
            if m not in minutes:
                continue
            s = pick(f)
            if not s:
                continue
            ask = ya if s > 0 else na
            if lo <= ask <= hi:
                won = up_won if s > 0 else not up_won
                out.append((day, ask, won, (1.0 if won else 0.0) - ask - kalshi_fee_charged(ask, 1)))
                break
    return out


def summarise(tr, rng):
    if not tr:
        return None
    by_day = defaultdict(list)
    for d, a, w, n in tr:
        by_day[d].append(n)
    days = list(by_day)
    mean = sum(t[3] for t in tr) / len(tr)
    boots = []
    for _ in range(BOOT):
        s = c = 0
        for d in rng.choices(days, k=len(days)):
            s += sum(by_day[d]); c += len(by_day[d])
        boots.append(s / c)
    boots.sort()
    p = (sum(b <= 0 for b in boots) + 1) / (BOOT + 1)   # one-sided, H1: net > 0
    return {
        "n": len(tr), "per_day": len(tr) / len(days),
        "win": sum(t[2] for t in tr) / len(tr), "ask": sum(t[1] for t in tr) / len(tr),
        "net": mean, "lo": boots[int(0.025 * BOOT)], "hi": boots[int(0.975 * BOOT)], "p": p,
    }


def run(asset, lo, hi, minutes, label, rules=RULES, split=None):
    rows = build(asset, lo, hi)
    if split is not None:
        days = sorted({r[1] for r in rows}); mid = days[len(days) // 2]
        rows = [r for r in rows if (r[1] < mid) == (split == 0)]
    rng = random.Random(7)
    ndays = len({r[1] for r in rows})
    print(f"\n=== {asset.upper()} {label}: {len(rows)} markets, {ndays} days, "
          f"ask {lo:.2f}-{hi:.2f}, minutes elapsed {min(minutes)}-{max(minutes)}")
    results = []
    ctl = [
        ("CONTROL always UP", lambda f: 1),
        ("CONTROL always DOWN", lambda f: -1),
        ("CONTROL coin flip", lambda f, r=random.Random(11): r.choice((1, -1))),
    ]
    for name, pick in ctl:
        results.append((name, summarise(trades_for(rows, pick, lo, hi, minutes), rng)))
    for feat, thr in rules:
        for mode, sgn in (("cont", 1), ("rev", -1)):
            def pick(f, feat=feat, thr=thr, sgn=sgn):
                v = f.get(feat)
                if v is None:
                    return 0
                return sgn if v > thr else (-sgn if v < -thr else 0)
            results.append((f"{feat}>{thr} {mode}", summarise(trades_for(rows, pick, lo, hi, minutes), rng)))
    # Holm over the rule family (controls excluded)
    fam = sorted([(r["p"], k) for k, (n, r) in enumerate(results) if r and not n.startswith("CONTROL")])
    holm = {}
    m = len(fam); run_max = 0
    for rank, (p, k) in enumerate(fam):
        run_max = max(run_max, min(1.0, p * (m - rank)))
        holm[k] = run_max
    print(f"{'rule':24} {'n':>5} {'/day':>5} {'win':>6} {'ask':>6} {'net/c':>8} {'95% CI':>19} {'p':>6} {'holm':>6}")
    for k, (name, r) in enumerate(results):
        if not r:
            print(f"{name:24} none"); continue
        print(f"{name:24} {r['n']:5d} {r['per_day']:5.1f} {r['win']:6.1%} {r['ask']:6.3f} "
              f"{r['net']:+8.4f} [{r['lo']:+.4f}, {r['hi']:+.4f}] {r['p']:6.3f} "
              f"{holm.get(k, float('nan')):6.3f}")
    return results


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--asset", default="btc", choices=DBS)
    ap.add_argument("--lo", type=float, default=0.45)
    ap.add_argument("--hi", type=float, default=0.50)
    ap.add_argument("--first", type=int, default=1, help="first minute elapsed")
    ap.add_argument("--last", type=int, default=13, help="last minute elapsed")
    ap.add_argument("--split", type=int, choices=(0, 1), default=None)
    a = ap.parse_args()
    run(a.asset, a.lo, a.hi, set(range(a.first, a.last + 1)),
        "all" if a.split is None else f"half {a.split}", split=a.split)
