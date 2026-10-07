"""What happened on the worst days, and which simple protection would have helped -
on the RECORDED signals and outcomes (every BTC alert, predictions.won), sized
like live (You: $5 a signal, 8% daily target of $104.77), after fees.

Protections are fixed in advance and scored on ALL days, not fitted to the bad
ones: a daily loss stop, a pause after losses in a row, and a give-back stop.
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
STAKE, CAP, RATE = 5.0, 104.77, 0.08
con = sqlite3.connect(f"file:{ROOT / 'btc15.db'}?mode=ro", uri=True)
sig = []
for wo, ask, won in con.execute("""
        SELECT a.window_open, o.our_ask, p.won FROM strategy_alerts a
        JOIN observations o ON o.window_open = a.window_open AND o.observed_ms = a.created_at
        JOIN predictions p ON p.window_open = a.window_open AND p.side = o.side
        WHERE a.strategy = 'primary' AND a.window_open >= ? AND p.won IS NOT NULL
          AND o.our_ask > 0 AND o.our_ask < 1 ORDER BY a.window_open""", (START,)):
    t = datetime.fromtimestamp(wo / 1000, NY)
    n = contracts_for_budget(STAKE, ask)
    sig.append((t.strftime("%m-%d"), t.hour, float(ask), int(won),
                n * (int(won) - ask) - kalshi_fee_charged(ask, n)))
days = sorted({s[0] for s in sig})


def run(rule):
    """rule(state) -> True to skip this signal. The 8% target always applies."""
    by_day = {}
    for d in days:
        st = {"pnl": 0.0, "peak": 0.0, "streak": 0, "cool": 0, "stop": False}
        for sd, hour, ask, won, p in sig:
            if sd != d:
                continue
            if st["stop"] or st["pnl"] >= RATE * CAP - 1e-9:
                continue
            if st["cool"] > 0:
                st["cool"] -= 1
                continue
            if rule(st, ask):
                continue
            st["pnl"] += p
            st["peak"] = max(st["peak"], st["pnl"])
            st["streak"] = 0 if p > 0 else st["streak"] + 1
            rule.after(st) if hasattr(rule, "after") else None
        by_day[d] = st["pnl"]
    return by_day


def plain(st, ask):
    return False


def loss_stop(pct):
    def r(st, ask):
        if st["pnl"] <= -pct * CAP:
            st["stop"] = True
        return st["stop"]
    return r


def streak_pause(k, m):
    def r(st, ask):
        return False

    def after(st):
        if st["streak"] >= k:
            st["cool"], st["streak"] = m, 0
    r.after = after
    return r


def give_back(arm, keep):
    def r(st, ask):
        if st["peak"] >= arm and st["pnl"] < keep * st["peak"]:
            st["stop"] = True
        return st["stop"]
    return r


# ---- what happened on the worst days (no protection, no target)
print("WORST DAYS - every signal, no target:")
for d in days:
    rows = [s for s in sig if s[0] == d]
    total = sum(s[4] for s in rows)
    if total > -5:
        continue
    losses = [s for s in rows if s[3] == 0]
    streak = best = 0
    for s in rows:
        streak = streak + 1 if s[3] == 0 else 0
        best = max(best, streak)
    running = peak = low = 0.0
    for s in rows:
        running += s[4]
        peak, low = max(peak, running), min(low, running)
    hours = defaultdict(float)
    for s in rows:
        hours[s[1]] += s[4]
    worst_hours = sorted(hours.items(), key=lambda kv: kv[1])[:3]
    cheap = [s for s in losses if s[2] < 0.65]
    print(f"  {d}: {len(rows)} signals, {len(rows) - len(losses)}W-{len(losses)}L, "
          f"total {total:+.2f}; high point {peak:+.2f}, low {low:+.2f}; longest losing run "
          f"{best}; losses at avg {sum(s[2] for s in losses) / len(losses):.2f} "
          f"({len(cheap)} under 65c); worst hours (ET) "
          + ", ".join(f"{h:02d}:00 {v:+.2f}" for h, v in worst_hours))

# ---- protections, scored on every day
RULES = [("8% target only (live)", plain)]
RULES += [(f"+ daily loss stop at -{p:.0%}", loss_stop(p)) for p in (0.03, 0.05, 0.08, 0.10)]
RULES += [(f"+ pause 1h after {k} losses in a row", streak_pause(k, 4)) for k in (2, 3)]
RULES += [(f"+ give-back stop (peak>=${a:g}, keep {int(k * 100)}%)", give_back(a, k))
          for a, k in ((3, 0.5), (5, 0.5))]
print(f"\nPROTECTIONS on all {len(days)} days (You, $5, 8% target), after fees:")
print(f"  {'rule':<42}{'total':>8}{'worst':>8}  " + " ".join(f"{d:>6}" for d in days))
for name, rule in RULES:
    by = run(rule)
    print(f"  {name:<42}{sum(by.values()):>+8.2f}{min(by.values()):>+8.2f}  "
          + " ".join(f"{by[d]:>+6.2f}" for d in days))
