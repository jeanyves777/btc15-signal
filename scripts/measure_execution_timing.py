"""Is there edge in WHEN we enter, given that there is none in WHAT we pick?

Section 36 measured direction models and found none beat the ask: logistic
0.2114, the price itself 0.2112, retrieval 0.2133. The remaining hypothesis is
the operator's - that any advantage has to come from execution timing, fill
behaviour and retracement rather than from another opinion about UP or DOWN.

This tests that hypothesis directly, and it is a fundamentally easier question
to answer than direction, for one reason:

  PAIRED MEASUREMENT. Enter-now and wait are evaluated ON THE SAME MARKET, so
  whether that market won or lost is held constant and cancels in the
  difference. Direction variance - the thing that swamped every measurement in
  sections 7 and 33 - is removed by construction rather than averaged over. A
  paired test on 6,428 markets sees a 1c timing effect that an unpaired test
  would need hundreds of thousands of trades to resolve.

Three policies, all implementable, none requiring foresight:

  ENTER NOW    cross at the current ask
  WAIT-TO-LAST take whatever the ask is at the last quote we see
  WAIT-LIMIT   rest a limit `delta` below the ask; if it never trades there,
               NO TRADE - not a simulated loss, not a simulated win

A fourth is reported as a ceiling only: WAIT-ORACLE, taking the best ask that
ever appeared. It needs foresight and can never be traded; it is there to say
how much room the realisable policies are leaving on the table, which bounds
how good any timing model could possibly get.

    python scripts/measure_execution_timing.py
"""

import math
import random
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from btc15_signal import baseline as B  # noqa: E402
from btc15_signal.validation import kalshi_fee_charged as fee  # noqa: E402

LIMIT_DELTAS = (0.01, 0.02, 0.03, 0.05)
FOLDS = 5


def load_markets() -> list[dict]:
    """One row per market, at its FIRST decision minute.

    Deduplicated for the reason section 36 sets out: a market that sat in the
    band for many minutes is disproportionately one that went on to win, so
    keeping every minute both duplicates the market and tilts the base rate.
    """
    path = Path("data/cohort.db")
    if not path.exists():
        return []
    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    seen: set[str] = set()
    out = []
    for row in db.execute(
        "SELECT * FROM cohort ORDER BY open_ms, remaining DESC"
    ):
        if row["ticker"] in seen:
            continue
        seen.add(row["ticker"])
        if row["ask"] is None or row["best_later_ask"] is None:
            continue
        out.append({
            "ticker": row["ticker"], "open_ms": row["open_ms"],
            "our_ask": row["ask"], "best_later": row["best_later_ask"],
            "last_ask": row["last_ask"], "ran_away": row["ran_away"],
            "won": int(row["won"]),
            "side": "UP" if row["side_is_up"] else "DOWN",
            "normalized_distance": row["normalized_distance"],
            "remaining_s": row["remaining"] * 60,
            "momentum_5m_bps": row["momentum_bps"],
            "volatility_5m_bps": row["volatility_bps"],
            "spread_bps": 0.0, "band_held_s": 0.0,
            "session": row["session"], "vol_regime": row["vol_regime"],
        })
    return out


def net(price: float, won: int) -> float:
    return (1.0 if won else 0.0) - price - fee(price, 1)


def policies(row: dict, delta: float) -> dict:
    """Net dollars per contract under each policy, for ONE market."""
    now = net(row["our_ask"], row["won"])
    last = net(row["last_ask"], row["won"]) if row["last_ask"] else now
    limit = row["our_ask"] - delta
    # A resting limit trades only if the ask actually reached it. If it never
    # did, there is no position: zero, and zero is NOT a loss - counting a
    # no-fill as a loss is what makes every wait policy look catastrophic, and
    # counting it as a win is what makes them look free.
    filled = row["best_later"] is not None and row["best_later"] <= limit + 1e-9
    wait_limit = net(limit, row["won"]) if filled else 0.0
    oracle = net(row["best_later"], row["won"]) if row["best_later"] else now
    return {
        "now": now, "last": last, "limit": wait_limit,
        "limit_filled": filled, "oracle": oracle,
    }


def paired_ci(diffs: list[float], draws: int = 4000, seed: int = 11):
    if len(diffs) < 2:
        return 0.0, 0.0
    rng = random.Random(seed)
    means = sorted(
        sum(rng.choice(diffs) for _ in diffs) / len(diffs) for _ in range(draws)
    )
    return means[int(0.025 * draws)], means[int(0.975 * draws) - 1]


def paired_t(diffs: list[float]) -> float:
    """Paired t statistic against zero. The pairing is the whole point."""
    n = len(diffs)
    if n < 2:
        return 0.0
    mean = sum(diffs) / n
    sd = math.sqrt(sum((d - mean) ** 2 for d in diffs) / (n - 1)) or 1e-12
    return mean / (sd / math.sqrt(n))


def main() -> None:
    markets = load_markets()
    print("=" * 84)
    print("EXECUTION TIMING - paired, same market, enter-now versus waiting")
    print("=" * 84)
    if len(markets) < 500:
        print("\nno corpus - run scripts/build_cohort.py first")
        return
    print(f"\n{len(markets):,} unique markets, one row each at the first "
          "decision minute.")

    # ---- 1. the unconditional question ------------------------------------
    print("\n1. DOES WAITING PAY AT ALL?  (paired difference against entering now)")
    print(f"   {'policy':<26}{'n':>7}{'fill':>7}{'mean diff':>12}"
          f"{'95% CI':>24}{'t':>8}")
    for delta in LIMIT_DELTAS:
        rows = [policies(m, delta) for m in markets]
        diffs = [r["limit"] - r["now"] for r in rows]
        filled = sum(1 for r in rows if r["limit_filled"]) / len(rows)
        lo, hi = paired_ci(diffs)
        mean = sum(diffs) / len(diffs)
        print(f"   {f'wait-limit {delta:.0%} below':<26}{len(diffs):>7}"
              f"{filled:>7.0%}{mean:>+12.4f}  [{lo:+.4f}, {hi:+.4f}]"
              f"{paired_t(diffs):>8.1f}")
    rows = [policies(m, 0.02) for m in markets]
    diffs = [r["last"] - r["now"] for r in rows]
    lo, hi = paired_ci(diffs)
    print(f"   {'wait-to-last quote':<26}{len(diffs):>7}{'100%':>7}"
          f"{sum(diffs) / len(diffs):>+12.4f}  [{lo:+.4f}, {hi:+.4f}]"
          f"{paired_t(diffs):>8.1f}")
    oracle = [r["oracle"] - r["now"] for r in rows]
    print(f"   {'wait-ORACLE (unreachable)':<26}{len(oracle):>7}{'--':>7}"
          f"{sum(oracle) / len(oracle):>+12.4f}"
          + "   <- the ceiling on any timing model")

    # ---- 2. can a model pick WHEN to wait? --------------------------------
    print("\n2. CAN A MODEL TELL WHEN WAITING PAYS?")
    print("   Walk-forward. The model predicts P(the ask improves by 2c),")
    print("   and the policy waits only when that probability clears 0.5.")
    delta = 0.02
    labels = [
        1 if (m["best_later"] is not None
              and m["best_later"] <= m["our_ask"] - delta) else 0
        for m in markets
    ]
    fold = len(markets) // (FOLDS + 1)
    selective: list[float] = []
    always_wait: list[float] = []
    hits = shots = 0
    for index in range(1, FOLDS + 1):
        train, test = markets[: fold * index], markets[fold * index : fold * (index + 1)]
        train_y = labels[: fold * index]
        if not test:
            break
        model = B.fit(train, train_y, cutoff_ms=train[-1]["open_ms"] + 900_000)
        if not model:
            continue
        for row in test:
            outcome = policies(row, delta)
            waiting = model.probability(row) >= 0.5
            shots += 1
            hits += int(waiting == bool(
                row["best_later"] is not None
                and row["best_later"] <= row["our_ask"] - delta
            ))
            selective.append(outcome["limit"] - outcome["now"] if waiting else 0.0)
            always_wait.append(outcome["limit"] - outcome["now"])
    if selective:
        lo, hi = paired_ci(selective)
        mean = sum(selective) / len(selective)
        print(f"\n   {'policy':<34}{'n':>7}{'mean vs enter-now':>20}{'95% CI':>24}")
        print(f"   {'model-selected waiting':<34}{len(selective):>7}"
              f"{mean:>+20.4f}  [{lo:+.4f}, {hi:+.4f}]")
        alo, ahi = paired_ci(always_wait)
        print(f"   {'always wait (no model)':<34}{len(always_wait):>7}"
              f"{sum(always_wait) / len(always_wait):>+20.4f}  "
              f"[{alo:+.4f}, {ahi:+.4f}]")
        print(f"\n   retracement called correctly: {hits / max(shots, 1):.1%} "
              f"of {shots:,}")
        edge = [s - a for s, a in zip(selective, always_wait, strict=True)]
        elo, ehi = paired_ci(edge)
        print(f"   model versus always-wait:     {sum(edge) / len(edge):+.4f}"
              f"  [{elo:+.4f}, {ehi:+.4f}]")
        if lo > 0:
            print("\n   => model-selected waiting BEATS entering now, "
                  "out of sample and paired.")
        else:
            print("\n   => model-selected waiting does NOT clear zero against "
                  "entering now.")

    # ---- 3. WHY. the mechanism, not just the verdict ----------------------
    print("\n3. WHY WAITING LOSES - the dip is information, not a discount")
    filled = [m for m in markets
              if m["best_later"] is not None
              and m["best_later"] <= m["our_ask"] - delta]
    missed = [m for m in markets if m not in filled] if len(markets) < 1 else [
        m for m in markets
        if not (m["best_later"] is not None
                and m["best_later"] <= m["our_ask"] - delta)
    ]
    print(f"   {'group':<36}{'n':>7}{'win rate':>10}{'avg ask':>9}")
    for label, group in (
        (f"limit FILLED (ask dipped {delta:.0%})", filled),
        ("limit MISSED (ask never dipped)", missed),
        ("all markets", markets),
    ):
        if not group:
            continue
        print(f"   {label:<36}{len(group):>7}"
              f"{sum(r['won'] for r in group) / len(group):>10.1%}"
              f"{sum(r['our_ask'] for r in group) / len(group):>9.2f}")
    if filled and missed:
        gap = (sum(r["won"] for r in missed) / len(missed)
               - sum(r["won"] for r in filled) / len(filled))
        print(f"\n   A {delta:.0%} dip costs {gap:.1%} of win rate. You save "
              f"{delta:.0%} on the price and\n   give up {gap:.1%} of a dollar "
              f"payout: {delta - gap:+.4f}/contract before fees.")
        print("\n   The dip is not a discount, it is the market telling you the")
        print("   trade is going wrong. A resting buy fills PRECISELY when you")
        print("   would rather it had not - the same adverse selection that")
        print("   killed maker orders in section 31.")
        print("\n   The mirror image is the useful half: a market whose ask")
        print(f"   never dips wins {sum(r['won'] for r in missed) / len(missed):.1%} "
              "of the time. Not tradeable at entry -")
        print("   it is only known in hindsight - but it is the strongest")
        print("   single signal in the archive, and it belongs to LIFECYCLE")
        print("   management: once held, a position that has not dipped is")
        print("   winning, and one that has is probably lost.")
    print("=" * 84)


if __name__ == "__main__":
    main()
