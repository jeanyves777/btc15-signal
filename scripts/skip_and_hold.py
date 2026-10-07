"""After-loss skip and a 2-minute side hold, on the RECORDED signals and polls.

Operator, 2026-09-29: "test skip the next signal after a loss instead of the 1 hour
pause, and the next signal must choose the side only when the side holds for 2
minutes before entry".

Signals: every BTC primary alert (strategy_alerts). Polls: observations (~10 s).
HOLD: an entry needs its side unchanged over the previous 120 s of recorded polls.
If the alert's side has not held, the entry waits inside the window for the first
poll whose side has held 120 s - that side, at that poll's recorded ask - and
never with under 120 s left. Outcome: Kalshi's result for the ticker
(settlements), for the side actually taken. Sized like live, after fees, per New
York day, with the daily target (You $5 / 8% of $104.77; mirrors $2 / 15%).
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
HOLD_MS, MIN_LEFT_S = 120_000, 120
con = sqlite3.connect(f"file:{ROOT / 'btc15.db'}?mode=ro", uri=True)
# Kalshi's result per WINDOW, from the result stamped on every signal
# (predictions: its side and whether that side won) - the account's own
# `settlements` only cover markets it traded, and would drop most signals.
result = {}
for wo, pside, pwon in con.execute(
        "SELECT window_open, side, won FROM predictions WHERE window_open >= ? "
        "AND won IS NOT NULL AND side IN ('UP','DOWN')", (START,)):
    result[wo] = "yes" if (pside == "UP") == bool(pwon) else "no"
polls = defaultdict(list)
for wo, ms, side, ask, rem, ticker in con.execute(
        "SELECT window_open, observed_ms, side, our_ask, remaining_s, ticker FROM observations "
        "WHERE window_open >= ? AND side IN ('UP','DOWN') ORDER BY window_open, observed_ms",
        (START,)):
    polls[wo].append((ms, side, ask, rem, ticker))
alerts = con.execute("SELECT window_open, created_at FROM strategy_alerts "
                     "WHERE strategy='primary' AND window_open >= ? ORDER BY window_open",
                     (START,)).fetchall()


def won(window, side):
    r = result.get(window)
    return None if r is None else int((r == "yes") == (side == "UP"))


def held_since(rows, i):
    """ms the side at rows[i] has been unchanged, over consecutive recorded polls."""
    j = i
    while j > 0 and rows[j - 1][1] == rows[i][1]:
        j -= 1
    return rows[i][0] - rows[j][0]


SIGNALS = []   # (day, at_alert_entry, hold_entry) - entry = (ask, won) or None
dropped = 0
for wo, created in alerts:
    rows = polls.get(wo, [])
    idx = next((i for i, r in enumerate(rows) if r[0] == created), None)
    if idx is None:
        dropped += 1
        continue
    ms, side, ask, rem, ticker = rows[idx]
    w = won(wo, side)
    if w is None or not ask or not 0 < ask < 1:
        dropped += 1
        continue
    plain = (ask, w)
    hold = None
    for i in range(idx, len(rows)):
        if rows[i][3] is not None and rows[i][3] < MIN_LEFT_S:
            break
        if held_since(rows, i) >= HOLD_MS and rows[i][2] and 0 < rows[i][2] < 1:
            hw = won(wo, rows[i][1])
            if hw is not None:
                hold = (rows[i][2], hw, i != idx, rows[i][1] != side)
            break
    SIGNALS.append((datetime.fromtimestamp(wo / 1000, NY).strftime("%m-%d"), plain, hold))
days = sorted({s[0] for s in SIGNALS})


def pnl(stake, entry):
    ask, w = entry[0], entry[1]
    n = contracts_for_budget(stake, ask)
    return n * (w - ask) - kalshi_fee_charged(ask, n)


def run(stake, target, skip_after_loss=False, hold="none", pause_after2=False):
    """hold: 'none' | 'all' (every entry) | 'after' (the first entry after a skip)."""
    by_day, trades, wins = {}, 0, 0
    for d in days:
        total, skip, need_hold, streak, cool = 0.0, 0, False, 0, 0
        for sd, plain, hold_e in SIGNALS:
            if sd != d or total >= target - 1e-9:
                continue
            if cool:
                cool -= 1
                continue
            if skip:
                skip -= 1
                need_hold = hold == "after"
                continue
            use_hold = hold == "all" or (hold == "after" and need_hold)
            entry = hold_e if use_hold else plain
            need_hold = False
            if entry is None:          # the side never held 2 minutes: no trade
                continue
            p = pnl(stake, entry)
            total += p
            trades += 1
            wins += p > 0
            if p <= 0:
                streak += 1
                if skip_after_loss:
                    skip = 1
                if pause_after2 and streak >= 2:
                    cool, streak = 4, 0
            else:
                streak = 0
        by_day[d] = total
    return by_day, trades, wins


RULES = [
    ("today: every signal", {}),
    ("skip next signal after a loss", {"skip_after_loss": True}),
    ("2-min side hold on every entry", {"hold": "all"}),
    ("skip next after loss, then 2-min hold", {"skip_after_loss": True, "hold": "after"}),
    ("skip next after loss + hold on all", {"skip_after_loss": True, "hold": "all"}),
    ("(ref) 1 h pause after 2 losses", {"pause_after2": True}),
]
held_at_alert = sum(1 for s in SIGNALS if s[2] and not s[2][2])
moved = sum(1 for s in SIGNALS if s[2] and s[2][2])
flipped = sum(1 for s in SIGNALS if s[2] and s[2][3])
never = sum(1 for s in SIGNALS if s[2] is None)
print(f"{len(SIGNALS)} signals ({dropped} dropped: no alert poll or unsettled), {len(days)} NY days")
print(f"2-min hold: already held at the alert {held_at_alert}; waited {moved} "
      f"(of which the side flipped {flipped}); never held with 2 min left {never}\n")
for label, stake, cap, rate in (("You $5, 8%", 5.0, 104.77, 0.08),
                                ("Wife $2, 15%", 2.0, 25.39, 0.15),
                                ("Uncle George $2, 15%", 2.0, 19.03, 0.15)):
    print(f"=== {label} (target ${rate * cap:.2f})")
    print(f"  {'rule':<40}{'total':>8}{'trades':>7}{'won':>7}{'worst':>8}  "
          + " ".join(f"{d:>6}" for d in days))
    for name, kw in RULES:
        by, t, w = run(stake, rate * cap, **kw)
        print(f"  {name:<40}{sum(by.values()):>+8.2f}{t:>7}{100 * w / max(t, 1):>6.1f}%"
              f"{min(by.values()):>+8.2f}  " + " ".join(f"{by[d]:>+6.2f}" for d in days))
    print()
