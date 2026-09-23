"""Re-grade intelligence rows that the tautological grader scored wrong.

The settlement loop passed `result == ("yes" if winning_side == "UP" else
"no")` as `won`. `winning_side` is derived from `result` one line above, so
the expression is true by construction and every graded row scored a WIN.

It only writes a FALSE row where the recorded side differs from the side that
won - which happens when the model flips inside a window, so the damage is
small and completely invisible: a 100% win rate looks like a good strategy,
not a broken grader.

This recomputes each graded row against the settled outcome in `predictions`
and corrects only the rows that disagree. It is idempotent, it prints every
change before making it, and it never invents an outcome for a market that
has not settled.

    python scripts/repair_intelligence_grades.py [--apply]
"""

import argparse
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from btc15_signal.config import Settings  # noqa: E402

TABLES = ("intelligence_decisions", "candidate_evaluations")


def winning_side(prediction_side: str, prediction_won: int) -> str:
    """The side that actually won, from a settled prediction."""
    if prediction_won:
        return prediction_side
    return "DOWN" if prediction_side == "UP" else "UP"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true",
                        help="write the corrections (default: dry run)")
    args = parser.parse_args()

    settings = Settings()
    db = sqlite3.connect(settings.database_path, timeout=30)
    db.row_factory = sqlite3.Row

    total_wrong = 0
    for table in TABLES:
        exists = db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        if not exists:
            continue
        rows = [dict(r) for r in db.execute(
            f"SELECT t.id, t.window_open, t.side, t.won, "
            f"p.side AS p_side, p.won AS p_won "
            f"FROM {table} t JOIN predictions p ON p.window_open = t.window_open "
            f"WHERE t.graded_ms IS NOT NULL AND p.won IS NOT NULL"
        )]
        wrong = []
        for row in rows:
            if not row["side"] or not row["p_side"]:
                continue        # cannot be scored on a side it never recorded
            truth = int(row["side"] == winning_side(row["p_side"], row["p_won"]))
            if row["won"] != truth:
                wrong.append((row, truth))

        print(f"{table}: {len(rows)} graded, {len(wrong)} scored wrong")
        for row, truth in wrong:
            print(f"    id={row['id']} window={row['window_open']} "
                  f"side={row['side']} (winner was "
                  f"{winning_side(row['p_side'], row['p_won'])}): "
                  f"won {row['won']} -> {truth}")
        total_wrong += len(wrong)

        if wrong and args.apply:
            db.executemany(
                f"UPDATE {table} SET won=? WHERE id=?",
                [(truth, row["id"]) for row, truth in wrong],
            )
            db.commit()
            print(f"    corrected {len(wrong)} rows")

    if not args.apply and total_wrong:
        print(f"\ndry run - {total_wrong} rows would change. "
              f"Re-run with --apply to write them.")
    elif not total_wrong:
        print("\nnothing to correct.")


if __name__ == "__main__":
    main()
