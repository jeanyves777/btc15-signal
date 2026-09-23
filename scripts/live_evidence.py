"""What the LIVE record actually says, with no appeal to historical candles.

Every number in FINDINGS above section 12 comes from minute candles and credits
a fill at the price the candle closed at. Live, orders miss. Historical quotes
record what the market did, not what OUR order got, so a backtest cannot see
execution at all and systematically reports the best case.

This report uses only what actually happened. It separates three things that a
single P&L figure hides:

  RULE       did the gates pick winners? Measurable on every qualifying signal,
             traded or not, because settlement is observed either way. This is
             the live test of the strategy, independent of execution.
  EXECUTION  what did getting the trade on cost? Orders that missed, and fills
             worse than the price the decision was made at. Invisible to any
             backtest.
  REALISED   what the account actually did. RULE plus EXECUTION plus variance.

And it answers the question that decides whether any of it means anything yet:
how far is this sample from telling a real edge from luck?

    python scripts/live_evidence.py
"""

import random
import sqlite3
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from btc15_signal.config import Settings  # noqa: E402
from btc15_signal.store import position_pnl, wilson_lower  # noqa: E402
from btc15_signal.validation import kalshi_fee_charged, trade_pnl  # noqa: E402

ACCOUNTED = "('filled','protected','unprotected','exited')"

# Losses required before a bootstrap interval means anything. A bootstrap can
# only resample what it was given: on 14 wins and 0 losses it never draws a
# loss, so it returns a tight interval around the winning payoff and reads as
# strong evidence when the downside is simply absent from the data. This is the
# standard rare-event failure and it is exactly the shape a short live record
# takes.
MIN_MINORITY = 3


def ci(values: list[float], draws: int = 4000, seed: int = 7):
    """Percentile bootstrap, REFUSED when the sample holds too few losses."""
    if len(values) < 2:
        return None, None
    if sum(1 for v in values if v < 0) < MIN_MINORITY:
        return None, None
    rng = random.Random(seed)
    means = sorted(
        sum(rng.choice(values) for _ in values) / len(values) for _ in range(draws)
    )
    return means[int(0.025 * draws)], means[int(0.975 * draws)]


def line(label: str, values: list[float], unit: str = "") -> None:
    if not values:
        print(f"  {label:<40} no data")
        return
    lo, hi = ci(values)
    mean = sum(values) / len(values)
    losses = sum(1 for v in values if v < 0)
    span = (
        f"[{lo:+.4f}, {hi:+.4f}]"
        if lo is not None
        else f"NO INTERVAL - only {losses} losses"
    )
    print(f"  {label:<40} n={len(values):<4} {mean:>+8.4f}{unit}  {span}")


def main() -> None:
    settings = Settings()
    db = sqlite3.connect(f"file:{settings.database_path}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row

    print("=" * 78)
    print("LIVE EVIDENCE - only what actually happened. No historical candles.")
    print("=" * 78)

    span = db.execute(
        "SELECT MIN(window_open), MAX(window_open) FROM predictions "
        "WHERE won IS NOT NULL"
    ).fetchone()
    if span[0]:
        print(f"\nSettled signals span {(span[1] - span[0]) / 3_600_000:.1f} hours.")

    # --- 1. THE RULE ------------------------------------------------------
    print("\n1. THE RULE - every settled signal at its signalled price")
    print("   (fill assumed, exactly as a backtest would. Traded or not.)")
    rows = db.execute(
        "SELECT window_open, won, contract_price FROM predictions "
        "WHERE won IS NOT NULL AND contract_price IS NOT NULL"
    ).fetchall()
    paper = {
        r["window_open"]: trade_pnl(r["contract_price"], bool(r["won"]), contracts=1)
        for r in rows
    }
    line("all signals (paper)", list(paper.values()), "/ct")
    banded = [
        trade_pnl(r["contract_price"], bool(r["won"]), contracts=1)
        for r in rows
        if 0.85 <= r["contract_price"] <= 0.93
    ]
    line("inside the 0.85-0.93 band", banded, "/ct")
    below = [
        trade_pnl(r["contract_price"], bool(r["won"]), contracts=1)
        for r in rows
        if r["contract_price"] < 0.85
    ]
    line("BELOW the band", below, "/ct")

    # --- 2. EXECUTION -----------------------------------------------------
    print("\n2. EXECUTION - what getting the trade on actually cost")
    print("   (no historical study can produce this section)")
    props = db.execute(
        "SELECT status, COUNT(*) c FROM trade_proposals "
        "WHERE entry_order_id IS NOT NULL GROUP BY status"
    ).fetchall()
    submitted = sum(r["c"] for r in props)
    filled = sum(
        r["c"]
        for r in props
        if r["status"] in ("filled", "protected", "unprotected", "exited")
    )
    if submitted:
        print(f"  orders submitted                         {submitted}")
        print(f"  filled                                   {filled} "
              f"({filled / submitted:.0%})")
        print(f"  missed                                   {submitted - filled} "
              f"({1 - filled / submitted:.0%})")
    slip = db.execute(
        f"SELECT entry_limit, fill_price FROM trade_proposals "
        f"WHERE status IN {ACCOUNTED} AND fill_price IS NOT NULL"
    ).fetchall()
    if slip:
        diffs = [r["fill_price"] - r["entry_limit"] for r in slip]
        print(f"  fill vs decision price                   "
              f"{statistics.mean(diffs):+.4f} mean over {len(diffs)} fills")

    # --- 3. REALISED ------------------------------------------------------
    print("\n3. REALISED - the account")
    # FROM THE LEDGER, NOT REBUILT. This section used to recompute P&L from
    # `trade_proposals` with `position_pnl`, and it was wrong twice over:
    # proposals exist for only 44 of the 127 traded markets, and one market
    # came out $2.00 from the truth (+0.2821 in the ledger, -1.7179 rebuilt).
    # It printed "+2.1583 dollars" under the heading "the account" while the
    # account held +0.3066.
    #
    # `daily_ledger` is the broker's own settled figure, net of fees, one row
    # per market. Money comes from the broker; a local reconstruction of it is
    # a second opinion, never the number.
    real = [r["pnl"] for r in db.execute(
        "SELECT pnl FROM daily_ledger WHERE pnl IS NOT NULL"
    )]
    line("realised P&L per market", real, "   ")
    if real:
        print(f"  {'total (broker ledger)':<40} {sum(real):+.4f} dollars "
              f"over {len(real)} markets")

    # --- 4. THE GAP -------------------------------------------------------
    traded = [
        r[0]
        for r in db.execute(
            f"SELECT DISTINCT window_open FROM trade_proposals "
            f"WHERE strategy='primary' AND status IN {ACCOUNTED}"
        )
    ]
    matched = [paper[w] for w in traded if w in paper]
    if real and matched:
        print("\n4. THE GAP - realised minus what a backtest would have credited")
        pm, rm = sum(matched) / len(matched), sum(real) / len(real)
        print(f"  paper on the same trades                 {pm:+.4f}/ct  n={len(matched)}")
        print(f"  realised (ledger, all markets)           {rm:+.4f}/ct  n={len(real)}")
        print(f"  difference                               {rm - pm:+.4f}/ct")
        print("  NOT a like-for-like subtraction: the paper leg covers the")
        print("  markets with a proposal row, the realised leg covers every")
        print("  market in the ledger. Read it as two summaries, not a bridge.")

    # --- 5. CAN IT DECIDE ANYTHING ---------------------------------------
    print("\n5. CAN THIS SAMPLE DECIDE ANYTHING YET?")
    target = 0.0220  # the measured 15-minute edge this is trying to confirm
    print(f"   Target effect to confirm: {target:+.4f}/contract.")
    print("   Sample size uses the THEORETICAL payoff variance, not the observed")
    print("   spread. A run containing no losses understates the variance to")
    print("   nearly zero and would claim a handful of trades is enough.")
    for label, vals, price in (
        ("realised trades", real, 0.85),
        ("banded paper signals", banded, 0.89),
    ):
        n = len(vals)
        wins = sum(1 for v in vals if v > 0)
        if n < 2:
            print(f"\n  {label}: n={n} - nothing can be concluded")
            continue
        # Payoff spans exactly one contract, so Var = q(1-q) for win rate q.
        # q comes from the PRICE - the market's own estimate - not from an
        # observed run that may contain no losses at all.
        sd = (price * (1 - price)) ** 0.5
        need = int(((1.96 + 0.84) * sd / target) ** 2) + 1
        low = wilson_lower(wins, n)
        breakeven = price + kalshi_fee_charged(price, 1)
        print(f"\n  {label}: {wins}/{n} won, reference price {price:.2f}")
        print(f"    win rate, 95% lower bound      {low:.1%}")
        print(f"    break-even after fee           {breakeven:.1%}")
        print("    verdict                        "
              + ("consistent with an edge, NOT established"
                 if low < breakeven else "lower bound CLEARS break-even"))
        print(f"    trades for 80% power           ~{need}")
        print(f"    at ~20 qualifying trades/day   ~{need / 20:.0f} days")

    print("\n" + "=" * 78)
    print("Section 2 is the part no backtest can produce. Section 5 is why")
    print("neither settles it yet, and is the only honest reading of a")
    print("nine-trade record.")
    print("=" * 78)


if __name__ == "__main__":
    main()
