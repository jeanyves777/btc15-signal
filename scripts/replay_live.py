"""Backtest the deployed rule against the LIVE TAPE, not against candles.

Every other backtest in this repo reconstructs a price from a minute candle and
credits a fill at it. This one replays `observations` - the quotes the running
service actually saw, at the cadence it actually polled, with the settlement it
actually observed - back through the deployed gate stack. No historical candle
is touched and no price is reconstructed.

What that buys, which the candle studies cannot have:

  * THE REAL ASK. `our_ask` is the Kalshi ask on OUR side at that instant, not
    a mid, not a close. A candle backtest has to guess this and guesses well.
  * THE REAL CADENCE. The service sees the book about every 12 seconds, so it
    can only act at those instants. A minute-candle study implicitly assumes a
    decision at every minute boundary and no other, which is both too coarse to
    catch a 24-second band touch and too generous about when it may act.
  * THE 120s SETTLE GATE. `entry_band_settle_s` is a rule about the PATH the
    price took, not about a price level. Reconstructing a continuous in-band
    streak needs sub-minute quotes. It cannot be evaluated on minute candles at
    all, and it is the gate currently deciding whether this system trades.

What it still cannot buy: n. The tape is hours, not months. Section 5 says so.

A note on `rule_match`: the column is stored live, but `strategy.json` was
edited mid-tape (0.80-0.99 -> 0.85-0.93 at 14:00 UTC on 2026-09-21), so rows
before that carry a verdict from a rule that is no longer deployed. Every gate
here is RECOMPUTED from the raw features against the rule on disk now, which is
the only way to ask "what would the current rule have done".

    python scripts/replay_live.py
"""

import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from btc15_signal.config import Settings  # noqa: E402
from btc15_signal.store import wilson_lower  # noqa: E402
from btc15_signal.strategy import EntryRule  # noqa: E402
from btc15_signal.validation import kalshi_fee_charged, trade_pnl  # noqa: E402

ACCOUNTED = "('filled','protected','unprotected','exited')"


def hhmm(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, UTC).strftime("%H:%M")


def gates(row: sqlite3.Row, rule: EntryRule, max_spread_bps: float) -> list[str]:
    """Every deployed gate, recomputed from the raw features. [] means eligible.

    Mirrors EntryRule.matches plus the two gates that live OUTSIDE the rule
    object in main.py: the entry window and the spread cap. Leaving those two
    out was the flaw in section 17 - the deployed window did not match the
    measured one - so they are applied here explicitly rather than assumed.
    """
    failed = []
    direction = 1 if row["side"] == "UP" else -1
    momentum = direction * (row["momentum_5m_bps"] or 0.0)
    ask = row["our_ask"]
    if not rule.min_ask <= ask <= rule.max_ask:
        failed.append("band")
    if (row["normalized_distance"] or 0.0) < rule.min_normalized_distance:
        failed.append("distance")
    if (row["raw_probability"] or 0.0) < rule.min_raw_probability:
        failed.append("model")
    if rule.require_momentum_alignment and momentum <= 0:
        failed.append("momentum-align")
    if momentum < rule.min_momentum_bps:
        failed.append("momentum")
    if (row["spread_bps"] or 0.0) > max_spread_bps:
        failed.append("spread")
    return failed


def band_streaks(polls: list[sqlite3.Row], low: float, high: float) -> list[float]:
    """Seconds the ask has sat CONTINUOUSLY in the band, as of each poll.

    Replicates store.band_streak_seconds by walking forward instead of reading
    backwards: the streak clock starts at the first in-band poll and is reset by
    any poll outside the band. Computed over the WHOLE window, not just the
    entry window, because the live version reads the last 60 observations
    regardless of how much time was left when they were taken.
    """
    out, started = [], None
    for row in polls:
        ask = row["our_ask"]
        if ask is not None and low <= ask <= high:
            if started is None:
                started = row["observed_ms"]
            out.append((row["observed_ms"] - started) / 1000)
        else:
            started = None
            out.append(0.0)
    return out


def summarise(label: str, fired: list[dict]) -> None:
    if not fired:
        print(f"  {label:<26} no trades")
        return
    pnl = [f["pnl"] for f in fired]
    wins = sum(1 for f in fired if f["won"])
    price = sum(f["price"] for f in fired) / len(fired)
    total = sum(pnl)
    print(f"  {label:<26} n={len(fired):<3} {wins}/{len(fired)} won  "
          f"avg ask {price:.3f}  {total / len(fired):+.4f}/ct  "
          f"total {total:+.4f}")


def main() -> None:
    settings = Settings()
    rule = EntryRule.load(settings.strategy_path)
    db = sqlite3.connect(f"file:{settings.database_path}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row

    print("=" * 78)
    print("REPLAY OF THE LIVE TAPE - deployed rule, real quotes, real cadence")
    print("=" * 78)
    print(f"\nRule on disk: band {rule.min_ask:.2f}-{rule.max_ask:.2f}, "
          f"distance >={rule.min_normalized_distance:.1f}x vol, "
          f"model >={rule.min_raw_probability:.2f}, "
          f"momentum >={rule.min_momentum_bps:.0f} bps")
    print(f"Entry window: {settings.entry_to_seconds}-{settings.entry_from_seconds}s "
          f"left.  Spread cap {settings.max_spread_bps:.1f} bps.  "
          f"Settle {settings.entry_band_settle_s}s.")

    windows = [r[0] for r in db.execute(
        "SELECT DISTINCT window_open FROM observations ORDER BY window_open"
    )]

    # --- 1. THE TAPE ------------------------------------------------------
    span = db.execute(
        "SELECT MIN(observed_ms), MAX(observed_ms), COUNT(*) FROM observations"
    ).fetchone()
    in_win = db.execute(
        "SELECT COUNT(*) FROM observations WHERE remaining_s BETWEEN ? AND ?",
        (settings.entry_to_seconds, settings.entry_from_seconds),
    ).fetchone()[0]
    print("\n1. THE TAPE")
    print(f"  windows observed                         {len(windows)}")
    print(f"  quotes recorded                          {span[2]}")
    print(f"  quotes inside the entry window           {in_win}")
    print(f"  covering                                 {hhmm(span[0])} - "
          f"{hhmm(span[1])} UTC ({(span[1] - span[0]) / 3.6e6:.1f}h)")

    # --- 2. THE FUNNEL ----------------------------------------------------
    # One pass per window, walking polls forward exactly as the service does.
    arms: dict[str, list[dict]] = {"touch": [], "settle60": [], "settle120": []}
    reached_band = eligible_windows = 0
    per_window = []
    for window in windows:
        polls = db.execute(
            "SELECT * FROM observations WHERE window_open=? AND our_ask IS NOT NULL "
            "ORDER BY observed_ms ASC", (window,)
        ).fetchall()
        if not polls:
            continue
        won = next((p["won"] for p in polls if p["won"] is not None), None)
        streaks = band_streaks(polls, rule.min_ask, rule.max_ask)
        touched = any(s > 0 for s in streaks)
        reached_band += touched
        # The streak that MATTERS is the one reached at a poll the service
        # could have acted on. Taking the maximum over the whole window
        # reported 12:15 as holding 121s when that streak was reached with
        # 120s left on the clock - two minutes past the entry window, and
        # failing three other gates. A number the gate could never have seen
        # is not an explanation of what the gate did.
        best_streak = 0.0
        in_band_polls = 0

        first: dict[str, dict] = {}
        for row, streak in zip(polls, streaks, strict=True):
            rem = row["remaining_s"]
            if rule.min_ask <= row["our_ask"] <= rule.max_ask:
                in_band_polls += 1
            if not settings.entry_to_seconds <= rem <= settings.entry_from_seconds:
                continue
            if gates(row, rule, settings.max_spread_bps):
                continue
            best_streak = max(best_streak, streak)
            # Eligible on the rule. Which arms would actually have fired?
            for arm, need in (("touch", 0.0), ("settle60", 60.0), ("settle120", 120.0)):
                if arm in first or streak < need:
                    continue
                first[arm] = {
                    "window": window, "price": row["our_ask"], "won": won,
                    "remaining_s": rem, "streak": streak,
                    "pnl": None if won is None else trade_pnl(
                        row["our_ask"], bool(won), contracts=1),
                }
        if first:
            eligible_windows += 1
        for arm, hit in first.items():
            if hit["pnl"] is not None:
                arms[arm].append(hit)
        per_window.append({
            "window": window, "won": won, "touched": touched,
            "best_streak": best_streak, "in_band_polls": in_band_polls,
            "first": first,
        })

    print("\n2. THE FUNNEL - how a window becomes a trade")
    print(f"  windows on the tape                      {len(windows)}")
    print(f"  ...whose ask entered the band            {reached_band}")
    print(f"  ...eligible on ALL gates at some poll    {eligible_windows}")
    for arm, need in (("touch", 0), ("settle60", 60), ("settle120", 120)):
        n = len({h['window'] for h in arms[arm]})
        print(f"  ...and held the band {need:>3}s -> TRADE       {n}")
    print("\n  The last two lines are the gate that is currently deciding this")
    print("  system's activity, and no minute-candle study can evaluate it.")

    # --- 3. WHAT THE SETTLE GATE DOES -------------------------------------
    # --- 2b. THE ONE NUMBER THAT LIMITS EVERY COMPARISON BELOW ------------
    # Read this before section 3. A gate can only be judged on cases where it
    # could have been wrong, and a band that contains no losing quotes offers
    # none. This is the same failure section 18 caught in the bootstrap: the
    # downside is absent from the sample, so everything downstream looks good.
    band_win = sum(p["in_band_polls"] for p in per_window if p["won"] == 1)
    band_loss = sum(p["in_band_polls"] for p in per_window if p["won"] == 0)
    lost_windows = [p for p in per_window if p["won"] == 0]
    print("\n2b. HOW MUCH DOWNSIDE IS IN THE BAND ON THIS TAPE?")
    print(f"  quotes in band, WINNING windows          {band_win}")
    print(f"  quotes in band, LOSING  windows          {band_loss}")
    print(f"  settled windows that lost                {len(lost_windows)}"
          f" of {sum(1 for p in per_window if p['won'] is not None)}")
    for p in lost_windows:
        print(f"    {hhmm(p['window'])} lost, and put {p['in_band_polls']} "
              f"quote(s) in the band")
    if band_loss <= 3:
        print("  => The band excluded both losers almost mechanically. Every")
        print("     win rate below is therefore near 100% BY CONSTRUCTION, and")
        print("     none of section 3 can be read as evidence the rule picks")
        print("     winners. What section 3 measures is the PRICE PATH.")

    print("\n3. THE SETTLE GATE - what waiting actually did, on this tape")
    print("   (same windows, same quotes; only the wait differs)")
    print("   Read as mechanics, not as P&L - see 2b.")
    for arm, need in (("touch", 0), ("settle60", 60), ("settle120", 120)):
        summarise(f"fire after {need:>3}s in band", arms[arm])

    common = {h["window"] for h in arms["touch"]} & {h["window"] for h in arms["settle120"]}
    if common:
        t = {h["window"]: h for h in arms["touch"]}
        s = {h["window"]: h for h in arms["settle120"]}
        dp = [s[w]["price"] - t[w]["price"] for w in common]
        print(f"\n  on the {len(common)} window(s) BOTH would trade, waiting 120s")
        print(f"  changed the entry price by               "
              f"{sum(dp) / len(dp):+.4f} (ask moves while you wait)")
    skipped = [
        p for p in per_window
        if "touch" in p["first"] and "settle120" not in p["first"] and p["won"] is not None
    ]
    if skipped:
        forgone = [trade_pnl(p["first"]["touch"]["price"], bool(p["won"]), contracts=1)
                   for p in skipped]
        wins = sum(1 for p in skipped if p["won"])
        print(f"\n  windows the 120s wait SKIPPED            {len(skipped)}")
        print(f"  they settled                             {wins}/{len(skipped)} won")
        print(f"  P&L the gate declined, at the touch ask  "
              f"{sum(forgone):+.4f} ({sum(forgone) / len(forgone):+.4f}/ct)")
        print("  That figure is NOT the cost of the gate. On a tape whose band")
        print("  holds no losers (2b) any filter can only ever look expensive.")
        print("  longest ELIGIBLE in-band streak each one reached:")
        for p in skipped:
            print(f"    {hhmm(p['window'])}  held {p['best_streak']:>5.0f}s of 120s  "
                  f"ask {p['first']['touch']['price']:.2f}  "
                  f"won={p['won']}")

    # --- 4. EXECUTION -----------------------------------------------------
    # The replay above credits a fill at the observed ask, which is the same
    # generosity every backtest here has. The live order log is the only thing
    # that can price that assumption, so it is applied rather than assumed.
    print("\n4. EXECUTION - pricing the fill assumption from the live order log")
    props = db.execute(
        "SELECT status, COUNT(*) c FROM trade_proposals "
        "WHERE entry_order_id IS NOT NULL GROUP BY status"
    ).fetchall()
    submitted = sum(r["c"] for r in props)
    filled = sum(r["c"] for r in props
                 if r["status"] in ("filled", "protected", "unprotected", "exited"))
    fill_rate = filled / submitted if submitted else None
    if submitted:
        print(f"  orders actually submitted                {submitted}")
        print(f"  filled                                   {filled} "
              f"({fill_rate:.0%})")
    slip = db.execute(
        f"SELECT entry_limit, fill_price FROM trade_proposals "
        f"WHERE status IN {ACCOUNTED} AND fill_price IS NOT NULL"
    ).fetchall()
    if slip:
        diffs = [r["fill_price"] - r["entry_limit"] for r in slip]
        mean_slip = sum(diffs) / len(diffs)
        worse = sum(1 for d in diffs if d > 0)
        print(f"  fill vs the price decided on             {mean_slip:+.4f} "
              f"over {len(diffs)} fills ({worse} worse, {len(diffs) - worse} better)")
    if fill_rate and arms["settle120"]:
        gross = sum(h["pnl"] for h in arms["settle120"])
        print(f"\n  replay at the deployed gate              {gross:+.4f} over "
              f"{len(arms['settle120'])} trade(s)")
        print(f"  same trades at the live {fill_rate:.0%} fill rate      "
              f"{gross * fill_rate:+.4f} expected")
        print("  A missed order is not a loss - it is a trade that never")
        print("  happened. The haircut is on the COUNT, not on the price, and")
        print("  that is the correction no candle study applies at all.")

    # --- 5. WHAT THIS CAN DECIDE -----------------------------------------
    print("\n5. WHAT THIS TAPE CAN AND CANNOT DECIDE")
    hours = (span[1] - span[0]) / 3.6e6
    for arm, need in (("touch", 0), ("settle120", 120)):
        fired = arms[arm]
        if len(fired) < 2:
            print(f"\n  {need:>3}s gate: {len(fired)} trade(s) - nothing can be concluded")
            continue
        n = len(fired)
        wins = sum(1 for f in fired if f["won"])
        price = sum(f["price"] for f in fired) / n
        low = wilson_lower(wins, n)
        breakeven = price + kalshi_fee_charged(price, 1)
        print(f"\n  {need:>3}s gate: {wins}/{n} won at an average ask of {price:.3f}")
        print(f"    win rate, 95% lower bound              {low:.1%}")
        print(f"    break-even after fee                   {breakeven:.1%}")
        print("    verdict                                "
              + ("consistent with an edge, NOT established"
                 if low < breakeven else "lower bound CLEARS break-even"))
    rate = len(windows) / hours if hours else 0
    print(f"\n  The tape is {hours:.1f} hours - {len(windows)} windows at "
          f"{rate:.1f}/hour.")
    print("  Section 7 of FINDINGS puts the sample needed at roughly 1,600")
    print("  trades. At the deployed gate's trade rate this tape is a rounding")
    print("  error against that, and it is the honest reading of every number")
    print("  above: these are DIAGNOSTICS of the gate stack, not evidence of")
    print("  an edge. What they do settle is mechanical - which gate stops")
    print("  which window, and what the ask did while the clock ran.")

    print("\n" + "=" * 78)
    print("No candle was read. Every price above is one the service saw live.")
    print("=" * 78)


if __name__ == "__main__":
    main()
