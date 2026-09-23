"""What are we REJECTING that wins, and is HIGH confidence earning its label?

The operator's correction, and it is the right one: the intelligence was never
meant to compete with the price on direction (section 36) or on entry timing
(section 37). Both were measured and both are closed. The question it was
always for is the other side of the ledger:

    "During the Asian session we saw fifteen wins straight. Why? Is there a
     regime making that win, and were we rejecting those signals blindly?
     And this pattern has been LOSING even at HIGH confidence - so demote it."

Two measurements, neither of which has been run:

  1. REJECTED SIGNALS. For every signal the rule refused, what would it have
     netted? Grouped by session and by the gate that refused it. A gate that
     is right on average can still be wrong inside one regime, and that is a
     pocket worth having.

  2. CONFIDENCE CALIBRATION. Does HIGH actually beat MEDIUM, per session? A
     label that does not sort outcomes is worse than no label, because it is
     acted on.

NET, NOT WIN RATE. A rejected signal at 96c that wins 81% of the time loses
money. Every figure here is dollars per contract after the fee Kalshi charges.

DEDUPED PER WINDOW. `observations` holds a row per poll, so a market that sat
in a state for ten minutes appears ~50 times and would otherwise dominate its
own cohort. One row per window, the first that matches the cohort.

    python scripts/measure_rejections.py
"""

import random
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from btc15_signal.config import Settings  # noqa: E402
from btc15_signal.validation import kalshi_fee_charged as fee  # noqa: E402

SESSIONS = ("asia", "europe", "us", "late-us")


def net(ask: float, won: int) -> float:
    return (1.0 if won else 0.0) - ask - fee(ask, 1)


def ci(values: list[float], draws: int = 2000, seed: int = 11):
    if len(values) < 2:
        return 0.0, 0.0
    rng = random.Random(seed)
    means = sorted(
        sum(rng.choice(values) for _ in values) / len(values) for _ in range(draws)
    )
    return means[int(0.025 * draws)], means[int(0.975 * draws) - 1]


def load(settings: Settings) -> list[dict]:
    """One row per corpus market, at its first decision minute.

    THE LIVE ARCHIVE IS TOO SMALL FOR THIS. It holds ~143 graded windows
    inside the entry window - enough to notice something, nowhere near enough
    to act on it. The 6,435-market corpus carries the same features, so the
    rule's gates can be recomputed on it and the rejections counted properly.

    Deduplicated to the FIRST decision minute, for the reason section 36 gives:
    a market that sat in a state for many minutes is disproportionately one
    that went on to win, so keeping every minute both duplicates the market
    and tilts the base rate.
    """
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from compare_series import load as load_corpus

    from btc15_signal.features import build_snapshots
    from btc15_signal.strategy import EntryRule

    rule = EntryRule.load(Path("strategy.json"))
    markets, klines, candles = load_corpus("data/market_data.db")
    by_market: dict[str, list] = {}
    for snap in build_snapshots(markets, klines, candles):
        by_market.setdefault(snap.ticker, []).append(snap)

    from_min = settings.entry_from_seconds / 60.0
    to_min = settings.entry_to_seconds / 60.0
    rows = []
    for ticker, snaps in by_market.items():
        snaps.sort(key=lambda s: -s.remaining)
        if snaps[0].result not in ("yes", "no"):
            continue
        for snap in snaps:
            if not to_min <= snap.remaining <= from_min:
                continue
            # The live convention (section 13): the side is where BTC sits
            # relative to the strike, not whichever contract is dearer.
            side = "UP" if snap.signed_distance_bps >= 0 else "DOWN"
            ask = snap.entry_price(side)
            if ask is None or not 0 < ask < 1:
                continue
            won = snap.won(side)
            if won is None:
                continue
            # Recompute the DEPLOYED gates, so a refusal here is the refusal
            # the bot actually makes.
            gates = []
            if not rule.min_ask <= ask <= rule.max_ask:
                gates.append("contract price band")
            if abs(snap.normalized_distance) < rule.min_normalized_distance:
                gates.append("target distance")
            direction = 1 if side == "UP" else -1
            if direction * snap.momentum_5m_bps < rule.min_momentum_bps:
                gates.append("momentum strength")
            rows.append({
                "window_open": snap.open_ms, "our_ask": ask, "won": int(won),
                "rule_match": 0 if gates else 1,
                "failed_gates": ", ".join(gates) or None,
                "session": snap.session, "vol_regime": snap.vol_regime,
                "normalized_distance": snap.normalized_distance,
                "momentum_5m_bps": snap.momentum_5m_bps,
                "raw_probability": None, "side": side,
                "remaining_s": int(snap.remaining * 60),
            })
            break
    return rows


def table(title: str, groups: dict[str, list[float]], minimum: int = 25) -> None:
    print(f"\n{title}")
    print(f"  {'cohort':<34}{'n':>6}{'net/ct':>10}{'95% CI':>22}{'total':>9}")
    for name, values in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        if len(values) < minimum:
            continue
        mean = sum(values) / len(values)
        low, high = ci(values)
        flag = "  <-- POSITIVE" if low > 0 else ""
        print(f"  {name[:34]:<34}{len(values):>6}{mean:>+10.4f}"
              f"  [{low:>+8.4f},{high:>+8.4f}]{sum(values):>+9.2f}{flag}")


def bandit_report(rows: list[dict]) -> None:
    """The contextual-bandit view: arms, evidence strength, proposals."""
    from btc15_signal.adaptive import (
        build_arms,
        proposals,
        score_admitting,
        score_blocking,
    )

    def reward(row):
        # Rejections are priced at the ask we RECORDED, plus the entry
        # slippage a real crossing pays. It is still a simulated fill, and is
        # labelled as one - FINDINGS 37 measured a 45-point win-rate gap
        # between quotes that filled and quotes that merely existed.
        settings = Settings()
        ask = row["our_ask"]
        if not row.get("rule_match"):
            ask = min(0.99, ask + settings.entry_slippage)
        return net(ask, row["won"])

    arms = build_arms(rows, reward)
    print(f"\n5. CONTEXTUAL ARMS: {len(arms)} (context x action) cells")
    strong = [a for a in arms.values() if a.n >= 40]
    print(f"   {len(strong)} with n>=40, the minimum this will reason about")

    found = proposals(arms)
    material = [p for p in found if p.strength != "neutral"]
    print(f"\n6. PROPOSALS: {len(material)} of {len(found)} clear the evidence bar")
    for p in found[:12]:
        print(f"   {p.line()}")

    # The scorecard for the single most-promising admit, whatever it is.
    admits = [p for p in found if p.kind == "execution"]
    if admits:
        best = admits[0]
        ctx = best.context
        card = score_admitting(
            rows,
            lambda r, c=ctx: __import__(
                "btc15_signal.adaptive", fromlist=["context_of"]
            ).context_of(r) == c,
            reward,
        )
        print(f"\n7. SCORECARD for admitting {ctx}")
        print(card.render())
        print("   (simulated fills - a rejected winner is evidence, not proof "
              "a fill was available)")

    losers = [p for p in found if p.kind == "confidence" and p.direction == "lower"]
    if losers:
        ctx = losers[0].context
        card = score_blocking(
            rows,
            lambda r, c=ctx: __import__(
                "btc15_signal.adaptive", fromlist=["context_of"]
            ).context_of(r) == c,
            reward,
        )
        print(f"\n8. SCORECARD for blocking {ctx}")
        print(card.render())


def main() -> None:
    settings = Settings()
    rows = load(settings)
    if not rows:
        print("no graded observations in the entry window")
        return
    taken = [r for r in rows if r["rule_match"]]
    rejected = [r for r in rows if not r["rule_match"]]
    print(f"windows: {len(rows)}   qualified: {len(taken)}   rejected: {len(rejected)}")

    values = [net(r["our_ask"], r["won"]) for r in taken]
    if values:
        low, high = ci(values)
        print(f"\nWHAT THE RULE TOOK: {sum(values) / len(values):+.4f}/ct "
              f"[{low:+.4f},{high:+.4f}] over {len(values)}")
    values = [net(r["our_ask"], r["won"]) for r in rejected]
    low, high = ci(values)
    print(f"WHAT IT REFUSED:    {sum(values) / len(values):+.4f}/ct "
          f"[{low:+.4f},{high:+.4f}] over {len(values)}")

    # 1. Rejections, by session.
    groups = defaultdict(list)
    for r in rejected:
        groups[f"rejected · {r['session'] or '?'}"].append(net(r["our_ask"], r["won"]))
    table("1. REJECTED SIGNALS BY SESSION - is a gate wrong in one regime?", groups)

    # 2. Rejections by the gate that refused them, per session.
    groups = defaultdict(list)
    for r in rejected:
        gates = (r["failed_gates"] or "none").split(", ")
        # The single-reason refusals are the interesting ones: a signal
        # refused for one reason is a clean test of that one gate.
        if len(gates) != 1:
            continue
        groups[f"{gates[0][:20]} · {r['session'] or '?'}"].append(
            net(r["our_ask"], r["won"])
        )
    table("2. SINGLE-GATE REFUSALS - one reason, so the gate is isolated", groups)

    # 3. Confidence calibration. The model score is what the header's HIGH /
    #    MEDIUM / LOW is largely built from, so it stands in for the label.
    groups = defaultdict(list)
    for r in taken:
        p = r["raw_probability"]
        if p is None:
            continue
        band = "model >=0.95" if p >= 0.95 else (
            "model 0.85-0.95" if p >= 0.85 else "model <0.85"
        )
        groups[f"{band} · {r['session'] or '?'}"].append(net(r["our_ask"], r["won"]))
    table("3. CONFIDENCE CALIBRATION on QUALIFIED signals - does high sort?",
          groups, minimum=20)

    # 4. The plain question: which session is the rule actually good in?
    groups = defaultdict(list)
    for r in taken:
        groups[f"taken · {r['session'] or '?'}"].append(net(r["our_ask"], r["won"]))
    table("4. WHAT THE RULE TAKES, BY SESSION", groups, minimum=20)

    bandit_report(rows)


if __name__ == "__main__":
    main()
