"""The `kalshi_brti` entry rule: every gate computed on the settlement price.

SHADOW. Nothing here places an order, and there is deliberately no flag that
makes it. Promoting it is a code change someone has to make on purpose, the
same contract the hourly ladder runs under.

WHY IT IS A SEPARATE RULE RATHER THAN A SETTING ON THE OLD ONE. The deployed
`EntryRule` gates on `min_normalized_distance = 1.5`, a number measured against
Binance RAW volatility. BRTI is published as a trailing 60-second mean, which
is a low-pass filter, so the identical market reads 10-20 on this instrument
(FINDINGS 43). Sharing a field between the two would let a threshold measured
for one be applied to the other by autocomplete, silently turning the gate off.
Different quantity, different rule, different file.

THE THRESHOLD WAS MEASURED, NOT TRANSLATED. `scripts/measure_brti_gates.py`
over 2,145 corpus markets, 1,913 in-band entries, net of fees:

    BRTI normalized distance    n      net/contract     95% CI
    < 5                        419        -0.0291   [-0.0719, +0.0142]
    5 - 10                     919        -0.0004   [-0.0281, +0.0258]
    10 - 15                    427        +0.0331   [-0.0012, +0.0665]
    15 - 20                    117        +0.0919   [+0.0407, +0.1359]

    floor >= 10  vs taking everything:   +0.0398   [+0.0126, +0.0668]
    floor >= 15  vs taking everything:   +0.0816   [+0.0361, +0.1213]

Positive in all four chronological quarters (+0.048, +0.055, +0.013, +0.046),
which is the check FINDINGS 14 used to kill a sub-band that only looked good
pooled.

WHAT IS NOT ESTABLISHED, and why this stays in shadow:

* The headline BRTI-native edge is **+0.0077/contract, CI [-0.0114, +0.0254]** -
  it spans zero. The DISTANCE STRUCTURE is what clears zero, not the rule.
* The floor was chosen by scanning six values. That is six chances to look
  good, and the interval is not corrected for it.
* The threshold moved with sample size while the backfill ran (n=886 -> 1,469
  -> 1,913). A number that is still drifting has not settled.

So this records what it WOULD have done and is graded against what the
deployed rule actually did. It earns a promotion by measurement or not at all.
"""

import json
from dataclasses import dataclass
from pathlib import Path

from .brti import BRTIFeatures


@dataclass(frozen=True)
class KalshiBRTIRule:
    """Entry gates on BRTI. Field names deliberately unlike `EntryRule`'s."""

    enabled: bool = False  # shadow; nothing reads this to place an order
    min_ask: float = 0.70
    max_ask: float = 0.93
    # The measured floor. NOT `min_normalized_distance` - that name belongs to
    # the Binance quantity and means something six times smaller.
    min_brti_normalized_distance: float = 10.0
    # Momentum is UNGATED until it is measured on BRTI. A 60-second mean lags
    # the raw series, so the Binance momentum thresholds describe a different
    # quantity here too, and an unmeasured gate is worse than no gate because
    # it looks deliberate.
    min_brti_momentum_bps: float = 0.0
    require_momentum_alignment: bool = False
    entry_from_seconds: int = 660
    entry_to_seconds: int = 360

    @classmethod
    def load(cls, path: str | Path) -> "KalshiBRTIRule":
        source = Path(path)
        if not source.exists():
            return cls()
        data = json.loads(source.read_text())
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in data.items() if k in known})

    def ask_for(self, features: BRTIFeatures, yes_bid: float, yes_ask: float) -> float:
        """The price of the side BRTI says is winning.

        The side comes from the official reference; the price comes from the
        Kalshi book. Long UP pays the yes ask, long DOWN pays 1 - yes bid.
        """
        return yes_ask if features.side == "UP" else round(1 - yes_bid, 4)

    def check_facts(
        self, features: BRTIFeatures, ask: float, remaining_s: int
    ) -> list[dict]:
        """The gates, each as a named fact, in the shape `messages` renders.

        Every one reads BRTI or the Kalshi book. None reads Binance.
        """
        direction = 1 if features.side == "UP" else -1
        aligned = direction * features.brti_momentum_bps
        band = f"{self.min_ask * 100:.0f}-{self.max_ask * 100:.0f}c"
        return [
            {
                "name": "Decision ask",
                "passed": self.min_ask <= ask <= self.max_ask,
                "pass_text": f"{ask * 100:.0f}c within {band}",
                "fail_text": f"{ask * 100:.0f}c - needs {band}",
                "value": ask,
            },
            {
                "name": "BRTI distance",
                "passed": (
                    features.brti_normalized_distance
                    >= self.min_brti_normalized_distance
                ),
                "pass_text": (
                    f"{features.brti_normalized_distance:.1f}x BRTI vol "
                    f"(needs {self.min_brti_normalized_distance:.0f}x)"
                ),
                "fail_text": (
                    f"{features.brti_normalized_distance:.1f}x - needs "
                    f"{self.min_brti_normalized_distance:.0f}x"
                ),
                "value": features.brti_normalized_distance,
            },
            {
                "name": "BRTI momentum",
                "passed": (
                    not self.require_momentum_alignment
                    or aligned >= self.min_brti_momentum_bps
                ),
                "pass_text": f"{aligned:+.1f} bps",
                "fail_text": f"{aligned:+.1f} bps - not aligned",
                "value": aligned,
            },
            {
                "name": "Reference",
                "passed": not features.stale,
                "pass_text": (
                    f"Kalshi BRTI, {features.samples} pts, "
                    f"${features.value:,.2f} vs target ${features.target:,.2f}"
                ),
                "fail_text": "Kalshi BRTI stale",
                "value": features.value,
            },
        ]

    def matches(
        self, features: BRTIFeatures, ask: float, remaining_s: int
    ) -> tuple[bool, str]:
        """(qualifies, why not). Shadow verdict only."""
        if not self.entry_to_seconds <= remaining_s <= self.entry_from_seconds:
            return False, f"outside the entry window at {remaining_s}s"
        if features.stale:
            return False, "BRTI reference is stale"
        for fact in self.check_facts(features, ask, remaining_s):
            if not fact["passed"]:
                return False, f"{fact['name']}: {fact['fail_text']}"
        return True, ""
