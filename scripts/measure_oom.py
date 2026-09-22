"""Backtest the out-of-the-money strategy: buy cheap early, take profit at 150%.

The operator's specification:

  * from the START of the window, not on a spike/rejection setup
  * enter when a side is available at about a 35% chance
  * set a take-profit at 150% of what was paid (0.35 -> 0.525)
  * watch BOTH sides for the same opportunity, not just one

This is deliberately different from the deployed spike-reversion rule in
`reversion_strategy.json`, which waits for 10-12 minutes remaining, requires an
early spike AND a rejection, enters 0.30-0.35 on one side only, and takes
profit at a FIXED 0.50. That rule measured NO_GROSS_EDGE over 760 trades:
-$0.0440 per contract, 95% CI [-0.0609, -0.0277] - an interval entirely below
zero, so it loses reliably rather than by chance. It is `enabled: false`.

What makes this worth re-testing anyway is one line in the same validation
report: of 428 losing reversion trades, 332 (77.6%) could have been closed for
at least 2c of profit before expiry, and the median loser reached +$0.12 above
entry. Losers DO spend time in the money. A take-profit is the bet that you can
collect that before it evaporates. The report's own exit-policy table is the
warning: no policy tested beat holding, every "vs hold" interval spanned zero,
and 92.1% of WINNERS went at least 2c underwater on the way - so an exit rule
that saves losers also cuts winners, and the two roughly cancel.

So the question this script answers is not "do cheap contracts come back" - they
do - but whether the take-profit collects more from the losers than it gives up
on the winners, net of what it costs to get in.

TWO FEE MODELS, because they are the difference between the strategy working
and not. The entry crosses the spread and pays the taker fee. The take-profit
is a resting good-till-cancelled order - that is what `execution.py` already
places - and a maker fill pays ZERO. Taker-both-ways is the pessimistic bound.

    python scripts/measure_oom.py
"""

import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from compare_series import load  # noqa: E402

from btc15_signal.features import build_snapshots  # noqa: E402
from btc15_signal.validation import kalshi_fee_charged as fee  # noqa: E402

ENTRY_LO, ENTRY_HI = 0.30, 0.35   # "about a 35% chance"
TP_MULTIPLE = 1.5                 # "take profit at 150%"


def sides(snap):
    """(label, ask to buy at, bid to sell at) for both sides of one snapshot.

    A DOWN position holds NO contracts: it is bought at 1 - yes_bid and sold at
    1 - yes_ask. Using the yes price for both sides is the mistake that makes a
    two-sided backtest look symmetric when the spread is not.
    """
    if snap.yes_ask is None or snap.yes_bid is None:
        return []
    return [
        ("UP", snap.yes_ask, snap.yes_bid),
        ("DOWN", 1 - snap.yes_bid, 1 - snap.yes_ask),
    ]


def trade_one_side(snaps, label, entry_lo, entry_hi, tp_multiple):
    """First qualifying entry from the START, then the earliest TP touch."""
    for index, snap in enumerate(snaps):
        for side, ask, _bid in sides(snap):
            if side != label or not entry_lo <= ask <= entry_hi:
                continue
            target = min(tp_multiple * ask, 0.99)
            settle_won = ((snap.result == "yes") if label == "UP"
                          else (snap.result == "no"))
            for later in snaps[index + 1:]:
                for later_side, _a, bid in sides(later):
                    if later_side == label and bid >= target:
                        return {
                            "side": label, "entry": ask, "exit": bid,
                            "tp": True, "won": None, "settle_won": settle_won,
                            "minute": snap.remaining - later.remaining,
                            "entry_minute": snap.remaining,
                        }
            return {
                "side": label, "entry": ask, "exit": 1.0 if settle_won else 0.0,
                "tp": False, "won": settle_won, "settle_won": settle_won,
                "minute": None,
                "entry_minute": snap.remaining,
            }
    return None


def pnl(trade, taker_exit: bool) -> float:
    """One contract. Entry always crosses; the exit may rest for free."""
    cost = trade["entry"] + fee(trade["entry"], 1)
    if trade["tp"]:
        proceeds = trade["exit"]
        if taker_exit:
            proceeds -= fee(trade["exit"], 1)
        return proceeds - cost
    return trade["exit"] - cost


def ci(values, draws=4000, seed=7):
    if len(values) < 2:
        return 0.0, 0.0
    rng = random.Random(seed)
    means = sorted(
        sum(rng.choice(values) for _ in values) / len(values) for _ in range(draws)
    )
    return means[int(0.025 * draws)], means[int(0.975 * draws) - 1]


def report(trades, label, taker_exit):
    if not trades:
        print(f"  {label:<34}{'no trades':>10}")
        return
    log = [pnl(t, taker_exit) for t in trades]
    total = sum(log)
    per = total / len(log)
    lo, hi = ci(log)
    tp_hit = sum(1 for t in trades if t["tp"]) / len(trades)
    settled_win = [t for t in trades if not t["tp"]]
    sw = sum(1 for t in settled_win if t["won"]) / len(settled_win) if settled_win else 0.0
    spent = sum(t["entry"] for t in trades)
    print(f"  {label:<34}{len(trades):>7}{tp_hit:>9.1%}{sw:>10.1%}"
          f"{total:>+11.2f}{per:>+11.4f}  [{lo:+.4f}, {hi:+.4f}]"
          f"{total / spent:>+9.1%}")


def main() -> None:
    markets, klines, candles = load("data/market_data.db")
    if not klines:
        print("no klines - run scripts/fetch_klines.py first")
        return

    by_market: dict[str, list] = {}
    for snap in build_snapshots(markets, klines, candles):
        by_market.setdefault(snap.ticker, []).append(snap)

    ordered = []
    for ticker, snaps in by_market.items():
        snaps.sort(key=lambda s: -s.remaining)   # START of the window first
        if snaps and snaps[0].result in ("yes", "no"):
            ordered.append((ticker, snaps))
    ordered.sort(key=lambda item: item[1][0].open_ms)

    print("=" * 104)
    print("OUT-OF-THE-MONEY STRATEGY - buy at ~35c from the start, "
          f"take profit at {TP_MULTIPLE:.0%}")
    print("=" * 104)
    print(f"\n{len(ordered)} settled markets, both sides watched independently.")
    print(f"Entry {ENTRY_LO:.2f}-{ENTRY_HI:.2f}; take-profit at "
          f"{TP_MULTIPLE:.1f}x entry (a 0.35 entry targets "
          f"{TP_MULTIPLE * 0.35:.3f}).")

    up, down = [], []
    for _ticker, snaps in ordered:
        for label, bucket in (("UP", up), ("DOWN", down)):
            t = trade_one_side(snaps, label, ENTRY_LO, ENTRY_HI, TP_MULTIPLE)
            if t is not None:
                bucket.append(t)
    both = up + down

    for taker_exit in (False, True):
        model = ("TAKER entry, MAKER take-profit (the resting GTC exit, fee-free)"
                 if not taker_exit else
                 "TAKER both ways (pessimistic: the exit crosses too)")
        print(f"\n--- {model} ---")
        print(f"  {'':<34}{'n':>7}{'TP hit':>9}{'held won':>10}"
              f"{'P&L':>11}{'per trade':>11}{'95% CI':>22}{'ROI':>9}")
        report(both, "BOTH SIDES (the specification)", taker_exit)
        report(up, "  UP side only", taker_exit)
        report(down, "  DOWN side only", taker_exit)

    # The comparison that decides it: the SAME trades, held to expiry. If the
    # take-profit is the idea, it has to beat simply holding what it bought.
    print("\n" + "-" * 104)
    print("DOES THE TAKE-PROFIT EARN ITS KEEP? Same entries, held to settlement.")
    held = []
    for t in both:
        won = t["settle_won"]
        held.append((1.0 if won else 0.0) - t["entry"] - fee(t["entry"], 1))
    tp_log = [pnl(t, False) for t in both]
    diff = [a - b for a, b in zip(tp_log, held)]
    lo, hi = ci(diff)
    print(f"  with take-profit      {sum(tp_log):+.2f}   ({sum(tp_log) / len(tp_log):+.4f}/trade)")
    print(f"  held to expiry        {sum(held):+.2f}   ({sum(held) / len(held):+.4f}/trade)")
    print(f"  difference            {sum(diff) / len(diff):+.4f}/trade   "
          f"95% CI [{lo:+.4f}, {hi:+.4f}]")
    print("  => " + ("the take-profit CANNOT be distinguished from holding"
                     if lo < 0 < hi else
                     "the take-profit differs from holding"))
    print("=" * 104)


if __name__ == "__main__":
    main()
