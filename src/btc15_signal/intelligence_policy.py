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

from . import feature_contract

NEUTRAL = "neutral"
ALLOW = "allow"
VETO = "veto"
ADMIT = "admit"

# Bumped when the FEATURE definitions change. A policy trained on a different
# feature version must not be applied - the numbers would be arithmetic over
# quantities that no longer mean the same thing.
FEATURE_VERSION = "brti-1"

# RETIRED. Binance is out of every active signal, intelligence, training,
# evaluation and execution path. Records built on it stay readable as
# history and are excluded from active learning; a policy keyed on them can
# never act again, whatever its metadata claims.
RETIRED_FEATURE_VERSIONS = ("binance-1",)
RETIRED_FEATURE_FAMILIES = ("binance",)

# The band names each feature version keys its contexts with. These are how a
# policy's ACTUAL provenance is read, because the declared field can be wrong
# - and was: the deployed `v1` said `brti-1` over seven Binance arms.
BRTI_BAND_NAMES = ("bd<5", "bd5-10", "bd10-15", "bd15+")
BINANCE_BAND_NAMES = ("dist<1.5", "dist1.5-3", "dist3+")


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
    # WHAT THE EVIDENCE SAID, before the operator's permissions were applied.
    #
    # `final_action` and `confidence_delta` are what actually took effect, so
    # every existing consumer - the alert, the grading, the ledger - is reading
    # the thing that happened. These two record what the policy WOULD have done
    # with authority, which is the counterfactual the shadow record exists for:
    # without it, a layer running in shadow is indistinguishable from a layer
    # with nothing to say, and it could never earn promotion.
    evidence_action: str = ""
    evidence_delta: int = 0
    # Why the two differ, named. An adjustment silently dropped for want of a
    # permission looks exactly like an adjustment that was never proposed.
    authority: str = ""

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
    # The last observation in the whole dataset. Freshness is judged on
    # this, not on `training_cutoff_ms`: the cutoff exists to prove no
    # hindsight entered the fit, while staleness asks whether the market
    # has moved since the evidence ENDS.
    data_end_ms: int = 0
    # context key -> {"n","mean","low","high","action","gate","delta"}
    arms: dict = field(default_factory=dict)
    vetoes_enabled: bool = False
    admissions_enabled: bool = False
    min_evidence: int = 120
    notes: str = ""
    # What the fit was actually computed under. `feature_version` is a name;
    # these are the definitions. Absent means "cannot be shown to match",
    # which `compatible()` treats as incompatible.
    feature_fingerprint: str = ""
    feature_definitions: dict = field(default_factory=dict)
    # WHERE THE EVIDENCE CAME FROM, and what the fit found. Fields on the
    # artefact rather than a sibling file, because a provenance record that
    # can be moved or edited apart from the thing it describes is a record of
    # nothing. Both round-trip through `load`/`save`, so a policy recovered
    # from disk after a restart can still say what it was built from.
    provenance: dict = field(default_factory=dict)
    report: dict = field(default_factory=dict)

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

    @property
    def keyed_feature_family(self) -> str:
        """Which INSTRUMENT the arms are keyed on, read off the keys.

        A `feature_version` field is a claim; this is the evidence. The
        deployed `v1` artefact declared `brti-1` while every one of its seven
        arms was a Binance context (`dist3+`, never `bd10-15`) - a
        Binance-trained policy wearing a BRTI label, which is exactly what
        the version guard exists to stop and exactly what it could not see.
        It was inert only because both action flags happened to be off.

        Family, not version: `brti-2` would legitimately reuse the brti band
        names, and flagging that as a lie would make the guard cry wolf at
        every honest revision. The band names distinguish INSTRUMENTS, which
        is the confusion that actually loses money.

        "unknown" when the keys say nothing either way.
        """
        for key in self.arms:
            if any(band in key for band in BRTI_BAND_NAMES):
                return "brti"
            if any(band in key for band in BINANCE_BAND_NAMES):
                return "binance"
        return "unknown"

    @property
    def declared_feature_family(self) -> str:
        return self.feature_version.split("-")[0] if self.feature_version else ""

    @property
    def keyed_feature_version(self) -> str:
        """Back-compat alias reporting the family with its retired suffix."""
        family = self.keyed_feature_family
        return "binance-1" if family == "binance" else (
            self.feature_version if family == "brti" else "unknown"
        )

    @property
    def mislabelled(self) -> bool:
        """Do the declared instrument and the keyed instrument disagree?"""
        keyed = self.keyed_feature_family
        return keyed != "unknown" and keyed != self.declared_feature_family


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
    # THREE CHECKS, NOT ONE. The declared version is a claim; the keys are
    # the evidence; and a retired instrument is barred whichever it says.
    if policy.mislabelled:
        return _with(
            base,
            reason=f"policy declares {policy.feature_version} but its arms are "
                   f"keyed on {policy.keyed_feature_family} "
                   f"({FEATURE_VERSION} expected)",
        )
    if policy.keyed_feature_family in RETIRED_FEATURE_FAMILIES:
        return _with(
            base,
            reason=f"policy is keyed on retired features "
                   f"{policy.keyed_feature_family}-1",
        )
    if policy.feature_version in RETIRED_FEATURE_VERSIONS:
        return _with(base, reason=f"{policy.feature_version} is retired")
    if policy.feature_version != FEATURE_VERSION:
        return _with(
            base,
            reason=f"policy feature version {policy.feature_version} != "
                   f"{FEATURE_VERSION}",
        )
    # THE FINGERPRINT. The three checks above compare NAMES; this compares
    # the definitions themselves - source, units, cadence, smoothing,
    # lookbacks, cutoff rule and every band boundary. A policy fitted under a
    # 300s lookback and applied under a 600s one agrees on every name and is
    # still arithmetic over a different quantity. Unknown is not compatible:
    # an artefact that does not say what it was fitted under cannot be shown
    # to match, and that is the same as must-not-act.
    if not feature_contract.compatible(policy.feature_fingerprint):
        differences = feature_contract.CONTRACT.differences(
            policy.feature_definitions
        )
        detail = differences[0] if differences else (
            f"artefact fp={policy.feature_fingerprint or 'absent'} != "
            f"live fp={feature_contract.FINGERPRINT}"
        )
        return _with(base, reason=f"feature contract mismatch ({detail})")
    freshness_ms = policy.data_end_ms or policy.training_cutoff_ms
    if max_age_ms and now_ms and freshness_ms and now_ms - freshness_ms > max_age_ms:
        return _with(base, reason="policy is stale")
    arm = policy.arms.get(context_key)
    if not arm:
        return _with(base, reason="no evidence for this context")
    n = int(arm.get("n") or 0)
    if n < policy.min_evidence:
        # CARRY THE COUNT, not just the sentence. The reason string said
        # "thin evidence (n=53)" while `evidence_n` stayed 0, so any aggregate
        # over the column read every below-bar cell as having no evidence at
        # all - and "how close is this cell to the bar?" could only be answered
        # by parsing English out of a text field.
        return _with(base, reason=f"thin evidence (n={n})", evidence_n=n)

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


def authorise(verdict: Verdict, *, may_confidence: bool, may_veto: bool,
              may_admit: bool) -> Verdict:
    """Apply the operator's permissions to an evidence-based verdict.

    TWO INDEPENDENT THINGS HAVE TO BE TRUE for an adjustment to bite, and they
    are checked in two different places on purpose:

        evidence     the policy was VALIDATED to do this, in this cell. Decided
                     by `decide` above, from a frozen artefact.
        authority    the OPERATOR has granted this class of change. Decided
                     here, from two switches nothing in the code can raise.

    Confidence, veto and admission are three separate permissions because they
    are three different risks. A confidence adjustment re-rates a label; it can
    never admit a setup a gate blocked, never refuse one a gate allowed, and
    never change size. Granting it therefore says nothing about whether the
    layer may touch an order, and this function is where that distinction stops
    being a comment and becomes arithmetic.

    The evidence verdict is preserved on the returned object rather than
    overwritten, so the archive keeps the counterfactual that a shadow run is
    entirely made of.
    """
    action = verdict.final_action
    delta = verdict.confidence_delta
    notes = []
    if action == VETO and not may_veto:
        action, notes = NEUTRAL, [*notes, "veto not authorised"]
    if action == ADMIT and not may_admit:
        action, notes = NEUTRAL, [*notes, "admission not authorised"]
    if delta and not may_confidence:
        delta, notes = 0, [*notes, "confidence adjustment not authorised"]
    if action == verdict.final_action and delta == verdict.confidence_delta:
        # Nothing was withheld. Record the evidence anyway so every row carries
        # both columns and a query never has to guess why one is empty.
        return _replace(verdict, evidence_action=verdict.final_action,
                        evidence_delta=verdict.confidence_delta, authority="")
    return _replace(
        verdict, final_action=action, confidence_delta=delta,
        overrides_gate=verdict.overrides_gate if action == ADMIT else None,
        evidence_action=verdict.final_action,
        evidence_delta=verdict.confidence_delta,
        authority="; ".join(notes),
    )


def _replace(verdict: Verdict, **changes) -> Verdict:
    data = {
        field: getattr(verdict, field) for field in Verdict.__dataclass_fields__
    }
    data.update(changes)
    return Verdict(**data)


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


def _with(base: Verdict, reason: str, evidence_n: int = 0) -> Verdict:
    return Verdict(
        base_qualified=base.base_qualified, failed_gates=base.failed_gates,
        final_action=NEUTRAL, reason=reason, context_key=base.context_key,
        model_version=base.model_version, policy_version=base.policy_version,
        training_cutoff_ms=base.training_cutoff_ms, evidence_n=evidence_n,
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
