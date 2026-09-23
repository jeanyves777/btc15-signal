"""Fit, validate, promote, withdraw. The judgement half of the learning loop.

Pure functions over rows. Nothing here opens a database, writes a file, or
knows what time it is unless it is told - so the same code that runs inside the
service every few hours can be run against a fixture in a test and produce the
identical artefact. A learning loop whose decisions cannot be reproduced offline
is a learning loop nobody can check.

FOUR QUESTIONS, DELIBERATELY KEPT APART. They have different evidence bars and
different consequences, and this system has previously answered one and acted as
though it had answered another.

    calibration   what is this cell actually worth?          descriptive
    confidence    may that re-rate what the operator sees?   needs evidence
    execution     may that refuse or admit an order?         needs the full bar
                                                             AND an operator
    withdrawal    has an active change stopped working?      needs its own bar

A confidence adjustment CANNOT admit a trade a gate refused, cannot veto one a
gate allowed, and cannot change size. It moves a label. That is the whole of its
authority, and keeping it that small is what makes it safe to grant on less
evidence than an execution change.

THE PROMOTION BAR IS NOT NEGOTIABLE DOWNWARD. It is stated once, here, and a
training run either clears it or does not. "Nothing qualified" is a result and
the loop keeps running and keeps measuring; manufacturing an active adjustment
by relaxing the bar would convert a measurement into a decision that was already
made. The bar:

    train n >= MIN_PROMOTION_N            enough to be worth acting on
    validate n >= MIN_VALIDATE_N          measured out of sample
    sign agreement                        train and validate point the same way
    widened interval excludes zero        widened by sqrt(candidates examined),
                                          because examining k cells and keeping
                                          the best of them is k chances to be
                                          fooled
    forward evidence does not contradict  where live rows exist for the cell

CHRONOLOGICAL, NEVER SHUFFLED. Markets in one session move together, so a random
split leaks the afternoon into the morning and every interval comes back too
narrow. Train is the oldest slice, validate the next, holdout the newest and is
not read during fitting at all.
"""

from __future__ import annotations

import datetime as dt
import math
import random
import time
from collections import defaultdict
from dataclasses import dataclass, field

from .intelligence_policy import ADMIT, NEUTRAL, VETO, Policy, shrink
from .validation import kalshi_fee_charged

# ---------------------------------------------------------------- the bar
MIN_CANDIDATE_N = 60       # enough to be worth WATCHING forward
MIN_PROMOTION_N = 120      # enough to be considered for CONTROLLING an order
MIN_VALIDATE_N = 40        # enough for the out-of-sample leg to mean anything
MIN_CONFIDENCE_N = 120     # enough to re-rate what the operator is shown
MIN_WITHDRAWAL_N = 20      # enough forward changes to call an active arm bad

TRAIN_FRACTION = 0.55
VALIDATE_FRACTION = 0.25

# Execution deltas were once scaled from dollars; DELTA_SCALE survives only for
# the candidate artefact's watch-list figure. CONFIDENCE is scaled in probability
# points (x100), never in dollars - see `_calibrate_confidence`.
DELTA_SCALE = 200
DELTA_CLAMP = 12


def net(ask: float, won: int | bool, fee: float | None = None) -> float:
    """Net dollars per contract. The fee is Kalshi's, charged or published.

    A real fill's real fee is used where one was recorded. Everywhere else the
    published formula stands in - and the row that used it is labelled a
    simulated fill, so nothing downstream can mistake the two.
    """
    charged = kalshi_fee_charged(float(ask), 1) if fee is None else float(fee)
    return (1.0 if won else 0.0) - float(ask) - charged


def reward_for(row: dict, slippage: float = 0.0) -> float:
    """What one row was worth, per contract, net of fees.

    AN EXECUTED TRADE IS WORTH WHAT KALSHI PAID. `realised_pnl` on an actual
    row is the exchange's own `pnl` divided by the contracts it settled - not a
    reconstruction from fill prices, and not a guess about which leg we held.
    That covers a held-to-expiry position and an early exit alike: a position
    sold at 100c on a market that later settled against us is a profit, and the
    settlement row already says so.

    A REFUSED OR UNFILLED SIGNAL IS PRICED WITH SLIPPAGE. It is a
    counterfactual: no order rested in that book, so the ask we recorded is the
    optimistic end of what crossing would have cost.
    """
    if row.get("fill_kind") == "actual":
        realised = row.get("realised_pnl")
        if realised is not None:
            return float(realised)
    ask = float(row["our_ask"])
    if not row.get("rule_match"):
        ask = min(0.99, ask + slippage)
    return net(ask, row.get("won"), None)


def day_of(ms: int) -> str:
    return dt.datetime.fromtimestamp(ms / 1000, dt.UTC).strftime("%Y-%m-%d")


def cluster_ci(values: list[float], groups: list, draws: int = 2000,
               seed: int = 11) -> tuple[float, float]:
    """Bootstrap over DAYS, not rows.

    Markets in one session move together - a trend that carries five windows
    carries their outcomes with it - so resampling rows treats correlated
    observations as independent and reports an interval far too narrow. The unit
    resampled here is the day. Fewer than two days is not an interval, and this
    returns a degenerate (0.0, 0.0) rather than a confident-looking number: an
    arm whose evidence is one afternoon has not been measured across anything.
    """
    if len(values) < 2:
        return 0.0, 0.0
    buckets = defaultdict(list)
    for value, group in zip(values, groups, strict=True):
        buckets[group].append(value)
    keys = list(buckets)
    if len(keys) < 2:
        return 0.0, 0.0
    rng = random.Random(seed)
    means = []
    for _ in range(draws):
        pool = []
        for _ in keys:
            pool.extend(buckets[rng.choice(keys)])
        means.append(sum(pool) / len(pool))
    means.sort()
    return means[int(0.025 * draws)], means[int(0.975 * draws) - 1]


def excludes_zero(low: float, high: float) -> bool:
    """A degenerate interval excludes nothing."""
    if low == 0.0 and high == 0.0:
        return False
    return low > 0 or high < 0


def _survives_widening(low: float, high: float, widening: float) -> bool:
    """Would this interval still exclude zero after a multiplicity correction?"""
    if low == 0.0 and high == 0.0:
        return False
    mid, half = (high + low) / 2, (high - low) / 2 * widening
    return mid - half > 0 or mid + half < 0


# --------------------------------------------------------------- fitting


@dataclass
class ArmFit:
    """One context-action cell and everything measured about it."""

    key: str
    n: int = 0
    markets: int = 0
    days: int = 0
    # PROFIT, in dollars per contract. Drives veto and admission.
    mean: float = 0.0
    low: float = 0.0
    high: float = 0.0
    win_rate: float = 0.0
    mean_ask: float = 0.0
    wins: int = 0
    # CALIBRATION, in probability points. Drives confidence, and nothing else.
    #
    # On Kalshi the ask IS the implied probability that OUR side wins: paying
    # 0.86 for a dollar payout is the market saying 86%. So the calibration
    # error of a cell is `observed win rate - mean ask`, measured per row as
    # `won - ask` and bootstrapped by day like everything else.
    #
    # Measured over 6,486 rows the market is close but not exact, and wrong in
    # a structured way: ask 0.9 wins 0.9025, ask 0.8 wins 0.8106, ask 0.6 wins
    # 0.5326. Favourites are slightly cheap and longshots dear - the
    # favourite-longshot bias - which is a real, directional signal about
    # WINNING, separate from whether the price leaves money on the table.
    calibration: float = 0.0
    calibration_low: float = 0.0
    calibration_high: float = 0.0
    validate_calibration: float | None = None
    # Filled in by `train`, which has the priors and the candidate count.
    shrunk: float = 0.0
    delta: int = 0
    delta_reason: str = ""
    probability: float | None = None
    action: str = NEUTRAL
    action_reason: str = ""
    gate: str | None = None
    promoted: bool = False
    validate_n: int = 0
    validate_mean: float | None = None
    # Every distinct failing-gate signature seen in this cell. An admission may
    # override exactly ONE named gate, so a cell whose refusals failed for
    # assorted reasons has no gate to name and cannot produce one.
    gate_signatures: frozenset = frozenset()
    # Which way a proposed change would have got it wrong, in markets.
    winners_blocked: int = 0
    losers_blocked: int = 0
    winners_admitted: int = 0
    losers_admitted: int = 0

    @property
    def applies_to(self) -> str:
        return self.key.split("|")[1] if "|" in self.key else "accept"

    @property
    def context(self) -> str:
        return self.key.split("|")[0]

    def payload(self) -> dict:
        """The shape `intelligence_policy.Policy` reads back."""
        return {
            "n": self.n, "markets": self.markets, "days": self.days,
            "mean": round(self.shrunk, 6),
            "observed_mean": round(self.mean, 6),
            "low": round(self.low, 6), "high": round(self.high, 6),
            "win_rate": round(self.win_rate, 6),
            "mean_ask": round(self.mean_ask, 6),
            "action": self.action, "gate": self.gate,
            "action_reason": self.action_reason,
            "delta": self.delta, "delta_reason": self.delta_reason,
            "probability": self.probability,
            "promoted": self.promoted,
            "validate_n": self.validate_n,
            "validate_mean": self.validate_mean,
            "winners_blocked": self.winners_blocked,
            "losers_blocked": self.losers_blocked,
            "winners_admitted": self.winners_admitted,
            "losers_admitted": self.losers_admitted,
        }


def fit_arms(rows: list[dict], reward, min_n: int = 1) -> dict[str, ArmFit]:
    """Group rows into context-action cells and measure each one.

    `markets` is counted separately from `n` throughout. They differ whenever a
    window contributed both an UP and a DOWN decision, and a cell whose sample
    is twelve rows over five markets has five independent things in it, not
    twelve.
    """
    groups: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        groups[context_key_of(row)].append(row)
    out: dict[str, ArmFit] = {}
    for key, items in groups.items():
        if len(items) < min_n:
            continue
        values = [reward(r) for r in items]
        days = [day_of(r["window_open"]) for r in items]
        low, high = cluster_ci(values, days)
        wins = sum(1 for r in items if r.get("won"))
        # PER-ROW CALIBRATION ERROR: outcome minus the probability the price
        # predicted, at the instant the decision was made. Bootstrapped by day
        # exactly like the money, because the same clustering applies.
        gaps = [
            (1.0 if r.get("won") else 0.0) - float(r["our_ask"]) for r in items
        ]
        gap_low, gap_high = cluster_ci(gaps, days)
        out[key] = ArmFit(
            key=key, n=len(values),
            markets=len({r["window_open"] for r in items}),
            days=len(set(days)),
            mean=sum(values) / len(values), low=low, high=high,
            wins=wins, win_rate=wins / len(items),
            mean_ask=sum(float(r["our_ask"]) for r in items) / len(items),
            calibration=sum(gaps) / len(gaps),
            calibration_low=gap_low, calibration_high=gap_high,
            gate_signatures=frozenset(
                (r.get("failed_gates") or "").strip()
                for r in items if not r.get("rule_match")
            ),
        )
    return out


def context_key_of(row: dict) -> str:
    """`"<context>|accept"` or `"<context>|reject"`, from whichever the row has.

    A live row already carries the key the order path computed, verbatim. A
    corpus row carries the BRTI feature columns and the key is derived from the
    SAME function the order path calls. Two routes to one definition, never two
    definitions.
    """
    context = row.get("context_key")
    if not context:
        from .adaptive import brti_context_of

        context = str(brti_context_of(row))
    leg = "accept" if row.get("rule_match") else "reject"
    return f"{context}|{leg}"


def chronological_split(rows: list[dict],
                        train_fraction: float = TRAIN_FRACTION,
                        validate_fraction: float = VALIDATE_FRACTION):
    """(train, validate, holdout), oldest first, never shuffled.

    SPLIT BY MARKET, NOT BY ROW. A window the model flipped inside contributes
    an UP row and a DOWN row, and cutting the list at a row index can land them
    on opposite sides of the boundary - which puts the same market's outcome in
    both the fit and the check of the fit. That is leakage, it is invisible
    (both slices look the right size), and it flatters exactly the cells that
    contain flipped windows.

    So the boundary falls between MARKETS. Every row of a window travels with
    its window. The slice sizes are therefore approximate rather than exact,
    which is the correct trade: a split that is 55.02% instead of 55% is not a
    problem, and a market in two splits is.
    """
    ordered = sorted(rows, key=lambda r: (r["window_open"], r.get("side") or ""))
    markets = sorted({r["window_open"] for r in ordered})
    count = len(markets)
    a = int(count * train_fraction)
    b = int(count * (train_fraction + validate_fraction))
    train_markets = set(markets[:a])
    validate_markets = set(markets[a:b])
    train, validate, holdout = [], [], []
    for row in ordered:
        window = row["window_open"]
        if window in train_markets:
            train.append(row)
        elif window in validate_markets:
            validate.append(row)
        else:
            holdout.append(row)
    return train, validate, holdout


# -------------------------------------------------------------- training


@dataclass
class TrainingReport:
    """Everything a person needs to audit a run without re-running it."""

    rows: int = 0
    markets: int = 0
    train_n: int = 0
    validate_n: int = 0
    holdout_n: int = 0
    arms_fitted: int = 0
    arms_eligible_for_confidence: int = 0
    arms_with_confidence: int = 0
    # How many confidence arms would survive the multiplicity widening the
    # EXECUTION bar applies. Reported because the confidence bar deliberately
    # does not apply it, and the honest way to hold a weaker bar is to publish
    # what the stronger one would have said.
    confidence_surviving_multiplicity: int = 0
    candidates_examined: int = 0
    promoted: int = 0
    baseline_accept: float = 0.0
    baseline_reject: float = 0.0
    training_cutoff_ms: int = 0
    data_end_ms: int = 0
    notes: list[str] = field(default_factory=list)

    def payload(self) -> dict:
        return dict(self.__dict__)


@dataclass
class TrainingResult:
    policy: Policy
    report: TrainingReport
    arms: dict[str, ArmFit] = field(default_factory=dict)
    ok: bool = True
    error: str = ""


def train(
    rows: list[dict],
    *,
    fingerprint: str,
    feature_definitions: dict,
    feature_version: str,
    provenance: dict | None = None,
    slippage: float = 0.0,
    forward: dict[str, dict] | None = None,
    now_ms: int | None = None,
    min_evidence: int = MIN_PROMOTION_N,
) -> TrainingResult:
    """Fit a Kalshi-native policy and decide what it is allowed to do.

    `forward` maps a context key to whatever live forward evaluation has
    already accumulated for it: `{"changes": n, "incremental": dollars}`. It can
    only ever VETO a promotion - forward evidence that contradicts the fit is a
    reason not to act, never a reason to act on less.
    """
    now_ms = int(time.time() * 1000) if now_ms is None else now_ms
    report = TrainingReport()
    if not rows:
        return TrainingResult(
            Policy(), report, ok=False,
            error="no Kalshi-native rows; nothing to fit",
        )

    def reward(row):
        return reward_for(row, slippage)

    train_rows, validate_rows, holdout_rows = chronological_split(rows)
    report.rows = len(rows)
    report.markets = len({r["window_open"] for r in rows})
    report.train_n = len(train_rows)
    report.validate_n = len(validate_rows)
    report.holdout_n = len(holdout_rows)
    # A SPLIT THAT CANNOT VALIDATE ANYTHING IS NOT A TRAINING RUN. Below
    # MIN_VALIDATE_N there is no out-of-sample leg for any arm to clear, so a
    # fit would produce an artefact whose every adjustment is unvalidated by
    # construction - and would then be compared against the running policy and
    # possibly activated. Refusing here is what keeps a fresh install from
    # fitting a policy on its first afternoon.
    if not train_rows or len(validate_rows) < MIN_VALIDATE_N:
        return TrainingResult(
            Policy(), report, ok=False,
            error=f"split too small to validate (train {len(train_rows)}, "
                  f"validate {len(validate_rows)} < {MIN_VALIDATE_N})",
        )
    report.training_cutoff_ms = int(train_rows[-1]["window_open"])
    report.data_end_ms = int(rows[-1]["window_open"]) if rows else 0

    taken = [reward(r) for r in train_rows if r["rule_match"]]
    refused = [reward(r) for r in train_rows if not r["rule_match"]]
    report.baseline_accept = round(sum(taken) / len(taken), 6) if taken else 0.0
    report.baseline_reject = (
        round(sum(refused) / len(refused), 6) if refused else 0.0
    )
    priors = {"accept": report.baseline_accept, "reject": report.baseline_reject}

    arms = fit_arms(train_rows, reward)
    validate_arms = fit_arms(validate_rows, reward)
    report.arms_fitted = len(arms)

    # PASS ONE: how many cells are even eligible to be examined as execution
    # candidates. The multiplicity widening needs this count before any single
    # arm can be judged, because looking at k cells and keeping the best is k
    # chances to be fooled - so the correction must be the same for all of them.
    examined = [
        key for key, arm in arms.items()
        if arm.n >= MIN_CANDIDATE_N
        and _proposed_action(arm, priors) != NEUTRAL
    ]
    report.candidates_examined = len(examined)
    widening = max(1.0, math.sqrt(max(1, len(examined))))

    for key, arm in arms.items():
        val = validate_arms.get(key)
        arm.validate_n = val.n if val else 0
        arm.validate_mean = round(val.mean, 6) if val else None
        arm.shrunk = shrink(arm.mean, arm.n, priors[arm.applies_to])
        _calibrate_confidence(arm, validate_arms)
        _propose_execution(arm, priors, validate_arms, widening,
                           forward or {}, min_evidence)
        _count_error_directions(arm, train_rows, validate_rows)

    report.arms_with_confidence = sum(1 for a in arms.values() if a.delta)
    eligible = [
        a for a in arms.values()
        if a.n >= MIN_CONFIDENCE_N and a.days >= 2
    ]
    report.arms_eligible_for_confidence = len(eligible)
    confidence_widening = max(1.0, math.sqrt(max(1, len(eligible))))
    report.confidence_surviving_multiplicity = sum(
        1 for a in eligible
        if a.delta and _survives_widening(a.low, a.high, confidence_widening)
    )
    if report.arms_with_confidence and not report.confidence_surviving_multiplicity:
        report.notes.append(
            f"{report.arms_with_confidence} confidence arm(s) clear their own "
            f"day-clustered interval; NONE survives the multiplicity widening "
            f"applied to execution decisions across "
            f"{report.arms_eligible_for_confidence} eligible cells. Confidence "
            f"is label-only and is not held to that bar, but the weaker "
            f"standard is the reason it is active and the stronger one is not."
        )
    promoted = [a for a in arms.values() if a.promoted]
    report.promoted = len(promoted)
    if not promoted:
        report.notes.append(
            "no arm cleared the promotion bar; the policy is confidence-only "
            "and every execution action is neutral"
        )

    policy = Policy(
        version=f"kalshi-{feature_version}-{now_ms // 1000}",
        model_version="arms-shrunk-2",
        feature_version=feature_version,
        training_cutoff_ms=report.training_cutoff_ms,
        data_end_ms=report.data_end_ms,
        arms={key: arm.payload() for key, arm in arms.items()},
        vetoes_enabled=any(a.promoted and a.action == VETO for a in arms.values()),
        admissions_enabled=any(
            a.promoted and a.action == ADMIT for a in arms.values()
        ),
        min_evidence=min_evidence,
        feature_fingerprint=fingerprint,
        feature_definitions=feature_definitions,
        notes=_notes(report, provenance or {}),
        # The provenance travels WITH the artefact, not in a sibling file that
        # can be lost or edited apart from it.
        provenance=provenance or {},
        report=report.payload(),
    )
    return TrainingResult(policy, report, arms)


def _proposed_action(arm: ArmFit, priors: dict) -> str:
    """Which direction this cell points, before any evidence bar is applied."""
    adjusted = shrink(arm.mean, arm.n, priors[arm.applies_to])
    if arm.applies_to == "accept" and adjusted < 0:
        return VETO
    if arm.applies_to == "reject" and adjusted > 0:
        return ADMIT
    return NEUTRAL


def _calibrate_confidence(arm: ArmFit, validate_arms: dict) -> None:
    """May this cell re-rate what the operator is shown, and by how much?

    CONFIDENCE IS ABOUT WINNING, NOT ABOUT PROFIT. Those come apart, and
    conflating them was this layer's error: an 89c contract that wins 88% of
    the time loses money on every trade and is still a high-confidence
    directional call. Sizing the confidence label off dollars would have shown
    "confidence lowered" on setups the market gets right nine times in ten,
    purely because they are expensive.

    So the quantity here is the CALIBRATION ERROR - observed win rate minus the
    probability the price implied - and nothing else. Profit drives veto and
    admission, which are decisions about money; this drives a label, which is a
    statement about the outcome.

    THE BAR. Confidence moves a label, so it is not held to the multiplicity
    widening the execution bar applies - a delta is computed for EVERY eligible
    cell rather than the best of k being picked to act on, so there is no
    selection to correct. But not being chosen is not the same as not needing
    validation, and it gets validated:

        n >= MIN_CONFIDENCE_N        enough rows to measure a few points of
                                     calibration error at all
        days >= 2                    an interval needs more than one afternoon
        train interval clear of zero the error is real in the fitted period
        validate n >= MIN_VALIDATE_N enough out-of-sample rows to check it
        validate agrees in sign      and it was still there afterwards

    `train` also records how many would survive the widened execution test, so
    the weaker bar is a published number rather than an argument.

    An unsupported cell gets delta 0 and a reason naming what it lacked,
    because a silent zero is indistinguishable from a cell nobody looked at.
    """
    val = validate_arms.get(arm.key)
    arm.validate_calibration = (
        round(val.calibration, 6) if val is not None else None
    )
    if arm.n < MIN_CONFIDENCE_N:
        arm.delta, arm.delta_reason = 0, (
            f"thin evidence for calibration (n={arm.n} < {MIN_CONFIDENCE_N})"
        )
        return
    if arm.days < 2:
        arm.delta, arm.delta_reason = 0, (
            f"evidence spans {arm.days} day; no interval can be formed"
        )
        return
    if not excludes_zero(arm.calibration_low, arm.calibration_high):
        arm.delta, arm.delta_reason = 0, (
            f"calibration {arm.calibration:+.4f} interval "
            f"[{arm.calibration_low:+.4f},{arm.calibration_high:+.4f}] "
            f"includes zero - the price is not measurably wrong here"
        )
        return
    if val is None or val.n < MIN_VALIDATE_N:
        arm.delta, arm.delta_reason = 0, (
            f"calibration {arm.calibration:+.4f} not validated out of sample "
            f"(validate n={val.n if val else 0} < {MIN_VALIDATE_N})"
        )
        return
    if (arm.calibration > 0) != (val.calibration > 0):
        arm.delta, arm.delta_reason = 0, (
            f"calibration {arm.calibration:+.4f} did not hold out of sample "
            f"(validate {val.calibration:+.4f}, opposite sign)"
        )
        return
    # IN PROBABILITY POINTS, on the same 0-100 scale the confidence label is
    # scored in. A cell that wins five points more often than its price implies
    # moves the label by five. No dollar figure enters this.
    arm.delta = max(-DELTA_CLAMP, min(DELTA_CLAMP,
                                      int(round(arm.calibration * 100))))
    # THE CALIBRATED PROBABILITY, shrunk toward the price. The price is the
    # market's own estimate and a good one (FINDINGS 36: no model beats the
    # ask), so a few hundred observations should move it a little, not replace
    # it.
    arm.probability = round(shrink(arm.win_rate, arm.n, arm.mean_ask), 6)
    if arm.delta == 0:
        arm.delta_reason = (
            f"calibration {arm.calibration:+.4f} rounds to no change"
        )
    else:
        arm.delta_reason = (
            f"wins {arm.win_rate:.3f} against an implied {arm.mean_ask:.3f} "
            f"({arm.calibration:+.4f}) over n={arm.n} in {arm.days} days; "
            f"interval [{arm.calibration_low:+.4f},"
            f"{arm.calibration_high:+.4f}] clear of zero; validate n={val.n} "
            f"{val.calibration:+.4f} agrees"
        )


def _propose_execution(arm: ArmFit, priors: dict,
                       validate_arms: dict[str, ArmFit], widening: float,
                       forward: dict[str, dict], min_evidence: int) -> None:
    """May this cell refuse or admit an ORDER? The full bar, stated in one place.

    Every failure sets `action_reason` to the specific thing that was missing.
    "Unsupported" with no reason is how a layer stops being auditable.
    """
    proposed = _proposed_action(arm, priors)
    if proposed == NEUTRAL:
        arm.action, arm.action_reason = NEUTRAL, (
            "cell does not point against the base decision"
        )
        return
    # A veto refuses a qualified setup; an admission rescues a refused one and
    # must name the single gate it overrides. Where a refused cell has more than
    # one distinct failing gate there is no single gate to name, and an
    # admission that overrode "whatever was wrong" would be an admission that
    # overrode the capital checks too.
    arm.action = NEUTRAL
    if proposed == ADMIT:
        gates = {g for g in arm.gate_signatures if g}
        if len(gates) != 1 or "," in next(iter(gates)):
            arm.action_reason = (
                f"admit proposed; the cell's refusals name "
                f"{len(gates)} distinct gate signatures, so there is no single "
                f"gate an exception could override"
            )
            return
        arm.gate = next(iter(gates))
    val = validate_arms.get(arm.key)
    if arm.n < MIN_PROMOTION_N:
        arm.action_reason = (
            f"{proposed} proposed; train n={arm.n} < {MIN_PROMOTION_N}"
        )
        return
    if val is None or val.n < MIN_VALIDATE_N:
        arm.action_reason = (
            f"{proposed} proposed; validate n={val.n if val else 0} < "
            f"{MIN_VALIDATE_N}"
        )
        return
    if (arm.shrunk > 0) != (val.mean > 0):
        arm.action_reason = (
            f"{proposed} proposed; train {arm.shrunk:+.4f} and validate "
            f"{val.mean:+.4f} disagree in sign"
        )
        return
    mid = (val.high + val.low) / 2
    half = (val.high - val.low) / 2 * widening
    if not (mid - half > 0 or mid + half < 0):
        arm.action_reason = (
            f"{proposed} proposed; validation interval widened for "
            f"multiplicity [{mid - half:+.4f},{mid + half:+.4f}] includes zero"
        )
        return
    live = forward.get(arm.key) or {}
    changes = int(live.get("changes") or 0)
    incremental = float(live.get("incremental") or 0.0)
    if changes >= MIN_WITHDRAWAL_N and incremental < 0:
        arm.action_reason = (
            f"{proposed} proposed and validated, but forward evidence "
            f"contradicts it ({incremental:+.4f} over {changes} changes)"
        )
        return
    arm.action = proposed
    arm.promoted = True
    if proposed == VETO:
        arm.gate = None          # a veto overrides nothing; it refuses
    arm.action_reason = (
        f"{proposed} cleared the bar: train n={arm.n} {arm.shrunk:+.4f}, "
        f"validate n={val.n} {val.mean:+.4f}, widened interval clear of zero"
    )


def _count_error_directions(arm: ArmFit, train_rows: list[dict],
                            validate_rows: list[dict]) -> None:
    """How a proposed change would have been WRONG, in markets, not dollars.

    Dollars alone hide the shape of a mistake: a veto that blocks nine losers
    and one very large winner can look identical to one that blocks five of
    each. The operator asked for winners wrongly blocked and losers newly
    admitted to be visible, so they are counted as markets and kept beside the
    money rather than folded into it.
    """
    key = arm.key
    for row in list(train_rows) + list(validate_rows):
        if context_key_of(row) != key:
            continue
        won = bool(row.get("won"))
        if arm.applies_to == "accept":
            if won:
                arm.winners_blocked += 1
            else:
                arm.losers_blocked += 1
        else:
            if won:
                arm.winners_admitted += 1
            else:
                arm.losers_admitted += 1


def _notes(report: TrainingReport, provenance: dict) -> str:
    lines = [
        f"Kalshi-only fit over {report.markets} markets "
        f"({report.rows} decisions), train {report.train_n} / validate "
        f"{report.validate_n} / holdout {report.holdout_n}, chronological.",
        f"Baseline on the train split: rule took {report.baseline_accept:+.4f}/ct, "
        f"refused {report.baseline_reject:+.4f}/ct.",
        f"{report.arms_with_confidence} of {report.arms_fitted} arms carry a "
        f"confidence adjustment; {report.promoted} cleared the execution bar "
        f"out of {report.candidates_examined} examined.",
    ]
    if provenance.get("sources"):
        lines.append("Sources: " + ", ".join(provenance["sources"]) + ".")
    # WHAT THESE ROWS ARE, stated on the artefact so it cannot be mislaid.
    # Three things that are not the same, and this fits the FIRST:
    #   signal decision    the rule qualified at this poll
    #   order eligibility  AND the price held the band 60s, no position is
    #                      open, and the day's limits allow it
    #   execution          AND an order was submitted, and filled
    # The archive leg reconstructs signal decisions against the deployed gates
    # and cannot model the band-hold timer or the one-position rule, which
    # existed only live. The live leg carries real fill status and real fees
    # where an order was placed. Anything priced at a recorded ask with no fill
    # behind it is a SIMULATED fill and is labelled one.
    lines.append(
        "These rows are SIGNAL decisions scored against the deployed gates. "
        "Order eligibility (the 60s band hold, the one-position rule, the "
        "daily floor) and execution are separate and are not modelled in the "
        "archive leg; the live leg carries the broker's own fill and fee where "
        "an order existed."
    )
    lines.extend(report.notes)
    lines.append(
        "Execution actions additionally require the operator's two switches "
        "(intelligence_mode and intelligence_authorised); evidence alone never "
        "grants them, and no code path raises them."
    )
    return " ".join(lines)


# ------------------------------------------------------------ comparison


@dataclass
class Comparison:
    """New against current, on the same rows, scored the same way."""

    rows: int = 0
    markets: int = 0
    new_pnl: float = 0.0
    current_pnl: float = 0.0
    new_changes: int = 0
    current_changes: int = 0
    winners_blocked: int = 0
    losers_blocked: int = 0
    winners_admitted: int = 0
    losers_admitted: int = 0
    current_valid: bool = False
    verdict: str = ""

    @property
    def delta(self) -> float:
        return round(self.new_pnl - self.current_pnl, 6)

    def payload(self) -> dict:
        data = dict(self.__dict__)
        data["delta"] = self.delta
        return data


def policy_is_valid(policy: Policy, *, fingerprint: str,
                    feature_version: str) -> tuple[bool, str]:
    """Could this artefact act at all? The same checks the live path makes.

    Kept here so the activation decision and the order path cannot disagree
    about what "valid" means - the retired artefact was inert only because two
    action flags happened to be off, and the label it wore said otherwise.
    """
    from . import intelligence_policy as intel

    if not policy.active:
        return False, "no active policy"
    if policy.mislabelled:
        return False, (
            f"declares {policy.feature_version} but its arms are keyed on "
            f"{policy.keyed_feature_family}"
        )
    if policy.keyed_feature_family in intel.RETIRED_FEATURE_FAMILIES:
        return False, f"keyed on retired features {policy.keyed_feature_family}"
    if policy.feature_version in intel.RETIRED_FEATURE_VERSIONS:
        return False, f"{policy.feature_version} is retired"
    if policy.feature_version != feature_version:
        return False, (
            f"feature version {policy.feature_version} != {feature_version}"
        )
    if policy.feature_fingerprint != fingerprint:
        return False, (
            f"feature contract mismatch (fp={policy.feature_fingerprint or 'absent'}"
            f" != {fingerprint})"
        )
    return True, "valid"


def compare(new: Policy, current: Policy, rows: list[dict], *,
            fingerprint: str, feature_version: str,
            slippage: float = 0.0) -> Comparison:
    """Replay both policies over the same rows and score the difference.

    Scored with the SAME `decide` function the order path calls, so a policy
    cannot be evaluated doing something it would not do live. Where a policy
    changes a decision the row is repriced accordingly: a veto banks nothing, an
    admission takes the trade at the recorded ask as a SIMULATED fill.
    """
    from . import intelligence_policy as intel

    out = Comparison(rows=len(rows),
                     markets=len({r["window_open"] for r in rows}))
    ok, _why = policy_is_valid(current, fingerprint=fingerprint,
                               feature_version=feature_version)
    out.current_valid = ok

    for row in rows:
        key = context_key_of(row)
        qualified = bool(row.get("rule_match"))
        value = reward_for(row, slippage)
        gates = tuple(
            g.strip() for g in (row.get("failed_gates") or "").split(",")
            if g.strip()
        )
        for policy, is_new in ((new, True), (current, False)):
            verdict = intel.decide(
                context_key=key, base_qualified=qualified, failed_gates=gates,
                ask=float(row["our_ask"]), policy=policy, enabled=True,
                features_ok=True,
            )
            takes = verdict.qualifies
            pnl = value if takes else 0.0
            if is_new:
                out.new_pnl += pnl
                if verdict.final_action in (intel.VETO, intel.ADMIT):
                    out.new_changes += 1
                    won = bool(row.get("won"))
                    if verdict.final_action == intel.VETO:
                        out.winners_blocked += int(won)
                        out.losers_blocked += int(not won)
                    else:
                        out.winners_admitted += int(won)
                        out.losers_admitted += int(not won)
            else:
                out.current_pnl += pnl
                if verdict.final_action in (intel.VETO, intel.ADMIT):
                    out.current_changes += 1
    out.new_pnl = round(out.new_pnl, 6)
    out.current_pnl = round(out.current_pnl, 6)
    return out


def activation_decision(new: Policy, current: Policy, comparison: Comparison, *,
                        fingerprint: str, feature_version: str,
                        tolerance: float = 0.0) -> tuple[bool, str]:
    """Should the new artefact replace the running one?

    THREE CASES, and the first is the one that matters here.

      * The running artefact CANNOT ACT - retired, mislabelled, fitted under
        different definitions, or absent. Any valid Kalshi-native policy is an
        improvement on an artefact whose every answer is a refusal, and it
        activates without needing to beat it on P&L. Refusing to replace it
        until it wins a horse race is how a retired policy stays deployed.
      * The new artefact is itself invalid. It never activates, whatever it
        scores. The running one stays.
      * Both are valid. The new one activates only if it does not regress on the
        validation slice, because a fresher fit is not automatically a better
        one.
    """
    ok, why = policy_is_valid(new, fingerprint=fingerprint,
                              feature_version=feature_version)
    if not ok:
        return False, f"new policy rejected: {why}"
    if not comparison.current_valid:
        return True, (
            "running artefact cannot act (it is retired, mislabelled or fitted "
            "under different feature definitions); replaced by a valid "
            "Kalshi-native policy"
        )
    if comparison.delta < -abs(tolerance):
        return False, (
            f"regression on the validation slice: {comparison.delta:+.4f} "
            f"against the running policy"
        )
    return True, (
        f"no regression on the validation slice ({comparison.delta:+.4f}); "
        f"activating the fresher fit"
    )


# ------------------------------------------------------------ withdrawal


def candidate_payload(result: TrainingResult, *, fingerprint: str,
                      feature_definitions: dict, feature_version: str,
                      now_ms: int) -> dict:
    """The frozen candidate artefact: cells worth WATCHING, forward.

    Refitting the policy without refitting these would leave the forward
    evaluation permanently frozen on whatever the first run happened to find,
    so a cell that becomes interesting next month would never be watched. That
    is the "no adjustment has earned promotion, therefore there is nothing to
    forward-test" mistake wearing a different hat.

    IDS ARE DERIVED FROM THE CONTEXT, not from position in a list. The previous
    scheme numbered them `c01`, `c02`... in descending order of sample size, so
    `c01` meant a different cell after every refit - and `candidate_evaluations`
    is keyed on `(window_open, candidate_id)`, which would have silently
    attributed one cell's forward record to another. A content-derived id means
    the same cell keeps its name for as long as it exists and a different cell
    can never inherit it.
    """
    import hashlib

    candidates = []
    for key, arm in sorted(result.arms.items(), key=lambda kv: -kv[1].n):
        if arm.n < MIN_CANDIDATE_N:
            continue
        proposed = arm.action
        if proposed == NEUTRAL:
            # A cell with no proposed change still has nothing to watch: the
            # candidate table measures DISAGREEMENT with the base rule, and a
            # row that agrees by construction would dilute every hit rate.
            proposed = _direction_only(arm)
        if proposed == NEUTRAL:
            continue
        digest = hashlib.sha1(key.encode("utf-8")).hexdigest()[:8]
        candidates.append({
            "candidate_id": f"c{digest}",
            "context": key,
            "proposed_action": proposed,
            "train_n": arm.n,
            "train_markets": arm.markets,
            "train_days": arm.days,
            "train_mean": round(arm.shrunk, 6),
            "train_low": round(arm.low, 6),
            "train_high": round(arm.high, 6),
            "validate_n": arm.validate_n,
            "validate_mean": arm.validate_mean,
            "promotes": arm.promoted,
            "delta": arm.delta,
            "reason": arm.action_reason,
        })
    return {
        "version": f"brti-cand-{now_ms // 1000}",
        "feature_version": feature_version,
        "feature_fingerprint": fingerprint,
        "feature_definitions": feature_definitions,
        "built_ms": now_ms,
        "data_end_ms": result.report.data_end_ms,
        "training_cutoff_ms": result.report.training_cutoff_ms,
        "min_candidate_n": MIN_CANDIDATE_N,
        "min_promotion_n": MIN_PROMOTION_N,
        "candidates": candidates,
    }


def _direction_only(arm: ArmFit) -> str:
    """Which way the cell points, ignoring every evidence bar.

    Used only to decide whether a cell is worth WATCHING. Watching is free and
    controls nothing; the bars exist for the question of whether it may act.
    """
    if arm.applies_to == "accept" and arm.shrunk < 0:
        return VETO
    if arm.applies_to == "reject" and arm.shrunk > 0:
        return ADMIT
    return NEUTRAL


@dataclass
class Withdrawal:
    key: str
    changes: int
    incremental: float
    reason: str


def deteriorated(policy: Policy, forward: dict[str, dict], *,
                 min_changes: int = MIN_WITHDRAWAL_N) -> list[Withdrawal]:
    """Which ACTIVE execution arms have stopped working, by the stated rule.

    The criteria are fixed in advance so that withdrawal is not a judgement made
    after seeing the number:

        the arm is ACTIVE      it is actually changing orders
        >= min_changes         enough forward markets it actually changed
        incremental < 0        it has cost money against the unchanged rule
        below its own floor    the forward mean is worse than the bottom of the
                               interval its promotion was granted on - it is
                               not merely unlucky, it is outside what the
                               evidence ever supported

    Confidence-only arms are not withdrawn here. They cannot change an order, so
    the thing being protected against does not apply to them; they are refitted
    on the next run like everything else.
    """
    out = []
    for key, arm in policy.arms.items():
        if not arm.get("promoted") or arm.get("action") in (None, NEUTRAL):
            continue
        live = forward.get(key) or {}
        changes = int(live.get("changes") or 0)
        if changes < min_changes:
            continue
        incremental = float(live.get("incremental") or 0.0)
        if incremental >= 0:
            continue
        per_change = incremental / changes
        floor = float(arm.get("low") or 0.0)
        if per_change >= floor:
            continue
        out.append(Withdrawal(
            key=key, changes=changes, incremental=round(incremental, 6),
            reason=(
                f"{incremental:+.4f} over {changes} forward changes "
                f"({per_change:+.4f}/change) is below the bottom of the "
                f"interval it was promoted on ({floor:+.4f})"
            ),
        ))
    return out


def withdraw(policy: Policy, withdrawals: list[Withdrawal]) -> Policy:
    """Return the policy with those arms made neutral. Evidence is preserved.

    The arm is NOT deleted. Its measurements stay exactly as they were and a
    reason is written beside them, because the record of an adjustment that was
    tried and withdrawn is evidence - and deleting it would let the next
    training run rediscover it as though for the first time.
    """
    if not withdrawals:
        return policy
    keys = {w.key: w for w in withdrawals}
    for key, arm in policy.arms.items():
        if key in keys:
            arm["action"] = NEUTRAL
            arm["promoted"] = False
            arm["gate"] = None
            arm["action_reason"] = f"withdrawn: {keys[key].reason}"
    policy.vetoes_enabled = any(
        a.get("promoted") and a.get("action") == VETO
        for a in policy.arms.values()
    )
    policy.admissions_enabled = any(
        a.get("promoted") and a.get("action") == ADMIT
        for a in policy.arms.values()
    )
    return policy
