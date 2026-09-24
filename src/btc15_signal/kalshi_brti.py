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
    # THE REVERSAL GATE. 1.0 means the whole recent advance has been
    # handed back. Shipped blocking on the operator's instruction; the
    # evidence behind the threshold is in FINDINGS 58 and is thin.
    max_brti_retrace: float = 0.60
    # Momentum is UNGATED until it is measured on BRTI. A 60-second mean lags
    # the raw series, so the Binance momentum thresholds describe a different
    # quantity here too, and an unmeasured gate is worse than no gate because
    # it looks deliberate.
    min_brti_momentum_bps: float = 0.0
    require_momentum_alignment: bool = False
    # MOMENTUM SCALES THE DISTANCE REQUIRED. The operator's reading, 2026-09-24:
    # "a lot of momentum can mean distance can revert fast", so momentum should
    # set how far you must be from the strike rather than which way you bet.
    #
    # Measured on 2,145 corpus markets sampled 660-360s, priced on the Kalshi
    # book, day-clustered over 68 days:
    #
    #   deployed  >=10x any momentum      n=4350  +0.0268  [+0.012, +0.041]
    #   proposed  calm >=10x, moving >=15x n=2861 +0.0364  [+0.020, +0.051]
    #
    # with the out-of-sample half BETTER than the whole (+0.0380). Non-calm
    # setups at 10x return +0.0160 with a lower bound of exactly zero; they
    # need 15x before the interval clears it.
    #
    # WHAT WAS MEASURED AND REJECTED: lowering the floor FOR calm setups. Edge
    # rises with the floor even when calm (+0.0318 at 4x, +0.0560 at 15x), and
    # the marginal band below 10x spans zero in every cut - 7-10x calm is
    # +0.0242 [-0.006, +0.052]. So this raises the bar on moving markets and
    # never lowers it on quiet ones.
    #
    # ALIGNMENT IS STILL OFF, and this is not it. Aligned +0.027 against
    # against +0.026 on BRTI: the Binance-era separation (84.2% vs 71.4%)
    # does not reproduce. Magnitude is the thing that carries; direction is not.
    calm_momentum_bps: float = 5.0
    moving_min_brti_normalized_distance: float = 15.0
    # THE THREE LEVEL-HOLDING GATES. Operator's decision, 2026-09-24, after
    # RSI failed: "we are mostly already in the money; will price stay above
    # the level we need it to stay". Direction at 15 minutes is already in
    # the price - whether the STRIKE is being defended is not.
    #
    # Measured on the bot's own 176 in-the-money executed trades, priced on
    # Kalshi's settlement P&L. Each alone:
    #
    #   accel >= -5       keeps 91%   +5.09 against +1.07 actual
    #   rejections >= 2   keeps 38%   +5.75
    #   held_s >= 300     keeps 27%   +5.00
    #
    # and together at the shipped settings: keeps 34%, 89.8% win, +9.21.
    #
    # HELD_S IS NOT MONOTONIC and the threshold is chosen knowing it: alone
    # at 120s and 180s it HURTS (-1.38, -2.75) and only helps at 300s. It
    # earns its place here in combination, not on its own, which is recorded
    # against it rather than hidden.
    #
    # THE SAMPLE IS 176 TRADES OVER 5 DAYS and the cell is the best of 36
    # swept combinations, with a trade-level rather than day-clustered
    # interval. That is weaker evidence than the distance floor rests on.
    # Shipped on the operator's decision with the weakness stated; the
    # features are archived on every decision so this is re-measurable.
    min_brti_accel: float = -5.0
    min_brti_held_s: float = 120.0
    min_brti_rejections: int = 2
    entry_from_seconds: int = 660
    entry_to_seconds: int = 360

    def distance_floor_for(self, momentum_bps: float | None) -> tuple[float, str]:
        """(floor, why) - the distance this setup must clear, and the reason.

        Returned together so the alert can say WHICH floor applied. A gate
        whose threshold moves without saying so reads as an inconsistent gate.
        """
        magnitude = abs(momentum_bps or 0.0)
        if magnitude < self.calm_momentum_bps:
            return self.min_brti_normalized_distance, "calm"
        # NEVER BELOW THE CALM FLOOR. These are two independent fields, so a
        # config could set the moving floor lower than the calm one and a
        # busy market would need LESS cushion than a quiet one - the exact
        # inverse of what was measured. Worse, raising
        # `min_brti_normalized_distance` alone would then stop tightening the
        # gate at all, which is how a test that sets it to 1e9 still saw
        # setups pass.
        return max(self.min_brti_normalized_distance,
                   self.moving_min_brti_normalized_distance), "moving"

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
        floor, regime = self.distance_floor_for(features.brti_momentum_bps)
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
                # THE FLOOR MOVES WITH MOMENTUM, and says which one applied.
                # A quiet market needs less cushion than a fast one, because
                # a fast one can erase the cushion before expiry.
                "name": "BRTI distance",
                "passed": (
                    features.brti_normalized_distance >= floor
                ),
                "pass_text": (
                    f"{features.brti_normalized_distance:.1f}x BRTI vol "
                    f"(needs {floor:.0f}x, {regime})"
                ),
                "fail_text": (
                    f"{features.brti_normalized_distance:.1f}x - needs "
                    f"{floor:.0f}x ({regime} market, "
                    f"{abs(features.brti_momentum_bps or 0.0):.1f} bps)"
                ),
                "value": features.brti_normalized_distance,
            },
            {
                # THE MOVE THAT BUILT THE CUSHION - still building, or spent?
                # The trades this refuses won 40 of 55 and still lost 6.93:
                # they win often and lose big.
                "name": "Move still working",
                "passed": (features.brti_accel is None
                           or features.brti_accel >= self.min_brti_accel),
                "pass_text": (
                    f"{features.brti_accel:+.1f} bps accel"
                    if features.brti_accel is not None else "not measurable"
                ),
                "fail_text": (
                    f"{features.brti_accel:+.1f} bps - the move is decaying "
                    f"(needs {self.min_brti_accel:+.0f})"
                    if features.brti_accel is not None else "not measurable"
                ),
                "value": features.brti_accel,
            },
            {
                # HOW LONG THE LEVEL HAS ACTUALLY HELD.
                "name": "Level held",
                "passed": (features.brti_held_s is None
                           or features.brti_held_s >= self.min_brti_held_s),
                "pass_text": (
                    f"{features.brti_held_s:.0f}s on side "
                    f"(needs {self.min_brti_held_s:.0f}s)"
                    if features.brti_held_s is not None else "not measurable"
                ),
                "fail_text": (
                    f"only {features.brti_held_s:.0f}s on side - needs "
                    f"{self.min_brti_held_s:.0f}s"
                    if features.brti_held_s is not None else "not measurable"
                ),
                "value": features.brti_held_s,
            },
            {
                # TESTED AND HELD beats never approached: a level price has
                # turned back from is evidence, one it never reached is not.
                "name": "Level tested",
                "passed": (features.brti_rejections is None
                           or features.brti_rejections
                           >= self.min_brti_rejections),
                "pass_text": (
                    f"{features.brti_rejections} rejection(s) held"
                    if features.brti_rejections is not None
                    else "not measurable"
                ),
                "fail_text": (
                    f"{features.brti_rejections} rejection(s) - needs "
                    f"{self.min_brti_rejections}"
                    if features.brti_rejections is not None
                    else "not measurable"
                ),
                "value": features.brti_rejections,
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
                "name": "BRTI stability",
                # None is NOT a pass. A setup whose recent path cannot be
                # measured is one we cannot say is stable, and the whole point
                # of this gate is that "looks fine" and "was checked" are
                # different states.
                "passed": (
                    features.brti_retrace is not None
                    and features.brti_retrace <= self.max_brti_retrace
                ),
                "pass_text": (
                    "holding"
                    if features.brti_retrace is None
                    else f"{features.brti_retrace * 100:.0f}% given back "
                         f"(max {self.max_brti_retrace * 100:.0f}%)"
                ),
                "fail_text": (
                    "recent move not measurable"
                    if features.brti_retrace is None
                    else f"{features.brti_retrace * 100:.0f}% of the move "
                         f"already reversed"
                ),
                "value": features.brti_retrace,
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
