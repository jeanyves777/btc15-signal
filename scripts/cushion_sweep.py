"""The after-loss cushion at several widths, per instrument, under the LIVE rules
($6 below the day's 8% target, $3 at/above it) - on the recorded signals and
lifecycle only (operator, 2026-10-01: "try higher bp just for the others except
BTC - try them at 8 to 12 bp, and see").

    python scripts/cushion_sweep.py eth15.db sol15.db gold15.db
"""
import contextlib
import io
import os
import runpy
import sys

ROOT = r"D:\Kalshi\btc15-signal"
WIDTHS = (0, 5, 6, 7, 8, 9, 10, 11, 12)
FREEZE_MS = None    # set by --before HH:MM (NY, today) to freeze the signal set


def load(db, need):
    os.environ["STUDY_DB"] = db
    sys.argv = ["x", str(need), "6", "3", "115.39"]
    with contextlib.redirect_stdout(io.StringIO()):
        return runpy.run_path(ROOT + r"\scripts\target_rules_live.py")


for db in sys.argv[1:] or ["eth15.db", "sol15.db", "gold15.db"]:
    print(f"=== {db}  (live rules; held to the result)")
    print(f"{'cushion':>8} {'total':>8} {'worst':>8} {'losing':>7} {'09-28':>8}   per day")
    rows = []
    for need in WIDTHS:
        g = load(db, need)
        res, _ = g["run"]("follow", need > 0)
        days = g["days"]
        per = [res[d][0] for d in days]
        rows.append((need, sum(per), min(per), sum(1 for v in per if v < 0),
                     res["09-28"][0] if "09-28" in res else float("nan"), days, per))
    for need, tot, worst, losing, d28, days, per in rows:
        print(f"{(str(need) + ' bp') if need else 'off':>8} {tot:>+8.2f} {worst:>+8.2f} {losing:>7} "
              f"{d28:>+8.2f}   " + " ".join(f"{v:+.1f}" for v in per))
    print(f"{'':>45}   days: " + " ".join(rows[0][5]))
    print()
