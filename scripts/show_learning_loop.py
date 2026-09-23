"""Show the loop closing: a decision recorded BEFORE settlement, graded after.

The whole forward-learning claim reduces to one observable thing - a row
written while the outcome was still unknown, and the same row carrying the
outcome afterwards. Anything else is a backtest wearing a live badge.

So this prints the rows in the order that proves it:

    ungraded    written at decision time, won IS NULL - the prediction
    graded      the same window, after settlement - the outcome

and checks the property that a tautological grader would violate: a losing
side must grade as a loss. That bug graded 21 of 21 rows as winners, and it
would look exactly like a working loop from the outside.

    python scripts/show_learning_loop.py
"""

import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from btc15_signal.config import Settings  # noqa: E402


def when(ms) -> str:
    return datetime.fromtimestamp(ms / 1000, UTC).strftime("%H:%M:%S") if ms else "-"


def show(db, table: str, title: str, extra: str = "") -> None:
    print(f"\n{'=' * 72}\n{title}\n{'=' * 72}")
    rows = [dict(r) for r in db.execute(
        f"SELECT * FROM {table} ORDER BY id DESC LIMIT 400"
    )]
    if not rows:
        print("  (no rows yet)")
        return
    ungraded = [r for r in rows if r.get("graded_ms") is None]
    graded = [r for r in rows if r.get("graded_ms") is not None]
    print(f"  {len(ungraded)} awaiting settlement, {len(graded)} graded")

    if ungraded:
        r = ungraded[0]
        print("\n  RECORDED BEFORE SETTLEMENT (the prediction)")
        print(f"    window {when(r['window_open'])}  decided {when(r['decided_ms'])}"
              f"  side {r.get('side')}  ask {r.get('ask')}")
        print(f"    context  {r.get('context_key')}")
        print(f"    {extra or 'action'}: {r.get('final_action') or r.get('proposed_action')}"
              f"   outcome: {r.get('won')!r}  (unknown, as it must be)")

    if graded:
        r = graded[0]
        print("\n  THE SAME KIND OF ROW, AFTER SETTLEMENT (the outcome)")
        print(f"    window {when(r['window_open'])}  graded {when(r['graded_ms'])}"
              f"  side {r.get('side')}")
        print(f"    context  {r.get('context_key')}")
        print(f"    outcome: won={r.get('won')}", end="")
        if "baseline_pnl" in r:
            print(f"   baseline {r['baseline_pnl']:+.4f}"
                  f"   candidate {r['candidate_pnl']:+.4f}")
        else:
            print()

    # The tautology check. `won` must vary with the side the row was on.
    sides = db.execute(
        f"SELECT side, COUNT(*) n, SUM(won) wins FROM {table} "
        f"WHERE graded_ms IS NOT NULL GROUP BY side"
    ).fetchall()
    if sides:
        print("\n  grading sanity (a tautological grader shows 100% everywhere)")
        total = wins = 0
        for s in sides:
            total += s["n"]
            wins += s["wins"] or 0
            print(f"    side {s['side'] or '?':<5} n={s['n']:<4} "
                  f"won={s['wins'] or 0:<4} ({(s['wins'] or 0) / s['n']:.0%})")
        if total and wins == total:
            print("    ** every graded row is a winner - suspect the grader,")
            print("       not the strategy **")


def main() -> None:
    settings = Settings()
    db = sqlite3.connect(f"file:{settings.database_path}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    show(db, "intelligence_decisions",
         "INTELLIGENCE DECISIONS - every signal, graded at settlement",
         extra="decision")
    show(db, "candidate_evaluations",
         "CANDIDATE FORWARD EVALUATION - what a frozen candidate would change",
         extra="candidate proposes")
    print()


if __name__ == "__main__":
    main()
