"""Outcome feedback over contexts: the reward function and the arm statistics.

THE SHAPE OF THE PROBLEM, named correctly. Adjusting confidence from observed
outcomes is a CONTEXTUAL BANDIT: a context (session, volatility, distance,
price, time remaining), an action (accept or reject, and at what confidence),
and a reward (realised net profit). It only becomes reinforcement learning when
the actions form a sequence whose earlier choices change the later state -
enter, hold, add, exit. This module is the bandit half, deliberately: the
sequence half needs the lifecycle work that is still unproven.

WHAT THE REWARD IS, AND WHAT IT IS NOT.

    accepted signal   ->  REALISED net, from a fill that actually happened
    rejected signal   ->  HYPOTHETICAL net, at the price we recorded

Those are not the same evidence and are never summed into one number here. A
rejected winner says the direction was right; it does not say a fill was
available at that price. FINDINGS 37 measured exactly how wrong that
assumption can be: a market whose ask never dipped wins 99.3%, while one where
a 2c limit actually filled wins 54.1%. Anything priced at a quote we never
crossed is marked `simulated` and carries an explicit fill assumption.

THE TWO ADJUSTMENTS ARE DIFFERENT THINGS, and conflating them is how a
confidence model quietly becomes an execution rule:

    confidence   how strongly a setup is rated. Cannot admit a signal that a
                 gate still blocks.
    execution    whether a setup may trade at all. Changes the gate.

A context whose evidence is thin or inconsistent gets a SMALL or NEUTRAL
adjustment, never a bold one. That is not timidity - FINDINGS 14 killed a
positive sub-band that looked strong pooled and died on a period split, and
the same trap is waiting for every pocket found here.
"""

import math
import random
from dataclasses import dataclass, field

# Wide buckets on purpose. A context split finely enough to be interesting is
# usually split finely enough to be noise: at four sessions x three volatility
# regimes x three distances x three prices there are already 108 cells, and
# the corpus has ~6,400 markets to spread across them.
DISTANCE_BANDS = ((0.0, 1.5, "dist<1.5"), (1.5, 3.0, "dist1.5-3"),
                  (3.0, 99.0, "dist3+"))
PRICE_BANDS = ((0.0, 0.70, "px<70"), (0.70, 0.85, "px70-85"),
               (0.85, 0.94, "px85-94"), (0.94, 1.0, "px94+"))


def _band(value: float, bands) -> str:
    for low, high, name in bands:
        if low <= value < high:
            return name
    return bands[-1][2]


@dataclass(frozen=True)
class Context:
    """The state an action is taken in. Hashable, so it keys the arms."""

    session: str
    vol_regime: str
    distance: str
    price: str

    def __str__(self) -> str:
        return f"{self.session} · {self.vol_regime} · {self.distance} · {self.price}"


def context_of(row: dict) -> Context:
    return Context(
        session=row.get("session") or "?",
        vol_regime=row.get("vol_regime") or "?",
        distance=_band(abs(row.get("normalized_distance") or 0.0), DISTANCE_BANDS),
        price=_band(row.get("our_ask") or 0.0, PRICE_BANDS),
    )


# ---------------------------------------------------------------- brti-1
#
# THE BRTI CONTEXT, and the reason it is defined HERE rather than in the
# training script.
#
# A BRTI distance is not a Binance distance. The reference is a 60-second
# mean - a low-pass filter - so the same market reads 10-20 on BRTI where
# Binance read 2-4, and BRTI volatility runs an order of magnitude lower
# (FINDINGS 43). Bands calibrated on one instrument put the other in the
# wrong cell every time, silently: the key still formats, it just names a
# pocket the policy never saw.
#
# That already happened once in a different guise - the live snapshot had no
# `session` or `vol_regime`, so the first real decision keyed "? · ? · ...".
# The lesson was that replay and live must derive the key from the SAME
# function, not from two that agree by inspection. So there is one function,
# and both sides call it.
BRTI_DISTANCE_BANDS = ((0.0, 5.0, "bd<5"), (5.0, 10.0, "bd5-10"),
                       (10.0, 15.0, "bd10-15"), (15.0, 999.0, "bd15+"))
BRTI_VOL_BANDS = ((0.0, 0.5, "low"), (0.5, 1.5, "mid"), (1.5, 9e9, "high"))

BRTI_FEATURE_VERSION = "brti-1"


def brti_vol_regime(volatility_bps: float) -> str:
    return _band(volatility_bps or 0.0, BRTI_VOL_BANDS)


def brti_context_of(row: dict) -> Context:
    """The canonical `brti-1` context key.

    `row` carries BRTI numbers under BRTI names - `brti_normalized_distance`
    and `brti_volatility_bps` - deliberately distinct from the Binance ones so
    a Binance row cannot be passed here by autocomplete and quietly produce a
    key that looks right.
    """
    return Context(
        session=row.get("session") or "?",
        vol_regime=brti_vol_regime(row.get("brti_volatility_bps") or 0.0),
        distance=_band(
            abs(row.get("brti_normalized_distance") or 0.0), BRTI_DISTANCE_BANDS
        ),
        price=_band(row.get("our_ask") or 0.0, PRICE_BANDS),
    )


@dataclass
class Arm:
    """One context, one action, and what it has been worth."""

    context: Context
    action: str                      # "accept" | "reject"
    rewards: list[float] = field(default_factory=list)
    wins: int = 0
    simulated: bool = False          # True when no fill actually happened

    @property
    def n(self) -> int:
        return len(self.rewards)

    @property
    def mean(self) -> float:
        return sum(self.rewards) / self.n if self.n else 0.0

    @property
    def total(self) -> float:
        return sum(self.rewards)

    def interval(self, draws: int = 2000, seed: int = 11) -> tuple[float, float]:
        if self.n < 2:
            return 0.0, 0.0
        rng = random.Random(seed)
        means = sorted(
            sum(rng.choice(self.rewards) for _ in self.rewards) / self.n
            for _ in range(draws)
        )
        return means[int(0.025 * draws)], means[int(0.975 * draws) - 1]


@dataclass(frozen=True)
class Proposal:
    """A suggested adjustment, with the evidence that produced it."""

    context: Context
    kind: str          # "confidence" | "execution"
    direction: str     # "raise" | "lower" | "admit" | "block"
    strength: str      # "neutral" | "small" | "material"
    n: int
    mean: float
    low: float
    high: float
    simulated: bool
    note: str

    def line(self) -> str:
        mark = " (simulated fills)" if self.simulated else ""
        return (
            f"{self.kind:<10} {self.direction:<6} {self.strength:<9} "
            f"{self.context}  n={self.n} {self.mean:+.4f} "
            f"[{self.low:+.4f},{self.high:+.4f}]{mark}  {self.note}"
        )


def strength_for(n: int, low: float, high: float, comparisons: int) -> str:
    """How boldly may this be acted on?

    Thin or interval-spanning-zero evidence earns NEUTRAL - recorded, acted on
    not at all. The interval is widened for the number of contexts examined,
    because the best-looking cell in a table of a hundred is partly a
    selection artefact, and pretending otherwise is how section 14's
    sub-band survived long enough to be believed.
    """
    if n < 40:
        return "neutral"
    # Bonferroni-ish widening: scale the half-width by sqrt(comparisons).
    mid = (low + high) / 2
    half = (high - low) / 2 * math.sqrt(max(1, comparisons))
    if (mid - half) > 0 or (mid + half) < 0:
        return "material" if n >= 120 else "small"
    return "neutral"


def build_arms(rows: list[dict], reward) -> dict[tuple, Arm]:
    """Group every recorded decision into (context, action) arms.

    `reward(row) -> float` is supplied so the caller decides what counts:
    realised money for fills, a hypothetical at the recorded price for
    refusals. The two never merge, because `action` differs.
    """
    arms: dict[tuple, Arm] = {}
    for row in rows:
        context = context_of(row)
        action = "accept" if row.get("rule_match") else "reject"
        key = (context, action)
        arm = arms.get(key)
        if arm is None:
            arm = arms[key] = Arm(
                context=context, action=action, simulated=(action == "reject")
            )
        arm.rewards.append(reward(row))
        arm.wins += int(bool(row.get("won")))
    return arms


def proposals(arms: dict[tuple, Arm], minimum: int = 40) -> list[Proposal]:
    """Adjustments the evidence supports. Usually very few, by design."""
    considered = [a for a in arms.values() if a.n >= minimum]
    out: list[Proposal] = []
    for arm in considered:
        low, high = arm.interval()
        strength = strength_for(arm.n, low, high, len(considered))
        if arm.action == "reject":
            # A refusal that MAKES money means the gate is right there; a
            # refusal that loses money is a candidate for admitting.
            if arm.mean <= 0:
                continue
            out.append(Proposal(
                context=arm.context, kind="execution", direction="admit",
                strength=strength, n=arm.n, mean=arm.mean, low=low, high=high,
                simulated=True,
                note="refusals here were profitable to refuse" if arm.mean < 0
                     else "refused signals netted positive at the recorded price",
            ))
        else:
            direction = "raise" if arm.mean > 0 else "lower"
            out.append(Proposal(
                context=arm.context, kind="confidence", direction=direction,
                strength=strength, n=arm.n, mean=arm.mean, low=low, high=high,
                simulated=False,
                note="accepted signals here " + (
                    "out-earn the book" if arm.mean > 0 else "lose money"
                ),
            ))
    out.sort(key=lambda p: (p.strength != "material", p.strength != "small", -p.n))
    return out


@dataclass
class Scorecard:
    """The four numbers any adjustment has to be judged on.

    An evaluation that counts only the winners it would have recovered is a
    sales pitch. The losers it would also have admitted belong in the same
    table, at the same prices.
    """

    recovered: float = 0.0        # profitable refusals a change would admit
    admitted_losses: float = 0.0  # losing refusals it would also admit
    avoided: float = 0.0          # losing acceptances a change would block
    forgone: float = 0.0          # profitable acceptances it would also block
    recovered_n: int = 0
    admitted_n: int = 0
    avoided_n: int = 0
    forgone_n: int = 0

    @property
    def net(self) -> float:
        return round(
            self.recovered + self.admitted_losses + self.avoided + self.forgone, 6
        )

    def render(self) -> str:
        return "\n".join([
            f"  recovered opportunities   {self.recovered_n:>5}  "
            f"{self.recovered:+8.2f}",
            f"  new losses admitted       {self.admitted_n:>5}  "
            f"{self.admitted_losses:+8.2f}",
            f"  losing trades avoided     {self.avoided_n:>5}  {self.avoided:+8.2f}",
            f"  winners wrongly blocked   {self.forgone_n:>5}  {self.forgone:+8.2f}",
            f"  {'NET':<25} {'':>5}  {self.net:+8.2f}",
        ])


def score_admitting(rows: list[dict], predicate, reward) -> Scorecard:
    """What admitting the contexts `predicate` selects would have been worth."""
    card = Scorecard()
    for row in rows:
        value = reward(row)
        if not row.get("rule_match") and predicate(row):
            if value > 0:
                card.recovered += value
                card.recovered_n += 1
            else:
                card.admitted_losses += value
                card.admitted_n += 1
    return card


def score_blocking(rows: list[dict], predicate, reward) -> Scorecard:
    """What blocking the contexts `predicate` selects would have been worth."""
    card = Scorecard()
    for row in rows:
        value = reward(row)
        if row.get("rule_match") and predicate(row):
            if value < 0:
                card.avoided += -value
                card.avoided_n += 1
            else:
                card.forgone += -value
                card.forgone_n += 1
    return card


# ---------------------------------------------------------------- brti-2
#
# THE SETUP, KEYED ON WHAT THE RULE ACTUALLY GATES ON.
#
# `brti-1` keyed `session · vol_regime · distance · price`: context first,
# setup last, and momentum - one of the four deployed gates - absent entirely.
# Measured over the assembled training dataset that produced 139 cells of which
# 18 reached n>=120, covering 58% of it. Session and volatility regime were
# consuming the evidence while being supporting context.
#
# The operator's framing is the correct one: the layer exists to learn which
# matching SETUPS deserve more confidence, which lose, and which refusals
# should have qualified. So the key is the quantities the deployed rule gates
# on, which are also exactly the rejection reasons:
#
#     Decision ask    -> price band       cut where the rule cuts, 0.70 / 0.93
#     BRTI distance   -> distance band    cut at the 10x floor
#     BRTI momentum   -> momentum band    cut at the gate (0) and the median
#     Reference       -> never reaches a decision row; a stale reference
#                        produces no signal at all, so there is nothing to key
#
# A reject cell therefore SAYS why it was refused - `bd<5` failed distance,
# `px<70` failed the band, `mom<=0` failed momentum - which is what makes
# "should this refusal have qualified" a question the table can answer, and
# what lets an admission name the single gate it overrides.
#
# Session, volatility regime and the band-hold timer are recorded beside every
# decision as CONTEXT. They are available to read and to slice by hand; they do
# not partition the evidence by default.
#
# The momentum cuts are declared from the FEATURE distribution before any
# outcome was consulted: the gate sits at 0, 6.9% of aligned momentum is at or
# below it, and the median of the rest is 5.4 bps.
BRTI_MOMENTUM_BANDS = ((-9e9, 0.0, "mom<=0"), (0.0, 5.0, "mom0-5"),
                       (5.0, 9e9, "mom5+"))

# Cut where the RULE cuts. `brti-1` used 0.94 for the top band while the rule
# admits up to 0.93, so eleven corpus rows keyed as an accept in a band whose
# name said they were above it.
SETUP_PRICE_BANDS = ((0.0, 0.70, "px<70"), (0.70, 0.85, "px70-85"),
                     (0.85, 0.9301, "px85-93"), (0.9301, 1.0, "px93+"))

SETUP_FEATURE_VERSION = "brti-2"


def brti_momentum_band(aligned_bps: float) -> str:
    """Band the momentum ALREADY SIGNED for our side.

    The caller signs it, because the sign depends on which side we are taking
    and this module does not know that. Passing the raw value would band an UP
    setup and a DOWN setup with identical momentum into the same cell while the
    rule treats one as aligned and the other as against.
    """
    return _band(aligned_bps, BRTI_MOMENTUM_BANDS)


@dataclass(frozen=True)
class SetupContext:
    """What was being traded. Hashable, so it keys the arms."""

    distance: str
    price: str
    momentum: str

    def __str__(self) -> str:
        return f"{self.distance} · {self.price} · {self.momentum}"


def setup_context_of(row: dict) -> SetupContext:
    """The canonical `brti-2` setup key. ONE function, both callers.

    `row` carries the aligned momentum under `brti_aligned_momentum_bps` - the
    value the gate is applied to - or the raw `brti_momentum_bps` plus a
    `side`, from which it is signed here.
    """
    aligned = row.get("brti_aligned_momentum_bps")
    if aligned is None:
        direction = 1 if row.get("side") == "UP" else -1
        aligned = direction * (row.get("brti_momentum_bps") or 0.0)
    return SetupContext(
        distance=_band(
            abs(row.get("brti_normalized_distance") or 0.0), BRTI_DISTANCE_BANDS
        ),
        price=_band(row.get("our_ask") or 0.0, SETUP_PRICE_BANDS),
        momentum=brti_momentum_band(aligned),
    )
