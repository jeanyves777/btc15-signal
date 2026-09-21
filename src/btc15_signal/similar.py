"""Similar-regime retrieval and action comparison. SHADOW ONLY.

The operator's specification, and the missing piece between a rule engine and
something that reasons: fingerprint the market at every poll, retrieve
COMPARABLE settled lifecycles, and compare the actions on what actually
happened to them - not on an opinion about what usually happens.

    "I found 37 comparable New York-morning markets. Entering now produced
     higher net profit than waiting, so enter."

Deterministic code retrieves the cohort and computes the probabilities, the
edge and the action comparison. The LLM's job is to explain the result, never
to produce it. That division is the operator's and it is the right one: a model
asked to estimate a win rate will produce a confident number from nothing.

**THIS DOES NOT TRADE.** Every read is recorded and shown; none of it reaches
the order path, the gates, the polling loop or the archive. Promotion to an
execution policy requires forward evidence that its calls beat the deployed
rule on realised P&L - which is exactly how the operator asked for it, and the
only way to find out whether retrieval adds anything or merely sounds clever.

TWO RULES BUILT IN, both from the operator:

1. A high win rate does not justify entry. 80% at 80c loses money after fees;
   80% at 75c is excellent. Every verdict here is computed on NET EDGE, never
   on win rate.
2. A small cohort is not evidence. The posterior win rate is shrunk toward the
   corpus base rate by cohort size, so a 3-market "cohort" reports very close
   to the base rate rather than 100%.
"""

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from .store import wilson_lower
from .validation import kalshi_fee_charged

COHORT_DB = "data/cohort.db"

# How far apart two markets may be and still be "comparable". Chosen to be
# generous - exact matches are rare and the operator said so - and then the
# cohort is ranked by weighted distance so the closest dominate anyway.
MAX_REMAINING_GAP = 2      # minutes
MAX_ASK_GAP = 0.06         # contract price
MAX_DISTANCE_GAP = 1.2     # normalized distance units
TARGET_COHORT = 60         # aim for this many neighbours
MIN_COHORT = 12            # below this, refuse to have an opinion

# Strength of the prior pulling a small cohort's win rate toward the base rate.
# 25 pseudo-observations: a 12-market cohort is still mostly prior, a
# 100-market cohort is mostly data.
PRIOR_STRENGTH = 25.0

# A price bucket wide enough to be well determined and narrow enough to still
# be about THIS price. Below this many rows the bucket is not trusted and the
# prior falls back to the market's own implied probability.
PRIOR_BAND = 0.03
PRIOR_MIN_ROWS = 200

# Below this many dipped markets the retracement arm has no opinion. Without
# it, 1-5 dipped markets that ALL lost still produced a positive WAIT edge,
# because the prior supplied the whole number.
MIN_DIP = 12

# A better price has to be better by more than a tick to count as a retracement
# worth waiting for.
MEANINGFUL = 0.01

# How far below the current ask a patient limit would rest.
RETRACE = 0.03


@dataclass(frozen=True)
class Fingerprint:
    remaining_s: int
    ask: float
    normalized_distance: float
    volatility_bps: float
    momentum_bps: float
    session: str
    vol_regime: str
    side_is_up: bool


@dataclass(frozen=True)
class CohortRead:
    n: int
    session: str
    ask: float
    base_rate: float
    raw_win_rate: float
    win_probability: float      # shrunk toward the PRICE-CONDITIONAL prior
    # Wilson bounds on the RAW count wins/n - NOT on `win_probability`. The
    # message printed them directly under the shrunk posterior, so the line
    # read as an interval for a number they are not about. They stay raw on
    # purpose: `edge_low`, the verdict, is taken on `win_low`, and an interval
    # around the posterior would lift that bound by borrowing confidence from
    # the prior - which is the move that said ENTER NOW on
    # KXBTC15M-26SEP211600-00. The message now names the rate they bound.
    win_low: float
    win_high: float
    prior: float                # what it was shrunk toward, and from where
    edge_low: float             # net edge at the lower bound - the verdict
    dip_n: int
    wait_limit_low: float
    # DOLLARS, like every sibling here. It was returned and archived in CENTS,
    # so shadow_decisions carried it 100x apart from `enter_now_net`,
    # `wait_real_net`, `wait_limit_net` and `edge_low` in the adjacent columns,
    # with nothing in the row saying which column was in which unit.
    net_edge_now: float
    fill_rate: float | None
    better_later_rate: float      # a cheaper ask appeared at SOME point (oracle)
    ran_away_rate: float
    cheaper_at_end_rate: float    # still cheaper at the last quote (realisable)
    mean_drift: float             # what the ask actually did, first to last
    enter_now_net: float
    wait_real_net: float        # implementable: take whatever is there later
    wait_oracle_net: float      # upper bound: cheapest ask, needs foresight
    wait_limit_net: float       # rest a limit below the ask; no fill = no trade
    dip_rate: float             # how often the price came back that far
    dip_price: float
    action: str
    reason: str
    corpus_id: str              # which corpus build this read came out of

    def as_message(self) -> str:
        """The operator's format, with the honest caveats attached."""
        lines = [
            "\U0001f9e0 <b>SIMILAR-REGIME READ</b> · <i>shadow, not trading</i>",
            f"Comparable unique markets: <b>{self.n}</b>",
            f"Session: {self.session}",
            f"Current ask: <b>{self.ask:.0%}</b>",
            f"Estimated win probability: <b>{self.win_probability:.0%}</b>"
            f" <i>(raw {self.raw_win_rate:.0%}, prior at this price {self.base_rate:.0%})</i>",
            f"95% interval on the raw {self.raw_win_rate:.0%}: "
            f"<b>[{self.win_low:.0%}, {self.win_high:.0%}]</b>"
            f" <i>- bounds the raw count, not the shrunk number above;"
            f" {self.n} markets settles nothing</i>",
            f"Net edge after costs: <b>{self.net_edge_now * 100:+.2f}c</b>"
            f" \u00b7 <b>{self.edge_low * 100:+.2f}c</b> at the 95% lower bound",
            "<b>Enter now</b>",
            f"  expected net edge {self.enter_now_net * 100:+.1f}c",
        ]
        if self.fill_rate is not None:
            lines.append(f"  live fill rate {self.fill_rate:.0%}")
        lines += [
            "<b>Wait for the final five minutes</b>",
            f"  ask drifted {self.mean_drift * 100:+.1f}c on average",
            f"  still cheaper at the last quote in "
            f"{self.cheaper_at_end_rate:.0%}",
            f"  a cheaper ask appeared at some point in "
            f"{self.better_later_rate:.0%} <i>(needs foresight to catch)</i>",
            f"  opportunity disappeared in {self.ran_away_rate:.0%}",
            f"  take whatever is there at the end: "
            f"{self.wait_real_net * 100:+.1f}c <i>(time decay - a late ask "
            f"is the price after the news)</i>",
            f"<b>Wait for a retracement</b> <i>(limit at "
            f"{self.dip_price:.0%})</i>",
            f"  price came back that far in {self.dip_rate:.0%} ({self.dip_n} markets)",
            f"  expected net edge {self.wait_limit_net * 100:+.1f}c "
            f"<i>(no fill = no trade)</i>",
            f"<b>Shadow preference: {self.action}</b>",
            f"<i>{self.reason}</i>",
        ]
        return "\n".join(lines)


def _distance(row: sqlite3.Row, fp: Fingerprint) -> float:
    """Weighted similarity. Lower is closer."""
    return (
        abs(row["remaining"] - fp.remaining_s / 60) / MAX_REMAINING_GAP
        + abs(row["ask"] - fp.ask) / MAX_ASK_GAP
        + abs(row["normalized_distance"] - fp.normalized_distance) / MAX_DISTANCE_GAP
        + (0.0 if row["vol_regime"] == fp.vol_regime else 0.5)
        + (0.0 if row["session"] == fp.session else 0.25)
    )


class Cohorts:
    """Read-only access to the precomputed corpus. Never on the order path."""

    def __init__(self, path: str = COHORT_DB) -> None:
        self.ok = Path(path).exists()
        self.db: sqlite3.Connection | None = None
        self.base_rate = 0.0
        # Which build of the corpus a read came from. Nothing recorded it, so a
        # shadow decision could not be reproduced once the cohort was rebuilt:
        # the same fingerprint retrieves a different cohort and the archived
        # row gives no way to tell that it did.
        self.corpus_id = ""
        if not self.ok:
            return
        self.db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        self.db.row_factory = sqlite3.Row
        row = self.db.execute("SELECT AVG(won) FROM cohort").fetchone()
        # Kept only for reporting. It must NOT be used as a prior: see
        # `prior_at`. A cached corpus-wide number is also not walk-forward,
        # so a decision shrunk toward it is informed by markets that had not
        # settled when it was taken.
        self.corpus_rate = float(row[0] or 0.0)
        # Read once, at construction - never per read, never on the order path.
        # Two numbers tell two builds apart: how many rows, and how recent the
        # newest market in them is.
        stamp = self.db.execute(
            "SELECT COUNT(*), MAX(open_ms) FROM cohort"
        ).fetchone()
        self.corpus_id = f"{int(stamp[0] or 0)}r@{int(stamp[1] or 0)}"

    def prior_at(self, price: float, as_of_ms: int | None = None) -> float:
        """The win rate to shrink toward, AT THIS PRICE and as of this moment.

        The prior used to be one number for the whole corpus, 0.789. That is
        not a price-free constant - it is the win rate at the corpus's AVERAGE
        ask, and the corpus is dominated by late, expensive quotes. Shrinking a
        cheap cohort toward it manufactures edge, and shrinking a dear one
        toward it destroys edge.

        It did exactly that on KXBTC15M-26SEP211600-00: 43/60 = 71.7% at an ask
        of 0.67 was pulled UP to 73.8%, so 2.13c of the reported +5.25c edge -
        41% of it - came from the prior rather than from the 60 comparable
        markets. That read said ENTER NOW and lost 68.55c.

        The fallback when a bucket is thin is the ASK ITSELF, which is the
        correct null: under a martingale price the contract wins at its price,
        so a cohort with nothing to say reports zero edge instead of inventing
        some.
        """
        if self.db is None:
            return price
        row = self.db.execute(
            "SELECT AVG(won), COUNT(*) FROM cohort "
            "WHERE ask BETWEEN ? AND ? AND open_ms + 900000 <= ?",
            (price - PRIOR_BAND, price + PRIOR_BAND,
             as_of_ms if as_of_ms is not None else 1 << 62),
        ).fetchone()
        if not row or (row[1] or 0) < PRIOR_MIN_ROWS or row[0] is None:
            return price
        return float(row[0])

    def neighbours(self, fp: Fingerprint, as_of_ms: int | None = None) -> list:
        """The closest comparable markets. ONE ROW PER MARKET, settled first.

        Two corrections, both of which decide whether this is intelligence or
        leakage dressed as intelligence:

        DEDUPLICATION. The corpus holds 74,582 decision minutes across 6,428
        markets, so a naive nearest-60 can return five minutes of the same
        lifecycle and call it five pieces of evidence. Measured before the fix,
        a 60-row cohort held as few as 51 distinct markets - and the bias is
        not random: a market that sat in the band for many minutes is
        disproportionately one that went on to win, so duplicates inflate the
        win rate in exactly the flattering direction. Only the closest row of
        each market survives, so `n` is always unique lifecycles.

        WALK-FORWARD. A comparable market must have SETTLED before the decision
        being scored. Live this is currently vacuous - the corpus ends
        2026-09-20 and every read is later - but it stops being vacuous the
        moment live markets join the corpus or a historical decision is
        replayed, and a leak found then would invalidate every number measured
        in between. A 15-minute market settles at `open_ms + 900_000`.
        """
        if self.db is None:
            return []
        minutes = fp.remaining_s / 60
        rows = self.db.execute(
            "SELECT * FROM cohort WHERE remaining BETWEEN ? AND ? "
            "AND ask BETWEEN ? AND ? AND normalized_distance BETWEEN ? AND ?",
            (
                minutes - MAX_REMAINING_GAP, minutes + MAX_REMAINING_GAP,
                fp.ask - MAX_ASK_GAP, fp.ask + MAX_ASK_GAP,
                fp.normalized_distance - MAX_DISTANCE_GAP,
                fp.normalized_distance + MAX_DISTANCE_GAP,
            ),
        ).fetchall()
        if as_of_ms is not None:
            rows = [r for r in rows if r["open_ms"] + 900_000 <= as_of_ms]
        rows.sort(key=lambda r: _distance(r, fp))
        seen: set[str] = set()
        unique = []
        for row in rows:
            if row["ticker"] in seen:
                continue
            seen.add(row["ticker"])
            unique.append(row)
            if len(unique) >= TARGET_COHORT:
                break
        return unique

    def read(
        self,
        fp: Fingerprint,
        fill_rate: float | None = None,
        as_of_ms: int | None = None,
    ) -> CohortRead | None:
        rows = self.neighbours(fp, as_of_ms)
        if len(rows) < MIN_COHORT:
            return None
        n = len(rows)
        wins = sum(r["won"] for r in rows)
        raw = wins / n
        # RULE 2: shrink toward the prior at THIS PRICE, by cohort size, so a
        # thin cohort cannot announce a number it has not earned - and cannot
        # borrow one from a corpus trading at a different price.
        prior = self.prior_at(fp.ask, as_of_ms)
        posterior = (wins + PRIOR_STRENGTH * prior) / (n + PRIOR_STRENGTH)
        # An interval, because "86% from 60 markets" reads as a fact and is
        # not one. A 1.57c edge off 60 neighbours is nowhere near established,
        # and the interval is the only part of the line that says so.
        win_low = wilson_lower(wins, n)
        win_high = 1.0 - wilson_lower(n - wins, n)

        fee = kalshi_fee_charged(fp.ask, 1)
        # RULE 1: the verdict is NET EDGE, never the win rate. 80% at 80c after
        # fee is a losing trade; the same 80% at 75c is a good one.
        net_now = posterior * 1.0 - fp.ask - fee

        # Both arms priced at OUR ask, on the SAME cohort, so the comparison
        # isolates the one thing waiting actually changes: the price. An
        # earlier version scored "enter now" at the cohort's own asks and the
        # headline edge at ours, and the two disagreed in sign on the same
        # read - which is not a close call, it is two different questions.
        #
        # For each comparable market, waiting would have had us pay our ask
        # plus the drift that market actually experienced, and the settlement
        # is unchanged. The win rate therefore cancels out of the difference,
        # correctly: waiting does not change which markets you are in, only
        # what you pay to be in them.
        drifts = [r["last_ask"] - r["ask"] for r in rows]
        mean_drift = sum(drifts) / n
        wait_price = min(0.99, max(0.01, fp.ask + mean_drift))

        enter_now = posterior - fp.ask - fee
        wait_real = posterior - wait_price - kalshi_fee_charged(wait_price, 1)
        # The upper bound: the cheapest ask each market ever showed afterwards.
        # No live rule can reach this - it needs foresight - and it is reported
        # only so that a waiting policy is never judged against a fantasy.
        oracle_drift = sum(r["best_later_ask"] - r["ask"] for r in rows) / n
        oracle_price = min(0.99, max(0.01, fp.ask + oracle_drift))
        wait_oracle = (
            posterior - oracle_price - kalshi_fee_charged(oracle_price, 1)
        )
        net_now = enter_now

        # THREE REAL POLICIES, not two and a straw man.
        #
        # "Wait and take whatever is there" ends up paying something like 95c,
        # and that is NOT an artifact to be argued away: a 15-minute contract
        # decays toward its outcome, so late prices are simply the price after
        # the information has arrived. It happens regardless and the comparison
        # has to carry it. What it shows is precisely why waiting is expensive
        # - you end up buying certainty you could have bought cheaply.
        #
        # The other waiting policy is the one worth testing: rest a limit BELOW
        # the current ask and trade only if the price comes back.
        #
        #     fill in P(dip), at the discount, with that market's outcome
        #     no fill otherwise, and no trade - which is worth exactly zero
        #
        # That last term is the honest part. Waiting is not free optionality;
        # its cost is the setups that never come back, and those contribute
        # nothing rather than contributing their winnings.
        dip_price = round(max(0.01, fp.ask - RETRACE), 4)
        dip_fee = kalshi_fee_charged(dip_price, 1)
        dipped = [r for r in rows if r["best_later_ask"] <= dip_price + 1e-9]
        dip_rate = len(dipped) / n
        dip_n = len(dipped)
        if dip_n >= MIN_DIP:
            dip_wins = sum(r["won"] for r in dipped)
            # Anchored at the DIP price, not at the current ask: the question
            # is what a contract bought at `dip_price` wins, and the prior has
            # to be about that price or it drags the answer toward a different
            # market. Its own lower bound gates the arm, exactly as the
            # enter-now bound gates that one.
            dip_prior = self.prior_at(dip_price, as_of_ms)
            dip_posterior = (
                (dip_wins + PRIOR_STRENGTH * dip_prior) / (dip_n + PRIOR_STRENGTH)
            )
            dip_low = wilson_lower(dip_wins, dip_n)
            wait_limit = dip_rate * (dip_posterior - dip_price - dip_fee)
            wait_limit_low = dip_rate * (dip_low - dip_price - dip_fee)
        else:
            # Fewer than MIN_DIP dipped markets is not evidence. Previously
            # 1-5 of them, all losers, still produced a positive WAIT edge
            # because the prior supplied the entire number.
            dip_posterior = dip_low = dip_price
            wait_limit = wait_limit_low = 0.0

        better = sum(1 for r in rows if r["best_later_ask"] < r["ask"] - MEANINGFUL)
        cheaper_at_end = sum(1 for r in rows if r["last_ask"] < r["ask"] - MEANINGFUL)
        gone = sum(1 for r in rows if r["ran_away"])

        # THE DECISION IS TAKEN ON THE BOUND, NOT THE POINT ESTIMATE.
        #
        # `win_low` was computed, printed and archived and then consulted
        # nowhere: the verdict ran entirely on `posterior`. On
        # KXBTC15M-26SEP211600-00 that point estimate said +5.25c and ENTER
        # NOW, while the 95% interval [59.2%, 81.5%] straddled the 68.55%
        # break-even and its lower bound was NINE POINTS short. The trade lost
        # 68.55c. An interval that does not enter the decision is decoration.
        edge_low = win_low - fp.ask - fee

        if enter_now <= 0 and wait_limit <= 0:
            action = "PASS"
            reason = (
                f"net edge {enter_now * 100:+.1f}c at {fp.ask:.0%} and "
                f"{wait_limit * 100:+.1f}c waiting - a {posterior:.0%} win rate "
                f"does not pay at this price"
            )
        elif edge_low <= 0 and wait_limit_low <= 0:
            # The point estimate pays and the bound does not. That is the
            # single most dangerous state this layer can be in, and it now has
            # a name instead of being rounded up to ENTER NOW.
            action = "UNCERTAIN"
            reason = (
                f"{n} markets put the win rate at {posterior:.0%} "
                f"({enter_now * 100:+.1f}c) but the 95% lower bound is "
                f"{win_low:.0%}, short of the {fp.ask + fee:.1%} break-even - "
                f"the edge is {edge_low * 100:+.1f}c at the bound and is NOT "
                f"established"
            )
        elif wait_limit_low > edge_low:
            action = "WAIT FOR RETRACEMENT"
            reason = (
                f"a limit at {dip_price:.0%} filled in {dip_rate:.0%} of {n} "
                f"comparable markets and is worth "
                f"{wait_limit_low * 100:+.1f}c at its own lower bound against "
                f"{edge_low * 100:+.1f}c for entering now"
            )
        else:
            action = "ENTER NOW"
            reason = (
                f"even the 95% lower bound of {win_low:.0%} clears the "
                f"{fp.ask + fee:.1%} break-even, worth "
                f"{edge_low * 100:+.1f}c across {n} comparable markets; the "
                f"ask drifted {mean_drift * 100:+.1f}c and a limit at "
                f"{dip_price:.0%} was worth {wait_limit_low * 100:+.1f}c"
            )
        return CohortRead(
            n=n, session=fp.session, ask=fp.ask, base_rate=prior,
            raw_win_rate=raw, win_probability=posterior,
            win_low=win_low, win_high=win_high, prior=prior,
            edge_low=edge_low, dip_n=dip_n, wait_limit_low=wait_limit_low,
            net_edge_now=net_now, fill_rate=fill_rate,
            better_later_rate=better / n, ran_away_rate=gone / n,
            cheaper_at_end_rate=cheaper_at_end / n, mean_drift=mean_drift,
            enter_now_net=enter_now, wait_real_net=wait_real,
            wait_oracle_net=wait_oracle, wait_limit_net=wait_limit,
            dip_rate=dip_rate, dip_price=dip_price,
            action=action, reason=reason, corpus_id=self.corpus_id,
        )
