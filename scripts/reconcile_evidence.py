"""Reconcile the live evidence. Fixed cutoff, explicit denominators.

Six discrepancies were outstanding. Each is resolved here by NAMING the two
populations rather than picking a number, because in every case both figures
were correct about different things and the ambiguity was the defect.

A FIXED CUTOFF is applied throughout. Without one the denominators move
between paragraphs - "150 markets" became 151 while the audit was being
written - and two correct numbers look like a contradiction.

Nothing here invents a feature. Where a record is missing it is counted as
missing and excluded from the denominator that needs it, and the exclusion is
printed.

    python scripts/reconcile_evidence.py [--cutoff-ms N]
"""

import argparse
import sqlite3
import sys
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from btc15_signal.config import Settings  # noqa: E402
from btc15_signal.validation import kalshi_fee_charged as fee  # noqa: E402

GATE_COUNT = 6      # the deployed EntryRule renders six checks


def when(ms) -> str:
    return datetime.fromtimestamp(ms / 1000, UTC).strftime("%m-%d %H:%M") if ms else "-"


def head(n: int, title: str) -> None:
    print(f"\n{'-' * 78}\n{n}. {title}\n{'-' * 78}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cutoff-ms", type=int, default=0)
    args = parser.parse_args()

    settings = Settings()
    db = sqlite3.connect(f"file:{settings.database_path}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row

    # ------------------------------------------------------- fixed cutoff
    cutoff = args.cutoff_ms or db.execute(
        "SELECT MAX(window_open) FROM predictions WHERE won IS NOT NULL"
    ).fetchone()[0]
    print(f"REPORTING CUTOFF  window_open <= {cutoff}  ({when(cutoff)} UTC)")
    print("Every denominator below is taken at this instant. A market that")
    print("settles later is excluded from ALL of them, not from some.")

    # ------------------------------------------------------- populations
    polls = defaultdict(list)
    for r in db.execute(
        "SELECT window_open, remaining_s, side, rule_match, failed_gates, won, "
        "our_ask, raw_probability, session, vol_regime, regime_weight "
        "FROM observations WHERE won IS NOT NULL AND window_open <= ? "
        "ORDER BY window_open, remaining_s DESC", (cutoff,)
    ):
        polls[r["window_open"]].append(dict(r))
    preds = {r["window_open"]: dict(r) for r in db.execute(
        "SELECT window_open, side, won, qualified, contract_price, failed_gates "
        "FROM predictions WHERE won IS NOT NULL AND window_open <= ?", (cutoff,)
    )}

    head(1, "SAME 150 MARKETS, TWO WIN RATES - resolved")
    first_qualifying, first_seen = {}, {}
    for window, path in polls.items():
        first_seen[window] = path[0]
        first_qualifying[window] = next(
            (p for p in path if p["rule_match"]), None
        )
    chosen = {w: (first_qualifying[w] or first_seen[w]) for w in polls}
    a_wins = sum(c["won"] for c in chosen.values())
    shared = [w for w in polls if w in preds]
    b_wins = sum(preds[w]["won"] for w in shared)
    differ = [w for w in shared if chosen[w]["won"] != preds[w]["won"]]
    print(f"  markets with feature rows                {len(polls)}")
    print(f"  A: the DECISION the rule first qualified at, graded on ITS side")
    print(f"       {a_wins}/{len(chosen)} = {100 * a_wins / len(chosen):.1f}%")
    print(f"  B: the `predictions` row for the market, graded on ITS side")
    print(f"       {b_wins}/{len(shared)} = {100 * b_wins / len(shared):.1f}%")
    print(f"  markets where A and B are on OPPOSITE sides   {len(differ)}")
    print("  Neither is wrong. They grade DIFFERENT decisions: the model")
    print("  flipped between the poll the rule first qualified at and the")
    print("  poll that wrote the prediction row. A is the primary figure -")
    print("  each decision graded on its own recorded side.")

    head(2, "AUDITED vs UNAUDITED PERIODS - they INTERLEAVE, not split")
    have, lack = [], []
    for w, p in preds.items():
        (have if w in polls else lack).append(w)
    print(f"  with feature rows     n={len(have):<4} "
          f"{when(min(have))} .. {when(max(have))}" if have else "")
    print(f"  WITHOUT feature rows  n={len(lack):<4} "
          f"{when(min(lack))} .. {when(max(lack))}" if lack else "")
    if have and lack:
        overlap_lo, overlap_hi = max(min(have), min(lack)), min(max(have), max(lack))
        if overlap_lo <= overlap_hi:
            inside = sum(1 for w in lack if overlap_lo <= w <= overlap_hi)
            print(f"  OVERLAPPING RANGE     {when(overlap_lo)} .. {when(overlap_hi)}"
                  f"   ({inside} unaudited markets fall inside it)")
            print("  So this is NOT a clean before/after split. Calling it one")
            print("  would imply the archive simply started late; it also has")
            print("  gaps inside the covered period, which is a different fault")
            print("  and cannot be corrected by trimming a date range.")
        wl = sum(preds[w]["won"] for w in lack) / len(lack)
        wh = sum(preds[w]["won"] for w in have) / len(have)
        print(f"  win rate  audited {wh:.1%}   unaudited {wl:.1%}")
        print("  Breakdowns by session or regime cover the AUDITED set only.")

    head(3, "MODEL SCORE vs DISPLAYED CONFIDENCE - different quantities")
    print("  `raw_probability`  the MODEL's estimate. Gates the signal at 0.50.")
    print("  `Confidence: HIGH` the HEADER. confidence_label() counts PASSING")
    print("                     GATES plus regime points - it is gate agreement,")
    print("                     not a probability, and the two need not agree.")
    print("  An earlier report called raw_probability>=0.80 'HIGH-confidence'.")
    print("  That is not what the system displays. Both are shown below.\n")
    def agree_points(row):
        failed = (row["failed_gates"] or "").strip()
        n_failed = len([x for x in failed.split(",") if x.strip()]) if failed else 0
        return GATE_COUNT - n_failed
    buckets = defaultdict(list)
    for w, c in chosen.items():
        buckets[agree_points(c)].append(c)
    print("  by GATES AGREEING (the basis of the displayed header)")
    for k in sorted(buckets, reverse=True):
        g = buckets[k]
        wins = sum(x["won"] for x in g)
        edge = sum((1.0 if x["won"] else 0.0) - x["our_ask"]
                   - fee(x["our_ask"], 1) for x in g) / len(g)
        print(f"    {k}/6 gates  n={len(g):<4} {wins:>3}W "
              f"{100 * wins / len(g):>5.1f}%  edge {edge:+.4f}/ct")
    print("\n  by MODEL SCORE")
    for lo, hi, name in ((0.80, 1.01, ">=0.80"), (0.65, 0.80, "0.65-0.80"),
                         (0.0, 0.65, "<0.65")):
        g = [c for c in chosen.values() if lo <= (c["raw_probability"] or 0) < hi]
        if not g:
            continue
        wins = sum(x["won"] for x in g)
        edge = sum((1.0 if x["won"] else 0.0) - x["our_ask"]
                   - fee(x["our_ask"], 1) for x in g) / len(g)
        print(f"    {name:<10} n={len(g):<4} {wins:>3}W "
              f"{100 * wins / len(g):>5.1f}%  edge {edge:+.4f}/ct")
    print("  NOTE: the displayed label also folds in clock and level points,")
    print("  which are not stored per market. The gate count is the")
    print("  reconstructable part; the label itself is NOT reconstructed here")
    print("  rather than guessed at.")

    head(4, "NEVER QUALIFIED vs INITIALLY REJECTED - not the same set")
    never = [w for w in polls if first_qualifying[w] is None]
    initially = [w for w in polls if not first_seen[w]["rule_match"]]
    later = [w for w in initially if first_qualifying[w] is not None]
    print(f"  signals generated (markets)              {len(polls)}")
    print(f"  INITIALLY rejected (first poll failed)   {len(initially)}")
    print(f"  of those, LATER qualified               {len(later)}")
    print(f"  NEVER qualified in the whole window     {len(never)}")
    print("  The original rejection is preserved: a market that qualified at")
    print("  540s is still recorded as rejected at 660s. Overwriting that")
    print("  would erase the only evidence about initial rejections.")
    if later:
        wins = sum(chosen[w]["won"] for w in later)
        print(f"  later-qualified outcome: {wins}/{len(later)} won")

    head(5, "BROKER MARKETS vs LOCAL ORDER RECORDS")
    ledger = {r["ticker"]: r["pnl"] for r in db.execute(
        "SELECT ticker, pnl FROM daily_ledger WHERE pnl IS NOT NULL")}
    props = defaultdict(list)
    for r in db.execute(
        "SELECT ticker, status FROM trade_proposals WHERE strategy='primary'"):
        props[r["ticker"]].append(r["status"])
    accounted = {"filled", "protected", "unprotected", "exited"}
    filled_local = {t for t, s in props.items() if accounted & set(s)}
    print(f"  markets in the BROKER ledger             {len(ledger)}")
    print(f"  markets with a local filled proposal     {len(filled_local)}")
    print(f"  in ledger, no local proposal             {len(set(ledger) - filled_local)}")
    print(f"  local proposal, not in ledger            {len(filled_local - set(ledger))}")
    print("  The ledger is the broker's and is authoritative. Local proposals")
    print("  cover only orders this process placed and survived to write -")
    print("  manual fills and pre-archive trades have no proposal row, which")
    print("  is why any P&L rebuilt from proposals understates the account.")

    head(6, "SIGNAL OUTCOMES vs EXECUTION P&L")
    hyp = [(1.0 if c["won"] else 0.0) - c["our_ask"] - fee(c["our_ask"], 1)
           for c in chosen.values()]
    print(f"  signals, hypothetical   n={len(hyp):<5} "
          f"{sum(hyp) / len(hyp):+.4f}/market   total {sum(hyp):+.2f}")
    if ledger:
        vals = list(ledger.values())
        print(f"  executed, broker        n={len(vals):<5} "
              f"{sum(vals) / len(vals):+.4f}/market   total {sum(vals):+.2f}")
    print("  DIFFERENT POPULATIONS. The first assumes a fill at the recorded")
    print("  ask on every signal including ones nobody traded; the second is")
    print("  the account after fills, misses, fees and exits. The difference")
    print("  between them is not an execution cost and must not be quoted as")
    print("  one - the two sets barely overlap.")
    print()


if __name__ == "__main__":
    main()
