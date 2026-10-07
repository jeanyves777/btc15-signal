"""All five instruments: what was blocked, what was let through, what happened.

Classified by market LIFECYCLE rather than by the alert-time snapshot. That
distinction is not academic - reading `predictions.qualified` as a market's
verdict once made BTC appear to have qualified 4 signals and declined 52 that
scored +7.7%, when 9 of those "declined" markets had actually been traded
9W/0L. A market is judged on whether it was EVER eligible.

Four questions, kept apart, because "the declined market won" answers none of
them:

    never eligible          no evaluation ever passed every gate
    became eligible         at least one did
    eligible, not executed  eligible but no order resulted
    traded                  an order filled

Money is the broker's, scoped to each instrument's own series. Signal records
count CALLS - a refused signal is still a prediction that was right or wrong -
so they include markets no order ever touched.

Residual is win rate minus the price the market quoted: a calibration measure,
not realised profit. It excludes fees and assumes a fill at the quoted price.
"""

import sqlite3
import statistics
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from btc15_signal import market_lifecycle as lc  # noqa: E402

INSTRUMENTS = [
    ("BTC", "btc15.db", "KXBTC15M"),
    ("ETH", "eth15.db", "KXETH15M"),
    ("GOLD", "gold15.db", "KXGOLD15M"),
    ("SILVER", "silver15.db", "KXSILVER15M"),
    ("SOL", "sol15.db", "KXSOL15M"),
]


def norm(p):
    return None if p is None else (p / 100 if p > 1 else p)


def money(db, series):
    d = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    row = d.execute(
        "SELECT COUNT(*), COALESCE(SUM(pnl > 0), 0), COALESCE(SUM(pnl), 0) "
        "  FROM daily_ledger WHERE ticker LIKE ?", (f"{series}-%",)).fetchone()
    sig = d.execute(
        "SELECT COUNT(*), COALESCE(SUM(won), 0) FROM predictions "
        " WHERE won IS NOT NULL AND contract_ticker LIKE ?",
        (f"{series}-%",)).fetchone()
    d.close()
    return {"markets": row[0] or 0, "wins": row[1] or 0,
            "pnl": row[2] or 0.0,
            "sig_n": sig[0] or 0, "sig_w": sig[1] or 0}


def scored(lives, keep):
    rows = [x for x in lives if keep(x) and x.outcome in (lc.WON, lc.LOST)
            and norm(x.contract_price) is not None]
    if not rows:
        return None
    win = sum(1 for x in rows if x.outcome == lc.WON) / len(rows)
    ask = statistics.mean(norm(x.contract_price) for x in rows)
    return len(rows), win, ask, win - ask


def fmt(s):
    if not s:
        return f"{'-':>34}"
    n, win, ask, res = s
    return f"n={n:<4} {win:>5.1%}  ask {ask:.3f}  res {res:>+6.1%}"


def main():
    print("=" * 78)
    print("ALL FIVE INSTRUMENTS - blocked, let through, and what happened")
    print("=" * 78)

    for label, db, series in INSTRUMENTS:
        if not Path(db).exists():
            continue
        lives = list(lc.build(db).values())
        m = money(db, series)
        print(f"\n{'-' * 78}\n{label}  ({series})\n{'-' * 78}")

        print(f"  MONEY (broker, this series only)   "
              f"{m['pnl']:+.4f} over {m['markets']} settled markets"
              f"  {m['wins']}W-{m['markets'] - m['wins']}L")
        if m["sig_n"]:
            print(f"  SIGNAL RECORD (every call)         "
                  f"{m['sig_w']}W-{m['sig_n'] - m['sig_w']}L over "
                  f"{m['sig_n']} settled = {m['sig_w'] / m['sig_n']:.0%}")
        if not lives:
            print("  no evaluations recorded")
            continue

        elig = [x for x in lives if x.ever_eligible]
        print(f"\n  LET THROUGH vs BLOCKED   {len(lives)} markets evaluated, "
              f"{sum(x.evaluations for x in lives)} evaluations")
        print(f"    let through (ever eligible) {len(elig):>5}"
              f"   {len(elig) / len(lives):>5.0%}")
        print(f"    blocked (never eligible)    "
              f"{len(lives) - len(elig):>5}"
              f"   {1 - len(elig) / len(lives):>5.0%}")

        gates = Counter()
        for x in lives:
            if x.ever_eligible:
                continue
            for g in str(x.first_failed_gates or "").replace("·", ",").split(","):
                g = g.strip()
                if g:
                    gates[g] += 1
        if gates:
            print("    what blocked them:")
            for g, n in gates.most_common(5):
                print(f"      {g[:44]:<44} {n}")

        print("\n  OUTCOMES")
        print(f"    blocked                  {fmt(scored(lives, lambda x: not x.ever_eligible))}")
        print(f"    let through              {fmt(scored(lives, lambda x: x.ever_eligible))}")
        print(f"      of which traded        {fmt(scored(lives, lambda x: x.traded))}")
        print(f"      eligible, not executed {fmt(scored(lives, lambda x: x.ever_eligible and not x.traded))}")

        ex = Counter(x.execution for x in lives if x.ever_eligible)
        if ex:
            print("    execution of the eligible: "
                  + ", ".join(f"{k} {v}" for k, v in ex.most_common()))

    print(f"\n{'=' * 78}")
    print("Residual is win rate minus quoted price - calibration, not realised")
    print("profit. It excludes fees and assumes a fill at the quoted price,")
    print("which for untraded markets is an addition counterfactual this")
    print("system cannot verify (no historical book depth).")


if __name__ == "__main__":
    main()
