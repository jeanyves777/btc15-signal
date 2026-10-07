"""How the live after-loss cushion (5 bps, live since 2026-09-30 00:30) has done -
on the LIVE book and the RECORDED lifecycle only (operator, 2026-09-30: "now
check how it has been doing and how the 5bp help out").

1. LIVE: every $ trade whose previous filled trade that day lost. What the cushion
   did (entered at the alert / waited, then entered / skipped), and what entering
   at the alert would have done instead: the alert's own ask (observations at the
   alert poll), the same stake, the same recorded result. Held to the result on
   both sides, so cash-outs do not muddy the comparison; the live net figure is
   shown beside it.
2. REPLAY, the whole day: every BTC signal as if traded at the live stake, with
   and without the cushion (as scripts/target_rules_live.py), so the hours the
   account sat out (paused at its target) show what the cushion would have done.

    python scripts/cushion_live_review.py [YYYY-MM-DD]      # default: today (NY)
"""
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime
from zoneinfo import ZoneInfo

sys.path.insert(0, r"D:\Kalshi\btc15-signal\src")
from btc15_signal.validation import contracts_for_budget, kalshi_fee_charged  # noqa: E402

NY = ZoneInfo("America/New_York")
ROOT = r"D:\Kalshi\btc15-signal"
DAY = sys.argv[1] if len(sys.argv) > 1 else datetime.now(NY).strftime("%Y-%m-%d")
LIVE_FROM = int(datetime(2026, 9, 30, 0, 30, 24, tzinfo=NY).timestamp() * 1000)  # the deploy
NEED, MIN_LEFT_S = 5.0, 120
d0 = int(datetime.fromisoformat(DAY).replace(tzinfo=NY).timestamp() * 1000)
d1 = d0 + 86_400_000
con = sqlite3.connect(rf"file:{ROOT}\btc15.db?mode=ro", uri=True)
con.row_factory = sqlite3.Row


def hm(ms):
    return datetime.fromtimestamp(ms / 1000, NY).strftime("%H:%M")


def pnl(stake_or_n, ask, won, count=None):
    n = count if count is not None else contracts_for_budget(stake_or_n, ask)
    return n * (won - ask) - kalshi_fee_charged(ask, n)


def alert(wo):
    return con.execute("""
        SELECT a.created_at, o.side, o.our_ask, p.won, p.side AS pside
        FROM strategy_alerts a
        JOIN observations o ON o.window_open = a.window_open AND o.observed_ms = a.created_at
        LEFT JOIN predictions p ON p.window_open = a.window_open
        WHERE a.strategy = 'primary' AND a.window_open = ?""", (wo,)).fetchone()


# ------------------------------------------------------------------ 1. LIVE
book = con.execute("SELECT * FROM allsignal_trades WHERE window_open >= ? AND window_open < ? "
                   "ORDER BY window_open", (d0, d1)).fetchall()
print(f"=== {DAY}: the live $ book (primary)")
last_won, rows = None, []
for r in book:
    after_loss = last_won is False and r["created_ms"] >= LIVE_FROM   # placed under the live cushion
    if r["status"] == "filled":
        a = alert(r["window_open"])
        won = a["won"] if a and a["won"] is not None and (not a["pside"] or a["pside"] == r["side"]) \
            else r["won"]
        if after_loss and won is not None:
            stake = r["stake"] or 5.0      # rows before the 09-30 12:46 change carry none: $5 then
            waited = (r["created_ms"] - a["created_at"]) / 1000 if a else 0.0
            took = pnl(None, float(r["fill_price"] or r["ask"]), int(won), count=float(r["filled"]))
            at_alert = pnl(stake, float(a["our_ask"]), int(won))
            live_net = (float(r["pnl"]) - float(r["fee"] or 0) - float(r["exit_fee"] or 0)
                        if r["pnl"] is not None else None)
            rows.append((hm(r["window_open"]), r["side"], waited, float(a["our_ask"]),
                         float(r["fill_price"] or r["ask"]), float(r["filled"]), int(won), took,
                         at_alert, live_net, stake))
        if won is not None:
            last_won = bool(won)
    elif r["status"] == "skipped" and after_loss:
        a = alert(r["window_open"])
        stake = r["stake"] or 5.0
        if a and a["won"] is not None:
            rows.append((hm(r["window_open"]), r["side"], None, float(a["our_ask"]), None, 0.0,
                         int(a["won"]), 0.0, pnl(stake, float(a["our_ask"]), int(a["won"])), 0.0, stake))

print(f"{'window':<7}{'side':<5}{'cushion did':<26}{'stake':>6}{'alert':>7}{'entry':>7}{'n':>3}"
      f"{'result':>7}{'cushion':>9}{'at alert':>9}{'gain':>7}{'live net':>9}")
tot_c = tot_a = 0.0
for t, side, waited, a_ask, e_ask, n, won, took, at_alert, live_net, stake in rows:
    did = ("SKIPPED (no cushion by 2 min)" if waited is None
           else "entered at the alert" if waited < 5 else f"waited {waited:.0f}s, entered")
    tot_c += took
    tot_a += at_alert
    print(f"{t:<7}{side:<5}{did:<26}{stake:>6g}{a_ask * 100:>6.0f}c"
          f"{(f'{e_ask * 100:.0f}c' if e_ask else '-'):>7}{n:>3g}{('WON' if won else 'LOST'):>7}"
          f"{took:>+9.2f}{at_alert:>+9.2f}{took - at_alert:>+7.2f}"
          f"{(f'{live_net:+.2f}' if live_net is not None else '-'):>9}")
print(f"{'total':<67}{tot_c:>+9.2f}{tot_a:>+9.2f}{tot_c - tot_a:>+7.2f}")
print("(cushion and at-alert both held to the result; 'live net' = the book's realised "
      "figure after fees, cash-outs included)")

# --------------------------------------------------------------- 2. REPLAY
sig = con.execute("""
    SELECT a.window_open, a.created_at, o.side, o.our_ask, p.won, p.side AS pside
    FROM strategy_alerts a
    JOIN observations o ON o.window_open = a.window_open AND o.observed_ms = a.created_at
    LEFT JOIN predictions p ON p.window_open = a.window_open
    WHERE a.strategy = 'primary' AND a.window_open >= ? AND a.window_open < ?
    ORDER BY a.window_open""", (d0, d1)).fetchall()
sig = [s for s in sig if s["our_ask"] and 0 < s["our_ask"] < 1 and s["won"] is not None
       and (not s["pside"] or s["pside"] == s["side"])]


def cushioned(s):
    for o in con.execute("SELECT observed_ms, side, yes_ask, no_ask, distance_bps, remaining_s "
                         "FROM observations WHERE window_open = ? AND observed_ms >= ? "
                         "ORDER BY observed_ms", (s["window_open"], s["created_at"])):
        if o["remaining_s"] is not None and o["remaining_s"] < MIN_LEFT_S:
            return None
        ask = o["yes_ask"] if s["side"] == "UP" else o["no_ask"]
        cush = (o["distance_bps"] or 0.0) if o["side"] == s["side"] else -(o["distance_bps"] or 0.0)
        if ask and 0 < ask < 1 and cush >= NEED:
            return float(ask), (o["observed_ms"] - s["created_at"]) / 1000
    return None


def replay(stake, cushion):
    total, last_won, events = 0.0, None, []
    for s in sig:
        ask = float(s["our_ask"])
        if cushion and last_won is False:
            got = cushioned(s)
            if got is None:
                events.append((hm(s["window_open"]), "skip", int(s["won"]),
                               pnl(stake, ask, int(s["won"]))))
                continue
            ask, waited = got
            events.append((hm(s["window_open"]), "wait" if waited else "alert", int(s["won"]),
                           pnl(stake, ask, int(s["won"])) - pnl(stake, float(s["our_ask"]), int(s["won"]))))
        total += pnl(stake, ask, int(s["won"]))
        last_won = bool(s["won"])
    return total, events


print(f"\n=== {DAY}: every signal as if traded (replay on the recorded lifecycle, held "
      f"to the result), {len(sig)} settled signals")
for stake in (6.0, 3.0):
    off, _ = replay(stake, False)
    on, ev = replay(stake, True)
    skips = [e for e in ev if e[1] == "skip"]
    waits = [e for e in ev if e[1] == "wait"]
    print(f"  ${stake:g} a signal: without the cushion {off:+.2f}, with it {on:+.2f} "
          f"({on - off:+.2f}); after a loss {len(ev)}: at the alert "
          f"{sum(1 for e in ev if e[1] == 'alert')}, waited {len(waits)} "
          f"({sum(e[3] for e in waits):+.2f} vs entering at once), skipped {len(skips)} "
          f"- {sum(1 for e in skips if not e[2])} of them losers, avoiding "
          f"{-sum(e[3] for e in skips if not e[2]):.2f}, and {sum(1 for e in skips if e[2])} "
          f"winners, missing {sum(e[3] for e in skips if e[2]):.2f}")
_, ev = replay(6.0, True)
print("  the skips: " + ", ".join(f"{t} {'won' if w else 'LOST'}" for t, k, w, _ in ev if k == "skip"))
