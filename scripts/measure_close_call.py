"""Close-call exits: does a LATE adverse cross justify selling?

Section 2 closed the general stop-loss - any cross, crosses held 2 and 3
minutes, crosses filtered by bid floor - and every variant lost to holding.
None of those variants conditioned on TIME REMAINING, and that is the
operator's proposal here: the original entry buffer is irrelevant near expiry,
so in the last N minutes a position sitting on the wrong side of the strike
should be sold at the executable bid rather than risk the whole stake.

That is a different rule from section 2's, so it is measured rather than
assumed, on the same corpus, paired, net of fees, live side convention.

THE DECIDING NUMBER is not the policy's dollars, it is this: among positions
adversely crossed with N minutes left, how often do they still win, and how
does that compare with what the market charges to close them? An exit is only
worth taking when the crossed subset wins LESS often than its own bid implies.
If it wins more, the bid is cheap and selling donates the difference.

    python scripts/measure_close_call.py
"""

import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from compare_series import load  # noqa: E402

from btc15_signal.config import Settings  # noqa: E402
from btc15_signal.features import build_snapshots  # noqa: E402
from btc15_signal.strategy import EntryRule  # noqa: E402
from btc15_signal.validation import kalshi_fee_charged as fee  # noqa: E402


def exit_bid(snap, side: str) -> float | None:
    """Executable bid for CLOSING a position on `side`, the side of the book
    the position must actually hit. Long UP sells into the yes bid; long DOWN
    sells into the no bid, which is 1 - yes ask."""
    if side == "UP":
        return snap.yes_bid
    return None if snap.yes_ask is None else round(1 - snap.yes_ask, 4)


def offside(snap, side: str) -> bool:
    """BTC is on the wrong side of the strike for this position."""
    return snap.signed_distance_bps < 0 if side == "UP" else snap.signed_distance_bps > 0


def hold_net(entry: float, won: bool) -> float:
    return (1.0 if won else 0.0) - entry - fee(entry)


def exit_net(entry: float, bid: float) -> float:
    return bid - entry - fee(entry) - fee(bid)


def paired_ci(diffs: list[float], draws: int = 4000, seed: int = 11):
    if len(diffs) < 2:
        return 0.0, 0.0
    rng = random.Random(seed)
    means = sorted(
        sum(rng.choice(diffs) for _ in diffs) / len(diffs) for _ in range(draws)
    )
    return means[int(0.025 * draws)], means[int(0.975 * draws) - 1]


def build_trades(rule: EntryRule, settings: Settings) -> list[dict]:
    """One trade per market, at its FIRST qualifying minute, live convention.

    Live convention (section 13): the side is chosen by where BTC sits relative
    to the strike, NOT by which contract is dearer. Using the dearer side would
    measure a rule the bot does not run.
    """
    markets, klines, candles = load("data/market_data.db")
    snapshots = build_snapshots(markets, klines, candles)

    by_market: dict[str, list] = {}
    for snap in snapshots:
        by_market.setdefault(snap.ticker, []).append(snap)

    from_min = settings.entry_from_seconds / 60.0
    to_min = settings.entry_to_seconds / 60.0

    trades = []
    for ticker, snaps in by_market.items():
        snaps.sort(key=lambda s: -s.remaining)
        if snaps[0].result not in ("yes", "no"):
            continue
        for index, snap in enumerate(snaps):
            if not to_min <= snap.remaining <= from_min:
                continue
            side = "UP" if snap.signed_distance_bps >= 0 else "DOWN"
            entry = snap.entry_price(side)
            if entry is None or not 0 < entry < 1:
                continue
            if not rule.min_ask <= entry <= rule.max_ask:
                continue
            if abs(snap.normalized_distance) < rule.min_normalized_distance:
                continue
            direction = 1 if side == "UP" else -1
            if direction * snap.momentum_5m_bps < rule.min_momentum_bps:
                continue
            won = snap.won(side)
            if won is None:
                continue
            trades.append({
                "ticker": ticker, "side": side, "entry": entry, "won": won,
                "entry_remaining": snap.remaining,
                "path": snaps[index + 1:],
            })
            break
    return trades


def close_call(trade: dict, window: int, slippage: float,
               min_bid: float = 0.0) -> tuple[float, bool, float | None]:
    """(net, fired, exit price) for the close-call policy on one trade.

    Fires on the FIRST minute inside the last `window` minutes at which the
    position is offside and a bid at or above `min_bid` is quotable.
    """
    side, entry = trade["side"], trade["entry"]
    for snap in trade["path"]:
        if snap.remaining > window:
            continue
        if not offside(snap, side):
            continue
        bid = exit_bid(snap, side)
        if bid is None:
            continue
        fill = round(bid - slippage, 4)
        if fill < min_bid or fill <= 0:
            continue
        return exit_net(entry, fill), True, fill
    return hold_net(entry, trade["won"]), False, None


def crossed_subset(trades: list[dict], window: int) -> list[dict]:
    """Every trade that is offside at least once inside the last `window`
    minutes, with the bid quotable at that moment and its eventual outcome.

    Carries the discriminators the proposal names - how deep the cross is, how
    long it has persisted, and how fast distance is moving - so each can be
    asked the only question that matters: does THIS bucket win less than its
    own bid?
    """
    out = []
    for trade in trades:
        side = trade["side"]
        run = 0
        previous = None
        for snap in trade["path"]:
            is_off = offside(snap, side)
            run = run + 1 if is_off else 0
            if snap.remaining > window or not is_off:
                previous = snap
                continue
            bid = exit_bid(snap, side)
            if bid is None:
                previous = snap
                continue
            depth = abs(snap.signed_distance_bps)
            velocity = None
            if previous is not None:
                direction = 1 if side == "UP" else -1
                velocity = direction * (
                    snap.signed_distance_bps - previous.signed_distance_bps
                )
            out.append({
                "bid": bid, "won": trade["won"], "entry": trade["entry"],
                "remaining": snap.remaining, "depth_bps": depth,
                "run": run, "velocity": velocity,
                "vol_bps": snap.volatility_5m_bps,
            })
            break
        else:
            continue
    return out


def bucket_report(title: str, subset: list[dict], key, edges) -> None:
    """Every bucket asked the same question: win rate vs its own mean bid."""
    print(f"\n  by {title}")
    print(f"  {'bucket':>16} {'n':>6} {'mean bid':>9} {'win rate':>9} {'edge vs bid':>12}")
    labelled = []
    for row in subset:
        value = key(row)
        if value is None:
            continue
        label = f"< {edges[0]:g}"
        for low, high in zip(edges, edges[1:], strict=False):
            if value >= low:
                label = f"{low:g} - {high:g}"
        if value >= edges[-1]:
            label = f">= {edges[-1]:g}"
        labelled.append((label, row))
    order = [f"< {edges[0]:g}"] + [
        f"{low:g} - {high:g}" for low, high in zip(edges, edges[1:], strict=False)
    ] + [f">= {edges[-1]:g}"]
    for label in order:
        rows = [row for name, row in labelled if name == label]
        if len(rows) < 25:
            continue
        mean_bid = sum(row["bid"] for row in rows) / len(rows)
        win_rate = sum(row["won"] for row in rows) / len(rows)
        print(f"  {label:>16} {len(rows):>6} {mean_bid:>9.3f} {win_rate:>9.1%} "
              f"{win_rate - mean_bid:>+12.3f}")


def report_discriminators(trades: list[dict], window: int = 2) -> None:
    subset = crossed_subset(trades, window)
    print(f"\nDISCRIMINATORS inside the last {window} minutes (n={len(subset)})")
    print("  A bucket is worth exiting only if its edge vs bid is clearly NEGATIVE.")
    bucket_report("cross depth (bps past strike)", subset,
                  lambda r: r["depth_bps"], (2, 5, 10, 20))
    bucket_report("minutes offside in a row", subset, lambda r: r["run"], (2, 3, 4))
    bucket_report("distance velocity (bps/min against)", subset,
                  lambda r: r["velocity"], (0, 5, 15))
    bucket_report("5m volatility (bps)", subset, lambda r: r["vol_bps"], (5, 10, 20))


def report_deciding_number(trades: list[dict]) -> None:
    print("\nTHE DECIDING NUMBER - do late-crossed positions win less than their bid?")
    print("  A bid of b says the market prices survival at ~b. If the crossed")
    print("  subset wins MORE than b, the bid is cheap and selling donates money.\n")
    print(f"  {'window':>7} {'n':>6} {'mean bid':>9} {'win rate':>9} {'edge vs bid':>12}")
    for window in (1, 2, 3, 4, 5):
        subset = crossed_subset(trades, window)
        if not subset:
            continue
        mean_bid = sum(row["bid"] for row in subset) / len(subset)
        win_rate = sum(row["won"] for row in subset) / len(subset)
        print(f"  {window:>7} {len(subset):>6} {mean_bid:>9.3f} {win_rate:>9.1%} "
              f"{win_rate - mean_bid:>+12.3f}")


def report_policy(trades: list[dict], slippage: float) -> None:
    baseline = [hold_net(t["entry"], t["won"]) for t in trades]
    hold_mean = sum(baseline) / len(baseline)
    print(f"\nHOLD TO SETTLEMENT: {hold_mean:+.4f}/contract on {len(trades)} trades "
          f"(total {sum(baseline):+.2f})")

    print(f"\nCLOSE-CALL EXIT vs HOLD (exit slippage {slippage:.2f}, both fees paid)")
    print(f"  {'window':>7} {'min bid':>8} {'fired':>6} {'wrong':>7} "
          f"{'exit-hold':>10} {'95% CI':>22} {'total $':>9}")
    for window in (1, 2, 3):
        for min_bid in (0.0, 0.10, 0.20, 0.30):
            diffs, fired, wrong = [], 0, 0
            for trade, hold in zip(trades, baseline, strict=True):
                net, did_fire, _ = close_call(trade, window, slippage, min_bid)
                diffs.append(net - hold)
                if did_fire:
                    fired += 1
                    if trade["won"]:
                        wrong += 1
            mean = sum(diffs) / len(diffs)
            low, high = paired_ci(diffs)
            wrong_pct = f"{wrong / fired:.0%}" if fired else "-"
            print(f"  {window:>7} {min_bid:>8.2f} {fired:>6} {wrong_pct:>7} "
                  f"{mean:>+10.4f} [{low:>+8.4f},{high:>+8.4f}] {sum(diffs):>+9.2f}")


def report_oracle(trades: list[dict], slippage: float) -> None:
    """Upper bound: exit ONLY the late-crossed positions that actually lose.
    Unreachable in real time; it bounds what any close-call model could win."""
    for window in (1, 2):
        gain = 0.0
        taken = 0
        for trade in trades:
            if trade["won"]:
                continue
            hold = hold_net(trade["entry"], False)
            net, fired, _ = close_call(trade, window, slippage)
            if fired and net > hold:
                gain += net - hold
                taken += 1
        per = gain / len(trades)
        print(f"  oracle window {window}m: rescue {taken} losers, "
              f"{per:+.4f}/contract over all trades (total {gain:+.2f})")


def main() -> None:
    settings = Settings()
    rule = EntryRule.load(Path("strategy.json"))
    print(f"Deployed rule: ask {rule.min_ask}-{rule.max_ask}, "
          f"distance >= {rule.min_normalized_distance}, "
          f"entry {settings.entry_from_seconds}-{settings.entry_to_seconds}s")

    trades = build_trades(rule, settings)
    if not trades:
        print("no qualifying trades")
        return

    report_deciding_number(trades)
    report_discriminators(trades, window=2)
    report_policy(trades, settings.exit_slippage)
    print("\nORACLE CEILING (hindsight, not implementable)")
    report_oracle(trades, settings.exit_slippage)


if __name__ == "__main__":
    main()
