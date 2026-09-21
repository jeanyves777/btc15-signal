"""Read the intelligence ledger.

The ledger accumulates across runs, so these queries answer questions about the
whole history rather than the latest report: which regimes keep showing up,
whether the early-exit advantage is stable, and how verdicts have moved as more
data arrived.
"""

import argparse
import json
import sqlite3
from pathlib import Path


def _rows(db: sqlite3.Connection, query: str, params: tuple = ()) -> list[dict]:
    cursor = db.execute(query, params)
    keys = [column[0] for column in cursor.description]
    return [dict(zip(keys, row, strict=True)) for row in cursor.fetchall()]


def show_runs(db: sqlite3.Connection, limit: int) -> None:
    rows = _rows(
        db,
        "SELECT run_id,created_at,data_start,data_end,markets,snapshots,verdict "
        "FROM runs ORDER BY created_at DESC LIMIT ?",
        (limit,),
    )
    print(f"\n{'RUN':<24} {'MARKETS':>8} {'SNAPSHOTS':>10}  VERDICT")
    print("-" * 78)
    for row in rows:
        print(
            f"{row['run_id']:<24} {row['markets']:>8} {row['snapshots']:>10}  "
            f"{row['verdict'] or '(incomplete)'}"
        )


def show_findings(db: sqlite3.Connection, run_id: str | None, include_noise: bool) -> None:
    clause = "WHERE run_id=?" if run_id else ""
    params = (run_id,) if run_id else ()
    rows = _rows(
        db,
        "SELECT kind,dimension,bucket,trades,win_rate,roi,lower_95,upper_95,p_value,verdict "
        f"FROM findings {clause} ORDER BY kind, p_value",
        params,
    )
    if not include_noise:
        rows = [row for row in rows if row["verdict"] != "noise"]
    print(f"\n{'STRATEGY/KIND':<22} {'DIMENSION':<18} {'BUCKET':<12} {'N':>6} "
          f"{'WIN':>7} {'EDGE 95% LOW':>13} {'p':>8}  VERDICT")
    print("-" * 110)
    if not rows:
        print("no findings survived correction - every apparent pocket is consistent with noise")
        return
    def cell(value, spec: str, width: int) -> str:
        # A missing number is printed as a dash, not crashed on. Findings rows
        # legitimately lack some columns depending on which analysis wrote them.
        return format(value, spec) if value is not None else "-".rjust(width)

    for row in rows:
        print(
            f"{row['kind'] or '-':<22} {row['dimension'] or '-':<18} "
            f"{row['bucket'] or '-':<12} {cell(row['trades'], '>6', 6)} "
            f"{cell(row['win_rate'], '>6.1%', 7)} {cell(row['lower_95'], '>13.4f', 13)} "
            f"{cell(row['p_value'], '>8.4f', 8)}  {row['verdict'] or '-'}"
        )


def show_lifecycle(db: sqlite3.Connection, run_id: str | None) -> None:
    clause = "WHERE run_id=?" if run_id else ""
    params = (run_id,) if run_id else ()
    rows = _rows(
        db,
        "SELECT strategy, COUNT(*) AS trades, "
        "SUM(CASE WHEN settled_won=0 THEN 1 ELSE 0 END) AS losers, "
        "SUM(rescuable_loss) AS rescuable, SUM(squandered_win) AS squandered, "
        "AVG(mfe) AS avg_mfe, AVG(mae) AS avg_mae, SUM(settlement_pnl) AS held_pnl "
        f"FROM trades {clause} GROUP BY strategy",
        params,
    )
    print(f"\n{'STRATEGY':<14} {'TRADES':>7} {'LOSERS':>7} {'RESCUABLE':>10} {'SQUANDERED':>11} "
          f"{'AVG MFE':>9} {'AVG MAE':>9} {'HELD P&L':>10}")
    print("-" * 92)
    for row in rows:
        share = row["rescuable"] / row["losers"] if row["losers"] else 0.0
        print(
            f"{row['strategy']:<14} {row['trades']:>7} {row['losers']:>7} "
            f"{row['rescuable']:>6} ({share:>3.0%}) {row['squandered']:>11} "
            f"{row['avg_mfe']:>9.4f} {row['avg_mae']:>9.4f} {row['held_pnl']:>10.2f}"
        )


def show_policies(db: sqlite3.Connection, run_id: str | None) -> None:
    clause = "WHERE run_id=?" if run_id else ""
    params = (run_id,) if run_id else ()
    rows = _rows(
        db,
        f"SELECT policy,trades,win_rate,total_pnl,roi FROM policy_results {clause} "
        "ORDER BY roi DESC",
        params,
    )
    print(f"\n{'POLICY':<34} {'TRADES':>7} {'WIN':>7} {'ROI':>9} {'P&L':>10}")
    print("-" * 72)
    for row in rows:
        if not row["trades"]:
            continue
        print(
            f"{row['policy']:<34} {row['trades']:>7} {row['win_rate']:>6.1%} "
            f"{row['roi']:>8.2%} {row['total_pnl']:>10.2f}"
        )


def show_path(db: sqlite3.Connection, ticker: str) -> None:
    """The full trigger-to-close story of one trade."""
    trades = _rows(db, "SELECT * FROM trades WHERE ticker=?", (ticker,))
    if not trades:
        print(f"no trade recorded for {ticker}")
        return
    for trade in trades:
        outcome = {1: "WON", 0: "LOST", None: "unsettled"}[trade["settled_won"]]
        print(
            f"\n{trade['ticker']}  {trade['strategy']}  {trade['side']} @ "
            f"{trade['entry_price']:.2f} on minute {trade['entry_minute']}  ->  {outcome} "
            f"(settlement P&L {trade['settlement_pnl']:+.2f})"
        )
        print(f"  best reachable {trade['mfe']:+.2f}   worst {trade['mae']:+.2f}")
        path = _rows(
            db,
            "SELECT minute,best,worst,close FROM trade_path "
            "WHERE run_id=? AND strategy=? AND ticker=? AND entry_minute=? ORDER BY minute",
            (trade["run_id"], trade["strategy"], ticker, trade["entry_minute"]),
        )
        print(f"  {'MIN':>4} {'BEST EXIT':>10} {'WORST':>8} {'CLOSE':>8}  {'P&L IF CLOSED':>14}")
        for point in path:
            print(
                f"  {point['minute']:>4} {point['best']:>10.2f} {point['worst']:>8.2f} "
                f"{point['close']:>8.2f}  {point['best'] - trade['entry_price']:>+14.2f}"
            )


def main() -> None:
    parser = argparse.ArgumentParser(description="Query the BTC15 intelligence ledger")
    parser.add_argument("--ledger", default="data/intelligence.db")
    parser.add_argument("--run", default=None, help="restrict to one run id")
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--include-noise", action="store_true")
    parser.add_argument("--path", default=None, help="print one market's full trade lifecycle")
    parser.add_argument(
        "--show",
        default="runs,findings,lifecycle,policies",
        help="comma separated: runs, findings, lifecycle, policies",
    )
    args = parser.parse_args()

    if not Path(args.ledger).exists():
        raise SystemExit(f"no ledger at {args.ledger} - run btc15-validate first")
    db = sqlite3.connect(args.ledger)

    if args.path:
        show_path(db, args.path)
        return

    sections = {
        "runs": lambda: show_runs(db, args.limit),
        "findings": lambda: show_findings(db, args.run, args.include_noise),
        "lifecycle": lambda: show_lifecycle(db, args.run),
        "policies": lambda: show_policies(db, args.run),
    }
    for name in args.show.split(","):
        handler = sections.get(name.strip())
        if handler:
            handler()
    print()


if __name__ == "__main__":
    main()


def run() -> None:
    main()


def as_json(ledger: str, run_id: str | None = None) -> str:
    db = sqlite3.connect(ledger)
    return json.dumps(
        {
            "runs": _rows(db, "SELECT * FROM runs ORDER BY created_at DESC LIMIT 20"),
            "findings": _rows(
                db,
                "SELECT * FROM findings" + (" WHERE run_id=?" if run_id else ""),
                (run_id,) if run_id else (),
            ),
        },
        indent=2,
        default=str,
    )
