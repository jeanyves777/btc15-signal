"""A DAILY LOSS STOP on top of the live rules ($6 below the day's 8% target, $3
at/above, 5 bps after-loss cushion), per instrument - on the recorded signals and
lifecycle only (operator, 2026-10-01: "what could a daily stop loss look like and
how it would help them").

    python scripts/loss_stop_sweep.py btc15.db eth15.db sol15.db gold15.db
"""
import contextlib
import io
import os
import runpy
import sys

ROOT = r"D:\Kalshi\btc15-signal"
STOPS = (None, 0.05, 0.08, 0.10, 0.15, 0.20)
CAP = 115.39


def load(db, stop):
    os.environ["STUDY_DB"] = db
    os.environ["STOP_RATE"] = str(stop or 0.0)
    sys.argv = ["x", "5", "6", "3", str(CAP)]
    with contextlib.redirect_stdout(io.StringIO()):
        return runpy.run_path(ROOT + r"\scripts\target_rules_live.py")


for db in sys.argv[1:] or ["btc15.db", "eth15.db", "sol15.db", "gold15.db"]:
    print(f"=== {db}  (live rules + a daily loss stop; held to the result)")
    print(f"{'stop at':>14} {'total':>8} {'worst':>8} {'losing':>7} {'stopped':>8}   per day")
    for stop in STOPS:
        g = load(db, stop)
        rule = "follow" if stop is None else "follow_stop"
        res, stats = g["run"](rule, True)
        days = g["days"]
        per = [res[d][0] for d in days]
        label = "none (live)" if stop is None else f"-{stop:.0%} (${stop * CAP:.2f})"
        print(f"{label:>14} {sum(per):>+8.2f} {min(per):>+8.2f} {sum(1 for v in per if v < 0):>7} "
              f"{stats.get('days stopped at the loss limit', 0):>8}   "
              + " ".join(f"{v:+.1f}" for v in per))
    print(f"{'':>50}days: " + " ".join(days))
    print()
