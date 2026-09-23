"""The ACTIVE signal, built on Kalshi only.

Source of truth throughout: Kalshi quotes, Kalshi order books, Kalshi
executions, Kalshi settlements and Kalshi-provided BRTI. No Binance endpoint
is reachable from here, and there is deliberately no fallback - when the
reference is missing or stale this returns `None` with a stated reason and
the caller records that. Substituting another exchange is how a system ends
up trading one instrument and settling on another.

WHY `predict()` DOES NOT RUN ON THIS PATH. The deployed model is a
hand-weighted Binance model:

    score = 1.15*normalized_distance
          + 0.75*direction*momentum/volatility
          + 0.55*direction*bid_imbalance        <- Binance only
          + 0.65*direction*taker_imbalance      <- Binance only
          + 0.10*direction*futures_basis_bps    <- Binance only
          - 1.1

Three of its five terms do not exist on Kalshi, and the two that do are on a
different scale: BRTI normalized distance reads 10-20 where Binance reads 2-4
(FINDINGS 43), so `1.15 * 15` saturates the sigmoid to ~1.0 and the
`model confidence >= 0.50` gate would pass on EVERYTHING while still
rendering as a tick. That is the "renamed features" failure in its most
expensive form: a gate that looks deliberate and is off.

So there is no probability model on this path. The side comes from the
official reference directly, and the gates are the four measured ones in
`KalshiBRTIRule`. Inventing a Kalshi-native probability to fill the field
would be fabricating the number the whole exercise is meant to measure.
"""

from dataclasses import dataclass

from .brti import BRTIFeatures
from .kalshi_brti import KalshiBRTIRule

# Sentinel for "this build has no Kalshi-native probability model". It is not
# 0.0 (which renders as 0% confidence and reads like a rejection) and not 1.0
# (which reads like certainty). Anything consuming it must check `available`.
NO_MODEL = None


@dataclass(frozen=True)
class KalshiPrediction:
    """The same shape the order path expects, with Kalshi numbers only."""

    side: str
    distance_bps: float
    raw_probability: float | None = NO_MODEL
    score: float = 0.0
    bucket: int = -1          # -1 = no model, so calibration is not consulted

    @property
    def model_available(self) -> bool:
        return self.raw_probability is not None


@dataclass(frozen=True)
class Unavailable:
    """Why no signal could be formed. Recorded, never silently skipped."""

    reason: str
    detail: str = ""

    def __str__(self) -> str:
        return f"{self.reason}{f' ({self.detail})' if self.detail else ''}"


def prediction_from(features: BRTIFeatures) -> KalshiPrediction:
    """Side and distance from the official reference, nothing else."""
    return KalshiPrediction(
        side=features.side,
        distance_bps=abs(features.signed_distance_bps),
    )


def signal_inputs(
    features: BRTIFeatures | None,
    contract,
    *,
    now_ms: int,
    stale_limit_ms: int,
    max_spread_cents: float = 20.0,
) -> tuple[KalshiPrediction, float, BRTIFeatures] | Unavailable:
    """(prediction, ask, features) or `Unavailable` - never a fallback.

    Four ways this legitimately returns nothing, each named so the archive
    records WHICH rather than a blank:

      * the reference never arrived
      * the reference is stale beyond the configured limit
      * the reference describes a different market than the one in hand
      * the Kalshi book has no usable two-sided quote, is crossed, or is
        wider than the configured limit
    """
    if features is None:
        return Unavailable("kalshi brti unavailable", "no reference poll")
    if features.stale:
        return Unavailable("kalshi brti stale", f"limit {stale_limit_ms}ms")
    age = now_ms - features.ts_ms
    if stale_limit_ms and age > stale_limit_ms:
        return Unavailable("kalshi brti stale", f"{age}ms old")
    target = getattr(contract, "target", None)
    if not target or not features.target:
        return Unavailable("no strike", "contract or reference has no target")
    if abs(features.target - target) > 1e-6:
        return Unavailable(
            "kalshi brti is for another window",
            f"reference target {features.target} != contract {target}",
        )
    yes_bid = getattr(contract, "yes_bid", 0.0) or 0.0
    yes_ask = getattr(contract, "yes_ask", 0.0) or 0.0
    if not (0 < yes_ask < 1) or not (0 <= yes_bid < 1):
        return Unavailable("no kalshi quote", f"bid {yes_bid} ask {yes_ask}")
    # SPREAD, IN CENTS OF THE CONTRACT - the natural unit here.
    #
    # The Binance path gated on `max_spread_bps = 2.0` against SPOT spread,
    # which over 10,094 archived observations had a 99th percentile of 0.001
    # bps: it never rejected anything. Carrying that number across would have
    # been catastrophic rather than merely wrong - a 2c spread on a 79c mid is
    # 253 bps, so every single Kalshi signal would have been silently
    # discarded by a gate that had never once fired.
    #
    # Measured contract spreads: median 0.4c, p75 3c, p90 7c, p95 10c, p99
    # 19c. The threshold sits at ~p99 so it keeps doing what the old gate
    # actually did - catch a pathological book - rather than quietly becoming
    # a new selective gate nobody measured.
    spread_c = round((yes_ask - yes_bid) * 100, 2)
    if spread_c > max_spread_cents:
        return Unavailable("kalshi book too wide", f"{spread_c:.1f}c")
    if spread_c < 0:
        # A crossed book is not a tight one. It means the two sides were read
        # at different instants or the feed is unwell, and it appears in ~10%
        # of archived observations, so it is refused rather than treated as a
        # bargain.
        return Unavailable("kalshi book crossed", f"{spread_c:.1f}c")

    prediction = prediction_from(features)
    ask = contract.ask(prediction.side)
    if not 0 < ask < 1:
        return Unavailable("no kalshi quote", f"ask {ask} for {prediction.side}")
    return prediction, ask, features


def evaluate(
    rule: KalshiBRTIRule,
    features: BRTIFeatures,
    ask: float,
    remaining_s: int,
) -> tuple[bool, list[dict], tuple[str, ...]]:
    """(qualifies, facts for display, names of the failing gates).

    The facts are the SAME objects the message renders, so the word in the
    header and the ticks beneath it cannot disagree.
    """
    facts = rule.check_facts(features, ask, remaining_s)
    failed = tuple(f["name"] for f in facts if not f["passed"])
    in_window = rule.entry_to_seconds <= remaining_s <= rule.entry_from_seconds
    return (not failed and in_window), facts, failed
