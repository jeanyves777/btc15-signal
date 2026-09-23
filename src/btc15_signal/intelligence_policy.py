"""ONE decision function, used by historical replay and by the live order path.

The whole point of this module is that there is only one of it. A policy
evaluated in a backtest and a policy running live must be the same code
reading the same frozen artefact, or the evaluation describes something the
bot does not do - which is the failure FINDINGS 13 records, where published
band tables measured a side convention the service never ran.

WHAT IT MAY AND MAY NOT DO.

    confidence   always allowed. Re-rates a setup. CANNOT admit a signal that
                 a strategy gate still blocks.
    veto         may refuse a QUALIFIED setup, only where the policy was
                 validated to do so, and only naming the pattern.
    admit        may pass a REJECTED setup, only through an exception that
                 NAMES the single gate it overrides.

An exception overrides ONE strategy gate. It cannot touch capital, exposure,
the daily loss floor, duplicate-order protection, retries, execution hours,
freshness or order reconciliation - those are not strategy opinions, they are
the things that stop a bad day becoming an unbounded one.

ECONOMICS ARE RE-EVALUATED AT SUBMISSION. An approval at 0.78 is not an
approval at 0.88: the same pattern at a worse price is a different trade, and
`reprice` exists so the order path can ask again with the price it is actually
about to pay.

NEUTRAL IS A REAL ANSWER. Missing features, a stale policy, thin evidence, or
intelligence switched off all produce NEUTRAL, which leaves the base strategy
exactly as it was. A layer that cannot say "I do not know" will eventually say
something worse.
"""

import json
import math
from dataclasses import dataclass, field
from pathlib import Path

NEUTRAL = "neutral"
ALLOW = "allow"
VETO = "veto"
ADMIT = "admit"

# Bumped when the FEATURE definitions change. A policy trained on a different
# feature version must not be applied - the numbers would be arithmetic over
# quantities that no longer mean the same thing.
FEATURE_VERSION = "brti-1"


@dataclass(frozen=True)
class Verdict:
    """Everything the archive needs to answer 'what did it change, and why?'"""

    base_qualified: bool
    failed_gates: tuple[str, ...]
    final_action: str
    reason: str
    confidence_delta: int = 0
    calibrated_probability: float | None = None
    expected_net: float | None = None
    evidence_n: int = 0
    uncertainty: float = 0.0
    model_version: str = "none"
    policy_version: str = "none"
    feature_version: str = FEATURE_VERSION
    training_cutoff_ms: int = 0
    context_key: str = ""
    overrides_gate: str | None = None
    # The price the approval was granted at, so repricing can tell a worse
    # fill from the one that was actually evaluated.
    decision_ask: float | None = None

    @property
    def changed(self) -> bool:
        return self.final_action in (VETO, ADMIT) or self.confidence_delta != 0

    @property
    def qualifies(self) -> bool:
        """The FINAL answer to 'may this trade', base plus intelligence."""
        if self.final_action == VETO:
            return False
        if self.final_action == ADMIT:
            return True
        return self.base_qualified


@dataclass
class Policy:
    """A frozen, versioned artefact. Never mutated in place while live."""

    version: str = "none"
    model_version: str = "none"
    feature_version: str = FEATURE_VERSION
    training_cutoff_ms: int = 0
    # context key -> {"n","mean","low","high","action","gate","delta"}
    arms: dict = field(default_factory=dict)
    vetoes_enabled: bool = False
    admissions_enabled: bool = False
    min_evidence: int = 120
    notes: str = ""

    @classmethod
    def load(cls, path: str | Path) -> "Policy":
        source = Path(path)
        if not source.exists():
            return cls()
        try:
            data = json.loads(source.read_text())
        except (ValueError, OSError):
            return cls()
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in data.items() if k in known})

    def save(self, path: str | Path) -> None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(json.dumps(self.__dict__, indent=2, sort_keys=True))

    @property
    def active(self) -> bool:
        return bool(self.arms) and self.version != "none"


def shrink(mean: float, n: int, prior: float, weight: float = 60.0) -> float:
    """Pull a sparse group toward the broader one it belongs to.

    A cell with eleven observations should not produce a bold adjustment just
    because eleven went well. The shrinkage weight is deliberately heavy: at
    n = weight the estimate is half prior, and only well past that does the
    cell speak mostly for itself.
    """
    if n <= 0:
        return prior
    return (n * mean + weight * prior) / (n + weight)


def decide(
    *,
    context_key: str,
    base_qualified: bool,
    failed_gates: tuple[str, ...],
    ask: float | None,
    policy: Policy,
    enabled: bool = True,
    features_ok: bool = True,
    now_ms: int = 0,
    max_age_ms: int = 0,
) -> Verdict:
    """The single decision. Pure, so replay and live cannot diverge."""
    base = Verdict(
        base_qualified=base_qualified, failed_gates=tuple(failed_gates),
        final_action=NEUTRAL, reason="", context_key=context_key,
        model_version=policy.model_version, policy_version=policy.version,
        training_cutoff_ms=policy.training_cutoff_ms,
    )
    if not enabled:
        return _with(base, reason="intelligence disabled")
    if not policy.active:
        return _with(base, reason="no active policy")
    if not features_ok:
        return _with(base, reason="features unavailable")
    if policy.feature_version != FEATURE_VERSION:
        return _with(
            base,
            reason=f"policy feature version {policy.feature_version} != "
                   f"{FEATURE_VERSION}",
        )
    if (
        max_age_ms and now_ms and policy.training_cutoff_ms
        and now_ms - policy.training_cutoff_ms > max_age_ms
    ):
        return _with(base, reason="policy is stale")
    arm = policy.arms.get(context_key)
    if not arm:
        return _with(base, reason="no evidence for this context")
    n = int(arm.get("n") or 0)
    if n < policy.min_evidence:
        return _with(base, reason=f"thin evidence (n={n})")

    mean = float(arm.get("mean") or 0.0)
    low, high = float(arm.get("low") or 0.0), float(arm.get("high") or 0.0)
    uncertainty = round((high - low) / 2, 6)
    delta = int(arm.get("delta") or 0)
    expected = None if ask is None else round(mean, 6)

    action = str(arm.get("action") or NEUTRAL)
    gate = arm.get("gate")

    if action == VETO and not policy.vetoes_enabled:
        action, gate = NEUTRAL, None
    if action == ADMIT and not policy.admissions_enabled:
        action, gate = NEUTRAL, None
    # An admission is meaningless unless the setup was actually refused, and
    # it may only rescue a setup refused for the ONE gate it names.
    if action == ADMIT and (base_qualified or tuple(failed_gates) != (gate,)):
        action, gate = NEUTRAL, None
    if action == VETO and not base_qualified:
        action = NEUTRAL

    reason = {
        VETO: "qualified setup matches an adverse pattern",
        ADMIT: f"validated exception to {gate}",
        NEUTRAL: "pattern supports the existing decision",
    }[action]

    return Verdict(
        base_qualified=base_qualified, failed_gates=tuple(failed_gates),
        final_action=action, reason=reason, confidence_delta=delta,
        calibrated_probability=arm.get("probability"),
        expected_net=expected, evidence_n=n, uncertainty=uncertainty,
        model_version=policy.model_version, policy_version=policy.version,
        training_cutoff_ms=policy.training_cutoff_ms, context_key=context_key,
        overrides_gate=gate, decision_ask=ask,
    )


def reprice(verdict: Verdict, ask: float, floor: float = 0.0) -> Verdict:
    """Ask again at the price actually about to be paid.

    An approval at one price is not an approval at any later price. If the
    expected value no longer clears the floor, the admission is withdrawn and
    the base verdict stands - which for an ADMIT means the trade does not
    happen.
    """
    if verdict.final_action != ADMIT:
        return verdict
    expected = verdict.expected_net
    if expected is None or verdict.decision_ask is None:
        return verdict
    # Every extra cent paid is a cent off the expected value, one for one: the
    # payout is fixed at a dollar, so the whole of a worse price comes out of
    # the edge. No modelling required, and none wanted - a repricing rule that
    # needs a model is a second place for the model to be wrong.
    if expected - max(0.0, ask - verdict.decision_ask) <= floor:
        return Verdict(
            base_qualified=verdict.base_qualified,
            failed_gates=verdict.failed_gates, final_action=NEUTRAL,
            reason=f"withdrawn at submission: {ask:.2f} no longer clears the floor",
            context_key=verdict.context_key,
            model_version=verdict.model_version,
            policy_version=verdict.policy_version,
            training_cutoff_ms=verdict.training_cutoff_ms,
            evidence_n=verdict.evidence_n,
        )
    return verdict


def _with(base: Verdict, reason: str) -> Verdict:
    return Verdict(
        base_qualified=base.base_qualified, failed_gates=base.failed_gates,
        final_action=NEUTRAL, reason=reason, context_key=base.context_key,
        model_version=base.model_version, policy_version=base.policy_version,
        training_cutoff_ms=base.training_cutoff_ms,
    )


def confidence_words(delta: int) -> str:
    if delta >= 8:
        return "raised"
    if delta <= -8:
        return "lowered"
    return "unchanged"


def wilson_half_width(rate: float, n: int) -> float:
    if n <= 0:
        return 1.0
    return 1.96 * math.sqrt(max(rate * (1 - rate), 1e-9) / n)
