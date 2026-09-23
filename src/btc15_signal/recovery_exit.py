"""When to STOP recovering, even though money is still missing.

THE OPERATOR'S RULE, and the reasoning behind it.

Recovery arms on a realised deficit and upsizes the next qualifying trade.
Left alone it stays armed until the deficit reaches zero, which means the
upsize is still on the book at the moment a loss is most expensive: late in a
recovery, the remaining deficit is small, but the position is still double
size, so one loss more than undoes the run of wins that got you there. That
is the loop - recover, lose bigger, recover again - and it is what this
module exists to break.

    "Even if after a 50% recovery of the initial loss, turn off recovery -
     that's enough, because we've seen that even regular size is able to
     recover on its own."

So recovery stands down early, on either of two conditions:

    HALFWAY     recovered >= 50% of the peak deficit
    PATIENCE    4 wins into the epoch and recovered >= 40%

The second exists because a long grind of small wins is exactly the state in
which the next loss hurts most: many trades have gone by, the upsize has been
riding all of them, and the deficit has barely moved.

WHAT "TURN OFF" MEANS, PRECISELY. It stops the UPSIZE. It does NOT zero the
deficit, and it must never be implemented that way: the money really is still
missing, and writing it off would make the ledger lie about the account. The
deficit stays on the books, keeps being reduced by ordinary base-size wins,
and clears when it genuinely reaches zero. `stood_down` is a separate flag
answering a separate question - "may we upsize?" - from "is money owed?".

ONCE DOWN, IT STAYS DOWN for the rest of the epoch. Re-arming on the next
loss would rebuild the loop this is meant to prevent. The epoch ends when the
deficit actually clears, and a future loss arms recovery again normally.
"""

from dataclasses import dataclass

# Recovered fraction that is "enough" on its own.
EXIT_FRACTION = 0.50
# After this many realised wins in the epoch, a lower bar applies.
PATIENCE_WINS = 4
PATIENCE_FRACTION = 0.40


@dataclass(frozen=True)
class ExitDecision:
    stand_down: bool
    reason: str
    recovered_fraction: float = 0.0

    def __bool__(self) -> bool:
        return self.stand_down


def recovered_fraction(peak: float, deficit: float) -> float:
    """How much of the worst point has been won back, as 0.0-1.0.

    Measured against the PEAK of this epoch, not the opening deficit. A loss
    part-way through recovery raises the peak, so progress is always judged
    against the deepest hole actually dug - otherwise a fresh loss would make
    the percentage jump backwards and the exit rule would fire on arithmetic
    rather than on progress.
    """
    if peak <= 0:
        return 0.0
    return max(0.0, min(1.0, (peak - max(0.0, deficit)) / peak))


def decide(
    peak: float,
    deficit: float,
    wins: int,
    *,
    exit_fraction: float = EXIT_FRACTION,
    patience_wins: int = PATIENCE_WINS,
    patience_fraction: float = PATIENCE_FRACTION,
    enabled: bool = True,
) -> ExitDecision:
    """Should recovery sizing stand down now?

    Pure, so the live path and any replay reach the same answer from the same
    three numbers.
    """
    fraction = recovered_fraction(peak, deficit)
    if not enabled:
        return ExitDecision(False, "partial exit disabled", fraction)
    if peak <= 0 or deficit <= 0:
        # Nothing owed: there is nothing to stand down FROM, and saying so
        # keeps "cleared" and "stood down" from being confused in the logs.
        return ExitDecision(False, "", fraction)
    if fraction >= exit_fraction:
        return ExitDecision(
            True,
            f"recovered {fraction:.0%} of the {peak:.2f} peak "
            f"(>= {exit_fraction:.0%}); base size recovers the rest",
            fraction,
        )
    if wins >= patience_wins and fraction >= patience_fraction:
        return ExitDecision(
            True,
            f"{wins} wins into recovery and {fraction:.0%} back "
            f"(>= {patience_fraction:.0%} after {patience_wins}); "
            f"not risking the upsize on the next loss",
            fraction,
        )
    return ExitDecision(False, "", fraction)
