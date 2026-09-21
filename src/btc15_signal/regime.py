"""Regime as INTELLIGENCE: a lean, never a gate and never a size.

The operator's instruction: hour-of-day should "add or reduce weight, not
[be] a blocker gate". That is a materially better idea than the filter section
4 forbade, and for a concrete reason: a filter removes trades, so a filter
fitted to noise destroys real opportunity permanently. A bounded multiplier
cannot remove anything - the floor here is 0.6, never 0 - so the worst a
misread regime can do is colour how a setup is described.

"Slightly wrong" is still a cost, though, and it is the cost that matters:
weighting on an estimate that is mostly sampling error adds variance with no
expected return. So the estimates are SHRUNK toward the overall edge by how
uncertain each one is, empirical-Bayes style:

    shrink = tau^2 / (tau^2 + se_h^2)

`tau^2` is the between-hour variance of the TRUE effects, estimated as the
observed spread of the hourly means minus the average sampling variance. If the
hours differ only because each is measured noisily, that subtraction lands near
zero, every shrink factor lands near zero, and every weight lands at 1.0 - the
correct answer, arrived at by the arithmetic rather than by an opinion.

Measured over 71 days by `scripts/measure_hour.py`. The pre-registered test it
was built for - 17-20 UTC against every other hour, which is the operator's
"from 1pm it goes bad" - came out at +0.0010/ct [-0.0272, +0.0305], p=0.542:
no difference. The table below is therefore mostly noise by construction, and
the shrinkage is what stops that noise reaching the order.

IT MUST NOT TOUCH SIZE. On 2026-09-21 this was briefly wired into the order
budget on the assumption that "weight" meant position size. It was never asked
for and was reverted the same day: **position size belongs to the operator and
to nobody else.** There is deliberately no helper here that takes a budget, so
reaching for one again requires writing it rather than calling it.

What the weight IS for: it is intelligence. It is shown in the alert, carried
into the decision narration, and archived so the hourly question keeps being
measured instead of re-argued from single days. It does not gate, it does not
size, and it does not throttle.
"""

from dataclasses import dataclass
from datetime import UTC, datetime

# hour UTC -> (n, net per contract, ci_low, ci_high) from measure_hour.py,
# deployed band, 6-11 minutes, one entry per market, 71 days.
HOURLY: dict[int, tuple[int, float, float, float]] = {
    0: (217, -0.0042, -0.0577, 0.0445),
    1: (220, 0.0026, -0.0515, 0.0524),
    2: (214, 0.0121, -0.0407, 0.0636),
    3: (218, 0.0132, -0.0389, 0.0661),
    4: (219, -0.0042, -0.0587, 0.0460),
    5: (216, -0.0533, -0.1133, 0.0050),
    6: (198, 0.0108, -0.0455, 0.0650),
    7: (180, 0.0399, -0.0151, 0.0918),
    8: (189, 0.0344, -0.0208, 0.0862),
    9: (222, 0.0248, -0.0251, 0.0724),
    10: (213, -0.0661, -0.1274, -0.0065),
    11: (224, -0.0403, -0.1024, 0.0127),
    12: (206, 0.0207, -0.0331, 0.0732),
    13: (215, -0.0208, -0.0756, 0.0333),
    14: (218, 0.0457, -0.0029, 0.0902),
    15: (232, 0.0364, -0.0116, 0.0803),
    16: (212, 0.0400, -0.0101, 0.0856),
    17: (208, 0.0122, -0.0404, 0.0658),
    18: (219, -0.0029, -0.0556, 0.0486),
    19: (211, 0.0412, -0.0112, 0.0887),
    20: (214, -0.0222, -0.0803, 0.0307),
    21: (206, 0.0304, -0.0234, 0.0798),
    22: (215, 0.0154, -0.0372, 0.0649),
    23: (202, -0.0120, -0.0697, 0.0439),
}

# A deviation of one SCALE from baseline moves the lean by one full unit
# before clamping. 0.05/contract is roughly twice the measured 15-minute edge,
# so even the most extreme hour stays well inside the bounds.
SCALE = 0.05
MIN_WEIGHT = 0.6
MAX_WEIGHT = 1.4


@dataclass(frozen=True)
class RegimeWeight:
    hour: int
    weight: float
    raw_edge: float
    shrunk_edge: float
    baseline: float
    shrink: float

    def describe(self) -> str:
        """One line for the alert. Says WHY, not just what."""
        if abs(self.weight - 1.0) < 0.005:
            return (
                f"{self.hour:02d}:00 UTC · x1.00 neutral "
                f"(hourly edge not separable from noise)"
            )
        direction = "favourable" if self.weight > 1 else "unfavourable"
        return (
            f"{self.hour:02d}:00 UTC · x{self.weight:.2f} leans {direction} "
            f"(raw {self.raw_edge:+.4f} shrunk to {self.shrunk_edge:+.4f})"
        )


def _se(low: float, high: float) -> float:
    """Standard error implied by a 95% interval."""
    return max((high - low) / (2 * 1.96), 1e-9)


def _baseline_and_tau(table: dict) -> tuple[float, float]:
    """Sample-weighted mean, and the between-hour variance of TRUE effects.

    `tau^2 = var(observed means) - mean(sampling variance)`, floored at zero.
    When the hours differ only because each is noisily measured, the two terms
    cancel and tau^2 is zero - which drives every shrink factor to zero and
    every weight to exactly 1.0. That is the whole safety property: no opinion
    is needed to switch this off, the arithmetic does it.
    """
    total_n = sum(row[0] for row in table.values())
    baseline = sum(row[0] * row[1] for row in table.values()) / total_n
    means = [row[1] for row in table.values()]
    spread = sum((m - baseline) ** 2 for m in means) / max(len(means) - 1, 1)
    noise = sum(_se(row[2], row[3]) ** 2 for row in table.values()) / len(table)
    return baseline, max(0.0, spread - noise)


def weight_for_hour(hour: int, table: dict | None = None) -> RegimeWeight:
    """This hour's lean. NEVER zero, and never read by the order path."""
    table = HOURLY if table is None else table
    row = table.get(hour % 24)
    baseline, tau2 = _baseline_and_tau(table) if table else (0.0, 0.0)
    if row is None:
        return RegimeWeight(hour % 24, 1.0, baseline, baseline, baseline, 0.0)
    _n, raw, low, high = row
    se2 = _se(low, high) ** 2
    shrink = tau2 / (tau2 + se2) if (tau2 + se2) > 0 else 0.0
    shrunk = baseline + shrink * (raw - baseline)
    weight = 1.0 + (shrunk - baseline) / SCALE
    weight = min(MAX_WEIGHT, max(MIN_WEIGHT, weight))
    return RegimeWeight(hour % 24, round(weight, 3), raw, shrunk, baseline, shrink)


def weight_at(now_ms: int, table: dict | None = None) -> RegimeWeight:
    return weight_for_hour(datetime.fromtimestamp(now_ms / 1000, UTC).hour, table)

# Confidence is scored out of 100 so a regime lean can be stated as points -
# "base HIGH, NY afternoon -8, adjusted MEDIUM" - rather than silently moving a
# label. The bands preserve the existing verdicts exactly: 4 of 4 agreeing
# signals is HIGH, 2-3 is MEDIUM, 0-1 is LOW.
BASE_POINTS = (0, 25, 50, 70, 100)
HIGH_AT = 85
MEDIUM_AT = 40
MAX_ADJUSTMENT = 25


def base_points(agreeing: int) -> int:
    return BASE_POINTS[max(0, min(len(BASE_POINTS) - 1, agreeing))]


def label_for(points: float) -> str:
    if points >= HIGH_AT:
        return "HIGH"
    return "MEDIUM" if points >= MEDIUM_AT else "LOW"


def confidence_points(weight: RegimeWeight) -> int:
    """The regime lean expressed as confidence points, positive or negative.

    This is the ONLY thing regime is allowed to move. It cannot skip a market,
    stop a poll, block a qualified order or silence an alert - it changes how
    confident the explanation sounds and nothing else.
    """
    points = round((weight.weight - 1.0) * 100)
    return int(max(-MAX_ADJUSTMENT, min(MAX_ADJUSTMENT, points)))


def adjust(agreeing: int, weight: RegimeWeight) -> dict:
    """Base -> adjustment -> adjusted, all three kept so the alert can show them."""
    base = base_points(agreeing)
    delta = confidence_points(weight)
    adjusted = max(0, min(100, base + delta))
    return {
        "base_points": base,
        "base_label": label_for(base),
        "regime_points": delta,
        "regime_reason": f"{weight.hour:02d}:00 UTC",
        "adjusted_points": adjusted,
        "adjusted_label": label_for(adjusted),
    }
