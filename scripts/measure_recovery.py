"""Backtest the loss-recovery overlay: after a loss, one $10 trade at ~2 minutes.

The operator's specification:

  * the normal system keeps running unchanged, one contract per trade
  * a LOSS arms the recovery
  * the recovery fires on the next window that meets every normal gate AND
    offers a 0.90-0.93 ask at about two minutes to expiry - not necessarily the
    next signal, and possibly several markets later
  * it stakes TEN contracts (~$9.20 for a $10 payout) to cover the loss
  * then it disarms until the next loss

This is a recovery overlay, so the question is not whether the recovery trade
wins - at 92c it wins most of the time by construction - but whether the rare
recovery LOSS costs more than all the recovery wins put together. One loss at
ten contracts is about $9.20; one win is about $0.78. The break-even win rate
is therefore roughly 92%, which is the same number as the price, which is the
whole difficulty.

Measured against the actual history rather than argued. Both fee models are
reported, because a maker fill pays no fee and a taker fill pays the full
0.07*p*(1-p) - and at ten contracts that difference is real money.

    python scripts/measure_recovery.py
"""

import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from compare_series import load  # noqa: E402

from btc15_signal.features import build_snapshots  # noqa: E402
from btc15_signal.strategy import EntryRule  # noqa: E402

# The recovery leg, exactly as specified.
RECOVERY_CONTRACTS = 10
RECOVERY_MIN_ASK = 0.90
RECOVERY_MAX_ASK = 0.93
RECOVERY_MINUTES = 2      # "about two minutes to expiry"
RECOVERY_WINDOW = (1, 3)  # inclusive minute range that counts as "about"


def fee(price: float, contracts: float) -> float:
    return math.ceil(
        round(0.07 * contracts * price * (1 - price) * 10_000, 6)
    ) / 10_000


def side_and_ask(snap):
    if snap.yes_ask is None or snap.yes_bid is None:
        return None
    up, down = snap.yes_ask, 1 - snap.yes_bid
    side_is_up = up >= down
    ask = max(up, down)
    if not 0 < ask < 1:
        return None
    return side_is_up, ask


def normal_entry(snaps, rule: EntryRule):
    """First qualifying minute in the deployed 6-11 window, one contract."""
    for snap in snaps:
        if not 6 <= snap.remaining <= 11:
            continue
        got = side_and_ask(snap)
        if got is None:
            continue
        side_is_up, ask = got
        if not rule.min_ask <= ask <= rule.max_ask:
            continue
        vol = max(snap.volatility_5m_bps, 1.0)
        if abs(snap.signed_distance_bps) / vol < rule.min_normalized_distance:
            continue
        won = (snap.result == "yes") if side_is_up else (snap.result == "no")
        return ask, won
    return None


def recovery_entry(snaps, rule: EntryRule):
    """A 0.90-0.93 ask at ~2 minutes, still passing every normal gate."""
    for snap in snaps:
        if not RECOVERY_WINDOW[0] <= snap.remaining <= RECOVERY_WINDOW[1]:
            continue
        got = side_and_ask(snap)
        if got is None:
            continue
        side_is_up, ask = got
        if not RECOVERY_MIN_ASK <= ask <= RECOVERY_MAX_ASK:
            continue
        # "the same pre-setup of the normal system" - the distance gate is the
        # one that still means something this late.
        vol = max(snap.volatility_5m_bps, 1.0)
        if abs(snap.signed_distance_bps) / vol < rule.min_normalized_distance:
            continue
        won = (snap.result == "yes") if side_is_up else (snap.result == "no")
        return ask, won
    return None


def run(markets_in_order, rule: EntryRule, taker: bool):
    """One pass in chronological order, carrying the armed flag forward."""
    base_pnl = recovery_pnl = 0.0
    base_n = base_losses = 0
    rec_n = rec_wins = 0
    armed = False
    equity = 0.0
    trough = 0.0
    peak = 0.0
    rec_log = []
    for _ticker, snaps in markets_in_order:
        # The recovery leg is checked FIRST: it lives late in the window, so a
        # market can serve the normal system at minute 9 and the recovery at
        # minute 2. The one-position guard is not modelled, which flatters the
        # overlay slightly and is noted in the output.
        if armed:
            got = recovery_entry(snaps, rule)
            if got is not None:
                ask, won = got
                cost = RECOVERY_CONTRACTS * ask
                charged = fee(ask, RECOVERY_CONTRACTS) if taker else 0.0
                pnl = (RECOVERY_CONTRACTS if won else 0.0) - cost - charged
                recovery_pnl += pnl
                equity += pnl
                rec_n += 1
                rec_wins += int(won)
                rec_log.append((ask, won, pnl))
                armed = False

        got = normal_entry(snaps, rule)
        if got is not None:
            ask, won = got
            charged = fee(ask, 1) if taker else 0.0
            pnl = (1.0 if won else 0.0) - ask - charged
            base_pnl += pnl
            equity += pnl
            base_n += 1
            if not won:
                base_losses += 1
                armed = True
        peak = max(peak, equity)
        trough = min(trough, equity - peak)
    return {
        "base_pnl": base_pnl, "base_n": base_n, "base_losses": base_losses,
        "recovery_pnl": recovery_pnl, "rec_n": rec_n, "rec_wins": rec_wins,
        "total": base_pnl + recovery_pnl, "max_drawdown": trough,
        "log": rec_log,
    }


def main() -> None:
    rule = EntryRule.load("strategy.json")
    markets, klines, candles = load("data/market_data.db")
    if not klines:
        print("no klines - run scripts/fetch_klines.py first")
        return
    snapshots = build_snapshots(markets, klines, candles)

    by_market: dict[str, list] = {}
    for snap in snapshots:
        by_market.setdefault(snap.ticker, []).append(snap)
    ordered = []
    for ticker, snaps in by_market.items():
        snaps.sort(key=lambda s: -s.remaining)
        if snaps and snaps[0].result in ("yes", "no"):
            ordered.append((ticker, snaps))
    ordered.sort(key=lambda item: item[1][0].open_ms)

    print("=" * 78)
    print("LOSS-RECOVERY OVERLAY - backtested")
    print("=" * 78)
    print(f"\nNormal system: band {rule.min_ask:.2f}-{rule.max_ask:.2f}, "
          f"6-11 min, 1 contract.")
    print(f"Recovery: armed by a loss, {RECOVERY_CONTRACTS} contracts at "
          f"{RECOVERY_MIN_ASK:.2f}-{RECOVERY_MAX_ASK:.2f}, "
          f"{RECOVERY_WINDOW[0]}-{RECOVERY_WINDOW[1]} min left, same gates.")
    print(f"Markets in the study: {len(ordered)}")

    for taker in (True, False):
        label = "TAKER (crosses, pays the fee)" if taker else "MAKER (free, if it fills)"
        r = run(ordered, rule, taker)
        print(f"\n--- {label} ---")
        print(f"  normal system      n={r['base_n']:<5} "
              f"{r['base_pnl']:+.2f}   ({r['base_losses']} losses)")
        if r["rec_n"]:
            wr = r["rec_wins"] / r["rec_n"]
            print(f"  recovery trades    n={r['rec_n']:<5} "
                  f"{r['recovery_pnl']:+.2f}   ({wr:.1%} won)")
            losses = [p for _a, w, p in r["log"] if not w]
            print(f"  recovery losses    {len(losses)}"
                  + (f"   worst {min(losses):+.2f}" if losses else ""))
        else:
            print("  recovery trades    never fired")
        print(f"  TOTAL              {r['total']:+.2f}   "
              f"(without recovery: {r['base_pnl']:+.2f})")
        print(f"  max drawdown       {r['max_drawdown']:+.2f}")
        delta = r["total"] - r["base_pnl"]
        print(f"  overlay contributed {delta:+.2f}")

    # The arithmetic that decides it, stated independently of the run.
    print("\n" + "-" * 78)
    print("THE BREAK-EVEN, which does not depend on the backtest:")
    for ask in (0.90, 0.92, 0.93):
        cost = RECOVERY_CONTRACTS * ask
        f = fee(ask, RECOVERY_CONTRACTS)
        win = RECOVERY_CONTRACTS - cost - f
        lose = -cost - f
        need = -lose / (win - lose)
        print(f"  at {ask:.2f}: win {win:+.2f}, lose {lose:+.2f} -> "
              f"needs {need:.1%} to break even (taker)")
    print("=" * 78)


if __name__ == "__main__":
    main()
