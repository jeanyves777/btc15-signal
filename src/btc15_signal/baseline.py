"""A calibrated logistic regression, as the thing the similarity layer must beat.

The spec's model-selection rule is "prefer the simplest model if performance is
statistically indistinguishable", and a nearest-neighbour retrieval over 6,400
markets is not the simplest thing available. This is: eight features, a linear
model, L2 regularisation, fitted by gradient descent in about forty lines. If
it matches the cohort read out of sample, the cohort read is not earning its
complexity and this should be deployed instead.

No sklearn on this box, so it is written out. That is a feature rather than a
compromise - every coefficient is inspectable, and a model whose weights can be
printed in a Telegram message is one whose behaviour can be argued with.

CALIBRATION IS THE POINT, NOT ACCURACY. These markets are bought at 70-93c, so
a model that is right 85% of the time and a price that is right 85% of the time
produce exactly zero edge between them. What matters is whether a stated 80%
means 80%, which is why `brier` and the reliability table live here and why the
fit optimises log-loss rather than anything that counts correct answers.

FEATURE SCHEMA IS VERSIONED. A model trained on one feature order and scored
against another is a silent, total corruption with no symptom, so `SCHEMA` is
recorded beside every prediction and checked on load.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field

# Bump on ANY change to `features()` - order, meaning or count.
SCHEMA = "lr-v1"

# Kept deliberately short. Every one is available at decision time and none is
# a restatement of another: price and distance carry most of the signal, the
# rest are the conditions the operator's rule already reasons about.
FEATURE_NAMES = (
    "ask",                  # what it costs - by far the strongest single term
    "normalized_distance",  # how far the strike is, in volatility units
    "remaining_minutes",
    "momentum_for_side",    # SIGNED FOR OUR SIDE, matching check_facts
    "volatility_bps",
    "spread_bps",
    "is_up",
    "band_held_s",
)


def features(row: dict) -> list[float]:
    """One observation as a feature vector, in SCHEMA order.

    Momentum is signed FOR OUR SIDE, the same convention
    `EntryRule.check_facts` uses, because the two must never disagree about
    what "+3.3 bps" means.
    """
    side_up = 1.0 if (row.get("side") or "UP") == "UP" else 0.0
    direction = 1.0 if side_up else -1.0
    return [
        float(row.get("our_ask") or row.get("ask") or 0.0),
        abs(float(row.get("normalized_distance") or 0.0)),
        float(row.get("remaining_s") or 0.0) / 60.0,
        direction * float(row.get("momentum_5m_bps") or 0.0),
        float(row.get("volatility_5m_bps") or 0.0),
        float(row.get("spread_bps") or 0.0),
        side_up,
        float(row.get("band_held_s") or 0.0),
    ]


@dataclass
class Standardiser:
    """Per-feature mean and scale, fitted on TRAINING ROWS ONLY.

    Standardising over the whole archive would leak the test period's
    distribution into the training fold - a mild leak, but the kind that makes
    a walk-forward claim untrue, and this layer's entire value rests on that
    claim being exact.
    """

    mean: list[float] = field(default_factory=list)
    scale: list[float] = field(default_factory=list)

    @classmethod
    def fit(cls, rows: list[list[float]]) -> Standardiser:
        width = len(rows[0])
        mean = [sum(r[i] for r in rows) / len(rows) for i in range(width)]
        scale = []
        for i in range(width):
            var = sum((r[i] - mean[i]) ** 2 for r in rows) / max(len(rows) - 1, 1)
            scale.append(math.sqrt(var) or 1.0)
        return cls(mean, scale)

    def apply(self, vector: list[float]) -> list[float]:
        return [(v - m) / s for v, m, s in zip(vector, self.mean, self.scale, strict=True)]


def _sigmoid(z: float) -> float:
    # Split so neither branch can overflow exp() on an extreme score.
    if z >= 0:
        return 1.0 / (1.0 + math.exp(-z))
    exp_z = math.exp(z)
    return exp_z / (1.0 + exp_z)


@dataclass
class LogisticModel:
    weights: list[float]
    bias: float
    standardiser: Standardiser
    schema: str = SCHEMA
    trained_n: int = 0
    training_cutoff_ms: int | None = None

    def probability(self, row: dict) -> float:
        vector = self.standardiser.apply(features(row))
        z = self.bias + sum(w * x for w, x in zip(self.weights, vector, strict=True))
        return _sigmoid(z)

    def explain(self) -> str:
        """The coefficients, largest first. An explainable model should say so."""
        terms = sorted(
            zip(FEATURE_NAMES, self.weights, strict=True),
            key=lambda pair: -abs(pair[1]),
        )
        return " · ".join(f"{name} {weight:+.2f}" for name, weight in terms)

    def to_json(self) -> str:
        return json.dumps({
            "schema": self.schema, "weights": self.weights, "bias": self.bias,
            "mean": self.standardiser.mean, "scale": self.standardiser.scale,
            "trained_n": self.trained_n, "cutoff": self.training_cutoff_ms,
        })

    @classmethod
    def from_json(cls, blob: str) -> LogisticModel:
        data = json.loads(blob)
        if data.get("schema") != SCHEMA:
            raise ValueError(
                f"feature schema {data.get('schema')!r} != {SCHEMA!r}; refusing "
                "to score a model against features it was not trained on"
            )
        return cls(
            data["weights"], data["bias"],
            Standardiser(data["mean"], data["scale"]),
            data["schema"], data.get("trained_n", 0), data.get("cutoff"),
        )


def fit(
    rows: list[dict],
    labels: list[int],
    *,
    l2: float = 1.0,
    steps: int = 600,
    learning_rate: float = 0.25,
    cutoff_ms: int | None = None,
) -> LogisticModel | None:
    """Full-batch gradient descent on log-loss. Deterministic: no shuffling,
    no random init, so identical input gives an identical model - which test 13
    requires and which a promotion report is worthless without."""
    if len(rows) < 50 or len(set(labels)) < 2:
        return None
    vectors = [features(r) for r in rows]
    standardiser = Standardiser.fit(vectors)
    design = [standardiser.apply(v) for v in vectors]
    width = len(design[0])
    weights = [0.0] * width
    bias = 0.0
    n = len(design)
    for _ in range(steps):
        gradient = [0.0] * width
        bias_gradient = 0.0
        for vector, label in zip(design, labels, strict=True):
            z = bias + sum(w * x for w, x in zip(weights, vector, strict=True))
            error = _sigmoid(z) - label
            bias_gradient += error
            for i, x in enumerate(vector):
                gradient[i] += error * x
        for i in range(width):
            # L2 on the weights only. Penalising the bias would drag the
            # model's base rate toward 0.5, and these markets settle near 0.85.
            weights[i] -= learning_rate * (gradient[i] / n + l2 * weights[i] / n)
        bias -= learning_rate * bias_gradient / n
    return LogisticModel(weights, bias, standardiser, SCHEMA, n, cutoff_ms)


def brier(probabilities: list[float], outcomes: list[int]) -> float:
    """Mean squared error of the probabilities. Lower is better; 0.25 is a coin."""
    if not probabilities:
        return float("nan")
    return sum(
        (p - o) ** 2 for p, o in zip(probabilities, outcomes, strict=True)
    ) / len(probabilities)


def log_loss(probabilities: list[float], outcomes: list[int]) -> float:
    if not probabilities:
        return float("nan")
    total = 0.0
    for p, o in zip(probabilities, outcomes, strict=True):
        clipped = min(max(p, 1e-6), 1 - 1e-6)
        total -= o * math.log(clipped) + (1 - o) * math.log(1 - clipped)
    return total / len(probabilities)


def reliability(
    probabilities: list[float], outcomes: list[int], bins: int = 5
) -> list[tuple[float, float, int]]:
    """(mean predicted, observed rate, n) per bin - the calibration evidence.

    A single Brier score hides the shape: a model can score well overall while
    being badly overconfident in exactly the 0.85-0.95 band this system trades.
    """
    buckets: dict[int, list[tuple[float, int]]] = {}
    for p, o in zip(probabilities, outcomes, strict=True):
        buckets.setdefault(min(int(p * bins), bins - 1), []).append((p, o))
    table = []
    for index in sorted(buckets):
        pairs = buckets[index]
        table.append((
            sum(p for p, _ in pairs) / len(pairs),
            sum(o for _, o in pairs) / len(pairs),
            len(pairs),
        ))
    return table
