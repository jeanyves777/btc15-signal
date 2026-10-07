"""What happened to a MARKET across its whole life - not at one instant.

Distinct from `lifecycle.py`, which reconstructs the price path of a trade that
was already taken (MFE, MAE, exit policies). This module answers the earlier
question: was there ever an executable opportunity in this market at all, and
if so what became of it.

WHY IT EXISTS. `predictions.qualified` is a snapshot taken when the alert was
written. Read as the market's verdict it classifies an entire 15-minute
contract from its FIRST evaluation, and on 2026-09-25 that produced a false
conclusion: BTC appeared to have qualified 4 signals and declined 52 that went
on to score +7.7%, which reads as a gate selecting the wrong setups. In fact 9
of those "declined" markets had been TRADED - 9W/0L at a 0.829 price - and BTC
had filled 13 markets, not 4. Classified by whether a market was EVER eligible,
the same data says the opposite: +8.5% eligible against +4.4% never eligible.

The archive already held the truth. `intelligence_decisions` records every
evaluation - a median of 27 per market - and shows that a failed distance check
rejects THAT MOMENT and nothing more: of 55 BTC markets that failed distance,
evaluation continued past it in all 55, and 11 later became eligible. Nothing
read it that way, so nothing reported it. The trading path was never at fault.

WHAT THIS MODULE DOES NOT DO. It derives; it never gates, sizes or orders. It
is read-only over tables the service already writes, so it cannot change what
trades. `qualified_at_alert` is preserved exactly as recorded - it is a real
fact about the alert, and the defect was reading it as a fact about the market.

THE FOUR QUESTIONS IT SEPARATES, which "did the declined market win?" cannot:

    never_eligible          no evaluation ever passed every gate
    became_eligible         at least one did, and when
    eligible_not_executed   eligible, but no order resulted, with the reason
    traded                  an order was filled

A market winning proves nothing about whether it offered an executable entry.
That distinction is the whole point of the module.
"""

from __future__ import annotations

import sqlite3
import time
from collections import defaultdict
from dataclasses import dataclass, field

# Execution outcomes, kept separate from eligibility. A market can be eligible
# and untraded for reasons that are not the strategy's - authorisation is off,
# a capital guard refused, the order never filled - and folding those together
# with "the rule said no" is how an execution SETTING gets read as a strategy
# failure. Gold running with auto-execution off is deliberate, and this
# vocabulary is what lets a report say so.
NO_PROPOSAL = "no_proposal"
# TWO DIFFERENT FACTS, deliberately not one. `auto_disabled` means automation
# is switched off for this instance, so a resting proposal is the normal state
# and no approval was ever requested - gold's 32 eligible markets are all of
# this kind. `awaiting_authorization` means automation IS on and a specific
# approval is outstanding, which is a thing someone can act on.
#
# Merging them would reintroduce the ambiguity this module exists to remove:
# one says nothing happened because nothing was asked, the other says something
# was asked and has not been answered.
AUTO_DISABLED = "auto_disabled"
AWAITING_AUTHORIZATION = "awaiting_authorization"
# A proposal that lapsed without an order. On all three live instances EVERY
# resting proposal is of this kind - 328, 72 and 38 of them, none with an order
# id, all past an expiry set about two minutes after creation.
EXPIRED = "expired"

# AUTHORIZATION IS ITS OWN FACT, not an inference from the automation setting.
# Automation being on does not prove a request was made; it only means one was
# not required. There is currently NO approval-request record in the schema -
# notification kinds are settlement, signal, fill, cash_out, session_close,
# learning and recovery, none of which is a request - so `NEVER_REQUESTED` is
# the only value the record can support today. A status of
# `awaiting_authorization` requires REQUESTED and an unexpired request; absent
# that record it must never be claimed.
REQUESTED = "requested"
APPROVED = "approved"
DECLINED = "declined"
# NOT "never_requested". The absence of a record does not establish the
# absence of the event - without instrumentation for approval requests, all
# that can be said is that none was recorded. Claiming a request never
# happened is the same over-reading as claiming one is outstanding.
NOT_RECORDED = "not_recorded"

# When no recorded event explains why a proposal lapsed, say so.
#
# "Automation was off" and "no order was submitted" are TWO OBSERVED FACTS.
# Joining them with "so" is a causal claim, and nothing in the archive records
# a blocker that links them - the order might have failed to submit for some
# entirely different reason on an instance that also happened to have
# automation off. A reason is stated only where a recorded event supports it,
# which in practice means `result_note`.
REASON_UNKNOWN = "expired without submission - reason unknown"
BLOCKED = "blocked"
SUBMITTED_UNFILLED = "submitted_unfilled"
PARTIALLY_FILLED = "partially_filled"
FILLED = "filled"

# Eligibility, from the decision sequence rather than the alert.
NEVER_ELIGIBLE = "never_eligible"
ELIGIBLE_AT_FIRST = "eligible_at_first"
BECAME_ELIGIBLE = "became_eligible"

# Outcome, with "not settled yet" distinct from "no record". Collapsing those
# was how an ETH count came out at 15 against 16 eligible markets; the cause
# was an anchor mismatch, but a vocabulary that cannot express "pending" makes
# such a gap invisible rather than reportable.
WON = "won"
LOST = "lost"
PENDING = "pending"
NO_RECORD = "no_record"


@dataclass
class MarketLife:
    """One market, from first evaluation to settlement."""

    ticker: str
    evaluations: int = 0
    # The alert-time snapshot, preserved. A fact about the alert, never read
    # as the market's classification.
    qualified_at_alert: bool | None = None
    eligibility: str = NEVER_ELIGIBLE
    first_eligible_ms: int | None = None
    first_eligible_remaining_s: int | None = None
    # Every flip between eligible and not, so "it qualified once at second 612"
    # can be told from "it qualified for nine minutes".
    transitions: list[tuple[int, bool]] = field(default_factory=list)
    eligible_evaluations: int = 0
    first_failed_gates: str = ""
    execution: str = NO_PROPOSAL
    # The four facts, kept apart. `execution` is the status; `automation_on`
    # is the setting as it stood; `authorization` is what was actually asked
    # and answered; `execution_reason` is only ever evidence-backed.
    # TODAY'S setting, not the setting as it stood. `settings` holds one
    # current value with no history table, so the value at the decision
    # instant is not recoverable - hence the name. None means no row was
    # recorded at all, which is distinct from a row recording False.
    automation_on_at_snapshot: bool | None = None
    authorization: str = NOT_RECORDED
    execution_reason: str = ""
    order_ids: list[str] = field(default_factory=list)
    fill_price: float | None = None
    outcome: str = NO_RECORD
    contract_price: float | None = None

    @property
    def ever_eligible(self) -> bool:
        return self.eligibility != NEVER_ELIGIBLE

    @property
    def traded(self) -> bool:
        return self.execution in (FILLED, PARTIALLY_FILLED)


def _execution_state(proposals: list[dict], auto_enabled: bool = True,
                     now_ms: int = 0) -> tuple[str, str, list[str]]:
    """Which of the six execution outcomes, with the reason and order ids.

    Ordered by precedence rather than by row: a market with a filled proposal
    and a later cancelled one has traded. `exited` means filled and then sold,
    which is still a fill.
    """
    if not proposals:
        return NO_PROPOSAL, "", []
    ids = [p["entry_order_id"] for p in proposals if p.get("entry_order_id")]
    filled = [p for p in proposals if p.get("fill_price") is not None]
    if filled:
        # A partial is a fill of fewer contracts than were asked for. Recorded
        # separately because sizing that did not happen is not a fill that did.
        for p in filled:
            want, got = p.get("count"), p.get("filled_count")
            if want and got is not None and got < want:
                return PARTIALLY_FILLED, f"{got} of {want} contracts", ids
        return FILLED, "", ids
    statuses = {str(p.get("status") or "").lower() for p in proposals}
    if "rejected" in statuses:
        note = next((str(p.get("result_note") or "") for p in proposals
                     if str(p.get("status") or "").lower() == "rejected"), "")
        return BLOCKED, note, ids
    if statuses & {"unfilled", "expired", "cancelled"}:
        note = next((str(p.get("result_note") or "") for p in proposals
                     if p.get("result_note")), "")
        return SUBMITTED_UNFILLED, note, ids
    if "pending" in statuses:
        # Never submitted - and three different facts hide under that, so the
        # expiry decides first. A proposal past its expiry is not waiting on
        # anybody: it lapsed. Only a proposal still inside its window can be
        # said to be waiting, and then automation decides on WHAT - with it
        # off nobody was ever asked, with it on an approval really is open.
        resting = [p for p in proposals
                   if str(p.get("status") or "").lower() == "pending"]
        live = [p for p in resting
                if p.get("expires_at") is None
                or (now_ms and p["expires_at"] >= now_ms)]
        if not live:
            # A recorded note is evidence; everything else is inference. The
            # automation setting is reported in its own field and deliberately
            # NOT used to explain the lapse, because "automation was off" and
            # "nothing was submitted" are two observations, not a cause.
            note = next((str(p.get("result_note") or "") for p in resting
                         if p.get("result_note")), "")
            return EXPIRED, (note or REASON_UNKNOWN), ids
        if auto_enabled is False:
            return AUTO_DISABLED, "automation not enabled at snapshot", ids
        # Still inside its window, automation on. This is NOT an outstanding
        # approval unless a request was recorded - see `authorization`.
        return NO_PROPOSAL, REASON_UNKNOWN, ids
    return NO_PROPOSAL, ", ".join(sorted(statuses)), ids


def automation_enabled(db: sqlite3.Connection) -> bool | None:
    """Today's automation setting, or None if none is recorded.

    NOT the setting as it stood at any decision - `settings` keeps one current
    value and there is no history table, so the historical value is simply not
    available. Callers must treat this as a snapshot.

    None and False are different: gold carries no `auto_trade_enabled` row at
    all, which is an absence of record rather than a recorded "off".
    """
    try:
        row = db.execute(
            "SELECT value FROM settings WHERE key='auto_trade_enabled'"
        ).fetchone()
    except sqlite3.Error:
        return None
    if row is None or row[0] is None:
        return None
    return bool(float(row[0]))


def build(db_path: str, since_ms: int = 0) -> dict[str, MarketLife]:
    """Derive every market's life from what the service already recorded.

    `since_ms` filters on the DECISION time, not the window open: a market
    whose window opened before a deploy but which was evaluated after it
    belongs to the new configuration. Mixing the two anchors is what made 16
    eligible ETH markets report as 15.
    """
    db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row

    lives: dict[str, MarketLife] = {}
    seq: dict[str, list[dict]] = defaultdict(list)
    for r in db.execute(
            "SELECT ticker, decided_ms, remaining_s, base_qualified, "
            "       failed_gates, ask FROM intelligence_decisions "
            " WHERE decided_ms >= ? ORDER BY ticker, decided_ms", (since_ms,)):
        seq[r["ticker"]].append(dict(r))

    for ticker, rows in seq.items():
        life = MarketLife(ticker=ticker, evaluations=len(rows))
        life.first_failed_gates = str(rows[0].get("failed_gates") or "")
        was = None
        for row in rows:
            ok = bool(row["base_qualified"])
            if ok:
                life.eligible_evaluations += 1
                if life.first_eligible_ms is None:
                    life.first_eligible_ms = row["decided_ms"]
                    life.first_eligible_remaining_s = row["remaining_s"]
            if was is None or ok != was:
                life.transitions.append((row["decided_ms"], ok))
                was = ok
        if life.first_eligible_ms is not None:
            life.eligibility = (ELIGIBLE_AT_FIRST
                                if bool(rows[0]["base_qualified"])
                                else BECAME_ELIGIBLE)
        lives[ticker] = life

    proposals: dict[str, list[dict]] = defaultdict(list)
    columns = {r[1] for r in db.execute("PRAGMA table_info(trade_proposals)")}
    extra = ", filled_count" if "filled_count" in columns else ""
    for r in db.execute(
            f"SELECT ticker, status, fill_price, count, entry_order_id, "
            f"       result_note, expires_at{extra} FROM trade_proposals"):
        proposals[r["ticker"]].append(dict(r))
    auto = automation_enabled(db)
    now_ms = int(time.time() * 1000)
    for ticker, life in lives.items():
        state, reason, ids = _execution_state(
            proposals.get(ticker, []), auto, now_ms)
        life.execution, life.execution_reason, life.order_ids = (
            state, reason, ids)
        # The setting as it stood, recorded beside the status rather than
        # folded into it. `authorization` stays NEVER_REQUESTED until the
        # schema carries approval-request events; claiming otherwise would be
        # an inference presented as a record.
        life.automation_on_at_snapshot = auto
        got = [p for p in proposals.get(ticker, [])
               if p.get("fill_price") is not None]
        if got:
            life.fill_price = got[0]["fill_price"]

    for r in db.execute(
            "SELECT contract_ticker, won, contract_price, qualified "
            "  FROM predictions WHERE contract_ticker IS NOT NULL"):
        life = lives.get(r["contract_ticker"])
        if life is None:
            continue
        life.contract_price = r["contract_price"]
        life.outcome = (PENDING if r["won"] is None
                        else (WON if r["won"] else LOST))
        # The alert-time snapshot, kept as itself.
        life.qualified_at_alert = bool(r["qualified"])

    db.close()
    return lives
