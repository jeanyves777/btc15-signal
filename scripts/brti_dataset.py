"""The learning dataset, built on the DEPLOYED BRTI features.

Everything the adaptive layer learned from until now was Binance-derived -
the instrument FINDINGS 41 measured disagreeing with the official settlement
on 19.4% of outcomes, and 40.5% of markets finishing within 5 bps of the
strike. Contexts built on it may be labelled wrong, which makes every cell
suspect before a single interval is computed.

This rebuilds the same rows from `brti_decision_points`: distance, momentum
and volatility as the live path now computes them, against the strike the
contract actually settles on. Prices still come from the Kalshi book, because
they always did.

WHAT IS AND IS NOT COMPARABLE. A BRTI distance is not a Binance distance - a
60-second mean is a low-pass filter, so the same market reads 10-20 where
Binance read 2-4 (FINDINGS 43). The bands here are therefore BRTI bands, and
a policy trained on this data carries `feature_version = brti-1` so it can
never be applied to features that mean something else.
"""

import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from btc15_signal.adaptive import (  # noqa: E402
    BRTI_FEATURE_VERSION,
    brti_context_of,
    brti_vol_regime,
)
from btc15_signal.sessions import session_of  # noqa: E402

# The bands live in `adaptive`, not here. Defining them in the training script
# is how replay and live drift apart: the live key still formats, it just
# names a pocket that was never trained. One definition, both callers.


def _minute_row(point: dict, quote: tuple, distance_floor: float,
                min_ask: float, max_ask: float) -> dict | None:
    """One decision minute, scored against the BRTI-calibrated gates.

    The band is unchanged (it was always a Kalshi price), the distance floor
    is the 10x FINDINGS 43 measured rather than the 1.5 that belongs to
    Binance volatility, and momentum is BRTI momentum.
    """
    yes_bid, yes_ask = quote
    side = point["brti_side"]
    ask = yes_ask if side == "UP" else round(1 - yes_bid, 4)
    if not 0 < ask < 1:
        return None
    won = (point["result"] == "yes") if side == "UP" else (
        point["result"] == "no"
    )
    distance = point["brti_normalized_distance"] or 0.0
    momentum = point["brti_momentum_bps"] or 0.0
    direction = 1 if side == "UP" else -1

    gates = []
    if not min_ask <= ask <= max_ask:
        gates.append("contract price band")
    if distance < distance_floor:
        gates.append("target distance")
    if direction * momentum <= 0:
        gates.append("momentum strength")

    window_ms = point["close_ms"] - 900_000
    return {
        "window_open": window_ms,
        "our_ask": ask, "won": int(won),
        "rule_match": 0 if gates else 1,
        "failed_gates": ", ".join(gates) or None,
        "session": session_of(window_ms),
        "vol_regime": brti_vol_regime(point["brti_volatility_bps"]),
        "brti_volatility_bps": point["brti_volatility_bps"] or 0.0,
        "brti_normalized_distance": distance,
        "brti_momentum_bps": momentum,
        "side": side, "remaining_s": point["remaining_s"],
        "feature_version": BRTI_FEATURE_VERSION,
    }


def load_policy_rows(distance_floor: float = 10.0,
                     min_ask: float = 0.70, max_ask: float = 0.93) -> list[dict]:
    """THE DEPLOYED POLICY: one row per market, scanned chronologically.

    The bot does not judge a market once. It scans every poll from 660s to
    360s remaining and takes the FIRST minute where the gates pass - then
    alerts once and stops ("Already alerted this window"). A market that is
    refused at 11 minutes and qualifies at 8 is a market the bot TRADES.

    Evaluating only the first minute answers a different question, and answers
    it pessimistically: it counts every later entry as a refusal. Evaluating
    every poll as its own trade answers a third, and answers it optimistically:
    it lets one market contribute six correlated outcomes and inflates every
    sample count sixfold. So the walk here is chronological and each market
    contributes exactly ONE row.

    For a market where no minute qualifies, the row is the FIRST decision
    minute. That is not an arbitrary pick: with the gates removed, the first
    poll qualifies by construction, so 660s is precisely where the policy
    would have entered without them - which is what the refused leg is
    measuring.
    """
    return _load(distance_floor, min_ask, max_ask, policy=True)


def load_brti_rows(distance_floor: float = 10.0,
                   min_ask: float = 0.70, max_ask: float = 0.93) -> list[dict]:
    """FIRST-MINUTE ANALYSIS: one row per market, judged at 660s only.

    Kept because it answers a real question - "what does the rule think when
    it first looks?" - but it is NOT the deployed strategy, which re-checks
    every minute to 360s. Results computed from this must be labelled
    first-minute. Use `load_policy_rows` for the policy.
    """
    return _load(distance_floor, min_ask, max_ask, policy=False)


def _load(distance_floor: float, min_ask: float, max_ask: float,
          policy: bool) -> list[dict]:
    brti = sqlite3.connect("file:data/brti_history.db?mode=ro", uri=True)
    brti.row_factory = sqlite3.Row
    points: dict[str, list[dict]] = {}
    for row in brti.execute(
        "SELECT * FROM brti_decision_points ORDER BY ticker, remaining_s DESC"
    ):
        points.setdefault(row["ticker"], []).append(dict(row))
    if not points:
        return []

    market = sqlite3.connect("file:data/market_data.db?mode=ro", uri=True)
    market.row_factory = sqlite3.Row
    quotes: dict[tuple, tuple] = {}
    for row in market.execute(
        "SELECT ticker, end_period_ts, yes_bid_close, yes_ask_close "
        "FROM contract_candles WHERE yes_bid_close IS NOT NULL "
        "AND yes_ask_close IS NOT NULL"
    ):
        quotes[(row["ticker"], row["end_period_ts"])] = (
            row["yes_bid_close"], row["yes_ask_close"]
        )

    rows = []
    for ticker, path in points.items():
        # `path` is already ordered 660s -> 360s: the order the bot sees them.
        minutes = []
        for point in path:
            end_ts = (point["close_ms"] - point["remaining_s"] * 1000) // 1000
            quote = quotes.get((ticker, end_ts))
            if quote is None:
                continue
            row = _minute_row(point, quote, distance_floor, min_ask, max_ask)
            if row is not None:
                minutes.append(row)
        chosen = choose_minute(minutes, policy=policy)
        if chosen is not None:
            rows.append(chosen)
    rows.sort(key=lambda r: r["window_open"])
    return rows


def choose_minute(minutes: list[dict], policy: bool) -> dict | None:
    """Which single minute represents this market.

    ONE row per market, never one per poll. Six polls of the same window are
    one opportunity; counting them separately would inflate every sample count
    sixfold with copies that all share an outcome.

    Under the policy the bot takes the FIRST minute whose gates pass and then
    stops. Where none passes it enters nothing, and the row is the first
    minute looked at - which is exactly where the policy WOULD have entered
    with the gates removed, so it is the right price for the refused leg.
    """
    if not minutes:
        return None
    if not policy:
        return minutes[0]
    for row in minutes:
        if row["rule_match"]:
            return row
    return minutes[0]


def brti_context(row: dict) -> str:
    """The context key, from the SAME function the live path calls."""
    return str(brti_context_of(row))


if __name__ == "__main__":
    from collections import Counter

    for label, loader in (("POLICY (scans 660s->360s)", load_policy_rows),
                          ("first-minute only (660s)", load_brti_rows)):
        rows = loader()
        taken = [r for r in rows if r["rule_match"]]
        print(f"{label:<28} markets {len(rows):<6} qualified {len(taken):<6} "
              f"rejected {len(rows) - len(taken)}")
        if taken:
            entry = Counter(r["remaining_s"] for r in taken)
            print("      entry minute of qualified:",
                  ", ".join(f"{s}s:{n}" for s, n in sorted(entry.items(),
                                                           reverse=True)))
    rows = load_policy_rows()
    print("\nsessions:", Counter(r["session"] for r in rows).most_common())
    print("vol:", Counter(r["vol_regime"] for r in rows).most_common())
    print("contexts:", len({brti_context(r) for r in rows}), "distinct cells")
