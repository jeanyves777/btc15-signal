"""Scale-in: what does a position that got cheaper go on to do?

The operator's conditional scale-in adds a second contract when the ask falls
meaningfully below the first fill, subject to the evidence still holding. The
operator already named the risk - "historical retracement fills showed strong
adverse selection" - and scoped the add-on to shadow because of it. This is the
measurement behind that instinct, run on the same corpus as everything else.

FINDINGS 37 measured the entry version of this question and the answer was
brutal: a market where a 2c limit filled wins 54.1%, a market whose ask never
dipped wins 99.3%. **A 2c dip costs 45 points of win rate.** The dip is not a
discount, it is the market saying the trade is going wrong.

Scale-in is the same mechanism pointed at a bigger number. It does not wait for
a 2c dip, it waits for a 10-20c one, and it responds by ADDING exposure to the
position the dip is warning about. So the question here is narrow and precise:

    given an in-band entry at price P, and given the ask for OUR side later
    fell to P - d, what is the win rate from that moment, and what would a
    second contract bought there have earned, net of fees?

That second contract is priced at the dipped ask, so it is profitable only if
the position wins more often than that price implies. Nothing else about the
scale-in design matters if this number is bad.

    python scripts/measure_scale_in.py
"""

import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from measure_close_call import build_trades  # noqa: E402

from btc15_signal.config import Settings  # noqa: E402
from btc15_signal.strategy import EntryRule  # noqa: E402
from btc15_signal.validation import kalshi_fee_charged as fee  # noqa: E402

DIPS = (0.02, 0.05, 0.10, 0.15, 0.20)


def ask_for(snap, side: str) -> float | None:
    """What a SECOND contract on our side would cost at this minute."""
    if side == "UP":
        return snap.yes_ask
    return None if snap.yes_bid is None else round(1 - snap.yes_bid, 4)


def ci(values: list[float], draws: int = 4000, seed: int = 11):
    if len(values) < 2:
        return 0.0, 0.0
    rng = random.Random(seed)
    means = sorted(
        sum(rng.choice(values) for _ in values) / len(values) for _ in range(draws)
    )
    return means[int(0.025 * draws)], means[int(0.975 * draws) - 1]


def net(price: float, won: bool) -> float:
    return (1.0 if won else 0.0) - price - fee(price, 1)


def main() -> None:
    settings = Settings()
    rule = EntryRule.load(Path("strategy.json"))
    trades = build_trades(rule, settings)
    if not trades:
        print("no qualifying trades")
        return

    base = [net(t["entry"], t["won"]) for t in trades]
    print(f"in-band entries: n={len(trades)}  "
          f"base {sum(base) / len(base):+.4f}/ct  "
          f"win {sum(1 for t in trades if t['won']) / len(trades):.1%}")

    print("\nTHE ADD-ON CONTRACT, bought when the ask falls this far below the fill")
    print("  A second contract at price p is worth buying only if the position")
    print("  goes on to win MORE than p. That is the whole test.\n")
    print(f"  {'dip':>6} {'markets':>8} {'add price':>10} {'win after':>10} "
          f"{'edge vs price':>14} {'add net/ct':>11} {'95% CI':>22}")

    for dip in DIPS:
        added = []
        for trade in trades:
            for snap in trade["path"]:
                price = ask_for(snap, trade["side"])
                if price is None or price > trade["entry"] - dip:
                    continue
                if not 0 < price < 1:
                    continue
                added.append((price, trade["won"]))
                break
        if len(added) < 30:
            continue
        mean_price = sum(p for p, _ in added) / len(added)
        win = sum(1 for _, w in added if w) / len(added)
        values = [net(p, w) for p, w in added]
        low, high = ci(values)
        print(f"  {dip * 100:>5.0f}c {len(added):>8} {mean_price:>10.3f} "
              f"{win:>10.1%} {win - mean_price:>+14.3f} "
              f"{sum(values) / len(values):>+11.4f} "
              f"[{low:>+8.4f},{high:>+8.4f}]")

    print("\nTHE SAME MARKETS, HELD WITHOUT ADDING")
    print("  If the dipped cohort is simply a losing cohort, the add-on is not")
    print("  buying a discount - it is doubling into the losers.\n")
    print(f"  {'dip':>6} {'markets':>8} {'win rate':>10} {'base net/ct':>12} "
          f"{'vs all entries':>15}")
    overall = sum(base) / len(base)
    for dip in DIPS:
        cohort = []
        for trade in trades:
            for snap in trade["path"]:
                price = ask_for(snap, trade["side"])
                if price is None or price > trade["entry"] - dip:
                    continue
                cohort.append(trade)
                break
        if len(cohort) < 30:
            continue
        values = [net(t["entry"], t["won"]) for t in cohort]
        win = sum(1 for t in cohort if t["won"]) / len(cohort)
        print(f"  {dip * 100:>5.0f}c {len(cohort):>8} {win:>10.1%} "
              f"{sum(values) / len(values):>+12.4f} "
              f"{sum(values) / len(values) - overall:>+15.4f}")

    print("\nAND THE MARKETS THAT NEVER DIPPED, for contrast")
    for dip in (0.05, 0.10):
        never = []
        for trade in trades:
            dipped = any(
                (price := ask_for(snap, trade["side"])) is not None
                and price <= trade["entry"] - dip
                for snap in trade["path"]
            )
            if not dipped:
                never.append(trade)
        if len(never) < 30:
            continue
        values = [net(t["entry"], t["won"]) for t in never]
        win = sum(1 for t in never if t["won"]) / len(never)
        print(f"  never dipped {dip * 100:.0f}c: n={len(never)} win {win:.1%} "
              f"net {sum(values) / len(values):+.4f}/ct")


if __name__ == "__main__":
    main()
