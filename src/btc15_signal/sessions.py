"""Trading sessions: the breakdown, and the report when one closes.

The session labels are the ones the whole archive already uses
(`features._session`), so a live report and a backtest mean the same thing by
"asia". They are UTC hour ranges, because that is what a session IS - the
venues do not move with our accounting day.

HOW THIS SITS INSIDE THE NEW YORK DAY, which is the only subtle part. The NY
day runs 04:00 UTC to 04:00 UTC, so an Asian session is split across it: hours
04-06 at the start, hours 00-03 at the end. That looks wrong until you count
them - 3 + 4 is the same seven hours the Asian session has, so a complete NY
day still contains exactly one session's worth of every label, and the
breakdown SUMS to the day's total. It is discontiguous, not incomplete, and a
breakdown that adds up is worth more than one that reads tidily.
"""

import datetime as dt
from dataclasses import dataclass

# (name, start hour inclusive, end hour exclusive), UTC. Matches
# `features._session` exactly - if these diverge, a live report and every
# measurement in FINDINGS stop describing the same thing.
SESSIONS: tuple[tuple[str, int, int], ...] = (
    ("asia", 0, 7),
    ("europe", 7, 13),
    ("us", 13, 21),
    ("late-us", 21, 24),
)

LABELS = {
    "asia": "\U0001f30f Asia",
    "europe": "\U0001f1ea\U0001f1fa Europe",
    "us": "\U0001f1fa\U0001f1f8 US",
    "late-us": "\U0001f319 Late US",
}


def session_of(ms: int) -> str:
    hour = dt.datetime.fromtimestamp(ms / 1000, dt.UTC).hour
    for name, start, end in SESSIONS:
        if start <= hour < end:
            return name
    return "late-us"


def closes_between(previous_ms: int, now_ms: int) -> list[str]:
    """Which sessions ENDED in (previous_ms, now_ms]. Usually none.

    Driven off the hour boundary rather than a timer, so a slow poll or a
    restart that straddles the close still reports it on the next pass instead
    of losing it. A gap longer than an hour can close more than one, which is
    why this returns a list.
    """
    if not previous_ms or now_ms <= previous_ms:
        return []
    ends = {end % 24: name for name, _start, end in SESSIONS}
    closed: list[str] = []
    # Walk the hour boundaries the poll stepped over.
    start_hour = dt.datetime.fromtimestamp(previous_ms / 1000, dt.UTC).replace(
        minute=0, second=0, microsecond=0
    )
    cursor = start_hour + dt.timedelta(hours=1)
    end = dt.datetime.fromtimestamp(now_ms / 1000, dt.UTC)
    while cursor <= end:
        name = ends.get(cursor.hour)
        if name and int(cursor.timestamp() * 1000) > previous_ms:
            closed.append(name)
        cursor += dt.timedelta(hours=1)
    return closed


@dataclass(frozen=True)
class SessionResult:
    name: str
    markets: int
    winners: int
    dollars: float

    @property
    def losers(self) -> int:
        return self.markets - self.winners

    @property
    def label(self) -> str:
        return LABELS.get(self.name, self.name)


def breakdown(rows: list[tuple[int, float]]) -> list[SessionResult]:
    """(window_ms, pnl) pairs -> one result per session, in session order.

    Sessions with no markets are dropped: a line of zeros says nothing and
    pushes the ones that matter off a phone screen.
    """
    buckets: dict[str, list[float]] = {name: [] for name, _s, _e in SESSIONS}
    for window_ms, pnl in rows:
        buckets[session_of(window_ms)].append(pnl)
    out = []
    for name, _start, _end in SESSIONS:
        values = buckets[name]
        if not values:
            continue
        out.append(
            SessionResult(
                name=name, markets=len(values),
                winners=sum(1 for v in values if v > 0),
                dollars=round(sum(values), 6),
            )
        )
    return out


def one_line(results: list[SessionResult]) -> str:
    """The compact breakdown that sits under the day's total."""
    if not results:
        return ""
    parts = [
        f"{r.label} {'+' if r.dollars >= 0 else '−'}${abs(r.dollars):,.2f} "
        f"({r.markets})"
        for r in results
    ]
    return "   ".join(parts)
