"""The readiness report: should the intelligence layer be promoted out of SHADOW?

Answers one question - REMAIN SHADOW / READY FOR ASSIST REVIEW / READY FOR LIVE
REVIEW - and shows the evidence for it. It never changes the mode: promotion is
a person's decision, taken having read this, and needs `intelligence_mode` AND
`intelligence_authorised` set together by hand.

WIN RATE IS NOT ONE OF THE CRITERIA, deliberately. These contracts are bought
at 70-93c, so a model right 85% of the time and a price right 85% of the time
produce exactly zero edge between them. What is measured instead is
calibration, net edge after real fees and slippage, and whether the model's
DISAGREEMENTS with the deployed rule did better than the rule - because
agreeing with the rule more often is worth nothing.

Two models are compared on the same corpus, walk-forward:

  * the deployed regime-conditioned nearest-neighbour read (`similar.py`)
  * a calibrated logistic regression (`baseline.py`)

The selection rule is the spec's: prefer the simpler model when the two are
statistically indistinguishable. A retrieval layer over 6,400 markets is not
simpler than eight coefficients, so the cohort read has to earn the difference.

    python scripts/promotion_report.py
"""

import random
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from btc15_signal import baseline as B  # noqa: E402
from btc15_signal.config import Settings  # noqa: E402
from btc15_signal.intel_mode import resolve  # noqa: E402
from btc15_signal.similar import Cohorts, Fingerprint  # noqa: E402
from btc15_signal.grading import drawdown, executable_pnl  # noqa: E402

FOLDS = 5
MIN_SETTLED_DECISIONS = 200   # the documented power floor; see power_note()


def power_note(edge: float, spread: float) -> tuple[int, str]:
    """How many settled decisions are needed to call `edge` real.

    Two-sided, 80% power, 5% significance: n = (1.96 + 0.84)^2 * sd^2 / edge^2.
    Stated as a number rather than a feeling, because "we need more data" is
    the easiest sentence in the world to say and the hardest to act on.
    """
    if edge <= 0:
        return 0, "no positive edge to power a test for"
    needed = int((1.96 + 0.84) ** 2 * (spread ** 2) / (edge ** 2)) + 1
    return needed, (
        f"n = (1.96+0.84)^2 x {spread:.4f}^2 / {edge:.4f}^2 = {needed:,} "
        "settled decisions for 80% power at 5%"
    )


def ci(values: list[float], draws: int = 2000, seed: int = 7) -> tuple[float, float]:
    if len(values) < 2:
        return 0.0, 0.0
    rng = random.Random(seed)
    means = sorted(
        sum(rng.choice(values) for _ in values) / len(values) for _ in range(draws)
    )
    return means[int(0.025 * draws)], means[int(0.975 * draws) - 1]


def walk_forward_corpus() -> dict:
    """Both models over `data/cohort.db`, expanding-window walk-forward.

    ONE ROW PER MARKET before anything else. The corpus holds 74,582 decision
    minutes across 6,428 markets, and a market that sat in the band for many
    minutes is disproportionately one that went on to win - so training on raw
    rows both leaks lifecycle structure and tilts the base rate upward.
    """
    path = Path("data/cohort.db")
    if not path.exists():
        return {}
    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    seen: set[str] = set()
    markets = []
    for row in db.execute("SELECT * FROM cohort ORDER BY open_ms, remaining DESC"):
        if row["ticker"] in seen:
            continue
        seen.add(row["ticker"])
        markets.append({
            "ticker": row["ticker"], "open_ms": row["open_ms"],
            "our_ask": row["ask"], "side": "UP" if row["side_is_up"] else "DOWN",
            "normalized_distance": row["normalized_distance"],
            "remaining_s": row["remaining"] * 60,
            "momentum_5m_bps": row["momentum_bps"],
            "volatility_5m_bps": row["volatility_bps"],
            "spread_bps": 0.0, "band_held_s": 0.0,
            "session": row["session"], "vol_regime": row["vol_regime"],
            "won": int(row["won"]),
        })
    if len(markets) < 500:
        return {}

    cohorts = Cohorts()
    fold = len(markets) // (FOLDS + 1)
    lr_p: list[float] = []
    knn_p: list[float] = []
    knn_y: list[int] = []
    lr_y: list[int] = []
    price_p: list[float] = []
    knn_n = 0
    for index in range(1, FOLDS + 1):
        train = markets[: fold * index]
        test = markets[fold * index : fold * (index + 1)]
        if not test:
            break
        cutoff = train[-1]["open_ms"] + 900_000
        model = B.fit(train, [m["won"] for m in train], cutoff_ms=cutoff)
        for row in test:
            if model:
                lr_p.append(model.probability(row))
                lr_y.append(row["won"])
                price_p.append(row["our_ask"])
            # The cohort read, restricted to markets settled before this one -
            # the same `as_of_ms` the live path passes.
            read = cohorts.read(
                Fingerprint(
                    remaining_s=row["remaining_s"], ask=row["our_ask"],
                    normalized_distance=row["normalized_distance"],
                    volatility_bps=row["volatility_5m_bps"],
                    momentum_bps=row["momentum_5m_bps"],
                    session=row["session"], vol_regime=row["vol_regime"],
                    side_is_up=row["side"] == "UP",
                ),
                as_of_ms=row["open_ms"],
            )
            if read:
                knn_p.append(read.win_probability)
                knn_y.append(row["won"])
                knn_n += 1
    return {
        "markets": len(markets), "lr_p": lr_p, "lr_y": lr_y,
        "knn_p": knn_p, "knn_y": knn_y, "price_p": price_p, "knn_n": knn_n,
    }


def _looks_numeric(value) -> bool:
    """A session column holding "0.6638" is a shifted row, not a session."""
    try:
        float(value)
    except (TypeError, ValueError):
        return False
    return True


def live_shadow(store_path: str) -> dict:
    """What the layer actually recommended live, graded against settlement."""
    db = sqlite3.connect(f"file:{store_path}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    try:
        rows = [dict(r) for r in db.execute(
            "SELECT * FROM shadow_decisions WHERE won IS NOT NULL"
        )]
    except sqlite3.OperationalError:
        return {}
    # QUARANTINE THE COLUMN-SHIFTED LEGACY ROWS.
    #
    # `record_shadow_decision` used to write `VALUES (?,?,...)` positionally
    # against a hand-counted width. When `dip_n` was appended to the live table
    # by a migration, the tuple order stopped matching the table order and
    # every value from that point shifted: `won` ended up holding the session
    # string, `session` a probability, `dip_n` the volatility regime. Three of
    # 98 rows are affected. They are excluded rather than repaired - the spec's
    # rule for legacy rows without stable lifecycle linkage - and counted, so
    # the exclusion is visible rather than a silently smaller sample.
    def intact(row: dict) -> bool:
        return row.get("won") in (0, 1, None) and isinstance(
            row.get("session"), (str, type(None))
        ) and not _looks_numeric(row.get("session"))

    corrupt = [r for r in rows if not intact(r)]
    rows = [r for r in rows if intact(r)]
    graded = [r for r in rows if r.get("win_probability") is not None]
    # Agreement is worthless. Only the windows where the model and the rule
    # wanted different things can tell you which one to trust.
    disagree = [
        r for r in graded
        if bool(r.get("rule_qualified")) != (r.get("action") == "ENTER NOW")
    ]
    return {"graded": graded, "disagree": disagree, "corrupt": len(corrupt)}


def main() -> None:
    settings = Settings()
    mode, why = resolve(settings.intelligence_mode, settings.intelligence_authorised)
    print("=" * 78)
    print("INTELLIGENCE PROMOTION REPORT")
    print("=" * 78)
    print(f"\ncurrent mode: {mode.upper()}  ({why})")

    blockers: list[str] = []

    # ---- 1. walk-forward model comparison ---------------------------------
    print("\n" + "-" * 78)
    print("1. WALK-FORWARD, OUT OF SAMPLE  (expanding window, corpus deduplicated)")
    wf = walk_forward_corpus()
    if not wf:
        print("   no corpus - run scripts/build_cohort.py first")
        blockers.append("no walk-forward evidence")
    else:
        print(f"   {wf['markets']:,} unique markets, {FOLDS} folds")
        print(f"   {'model':<28}{'n':>7}{'Brier':>9}{'LogLoss':>10}")
        if wf["lr_p"]:
            print(f"   {'logistic baseline':<28}{len(wf['lr_p']):>7}"
                  f"{B.brier(wf['lr_p'], wf['lr_y']):>9.4f}"
                  f"{B.log_loss(wf['lr_p'], wf['lr_y']):>10.4f}")
            print(f"   {'the market price itself':<28}{len(wf['price_p']):>7}"
                  f"{B.brier(wf['price_p'], wf['lr_y']):>9.4f}"
                  f"{B.log_loss(wf['price_p'], wf['lr_y']):>10.4f}")
        if wf["knn_p"]:
            print(f"   {'cohort nearest-neighbour':<28}{len(wf['knn_p']):>7}"
                  f"{B.brier(wf['knn_p'], wf['knn_y']):>9.4f}"
                  f"{B.log_loss(wf['knn_p'], wf['knn_y']):>10.4f}")
            print("\n   cohort calibration (predicted -> observed, n):")
            for pm, ob, n in B.reliability(wf["knn_p"], wf["knn_y"]):
                flag = "" if abs(pm - ob) < 0.05 else "   <- off"
                print(f"     {pm:.2f} -> {ob:.2f}  n={n}{flag}")
        else:
            print("   cohort read returned nothing in walk-forward")
            blockers.append("cohort produced no out-of-sample predictions")

        # THE SELECTION RULE. Simpler wins ties.
        if wf["lr_p"] and wf["knn_p"]:
            lr_b = B.brier(wf["lr_p"], wf["lr_y"])
            knn_b = B.brier(wf["knn_p"], wf["knn_y"])
            price_b = B.brier(wf["price_p"], wf["lr_y"])
            print(f"\n   cohort Brier {knn_b:.4f} vs logistic {lr_b:.4f} "
                  f"vs price {price_b:.4f}")
            if knn_b >= price_b and lr_b >= price_b:
                print("   => NEITHER model beats the price. The price is already "
                      "the best available\n      estimate of the outcome, which "
                      "is what the favourite-longshot bias\n      in FINDINGS 1 "
                      "predicts.")
                blockers.append("no model beats the market price out of sample")
            elif knn_b > lr_b - 0.002:
                print("   => the cohort read does NOT beat the simpler model; "
                      "prefer the logistic")
                blockers.append("cohort does not beat the logistic baseline")

    # ---- 2. the live shadow record ----------------------------------------
    print("\n" + "-" * 78)
    print("2. LIVE SHADOW RECORD  (what it recommended, graded at settlement)")
    live = live_shadow(settings.database_path)
    graded = live.get("graded", [])
    disagree = live.get("disagree", [])
    if live.get("corrupt"):
        print(f"   EXCLUDED {live['corrupt']} column-shifted legacy row(s) "
              "- see record_shadow_decision")
    print(f"   graded decisions        {len(graded)}")
    print(f"   model/rule disagreements {len(disagree)}")
    if graded:
        probs = [float(r["win_probability"]) for r in graded]
        outs = [int(r["won"]) for r in graded]
        print(f"   Brier on live decisions {B.brier(probs, outs):.4f}")
        pnl = executable_pnl(graded)
        if pnl:
            lo, hi = ci(pnl)
            mean = sum(pnl) / len(pnl)
            equity = peak = trough = 0.0
            for value in pnl:
                equity += value
                peak = max(peak, equity)
                trough = min(trough, equity - peak)
            print(f"   executable net edge     {mean:+.4f}/contract "
                  f"over {len(pnl)}  95% CI [{lo:+.4f}, {hi:+.4f}]")
            print(f"   max drawdown            {trough:+.2f}")
            if lo <= 0:
                blockers.append("live executable edge does not clear zero")
            spread = (
                sum((v - mean) ** 2 for v in pnl) / max(len(pnl) - 1, 1)
            ) ** 0.5
            # The observed edge, NOT a clamped stand-in for it. Feeding
            # `max(mean, 1e-9)` in when the edge is negative divided by
            # effectively zero and printed a requirement of 2 x 10^18 settled
            # decisions, which is not a large number, it is a broken one.
            needed, note = power_note(mean, spread or 0.35)
            print(f"   power                   {note}")
            floor = max(needed, MIN_SETTLED_DECISIONS) if needed else MIN_SETTLED_DECISIONS
            if len(pnl) < floor:
                blockers.append(
                    f"{len(pnl)} settled ENTER NOW decisions; need {floor:,}"
                )
        else:
            print("   executable net edge     no filled ENTER NOW decisions yet")
            blockers.append("no executable live decisions")
    else:
        print("   nothing graded yet")
        blockers.append("no graded live decisions")

    if not disagree:
        print("\n   NO DISAGREEMENTS. A model that never differs from the rule "
              "cannot be shown\n   to be better than it, however well it scores "
              "- agreeing is free.")
        blockers.append("no model/rule disagreements to judge")

    # ---- 3. leakage -------------------------------------------------------
    print("\n" + "-" * 78)
    print("3. ANTI-LEAKAGE")
    print("   walk-forward `as_of_ms` on every retrieval    enforced in similar.py")
    print("   one row per market before training            enforced")
    print("   standardiser fitted on training fold only     enforced in baseline.py")
    print("   feature schema pinned and checked on load     "
          f"{B.SCHEMA}")
    print("   covered by tests/test_intelligence.py")

    # ---- verdict ----------------------------------------------------------
    print("\n" + "=" * 78)
    if blockers:
        print("RECOMMENDATION:  REMAIN SHADOW")
        print("\nblocking:")
        for item in blockers:
            print(f"  - {item}")
    else:
        print("RECOMMENDATION:  READY FOR ASSIST REVIEW")
        print("\n  Every criterion passed. Promotion is still a person's "
              "decision: set\n  intelligence_mode=assist AND "
              "intelligence_authorised=true by hand.")
    print("=" * 78)


if __name__ == "__main__":
    main()
