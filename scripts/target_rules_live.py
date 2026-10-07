"""Daily-target rules WITH the live after-loss cushion - on the RECORDED signals,
their RECORDED poll-by-poll lifecycle and their RECORDED outcomes only
(operator, 2026-09-30: "did you put in play the new implementation 5 bps
required after a loss").

The cushion exactly as main.allsignal_on_alert / allsignal_cushion_poll run it:
after the last FILLED $ trade of the New York day LOST (skips and pauses do not
count; midnight resets it), the next signal enters only once the price is >= 5
bps clear of the target line on its side - at the alert if it already is, else
at the first recorded poll that is, at that poll's ask - and is skipped if that
has not happened with 120 s left. Cushion = observations.distance_bps (unsigned,
on the price's side) signed to the signal's side, as scripts/wait_after_loss_day.py.

Rules, per New York day (fixed capital as scripts/target_study.py):
  no target       every signal at $5
  pause (live)    $5 until 8% of 104.77, then nothing until midnight
  lower after     $5 until 8%, then $2 until midnight
Mirrors copy the primary's FILLS, as live: a mirror trades a signal only if the
primary took it, at its own $2, until its own 15% (pause) or at $1 after it.
Held to the result (no cash-out), the system's own fee.

    python scripts/target_rules_live.py [cushion_bps] [base] [lower] [capital]
    default 5 5 2 104.77; the live primary since 2026-09-30: 5 6 3 115.39
"""
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime
from zoneinfo import ZoneInfo

sys.path.insert(0, r"D:\Kalshi\btc15-signal\src")
from btc15_signal.validation import contracts_for_budget, kalshi_fee_charged  # noqa: E402

NY = ZoneInfo("America/New_York")
START = int(datetime(2026, 9, 22, 23, 0, tzinfo=NY).timestamp() * 1000)
NEED = float(sys.argv[1]) if len(sys.argv) > 1 else 5.0
MIN_LEFT_S = 120
# "lower_stop": after the first target at the lower stake, the day closes once
# the lower-stake phase itself has made SECOND_RATE of the capital (8% + 8% = 16%).
SECOND_RATE = 0.08
# "follow_stop": the follow rule plus a DAILY LOSS STOP at STOP_RATE of the capital
# (env STOP_RATE, e.g. 0.10 = stop for the day once down 10%).
STOP_RATE = float(__import__("os").environ.get("STOP_RATE", "0.10"))
# [cushion] [primary base] [primary lower] [primary capital]
_p = [float(x) for x in sys.argv[2:5]]
PRIMARY = ("You", *(_p + [5.0, 2.0, 104.77][len(_p):]), )
PRIMARY = (PRIMARY[0], PRIMARY[1], PRIMARY[2], 0.08, PRIMARY[3])
MIRRORS = [("Wife", 2.0, 1.0, 0.15, 25.39), ("Uncle George", 2.0, 1.0, 0.15, 19.03)]

# STUDY_DB=eth15.db (etc.) runs the same rules on another instrument's record.
import os  # noqa: E402

STUDY_DB = os.environ.get("STUDY_DB", "btc15.db")
con = sqlite3.connect(f"file:D:/Kalshi/btc15-signal/{STUDY_DB}?mode=ro", uri=True)
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
           if ask and 0 < ask < 1 and won is not None and (not ps or ps == side)]
polls = defaultdict(list)
for wo, ms, rside, ya, na, dist, rem in con.execute(
        "SELECT window_open, observed_ms, side, yes_ask, no_ask, distance_bps, remaining_s "
        "FROM observations WHERE window_open >= ? ORDER BY window_open, observed_ms", (START,)):
    polls[wo].append((ms, rside, ya, na, dist, rem))
days = sorted({s["day"] for s in signals})


def pnl_of(stake, ask, won):
    n = contracts_for_budget(stake, ask)
    return n * (won - ask) - kalshi_fee_charged(ask, n)


def cushioned_entry(s):
    """(ask, seconds waited) at the first poll from the alert >= NEED bps clear
    on the signal's side, or None if 120 s left came first."""
    for ms, rside, ya, na, dist, rem in polls[s["wo"]]:
        if ms < s["at"]:
            continue
        if rem is not None and rem < MIN_LEFT_S:
            return None
        ask = ya if s["side"] == "UP" else na
        cushion = (dist or 0.0) if rside == s["side"] else -(dist or 0.0)
        if ask and 0 < ask < 1 and cushion >= NEED:
            return float(ask), (ms - s["at"]) / 1000
    return None


def run(rule, cushion):
    """Per day: primary P&L and the list of entries (wo, ask, won) it took."""
    _, high, low, rate, cap = PRIMARY
    out = {}
    stats = defaultdict(int)
    for d in days:
        day, reached, last_won, entries, low_pt = 0.0, False, None, [], 0.0
        phase2, closed = 0.0, False
        hit_once, armed = False, False         # the follow-rule safeguards
        for s in (x for x in signals if x["day"] == d):
            if (reached and rule == "pause") or closed:
                continue
            ask = s["ask"]
            if cushion and NEED > 0 and last_won is False:
                stats["after a loss"] += 1
                got = cushioned_entry(s)
                if got is None:
                    stats["skipped (no cushion by 2 min left)"] += 1
                    continue
                ask, waited = got
                stats["entered at the alert" if waited == 0 else "entered after waiting"] += 1
            if rule in ("follow", "guard_below", "guard_loss", "follow_stop"):
                # THE STAKE FOLLOWS THE DAY: the lower one while the day stands at
                # or above its target, the base one whenever it is below.
                stake = low if day + 1e-9 >= rate * cap else high
                if stake == low:
                    stats["signals at the lower stake"] += 1
                elif reached:
                    stats["signals back at the base after falling below"] += 1
            else:
                stake = low if (reached and rule in ("lower", "lower_stop")) else high
            p = pnl_of(stake, ask, s["won"])
            day += p
            low_pt = min(low_pt, day)
            last_won = bool(s["won"])
            entries.append((s["wo"], ask, s["won"]))
            if rule == "follow_stop" and day <= -STOP_RATE * cap + 1e-9:
                # THE DAILY LOSS STOP: down STOP_RATE of the capital (net of
                # fees) -> no new entry for the rest of the day.
                closed = True
                stats["days stopped at the loss limit"] += 1
            if rule in ("guard_below", "guard_loss"):
                # THE SAFEGUARD: after the day first reaches its target, a fall
                # back below it (guard_below) or into a loss for the day
                # (guard_loss) ARMS it; the next return to the target then
                # closes the day - the target is locked in, no more entries.
                at = day + 1e-9 >= rate * cap
                if at and armed:
                    closed = True
                    stats["days locked at the target after the safeguard armed"] += 1
                elif at:
                    hit_once = True
                elif hit_once and not armed and (
                        day < rate * cap if rule == "guard_below" else day < 0):
                    armed = True
                    stats["days the safeguard armed"] += 1
            if reached:
                phase2 += p
                if rule == "lower_stop" and phase2 + 1e-9 >= SECOND_RATE * cap:
                    closed = True
                    stats["days closed at the second target"] += 1
            elif rule != "none" and day + 1e-9 >= rate * cap:
                reached = True
        out[d] = (day, entries, low_pt)
    return out, stats


def mirror(entries_by_day, rule, account):
    _, high, low, rate, cap = account
    out = {}
    for d in days:
        day, reached = 0.0, False
        for _wo, ask, won in entries_by_day[d][1]:
            if reached and rule == "pause":
                break
            stake = low if (reached and rule == "lower") else high
            day += pnl_of(stake, ask, won)
            if not reached and rule != "none" and day + 1e-9 >= rate * cap:
                reached = True
        out[d] = day
    return out


print(f"[{STUDY_DB}] {len(signals)} signals {days[0]} to {days[-1]} ({len(days)} NY days; 09-22 is one hour). "
      f"Cushion after a loss: {NEED:g} bps (0 = off). Held to the result.\n")
names = {"none": "no target", "pause": "pause at target", "lower": "lower base after target",
         "lower_stop": "lower, close at +8% more", "follow": "lower only while >= target",
         "guard_below": "follow, lock 2nd hit (A)", "guard_loss": "follow, lock if <0 (B)"}
results = {}
for cushion in ((False, True) if NEED > 0 else (False,)):
    tag = f"WITH the {NEED:g} bps cushion (live)" if cushion else "WITHOUT the cushion"
    print(f"===== {tag}")
    print(f"{'rule':<26} {'You $' + format(PRIMARY[1], 'g'):>8} {'worst':>7} {'losing':>7} {'lowest':>8}   "
          f"{'Wife':>7} {'George':>7}")
    for rule in ("none", "pause", "lower", "lower_stop", "follow", "guard_below", "guard_loss"):
        res, stats = run(rule, cushion)
        results[(cushion, rule)] = res
        tot = sum(v[0] for v in res.values())
        # MIRRORS AS LIVE: no target under "none"; otherwise each pauses at its
        # own 15%, copying the primary's taken signals until then. ("lower"
        # used to model a $1 mirror phase - never deployed.)
        m = [sum(mirror(res, "none" if rule == "none" else "pause", acc).values())
             for acc in MIRRORS]
        print(f"{names[rule]:<26} {tot:>+8.2f} {min(v[0] for v in res.values()):>+7.2f} "
              f"{sum(1 for v in res.values() if v[0] < 0):>7} {min(v[2] for v in res.values()):>+8.2f}   "
              f"{m[0]:>+7.2f} {m[1]:>+7.2f}")
        if cushion and rule in ("lower", "lower_stop", "follow", "guard_below", "guard_loss"):
            print(f"   {rule}: " + ", ".join(f"{k} {v}" for k, v in stats.items()))
    print()

if NEED > 0:
    low_label = "$" + format(PRIMARY[2], "g") + " after"
    print(f"Per day, You at ${PRIMARY[1]:g} (with the cushion):   {'pause':>8} {low_label:>9} "
          f"{'close +8%':>10} {'follow':>8} {'A':>8} {'B':>8} {'no target':>10}")
    for d in days:
        print(f"   {d}{'':<32}{results[(True, 'pause')][d][0]:>+8.2f} "
              f"{results[(True, 'lower')][d][0]:>+9.2f} {results[(True, 'lower_stop')][d][0]:>+10.2f} "
              f"{results[(True, 'follow')][d][0]:>+8.2f} "
              f"{results[(True, 'guard_below')][d][0]:>+8.2f} "
              f"{results[(True, 'guard_loss')][d][0]:>+8.2f} "
              f"{results[(True, 'none')][d][0]:>+10.2f}")

if NEED > 0:
    # THE LIVE CONFIGURATION SINCE 2026-09-30: the primary drops to its lower
    # stake after its target, while each mirror still PAUSES at its own target -
    # copying the primary's taken signals until then.
    res, _ = run("lower", True)
    m = [sum(mirror(res, "pause", acc).values()) for acc in MIRRORS]
    print()
    print(f"Live since 09-30 (primary ${PRIMARY[1]:g} then ${PRIMARY[2]:g}; mirrors pause at "
          f"15%): You {sum(v[0] for v in res.values()):+.2f}, Wife {m[0]:+.2f}, George {m[1]:+.2f}")
