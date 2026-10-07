"""Martingale and other stake progressions, on the RECORDED signals and outcomes.

Operator, 2026-09-29: "what about testing a martingale after a loss, and on a win
reduce the size?" Every BTC alert (strategy_alerts), its ask at the alert poll,
Kalshi's result (predictions.won); contracts = floor(stake / ask), the system's
own fee, held to the result. Daily 8% target on $104.77 (as live), and never a
stake above the account's balance at the time.
"""
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from btc15_signal.validation import contracts_for_budget, kalshi_fee_charged  # noqa: E402

NY = ZoneInfo("America/New_York")
START = int(datetime(2026, 9, 22, 23, 0, tzinfo=NY).timestamp() * 1000)
BASE, CAPITAL, RATE = 5.0, 104.77, 0.08
con = sqlite3.connect(f"file:{ROOT / 'btc15.db'}?mode=ro", uri=True)
sig = [(datetime.fromtimestamp(wo / 1000, NY).strftime("%m-%d"), float(ask), int(won))
       for wo, ask, won in con.execute("""
        SELECT a.window_open, o.our_ask, p.won FROM strategy_alerts a
        JOIN observations o ON o.window_open = a.window_open AND o.observed_ms = a.created_at
        JOIN predictions p ON p.window_open = a.window_open AND p.side = o.side
        WHERE a.strategy = 'primary' AND a.window_open >= ? AND p.won IS NOT NULL
          AND o.our_ask > 0 AND o.our_ask < 1 ORDER BY a.window_open""", (START,))]
days = sorted({d for d, _, _ in sig})


def trade(stake, ask, won):
    n = contracts_for_budget(stake, ask)
    return n * (won - ask) - kalshi_fee_charged(ask, n), n * ask


def flat(stake, won, day_loss, ask):
    return BASE


def mart2(stake, won, day_loss, ask):          # double after a loss, reset on a win
    return BASE if won else min(stake * 2, 40.0)


def mart2_ease(stake, won, day_loss, ask):     # double after a loss, halve after a win
    return max(BASE, stake / 2) if won else min(stake * 2, 40.0)


def steps(stake, won, day_loss, ask):          # +$5 after a loss, -$5 after a win
    return max(BASE, stake - 5) if won else stake + 5


def recover(stake, won, day_loss, ask):        # win back the day's losses in one trade
    if day_loss <= 0:
        return BASE
    # at price p a stake s wins about s * (1 - p) / p; pay back the loss plus a base win
    p = ask
    return BASE + day_loss * p / (1 - p)


RULES = [("flat $5 (live)", flat), ("martingale x2, reset on win (cap $40)", mart2),
         ("martingale x2, halve on win (cap $40)", mart2_ease),
         ("+$5 after loss / -$5 after win", steps),
         ("full recovery (win back the day's loss)", recover)]

print(f"{len(sig)} recorded BTC signals, {len(days)} NY days, 8% target (${RATE * CAPITAL:.2f}), "
      f"base ${BASE:g}, account ${CAPITAL}\n")
print(f"{'rule':<42}{'total':>9}{'biggest bet':>12}{'worst trade':>12}{'worst day':>10}"
      f"{'deepest drop':>13}  " + " ".join(f"{d:>7}" for d in days))
for name, rule in RULES:
    balance, peak, deepest = CAPITAL, CAPITAL, 0.0
    biggest, worst_trade, per_day = 0.0, 0.0, {}
    for d in days:
        day_pnl, stake, day_loss, stopped = 0.0, BASE, 0.0, False
        for sd, ask, won in sig:
            if sd != d or stopped:
                continue
            stake = min(stake, max(balance, 0.0))
            if stake < ask:                     # cannot afford one contract
                break
            p, cost = trade(stake, ask, won)
            biggest, worst_trade = max(biggest, cost), min(worst_trade, p)
            balance += p
            day_pnl += p
            day_loss = max(0.0, day_loss - p) if p > 0 else day_loss - p
            peak = max(peak, balance)
            deepest = min(deepest, balance - peak)
            if day_pnl >= RATE * CAPITAL - 1e-9:
                stopped = True
            stake = rule(stake, won, day_loss, ask)
        per_day[d] = day_pnl
    print(f"{name:<42}{balance - CAPITAL:>+9.2f}{biggest:>12.2f}{worst_trade:>+12.2f}"
          f"{min(per_day.values()):>+10.2f}{deepest:>+13.2f}  "
          + " ".join(f"{per_day[d]:>+7.2f}" for d in days))
