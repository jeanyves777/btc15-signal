"""Every conclusion the brain is allowed to state, computed here in Python.

The brain used to be handed raw depths and asked to describe the book. It
promptly announced that "the yes side has deeper total depth (53,883.2) than
the no side (59,779.3)" - arithmetically false, and contradicted by the share
it quoted in the same breath. `strip_invented_numbers` did not catch it because
every figure was real; only the comparison was wrong.

So the division of labour is now absolute:

**Python decides. The model only narrates.** Every comparison, ratio, threshold
and verdict is computed here and arrives as a finished label - NO_DEPTH_DOMINANT
with the difference attached, not two numbers to weigh up. A model that cannot
see a comparison cannot get one wrong.

The second failure was subtler and worse: the old commentary described depth
without ever connecting it to the price, the distance to the strike, the time
left, the model's own probability, or whether the trade was even worth doing.
It was a book narrator attached to a trading system. Every field below exists
because a decision needs it.
"""

from dataclasses import dataclass

from .regime import adjust as regime_adjust
from .regime import base_points as regime_base_points
from .regime import label_for as regime_label
from .regime import weight_for_hour
from .validation import kalshi_fee_charged


@dataclass(frozen=True)
class Level:
    price: float
    size: float


def weighted_imbalance(
    yes_levels: list[tuple[float, float]],
    no_levels: list[tuple[float, float]],
    decay: float = 0.6,
) -> tuple[float, str, float]:
    """(imbalance in [-1, +1], label, absolute size difference).

    Total depth is the wrong measure: a hundred thousand contracts resting ten
    cents away are not pressure, and treating them as equal to size at the touch
    is how a book that leans one way gets read as leaning the other. Levels are
    therefore weighted by how close they sit to the best price, decaying by
    `decay` per level out.

    Positive favours YES, negative favours NO.
    """
    def side_weight(levels: list[tuple[float, float]]) -> float:
        ordered = sorted(levels, key=lambda level: -level[0])
        return sum(size * (decay**index) for index, (_price, size) in enumerate(ordered))

    yes = side_weight(yes_levels)
    no = side_weight(no_levels)
    total = yes + no
    if total <= 0:
        return 0.0, "NO_BOOK", 0.0
    imbalance = (yes - no) / total
    if imbalance > 0.20:
        label = "YES_DEPTH_DOMINANT"
    elif imbalance < -0.20:
        label = "NO_DEPTH_DOMINANT"
    else:
        label = "BOOK_BALANCED"
    return round(imbalance, 4), label, round(abs(yes - no), 1)


def executable_price(levels: list[tuple[float, float]], size: float) -> float | None:
    """Average price to fill `size` by walking the book, or None if too thin.

    The touch is what you see; this is what you would actually pay. On a thin
    book the two differ, and a decision made on the touch is made on a price
    that does not exist for the size being traded.
    """
    remaining, cost = size, 0.0
    for price, available in sorted(levels, key=lambda level: level[0]):
        take = min(remaining, available)
        cost += take * price
        remaining -= take
        if remaining <= 0:
            return round(cost / size, 4)
    return None


def decision_facts(
    *,
    ticker: str,
    remaining_s: int,
    side: str,
    btc: float,
    target: float,
    our_ask: float | None,
    exit_bid: float | None,
    yes_bid: float | None,
    yes_ask: float | None,
    no_ask: float | None,
    yes_levels: list[tuple[float, float]],
    no_levels: list[tuple[float, float]],
    momentum_5m_bps: float,
    volatility_5m_bps: float,
    futures_basis_bps: float,
    taker_imbalance: float,
    spread_bps: float,
    session: str,
    vol_regime: str,
    book_age_s: float | None,
    rule_match: bool,
    failed_gates: str,
    holding: bool,
    entry_paid: float | None,
    unrealised: float | None,
    model_probability: float,
    measured_edge: float | None,
    slippage: float,
    count: float = 1.0,
    hour_utc: int | None = None,
) -> dict:
    """Finished conclusions for one decision point. No raw comparisons escape.

    `measured_edge` is the historical per-contract edge for the band being
    traded, so "expected edge after costs" is grounded in what was measured
    rather than in the model's own opinion of itself.
    """
    distance = btc - target
    signed = distance if side == "UP" else -distance
    vol_units = abs(distance) / target * 10_000 / max(volatility_5m_bps, 1.0)

    imbalance, imbalance_label, depth_gap = weighted_imbalance(yes_levels, no_levels)
    # The book imbalance expressed for OUR side, so the label never has to be
    # mentally flipped when the position is DOWN.
    favours_us = imbalance if side == "UP" else -imbalance
    if favours_us > 0.20:
        book_for_us = "BOOK_FAVOURS_US"
    elif favours_us < -0.20:
        book_for_us = "BOOK_AGAINST_US"
    else:
        book_for_us = "BOOK_NEUTRAL"

    our_levels = yes_levels if side == "UP" else no_levels
    fillable = executable_price(our_levels, count)

    implied = our_ask if our_ask is not None else None
    disagreement = (
        round(model_probability - implied, 4) if implied is not None else None
    )

    # The fee is certain and always paid. The slippage allowance is NOT: it is
    # a ceiling that costs nothing unless the book moves, so charging it on
    # every trade understates the edge and would refuse trades that are fine.
    # Both are reported, and the verdict uses the certain one.
    # A MEANINGFUL margin, not merely a positive number. The old test was
    # `> 0`, so +0.0006 - six hundredths of a cent, indistinguishable from zero -
    # was announced as "worth having". And `measured_edge` is None at prices
    # nobody studied, where the honest answer is "unknown", not a figure.
    WORTH_HAVING = 0.005
    edge_after_costs = worst_case_edge = fee_per = None
    if implied is not None and measured_edge is not None:
        fee_per = round(kalshi_fee_charged(implied, count) / count, 4)
        edge_after_costs = round(measured_edge - fee_per, 4)
        worst_case_edge = round(edge_after_costs - slippage, 4)

    if holding:
        action = "HOLD"
    elif not rule_match or edge_after_costs is not None and edge_after_costs <= 0:
        action = "PASS"
    else:
        action = "BUY"

    # Confidence is about the strength of agreement between INDEPENDENT
    # signals, never about the model's own certainty - which is not calibrated.
    # Two of the original five were not independent, and the count was inflated
    # on every setup as a result:
    #
    #   `signed > 0` could not fail on an entry. `model.predict` chooses the
    #   side FROM the sign of the distance, so `signed` is non-negative by
    #   construction and this was a free point on every alert ever sent.
    #
    #   `vol_units >= 1.5` restated `min_normalized_distance`, which
    #   `rule_match` had already counted, and it ticked identically at 1.9x and
    #   at 6x - so a setup scraping over the floor scored the same as one with
    #   four times the room.
    #
    # On 2026-09-21 the 14:45 window was announced "BUY - confidence high (5/5
    # signals agree)" on a 1.9x gap that the evidence line immediately below
    # called "moderate"; one minute later BTC was 83 cents from the target and
    # the market had flipped to 68% the other way. A margin that thin is not
    # five signals agreeing.
    #
    # So the distance term now requires a REAL margin - the same 3x the
    # evidence line calls "comfortable", so the number and the prose can no
    # longer contradict each other - and the free term is gone.
    agreeing = sum(
        [
            bool(rule_match),
            vol_units >= 3.0,
            book_for_us == "BOOK_FAVOURS_US",
            (momentum_5m_bps > 0) == (side == "UP"),
        ]
    )
    # Time-of-day adjusts the CONFIDENCE EXPLANATION and nothing else. It may
    # never skip a market, stop a poll, prevent evaluation, block a qualified
    # order or silence an alert - the 15-minute system runs 24/7 regardless of
    # what the clock says about the regime. Stated as points so the move is
    # visible: base HIGH, adjustment -25, adjusted MEDIUM.
    regime_weight = weight_for_hour(hour_utc) if hour_utc is not None else None
    if regime_weight is not None:
        scored = regime_adjust(agreeing, regime_weight)
    else:
        base = regime_base_points(agreeing)
        scored = {
            "base_points": base, "base_label": regime_label(base),
            "regime_points": 0, "regime_reason": "unknown hour",
            "adjusted_points": base, "adjusted_label": regime_label(base),
        }
    confidence = scored["adjusted_label"]

    # The level at which this trade is simply wrong, stated as a price.
    invalidation = round(target, 2)

    stale = book_age_s is not None and book_age_s > 60

    # Evidence written out in plain English, here, in code.
    #
    # Handing the model a nested dict made it recite field names - "spread_bps
    # is 0.0", "our_side_is_winning is true" - and state the same fact twice in
    # opposite dialects: "imbalance is YES_DEPTH_DOMINANT, but book is
    # BOOK_AGAINST_US", which for a DOWN bet is one fact, not a contradiction.
    # So only ONE statement is made per piece of evidence, already resolved to
    # our side, already in words. The model's job is prose, nothing else.
    evidence: list[str] = []
    evidence.append(
        f"BTC is {abs(distance):,.0f} dollars "
        f"{'above' if distance > 0 else 'below'} the target, "
        f"{'the right side for this bet' if signed > 0 else 'the WRONG side for this bet'}"
    )
    evidence.append(
        f"that is {vol_units:.1f} times the recent 5-minute move, so the gap is "
        + ("comfortable" if vol_units >= 3 else "slim" if vol_units < 1.5 else "moderate")
    )
    if book_for_us == "BOOK_FAVOURS_US":
        evidence.append("resting size in the book leans our way")
    elif book_for_us == "BOOK_AGAINST_US":
        evidence.append("resting size in the book leans against us")
    else:
        evidence.append("the book is roughly balanced and says little")
    momentum_agrees = (momentum_5m_bps > 0) == (side == "UP")
    evidence.append(
        "five-minute momentum is pushing our way"
        if momentum_agrees
        else "five-minute momentum is pushing against us"
    )
    if stale:
        evidence.append("the order book data is stale, so treat the book point lightly")
    if not rule_match:
        evidence.append(f"the strategy refuses this setup: {failed_gates or 'unspecified'}")
    if holding and unrealised is not None:
        evidence.append(
            f"we already hold this at {entry_paid:.0%} and it is "
            f"{'up' if unrealised >= 0 else 'down'} {abs(unrealised):.2f} right now"
        )
    if measured_edge is None:
        evidence.append(
            f"no edge has ever been measured at {implied:.0%}, so there is nothing "
            "to expect from this price either way"
            if implied is not None
            else "no edge has been measured at this price"
        )
    elif edge_after_costs is not None:
        if edge_after_costs >= WORTH_HAVING:
            verdict_words = "which is worth having"
        elif edge_after_costs > 0:
            verdict_words = "which is too thin to be worth having"
        else:
            verdict_words = "which is not worth having"
        evidence.append(
            f"after the fee the expected edge is {edge_after_costs:+.4f} per "
            f"contract, {verdict_words}"
        )

    return {
        "market": {
            "ticker": ticker,
            "minutes_left": round(remaining_s / 60, 1),
            "session": session,
            "vol_regime": vol_regime,
            "book_age_s": book_age_s,
            "book_is_stale": stale,
        },
        "price": {
            "side": side,
            "btc": round(btc, 2),
            "target": round(target, 2),
            "distance_dollars": round(distance, 2),
            "distance_bps": round(abs(distance) / target * 10_000, 1),
            "distance_vol_units": round(vol_units, 2),
            "position_vs_target": "ABOVE" if distance > 0 else "BELOW",
            "our_side_is_winning": signed > 0,
        },
        "quotes": {
            "our_ask": our_ask,
            "exit_bid": exit_bid,
            "yes_bid": yes_bid,
            "yes_ask": yes_ask,
            "no_ask": no_ask,
            "spread_bps": round(spread_bps, 2),
            # Pre-computed, because the model was caught comparing these two:
            # "exit_bid is lower than our_ask". Always true, and not its job.
            "bid_ask_gap": (
                round(our_ask - exit_bid, 4)
                if our_ask is not None and exit_bid is not None
                else None
            ),
            "cost_to_enter_and_leave_now": (
                round(our_ask - exit_bid, 4)
                if our_ask is not None and exit_bid is not None
                else None
            ),
            "executable_price_for_size": fillable,
            "slips_past_touch": (
                fillable is not None and our_ask is not None and fillable > our_ask
            ),
        },
        "book": {
            "weighted_imbalance": imbalance,
            "imbalance_label": imbalance_label,
            "depth_difference": depth_gap,
            "for_our_side": book_for_us,
        },
        "flow": {
            "momentum_5m_bps": round(momentum_5m_bps, 1),
            "momentum_agrees_with_side": (momentum_5m_bps > 0) == (side == "UP"),
            "volatility_5m_bps": round(volatility_5m_bps, 1),
            "futures_basis_bps": round(futures_basis_bps, 1),
            "taker_imbalance": round(taker_imbalance, 3),
        },
        "probability": {
            "model": round(model_probability, 4),
            "market_implied": implied,
            "model_minus_market": disagreement,
            "note": "model probability is NOT calibrated; the market price is the better estimate",
        },
        "economics": {
            "measured_edge_per_contract": measured_edge,
            "price_was_measured": measured_edge is not None,
            "fee_per_contract": fee_per,
            "edge_after_fees": edge_after_costs,
            "slippage_allowance": slippage,
            "edge_if_slippage_fully_paid": worst_case_edge,
            "fee_note": (
                "the Kalshi fee is 0.07*P*(1-P), so it is LARGEST mid-book and "
                "smallest at the extremes - the cheap end of the band is the "
                "expensive end after fees"
            ),
            "worth_doing": (
                edge_after_costs is not None and edge_after_costs >= WORTH_HAVING
            ),
        },
        "position": {
            "holding": holding,
            "entry_paid": entry_paid,
            "unrealised": unrealised,
        },
        "summary": {
            "headline": (
                f"{side} at {implied:.0%} with {round(remaining_s / 60, 1)} minutes left"
                if implied is not None
                else f"{side} with {round(remaining_s / 60, 1)} minutes left"
            ),
            "evidence": evidence,
            "action": action,
            "confidence": confidence.lower(),
            "invalidation": (
                f"BTC settling {'below' if side == 'UP' else 'above'} "
                f"{invalidation:,.2f}"
            ),
        },
        "verdict": {
            "action": action,
            "confidence": confidence,
            "signals_agreeing": agreeing,
            "signals_total": 4,
            "base_confidence": scored["base_label"],
            "regime_adjustment": scored["regime_points"],
            "regime_hour": scored["regime_reason"],
            "adjusted_confidence": scored["adjusted_label"],
            "rule_match": rule_match,
            "failed_gates": failed_gates or "none",
            "invalidation_price": invalidation,
            "invalidation_note": (
                f"this trade is wrong if BTC settles "
                f"{'below' if side == 'UP' else 'above'} {invalidation}"
            ),
        },
    }
