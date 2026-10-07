"""Do the losing signals have something in common? On the RECORDED signals only.

Every BTC primary alert (strategy_alerts), the facts recorded AT the alert poll
(observations at observed_ms = created_at) and Kalshi's result (predictions.won).
Each fact is split into thirds over all signals; per third: signals, win rate, and
P&L at $5 after fees (held). A difference counts only if it points the same way in
both halves of the days AND the P&L follows it - a cheaper third can lose more
often and still pay, because the price already knows.
"""
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from btc15_signal.validation import contracts_for_budget, kalshi_fee_charged  # noqa: E402

NY = ZoneInfo("America/New_York")
START = int(datetime(2026, 9, 22, 23, 0, tzinfo=NY).timestamp() * 1000)
DB = sys.argv[1] if len(sys.argv) > 1 else "btc15.db"
con = sqlite3.connect(f"file:{ROOT / DB}?mode=ro", uri=True)
con.row_factory = sqlite3.Row
rows = con.execute("""
    SELECT o.*, p.won AS result FROM strategy_alerts a
    JOIN observations o ON o.window_open = a.window_open AND o.observed_ms = a.created_at
    JOIN predictions p ON p.window_open = a.window_open AND p.side = o.side
    WHERE a.strategy = 'primary' AND a.window_open >= ? AND p.won IS NOT NULL
      AND o.our_ask > 0 AND o.our_ask < 1 ORDER BY a.window_open""", (START,)).fetchall()


def num(v):
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


sig = []
for r in rows:
    up = 1.0 if r["side"] == "UP" else -1.0
    ask = float(r["our_ask"])
    n = contracts_for_budget(5.0, ask)
    d = dict(r)
    t = datetime.fromtimestamp(r["window_open"] / 1000, NY)
    f = {
        "price (ask)": ask,
        "distance to target, toward side (bps)": None if num(d.get("distance_bps")) is None
        else up * num(d["distance_bps"]),
        "normalized distance, toward side": None if num(d.get("normalized_distance")) is None
        else up * num(d["normalized_distance"]),
        "momentum 5m, toward side (bps)": None if num(d.get("momentum_5m_bps")) is None
        else up * num(d["momentum_5m_bps"]),
        "volatility 5m (bps)": num(d.get("volatility_5m_bps")),
        "spread (bps)": num(d.get("spread_bps")),
        "taker imbalance, toward side": None if num(d.get("taker_imbalance")) is None
        else up * num(d["taker_imbalance"]),
        "book share on side": None if num(d.get("book_yes_share")) is None
        else (num(d["book_yes_share"]) if up > 0 else 1 - num(d["book_yes_share"])),
        "time left at alert (s)": num(d.get("remaining_s")),
        "hour (ET)": float(t.hour),
    }
    sig.append({"day": t.strftime("%m-%d"), "won": int(r["result"]),
                "pnl": n * (int(r["result"]) - ask) - kalshi_fee_charged(ask, n),
                "side": r["side"], "session": d.get("session") or "?",
                "regime": d.get("vol_regime") or "?", "f": f})
days = sorted({s["day"] for s in sig})
half = set(days[: len(days) // 2])
won = sum(s["won"] for s in sig)
print(f"{len(sig)} signals, {won} won / {len(sig) - won} lost ({100 * won / len(sig):.1f}%), "
      f"P&L at $5 {sum(s['pnl'] for s in sig):+.2f}; halves: {sorted(half)} | "
      f"{sorted(set(days) - half)}\n")


def cell(group):
    if not group:
        return "   -              "
    w = sum(s["won"] for s in group)
    return f"{len(group):>4} {100 * w / len(group):5.1f}% {sum(s['pnl'] for s in group) / len(group):+.3f}"


print(f"{'fact at the alert':<40}{'third':<16}{'all: n  won  $/sig':<24}{'1st half':<24}{'2nd half'}")
for name in sig[0]["f"]:
    vals = sorted(s["f"][name] for s in sig if s["f"][name] is not None)
    if len(vals) < 60:
        print(f"{name:<40}(recorded on {len(vals)} signals - too few)")
        continue
    cuts = [vals[len(vals) // 3], vals[2 * len(vals) // 3]]
    for i, label in enumerate(("low", "mid", "high")):
        lo = -1e18 if i == 0 else cuts[i - 1]
        hi = 1e18 if i == 2 else cuts[i]
        grp = [s for s in sig if s["f"][name] is not None and lo <= s["f"][name] < hi]
        rng = f"{lo:.3g}..{hi:.3g}" if 0 < i < 2 else (f"< {hi:.3g}" if i == 0 else f">= {lo:.3g}")
        print(f"{name if i == 0 else '':<40}{label + ' ' + rng:<16}{cell(grp):<24}"
              f"{cell([s for s in grp if s['day'] in half]):<24}"
              f"{cell([s for s in grp if s['day'] not in half])}")
for key in ("side", "session", "regime"):
    for v in sorted({s[key] for s in sig}):
        grp = [s for s in sig if s[key] == v]
        print(f"{key if v == sorted({s[key] for s in sig})[0] else '':<40}{str(v):<16}"
              f"{cell(grp):<24}{cell([s for s in grp if s['day'] in half]):<24}"
              f"{cell([s for s in grp if s['day'] not in half])}")
