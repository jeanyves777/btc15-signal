"""The meta-label: given that the rule says YES, should we actually execute?

Sections 36 and 37 closed the two obvious questions - no model beats the ask on
direction, and every implementable wait policy loses seven cents. This is the
operator's reframing, and it is a genuinely different question:

  "The base strategy says this trade qualifies. Do historically similar
   QUALIFIED setups show enough loss risk to skip it?"

The model never predicts UP or DOWN. It predicts, among signals the deployed
rule already approved, which ones lose. That is a secondary filter over a
narrow, homogeneous population, not a competitor to Kalshi's price.

WHY THE ASYMMETRY MAKES THIS WINNABLE WHERE DIRECTION WAS NOT. At 80c a
correct veto saves the whole 80c stake; a false veto costs only the 20c profit
forgone. Four false vetoes are paid for by one correct one. Formally, a veto is
worth taking whenever the vetoed subset's win rate falls below its PRICE - so
the model does not need to be right, it needs to find a pocket where the
favourite-longshot edge (section 1) reverses.

That is also the honest warning attached to this whole script: the qualified
population has POSITIVE edge by construction, so vetoing at random LOSES money
in proportion to how much you veto. The random-veto control is therefore not a
formality, it is the thing the model has to beat.

MEASURED IN DOLLARS, NOT ACCURACY:

    veto value = losses avoided - profits missed from false vetoes

    python scripts/measure_meta_label.py
"""

import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from measure_execution_timing import load_markets  # noqa: E402

from btc15_signal import baseline as B  # noqa: E402
from btc15_signal.config import Settings  # noqa: E402
from btc15_signal.strategy import EntryRule  # noqa: E402
from btc15_signal.validation import kalshi_fee_charged as fee  # noqa: E402

FOLDS = 5
THRESHOLDS = (0.20, 0.25, 0.30, 0.35, 0.40, 0.50)


def qualifies(row: dict, rule: EntryRule) -> bool:
    """The DEPLOYED rule's gates, on the fields the corpus carries.

    `min_raw_probability` is not applied: the corpus has no model score, and
    inventing one would be training on a feature the live path computes
    differently. The three gates that ARE applied - band, distance, momentum -
    are the ones that decide almost every live refusal.
    """
    if not rule.min_ask <= row["our_ask"] <= rule.max_ask:
        return False
    if abs(row["normalized_distance"]) < rule.min_normalized_distance:
        return False
    direction = 1 if row["side"] == "UP" else -1
    if direction * row["momentum_5m_bps"] < rule.min_momentum_bps:
        return False
    return True


def outcome(row: dict) -> tuple[float, float]:
    """(what a win pays, what a loss costs) for one contract, net of the fee."""
    ask = row["our_ask"]
    charged = fee(ask, 1)
    return 1.0 - ask - charged, ask + charged


def veto_value(vetoed: list[dict]) -> tuple[float, float, float]:
    """(value, losses avoided, profits missed). Dollars, not accuracy."""
    avoided = missed = 0.0
    for row in vetoed:
        win_pays, loss_costs = outcome(row)
        if row["won"]:
            missed += win_pays        # a false veto: we gave up this profit
        else:
            avoided += loss_costs     # a correct veto: we kept this stake
    return avoided - missed, avoided, missed


def bootstrap(values: list[float], draws: int = 2000, seed: int = 13):
    if len(values) < 2:
        return 0.0, 0.0
    rng = random.Random(seed)
    totals = sorted(
        sum(rng.choice(values) for _ in values) for _ in range(draws)
    )
    return totals[int(0.025 * draws)], totals[int(0.975 * draws) - 1]


def per_decision(vetoed: list[dict]) -> list[float]:
    """The veto's value on each individual decision, for an interval."""
    out = []
    for row in vetoed:
        win_pays, loss_costs = outcome(row)
        out.append(-win_pays if row["won"] else loss_costs)
    return out


def live_qualified(database: str) -> list[dict]:
    """The signals this bot actually qualified, with what they really cost.

    The corpus is reconstructed history; this is the account. Where a market
    settled on the exchange, `settlements.pnl` is used verbatim rather than
    recomputed - the same rule as section 34, money is read, never modelled -
    and the fee is the one Kalshi charged.

    Features come from the observation the rule acted on, so nothing here knows
    anything the live decision did not.
    """
    import sqlite3

    db = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    rows = []
    for pred in db.execute(
        "SELECT * FROM predictions WHERE won IS NOT NULL AND qualified=1 "
        "ORDER BY window_open"
    ):
        obs = db.execute(
            "SELECT * FROM observations WHERE window_open=? AND rule_match=1 "
            "ORDER BY remaining_s DESC LIMIT 1",
            (pred["window_open"],),
        ).fetchone()
        if obs is None or obs["normalized_distance"] is None:
            continue          # no decision-time features: excluded, not guessed
        ask = pred["contract_price"] or obs["our_ask"]
        if not ask:
            continue
        settled = db.execute(
            "SELECT pnl, fee_cost FROM settlements WHERE ticker=?",
            (obs["ticker"],),
        ).fetchone()
        rows.append({
            "ticker": obs["ticker"], "open_ms": pred["window_open"],
            "our_ask": ask, "won": int(pred["won"]), "side": pred["side"],
            "normalized_distance": obs["normalized_distance"],
            "remaining_s": obs["remaining_s"],
            "momentum_5m_bps": obs["momentum_5m_bps"] or 0.0,
            "volatility_5m_bps": obs["volatility_5m_bps"] or 0.0,
            "spread_bps": obs["spread_bps"] or 0.0,
            "band_held_s": 0.0,
            "session": obs["session"], "vol_regime": obs["vol_regime"],
            # Real money where the exchange reported it.
            "realised_pnl": settled["pnl"] if settled else None,
            "real_fee": settled["fee_cost"] if settled else None,
            "executed": settled is not None,
        })
    return rows


def report_live(database: str) -> None:
    """The same veto arithmetic, on the account's own qualified signals."""
    rows = live_qualified(database)
    print("\n" + "=" * 84)
    print("THE LIVE RECORD - signals this bot qualified, graded on real money")
    print("=" * 84)
    if not rows:
        print("\n  no qualified live signals with decision-time features yet")
        return
    executed = [r for r in rows if r["executed"]]
    print(f"\n  qualified live signals        {len(rows)}")
    print(f"  of those actually executed    {len(executed)}")
    wins = sum(r["won"] for r in rows)
    print(f"  win rate                      {wins / len(rows):.1%}")
    print(f"  average ask                   "
          f"{sum(r['our_ask'] for r in rows) / len(rows):.2f}")

    # What a veto would have been worth on the live set, at every threshold a
    # perfect oracle could pick - the CEILING, since no model is applied here.
    losers = [r for r in rows if not r["won"]]
    winners = [r for r in rows if r["won"]]
    avoided = sum(outcome(r)[1] for r in losers)
    missed_all = sum(outcome(r)[0] for r in winners)
    print(f"\n  a PERFECT veto (blocks every loser, never a winner)")
    print(f"    losses avoided              {avoided:+.2f} over {len(losers)}")
    print(f"    that is the entire ceiling for any veto model on this sample")
    print(f"  vetoing EVERYTHING instead")
    print(f"    profits missed              {-missed_all:+.2f} over {len(winners)}")
    print(f"    net                         {avoided - missed_all:+.2f}")
    if avoided - missed_all < 0:
        print("\n  => Blocking indiscriminately loses money here, as it must "
              "while the\n     qualified population wins more often than its "
              "price. A veto only\n     pays if it can find the losers "
              "specifically.")
    if executed:
        real = sum(r["realised_pnl"] for r in executed
                   if r["realised_pnl"] is not None)
        print(f"\n  realised P&L on the executed subset, from the exchange: "
              f"{real:+.2f}")
    print(f"\n  {len(rows)} qualified signals is far short of what a veto "
          "model needs; this\n  section exists so the live number and the "
          "corpus number are read together,\n  not so the live number can "
          "carry a decision on its own.")


def main() -> None:
    rule = EntryRule.load("strategy.json")
    markets = load_markets()
    if len(markets) < 500:
        print("no corpus - run scripts/build_cohort.py first")
        return

    qualified = [m for m in markets if qualifies(m, rule)]
    print("=" * 84)
    print("META-LABEL - should an ALREADY-QUALIFIED signal actually execute?")
    print("=" * 84)
    print(f"\n{len(markets):,} markets, {len(qualified):,} qualify under the "
          f"deployed rule\n  (band {rule.min_ask:.0%}-{rule.max_ask:.0%}, "
          f"distance >={rule.min_normalized_distance:.1f}x, "
          f"momentum >={rule.min_momentum_bps:.0f})")
    if len(qualified) < 300:
        print("too few qualified markets to measure")
        return

    base_win = sum(m["won"] for m in qualified) / len(qualified)
    base_ask = sum(m["our_ask"] for m in qualified) / len(qualified)
    total_edge = sum(
        (outcome(m)[0] if m["won"] else -outcome(m)[1]) for m in qualified
    )
    print(f"\n  qualified win rate  {base_win:.1%}")
    print(f"  average ask         {base_ask:.2f}")
    print(f"  edge over the price {base_win - base_ask:+.3f}  "
          "<- positive, which is the problem")
    print(f"  total P&L if all taken  {total_edge:+.2f} over "
          f"{len(qualified):,} contracts")
    print("\n  A veto only pays where the vetoed subset wins LESS than its "
          "price.\n  Vetoing at random destroys value in proportion to how "
          "much is vetoed,\n  so the random control below is the bar, not a "
          "formality.")

    # ---- walk-forward meta-model ------------------------------------------
    labels = [1 - m["won"] for m in qualified]        # predict the LOSS
    fold = len(qualified) // (FOLDS + 1)
    scored: list[tuple[float, dict]] = []
    for index in range(1, FOLDS + 1):
        train = qualified[: fold * index]
        test = qualified[fold * index : fold * (index + 1)]
        if not test:
            break
        model = B.fit(
            train, labels[: fold * index],
            cutoff_ms=train[-1]["open_ms"] + 900_000,
        )
        if not model:
            continue
        for row in test:
            scored.append((model.probability(row), row))
    if not scored:
        print("\nno out-of-sample scores")
        return

    print(f"\nWALK-FORWARD, {len(scored):,} out-of-sample qualified signals")
    print(f"  {'veto when P(loss) >=':<22}{'vetoed':>8}{'of':>7}"
          f"{'win% vetoed':>13}{'avoided':>10}{'missed':>9}"
          f"{'VETO VALUE':>12}{'95% CI':>22}")
    rng = random.Random(29)
    for threshold in THRESHOLDS:
        vetoed = [row for p, row in scored if p >= threshold]
        if not vetoed:
            print(f"  {f'{threshold:.0%}':<22}{'0':>8}")
            continue
        value, avoided, missed = veto_value(vetoed)
        lo, hi = bootstrap(per_decision(vetoed))
        win_rate = sum(r["won"] for r in vetoed) / len(vetoed)
        print(f"  {f'{threshold:.0%}':<22}{len(vetoed):>8}{len(scored):>7}"
              f"{win_rate:>13.1%}{avoided:>10.2f}{missed:>9.2f}"
              f"{value:>+12.2f}  [{lo:+.2f}, {hi:+.2f}]")

    # ---- the control that matters -----------------------------------------
    print("\nRANDOM VETO CONTROL - the same COUNT, chosen at random")
    print("  (a veto that carries no information destroys the positive edge it "
          "removes)")
    print(f"  {'vetoed':<10}{'random veto value':>20}{'model veto value':>20}"
          f"{'difference':>14}")
    for threshold in (0.25, 0.35, 0.50):
        vetoed = [row for p, row in scored if p >= threshold]
        if not vetoed:
            continue
        model_value, _, _ = veto_value(vetoed)
        randoms = []
        for _ in range(200):
            sample = rng.sample([row for _p, row in scored], len(vetoed))
            randoms.append(veto_value(sample)[0])
        random_mean = sum(randoms) / len(randoms)
        print(f"  {len(vetoed):<10}{random_mean:>+20.2f}{model_value:>+20.2f}"
              f"{model_value - random_mean:>+14.2f}")

    # ---- what the model actually separates --------------------------------
    print("\nWHAT THE META-MODEL SEES  (deciles of predicted loss risk)")
    scored.sort(key=lambda pair: pair[0])
    size = max(len(scored) // 10, 1)
    print(f"  {'decile':<9}{'n':>7}{'mean P(loss)':>14}{'actual loss%':>14}"
          f"{'avg ask':>9}{'edge vs price':>15}")
    for decile in range(10):
        chunk = scored[decile * size : (decile + 1) * size]
        if not chunk:
            continue
        rows = [row for _p, row in chunk]
        predicted = sum(p for p, _r in chunk) / len(chunk)
        actual = 1 - sum(r["won"] for r in rows) / len(rows)
        ask = sum(r["our_ask"] for r in rows) / len(rows)
        edge = (1 - actual) - ask
        print(f"  {decile + 1:<9}{len(rows):>7}{predicted:>14.1%}"
              f"{actual:>14.1%}{ask:>9.2f}{edge:>+15.3f}")
    print("\n  A decile whose edge is NEGATIVE is one worth vetoing. If every "
          "decile is\n  positive, the rule's qualified population has no loss "
          "pocket to find.")

    # ---- base versus base+veto, as equity curves --------------------------
    print("\nBASE STRATEGY versus BASE + VETO  (same signals, same order)")
    print(f"  {'policy':<30}{'trades':>8}{'P&L':>10}{'max DD':>10}"
          f"{'win%':>8}{'P&L/trade':>12}")
    ordered = sorted(scored, key=lambda pair: pair[1]["open_ms"])
    for label, threshold in (
        ("base strategy alone", None),
        ("base + veto at P(loss)>=20%", 0.20),
        ("base + veto at P(loss)>=25%", 0.25),
        ("base + veto at P(loss)>=30%", 0.30),
    ):
        taken = [
            row for p, row in ordered
            if threshold is None or p < threshold
        ]
        if not taken:
            continue
        pnl = [
            (outcome(r)[0] if r["won"] else -outcome(r)[1]) for r in taken
        ]
        equity = peak = trough = 0.0
        for value in pnl:
            equity += value
            peak = max(peak, equity)
            trough = min(trough, equity - peak)
        print(f"  {label:<30}{len(taken):>8}{sum(pnl):>+10.2f}{trough:>+10.2f}"
              f"{sum(r['won'] for r in taken) / len(taken):>8.1%}"
              f"{sum(pnl) / len(pnl):>+12.4f}")

    # ---- where the veto helps, if anywhere --------------------------------
    print("\nVETO VALUE BY SESSION AND REGIME  (threshold 25%)")
    vetoed = [row for p, row in scored if p >= 0.25]
    for field, title in (("session", "session"), ("vol_regime", "regime")):
        groups: dict[str, list[dict]] = {}
        for row in vetoed:
            groups.setdefault(row.get(field) or "unknown", []).append(row)
        if not groups:
            continue
        print(f"  by {title}:")
        print(f"    {'':<12}{'vetoed':>8}{'win%':>8}{'avg ask':>9}"
              f"{'veto value':>13}")
        for key in sorted(groups):
            group = groups[key]
            value, _a, _m = veto_value(group)
            print(f"    {key:<12}{len(group):>8}"
                  f"{sum(r['won'] for r in group) / len(group):>8.1%}"
                  f"{sum(r['our_ask'] for r in group) / len(group):>9.2f}"
                  f"{value:>+13.2f}")
    print("\n  A veto worth deploying should not depend on one session or one "
          "regime\n  carrying it; a single positive cell among several "
          "negative ones is the\n  shape a search over noise produces "
          "(section 7).")
    print("=" * 84)

    report_live(Settings().database_path)


if __name__ == "__main__":
    main()
