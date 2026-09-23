"""The LIVE record, audited. Two populations, never mixed.

This leads with what the system actually collected, because that is the only
evidence about the rules that are running. The historical corpus replays a
DIFFERENT rule - BRTI distance >= 10 where the deployed rule uses Binance
distance >= 1.5, momentum alignment required where deployed does not, and
none of the band-hold, position, retry or fill logic - so it develops
hypotheses and cannot settle them.

    A. SIGNALS    every live signal, qualified or rejected, with its side,
                  time, confidence, rejection reason, session, regime and
                  settled outcome. P&L here is HYPOTHETICAL: settlement is
                  observed for every market, but no fill was available for a
                  signal nobody traded.

    B. EXECUTIONS what the account actually did - orders, fills, misses,
                  fees, exits, and the broker's own net P&L. Never rebuilt
                  locally; `daily_ledger` is the broker's figure.

Three things are kept distinct throughout, because collapsing them is how a
qualifying minute gets reported as a trade:

    signal decision    the rule qualified at this poll
    order eligibility  AND the band held 60s, no position open, limits allow
    execution          AND an order was submitted, and filled

And two denominators are kept distinct: DECISIONS and MARKETS. Two decisions
in one window share a price path and an outcome, so they are two decisions
and one sample.

    python scripts/audit_live_signals.py
"""

import sqlite3
import sys
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from btc15_signal.config import Settings  # noqa: E402
from btc15_signal.store import wilson_lower  # noqa: E402
from btc15_signal.validation import kalshi_fee_charged as fee  # noqa: E402

MIN_CELL = 8          # below this a cell is listed but never called a finding


def net(ask: float, won: bool) -> float:
    return (1.0 if won else 0.0) - ask - fee(ask, 1)


def rule(title: str) -> None:
    print(f"\n{'=' * 78}\n{title}\n{'=' * 78}")


def market_rows(db) -> tuple[list[dict], dict]:
    """One row per settled market: the FIRST signal decision in the window.

    First qualifying poll if the rule ever qualified, else the first poll
    looked at. That is the SIGNAL decision - not an order, and not a fill.
    """
    per_window: dict[int, list[dict]] = defaultdict(list)
    for r in db.execute(
        "SELECT window_open, remaining_s, observed_ms, ticker, side, "
        "raw_probability, bucket, our_ask, rule_match, failed_gates, won, "
        "session, vol_regime, normalized_distance, volatility_5m_bps, "
        "momentum_5m_bps, spread_bps "
        "FROM observations WHERE won IS NOT NULL ORDER BY window_open, remaining_s DESC"
    ):
        per_window[r["window_open"]].append(dict(r))

    gaps = {"windows": len(per_window), "no_session": 0, "no_regime": 0,
            "no_ask": 0, "no_side": 0}
    rows = []
    for window, polls in per_window.items():
        chosen = next((p for p in polls if p["rule_match"]), polls[0])
        chosen["decisions_in_window"] = len(polls)
        chosen["ever_qualified"] = int(any(p["rule_match"] for p in polls))
        chosen["sides_seen"] = len({p["side"] for p in polls})
        if not chosen.get("session"):
            gaps["no_session"] += 1
        if not chosen.get("vol_regime"):
            gaps["no_regime"] += 1
        if chosen.get("our_ask") is None:
            gaps["no_ask"] += 1
        if not chosen.get("side"):
            gaps["no_side"] += 1
        rows.append(chosen)
    rows.sort(key=lambda r: r["window_open"])
    return rows, gaps


def confidence_band(p: float) -> str:
    if p is None:
        return "?"
    if p >= 0.80:
        return "HIGH >=80%"
    if p >= 0.65:
        return "mid 65-80%"
    return "low <65%"


def summarise(rows: list[dict], label: str) -> None:
    if not rows:
        print(f"  {label:<34} (none)")
        return
    wins = sum(r["won"] for r in rows)
    edge = sum(net(r["our_ask"], bool(r["won"])) for r in rows) / len(rows)
    lower = wilson_lower(wins, len(rows))
    breakeven = sum(r["our_ask"] for r in rows) / len(rows)
    flag = " *" if len(rows) < MIN_CELL else ""
    print(f"  {label:<34} n={len(rows):<4} {wins:>3}W "
          f"{100 * wins / len(rows):>5.1f}%  edge {edge:+.4f}/ct  "
          f"px {breakeven:.2f}  win-lb {lower:.0%}{flag}")


def main() -> None:
    settings = Settings()
    db = sqlite3.connect(f"file:{settings.database_path}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row

    rows, gaps = market_rows(db)

    # ---------------------------------------------------------------- data
    rule("0. DATA AUDIT - what is missing or unusable, stated before any finding")
    settled_predictions = db.execute(
        "SELECT COUNT(*) FROM predictions WHERE won IS NOT NULL"
    ).fetchone()[0]
    headline = db.execute(
        "SELECT COUNT(*), SUM(won) FROM predictions WHERE won IS NOT NULL"
    ).fetchone()
    print(f"  settled signals in `predictions`        {headline[0]}  "
          f"({headline[1]} won, {100 * headline[1] / headline[0]:.1f}%)"
          f"   <- the headline figure")
    print(f"  settled markets with observations       {gaps['windows']}")
    missing = settled_predictions - gaps["windows"]
    print(f"  MARKETS WITH NO FEATURE ROWS            {missing}"
          f"   <- cannot be audited by session/regime")
    # The gap is not random, and pretending it is would quietly bias every
    # breakdown below.
    split = db.execute(
        "SELECT ((SELECT COUNT(*) FROM observations o "
        "         WHERE o.window_open = p.window_open AND o.won IS NOT NULL) > 0) "
        "       AS has_obs, COUNT(*) n, SUM(p.won) w, "
        "       MIN(p.window_open) lo, MAX(p.window_open) hi "
        "FROM predictions p WHERE p.won IS NOT NULL GROUP BY has_obs"
    ).fetchall()
    print("\n  THE GAP IS A TIME SPLIT, NOT A RANDOM SAMPLE")
    for s in split:
        span = (f"{datetime.fromtimestamp(s['lo'] / 1000, UTC):%m-%d %H:%M}"
                f" .. {datetime.fromtimestamp(s['hi'] / 1000, UTC):%m-%d %H:%M}")
        label = "audited" if s["has_obs"] else "NOT audited"
        print(f"    {label:<13} n={s['n']:<4} {100 * s['w'] / s['n']:>5.1f}% won"
              f"   {span}")
    print("    The feature archive began partway through the record, so every")
    print("    breakdown below covers the LATER period only - which happens to")
    print("    be the weaker one. Session and regime splits inherit that.")
    print(f"  missing session label                   {gaps['no_session']}")
    print(f"  missing volatility regime               {gaps['no_regime']}")
    print(f"  missing side                            {gaps['no_side']}")
    print(f"  missing price                           {gaps['no_ask']}")
    total_polls = sum(r["decisions_in_window"] for r in rows)
    flipped = sum(1 for r in rows if r["sides_seen"] > 1)
    print(f"\n  DECISIONS vs MARKETS - different denominators")
    print(f"    signal decisions (polls)              {total_polls}")
    print(f"    unique markets                        {len(rows)}")
    print(f"    markets where the side FLIPPED        {flipped}")
    print(f"    -> statistics below use MARKETS. Two decisions in one window")
    print(f"       share a price path and an outcome; they are not two samples.")

    # ------------------------------------------------------------- signals
    rule("A. ALL LIVE SIGNALS - prediction quality. P&L HYPOTHETICAL throughout")
    print("  No fill was available for a signal nobody traded. A rejected")
    print("  winner is evidence about direction, not proof of an executable")
    print("  profit. Section B is the money.\n")
    taken = [r for r in rows if r["ever_qualified"]]
    refused = [r for r in rows if not r["ever_qualified"]]
    summarise(rows, "every settled market")
    summarise(taken, "rule QUALIFIED (at some poll)")
    summarise(refused, "rule REFUSED (never qualified)")

    print("\n  by session")
    for key in ("asia", "europe", "us", "late-us"):
        summarise([r for r in rows if r["session"] == key], f"  {key}")
    print("\n  by volatility regime")
    for key in ("low", "mid", "high"):
        summarise([r for r in rows if r["vol_regime"] == key], f"  {key}")
    print("\n  by contract price")
    for lo, hi, name in ((0, .70, "<70c"), (.70, .85, "70-85c"),
                         (.85, .94, "85-94c"), (.94, 1.01, "94c+")):
        summarise([r for r in rows if lo <= r["our_ask"] < hi], f"  {name}")
    print("\n  by model confidence")
    for key in ("HIGH >=80%", "mid 65-80%", "low <65%"):
        summarise([r for r in rows
                   if confidence_band(r["raw_probability"]) == key], f"  {key}")

    # --------------------------------------------------- the two questions
    rule("A1. WHICH REJECTED PATTERNS PERFORMED WELL?  (hypothetical)")
    print("  Rejection reasons, on markets the rule never qualified.\n")
    reasons: dict[str, list] = defaultdict(list)
    for r in refused:
        for reason in (r["failed_gates"] or "unknown").split(", "):
            reasons[reason.strip()].append(r)
    for reason, group in sorted(reasons.items(), key=lambda kv: -len(kv[1])):
        summarise(group, f"  {reason[:30]}")
    good = [(k, g) for k, g in reasons.items() if len(g) >= MIN_CELL
            and sum(net(r["our_ask"], bool(r["won"])) for r in g) / len(g) > 0]
    print()
    if good:
        for k, g in good:
            mean = sum(net(r["our_ask"], bool(r["won"])) for r in g) / len(g)
            print(f"  -> '{k}' refused {len(g)} markets worth {mean:+.4f}/ct "
                  f"hypothetically")
        print("  These are CANDIDATES for forward evaluation, not adjustments.")
    else:
        print("  No rejection reason with n>=8 shows positive hypothetical edge.")
        print("  On this sample the gates are not leaving money on the table.")

    rule("A2. WHICH HIGH-CONFIDENCE PATTERNS UNDERPERFORMED?  (hypothetical)")
    print("  High model confidence, broken out. A high win rate at a high")
    print("  price is not an edge: at 89c a contract needs ~90% to break even.\n")
    high = [r for r in rows if confidence_band(r["raw_probability"]) == "HIGH >=80%"]
    summarise(high, "  all HIGH-confidence")
    for key in ("asia", "europe", "us", "late-us"):
        summarise([r for r in high if r["session"] == key], f"    {key}")
    for lo, hi, name in ((0, .85, "  <85c"), (.85, 1.01, "  85c+")):
        summarise([r for r in high if lo <= r["our_ask"] < hi], f"  {name}")
    bad = [r for r in high if net(r["our_ask"], bool(r["won"])) < 0]
    print(f"\n  HIGH-confidence markets with negative hypothetical net: "
          f"{len(bad)} of {len(high)}")

    # ---------------------------------------------------------- executions
    rule("B. ACTUAL EXECUTIONS - the money. Broker figures only")
    proposals = db.execute(
        "SELECT status, COUNT(*) n FROM trade_proposals "
        "WHERE strategy='primary' GROUP BY status"
    ).fetchall()
    print("  order outcomes")
    for p in proposals:
        print(f"    {p['status']:<34} {p['n']}")
    ledger = [dict(r) for r in db.execute(
        "SELECT ticker, pnl, window_ms FROM daily_ledger WHERE pnl IS NOT NULL"
    )]
    if ledger:
        total = sum(r["pnl"] for r in ledger)
        winners = sum(1 for r in ledger if r["pnl"] > 0)
        print(f"\n  markets traded (broker ledger)          {len(ledger)}")
        print(f"  winners                                 {winners} "
              f"({100 * winners / len(ledger):.1f}%)")
        print(f"  NET REALISED                            {total:+.4f} dollars")
        print(f"  per market                              "
              f"{total / len(ledger):+.4f}")
        print("\n  This is the number that decides whether the system makes")
        print("  money. It is the broker's, net of fees, one row per market.")

    # ------------------------------------------------------ the comparison
    rule("C. THE TWO POPULATIONS SIDE BY SIDE")
    if ledger and rows:
        hyp = sum(net(r["our_ask"], bool(r["won"])) for r in rows) / len(rows)
        real = sum(r["pnl"] for r in ledger) / len(ledger)
        print(f"  hypothetical, all signals   n={len(rows):<5} {hyp:+.4f}/market")
        print(f"  realised, executed only     n={len(ledger):<5} {real:+.4f}/market")
        print("\n  Different populations and different questions. The first is")
        print("  prediction quality; the second is what the account did after")
        print("  fills, misses, fees and exits. Neither replaces the other and")
        print("  the difference between them is not an 'execution cost'.")
    print()


if __name__ == "__main__":
    main()
