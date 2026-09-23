"""Frozen candidates, evaluated forward on live signals. They control nothing.

THE DISTINCTION THIS MODULE EXISTS FOR.

    promotion   may this change a live order?      needs the full bar
    candidate   is this worth watching forwards?   needs only evidence

A candidate that has not earned promotion is still worth recording against
every eligible signal: what the unchanged strategy decided, what the candidate
would have changed, and - once the market settles - which was right. That is
the only way a candidate accumulates evidence on data it was not fitted to.
"No adjustment has earned promotion" is a result; "therefore there is nothing
to forward-test" does not follow, and acting as though it did is how a
learning loop never starts.

Every row here is a PREDICTION made before the outcome existed. The artefact
is versioned and frozen on disk, so the candidate that made a prediction can
always be identified afterwards - a candidate silently refitted between a
prediction and its grading would make the record meaningless.
"""

import json
from dataclasses import dataclass
from pathlib import Path

from .intelligence_policy import ADMIT, VETO


@dataclass(frozen=True)
class Candidate:
    candidate_id: str
    context: str            # "<context>|<action>"
    proposed_action: str
    train_n: int
    train_mean: float
    promotes: bool
    delta: int = 0

    @property
    def context_key(self) -> str:
        return self.context.split("|")[0]

    @property
    def applies_to(self) -> str:
        return self.context.split("|")[1] if "|" in self.context else "accept"

    def would_change(self, qualified: bool, context_key: str) -> bool:
        """Does this candidate disagree with the base strategy here?

        A veto only bites on a setup the rule ACCEPTED; an admission only on
        one it REFUSED. A candidate that "changes" a decision that already
        went its way has changed nothing, and counting those would inflate
        every hit rate this table is meant to measure.
        """
        if context_key != self.context_key:
            return False
        if self.proposed_action == VETO:
            return qualified
        if self.proposed_action == ADMIT:
            return not qualified
        return False


@dataclass
class CandidateSet:
    version: str = "none"
    feature_version: str = "none"
    built_ms: int = 0
    data_end_ms: int = 0
    training_cutoff_ms: int = 0
    candidates: tuple[Candidate, ...] = ()

    @classmethod
    def load(cls, path: str | Path) -> "CandidateSet":
        source = Path(path)
        if not source.exists():
            return cls()
        try:
            data = json.loads(source.read_text())
        except (ValueError, OSError):
            return cls()
        items = tuple(
            Candidate(
                candidate_id=c.get("candidate_id", "?"),
                context=c.get("context", ""),
                proposed_action=c.get("proposed_action", ""),
                train_n=int(c.get("train_n") or 0),
                train_mean=float(c.get("train_mean") or 0.0),
                promotes=bool(c.get("promotes")),
                delta=int(c.get("delta") or 0),
            )
            for c in data.get("candidates", [])
        )
        return cls(
            version=data.get("version", "none"),
            feature_version=data.get("feature_version", "none"),
            built_ms=int(data.get("built_ms") or 0),
            data_end_ms=int(data.get("data_end_ms") or 0),
            training_cutoff_ms=int(data.get("training_cutoff_ms") or 0),
            candidates=items,
        )

    def evaluate(self, *, context_key: str, qualified: bool) -> list[dict]:
        """One row per candidate that SPEAKS to this signal.

        Candidates whose context does not match are not recorded: a table of
        non-opinions buries the opinions.
        """
        out = []
        for candidate in self.candidates:
            if candidate.context_key != context_key:
                continue
            out.append({
                "candidate_id": candidate.candidate_id,
                "candidate_version": self.version,
                "context_key": context_key,
                "proposed_action": candidate.proposed_action,
                "baseline_qualified": int(qualified),
                "would_change": int(candidate.would_change(qualified, context_key)),
                "feature_version": self.feature_version,
            })
        return out


def grade(row: dict, won: bool, reward) -> tuple[float, float]:
    """(baseline P&L, candidate P&L) for one graded prediction.

    The baseline is what actually happened - a trade if the rule took it,
    nothing if it did not. The candidate's figure is what its own decision
    would have produced, and where that means a trade nobody made it is a
    SIMULATED fill, priced at the recorded ask. A rejected winner is evidence
    about direction, not proof a fill was available.
    """
    ask = row.get("ask")
    qualified = bool(row.get("baseline_qualified"))
    changed = bool(row.get("would_change"))
    if ask is None:
        return 0.0, 0.0
    traded = reward(ask, won)
    baseline = traded if qualified else 0.0
    if not changed:
        return baseline, baseline
    if row.get("proposed_action") == VETO:
        return baseline, 0.0        # it would have stood aside
    return baseline, traded          # it would have taken the trade
