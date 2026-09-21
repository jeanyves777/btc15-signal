"""Grade the similarity layer's calls against what actually happened.

The operator's loop: allow both early and late entries, record what each would
have made, and let the next identical setup be decided by which one paid. This
is the grading half. Until it says the layer's calls beat the deployed rule on
realised money, the layer stays in shadow - that condition is the operator's
own and it is the only thing that separates a reasoning system from a
convincing one.

Three questions, in the order that matters:

1. Is the retrieved win probability CALIBRATED? A layer that says 79% and wins
   60% of the time is worse than useless, however good its prose.
2. When it said ENTER NOW, was entering now actually right?
3. When it said WAIT, would waiting actually have paid?

Question 1 comes first because 2 and 3 are meaningless if it is not.

    python scripts/score_shadow.py
"""

import math
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from btc15_signal.config import Settings  # noqa: E402
from btc15_signal.store import wilson_lower  # noqa: E402
from btc15_signal.validation import kalshi_fee_charged  # noqa: E402

# A calibration verdict needs a sample to be a verdict. On TWO settled
# reads this report printed "consistent with being calibrated": at n=2 the
# z-statistic cannot reach 1.96 for any miscalibration the layer could
# plausibly have, so the test had no power and the reassuring line was an
# artifact of the sample size rather than a finding about the layer.
MIN_CALIBRATION_N = 30


def main() -> None:
    settings = Settings()
    db = sqlite3.connect(f"file:{settings.database_path}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    try:
        rows = db.execute(
            "SELECT * FROM shadow_decisions WHERE won IS NOT NULL"
        ).fetchall()
    except sqlite3.OperationalError:
        print("no shadow_decisions table yet - the layer has not run")
        return

    print("=" * 78)
    print("SHADOW SCORECARD - what the similarity layer would have decided")
    print("=" * 78)
    pending = db.execute(
        "SELECT COUNT(*) FROM shadow_decisions WHERE won IS NULL"
    ).fetchone()[0]
    print(f"\nsettled reads {len(rows)}, awaiting settlement {pending}")
    if not rows:
        print("\nNothing settled yet. Re-run once windows have closed.")
        print("=" * 78)
        return

    # --- 0. THE LEDGER ----------------------------------------------------
    # Every read, one line, with what it cost. A scorecard that only reports
    # aggregates hides the single bad call that taught you something - and on
    # 2026-09-21 one bad call is most of what there is to learn from.
    print("\n0. THE LEDGER - every settled recommendation and what it cost")
    # LABELLED rather than made purely ex-post, because a purely ex-post
    # regret is not computable from what is archived: whether the price
    # actually came back in THIS window is nowhere in the row - only the
    # cohort's dip_rate is, and that is an expectation over other markets.
    # Differencing an ex-post outcome against an ex-ante expectation under
    # a bare "realised" header is what the tilde now stops.
    print("   `realised` is ex-post: what entering actually paid, net of fee.")
    print("   `regret~` is MIXED: the ENTER NOW arm is ex-post, the WAIT arm")
    print("   is only the ex-ante expected value of the retracement policy.")
    print(f"  {'when':<9}{'ticker':<26}{'rec':<12}{'ask':>6}{'p(win)':>8}"
          f"{'out':>6}{'realised':>10}{'regret~':>9}")
    for r in sorted(rows, key=lambda r: r["created_at"]):
        ask = r["ask"]
        fee = kalshi_fee_charged(ask, 1)
        # What entering actually would have paid, which for a loss is the whole
        # stake plus the fee - the counterfactual the recommendation owns.
        realised = (1.0 if r["won"] else 0.0) - ask - fee
        waited = r["wait_limit_net"] or 0.0
        chosen = realised if r["action"] == "ENTER NOW" else (
            waited if r["action"] == "WAIT FOR RETRACEMENT" else 0.0
        )
        # Regret against the best action available in hindsight, including PASS
        # (worth exactly zero, which is often the best of the three).
        # MIXED CLOCKS: `realised` is ex-post, `waited` is an ex-ante
        # expectation already multiplied by a fill probability. This is the
        # `regret~` column, not a realised regret - see the header above.
        regret = chosen - max(realised, waited, 0.0)
        when = datetime.fromtimestamp(
            r["created_at"] / 1000, UTC
        ).strftime("%H:%M:%S")
        print(f"  {when:<9}{r['ticker'][-24:]:<26}{r['action']:<12}"
              f"{ask:>6.2f}{r['win_probability']:>8.0%}"
              f"{'WIN' if r['won'] else 'LOSS':>6}"
              f"{realised:>+10.4f}{regret:>+9.4f}")

    # Calibration error per read, and the Brier score, which punishes a
    # confident wrong answer far harder than an unsure one - the right
    # scoring rule for a layer whose failure mode is misplaced certainty.
    brier = sum((r["win_probability"] - (1.0 if r["won"] else 0.0)) ** 2
                for r in rows) / len(rows)
    base = sum(r["won"] for r in rows) / len(rows)
    base_brier = sum((base - (1.0 if r["won"] else 0.0)) ** 2
                     for r in rows) / len(rows)
    print(f"\n  Brier score                   {brier:.4f} "
          f"(always-predict-{base:.0%} scores {base_brier:.4f})")
    print("  Lower is better. Beating the constant predictor is the minimum")
    print("  bar for the retrieval to be adding anything at all.")

    # --- 1. CALIBRATION ---------------------------------------------------
    print("\n1. IS THE PREDICTED WIN PROBABILITY CALIBRATED?")
    predicted = sum(r["win_probability"] for r in rows)
    actual = sum(r["won"] for r in rows)
    variance = sum(
        r["win_probability"] * (1 - r["win_probability"]) for r in rows
    )
    z = (actual - predicted) / math.sqrt(variance) if variance > 0 else 0.0
    print(f"  reads                         {len(rows)}")
    print(f"  wins it predicted             {predicted:.1f}")
    print(f"  wins that happened            {actual}")
    print(f"  z                             {z:+.2f}")
    if len(rows) >= MIN_CALIBRATION_N and abs(z) < 1.96:
        print("  => consistent with being calibrated on this sample")
    elif abs(z) >= 1.96:
        # A DETECTED miss is still reported under the minimum: refusing to
        # affirm calibration is the fix, refusing to report a failure would
        # be a new defect.
        print("  => NOT calibrated: it is systematically "
              + ("over" if z < 0 else "under") + "confident")
        if len(rows) < MIN_CALIBRATION_N:
            print(f"     (provisional: {len(rows)} reads, under the "
                  f"{MIN_CALIBRATION_N} this report requires)")
    else:
        print(f"  => NO VERDICT: {len(rows)} settled reads is under the "
              f"{MIN_CALIBRATION_N} this report requires - "
              f"{MIN_CALIBRATION_N - len(rows)} more needed")
        print("     |z| under 1.96 on a sample this small means the test")
        print("     had no power, NOT that the probability is calibrated.")

    # --- 2. THE CALLS -----------------------------------------------------
    print("\n2. WERE THE CALLS RIGHT?")
    # These two columns were not comparable: `now` was GROSS of fee while
    # `waited` was net of fee AND multiplied by a fill probability. Both
    # biases pointed the same way - toward ENTER NOW, which is the layer's
    # own default - so the `better` column was rigged in its favour.
    print("   `now/ct` is entering at the read's own ask, NET OF FEE, and it")
    print("   fills with certainty: NO fill probability is applied to it.")
    print("   `waited/ct` is the retracement policy, also NET OF FEE, but it")
    print("   IS multiplied by the fill probability - windows that never came")
    print("   back contribute zero. The arms are therefore not like for like")
    print("   and `better` compares expectations, not realised money.")
    print(f"\n  {'action':<22}{'n':>5}{'won':>7}{'now/ct':>10}{'waited/ct':>11}"
          f"{'better':>9}")
    for action in ("ENTER NOW", "WAIT FOR RETRACEMENT", "PASS"):
        sel = [r for r in rows if r["action"] == action]
        if not sel:
            print(f"  {action:<22}{0:>5}   no reads")
            continue
        n = len(sel)
        wins = sum(r["won"] for r in sel)
        # Net of the fee that would actually have been charged, so this is
        # now the same quantity `waited` already was.
        now = sum(
            (1.0 if r["won"] else 0.0) - r["ask"] - kalshi_fee_charged(r["ask"], 1)
            for r in sel
        ) / n
        # NULL on rows written before the column existed; matches the `or
        # 0.0` the ledger above already uses rather than raising TypeError
        # on a mixed-age archive.
        waited = sum((r["wait_limit_net"] or 0.0) for r in sel) / n
        better = "now" if now > waited else "waited"
        print(f"  {action:<22}{n:>5}{wins / n:>7.0%}{now:>+10.4f}"
              f"{waited:>+11.4f}{better:>9}")

    # --- 3. AGAINST THE DEPLOYED RULE ------------------------------------
    print("\n3. AGAINST THE RULE THAT IS ACTUALLY TRADING")
    agree = [r for r in rows if r["rule_qualified"] and r["action"] == "ENTER NOW"]
    override = [r for r in rows if r["rule_qualified"] and r["action"] != "ENTER NOW"]
    missed = [r for r in rows if not r["rule_qualified"] and r["action"] == "ENTER NOW"]
    for label, sel in (
        ("rule YES, shadow ENTER NOW", agree),
        ("rule YES, shadow disagreed", override),
        ("rule NO, shadow ENTER NOW", missed),
    ):
        if not sel:
            print(f"  {label:<30} 0")
            continue
        n = len(sel)
        wins = sum(r["won"] for r in sel)
        # Was the same bare gross figure as section 2, labelled "gross" and
        # compared against nothing net - so every disagreement in this
        # section read a fee better than it actually was.
        net = sum(
            (1.0 if r["won"] else 0.0) - r["ask"] - kalshi_fee_charged(r["ask"], 1)
            for r in sel
        ) / n
        print(f"  {label:<30} {n:<5} {wins}/{n} won  {net:+.4f}/ct net   "
              f"lower bound {wilson_lower(wins, n):.0%}")

    print("\n" + "=" * 78)
    print("PROMOTION REQUIRES section 1 to be calibrated AND the disagreements")
    print("in section 3 to be paying. Neither is decidable on a handful of")
    print("reads - section 7 puts the sample at thousands - so the honest")
    print("state of this layer stays SHADOW until this report says otherwise.")
    print("=" * 78)


if __name__ == "__main__":
    main()
