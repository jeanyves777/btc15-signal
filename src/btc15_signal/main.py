import asyncio
import hashlib
import json
import time
import traceback
from dataclasses import replace
from datetime import UTC, datetime
from zoneinfo import ZoneInfo
from html import escape
from pathlib import Path
from uuid import NAMESPACE_URL, uuid4, uuid5

import httpx

from . import autotrade, combo_recovery, intel_mode, kalshi_signal, messages, revision, surface
from . import brain as brain_mod
from . import intelligence_policy as intel
from .adaptive import brti_vol_regime, context_of, setup_context_of
from .snapshot import MarketSnapshot
from .candidates import CandidateSet
from .capital import CapitalController, ny_day
from .config import Settings
from .decision import decision_facts
from .execution import KalshiExecutionClient, fresh_order_event
from .mirror import (
    COPY_MISSED_ID,
    COPY_NOTE,
    MirroringExecutionClient,
    current_instance,
    mirror_allowed,
    targets_from_settings,
)
from .features import _session
from .hourly_shadow import HourlyShadow
from .kalshi import KalshiClient, KalshiMarket
from .kalshi_brti import KalshiBRTIRule
from .learning_runner import LearningRunner
from .levels import LevelTracker
from .levels import confidence_points as level_points
from .model import predict
from .notify import Notifier
from .recovery_add_runner import RecoveryAddRunner
from .reference_shadow import Crossing, ReferenceShadow
from .regime import base_points as regime_base_points
from .regime import confidence_points as regime_confidence_points
from .regime import label_for as regime_label
from .regime import model_points as regime_model_points
from .regime import weight_at, weight_for_hour
from .sessions import (
    breakdown as session_breakdown,
)
from .sessions import (
    closes_between,
)
from .similar import Cohorts, Fingerprint
from .store import RecoveryState, Store, TradeProposal
from .store import position_pnl as store_position_pnl
from .strategy import EntryRule, ReversionRule, ReversionSetup
from .telegram import Telegram
from .validation import contracts_for_budget, kalshi_fee_charged


def report_sizing(settings: Settings) -> tuple[dict, str]:
    """How the PAPER per-signal P&L is sized, and the label that says so.

    This sizes the hypothetical figure covering every settled signal, most of
    which were never traded. Real money never comes from here - it comes from
    `Store.realised_record`, which uses the count, price and fee of orders that
    actually filled.

    The default is one contract, the unit the edge was measured in
    (+$0.0214/contract) and the unit the bot actually orders at a $1 budget.
    It used to be ten contracts, a leftover from paper testing, which reported
    -$3.91 on a night whose real loss was $0.85.

    "payout" remains available for Kalshi's own convention, where a "$10
    position" means $10 of max payout - 10 contracts, costing 10 x price, so
    $8.68 at 86.8c rather than $10.
    """
    if settings.report_basis == "cash":
        return {"cash": settings.dashboard_stake}, f"${settings.dashboard_stake:,.0f} cash"
    if settings.report_basis == "payout":
        # Each contract pays $1, so a $10 max payout is simply 10 contracts.
        return (
            {"contracts": settings.report_payout},
            f"${settings.report_payout:,.0f} max payout",
        )
    count = float(settings.trade_contract_count)
    return {"contracts": count}, f"{count:g} contract" + ("s" if count != 1 else "")


# Per-contract edge by session, measured over 22,560 historical entries in the
# 0.70-0.99 band at 6-11 minutes remaining, clustered by market. Asia and the
# US afternoon have intervals straddling zero; the rest do not. Shown beside the
# live record so drift is visible, never used to gate a trade.
# Per-contract edge measured for the deployed band (0.70-0.99, 6-11 min):
# +0.0166, 95% CI [+0.0084, +0.0247] over 22,560 clustered entries. Used to
# say what a trade is worth AFTER costs, rather than letting the model guess.
MEASURED_BAND_EDGE = 0.0166

# Per-price net-of-fee edge, from the bucket study. Outside these ranges we have
# NOT measured an edge, and saying "+0.0006 expected edge" about a 65c contract
# - as the commentary did on 2026-09-21 - invents a number for a price nobody
# studied. `None` means exactly that: unknown, not zero.
MEASURED_BUCKETS = (
    (0.85, 0.90, 0.0177),   # CI [+0.0048, +0.0298] - the only bucket excluding zero
    (0.90, 0.93, 0.0114),   # CI [-0.0015, +0.0236] - marginal
)


def measured_edge_at(price: float) -> float | None:
    """Gross edge measured for this price, or None where none was measured."""
    for low, high, edge in MEASURED_BUCKETS:
        if low <= price < high:
            return edge
    return None

COHORTS = Cohorts()

MEASURED_SESSION_EDGE = {
    "asia": 0.0093,
    "europe": 0.0213,
    "us": 0.0223,
    "late-us": 0.0266,
}


def samples_needed(store: Store, effect: float = 0.01) -> int:
    """Settled signals needed before an edge of `effect` separates from luck.

    Uses the average price actually traded, because the variance of a binary
    payoff is p(1-p) and that collapses at the favourite prices this strategy
    lives at - which is the one thing working in our favour on sample size.
    """
    rows = store.db.execute(
        "SELECT AVG(contract_price) FROM predictions "
        "WHERE won IS NOT NULL AND contract_price IS NOT NULL"
    ).fetchone()
    price = rows[0] if rows and rows[0] else 0.85
    variance = price * (1 - price)
    return int((1.96 * (variance ** 0.5) / effect) ** 2) if variance else 0


def choppiness_points(choppiness: float | None, penalty: int) -> int:
    """Confidence points to subtract for a window that went nowhere.

    CONFIDENCE ONLY. The operator's instruction was explicit - choppiness
    influences confidence and nothing else - so this returns POINTS, it is
    consumed by `confidence_label`, and it appears in no `check_facts` list.
    There is no threshold for a setup to fail on it and no branch anywhere
    that can turn it into a refusal. A number that cannot reach a gate cannot
    accidentally become one.

    Proportional rather than stepped: a 0.9 window is not the same as a 0.5
    one, and a cliff would make the word flip on a rounding change.
    """
    if choppiness is None:
        return 0
    return -int(round(max(0.0, min(1.0, choppiness)) * max(0, penalty)))


def confidence_label(facts: list[dict], opened: int, blocking_level: float | None,
                     intelligence_delta: int = 0,
                     choppiness_delta: int = 0) -> str:
    """HIGH / MEDIUM / LOW for the signal header.

    Scored from the SAME facts the checks are rendered from, so the word and
    the ticks below it can never disagree - and through the same regime
    arithmetic `entry_context` uses, so the header and the DETAILS breakdown
    are two views of one number rather than two numbers.

    The clock and the protective level adjust it and never gate it: both were
    tried as gates and both measured as noise (FINDINGS 23, p=0.090 and
    p=0.542). The operator's standing rule is that time of day may raise or
    lower confidence but can never stop the 15-minute system.

    `intelligence_delta` is the learned calibration, and it enters HERE rather
    than being rendered as a separate line beside an unchanged word. A layer
    that reports "confidence lowered" next to a header still reading HIGH has
    not lowered confidence; it has printed a sentence. The delta is on the same
    0-100 points scale the rest of this function works in, it is clamped with
    everything else, and it can only move the LABEL - it reaches no gate, no
    order and no size, which is the whole of a confidence adjustment's
    authority.

    `choppiness_delta` is the second such adjustment and carries exactly the
    same authority: none beyond the word. A window that crossed the strike six
    times and finished where it started is a coin toss whatever the gates say,
    and this is where that gets said - not in a refusal.
    """
    return regime_label(
        max(0, min(100, model_confidence_points(facts, opened, blocking_level)
                   + int(intelligence_delta or 0)
                   + int(choppiness_delta or 0)))
    )


def confidence_breakdown(facts: list[dict], opened: int,
                         blocking_level: float | None,
                         intelligence_delta: int = 0,
                         choppiness: float | None = None,
                         choppiness_delta: int = 0) -> dict:
    """Every term behind the confidence word, and the word itself.

    ONE COMPUTATION FOR BOTH MESSAGES. `fill_message` used to call
    `confidence_label(fill_facts, opened, blocking_level)` with no deltas
    while the signal that preceded it passed both, so the same trade could
    alert MEDIUM and fill HIGH - the exact thing `signal_message` promises in
    its docstring cannot happen. The label here is computed once, from these
    components, and handed to whoever renders it.
    """
    from .regime import HIGH_AT, base_points as regime_base
    from .regime import confidence_points as regime_clock
    from .regime import weight_at

    agreeing = sum(1 for fact in facts if fact["passed"])
    failed = len(facts) - agreeing
    checks = regime_base(agreeing)
    clock = regime_clock(weight_at(opened))
    shield = level_points(blocking_level is not None)
    # A PENALTY MUST NOT BE ABSORBED BY THE CLAMP ON THE BONUSES.
    #
    # This summed everything and clamped once, so a setup whose positive
    # terms already exceeded 100 paid nothing for being choppy. On
    # 2026-09-24 both instruments took the 16:00 window at 5/5 and
    # "Score 100/100 · HIGH": BTC with choppiness 0.94 and a -14 penalty,
    # ETH with 0.87 and -13. Both sums were 104 and 114 before the clamp, so
    # BOTH PENALTIES COST NOTHING and a 94%-choppy market scored exactly what
    # a perfectly clean one would. Both lost.
    #
    # That is the opposite of what choppiness is for. It exists to mark the
    # windows that went nowhere, and the strongest-looking setups are
    # precisely where a false HIGH is most expensive.
    #
    # So the bonuses are clamped FIRST and the penalty applied after. The
    # archive says it earns its place: BTC qualified setups at choppiness
    # >= 0.90 won 45.0% against an 86.1% price, a -41.1% residual over 20
    # decisions, while the 0.50-0.75 band returned +9.8%.
    base = max(0, min(100, checks + clock + shield
                      + int(intelligence_delta or 0)))
    points = max(0, min(100, base + int(choppiness_delta or 0)))
    label = regime_label(points)
    # A REFUSED SETUP IS NOT A MEDIUM ONE. Operator, 2026-09-24, on
    # KXBTC15M-26SEP241015-15: 65c against a 70-93c band and 3.5x against a
    # 10x floor - two gates failed - and the header still read MEDIUM,
    # because 3 of 5 agreeing scores 70 and the clock added 21 on top.
    #
    # The old rule was that confidence describes how good the setup LOOKS and
    # says nothing about whether it may be traded. That reads as a
    # contradiction on every refused signal, and the operator reads the word
    # before the ticks. A setup a gate refused is LOW, whatever the
    # arithmetic underneath says - and the arithmetic is still printed in
    # full on the score line, so nothing is hidden by the cap.
    #
    # POINTS ARE NOT TOUCHED. `model_confidence_points` feeds the training
    # corpus through the same `regime_model_points`, and moving it would
    # silently re-score every row the calibration compares against. This caps
    # the WORD only.
    if failed:
        label = "LOW"
    return {
        "points": points, "high_at": HIGH_AT, "checks": checks,
        "clock": clock, "shield": shield,
        "intelligence": int(intelligence_delta or 0),
        "choppiness": choppiness,
        "choppiness_points": int(choppiness_delta or 0),
        "failed": failed,
        "label": label,
    }


def model_confidence_points(facts: list[dict], opened: int,
                            blocking_level: float | None) -> int:
    """The model's own score, BEFORE any learned adjustment.

    Recorded on every decision so the confidence it produced can later be
    compared with what actually happened. A calibration needs the prediction
    the model made at the time; reconstructing it afterwards from a label is
    guessing, and reconstructing it from today's code measures today's code.
    """
    agreeing = sum(1 for fact in facts if fact["passed"])
    return regime_model_points(
        agreeing, opened, level_points(blocking_level is not None)
    )


def record_block(store: Store, settings: Settings) -> str:
    """Both running records, as a FOOTER rather than a header.

    Three lines: signal accuracy, the paper figure on the size the bot orders,
    and the account. They answer different questions and are never merged -
    merging them once reported -$3.91 on a night whose real loss was $0.85.

    Moved below the alert rather than above it. Leading every message with
    three lines of statistics pushed the side, the price and the checks - the
    things acted on - down the screen; dropping them entirely, which the first
    version of this layout did, lost the signal record the operator tracks.
    """
    return head_for(store, settings)


def head_for(store: Store, settings: Settings) -> str:
    """The scoreboard header every Telegram message opens with.

    A single function because there were eight identical copies of this
    expression, which is precisely why one stale default - reporting at ten
    contracts while the bot ordered one - was wrong in eight places at once.
    """
    sizing, basis = report_sizing(settings)
    # The basis is passed through rather than hardcoded: the header said
    # "per contract" whatever report_basis actually was, so a payout basis
    # labelled a ten-contract figure as a one-contract one.
    return messages.scoreboard(
        *store.scoreboard(**sizing), basis=basis, live=store.money_snapshot()
    )


def budget_for(store: Store, settings: Settings, auto: bool) -> float:
    """Dollars to spend on one order, runtime override winning over .env."""
    key = "auto_budget" if auto else "manual_budget"
    default = settings.auto_budget if auto else settings.manual_budget
    return store.get_setting(key, default)


def handle_size_command(
    store: Store, settings: Settings, command: str, now_ms: int
) -> str:
    """`/size` and `/autosize`: read or set the dollar budget per order.

    Kept as a pure function so the parsing, the bounds and the wording are
    testable without a Telegram client. Sizing is stored in the database rather
    than .env precisely because .env needs a restart, and a restart in the
    middle of an entry window is how the service got killed once already.
    """
    parts = command.split(maxsplit=1)
    auto = parts[0] == "/autosize"
    key = "auto_budget" if auto else "manual_budget"
    label = "Auto" if auto else "Manual"

    if len(parts) < 2 or not parts[1].strip():
        current = budget_for(store, settings, auto)
        return (
            f"\U0001f4b5 <b>{label} size</b> is <code>${current:,.2f}</code> per order.\n"
            f"<i>Change it with</i> <code>{parts[0]} 5</code>"
        )
    try:
        amount = float(parts[1].strip().lstrip("$").replace(",", ""))
    except ValueError:
        return f"⚠️ Could not read an amount from <code>{escape(parts[1].strip())}</code>"
    if not 0 < amount <= settings.max_budget:
        return (
            f"⚠️ Size must be between $0 and ${settings.max_budget:,.0f}. "
            f"Asked for ${amount:,.2f}."
        )
    store.set_setting(key, amount, now_ms)
    # Illustrated across the band the rule actually trades rather than at one
    # invented price: cost varies by about 12% between 85c and 95c, and the
    # single 90c example was never the price any order paid.
    low, high = contracts_for_budget(amount, 0.85), contracts_for_budget(amount, 0.95)
    lines = [
        f"✅ <b>{label} size set to ${amount:,.2f}</b> per order.",
        f"<i>In the 85–95c band that buys {high}–{low} contract(s), "
        f"costing ${high * 0.95:,.2f}–${low * 0.85:,.2f}.</i>",
    ]
    if amount < 0.95:
        # contracts_for_budget floors at one contract, because an order for
        # less than one cannot exist. So a sub-contract budget does not buy a
        # fraction - it quietly spends more than asked, and that must be said.
        lines.append(
            f"⚠️ <i>Below the cost of one contract: an order still buys one, so "
            f"up to ${0.95 - amount:,.2f} more than ${amount:,.2f} may be spent.</i>"
        )
    return "\n".join(lines)


def scaled_loss_limit(store: Store, settings: Settings,
                      now_ms: int | None = None) -> float:
    """The day's loss floor, scaled with the base size.

    OPERATOR, 2026-09-27: "loss limit must scale" - with the base, which
    scales with capital uncapped (FINDINGS 106). The configured limit (the
    instance's AUTO_DAILY_LOSS_LIMIT, or a Telegram override) is the limit for
    `loss_limit_reference_base` contracts - 2, the base it was set for - and
    it moves in proportion: at base 4, SOL's $5 is $10, ETH's $10 is $20,
    BTC's $20 is $40. So the floor allows the same NUMBER of losses per day
    whatever the size. Before the day's capital review the base reads 1, so
    the floor is briefly TIGHTER, never looser.
    """
    raw = float(store.get_setting(
        "auto_daily_loss_limit", settings.auto_daily_loss_limit))
    if not (settings.loss_limit_scales_with_base and settings.capital_sizing_enabled):
        return raw
    now_ms = int(time.time() * 1000) if now_ms is None else now_ms
    reference = max(1, int(settings.loss_limit_reference_base))
    return round(raw * store.base_tier_at(now_ms) / reference, 2)


def auto_limits(store: Store, settings: Settings,
                now_ms: int | None = None) -> autotrade.AutoLimits:
    """Limits with any Telegram override applied, the loss floor scaled."""
    return autotrade.AutoLimits(
        daily_loss_limit=scaled_loss_limit(store, settings, now_ms),
        max_trades_per_day=int(
            store.get_setting("auto_max_trades_per_day", settings.auto_max_trades_per_day)
        ),
        max_trades_per_hour=int(
            store.get_setting("auto_max_trades_per_hour", settings.auto_max_trades_per_hour)
        ),
        min_seconds_between=int(
            store.get_setting("auto_min_seconds_between", settings.auto_min_seconds_between)
        ),
        budget=budget_for(store, settings, auto=True),
    )


def shadow_summary_sender(settings) -> bool:
    """Is this the instance that sends the SHADOW SUMMARY? The first
    instrument on `telegram_alert_instruments` (BTC), so exactly one does."""
    raw = getattr(settings, "telegram_alert_instruments", "") or ""
    first = next((x.strip().upper() for x in raw.split(",") if x.strip()), "")
    return bool(first) and surface.asset(settings.kalshi_series) == first


# ---------------------------------------------- the all-signal strategy
ALLSIGNAL_TASKS: set = set()


def allsignal_on(store: Store, settings: Settings) -> bool:
    """Listed instrument, the kill switch on, and its own switch on."""
    raw = getattr(settings, "allsignal_instruments", "") or ""
    listed = {x.strip().upper() for x in raw.split(",") if x.strip()}
    if (surface.asset(settings.kalshi_series) or "") not in listed:
        return False
    if not auto_is_on(store, settings):
        return False  # /auto off stops everything, this included
    return bool(store.get_setting("allsignal_enabled", 1.0))


# ------------------------------------------------ the day's TAKEN $ trades
# A $ trade TAKEN today is the primary's fill - or, once the primary is DONE for
# the day at its cap, the signal it copied to the mirrors still trading
# ('copied'; operator, 2026-10-05: Affoue "becomes the account that keeps
# trading after target hit"; FINDINGS 163). The cushion, the trend skip and the
# after-a-loss stakes read this one sequence, so the rules run on as before once
# the primary stops. A copied row has no fill of the primary's and never a `won`
# of its own - no money report counts it; its result is the market's, from the
# alert's prediction on the same side.

def _taken_rows(store: Store, opened: int, newest_first: bool,
                limit: int | None = None) -> list:
    """Today's taken $ trades before window `opened`, with what decides each
    one's result."""
    sql = ("SELECT a.won, a.side, a.status, s.market_result, "
           "p.won AS p_won, p.side AS p_side FROM allsignal_trades a "
           "LEFT JOIN settlements s ON s.ticker = a.ticker "
           "LEFT JOIN predictions p ON p.window_open = a.window_open "
           "WHERE a.status IN ('filled', 'copied') "
           "AND a.window_open < ? AND a.window_open >= ? "
           "ORDER BY a.window_open " + ("DESC" if newest_first else "ASC"))
    args = [int(opened), store.day_start_ms(int(opened))]
    if limit is not None:
        sql += " LIMIT ?"
        args.append(int(limit))
    return store._dicts(sql, tuple(args))


def _taken_lost(row: dict) -> bool | None:
    """Did this taken trade lose? None while its result is not known."""
    if row["won"] is not None:
        return not row["won"]
    if row["market_result"] in ("yes", "no"):
        return (row["market_result"] == "yes") != (row["side"] == "UP")
    if row["status"] == "copied" and row["p_won"] is not None \
            and row["p_side"] in ("UP", "DOWN"):
        return not row["p_won"] if row["p_side"] == row["side"] else bool(row["p_won"])
    return None


def allsignal_after_loss_boost(store: Store, settings: Settings, opened: int) -> bool:
    """Is window `opened` inside the AFTER-A-LOSS BOOST (operator, 2026-10-05:
    "implement I $30 boost only below 8%"; FINDINGS 160, 163)? The latest KNOWN
    loss among today's taken $ trades is followed by fewer than
    `allsignal_after_loss_trades` taken trades - so the N after a loss go at the
    boost, and a loss inside them restarts it. An unknown result is not a loss
    (the smaller stake when unsure); skipped and unfilled signals are not
    trades. The primary and every mirror read the same sequence, each below its
    own target. Never raises (False)."""
    try:
        n = int(getattr(settings, "allsignal_after_loss_trades", 2) or 0)
        if n <= 0:
            return False
        since = None                      # taken trades since the latest known loss
        for row in _taken_rows(store, opened, newest_first=False):
            since = 0 if _taken_lost(row) is True else (None if since is None else since + 1)
        return since is not None and since < n
    except Exception as exc:  # noqa: BLE001
        print(f"allsignal: after-loss boost check failed {exc!r} - base stake", flush=True)
        return False


def mirror_stake_now(store: Store, settings: Settings, name: str,
                     opened: int) -> float | None:
    """Mirror `name`'s $ stake for its copy of the signal in window `opened`
    (operator, 2026-10-05: "Mirrors: boost all three by $1 ... Affoue ... $6 base
    and $8 after 1 loss and the after target hit $3"; FINDINGS 163):
      * at or above ITS OWN target, an account that keeps trading past it
        (`mirror_n_allsignal_after_target_stake`: Affoue) goes at that stake;
      * below it, inside the after-a-loss boost (the primary's taken sequence),
        at `mirror_n_allsignal_after_loss_stake` if set, else the day's stake +
        `mirror_allsignal_after_loss_add`;
      * otherwise the day's stake (scaled at midnight, as before).
    None - the day's stake, as before - when it cannot tell. Never raises."""
    try:
        guard = next((g for g in getattr(store, "daily_profit_guards", None) or []
                      if getattr(g, "account", "") == name), None)
        if guard is None:
            return None
        state = guard.state()
        day = float(guard.stake_today(state))
        if day <= 0:
            return None
        after = float(getattr(guard, "after_target_stake", 0.0) or 0.0)
        if after > 0 and state and state["pnl"] + 1e-8 >= state["target"]:
            return min(day, after)
        own = float(getattr(settings, f"mirror_{name[1:]}_allsignal_after_loss_stake",
                            0.0) or 0.0)
        add = float(getattr(settings, "mirror_allsignal_after_loss_add", 0.0) or 0.0)
        boost = own if own > 0 else (day + add if add > 0 else 0.0)
        if boost > day and allsignal_after_loss_boost(store, settings, opened):
            return boost
        return day
    except Exception as exc:  # noqa: BLE001
        print(f"mirror stake [{name}]: {exc!r} - the day's stake", flush=True)
        return None


def allsignal_stake_now(store: Store, settings: Settings, opened: int | None = None) -> float:
    """The primary's stake for a NEW $ entry: `allsignal_after_target_stake`
    while the day stands AT OR ABOVE its target, `allsignal_stake` whenever it
    is below - before the target, and again if losses bring it back below
    (operator, 2026-09-30: "make primary account base size 6 and apply $3 after
    the 8% target hit"; 17:5x: losses back below the target "invalidate the
    daily target hit and trade size back to the $6 ... until the target is hit
    and then back to the $3"). Resets with the day at 00:00 New York. Mirrors
    size from their own budgets and are paused by their own targets; this never
    touches them. Never raises: unreadable figures give the LOWER stake - and
    the order is then refused by the target check anyway, which fails closed.
    AFTER A LOSS (operator, 2026-10-05: "implement I $30 boost only below 8%";
    FINDINGS 160, 163): below the target, the next `allsignal_after_loss_trades`
    taken $ trades after a known loss go at `allsignal_after_loss_stake`."""
    base = float(settings.allsignal_stake)
    low = float(getattr(settings, "allsignal_after_target_stake", 0.0) or 0.0)
    boost = float(getattr(settings, "allsignal_after_loss_stake", 0.0) or 0.0)
    base_rate = float(getattr(settings, "allsignal_stake_rate", 0.0) or 0.0)
    boost_rate = float(getattr(settings, "allsignal_after_loss_stake_rate", 0.0) or 0.0)
    if low <= 0 and boost <= 0 and base_rate <= 0 and boost_rate <= 0:
        return base
    at_target = False
    for guard in getattr(store, "daily_profit_guards", None) or []:
        if getattr(guard, "account", "") != "primary":
            continue
        try:
            state = guard.state()
        except Exception:  # noqa: BLE001
            return min(base, low) if low > 0 else base
        if state and 0 < base_rate <= 1:
            base = float(state["opening"]) * base_rate
        if state and 0 < boost_rate <= 1:
            boost = float(state["opening"]) * boost_rate
        at_target = bool(state) and state["pnl"] + 1e-8 >= state["target"]
        break
    if at_target and low > 0:
        return min(base, low)
    if boost > 0 and not at_target and allsignal_after_loss_boost(
            store, settings, int(opened if opened is not None else time.time() * 1000)):
        return boost
    return base


def allsignal_count_now(store: Store, settings: Settings, limit: float,
                        opened: int | None = None, quote: float | None = None) -> int:
    """Primary contract count at the final order limit, including entry fees."""
    after_loss = allsignal_after_loss_boost(
        store, settings, int(opened if opened is not None else time.time() * 1000))
    for guard in getattr(store, "daily_profit_guards", None) or []:
        if getattr(guard, "account", "") != "primary":
            continue
        try:
            dynamic = guard.entry_count(float(limit), after_loss=after_loss)
            if dynamic is not None:
                return int(dynamic)
        except Exception as exc:  # noqa: BLE001
            print(f"allsignal: dynamic size unavailable {exc!r} - using one contract", flush=True)
            return 1
        break
    return contracts_for_budget(
        allsignal_stake_now(store, settings, opened), float(quote or limit))


def _row_stake(row: dict, settings: Settings) -> float:
    """The stake a $ book row was sized at (older rows: the configured one)."""
    try:
        return float(row.get("stake") or settings.allsignal_stake)
    except (TypeError, ValueError):
        return float(settings.allsignal_stake)


def spawn_allsignal(store: Store, settings: Settings, trader, ticker: str,
                    side: str, ask: float, opened: int, now_ms: int,
                    telegram=None) -> None:
    """On a signal, buy its side for `allsignal_stake` dollars - in the
    background, so the main strategy's poll never waits on it. Never raises."""
    try:
        if trader is None or not (0.0 < float(ask) < 1.0):
            return
        if not allsignal_on(store, settings):
            return
        limit = round(min(0.99, ask + settings.entry_slippage), 2)
        stake = allsignal_stake_now(store, settings, opened)
        count = allsignal_count_now(store, settings, limit, opened, quote=ask)
        if count <= 0:
            print(f"allsignal: {ticker} not placed; daily capital risk cap cannot fund "
                  f"one contract at {limit:.0%}", flush=True)
            return
        if not store.allsignal_claim(opened, ticker, side, ask, count, limit, now_ms,
                                     stake=stake):
            return
        task = asyncio.get_running_loop().create_task(_allsignal_order(
            store, settings, trader, ticker, side, ask, count, limit, opened,
            telegram))
        ALLSIGNAL_TASKS.add(task)
        task.add_done_callback(ALLSIGNAL_TASKS.discard)
    except Exception as exc:  # noqa: BLE001 - never on the main path
        print(f"allsignal: not placed {exc!r}", flush=True)


# ------------------------------------------ after a loss: wait for a cushion
# The window (only ever the current one) waiting for its cushion, and the note
# its entry message carries. Both in memory: a restart inside a waiting window
# loses that one window's trade, never adds one.
ALLSIGNAL_WAIT: dict = {}
ALLSIGNAL_WAIT_NOTE: dict = {}


def cushion_bps(snapshot, side: str) -> float | None:
    """How far the reference price sits from the target ON `side`, in bps;
    negative when it is on the other side. The live twin of
    observations.distance_bps, which the study measured."""
    try:
        price, target = float(snapshot.price), float(snapshot.target)
    except (TypeError, ValueError, AttributeError):
        return None
    if not target:
        return None
    signed = (price - target) / target * 10_000
    return signed if side == "UP" else -signed


def allsignal_after_loss(store: Store, opened: int) -> bool:
    """Did the last $ trade taken today, before this window, lose? A result
    not yet known counts as a loss: the wait can only delay or skip a trade,
    never add one. Copied signals count, once the primary is done for the day
    (FINDINGS 163)."""
    rows = _taken_rows(store, opened, newest_first=True, limit=1)
    if not rows:
        return False
    lost = _taken_lost(rows[0])
    return True if lost is None else lost


def _note_wait(opened: int, text: str) -> None:
    ALLSIGNAL_WAIT_NOTE[opened] = text
    for key in sorted(ALLSIGNAL_WAIT_NOTE)[:-8]:      # keep it small
        ALLSIGNAL_WAIT_NOTE.pop(key, None)


WAIT_KEY = "allsignal_wait"    # settings_text: a wait survives a restart


def _save_wait(store: Store, wait: dict | None) -> None:
    try:
        store.set_setting_text(WAIT_KEY, json.dumps(wait) if wait else "",
                               int(time.time() * 1000))
    except Exception as exc:  # noqa: BLE001 - memory still holds it
        print(f"allsignal: could not save the wait {exc!r}", flush=True)


def _start_wait(store: Store, opened: int, side: str, ask: float, ticker: str,
                now_ms: int, best) -> None:
    ALLSIGNAL_WAIT.clear()
    ALLSIGNAL_WAIT[opened] = {"opened": opened, "side": side, "since": now_ms, "ask": ask,
                              "ticker": ticker, "best": best}
    _save_wait(store, ALLSIGNAL_WAIT[opened])


def _end_wait(store: Store, opened: int) -> None:
    ALLSIGNAL_WAIT.pop(opened, None)
    _save_wait(store, None)


def _skip_wait(store: Store, wait: dict, note: str, now_ms: int) -> None:
    opened = int(wait["opened"])
    # ONLY A WINDOW WITHOUT A TRADE OF ITS OWN: a stale wait must never relabel
    # a real fill as skipped (safety review, 2026-09-30).
    if store.allsignal_claim(opened, wait["ticker"], wait["side"], wait["ask"], 0, 0.0,
                             now_ms):
        store.allsignal_finish(opened, "skipped", note=note)
        print(f"allsignal: skipped - {note}", flush=True)
    _end_wait(store, opened)


# --------------------- after TWO losses: never against the 15-minute trend
# Operator, 2026-10-02: "the 15 minutes after 2 losses is the one I want live,
# nothing else changes to the live rule" (FINDINGS 139-142). The signal bets the
# price stays on its side of the strike ~4 minutes in, from 5-minute momentum
# only, so in a slide an intra-window bounce reads as a strong UP (10-02
# 11:45-12:45, four such losses). After the day's last TWO taken $ trades both
# lost, a signal AGAINST the 15-minute BRTI trend by more than 10 bps is skipped.
# On the record (09-23..10-02): +14.23 vs live, it acted 4 times (2 losses
# avoided, 2 small wins missed); the mirrors hit their targets as before.

def allsignal_loss_streak(store: Store, opened: int, n: int) -> bool:
    """Did the last `n` $ trades TAKEN today, before this window, all lose -
    KNOWN losses only? Unlike the cushion, a result not yet known does NOT count:
    this skip is final at the alert, and a late Kalshi settlement had two
    WINNERS still unknown at 09-29 21:34 (review 2026-10-02). The cushion still
    waits on an unknown result, as before. Skipped and unfilled signals are not
    trades: they neither extend nor break a streak. Copied signals count, once
    the primary is done for the day (FINDINGS 163)."""
    if n <= 0:
        return False
    # An unknown result is not a known loss - trade as before.
    rows = _taken_rows(store, opened, newest_first=True, limit=int(n))
    return len(rows) >= n and all(_taken_lost(r) is True for r in rows)


def brti_trend_bps(settings: Settings, at_ms: int, minutes: int) -> float | None:
    """The reference's move over the last `minutes`, in bps, from the BRTI this
    service records (settlement_reference.db brti_features) - KALSHI ONLY, and
    exactly as the study measured it: a value counts at time t only if it was
    both stamped (ts_ms) and received by t and is at most 60 s old at t. None
    when either end is missing: then nothing is skipped."""
    path = Path(getattr(settings, "reference_database_path", "") or "")
    if not path.is_file():
        return None
    import sqlite3

    db = sqlite3.connect(f"file:{path.resolve().as_posix()}?mode=ro", uri=True, timeout=2)
    try:
        def value_at(t: int) -> float | None:
            row = db.execute(
                "SELECT brti_value, ts_ms FROM brti_features "
                "WHERE window_open_ms BETWEEN ? AND ? AND stale = 0 "
                "AND ts_ms <= ? AND received_ms <= ? ORDER BY ts_ms DESC LIMIT 1",
                (t - 1_800_000, t, t, t)).fetchone()
            if not row or not row[0] or t - int(row[1]) > 60_000:
                return None
            return float(row[0])

        now, then = value_at(int(at_ms)), value_at(int(at_ms) - int(minutes) * 60_000)
    finally:
        db.close()
    if now is None or then is None:
        return None
    return (now / then - 1) * 10_000


def allsignal_trend_skip(store: Store, settings: Settings, opened: int, side: str,
                         now_ms: int) -> str:
    """The note to skip this signal with, or "" to let it through to the
    cushion. BTC only (fitted on BTC's record); off at 0. Never raises: an error
    lets the signal through, which is the live rule as it was before this one."""
    try:
        n = int(getattr(settings, "allsignal_trend_skip_after_losses", 0) or 0)
        if n <= 0 or surface.asset(settings.kalshi_series) != "BTC":
            return ""
        if not allsignal_on(store, settings) or not allsignal_loss_streak(store, opened, n):
            return ""
        minutes = int(getattr(settings, "allsignal_trend_skip_minutes", 15) or 15)
        need = float(getattr(settings, "allsignal_trend_skip_bps", 10.0) or 10.0)
        trend = brti_trend_bps(settings, now_ms, minutes)
        if trend is None:
            return ""
        against = trend < -need if side == "UP" else trend > need
        if not against:
            return ""
        return (f"after {n} losses in a row: {side} is against the {minutes}-min trend "
                f"({trend:+.1f} bps)")
    except Exception as exc:  # noqa: BLE001
        print(f"allsignal: trend check failed {exc!r} - not applied", flush=True)
        return ""


LOCK_WAIT: dict = {}     # the window waiting for its ask to reach the lock floor
PRICE_WAIT_KEY = "allsignal_price_wait"
PRICE_CONFIRMED_KEY = "allsignal_price_confirmed"


def _price_wait_floor(settings: Settings) -> float:
    if surface.asset(settings.kalshi_series) != "BTC":
        return 0.0
    value = float(getattr(settings, "allsignal_skip_wait_min_ask", 0.0) or 0.0)
    return value if 0.0 < value < 1.0 else 0.0


def _save_price_wait(store: Store, wait: dict | None, now_ms: int) -> None:
    store.set_setting_text(PRICE_WAIT_KEY, json.dumps(wait) if wait else "", now_ms)


def _price_confirmation(store: Store, opened: int) -> dict:
    return json.loads(store.get_setting_text(PRICE_CONFIRMED_KEY) or "{}").get(str(opened), {})


def _remember_price_confirmation(store: Store, opened: int, floor: float,
                                 note: str, now_ms: int) -> None:
    saved = json.loads(store.get_setting_text(PRICE_CONFIRMED_KEY) or "{}")
    saved[str(opened)] = {"floor": floor, "note": note}
    for key in sorted(saved, key=int)[:-8]:
        saved.pop(key, None)
    store.set_setting_text(PRICE_CONFIRMED_KEY, json.dumps(saved), now_ms)


def _begin_price_wait(store: Store, settings: Settings, opened: int, ticker: str,
                      side: str, ask: float, floor: float, note: str, now_ms: int,
                      telegram=None) -> None:
    # A live order owns its window. Repeated alerts cannot replace or duplicate it.
    if store.db.execute("SELECT 1 FROM allsignal_trades WHERE window_open=?", (opened,)).fetchone():
        return
    if opened in LOCK_WAIT:
        return
    wait = dict(opened=opened, ticker=ticker, side=side, ask=float(ask),
                floor=floor, since=now_ms, note=note)
    _save_price_wait(store, wait, now_ms)
    LOCK_WAIT[opened] = wait
    if opened in ALLSIGNAL_WAIT:
        _end_wait(store, opened)
    _lock_shadow(settings, dict(event="price_wait", window_open=opened, side=side,
                                ask=ask, floor=floor, note=note, at_ms=now_ms))
    if telegram is not None:
        text = (f"\u23f3 <b>BTC PRICE CONFIRMATION</b>\n"
                f"Signal: {side} {ask * 100:.0f}\u00a2\n"
                f"Waiting for UP or DOWN at <b>{floor * 100:.0f}\u00a2 or higher</b>.\n"
                f"Reason: {escape(note)}\n"
                f"Entry cutoff: {settings.allsignal_cushion_min_left_s // 60} min before expiry. "
                "Shadow tracking continues.")
        task = asyncio.get_running_loop().create_task(
            Notifier(telegram, store, settings).send_once("allsignal_price_wait", str(opened), text, now_ms))
        ALLSIGNAL_TASKS.add(task)
        task.add_done_callback(ALLSIGNAL_TASKS.discard)


def _lock_shadow(settings: Settings, event: dict) -> None:
    """Append one lock event to lock_shadow.jsonl beside the reference db. Never raises."""
    try:
        path = Path(settings.reference_database_path).parent / "lock_shadow.jsonl"
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(event) + "\n")
    except Exception as exc:  # noqa: BLE001
        print(f"allsignal: lock shadow not recorded {exc!r}", flush=True)


def allsignal_ohlc_lock_note(settings: Settings, now_ms: int) -> str:
    """The note for a signal made while a chop range is locked, else "". Never
    raises: an error or missing data lets the signal through untouched."""
    try:
        if not getattr(settings, "allsignal_ohlc_lock_wait", False):
            return ""
        if surface.asset(settings.kalshi_series) != "BTC":
            return ""
        path = Path(getattr(settings, "reference_database_path", "") or "")
        if not path.is_file():
            return ""
        from .ohlc_regime import range_lock

        lock = range_lock(path, int(now_ms))
        if not lock["locked"]:
            return ""
        return f"chop range locked ({lock['low']:.0f}-{lock['high']:.0f})"
    except Exception as exc:  # noqa: BLE001
        print(f"allsignal: range-lock check failed {exc!r} - not applied", flush=True)
        return ""


def allsignal_lock_poll(store: Store, settings: Settings, trader, contract, snapshot,
                        opened: int, remaining: int, now_ms: int, telegram=None) -> None:
    """Restore a price wait and take the first unambiguous 85c side before cutoff."""
    try:
        if not LOCK_WAIT:
            saved = json.loads(store.get_setting_text(PRICE_WAIT_KEY) or "null")
            if saved:
                LOCK_WAIT[int(saved["opened"])] = saved
        for key in list(LOCK_WAIT):
            wait = LOCK_WAIT[key]
            expired = key != opened or remaining < settings.allsignal_cushion_min_left_s
            expired = expired or now_ms >= key + 900_000
            if not expired:
                continue
            floor = float(wait.get("floor") or settings.allsignal_ohlc_lock_min_ask)
            note = (f"price confirmation expired: neither side reached {floor * 100:.0f} cents "
                    "before the entry cutoff")
            if store.allsignal_claim(key, wait.get("ticker", contract.ticker), wait["side"],
                                     wait["ask"], 0, 0.0, now_ms):
                store.allsignal_finish(key, "price_expired" if _price_wait_floor(settings)
                                      else "skipped", note=note)
            _lock_shadow(settings, dict(event="passed", window_open=key, at_ms=now_ms, note=note))
            LOCK_WAIT.pop(key, None)
            _save_price_wait(store, LOCK_WAIT.get(opened), now_ms)
        wait = LOCK_WAIT.get(opened)
        if wait is None or wait.get("ticker", contract.ticker) != contract.ticker:
            return
        if store.db.execute("SELECT 1 FROM allsignal_trades WHERE window_open=?", (opened,)).fetchone():
            LOCK_WAIT.pop(opened, None)
            _save_price_wait(store, None, now_ms)
            return
        if trader is None or not allsignal_on(store, settings):
            return
        floor = float(wait.get("floor") or settings.allsignal_ohlc_lock_min_ask)
        # Two very expensive asks can mean a wide/incoherent book, not direction.
        eligible = [side for side in ("UP", "DOWN")
                    if floor <= float(contract.ask(side) or 0) < 1.0]
        if len(eligible) != 1:
            return
        side = eligible[0]
        ask = float(contract.ask(side))
        waited = (now_ms - wait["since"]) / 1000
        flipped = side != wait["side"]
        note = (f"{wait['note']}: alert {wait['side']} {wait['ask'] * 100:.0f} cents, "
                f"waited {waited:.0f}s; " + (f"FLIPPED to {side}" if flipped else f"confirmed {side}")
                + f" at {ask * 100:.0f} cents")
        _remember_price_confirmation(store, opened, floor, note, now_ms)
        _note_wait(opened, note)
        _lock_shadow(settings, dict(event="entered", window_open=opened, side=side,
                                    flipped=flipped, ask=ask, alert_ask=wait["ask"],
                                    waited_s=waited, at_ms=now_ms))
        allsignal_on_alert(store, settings, trader, contract, side, ask, snapshot, opened,
                           now_ms, telegram, released=True)
        # A claim (including a broker pause) owns the window. If a local write
        # failed, retain the wait to retry safely, with claim preventing duplicates.
        if store.db.execute("SELECT 1 FROM allsignal_trades WHERE window_open=?", (opened,)).fetchone():
            LOCK_WAIT.pop(opened, None)
            _save_price_wait(store, None, now_ms)
    except Exception as exc:  # noqa: BLE001
        print(f"allsignal: price confirmation poll failed {exc!r}", flush=True)


def allsignal_on_alert(store: Store, settings: Settings, trader, contract, side: str,
                       ask: float, snapshot, opened: int, now_ms: int,
                       telegram=None, released: bool = False) -> None:
    """Apply BTC's universal price confirmation, then enter the confirmed side.

    With a configured price floor every signal waits for exactly one side to
    quote at or above the floor. The opposite side may confirm first. Without a
    floor, retain the historical skip/cushion path.
    """
    # AFTER TWO LOSSES, NEVER AGAINST THE 15-MIN TREND (FINDINGS 142): skipped
    # here, said by the miss notice, and the streak stands until a trade is taken.
    skip = "" if released else allsignal_trend_skip(store, settings, opened, side, now_ms)
    price_floor = _price_wait_floor(settings)
    if price_floor and allsignal_on(store, settings):
        if released:
            # Contract-price confirmation replaces every old entry-time gate.
            spawn_allsignal(store, settings, trader, contract.ticker, side, ask, opened,
                            now_ms, telegram)
            return
        loss_wait = allsignal_after_loss(store, opened)
        lock = allsignal_ohlc_lock_note(settings, now_ms)
        reasons = [x for x in (skip,
                   "after a loss or while the previous result is pending" if loss_wait else "",
                   lock) if x]
        reason = "; ".join(reasons) if reasons else "universal BTC price confirmation"
        if reasons:
            _lock_shadow(settings, dict(event="strategy_skip_shadow", window_open=opened,
                                        side=side, ask=ask, note=reason, at_ms=now_ms))
        _begin_price_wait(store, settings, opened, contract.ticker, side, ask,
                          price_floor, reason, now_ms, telegram)
        allsignal_lock_poll(store, settings, trader, contract, snapshot, opened,
                            (opened + 900_000 - now_ms) // 1000, now_ms, telegram)
        return
    if skip:
        # Never raises (review 2026-10-02): a locked database loses the record
        # of the skip, never the alert's own prediction row.
        try:
            if store.allsignal_claim(opened, contract.ticker, side, ask, 0, 0.0, now_ms):
                store.allsignal_finish(opened, "skipped", note=skip)
        except Exception as exc:  # noqa: BLE001
            print(f"allsignal: skip not recorded {exc!r}", flush=True)
        print(f"allsignal: skipped - {skip}", flush=True)
        return
    # CHOP RANGE LOCKED: logged, and held for the floor price - never skipped.
    lock = "" if released else allsignal_ohlc_lock_note(settings, now_ms)
    if lock:
        floor = float(getattr(settings, "allsignal_ohlc_lock_min_ask", 0.0) or 0.0)
        if price_floor and floor:
            _lock_shadow(settings, dict(event="strategy_skip_shadow", window_open=opened,
                                        side=side, ask=ask, note=lock, at_ms=now_ms))
            _begin_price_wait(store, settings, opened, contract.ticker, side, ask,
                              price_floor, lock, now_ms, telegram)
            allsignal_lock_poll(store, settings, trader, contract, snapshot, opened,
                                (opened + 900_000 - now_ms) // 1000, now_ms, telegram)
            return
        hold = 0.0 < floor < 1.0 and float(ask) < floor and allsignal_on(store, settings)
        _lock_shadow(settings, dict(event="locked", window_open=opened, side=side, ask=ask,
                                    note=lock, floor=floor, held=hold, at_ms=now_ms))
        if hold:
            LOCK_WAIT.clear()
            LOCK_WAIT[opened] = {"opened": opened, "ticker": contract.ticker, "floor": floor,
                                 "side": side, "ask": float(ask), "since": now_ms, "note": lock}
            _save_price_wait(store, LOCK_WAIT[opened], now_ms)
            print(f"allsignal: {lock} - waiting for {side} ask {floor:g} (now {ask})",
                  flush=True)
            return
    need = float(getattr(settings, "allsignal_after_loss_cushion_bps", 0.0) or 0.0)
    if surface.asset(settings.kalshi_series) != "BTC":
        need = 0.0      # measured on BTC's signals only (FINDINGS 112)
    if need <= 0 or not allsignal_on(store, settings):
        spawn_allsignal(store, settings, trader, contract.ticker, side, ask, opened,
                        now_ms, telegram)
        return
    try:
        if not allsignal_after_loss(store, opened):
            spawn_allsignal(store, settings, trader, contract.ticker, side, ask, opened,
                            now_ms, telegram)
            return
        c = cushion_bps(snapshot, side)
    except Exception as exc:  # noqa: BLE001
        print(f"allsignal: cushion check failed {exc!r} - waiting", flush=True)
        _start_wait(store, opened, side, ask, contract.ticker, now_ms, None)
        return
    if c is not None and c >= need:
        _note_wait(opened, f"after a loss: {c:.1f} bps clear of the line at the alert")
        spawn_allsignal(store, settings, trader, contract.ticker, side, ask, opened,
                        now_ms, telegram)
        return
    _start_wait(store, opened, side, ask, contract.ticker, now_ms, c)
    print(f"allsignal: after a loss - waiting for {need:g} bps clear of the line "
          f"({side}, now {c if c is not None else float('nan'):.1f})", flush=True)


def allsignal_cushion_poll(store: Store, settings: Settings, trader, contract, snapshot,
                           opened: int, remaining: int, now_ms: int,
                           telegram=None) -> None:
    """EVERY POLL: a waiting window enters on the first poll its cushion is
    there - or at once if the last result, late at the alert, came in as a win
    - at that moment's ask; with under `allsignal_cushion_min_left_s` left it
    is skipped, and said. A wait survives a restart; one whose window ended
    without a verdict is recorded as skipped. Never raises."""
    try:
        if opened not in ALLSIGNAL_WAIT:
            raw = store.get_setting_text(WAIT_KEY) or ""
            saved = json.loads(raw) if raw else None
            if saved:
                o = int(saved["opened"])
                if store.db.execute("SELECT 1 FROM allsignal_trades WHERE window_open = ?",
                                    (o,)).fetchone():
                    _save_wait(store, None)      # that window already has its row
                else:
                    ALLSIGNAL_WAIT[o] = saved
        for key in [k for k in ALLSIGNAL_WAIT if k != opened]:
            if _price_wait_floor(settings):
                old = ALLSIGNAL_WAIT[key]
                _begin_price_wait(store, settings, key, old["ticker"], old["side"],
                                  old["ask"], _price_wait_floor(settings),
                                  "after-loss price confirmation", now_ms)
                allsignal_lock_poll(store, settings, trader, contract, snapshot, opened,
                                    remaining, now_ms, telegram)
                _end_wait(store, key)
                continue
            _skip_wait(store, ALLSIGNAL_WAIT[key],
                       "after a loss: the wait was interrupted before a verdict", now_ms)
        wait = ALLSIGNAL_WAIT.get(opened)
        if wait is None:
            return
        price_floor = _price_wait_floor(settings)
        if price_floor:
            # Upgrade an old persisted cushion wait at deployment/restart.
            _begin_price_wait(store, settings, opened, contract.ticker, wait["side"],
                              wait["ask"], price_floor, "after-loss price confirmation",
                              now_ms, telegram)
            allsignal_lock_poll(store, settings, trader, contract, snapshot, opened,
                                remaining, now_ms, telegram)
            return
        need = float(settings.allsignal_after_loss_cushion_bps)
        side = wait["side"]
        c = cushion_bps(snapshot, side)
        if c is not None:
            wait["best"] = c if wait.get("best") is None else max(wait["best"], c)
        if remaining < settings.allsignal_cushion_min_left_s:
            best = wait.get("best")
            _skip_wait(store, wait,
                       f"after a loss: never {need:g} bps clear of the line before "
                       f"{settings.allsignal_cushion_min_left_s // 60} min left"
                       + (f" (best {best:.1f} bps)" if best is not None else ""), now_ms)
            return
        waited = (now_ms - wait["since"]) / 1000
        alert = f"alert {float(wait['ask']) * 100:.0f}\u00a2"
        if not allsignal_after_loss(store, opened) and c is not None and c > 0:
            # The previous result was not known at the alert and has now come
            # in as a win - which the study knew all along (review 2026-09-29).
            # Only while the price is still on the alert's side; otherwise the
            # cushion rule goes on deciding (safety review, 2026-09-30).
            _end_wait(store, opened)
            _note_wait(opened, f"last result came in late as a win - entered "
                               f"({alert}, waited {waited:.0f}s)")
            spawn_allsignal(store, settings, trader, contract.ticker, side,
                            contract.ask(side), opened, now_ms, telegram)
            return
        if c is not None and c >= need:
            _end_wait(store, opened)
            _note_wait(opened, f"after a loss: {alert}, waited {waited:.0f}s for "
                               f"{c:.1f} bps clear of the line")
            print(f"allsignal: cushion {c:.1f} bps after {waited:.0f}s - entering {side}",
                  flush=True)
            spawn_allsignal(store, settings, trader, contract.ticker, side,
                            contract.ask(side), opened, now_ms, telegram)
    except Exception as exc:  # noqa: BLE001
        print(f"allsignal: cushion poll failed {exc!r}", flush=True)


# ----------------------------------- a $ order that missed: retry, if aligned
# Operator, 2026-09-30: "retry 60 after check is everything still aligned".
# 60 s after a miss (and after each further miss) the window is re-checked on
# every poll and the SAME order - same count, same price cap - is sent again the
# first moment everything still lines up: >= 2 min left, the price still on the
# signal's side of the line, its ask still at or under the cap (never chasing),
# and - after a loss - the 5 bps cushion. The daily target still decides at the
# order itself. Mirrors copy only a fill, as always.
RETRY_AFTER_MS = 60_000


def allsignal_retry_poll(store: Store, settings: Settings, trader, contract, snapshot,
                         opened: int, remaining: int, now_ms: int,
                         telegram=None) -> None:
    """Every poll: re-send a $ order that did not fill, once aligned. Never raises."""
    try:
        if trader is None or remaining < settings.allsignal_cushion_min_left_s:
            return
        rows = store._dicts("SELECT * FROM allsignal_trades WHERE window_open = ?", (opened,))
        if not rows:
            return
        row = rows[0]
        # A price miss only: failed / paused / skipped rows are not retried,
        # nor one whose NOT FILLED was already sent.
        if row["status"] != "unfilled" or not row.get("order_id") \
                or row["ticker"] != contract.ticker or row.get("reported_ms"):
            return
        last = int(row.get("attempt_ms") or row["created_ms"])
        retries = int(row.get("retries") or 0)
        side, cap = row["side"], float(row["limit_price"])
        ask = contract.ask(side)
        confirmed_floor = float(_price_confirmation(store, opened).get("floor") or 0)
        if confirmed_floor and not confirmed_floor <= float(ask or 0) < 1:
            store.db.execute("UPDATE allsignal_trades SET note=? WHERE window_open=? AND status='unfilled'",
                             (f"price confirmation: the quote no longer qualified at "
                              f"{confirmed_floor * 100:.0f} cents or higher; waiting to retry", opened))
            store.db.commit()
            return  # The retry must not buy below the price that confirmed direction.
        # THE CHASE (operator, 2026-10-05: "it's better to take it than just
        # letting it go"; FINDINGS 153). A miss means the price ran the signal's
        # way past the cap: from `allsignal_chase_after_s` after it, buy at the
        # MOVED price, up to `allsignal_chase_max`, at most
        # `allsignal_chase_attempts` times - instead of waiting 60 s for the old
        # cap. 30 live misses 09-28..10-05: 25 won; at the moved price, +11.13.
        chase_max = float(getattr(settings, "allsignal_chase_max", 0.0) or 0.0)
        chasing = (chase_max > 0 and cap < ask <= chase_max
                   and retries < int(getattr(settings, "allsignal_chase_attempts", 3) or 0))
        wait_ms = (int(getattr(settings, "allsignal_chase_after_s", 10) or 0) * 1000
                   if chasing else RETRY_AFTER_MS)
        if now_ms - last < wait_ms:
            return
        c = cushion_bps(snapshot, side)
        if (not confirmed_floor and (c is None or c <= 0)) or not 0 < ask <= (chase_max if chasing else cap):
            return                           # not aligned yet - look again next poll
        need = float(getattr(settings, "allsignal_after_loss_cushion_bps", 0.0) or 0.0)
        if not confirmed_floor and need > 0 and surface.asset(settings.kalshi_series) == "BTC" \
                and allsignal_after_loss(store, opened) and c < need:
            return
        if not allsignal_on(store, settings):
            return
        retries += 1
        waited = (now_ms - int(row["created_ms"])) / 1000
        if chasing:
            cap = round(min(chase_max, ask + settings.entry_slippage), 2)
        store.allsignal_finish(opened, "claimed", note=f"retry {retries}")
        store.allsignal_mark_attempt(opened, now_ms, retries)
        _note_wait(opened, (f"chased: bought at the moved price {waited:.0f}s after the signal "
                            if chasing else f"filled on retry {retries}, {waited:.0f}s after "
                            "the signal ")
                           + f"(ask {ask * 100:.0f}\u00a2, cap {cap * 100:.0f}\u00a2)")
        print(f"allsignal: retry {retries} {side} ask {ask:.2f} <= cap {cap:.2f}, "
              f"{c if c is not None else float('nan'):.1f} bps, {remaining}s left", flush=True)
        # SIZED FOR NOW: a window claimed at $6 whose retry comes after the
        # primary's target goes at the lower stake (review, 2026-09-30).
        stake = allsignal_stake_now(store, settings, opened)
        # SIZED AND PRICED AT NOW (review 2026-10-05): every account sizes its copy
        # by the order's entry price - sent at the signal's old ask, a chased order
        # over-spent the mirrors' stakes by up to ~50%. The record and messages
        # keep the signal's own price (the row's ask).
        dynamic_count = allsignal_count_now(store, settings, cap, opened, quote=ask)
        count = min(int(row["count"]), dynamic_count)
        if chasing:                          # the same dollars at the moved price
            count = dynamic_count
        if count <= 0:
            store.allsignal_finish(opened, "paused",
                                   note="daily capital risk cap cannot fund one contract")
            return
        if count != int(row["count"]) or chasing:
            store.db.execute("UPDATE allsignal_trades SET stake=?, count=?, limit_price=? "
                             "WHERE window_open=?", (stake, count, cap, opened))
            store.db.commit()
        task = asyncio.get_running_loop().create_task(_allsignal_order(
            store, settings, trader, row["ticker"], side, ask,
            count, cap, opened, telegram))
        ALLSIGNAL_TASKS.add(task)
        task.add_done_callback(ALLSIGNAL_TASKS.discard)
    except Exception as exc:  # noqa: BLE001 - never on the main path
        print(f"allsignal: retry check failed {exc!r}", flush=True)


PREWARMED: dict = {}      # ticker -> True, in the order added (never by name:
                          # "...26OCT01..." sorts before "...26SEP30...")


def prewarm_order_path(trader, settings: Settings, ticker: str, store: Store) -> None:
    """At the start of each window, in the BACKGROUND: look the market's shard
    up and fund it for one order, on the primary AND each mirror - so no
    order waits on either (2026-09-30). Once per window per ticker, even if it
    fails (the order path then does it itself). Never raises."""
    if trader is None or ticker in PREWARMED:
        return
    try:
        if not allsignal_on(store, settings):
            return                  # (3) only the instance trading $, while it is on
    except Exception:  # noqa: BLE001
        return
    PREWARMED[ticker] = True
    for old in list(PREWARMED)[:-4]:
        PREWARMED.pop(old, None)
    # FUNDED FOR THE LARGEST ORDER THE WINDOW CAN SEND - the after-a-loss stake
    # too (review 2026-10-05: a $30 / +$1 / $8 order topped its shard up inline).
    boost = float(getattr(settings, "allsignal_after_loss_stake", 0.0) or 0.0)
    add = float(getattr(settings, "mirror_allsignal_after_loss_add", 0.0) or 0.0)
    clients = [(getattr(trader, "_primary", trader), max(float(settings.allsignal_stake), boost))]
    for m in getattr(trader, "_mirrors", []) or []:
        day = float(m.target.allsignal_budget)
        own = float(getattr(settings, f"mirror_{m.target.name[1:]}_allsignal_after_loss_stake",
                            0.0) or 0.0)
        clients.append((m.client, max(day, own, day + add)))

    async def warm(client, stake):
        try:
            await asyncio.wait_for(client.market_shard(ticker), 3.0)
            if getattr(client, "auto_fund", False):
                count = contracts_for_budget(stake, 0.70)
                await asyncio.wait_for(
                    client.ensure_funds(ticker, count, 0.75, reserve=False), 12.0)
        except Exception as exc:  # noqa: BLE001
            print(f"prewarm [{ticker}]: {exc!r}", flush=True)

    try:
        loop = asyncio.get_running_loop()
        for client, stake in clients:
            task = loop.create_task(warm(client, stake))
            ALLSIGNAL_TASKS.add(task)
            task.add_done_callback(ALLSIGNAL_TASKS.discard)
    except Exception as exc:  # noqa: BLE001
        print(f"prewarm failed {exc!r}", flush=True)


AWAITED_SENDS: set = set()
TELEGRAM_TASK: dict = {}     # the background Telegram read of an urgent poll


async def record_shadows(hourly, reference, market, now_ms: int) -> None:
    """The shadow recorders on a poll with NO usable 15-minute quote.

    A PAUSE STOPS ORDERS, NEVER RECORDING (operator, 2026-09-30: "every system
    collecting data in the shadow still continue collecting their data"). The
    no-quote branch `continue`d past the hourly ladder: since 09-24, 204 of the
    211 hourly.db gaps over 90 s fell on no-quote polls in the last 1-3 minutes
    of a window, and 35 of 143 chains lost their final snapshot. The reference
    poll already ran on that path; its reconciliation did not. Every call here
    swallows its own errors and throttles itself.
    """
    if hourly:
        await hourly.poll(
            now_ms, market,
            brti=(reference.current_features() if reference is not None else None),
        )
        await hourly.settle(now_ms)
    if reference:
        await reference.reconcile(now_ms)


def closed_poll_seconds(settings: Settings, closed_until: int, now_ms: int) -> float:
    """How long to sleep while the venue is shut: the usual ten minutes, but
    never past the reopen. A flat 600 s woke up to ten minutes after the Sunday
    open, and a first poll more than ~9 minutes late misses the whole entry
    range of the first window - no alert, no prediction, no outcome (2026-09-30
    audit). The first poll after it ends the closure (end_closure_at_reopen)
    and polls normally until Kalshi lists the market, ~35-55 s later."""
    return max(1.0, min(float(settings.venue_closed_poll_seconds),
                        (closed_until - now_ms) / 1000 + 5))


def end_closure_at_reopen(now_ms: int, min_closed_ms: int = 1_800_000) -> bool:
    """A closure whose announced reopen has passed is OVER: time the gap from
    the reopen, not from Friday. Otherwise the first poll after the open -
    before Kalshi has listed the market, which takes ~35-55 s - saw a 48-hour
    gap: it either slept another 600 s (missing the first window's entry
    range) or raised a false "NO MARKET AT THE EXCHANGE" (review 2026-09-30).
    Only a REAL closure (at least `min_closed_ms`, the venue_closed_gap_s that
    defines one): a weekday outage can look like a string of 15-minute
    closures, and resetting each would understate how long it lasted.
    True when it ended one."""
    reopen = VENUE_SCHEDULE.get("next_open_ms")
    if not (reopen and now_ms >= reopen and MARKET_GAP.get("closed_told")):
        return False
    if reopen - MARKET_GAP.get("since", reopen) < min_closed_ms:
        return False
    MARKET_GAP["since"] = reopen
    MARKET_GAP.pop("closed_told", None)
    VENUE_SCHEDULE.clear()
    return True


CYCLE_ERRORS: dict = {}      # error signature -> what has been logged and said
CYCLE_REMIND_MS = 3_600_000  # a recurring error is said again every hour
CYCLE_RETRY_MS = 300_000     # a failed alert is sent again after 5 minutes
CYCLE_EFFECT = {
    "trading": "\U0001f6ab Orders did NOT run on those polls. Recording continues.",
    "settlement": "\U0001f6ab Settled markets were not processed on those polls. "
                  "Trading and recording continue.",
    "poll": "\U0001f6ab The rest of each of those polls was skipped (exits and "
            "cash-outs included). The service keeps running.",
}


def _error_site(exc: BaseException) -> str:
    """file:line of the deepest frame in OUR code - where the bug is."""
    site = ""
    for frame in traceback.extract_tb(exc.__traceback__):
        if "btc15_signal" in frame.filename:
            site = f"{Path(frame.filename).name}:{frame.lineno}"
    return site or "?"


async def report_cycle_error(exc: BaseException, where: str, settings: Settings,
                             store: Store, telegram, now_ms: int) -> None:
    """An UNEXPECTED error in a poll - a bug, not a network failure.

    It used to end the process: the loop caught only network-shaped errors, so
    a TypeError, NameError or sqlite error left service(), and after seven exits
    in an hour the watchdog gave up. Every recorder on that instance then stayed
    down until someone restarted it (2026-09-30 audit; ETH lost 20 minutes of
    observations this way on 09-25). Now the poll carries on, so recording
    continues, and the fault is said out loud:
      * the traceback in the log once per error per 15-minute window;
      * Telegram at once, from every instance, saying what did not run; while
        it keeps happening, again every hour from an instance that alerts
        (TELEGRAM_ALERT_INSTRUMENTS) and once a New York day from the shadows,
        with the count. Quiet for an hour, it counts as new when it returns;
      * sent DIRECTLY, not through the store: the error may BE the store
        (review 2026-09-30), and a send that fails is retried after 5 minutes.
    Never raises.
    """
    try:
        sig = f"{where}:{type(exc).__name__}:{_error_site(exc)}"
        seen = CYCLE_ERRORS.get(sig)
        if seen is None or now_ms - seen["last_ms"] > CYCLE_REMIND_MS:
            seen = CYCLE_ERRORS[sig] = {
                "window": None, "first_ms": now_ms, "last_ms": now_ms, "count": 0,
                "said_ms": None, "tried_ms": None}
        seen["count"] += 1
        seen["last_ms"] = now_ms
        window = now_ms // 900_000
        if seen["window"] != window:
            seen["window"] = window
            print(f"cycle error (unexpected) [{sig}] x{seen['count']}: {exc!r}\n"
                  + "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)),
                  flush=True)
        from .shadow_summary import alert_list

        listed = alert_list(settings)
        loud = not listed or (surface.asset(settings.kalshi_series) or "") in listed
        due = seen["said_ms"] is None or (
            now_ms - seen["said_ms"] >= CYCLE_REMIND_MS
            and (loud or ny_day(now_ms) != ny_day(seen["said_ms"])))
        retry_ok = seen["tried_ms"] is None or now_ms - seen["tried_ms"] >= CYCLE_RETRY_MS \
            or seen["said_ms"] == seen["tried_ms"]
        if telegram is None or not due or not retry_ok:
            return
        seen["tried_ms"] = now_ms
        asset = surface.asset(settings.kalshi_series) or settings.kalshi_series
        again = seen["said_ms"] is not None
        first = datetime.fromtimestamp(seen["first_ms"] / 1000, ZoneInfo("America/New_York"))
        since = first.strftime("%H:%M" if ny_day(seen["first_ms"]) == ny_day(now_ms)
                               else "%m-%d %H:%M")
        text = (f"\u26a0\ufe0f <b>{escape(asset)} \u00b7 SOFTWARE ERROR"
                f"{' - STILL HAPPENING' if again else ''}</b>\n"
                f"<code>{escape(sig)}</code>\n"
                f"{escape(_redacted(telegram, str(exc))[:200])}\n"
                + (f"\U0001f501 {seen['count']:,} times since {since} ET\n"
                   if seen["count"] > 1 else "")
                + CYCLE_EFFECT.get(where, "The service keeps running and recording.")
                + "\n<i>This needs a fix.</i>")
        await telegram.send(text)
        seen["said_ms"] = now_ms
        seen["tried_ms"] = now_ms
    except Exception as inner:  # noqa: BLE001 - the reporter must never raise
        print(f"cycle error report failed: {inner!r}", flush=True)


def _report_background_failure(task) -> None:
    """Say it when a background Telegram read fails - it is never awaited."""
    try:
        if not task.cancelled() and task.exception() is not None:
            print(f"telegram (background) failed: {task.exception()!r}", flush=True)
    except Exception:  # noqa: BLE001
        pass


def allsignal_urgent(store: Store, settings: Settings, now_ms: int) -> bool:
    """Is a $ entry decision imminent this poll? Then the poll reads the price
    and decides FIRST, and its chores (Telegram, the Kalshi sync) wait: before
    the price is read they took ~0.7 s every poll and ~1.25 s more once a
    minute (2026-09-30). Imminent: the alert is still pending in the entry range
    (6-11 min left, plus a poll either side), a cushion wait is running, or a
    missed order's retry is due. Never raises; False when unsure."""
    try:
        if not allsignal_on(store, settings):
            return False
        opened = now_ms // 900_000 * 900_000
        remaining = (opened + 900_000 - now_ms) / 1000
        if opened in ALLSIGNAL_WAIT or opened in LOCK_WAIT:
            return True
        row = store.db.execute(
            "SELECT status, COALESCE(attempt_ms, created_ms) FROM allsignal_trades "
            "WHERE window_open = ?", (opened,)).fetchone()
        if row is not None:
            # THE CHASE is due 10 s after a miss (2026-10-05): urgent from the
            # miss on, so the order is not queued behind the poll's chores.
            due = RETRY_AFTER_MS
            if float(getattr(settings, "allsignal_chase_max", 0.0) or 0.0) > 0:
                due = min(due, int(getattr(settings, "allsignal_chase_after_s", 10) or 0) * 1000)
            return (row[0] == "unfilled" and remaining >= settings.allsignal_cushion_min_left_s
                    and now_ms - int(row[1]) >= due - 15_000)
        lo = settings.entry_to_seconds - settings.poll_seconds
        hi = settings.entry_from_seconds + settings.poll_seconds + 5
        if not lo <= remaining <= hi:
            return False
        return store.db.execute(
            "SELECT 1 FROM strategy_alerts WHERE strategy = 'primary' AND window_open = ?",
            (opened,)).fetchone() is None
    except Exception:  # noqa: BLE001
        return False


async def await_order_sent(store: Store, opened: int, timeout: float = 2.0) -> None:
    """Hold this poll until a $ order spawned THIS poll has been answered by
    Kalshi (about one round trip; at most `timeout`), so the order goes out
    before the rest of the poll's work - one loop turn was not enough, httpx
    needs several (review 2026-09-30). Once per attempt. Never raises."""
    try:
        row = store.db.execute(
            "SELECT status, COALESCE(attempt_ms, created_ms) FROM allsignal_trades "
            "WHERE window_open = ?", (opened,)).fetchone()
        if not row or row[0] != "claimed" or (opened, row[1]) in AWAITED_SENDS:
            return
        AWAITED_SENDS.add((opened, row[1]))
        for old in sorted(AWAITED_SENDS)[:-8]:
            AWAITED_SENDS.discard(old)
        event = fresh_order_event(f"allsignal:{opened}")
        await asyncio.wait_for(event.wait(), timeout)
    except Exception:  # noqa: BLE001 - a timeout just lets the poll go on
        pass


async def _allsignal_order(store: Store, settings: Settings, trader, ticker: str,
                           side: str, ask: float, count: int, limit: float,
                           opened: int, telegram=None) -> None:
    from .store import TradeProposal

    # BOTH ACCOUNTS since 2026-09-28 (operator): through the mirroring
    # client, which copies a FILLED order to the wife's account, sized $1 there
    # too (`MirrorTarget.allsignal_budget`, keyed on strategy "allsignal").
    client = trader if settings.allsignal_mirror else getattr(trader, "_primary", trader)
    order = TradeProposal(
        id=f"allsignal:{opened}", strategy="allsignal", window_open=opened,
        ticker=ticker, side=side, entry_limit=ask, take_profit=0.0,
        count=count, expires_at=opened + 900_000, close_ms=opened + 900_000,
        status="claimed")
    asset = surface.asset(settings.kalshi_series) or "?"
    try:
        result = await client.execute_with_take_profit(
            order, settings.entry_slippage, ceiling=limit)
    except Exception as exc:  # noqa: BLE001
        store.allsignal_finish(opened, "failed", note=f"{type(exc).__name__}: {exc}"[:300])
        print(f"allsignal [{asset}] {ticker} {side} x{count}: failed {exc!r}", flush=True)
        return
    if result.status == "copied":
        # THE PRIMARY IS DONE FOR THE DAY; the signal went to the mirrors still
        # trading (operator, 2026-10-05; FINDINGS 163). Not the primary's trade -
        # no fill, no money here - but a taken one for the rules that read the
        # day's sequence (`_taken_rows`).
        store.allsignal_finish(opened, "copied", note=result.note)
        print(f"allsignal [{asset}] {ticker} {side}: {result.note}", flush=True)
        await announce_allsignal_copy(store, settings, telegram, opened, "allsignal_copied")
        return
    if result.status == "paused":
        store.allsignal_finish(opened, "paused", note=result.note)
        print(f"allsignal [{asset}] paused: {result.note}", flush=True)
        return
    if result.filled_count <= 0:
        store.allsignal_finish(opened, "unfilled", order_id=result.entry_order_id,
                               note=result.note)
        print(f"allsignal [{asset}] {ticker} {side} x{count} limit {limit:.2f}: "
              f"unfilled", flush=True)
        return
    paid, filled, fee = limit, float(result.filled_count), None
    # Kalshi's fills feed lags the order ack, as the main path found; a price
    # still missing after the retries is refined from the synced fills table
    # by `Store.allsignal_reconcile`.
    for _ in range(4):
        try:
            await asyncio.sleep(1.5)
            # The fill read is OURS: the primary account's client, whatever
            # routed the order.
            detail = await getattr(trader, "_primary", trader).fill_detail(
                result.entry_order_id, side)
        except Exception as exc:  # noqa: BLE001
            print(f"allsignal: fill detail failed {exc!r}", flush=True)
            continue
        if detail:
            paid, filled, fee = detail
            break
    store.allsignal_finish(opened, "filled", order_id=result.entry_order_id,
                           filled=filled, fill_price=paid, fee=fee, note=result.note)
    print(f"allsignal [{asset}] {ticker} {side} x{filled:g} at {paid:.4f} "
          f"(signal {ask:.2f})", flush=True)
    await announce_allsignal(store, settings, telegram, opened)


def _copy_accounts(row: dict) -> str:
    """'Affoue 4/4 ($3); Wife paused' - who took a copied window, from its record."""
    note = row.get("note") or ""
    return note.split(COPY_NOTE, 1)[1] if COPY_NOTE in note else note


async def announce_allsignal_copy(store: Store, settings: Settings, telegram, opened: int,
                                  kind: str, *, won=None, missed: bool = False) -> bool:
    """A window the MIRRORS took once the primary was done for the day (2026-10-05,
    FINDINGS 163; review: these real-money trades said nothing): the entry
    (`allsignal_copied`), the market's result (`allsignal_copy_result`) or the
    final miss (`allsignal_copy_missed`). Never raises; True if it went out."""
    if telegram is None:
        return False
    try:
        row = store._dicts("SELECT * FROM allsignal_trades WHERE window_open = ?",
                           (int(opened),))[0]
        target, ref, final = _window_prices(store, int(opened), row.get("ticker", ""))
        text = messages.allsignal_copy_message(
            asset=surface.asset(settings.kalshi_series) or "?", window_open=int(opened),
            side=row["side"], signal_ask=float(row["ask"]), accounts=_copy_accounts(row),
            won=won, missed=missed, target=target, ref=ref,
            final=final if won is not None else None)
        return await Notifier(telegram, store, settings).send_once(
            kind, str(int(opened)), text, int(time.time() * 1000))
    except Exception as exc:  # noqa: BLE001
        print(f"allsignal: copy notice failed {exc!r}", flush=True)
        return False


def mirror_done_note(store: Store, trader) -> str:
    """Said under the primary's DAILY CAP REACHED: which mirrors still take the
    signals and at what stake - built when said, from each account's own day and
    switch (review 2026-10-05). Never raises ('' = no line)."""
    try:
        names = trader._still_trading()
        if not names:
            return "\u23f9 No mirror is taking signals now (at their targets or switched off)"
        labels = getattr(trader, "labels", None) or {}
        parts = []
        for name in names:
            g = next((g for g in getattr(store, "daily_profit_guards", None) or []
                      if g.account == name), None)
            state = g.state() if g is not None else None
            past = bool(g is not None and g.after_target_stake and state
                        and state["pnl"] + 1e-8 >= state["target"])
            parts.append(f"{labels.get(name, name)} "
                         + (f"at ${g.after_target_stake:g} past its target" if past
                            else "until its own target"))
        return "\u25b6\ufe0f Still taking the signals: " + ", ".join(parts)
    except Exception:  # noqa: BLE001
        return ""


def _with_target(store: Store, text: str, full: bool = False) -> str:
    """The 3% daily target under a result (one line) or a summary (the block).
    Unchanged when the target is not running. Never raises."""
    guards = getattr(store, "daily_profit_guards", None)
    if not guards:
        return text
    try:
        from .daily_profit import summary

        return text + "\n" + "━" * 14 + "\n" + summary(guards)
    except Exception:  # noqa: BLE001 - a status line never blocks a result
        return text


TARGET_REVIEW_AT = 2000   # recorded BTC signals (operator, 2026-09-29)
TARGET_REVIEW_FROM = int(datetime(2026, 9, 22, 23, 0, tzinfo=ZoneInfo("America/New_York"))
                         .timestamp() * 1000)   # the current alert logic


async def remind_target_review(store: Store, settings: Settings, telegram,
                               now_ms: int) -> None:
    """Once 2,000 BTC signals are recorded, say it is time to re-check the
    daily targets (operator: "we can reevaluate after 2000 signals collected").
    Once only; BTC instance only. Never raises."""
    if telegram is None or settings.kalshi_series != "KXBTC15M":
        return
    try:
        n = store.db.execute(
            "SELECT COUNT(*) FROM strategy_alerts WHERE strategy='primary' "
            "AND window_open >= ?", (TARGET_REVIEW_FROM,)).fetchone()[0]
        if n < TARGET_REVIEW_AT:
            return
        await Notifier(telegram, store, settings).send_once(
            "target_review", str(TARGET_REVIEW_AT),
            f"\U0001f4ca <b>{TARGET_REVIEW_AT:,} BTC SIGNALS RECORDED</b>\n"
            f"\U0001f3af Time to re-check the daily targets "
            f"(You {settings.daily_profit_target_rate:.0%} · mirrors "
            f"{settings.mirror_daily_profit_target_rate:.0%}) on the recorded outcomes:\n"
            "<code>python scripts/target_study.py</code>",
            now_ms, money=True)
    except Exception as exc:  # noqa: BLE001 - a reminder is never fatal
        print(f"target review reminder failed: {exc!r}", flush=True)


async def _refresh_accounts(store: Store) -> None:
    """Re-read every account from the broker BEFORE a result or summary is
    written, so the account lines include the very trade they follow
    (operator, 2026-09-29: "accurate updates"). The monitor alone refreshes
    every 15 s, which let a result show the day one trade behind."""
    guards = getattr(store, "daily_profit_guards", None) or []
    if len(guards) > 1:
        # The mirrors' own sale or settlement lands about a second after the
        # primary's; without this pause a cash-out result showed their day one
        # trade behind (review, 2026-09-29).
        await asyncio.sleep(2)
    for guard in guards:
        try:
            await guard.refresh(force=True)
        except Exception as exc:  # noqa: BLE001 - the line then says it is stale
            print(f"account refresh failed ({guard.label}): {exc!r}", flush=True)


def _miss_reason(row: dict) -> str:
    """Why a $ signal bought nothing, including what the retry did."""
    if str(row.get("note") or "").startswith("price confirmation:"):
        return str(row["note"]).replace("; waiting to retry", "; no qualifying retry before cutoff")
    base = _order_failure(row.get("note"))
    if row.get("status") != "unfilled":
        return base
    n = int(row.get("retries") or 0)
    if n:
        return f"the price stayed above the cap - retried {n} time{'s' if n != 1 else ''}"
    return "the price moved above the cap and never lined up again in time to retry"


def _order_failure(note) -> str:
    """The broker's refusal, in words the operator can act on."""
    n = (note or "").lower()
    if "insufficient_balance" in n or "insufficient balance" in n:
        return "not enough money on the BTC balance"
    if "no fill" in n or "book moved" in n:
        return "the price moved above the cap before the order landed"
    if "reconciliation unavailable" in n:
        return "the daily target check could not read Kalshi, so the entry was blocked for safety"
    if "authentication" in n or "401" in n:
        return "the account's API key was refused"
    return ""


def _allsignal_book(settings: Settings, store: Store, now_ms: int) -> list:
    from . import shadow_summary

    root = Path(settings.database_path).resolve().parent
    return shadow_summary.allsignal_book(root, settings, store.day_start_ms(now_ms))


def _allsignal_copies(settings: Settings) -> dict | None:
    """{account: [$1 instruments it copies now]} per mirror, for the summary."""
    from . import shadow_summary

    try:
        root = Path(settings.database_path).resolve().parent
        return {mirror_name(settings, t.name):
                shadow_summary.mirror_copies(root, settings, t.name)
                for t in targets_from_settings(settings)}
    except Exception:  # noqa: BLE001 - the footer falls back to its old words
        return None


def _window_prices(store: Store, window_open: int, ticker: str = "") -> tuple:
    """(Kalshi target, reference at the signal, official settlement value) for a
    $ window - for the messages only (operator, 2026-10-03: "show the Kalshi
    target price for clarity"). From what this service already recorded: the
    alert's observation (target = the window's strike, btc = the Kalshi BRTI
    reference) and the synced settlement. Unknown parts are None; never raises."""
    target = ref = final = None
    try:
        row = store.db.execute(
            "SELECT o.target, o.btc FROM strategy_alerts a JOIN observations o "
            "ON o.window_open = a.window_open AND o.observed_ms = a.created_at "
            "WHERE a.window_open = ? AND a.strategy = 'primary' LIMIT 1",
            (int(window_open),)).fetchone()
        if row:
            target, ref = row[0], row[1]
        if not target:
            row = store.db.execute("SELECT target FROM observations WHERE window_open = ? "
                                   "AND target > 0 LIMIT 1", (int(window_open),)).fetchone()
            target = row[0] if row else None
        if ticker:
            row = store.db.execute("SELECT strike, expiration_value FROM settlements "
                                   "WHERE ticker = ?", (ticker,)).fetchone()
            if row:
                target = target or row[0]
                final = row[1]
            if not final:
                # THE SETTLEMENT SYNC LANDS ~60-70 s AFTER THE RESULT MESSAGE
                # (review 2026-10-03: 24 of 25 results had no settled value).
                # Kalshi's next window opens at this one's settled value, and
                # this service records that strike within seconds of the open -
                # it matched the official value in every case checked.
                row = store.db.execute("SELECT target FROM observations WHERE window_open = ? "
                                       "AND target > 0 LIMIT 1",
                                       (int(window_open) + 900_000,)).fetchone()
                final = row[0] if row else None
    except Exception:  # noqa: BLE001 - the message goes out without the line
        pass
    return target, ref, final


def _allsignal_text(store: Store, settings: Settings, row: dict, now_ms: int,
                    with_book: bool = True) -> str:
    target, ref, _ = _window_prices(store, int(row["window_open"]))
    return messages.allsignal_trade_message(
        asset=surface.asset(settings.kalshi_series) or "?",
        window_open=int(row["window_open"]), side=row["side"],
        signal_ask=float(row["ask"]),
        fill_price=float(row["fill_price"] or row["limit_price"]),
        count=float(row["filled"] or row["count"]),
        mirrored=mirror_label(store, settings) if settings.allsignal_mirror else "",
        won=row.get("won"),
        pnl=(float(row["pnl"]) - float(row.get("fee") or 0)
             - float(row.get("exit_fee") or 0)) if row.get("pnl") is not None else None,
        book=_allsignal_book(settings, store, now_ms) if with_book else None,
        exit_price=row.get("exit_price"), exit_count=row.get("exit_count"),
        exited_ms=row.get("exited_ms"), budget=_row_stake(row, settings),
        cushion_note=(ALLSIGNAL_WAIT_NOTE.get(int(row["window_open"]))
                      or _price_confirmation(store, int(row["window_open"])).get("note", "")),
        target=target, ref=ref)


async def allsignal_balances(store: Store, trader, settings=None) -> list:
    """[(label, start, start_ms, now)] for the operator's account and each
    mirror, for the session summary. The start is the value each account had
    when the $1 strategy began running alone (scripts/allsignal_baseline.py);
    now is cash plus open positions at cost. Never raises."""
    guards = getattr(store, "daily_profit_guards", [])
    if guards:
        out = []
        for guard in guards:
            state = guard.state()
            if state:
                try:
                    value = await guard.client.account_value()
                except Exception:
                    value = None
                out.append((guard.label, state["opening"], state["captured_ms"], value))
        return out
    try:
        starts = json.loads(store.get_setting_text("allsignal_start_balances") or "{}")
    except (ValueError, TypeError):
        starts = {}
    if not starts or trader is None:
        return []
    clients = [("You", getattr(trader, "_primary", trader))]
    for mirror in getattr(trader, "_mirrors", []) or []:
        name = getattr(getattr(mirror, "target", None), "name", "")
        clients.append((mirror_name(settings, name), mirror.client))
    out = []
    for label, client in clients:
        start = starts.get(label)
        if not start:
            continue
        try:
            now = await client.account_value()
        except Exception:  # noqa: BLE001 - a missing figure, not a failure
            now = None
        out.append((label, float(start["value"]), int(start["at_ms"]), now))
    return out


async def announce_allsignal(store: Store, settings: Settings, telegram,
                             opened: int) -> None:
    """The trade's ENTRY message, sent at execution - what was bought, at
    what price, on which accounts. Never rewritten afterwards, and it carries
    no record: the result follows as a reply (`report_allsignal_results`),
    and the record belongs to the result. Never raises."""
    if telegram is None:
        return
    try:
        now_ms = int(time.time() * 1000)
        row = store._dicts("SELECT * FROM allsignal_trades WHERE window_open=?",
                           (opened,))[0]
        sent = await Notifier(telegram, store, settings).send_once(
            "allsignal_trade", str(opened),
            _allsignal_text(store, settings, row, now_ms, with_book=False), now_ms)
        if sent:
            got = store.delivered("allsignal_trade", str(opened)) or {}
            store.allsignal_set_message(opened, got.get("message_id"))
    except Exception as exc:  # noqa: BLE001 - reporting is never fatal
        print(f"allsignal: announce failed {exc!r}", flush=True)


async def report_allsignal_results(store: Store, settings: Settings, telegram,
                                   now_ms: int) -> None:
    """Each settled (or cashed-out) trade's RESULT, as a reply to its entry
    message, with the day's and overall record. A trade with no entry message
    gets one full message instead. Never raises.

    THE ENTRY IS NOT REWRITTEN. Until 2026-09-28 19:0x the entry was edited
    to the result AND the result was sent as a reply, so every result showed
    twice - once back at the entry's time, carrying a record frozen at the
    edit - and the thread read out of order (operator: "the messaging is
    messed up ... doesn't look right"). One entry, one result, in time order.
    """
    if telegram is None:
        return
    unreported = store.allsignal_unreported()
    if unreported:
        await _refresh_accounts(store)
    for row in unreported:
        try:
            if row.get("tg_message_id"):
                await Notifier(telegram, store, settings).send_once(
                    "allsignal_settled", str(row["window_open"]),
                    _with_target(store, messages.allsignal_settled_message(
                        asset=surface.asset(settings.kalshi_series) or "?",
                        window_open=int(row["window_open"]), side=row["side"],
                        fill_price=float(row["fill_price"] or row["limit_price"]),
                        won=bool(row["won"]), pnl=(float(row["pnl"] or 0.0)
                            - float(row.get("fee") or 0) - float(row.get("exit_fee") or 0)),
                        book=_allsignal_book(settings, store, now_ms),
                        count=float(row["filled"] or row["count"]),
                        exit_price=row.get("exit_price"),
                        exit_count=row.get("exit_count"),
                        exited_ms=row.get("exited_ms"), budget=_row_stake(row, settings),
                        target=_window_prices(store, int(row["window_open"]),
                                              row.get("ticker", ""))[0],
                        final=_window_prices(store, int(row["window_open"]),
                                             row.get("ticker", ""))[2])),
                    now_ms, reply_to=int(row["tg_message_id"]))
            else:
                await Notifier(telegram, store, settings).send_once(
                    "allsignal_result", str(row["window_open"]),
                    _allsignal_text(store, settings, row, now_ms), now_ms)
            store.allsignal_mark_reported(int(row["window_open"]), now_ms)
        except Exception as exc:  # noqa: BLE001
            print(f"allsignal: result report failed {exc!r}", flush=True)
    # EVERY SIGNAL IS ACCOUNTED FOR, not only the filled ones: a signal whose
    # order bought nothing said nothing, and the window simply looked skipped
    # (GOLD 18:45, 2026-09-28: "the book moved before the order landed").
    # Recent ones only, so a restart cannot replay the afternoon's misses.
    for row in store.allsignal_unannounced_misses(now_ms - 30 * 60_000):
        # A miss that can still be retried is not final: said once, when its
        # window has under the retry limit left (allsignal_retry_poll).
        if row["status"] == "unfilled" and now_ms < int(row["window_open"]) + 900_000 \
                - settings.allsignal_cushion_min_left_s * 1000:
            continue
        # A COPY THAT MISSED once the primary was done (FINDINGS 163): the mirrors'
        # miss, said as theirs - not the primary's $ stake.
        if row["status"] == "unfilled" and row.get("order_id") == COPY_MISSED_ID:
            await announce_allsignal_copy(store, settings, telegram, int(row["window_open"]),
                                          "allsignal_copy_missed", missed=True)
            store.allsignal_mark_reported(int(row["window_open"]), now_ms)
            continue
        # A signal refused by the 3% pause is not a miss: the pause was
        # announced once, and repeating it every 15 minutes is noise.
        if "profit target" in (row.get("note") or ""):
            store.allsignal_mark_reported(int(row["window_open"]), now_ms)
            continue
        try:
            await Notifier(telegram, store, settings).send_once(
                "allsignal_missed", str(row["window_open"]),
                messages.allsignal_missed_message(
                    asset=surface.asset(settings.kalshi_series) or "?",
                    window_open=int(row["window_open"]), side=row["side"],
                    signal_ask=float(row["ask"]), limit=float(row["limit_price"]),
                    failed=row["status"] == "failed",
                    budget=(allsignal_stake_now(store, settings)
                            if row["status"] == "skipped" and not row.get("stake")
                            else _row_stake(row, settings)),
                    reason=(row.get("note") if row["status"] in ("skipped", "price_expired")
                            else _miss_reason(row)),
                    skipped=row["status"] == "skipped",
                    confirmation_expired=row["status"] == "price_expired",
                    target=_window_prices(store, int(row["window_open"]))[0],
                    ref=_window_prices(store, int(row["window_open"]))[1]),
                now_ms)
            store.allsignal_mark_reported(int(row["window_open"]), now_ms)
        except Exception as exc:  # noqa: BLE001
            print(f"allsignal: miss report failed {exc!r}", flush=True)
    # THE MIRRORS' COPIED WINDOWS, settled (FINDINGS 163): the market's result on
    # the signal's side, from the alert's prediction. No dollar figure - a mirror's
    # money is its own broker balance, shown in the session summary.
    try:
        copied = store._dicts(
            "SELECT a.window_open, CASE WHEN p.side=a.side THEN p.won ELSE 1-p.won END AS p_won "
            "FROM allsignal_trades a JOIN predictions p ON p.window_open = a.window_open "
            "WHERE a.status = 'copied' AND a.reported_ms IS NULL AND p.won IS NOT NULL "
            "AND a.created_ms >= ? ORDER BY a.window_open", (now_ms - 3 * 3_600_000,))
    except Exception as exc:  # noqa: BLE001
        print(f"allsignal: copy results unreadable {exc!r}", flush=True)
        copied = []
    for row in copied:
        await announce_allsignal_copy(store, settings, telegram, int(row["window_open"]),
                                      "allsignal_copy_result", won=bool(row["p_won"]))
        store.allsignal_mark_reported(int(row["window_open"]), now_ms)


async def allsignal_cash_out(settings: Settings, store: Store, telegram, contract,
                             opened: int, remaining: int, now_ms: int,
                             trader=None) -> None:
    """THE CASH-OUT, on the $1 book (operator, 2026-09-28: "cash out must be
    part of the system at all levels ... it cashes out at max profit, no need
    to wait for expiry").

    `cash_out_exit`'s rule exactly - the same settings, gates and order - on
    this window's all-signal position, which that function cannot see: it
    reads `trade_proposals`, and the $1 strategy books in `allsignal_trades`.
    That is the whole reason every $1 trade rode to settlement from its start
    until this was added.

    Through the mirroring client, like the entry, so her account sells too.
    Gated by the kill switch only: a position already held is managed to the
    close even with the $1 strategy switched off, as the main one's are. One
    attempt per window (`record_alert`), as the main rule; a miss holds.
    Never raises.
    """
    try:
        if not settings.cash_out_enabled or remaining < settings.exit_min_seconds:
            return
        if trader is None or not auto_is_on(store, settings):
            return
        row = store.allsignal_open(opened)
        if row is None or row["ticker"] != contract.ticker:
            return
        side = row["side"]
        paid = float(row["fill_price"] or row["limit_price"])
        held = float(row["filled"] or 0)
        count = held - float(row.get("exit_count") or 0)
        if count <= 0:
            return
        # The discounted bid, for the gate AND the order - see cash_out_exit.
        quoted = contract.bid(side)
        if quoted <= 0.0:
            quoted = 1 - contract.ask("DOWN" if side == "UP" else "UP")
        bid = round(quoted - settings.exit_slippage, 4)
        if not 0.0 < bid < 1.0 or bid < settings.cash_out_min_bid:
            return
        available = 1.0 - paid
        at_max = bid >= settings.cash_out_at_bid
        if not at_max and (
            available <= 0 or (bid - paid) < settings.cash_out_capture * available
        ):
            return
        if bid <= paid:                      # never sell at a loss
            return
        if not store.record_alert("allsignal-cashout", opened, now_ms):
            return
    except Exception as exc:  # noqa: BLE001 - never on the main path
        print(f"allsignal: cash-out check failed {exc!r}", flush=True)
        return

    asset = surface.asset(settings.kalshi_series) or "?"
    client = trader if settings.allsignal_mirror else getattr(trader, "_primary", trader)
    sold, sold_at, fee, order_id = 0.0, bid, None, None
    try:
        result = await client.close_position(
            row["ticker"], side, count, bid, floor=settings.min_exit_price)
        sold, order_id = float(result.filled_count or 0), result.entry_order_id
        if sold > 0:
            # THE SALE PRICE IS READ BACK WITH THE ENTRY'S RETRIES. Kalshi's
            # fills feed lags the order ack; one read came back empty and BTC
            # 18:45 on 09-28 was booked - and announced - at the 0.974 quote
            # while the broker filled 0.99 (operator: "close these loops").
            # Whatever is still missing after this is corrected from the
            # synced fills by Store.allsignal_reconcile_exits (exit_fee NULL
            # marks a price the broker has not confirmed yet).
            detail = None
            for attempt in range(4):
                try:
                    detail = await getattr(trader, "_primary", trader).fill_detail(
                        order_id, side)
                except Exception:  # noqa: BLE001 - the quoted price stands in
                    detail = None
                if detail:
                    sold_at, sold, fee = detail
                    break
                if attempt < 3:
                    await asyncio.sleep(1.5)
            store.allsignal_mark_exited(opened, price=sold_at, count=sold, fee=fee,
                                        order_id=order_id, now_ms=now_ms)
        print(f"allsignal [{asset}] cash-out {row['ticker']} {side} x{count:g} "
              f"bid {bid:.2f}: sold {sold:g} at {sold_at:.4f} (paid {paid:.4f}, "
              f"{remaining}s left) - {result.note}", flush=True)
    except Exception as exc:  # noqa: BLE001
        print(f"allsignal [{asset}] cash-out failed {exc!r}", flush=True)

    if telegram is None:
        return
    try:
        if store.allsignal_open(opened) is None and sold > 0:
            # Fully sold, so graded at the sale: the trade's own message flips
            # to CASHED OUT and the result goes out as a reply, now.
            await report_allsignal_results(store, settings, telegram, now_ms)
            return
        reply = store._dicts("SELECT tg_message_id, stake FROM allsignal_trades "
                             "WHERE window_open=?", (opened,))
        reply_to = (reply[0].get("tg_message_id") if reply else None) or None
        await Notifier(telegram, store, settings).send_once(
            "allsignal_cashout", str(opened),
            messages.allsignal_cashout_notice(
                asset=asset, window_open=opened, side=side, fill_price=paid,
                count=count, sold=sold, sold_at=sold_at, bid=bid,
                remaining=remaining,
                budget=_row_stake(reply[0], settings) if reply else settings.allsignal_stake),
            now_ms, reply_to=int(reply_to) if reply_to else None)
    except Exception as exc:  # noqa: BLE001 - reporting is never fatal
        print(f"allsignal: cash-out report failed {exc!r}", flush=True)


def mirror_on(store: Store, name: str) -> bool:
    """May mirror account `name` (m1 = the wife's, m2) take NEW positions on
    THIS instrument?

    Operator, 2026-09-28: "make the wife mirror account or any other mirror
    account follow an on/off flag per asset ... now I want it to only trade
    BTC". Row mirror_<name>_enabled in this instance's store, set by
    scripts/mirror_switch.py; no row = on. Read on every mirrored order, so a
    switch takes effect on the next one with no restart. Exits are never
    switched off (mirror.MirroringExecutionClient._dispatch). The instance must
    also be in MIRROR_INSTANCES for anything to be copied at all.
    """
    return bool(store.get_setting(f"mirror_{name}_enabled", 1.0))


def mirror_name(settings, name: str) -> str:
    """The account's name for messages: m1 -> "Wife", m2 -> "Uncle George",
    m3 -> MIRROR_3_LABEL ("Mirror 3" until named)."""
    label = getattr(settings, f"mirror_{name[1:]}_label", "") if settings else ""
    return (label or ("Wife" if name == "m1" else name)).strip()


def mirror_label(store: Store, settings: Settings) -> str:
    """Who copies THIS instrument right now - "Wife", "Wife + Uncle George" -
    or "" when nobody does."""
    ok, _ = mirror_allowed(settings)
    if not ok:
        return ""
    names = [t.name for t in targets_from_settings(settings) if mirror_on(store, t.name)]
    for guard in getattr(store, "daily_profit_guards", []):
        state = guard.state()
        # An account that trades on past its target (Affoue, 2026-10-05) still copies.
        if guard.account in names and (guard.error or not state or (
                state["paused_ms"] and not getattr(guard, "after_target_stake", 0))):
            names.remove(guard.account)
    return " + ".join(mirror_name(settings, n) for n in names)


def main_strategy_on(store: Store) -> bool:
    """The MAIN strategy's own switch (operator, 2026-09-28: "pause the main
    strategy and let it run in shadow while we let the new strategy run").

    Off, it still records every decision - shadow - and places no NEW entry;
    positions it already holds are still managed to the close. /auto off stays
    the kill switch for everything. scripts/strategy_switch.py sets it.
    """
    return bool(store.get_setting("main_enabled", 1.0))


def auto_is_on(store: Store, settings: Settings) -> bool:
    """Auto trading only runs when it was switched on deliberately.

    The .env value is a default that can only permit; the stored flag is what
    actually decides, so `/auto off` from a phone stops it immediately and a
    restart cannot silently turn it back on.
    """
    default = 1.0 if settings.auto_trade_enabled else 0.0
    return bool(store.get_setting("auto_trade_enabled", default))


def handle_auto_command(
    store: Store, settings: Settings, command: str, now_ms: int, trader_ready: bool
) -> str:
    """`/auto`, `/auto on`, `/auto off` - the kill switch and its status.

    Turning it OFF is unconditional and instant. Turning it ON refuses unless
    execution is actually configured, because an "on" that silently does
    nothing is worse than an error: you would go to sleep believing it was
    trading.
    """
    parts = command.split(maxsplit=1)
    arg = parts[1].strip().lower() if len(parts) > 1 else ""
    limits = auto_limits(store, settings)

    if arg in {"off", "stop", "0", "false"}:
        store.set_setting("auto_trade_enabled", 0.0, now_ms)
        return (
            "\U0001f6d1 <b>AUTO TRADING OFF</b>\n"
            "<i>No further orders will be placed.</i>"
        )
    if arg in {"on", "start", "1", "true"}:
        if not trader_ready:
            return (
                "⚠️ <b>Cannot enable auto trading.</b>\n"
                f"<i>Execution is not configured: {escape(missing_for_execution(settings))}</i>"
            )
        store.set_setting("auto_trade_enabled", 1.0, now_ms)
        return (
            f"\U0001f916 <b>AUTO TRADING ON</b> · ${limits.budget:,.2f} per order\n"
            f"<i>Stops automatically at {-abs(limits.daily_loss_limit):,.2f} for the day, "
            f"{limits.max_trades_per_day} trades, or {limits.max_trades_per_hour}/hour. "
            f"One position at a time. Send /auto off to stop.</i>"
        )

    state = autotrade.AutoState(*store.auto_state(now_ms))
    on = auto_is_on(store, settings)
    blocked = autotrade.auto_block_reason(
        limits, state, 0.9, enabled=on and trader_ready
    )
    return messages.auto_status(
        head=head_for(store, settings),
        on=on,
        budget=limits.budget,
        limits=limits,
        state=state,
        blocked=blocked,
    )


def rejection_reason(
    live_ticker: str, live_ask: float, proposal_ticker: str, entry_limit: float, side: str
) -> str:
    """Why a press must not be filled, or "" if it may proceed.

    Names the specific check and the size of the move. "Market or price
    changed" leaves you unable to tell a rolled window from a one-cent drift,
    and on an edge of roughly a cent per contract that is the whole decision.
    """
    if live_ticker != proposal_ticker:
        return f"the window rolled to {live_ticker}; your press was for {proposal_ticker}"
    if live_ask > entry_limit:
        moved = (live_ask - entry_limit) * 100
        return (
            f"{side} moved to {live_ask:.0%} from the quoted {entry_limit:.0%} "
            f"(+{moved:.0f}c). Not filled - paying over the quote would cost more "
            f"than the edge is worth."
        )
    return ""


def missing_for_execution(settings: Settings) -> str:
    """Exactly which settings still block a press, in the order to fix them."""
    gaps = []
    if not settings.telegram_authorized_user_id:
        gaps.append("TELEGRAM_AUTHORIZED_USER_ID (send /id)")
    if not settings.execution_enabled:
        gaps.append("EXECUTION_ENABLED=true")
    if not settings.kalshi_api_key_id:
        gaps.append("KALSHI_API_KEY_ID")
    if not settings.kalshi_private_key_path:
        gaps.append("KALSHI_PRIVATE_KEY_PATH")
    return ", ".join(gaps)


def execution_configured(settings: Settings) -> bool:
    return bool(
        settings.execution_enabled
        and settings.kalshi_api_key_id
        and settings.kalshi_private_key_path
        and settings.telegram_authorized_user_id
    )


COMMAND_FAILURES: dict = {}     # update_id -> failed attempts


def _redacted(telegram, text: str) -> str:
    """An httpx error's text carries the request URL - and a Telegram URL
    carries the bot token. Never send it to the chat (review 2026-09-30)."""
    token = getattr(telegram, "token", None)
    return text.replace(token, "[token]") if token else text


async def _command_failed(telegram, update, exc: BaseException) -> None:
    """A Telegram update whose handler raised (2026-09-30 review).

    A COMMAND is read again on the next poll - /auto off is retried, up to
    three times - and then said to have failed; it is never silently dropped.
    A BUTTON press is not replayed (a replayed Execute could order twice); it
    is said to have failed. The rest of the batch is read again either way.
    Never raises."""
    try:
        update_id = (update or {}).get("update_id")
        print(f"telegram update {update_id} failed: {exc!r}\n"
              + "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)),
              flush=True)
        if update_id is None:
            return
        if update.get("message") is not None:
            tries = COMMAND_FAILURES[update_id] = COMMAND_FAILURES.get(update_id, 0) + 1
            if tries < 3:
                telegram.offset = update_id          # this command again, next poll
                return
            what = (update["message"].get("text") or "")[:60]
        else:
            what = "button press (not repeated)"
        telegram.offset = update_id + 1              # the rest of the batch again
        await telegram.send(
            "\u26a0\ufe0f <b>COMMAND FAILED</b>\n"
            f"<code>{escape(what)}</code>\n"
            f"{escape(type(exc).__name__)}: {escape(_redacted(telegram, str(exc))[:150])}\n"
            "<i>It may not have been applied - check its status and send it again.</i>")
    except Exception as inner:  # noqa: BLE001 - the report must never raise
        print(f"command failure report failed: {inner!r}", flush=True)


async def process_telegram(
    telegram: Telegram,
    store: Store,
    market_client: KalshiClient,
    trader: KalshiExecutionClient | None,
    settings: Settings,
) -> None:
    # ONLY ONE INSTANCE MAY CONSUME THE COMMAND STREAM. getUpdates
    # acknowledges with an offset, so a second process polling the same bot
    # token silently steals messages from the first - and the message it
    # steals could be the kill switch. A non-listening instance still sends
    # every alert it would have sent.
    if not settings.telegram_commands_enabled:
        return
    # A FAILED COMMAND IS READ AGAIN, NEVER DROPPED. `updates()` moves the
    # offset past the whole batch before any handler runs; until 2026-09-30 a
    # handler error also ended the process, and the restart re-read the batch.
    # The poll now survives errors, so the reader rewinds instead.
    batch = await telegram.updates()
    current: dict = {}
    try:
        for update in batch:
            current["update"] = update
            message = update.get("message")
            command = message.get("text", "").split("@")[0] if message else ""
            if command == "/id":
                # Deliberately unauthenticated: it only tells you your own Telegram
                # id, which you need before TELEGRAM_AUTHORIZED_USER_ID can be set -
                # and until it is set, nothing authenticates at all.
                sender = int(message.get("from", {}).get("id", 0))
                await telegram.send(
                    "\U0001f194 <b>Your Telegram user id</b>\n"
                    f"<code>{sender}</code>\n"
                    "<i>Put this in .env as TELEGRAM_AUTHORIZED_USER_ID, then restart "
                    "the service. Only this id can press Execute.</i>"
                )
                continue
            verb = command.split(maxsplit=1)[0] if command else ""
            if verb == "/auto":
                if int(message.get("from", {}).get("id", 0)) != settings.telegram_authorized_user_id:
                    continue
                ready = bool(execution_configured(settings) and trader)
                await telegram.send(
                    handle_auto_command(store, settings, command, int(time.time() * 1000), ready)
                )
                continue
            if verb in {"/size", "/autosize"}:
                if int(message.get("from", {}).get("id", 0)) != settings.telegram_authorized_user_id:
                    continue
                reply = handle_size_command(store, settings, command, int(time.time() * 1000))
                await telegram.send(reply)
                continue
            if command == "/ledger":
                if int(message.get("from", {}).get("id", 0)) != settings.telegram_authorized_user_id:
                    continue
                await telegram.send(
                    messages.ledger(head=head_for(store, settings), rows=store.ledger())
                )
                continue
            if command == "/intel":
                if int(message.get("from", {}).get("id", 0)) != settings.telegram_authorized_user_id:
                    continue
                await telegram.send(
                    messages.intel(
                        head=head_for(store, settings),
                        gates=store.gate_study(),
                        needed=samples_needed(store),
                    )
                )
                continue
            if command == "/learning":
                if int(message.get("from", {}).get("id", 0)) != settings.telegram_authorized_user_id:
                    continue
                # The RUNNING learner, not a fresh one. A second runner built here
                # would read the same files and report a plausible snapshot while
                # knowing nothing about whether a fit is in progress - which is one
                # of the four states this message exists to distinguish.
                runner = LEARNING.get("runner")
                if runner is None:
                    runner = LearningRunner(settings, store)
                await telegram.send(
                    messages.learning(
                        head=head_for(store, settings),
                        state=runner.snapshot(int(time.time() * 1000)),
                        candidates=active_candidates(settings),
                        board=store.candidate_scoreboard(),
                    )
                )
                continue
            if command == "/sessions":
                if int(message.get("from", {}).get("id", 0)) != settings.telegram_authorized_user_id:
                    continue
                await telegram.send(
                    messages.sessions(
                        head=head_for(store, settings),
                        buckets=store.by_session(),
                        measured=MEASURED_SESSION_EDGE,
                    )
                )
                continue
            if command in {"/keys", "/status"}:
                if int(message.get("from", {}).get("id", 0)) != settings.telegram_authorized_user_id:
                    continue
                await telegram.send(
                    messages.status(
                        head=head_for(store, settings),
                        execution_ready=bool(execution_configured(settings) and trader),
                        manual=settings.manual_execution_enabled,
                        window=(settings.entry_from_seconds, settings.entry_to_seconds),
                    )
                )
                continue
            callback = update.get("callback_query")
            if not callback:
                continue
            callback_id = callback["id"]
            user_id = int(callback.get("from", {}).get("id", 0))
            callback_message = callback.get("message", {})
            if user_id != settings.telegram_authorized_user_id:
                if not settings.telegram_authorized_user_id:
                    # Bootstrap: with no authorized user configured, "Not authorized"
                    # is a dead end - there is no way to become authorized without
                    # knowing this id. Hand it over instead of stonewalling.
                    await telegram.answer_callback(
                        callback_id,
                        f"Setup needed. Your Telegram id is {user_id}. Put it in .env as "
                        f"TELEGRAM_AUTHORIZED_USER_ID and restart; then this button works.",
                    )
                    print(f"SETUP: unauthorized press from telegram id {user_id}", flush=True)
                else:
                    await telegram.answer_callback(callback_id, "Not authorized")
                continue
            data = callback.get("data", "")
            action, separator, proposal_id = data.partition(":")
            if not separator or action not in {"execute", "skip", "details"}:
                await telegram.answer_callback(callback_id, "Invalid action")
                continue
            if action == "details":
                # Read back verbatim, never recomputed. The button exists to show
                # what was true when the call was made, and re-deriving it here
                # would quietly describe a market that has since moved.
                body = store.details(proposal_id)
                await telegram.answer_callback(callback_id, "" if body else "No details stored")
                if body:
                    await telegram.send(body)
                continue
            if action == "skip":
                skipped = store.skip_proposal(proposal_id)
                await telegram.answer_callback(callback_id, "Skipped" if skipped else "Already handled")
                if skipped:
                    await telegram.clear_buttons(
                        callback_message["chat"]["id"], callback_message["message_id"]
                    )
                continue
            if not execution_configured(settings) or trader is None:
                await telegram.answer_callback(
                    callback_id, "Execution is disabled; run local key setup"
                )
                continue
            proposal = store.proposal(proposal_id)
            now_ms = int(time.time() * 1000)
            if proposal is None or proposal.status != "pending" or proposal.expires_at < now_ms:
                await telegram.answer_callback(callback_id, "This entry is expired or already handled")
                continue
            try:
                live_market = await market_client.active_market(now_ms)
                live_ask = live_market.ask(proposal.side)
                # Say which check failed and by how much. "Market or price changed"
                # leaves you unable to tell a rolled window from a one-cent move,
                # and on an edge of about a cent per contract that difference is
                # the whole decision.
                detail = rejection_reason(
                    live_market.ticker,
                    live_ask,
                    proposal.ticker,
                    proposal.entry_limit,
                    proposal.side,
                )
                if detail:
                    store.finish_proposal(proposal.id, "rejected", detail)
                    await telegram.answer_callback(callback_id, f"Not filled: {detail}")
                    await telegram.clear_buttons(
                        callback_message["chat"]["id"], callback_message["message_id"]
                    )
                    continue
                claimed = store.claim_proposal(proposal.id, now_ms)
                if claimed is None:
                    await telegram.answer_callback(callback_id, "Already handled")
                    continue
                # As in the unattended path: only the order call may mark this
                # failed. Once it returns, money has moved, and a Telegram error
                # must not rewrite a real position out of the loss floor.
                submitted_ms = int(time.time() * 1000)
                result = await trader.execute_with_take_profit(
                            claimed, settings.entry_slippage,
                            ceiling=settings.max_entry_price,
                        )
                log_execution(
                    store, claimed=claimed, result=result,
                    decision_ask=claimed.entry_limit,
                    submitted_ms=submitted_ms, acked_ms=int(time.time() * 1000),
                    attempt=1, entry_slippage=settings.entry_slippage,
                )
                store.finish_proposal(
                    claimed.id,
                    result.status,
                    result.note,
                    result.entry_order_id,
                    result.take_profit_order_id,
                )
                try:
                    paid, filled_count = claimed.entry_limit, result.filled_count
                    fee, exact = None, True
                    if result.filled_count > 0:
                        paid, filled_count, fee, exact = await record_fill_detail(
                            store, trader, claimed.id, claimed.window_open, claimed.side,
                            result.entry_order_id, claimed.entry_limit, result.filled_count,
                        )
                    await telegram.answer_callback(callback_id, result.note)
                    await telegram.clear_buttons(
                        callback_message["chat"]["id"], callback_message["message_id"]
                    )
                    await telegram.send(
                        messages.order_result(
                            status=result.status,
                            side=claimed.side,
                            count=filled_count,
                            note=result.note,
                            order_id=result.entry_order_id,
                            price=paid if result.filled_count > 0 else None,
                            limit=claimed.entry_limit,
                            fee=fee,
                            exact=exact,
                        )
                    )
                except (httpx.HTTPError, OSError, RuntimeError, ValueError) as exc:
                    print(
                        f"press: post-order reporting failed {type(exc).__name__}: {exc}",
                        flush=True,
                    )
            except (httpx.HTTPError, OSError, RuntimeError, ValueError) as exc:
                # Reached only when the ORDER itself failed - see above.
                store.finish_proposal(proposal.id, "failed", f"{type(exc).__name__}: {exc}")
                await telegram.answer_callback(callback_id, "Order failed; see local logs")
                print(f"execution error: {type(exc).__name__}: {exc}", flush=True)
    except Exception as exc:  # noqa: BLE001 - see _command_failed
        await _command_failed(telegram, current.get("update"), exc)


def log_execution(
    store: Store,
    *,
    claimed,
    result,
    decision_ask: float | None,
    submitted_ms: int,
    acked_ms: int,
    attempt: int,
    entry_slippage: float = 0.0,
) -> None:
    """Write down what one submitted order actually got. NEVER raises.

    A backtest credits a fill at the price it saw; live, 9 of 21 orders filled.
    Historical candles cannot close that gap - they record what the market did,
    not what our order got - so every attempt has to be written down as it
    happens, filled or not.

    No book is read here. Reading before submitting would add a round trip to
    the critical path and worsen the staleness being measured;
    `decision_to_submit_ms` is the free version of it. Reading after would use
    the recorder's `orderbook_fp` mapping, which FINDINGS records as unresolved
    and must not be built on. The price after a miss is already captured
    reliably by the next poll's observation row and is joined at analysis time.

    Catches BaseException-minus-the-unignorable on purpose: this is pure
    bookkeeping running immediately after money has moved, and nothing it does
    may be allowed to propagate into the order path.
    """
    try:
        filled = bool(result is not None and result.filled_count > 0)
        # Looked up here, not passed in. `TradeProposal` has no `created_at`,
        # and reading a missing attribute in an ARGUMENT expression raises at
        # the call site - outside this try, in the order path, straight past
        # `finish_proposal`. Anything that can fail belongs inside this block.
        decision_ms = store.proposal_created_at(claimed.id)
        store.record_execution({
            "proposal_id": claimed.id,
            "signal_id": signal_id_for(claimed.ticker),
            "session_id": SESSION_ID,
            "ticker": claimed.ticker,
            "side": claimed.side,
            "window_open": claimed.window_open,
            "attempt": attempt,
            "decision_ask": decision_ask,
            # The limit that actually went to Kalshi, which is the proposal's
            # limit PLUS the slippage allowance (`execute_with_take_profit`
            # caps the sum at 0.99). Recording `entry_limit` here logged 0.85
            # for three orders that were really submitted at 0.86 - and the
            # slippage allowance is exactly the quantity that decides whether
            # a fill happens, so the column that exists to explain misses was
            # hiding the variable under test.
            "limit_submitted": min(
                claimed.entry_limit + max(0.0, entry_slippage), 0.99
            ),
            "decision_ms": decision_ms,
            "submitted_ms": submitted_ms,
            "acked_ms": acked_ms,
            "decision_to_submit_ms": (
                submitted_ms - decision_ms if decision_ms else None
            ),
            "round_trip_ms": acked_ms - submitted_ms,
            "timing": timing_breakdown(),
            "filled": filled,
            # ExecutionResult has no fill price - it is read back separately
            # by `record_fill_detail` and lands on `trade_proposals.fill_price`,
            # which is joined at analysis time. Do not invent one here.
            "fill_price": None,
            "fill_count": result.filled_count if result is not None else 0.0,
            "ask_after": None,
        })
    except Exception as exc:  # noqa: BLE001 - bookkeeping after money moved
        # Deliberately broad. This runs immediately after an order has been
        # placed; there is no failure here worth losing a filled position over.
        print(f"execution log failed: {type(exc).__name__}: {exc}", flush=True)


async def record_fill_detail(
    store: Store,
    trader: KalshiExecutionClient,
    proposal_id: str,
    window_open: int,
    side: str,
    order_id: str,
    fallback_price: float,
    fallback_count: float,
) -> tuple[float, float, float | None, bool]:
    """Read back what the exchange actually did, and store it.

    Returns (price paid, contracts, fee, exact). `exact` is False when the
    lookup failed and the posted limit is standing in - the caller must say so
    rather than present the limit as the price paid, because a buy limit only
    ever fills at or below itself and the difference is real money: the first
    live order was limited at 87c and filled at 84c.

    Shared by the unattended and the hand-pressed paths. Only the unattended one
    recorded fills at first, so a trade taken by button was booked at the quoted
    ask and fed that price to the daily loss floor.
    """
    # Retried, because the usual failure is a race rather than an error: the
    # order returns filled before Kalshi's fills feed has published the fill, so
    # an immediate lookup finds nothing. Observed live on 2026-09-21 - the fill
    # was absent at the moment of asking and present seconds later, at 73c
    # against a 74c limit. Waiting costs nothing: the alert is already out and
    # the next window is a quarter of an hour away.
    detail = None
    for attempt in range(4):
        if attempt:
            await asyncio.sleep(1.5 * attempt)
        try:
            detail = await trader.fill_detail(order_id, side)
        except (httpx.HTTPError, OSError, ValueError, KeyError) as exc:
            print(f"fill lookup failed: {type(exc).__name__}: {exc}", flush=True)
            detail = None
        if detail:
            break
    if not detail:
        print(f"fill for {order_id} never appeared; pricing at the limit", flush=True)
        return fallback_price, fallback_count, None, False
    paid, count, fee = detail
    store.record_fill(proposal_id, window_open, paid, count, fee)
    return paid, count, fee, True


def create_proposal(
    store: Store,
    strategy: str,
    opened: int,
    contract: KalshiMarket,
    side: str,
    entry: float,
    take_profit: float,
    count: int,
    now_ms: int,
    lifetime_seconds: int,
    base_count: int | None = None,
) -> TradeProposal:
    expires_at = min(now_ms + lifetime_seconds * 1000, contract.close_ms - 10_000)
    return store.create_proposal(
        strategy,
        opened,
        contract.ticker,
        side,
        entry,
        take_profit,
        count,
        expires_at,
        contract.close_ms,
        now_ms,
        base_count=base_count,
    )


# One id per service run, generated at import. Restarts get a new one on
# purpose: it is how you tell "the archive went quiet because nothing happened"
# apart from "the archive went quiet because the process died".
SESSION_ID = uuid4().hex[:12]

# A fixed namespace so signal_id is derived, not generated. The same market
# always yields the same id, before and after a restart, which is what lets a
# path survive the service dying mid-window.
SIGNAL_NAMESPACE = uuid5(NAMESPACE_URL, "https://kalshi.com/btc15-signal")


def signal_id_for(ticker: str) -> str:
    """Stable id for one opportunity, derived from the market it belongs to."""
    return uuid5(SIGNAL_NAMESPACE, ticker).hex[:16]


def book_metrics(settings: Settings, ticker: str) -> dict:
    """Latest recorded order book for a ticker, flattened for the archive.

    Read from the recorder's database rather than fetched, so archiving costs
    the trading path no API call and cannot contribute to a rate limit on the
    request that actually matters. Empty when the recorder is down or its last
    snapshot is stale - the observation is still written, just without depth.
    """
    import json
    import sqlite3

    try:
        db = sqlite3.connect(f"file:{settings.microstructure_path}?mode=ro", uri=True)
        row = db.execute(
            "SELECT captured_ms, yes_bid, yes_bid_size, yes_ask_size, book_yes, book_no, "
            "depth_bid_qty, depth_ask_qty, trade_count, buy_volume, sell_volume, vwap, "
            "open_interest, volume FROM book_snapshots WHERE ticker=? "
            "ORDER BY captured_ms DESC LIMIT 1",
            (ticker,),
        ).fetchone()
        db.close()
    except sqlite3.Error:
        return {}
    if not row:
        return {}
    (captured, yes_bid, bid_size, ask_size, raw_yes, raw_no, dbq, daq,
     trades, buys, sells, vwap, oi, volume) = row
    age = (time.time() * 1000 - captured) / 1000
    if age > 180:
        return {"book_age_s": round(age, 1)}  # stale: record the staleness, not the numbers
    try:
        yes_levels = json.loads(raw_yes or "[]")
        no_levels = json.loads(raw_no or "[]")
    except ValueError:
        yes_levels = no_levels = []
    yes_depth = sum(size for _, size in yes_levels)
    no_depth = sum(size for _, size in no_levels)
    total = yes_depth + no_depth
    return {
        "yes_bid": yes_bid,
        "book_yes_depth": round(yes_depth, 1),
        "book_no_depth": round(no_depth, 1),
        "book_yes_share": round(yes_depth / total, 4) if total else None,
        "book_bid_size": bid_size,
        "book_ask_size": ask_size,
        "book_levels": len(yes_levels) + len(no_levels),
        "depth_bid_qty": dbq,
        "depth_ask_qty": daq,
        "trade_count": trades,
        "buy_volume": buys,
        "sell_volume": sells,
        "vwap": vwap,
        "open_interest": oi,
        "volume": volume,
        "book_age_s": round(age, 1),
    }


def archive_observation(
    settings: Settings,
    store: Store,
    contract: KalshiMarket,
    snapshot: MarketSnapshot,
    opened: int,
    remaining: int,
    now_ms: int,
    brti=None,
) -> None:
    """One row per poll, for the WHOLE window, whatever the rule thinks.

    Deliberately outside every trading gate. The first archive sat inside the
    entry scan, so it saw only 630s-330s and skipped any poll where the spread
    was wide - which threw away both the path from entry to expiry and exactly
    the conditions worth studying. A strategy we do not yet have will not be
    found in the minutes we already trade.

    Never raises: archiving must not be able to stop the service trading.
    """
    try:
        from .features import _session, _weekday

        # NEITHER THE BINANCE MODEL NOR THE BINANCE RULE RUNS HERE.
        #
        # This called `predict(snapshot)` and `EntryRule.matches(...)` on every
        # poll regardless of instrument. Under `kalshi_only` the snapshot is
        # built from BRTI, so that was Binance-fitted arithmetic over
        # Kalshi-scaled inputs, written to `side`, `raw_probability`,
        # `bucket`, `distance_bps` and `rule_match` - names that say nothing
        # about which instrument produced them. It is also what crashed the
        # service every fifteen minutes on the morning of 2026-09-23:
        # `strategy.check_facts` compares `prediction.raw_probability`, which
        # a Kalshi prediction does not have.
        #
        # A field we cannot fill from Kalshi is left NULL. A missing row is
        # visible; a mislabelled one is not.
        if settings.kalshi_only:
            if brti is None:
                return          # no reference, nothing honest to archive
            prediction = kalshi_signal.prediction_from(brti)
            our_ask = contract.ask(prediction.side)
            rule_match, _facts, _failed_names = kalshi_signal.evaluate(
                KalshiBRTIRule.load(settings.kalshi_strategy_path),
                brti, our_ask, remaining,
            )
            failed = ()
            volatility = max(brti.brti_volatility_bps, 1.0)
        else:
            rule = EntryRule.load(settings.strategy_path)
            prediction = predict(snapshot)
            our_ask = contract.ask(prediction.side)
            rule_match, failed = rule.matches(prediction, snapshot, our_ask)
            volatility = max(snapshot.volatility_5m_bps, 1.0)

        # What we could close for right now, and what we are sitting on.
        exit_bid = 1 - contract.ask("DOWN" if prediction.side == "UP" else "UP")
        position = store.open_position_detail(opened)
        holding, paid, unrealised = 0, None, None
        if position:
            side, paid, count, _t, _i = position
            holding = 1
            held_bid = 1 - contract.ask("DOWN" if side == "UP" else "UP")
            unrealised = round((held_bid - paid) * count, 4)

        # What the order looks like right now, if there is one. Read per poll so
        # the path shows the transition from proposed to filled to exited rather
        # than only the end state.
        proposal_id = order_state = exit_price = exit_reason = None
        order = store.db.execute(
            "SELECT id, status, exit_price, result_note FROM trade_proposals "
            "WHERE window_open=? AND strategy='primary' ORDER BY created_at DESC LIMIT 1",
            (opened,),
        ).fetchone()
        if order:
            proposal_id, status, exit_price, note = order
            order_state = {
                "pending": "proposed", "executing": "proposed",
                "filled": "filled", "protected": "filled", "unprotected": "filled",
                "exited": "exited",
            }.get(status, status)
            if status == "exited":
                exit_reason = note

        _hour = datetime.fromtimestamp(opened / 1000, UTC).hour
        # Archived, never enforced: the regime lean is recorded on every
        # observation so the hour question accumulates evidence. It moves
        # the confidence EXPLANATION only - it cannot skip a market, stop
        # polling, block an order or silence an alert.
        _regime = weight_for_hour(_hour)
        row = {
            "session_id": SESSION_ID,
            "market_id": contract.ticker,
            "signal_id": signal_id_for(contract.ticker),
            "proposal_id": proposal_id,
            "order_state": order_state or "none",
            "exit_price": exit_price,
            "exit_reason": exit_reason,
            "alerted": int(
                store.db.execute(
                    "SELECT COUNT(*) FROM strategy_alerts "
                    "WHERE strategy='primary' AND window_open=?",
                    (opened,),
                ).fetchone()[0]
                > 0
            ),
            "window_open": opened, "remaining_s": remaining, "observed_ms": now_ms,
            "ticker": contract.ticker, "target": snapshot.target, "btc": snapshot.price,
            "side": prediction.side,
            # NULL ON THE KALSHI PATH. There is no Kalshi-native model, so
            # there is no probability and no bucket - and writing the Binance
            # ones here is how a column comes to hold two different meanings.
            "raw_probability": getattr(prediction, "raw_probability", None),
            "bucket": getattr(prediction, "bucket", None),
            "our_ask": our_ask,
            "yes_ask": contract.yes_ask, "no_ask": contract.no_ask,
            "exit_bid": exit_bid,
            # The reference's own numbers on the Kalshi path, the spot ones
            # on the legacy path. They are not the same quantity and the
            # thresholds measured for one do not transfer to the other.
            "momentum_5m_bps": (
                brti.brti_momentum_bps if settings.kalshi_only
                else snapshot.momentum_5m_bps
            ),
            "volatility_5m_bps": (
                brti.brti_volatility_bps if settings.kalshi_only
                else snapshot.volatility_5m_bps
            ),
            "distance_bps": (
                abs(brti.signed_distance_bps) if settings.kalshi_only
                else prediction.distance_bps
            ),
            "normalized_distance": (
                brti.brti_normalized_distance if settings.kalshi_only
                else prediction.distance_bps / volatility
            ),
            "spread_bps": snapshot.spread_bps,
            "futures_basis_bps": snapshot.futures_basis_bps,
            "taker_imbalance": snapshot.taker_imbalance,
            "window_high": snapshot.window_high, "window_low": snapshot.window_low,
            "elapsed_minutes": snapshot.elapsed_minutes,
            "rule_match": int(rule_match), "failed_gates": failed or None,
            "session": _session(opened), "weekday": _weekday(opened),
            "hour_utc": _hour,
            "vol_regime": (
                "low" if snapshot.volatility_5m_bps < 8
                else ("high" if snapshot.volatility_5m_bps > 20 else "mid")
            ),
            "regime_weight": _regime.weight,
            "confidence_adjustment": regime_confidence_points(_regime),
            "holding": holding, "entry_paid": paid, "unrealised": unrealised,
        }
        row.update(book_metrics(settings, contract.ticker))
        store.observe_full(row)
    except Exception as exc:  # noqa: BLE001 - archiving must never stop trading
        print(f"archive failed: {type(exc).__name__}: {exc}", flush=True)


def max_net_profit(count: int, ask: float) -> float:
    """What this order can win at most, net of fees, if it settles in the money.

    Settlement pays the dollar with no second fee, so only the entry fee comes
    off. The fee is the deployed `kalshi_fee_charged` and is never modelled
    here: a sizing rule that disagreed with the fee schedule would be deciding
    on money the account does not have.
    """
    return count * (1.0 - ask) - kalshi_fee_charged(ask, count)


def recovery_size(
    store: Store, settings: Settings, base: int, ask: float,
    state: RecoveryState | None = None,
) -> tuple[int, str]:
    """Size up while a realised deficit is outstanding AND this trade can dent it.

    THE OPERATOR'S RULE, 2026-09-22, in two parts.

    The deficit: recovery activates on a realised net loss, tracks what is
    still missing after fees, and turns off the moment realised profit has
    covered it - see `Store.recovery_state`. A further loss increases it.

    The eligibility, which is this function: recovery NEVER creates a trade.
    The strategy's own gates have already passed by the time this is called;
    all this decides is the size of a trade that is happening anyway. The
    deficit is divided across the remaining planned steps, and the upsize
    applies only if this trade's maximum net profit covers that share.
    Otherwise the trade goes out at BASE size and recovery stays ACTIVE - a
    base-size win still pays the deficit down.

        deficit 1.64 over 4 steps -> 0.41 a trade
        2 @ 0.90 -> 0.1874 net     -> not eligible, base size
        2 @ 0.75 -> 0.4737 net     -> eligible

    That gate exists because 2 contracts at 90c risk $1.80 to win 19c: at the
    top of the price band the upsize adds exposure it cannot recover with.

    It never escalates - the cap is `high_confidence_contracts`, whatever the
    deficit is - so this is not a martingale. The $10 martingale measured
    -15.74 on a day the base system made +4.41 (section 32) and is not this.

    `state` is passed in by the order path so the ledger is folded once per
    decision; it reads it itself everywhere else.

    THE UPFRONT UPSIZE IS OFF (operator, 2026-09-22). Recovery no longer buys
    a larger BASE position; it acts only through the conditional add-on in
    `recovery_add.py`, which rests a second contract 2c below the actual fill
    and only while the BRTI evidence still holds.

    Leaving both on would stack: two contracts bought upfront and a third
    rested behind them, on a deficit that justified one extra. The base entry
    is one contract whether or not a deficit is outstanding.

    The eligibility arithmetic below is kept and still exercised by the
    add-on's own gate, so turning this back on is a one-line change rather
    than a rewrite.
    """
    state = (
        store.recovery_state(settings.recovery_steps) if state is None else state
    )
    if not state.active or not settings.recovery_upfront_upsize_enabled:
        return base, ""
    count = max(base, settings.high_confidence_contracts)
    required = state.required_per_trade()
    profit = max_net_profit(count, ask)
    if profit < required:
        # Base size, and an EMPTY reason: the caller only overrides the size
        # when there is one, and a line saying "recovering" beside a base-size
        # order would describe something that is not happening.
        return base, ""
    return count, (
        f"recovering {state.deficit:.2f} outstanding - {profit:.2f} max net "
        f"covers the {required:.2f} share of {max(1, state.steps)} step(s)"
    )


def loss_step_size(
    store: Store, settings: Settings, base: int, ask: float,
    lost: bool | None = None,
) -> tuple[int, str]:
    """(contracts, why) - after a losing market, size up AT A PRICE THAT PAYS.

    THE OPERATOR'S RULE, 2026-09-24, as AMENDED BY THEM ON 2026-09-25: the
    upsize no longer fires on whatever trade comes next. It waits for an ask
    inside `loss_step_band_lo`-`loss_step_band_hi` (0.70-0.79), which may be
    three to five trades later, because that is where a fixed dollar budget can
    actually dent a deficit: the extra contract wins `1 - ask`, so at 0.90 it
    risks 90c to make 10c and at 0.75 it risks 75c to make 25c. Spending the
    step at the top of the band earns about 20c, which base size would have
    earned anyway at a better price - the operator's exact objection.

    WHAT WAITING CHANGES, stated because it is a real change and not a tweak.
    Before, the step fired once per loss, immediately, and a win reset it. Now
    it stays ARMED across intervening markets whatever they do, and it fires on
    the first in-band trade. So it can land after a win, which the old rule
    could not do. Three bounds keep that from becoming a standing upsize:

      * it expires after `loss_step_wait_markets` settled markets, unspent;
      * it fires ONCE per losing episode - `Store.upsized_since` reads whether
        an upsized entry already went out, rather than trusting a flag;
      * the budget never escalates, and a loss ON THE RECOVERY TRADE ITSELF
        arms nothing (2026-09-27) - only the next ordinary loss arms again.

    So a losing recovery is never followed by another recovery: this is one
    step per episode, not a martingale.

    THE EVIDENCE DOES NOT SUPPORT THE BAND, and that is recorded rather than
    hidden. Expected value per extra contract is `p - ask`, which is the
    calibration residual itself, so the payoff ratio cancels: 25c at 0.75 beats
    10c at 0.90 only if the win rate fails to make up the difference. On BTC's
    7,139 ungated brti-4 points the 0.70-0.79 band measured +0.0026 per
    contract against +0.0159 outside it, and 0.90-0.93 was the strongest cell
    at +0.0240 [+0.0009]. The population that actually matters - setups the
    gates admit - could not be scored, because BTC's brti-2-era floors admit
    too few brti-4 points. The operator decided with that in view and
    instructed it be shipped; sizing is theirs. See config.py for the table.

    IT KEYS ON THE LAST MARKET, NOT THE DEFICIT. `recovery_size` above sizes
    while money is outstanding and divides it across a plan; this asks one
    question - did the previous market lose - and that is the rule that was
    measured. They must not both run, so the caller applies this LAST and the
    upfront recovery upsize is off.

    IT NEVER CAUSES A TRADE. Every gate has already passed by the time this is
    called; it only decides the size of an order that is going out anyway. A
    loss is never a reason to enter.

    THE CEILING IS REAL WORK, not decoration. At the 0.70 floor of the price
    band the budget buys more contracts than at the 0.93 ceiling, and a stale
    or mispriced ask is what turns a dollar budget into a position nobody
    chose, so the count is capped before it leaves this function. The cap is
    deliberately left above what this rule can now reach: it bounds the other
    upsize paths too.

    The evidence, which does not support the rule on its own, is recorded
    against `loss_step_enabled` in config.py and in FINDINGS 61. The operator
    decided with it in view.

    IT FOLLOWS THE BASE (operator, 2026-09-27: "upsizing recovery must follow
    as well" - "automatically double"). `loss_step_budget` is dollars PER BASE
    CONTRACT, the way `auto_budget` is. At base 1 that is the $2 step as it
    always was - 2 contracts anywhere in the band. At base 2 it is 4. It was a
    flat $2 until then, and at 00:00 on 2026-09-27 the capital review moved
    every instance to base 2: $2 buys 2 in the band, 2 is not above a base of
    2, and the step returned the base with an empty reason on every trade -
    recovery was OFF, silently, and the wife's mirror (which only follows an
    actual upsize) lost its recovery with it.
    """
    if not settings.loss_step_enabled or not settings.recovery_enabled:
        return base, ""

    lo, hi = settings.loss_step_band_lo, settings.loss_step_band_hi
    wait = max(1, settings.loss_step_wait_markets)

    # THE ARMING LOSS, and how many markets have settled since it. Both come
    # off `settled_bot_markets`, which is the same join, fees and early-exit
    # arithmetic as `last_market_lost` - so "did it lose" and "how long ago"
    # cannot disagree about which markets those were or who traded them.
    history = store.settled_bot_markets(limit=max(4 * wait, 24))
    if not history:
        # No settled bot market at all. None is not a loss: a fresh database
        # has not seen a win either, and upsizing on "not a win" is the
        # opposite of the rule asked for.
        return base, ""
    armed_window = None
    markets_since = 0
    for index, (window, won) in enumerate(history):
        if not won:
            armed_window, markets_since = window, index
            break
    if armed_window is None:
        return base, ""

    # `lost` IS NOT CONSULTED for arming any more, and that is the point of the
    # amendment rather than an oversight. It answers "did the LAST market lose",
    # and the waiting step has to survive exactly the case where it did not: a
    # loss, then a win at 0.88, then an in-band setup two markets later is the
    # behaviour the operator asked for, and an early return on `lost is False`
    # would have refused it. The parameter is kept because callers and tests
    # pass it and it still reflects the ledger; `history` is the authority.

    # ONCE PER LOSING EPISODE, NEVER BACK TO BACK (operator, 2026-09-27:
    # "remove the back to back, it should only happen once"). A loss on a
    # trade the step itself sized does not arm another step - that loss IS the
    # recovery failing, and re-arming at the same size is what put SOL through
    # two 4-contract losses in a row that afternoon (16:00 -2.98, then 16:15
    # -3.32). The next ORDINARY loss arms normally.
    if store.window_was_upsized(
        armed_window, tiered=settings.capital_sizing_enabled
    ):
        return base, "last loss was the recovery trade itself - no second recovery"
    if markets_since >= wait:
        return base, (
            f"recovery expired unspent - {markets_since} markets since the "
            f"loss, limit {wait}"
        )
    if store.upsized_since(armed_window, tiered=settings.capital_sizing_enabled):
        return base, "recovery already taken for this loss"
    if not lo <= ask <= hi:
        # NOT an upsize, and it must not read as one. The step stays armed and
        # this trade goes out at base size, which still wins its own money.
        return base, (
            f"recovery waiting for {lo:.2f}-{hi:.2f} - this ask is {ask:.2f} "
            f"({markets_since + 1} of {wait} markets used)"
        )

    per_base = contracts_for_budget(settings.loss_step_budget, ask)
    # NOT CAPPED ANY MORE. This count is no longer an order size - since the
    # recovery became a combo at base size it only answers "is a recovery
    # due" (count above base). Capped at `loss_step_max_contracts` (8), a base
    # of 8 or more - reachable now the base scales with capital, uncapped -
    # would have read as "not due" and silently ended every recovery.
    count = per_base * max(1, base)
    if count <= base:
        # Empty reason, so no line claims an upsize that did not happen.
        return base, ""
    return count, (
        f"recovery taken at {ask:.2f}, inside {lo:.2f}-{hi:.2f} - "
        f"${settings.loss_step_budget:.2f} per base contract buys {per_base}, "
        f"x base {base} = {count}, "
        f"{markets_since} market(s) after the loss"
    )


def add_on_stands_down(store, settings, contract, position) -> bool:
    """Should the conditional add-on stand down for this position?

    It stands down only when the LOSS STEP ACTUALLY SIZED THIS POSITION. The two
    must never stack: the step was measured as the whole position, so resting
    another contract behind it adds exposure nobody chose.

    EXTRACTED SO IT CAN BE EXECUTED BY A TEST. This lived inline inside
    `service`, and on 2026-09-25 it read `position.side` - but `position` is the
    TUPLE `open_position_detail` returns, `(side, paid, count, ticker, id)`. It
    raised AttributeError on every poll once an instrument held a position, the
    watchdog restarted ETH every 11 seconds until it hit its 6-per-hour budget,
    and ETH was then DOWN for twenty minutes with no alert.

    The test that covered this block passed throughout, because it SCANNED THE
    SOURCE for strings rather than running it. A source scan cannot catch an
    AttributeError, so the logic now lives somewhere a test can call.

    IT READS THE POSITION, NOT A RE-RUN OF THE STEP (2026-09-27). It used to
    ask "would the step fire now, at base 1, at the current ask?", which is
    the wrong question twice over. Once the step HAD sized the position,
    `upsized_since` found that very entry, the re-run answered "already
    taken" at base size, and the add-on was cleared to rest behind the
    stepped position - the stacking this exists to prevent. And at base 2,
    an ordinary entry with a loss armed and the ask drifting into the band
    read as stepped, standing the add-on down for an upsize that never
    happened. The fact is on the entry row: it went out above its day's
    base, or it did not. `contract` is kept for the callers.
    """
    if not position:
        # No position, nothing to stand down - and `position[0]` would raise.
        return False
    if not settings.loss_step_enabled:
        return False
    return store.entry_was_upsized(
        position[4], tiered=settings.capital_sizing_enabled
    )


def recovery_sizing(
    store: Store, settings: Settings, count: int, size_reason: str, ask: float,
) -> tuple[int, str, int, bool, str, bool]:
    """(count, size_reason, base_count, is_recovery, recovery_reason, combo_due).

    `combo_due` - a recovery is due on this entry, to be placed as a combo at
    the base count (2026-09-27). `is_recovery` is always False since then.

    `recovery_reason` is the UPFRONT recovery's own reason ("" unless
    `recovery_size` upsized). The post-order path reads it to decide whether
    a fill spends a step of the recovery plan - so it has to come back out.
    When this was first lifted out of `primary_signal` it did not, and on
    2026-09-27 ETH and SOL each crashed with a NameError straight after their
    first fill: that line runs only once an order has filled, and nothing that
    calls `primary_signal` in the tests has a trader.

    The recovery half of the order path, lifted out of `primary_signal` so a
    test can run it: nothing that calls `primary_signal` has a trader, so the
    size it sends and the mirror's recovery mark were untestable there.

    `count` arrives as the BASE - the tier, after the budget - and
    `base_count` goes back out unchanged, so the proposal can record what the
    base was and "was this the step" never has to be inferred later.

    RECOVERY WINS OVER THE BAND. A realised deficit is a fact about the
    account; the confidence band is an opinion about the setup. Checked
    second so its reason is the one reported when both apply.

    NOTHING HERE CAN CAUSE A TRADE. Every gate has already passed - this only
    decides the size of an order that is going out anyway, and a deficit is
    never a reason to enter.

    The state is read ONCE and carried to the step below, so the plan that is
    charged for this order is the plan its size was chosen from.
    """
    recovery = store.recovery_state(settings.recovery_steps)
    recovered, recovery_reason = recovery_size(
        store, settings, count, ask, state=recovery
    )
    if recovery_reason:
        count, size_reason = recovered, recovery_reason
    elif recovery.active:
        # Said out loud, because a silent non-upsize during recovery looks
        # exactly like recovery not working - which is how the last sizing
        # defect stayed invisible for a day.
        upsized = max(count, settings.high_confidence_contracts)
        print(
            f"auto: recovery holding at base - "
            f"{max_net_profit(upsized, ask):.2f} max net does "
            f"not cover {recovery.required_per_trade():.2f} of "
            f"{recovery.deficit:.2f} outstanding",
            flush=True,
        )
    # THE LOSS STEP, applied LAST so it is the single authority when it fires.
    # The operator's 2026-09-24 rule sizes on the previous market's result,
    # not on the deficit, and it was measured as the whole position - so when
    # it applies it REPLACES the count rather than adding to it, and the
    # conditional add-on stands down for this position. Two rules that each
    # looked bounded is exactly how a cap gets exceeded by their sum.
    last_lost = store.last_market_lost()
    base_count = count
    stepped, step_reason = loss_step_size(
        store, settings, count, ask, lost=last_lost
    )
    # THE STEP NO LONGER UPSIZES (operator, 2026-09-27: "replace the single
    # recover into a Combo with same base size, no more up scaling"). It still
    # decides WHEN a recovery is due - armed, in band, unspent, not back to
    # back - and says so; the order path then places that recovery as a combo
    # at this same base count (`combo_recovery`). The count never moves here.
    combo_due = bool(step_reason) and stepped > base_count
    # NO PARTNER, NO RECOVERY TO LABEL (BTC since 2026-09-28, gold always:
    # "BTC AND GOLD ONLY"). Every entry is an ordinary base-size entry, so none
    # is labelled "recovery due / waiting / taken" - "due", never spent because
    # no combo row is ever written, repeated on every in-band entry for five
    # markets and re-armed after a loss, reading like the back-to-back the
    # operator removed. Nothing about the count changes; it never did.
    if not combo_recovery.PARTNERS.get(surface.asset(settings.kalshi_series)):
        combo_due = False
        step_reason = ""
    if combo_due:
        size_reason = (f"recovery due at {ask:.2f} - combo at base size "
                       f"{base_count}")
        combo_due = bool(settings.recovery_combo_enabled)
    elif step_reason:
        size_reason = step_reason
    # `is_recovery` - the mark that made the wife's mirror size its own $2
    # recovery - is always False now: nothing is upsized. The combo reaches
    # her through its own dispatch.
    return (count, size_reason, base_count, False, recovery_reason, combo_due)


async def place_combo_recovery(
    store: Store, settings: Settings, telegram, trader, contract, side: str,
    ask: float, base_count: int, opened: int, remaining: int, now_ms: int,
) -> tuple[bool, str]:
    """Place the due recovery as a combo at BASE size. Returns (stop, note).

    stop=True  - nothing else goes out THIS poll: the combo was bought or may
                 have been, or a quote was asked for and refused - which took
                 up to half a minute, so the single-leg entry must be decided
                 again on the next poll with fresh gates and a fresh ask, not
                 sent now on a stale one crossing to the ceiling.
    stop=False - nothing was asked (no partner, market not created): the entry
                 goes out now, single-leg at base size. `note` says why, for
                 its fill message.

    Recorded as a `combo_recovery` proposal on the TRIGGER'S window, created
    BEFORE any money can move so a crash mid-buy still leaves a row:
      * bought  -> 'filled': a held position until the window closes, so the
                   one-position guard blocks anything on top of it, and it
                   spends the loss step (`Store.upsized_since`);
      * unknown -> 'unprotected' at the quoted price - treated as HELD, the
                   conservative reading of an accept whose answer was lost;
      * none    -> 'unfilled': spends nothing, and the single-leg base entry
                   goes out in its place.
    The operator's rule is in `combo_recovery`'s docstring and FINDINGS 105.
    """
    instrument = surface.asset(settings.kalshi_series)
    if not combo_recovery.PARTNERS.get(instrument):
        # No partner by the operator's rule (BTC since 09-28, gold always):
        # nothing is asked of anyone, the entry goes out at base size.
        return False, "recovery due - no combo partner; single-leg at base size"
    partners = "/".join(combo_recovery.PARTNERS.get(instrument, ()))
    root = Path(settings.database_path).resolve().parent
    partner = combo_recovery.partner_now(
        root, instrument, opened, now_ms,
        (settings.combo_partner_band_lo, settings.combo_partner_band_hi),
        settings.combo_partner_stale_s,
    )
    band = (settings.combo_partner_band_lo, settings.combo_partner_band_hi)
    client = getattr(trader, "_primary", trader)
    if partner is not None:
        partner = await combo_recovery.refresh(client, partner, band)
    if partner is None:
        note = (f"recovery due - no {partners} partner priced "
                f"{band[0]:.2f}-{band[1]:.2f} now; single-leg at base size")
        print(f"recovery combo: {note}", flush=True)
        return False, note
    legs = [combo_recovery.Leg(instrument, contract.ticker, side, ask, now_ms),
            partner]
    try:
        market = await combo_recovery.create_market(client, legs)
    except Exception as exc:  # noqa: BLE001 - no money has moved
        note = "recovery due - combo market not created; single-leg at base size"
        print(f"recovery combo: {note} ({exc})", flush=True)
        return False, note
    product = round(ask * partner.ask, 4)
    # The most it will pay: a combo is never worth more than its cheaper leg.
    cap = round(min(ask, partner.ask) * settings.combo_max_price_ratio, 4)
    expires_at = min(now_ms + settings.proposal_seconds * 1000,
                     contract.close_ms - 10_000)
    proposal = store.create_proposal(
        "combo_recovery", opened, market, side, cap, 0, base_count,
        expires_at, contract.close_ms, now_ms, base_count=base_count,
    )
    claimed = store.claim_proposal(proposal.id, now_ms)
    if claimed is None:
        return False, "recovery due - combo not claimed; single-leg at base size"
    print(f"recovery combo: {instrument} {side} @{ask:.2f} + {partner.asset} "
          f"{partner.side} @{partner.ask:.2f} -> {market}, {base_count} at or "
          f"below {cap:.4f} (legs multiplied {product:.4f}); asking for a "
          f"quote", flush=True)
    result = await combo_recovery.buy(
        client, legs, market, base_count,
        max_ratio=settings.combo_max_price_ratio,
        wait_s=settings.combo_quote_wait_s,
        fund=settings.combo_auto_fund,
    )
    note = combo_recovery.note_for(legs, result)
    if result.outcome == "none":
        store.finish_proposal(claimed.id, "unfilled", note)
        print(f"recovery combo not bought: {result.reason} - the entry is "
              f"decided again next poll, single-leg at base size", flush=True)
        return True, ""
    if result.bought:
        store.record_fill(claimed.id, opened, result.price, result.filled,
                          result.fee)
        store.finish_proposal(claimed.id, "filled", note,
                              entry_order_id=result.quote_id)
    else:
        store.record_fill(claimed.id, opened, result.price or cap,
                          base_count, 0.0)
        store.finish_proposal(claimed.id, "unprotected", note,
                              entry_order_id=result.quote_id)
    print(f"recovery combo {'BOUGHT' if result.bought else 'OUTCOME UNKNOWN'}: "
          f"{result.reason}", flush=True)
    # Only a confirmed fill is copied: the mirrors never act on "maybe".
    if result.bought and hasattr(trader, "dispatch_combo"):
        trader.dispatch_combo(legs, market, cap,
                              settings.combo_max_price_ratio,
                              settings.combo_quote_wait_s)
    try:
        notifier = Notifier(telegram, store, settings)
        await notifier.send_once(
            "combo", f"{opened}:{claimed.id}",
            messages.combo_recovery_message(
                legs=legs, market=market, result=result,
                contracts=result.filled if result.bought else base_count,
                remaining=remaining, snapshot=notifier.snapshot(now_ms),
            ),
            now_ms,
        )
    except Exception as exc:  # noqa: BLE001 - the money has already moved
        print(f"combo message failed: {exc!r}", flush=True)
    return True, ""


def partial_exit_pnl(
    *, paid: float, bid: float, filled: float, held: float,
    entry_fee: float | None, exit_fee: float | None,
) -> float:
    """Realised P&L for the portion actually sold, fees allocated to it.

    Selling one of two contracts realises one contract's gain and carries ONE
    contract's share of the entry fee. Charging the whole entry fee against
    the part sold overstates that cash flow and leaves the remaining contract
    owing nothing, so the settlement of the remainder is overstated in turn -
    and the deficit, which is folded from these amounts in order, inherits
    both errors.

    `held` is the size the entry fee was charged on. A full exit allocates all
    of it, which is the previous behaviour and the common case.
    """
    share = (filled / held) if held else 1.0
    return (
        (bid - paid) * filled
        - (entry_fee or 0.0) * share
        - (exit_fee or 0.0)
    )


def confidence_size(
    settings: Settings, snapshot: MarketSnapshot, prediction, base: int
) -> tuple[int, str]:
    """(contracts, why) - size up only where the edge was measured to be larger.

    The distance gate is a floor, not a ranking: the edge peaks at 2-4x
    volatility and decays above it, because a strike far enough away to be
    safe is already priced for being safe. Measured over 3,841 deployed
    entries, momentum aligned: 2.0-4.0x returns +0.0359/contract against
    +0.0149 for all entries and +0.0157 for everything else.

    Momentum is required because it is the one condition that separates on its
    own: aligned measures +0.0197, against measures -0.0701 with an interval
    clear of zero.

    OFF SINCE 2026-09-22, by the operator's decision, and the measurement above
    is recorded rather than deleted because it is still what was measured. The
    band doubled exposure on KXBTC15M-26SEP221330-30 - "3.0x vol is inside the
    measured 2-4x edge band", 2 contracts at 81c - and the market settled
    against us for -$1.64 where one contract would have been about -$0.82.
    Intelligence must not change size. Beyond the operator's rule, the evidence
    itself is now in question: `normalized_distance` here is computed from the
    Binance feed, and FINDINGS 41/43 measured that the Binance view disagrees
    with Kalshi's official BRTI reference on about 20% of markets. Sizing
    returns to one contract until it has independent evidence.

    The gate is a flag and not a deletion so that re-enabling it is a
    deliberate act with a number behind it. It returns `(base, "")` when off:
    an empty reason, so no size line appears claiming a band nothing acted on.
    """
    if not settings.confidence_sizing_enabled:
        return base, ""
    normalized = prediction.distance_bps / max(snapshot.volatility_5m_bps, 1.0)
    direction = 1 if prediction.side == "UP" else -1
    aligned = direction * snapshot.momentum_5m_bps > 0
    in_band = (
        settings.high_confidence_distance_min
        <= normalized
        < settings.high_confidence_distance_max
    )
    if in_band and aligned:
        return max(base, settings.high_confidence_contracts), (
            f"{normalized:.1f}x vol is inside the measured "
            f"{settings.high_confidence_distance_min:.0f}-"
            f"{settings.high_confidence_distance_max:.0f}x edge band"
        )
    if not aligned:
        return base, "momentum is not aligned"
    return base, f"{normalized:.1f}x vol is outside the 2-4x edge band"


def entry_context(
    settings: Settings,
    snapshot: MarketSnapshot,
    prediction,
    ask: float,
    priced_edge: float | None,
    settled_s: float,
    opened: int,
    rule_match: bool = False,
    blocking_level: float | None = None,
    model_ok: bool = False,
) -> list[tuple[str, str]]:
    """The numbers the Checks block does not carry, in the order they matter.

    Settle first, because it is the gate that actually decides: on 2026-09-21
    it refused ten of thirteen qualifying windows and its countdown appeared in
    no message. Edge second, because "the rule qualifies" says nothing about
    whether the price is worth paying. The rest is the context needed to judge
    an override by hand.
    """
    rows: list[tuple[str, str]] = []
    normalized = prediction.distance_bps / max(snapshot.volatility_5m_bps, 1.0)
    direction = 1 if prediction.side == "UP" else -1
    momentum_aligned = direction * snapshot.momentum_5m_bps > 0
    need = settings.entry_band_settle_s
    rows.append((
        "band settle",
        f"held {settled_s:.0f}s of {need}s"
        + ("  READY" if settled_s >= need else "  waiting"),
    ))
    if priced_edge is not None:
        fee = kalshi_fee_charged(ask, 1)
        net = priced_edge - fee
        rows.append((
            "measured edge",
            f"{net:+.4f}/ct after a {fee:.4f} fee"
            + ("" if net > 0.005 else "  (thin)"),
        ))
    else:
        rows.append(("measured edge", "unknown - no study at this price"))
    rows.append((
        "distance",
        f"${abs(snapshot.price - snapshot.target):,.0f} from target",
    ))
    rows.append((
        "volatility",
        f"{snapshot.volatility_5m_bps:.1f} bps / 5m",
    ))
    rows.append(("spread", f"{snapshot.spread_bps:.1f} bps"))
    # Session AND the hour. Regime is archived on every observation
    # (`session`, `hour_utc`, `weekday`, `vol_regime`) and registered in
    # `scripts/forward_test.py`, but it does NOT gate: measured over 71 days,
    # 17-20 UTC against every other hour is +0.0010/ct [-0.0272, +0.0305],
    # p=0.542, and section 4 found every session interval overlapping every
    # other. Shown so a live pattern can be seen and checked against the
    # record, not so it can be traded on a hunch.
    hour = datetime.fromtimestamp(opened / 1000, UTC).hour
    rows.append(("session", f"{_session(opened)} · {hour:02d}:00 UTC"))
    weight = weight_at(opened)
    rows.append(("regime weight", weight.describe()))

    # Confidence as arithmetic: base, then each contributor, then the result.
    # Neither the clock nor the level may gate - both were tried as gates and
    # both were wrong - so they appear here, priced in points, where their
    # size can be seen and argued with.
    signals = [
        bool(rule_match),
        2.0 <= normalized < 4.0,
        momentum_aligned,
        model_ok,
    ]
    base = regime_base_points(sum(signals))
    clock = regime_confidence_points(weight)
    level = level_points(blocking_level is not None)
    adjusted = max(0, min(100, base + clock + level))
    rows.append((
        "protective level",
        (f"${blocking_level:,.0f} shields the target"
         if blocking_level is not None
         else "nothing shielding the target") + f"  ({level:+d})",
    ))
    rows.append((
        "confidence",
        f"base {regime_label(base)} ({sum(signals)}/4) \u00b7 clock {clock:+d} "
        f"\u00b7 shield {level:+d} \u00b7 adjusted {regime_label(adjusted)}",
    ))
    return rows


# Where the poll cycle spends its time. `now_ms` is stamped at the top of the
# loop and an order goes out much later, and the gap between them - measured at
# ~1,930-2,180ms against a 200ms round trip to Kalshi - is the single largest
# known cause of missed fills. Moving the hourly recorder off the path did not
# shift it, so this stops guessing and records the breakdown on the execution
# row itself.
#
# A dict of integers touched a handful of times per poll. It cannot fail and it
# cannot slow anything down, which is the only acceptable cost for something
# sitting this close to an order.
POLL_MARKS: dict[str, int] = {}
# Last successful settlement mirror, so the sync is throttled to once a
# minute rather than running on every poll.
SETTLEMENT_SYNC: dict[str, int] = {}
# When the exchange last had no market, and whether that has been reported.
# A gap of seconds is the normal shape of a window boundary; a gap of hours is
# an outage whose only symptom is silence.
MARKET_GAP: dict[str, int] = {}
# The venue's own schedule, cached. Asking Kalshi every poll would replace the
# requests the closure is meant to save.
VENUE_SCHEDULE: dict[str, int] = {}


async def _venue_closed_until(kalshi, settings, now_ms: int,
                              gap_s: float) -> int | None:
    """When the series reopens, if it is CLOSED rather than merely between
    windows. None means "not closed, or cannot be shown to be".

    GOLD AND SILVER KEEP NEW YORK HOURS - closed at the New York close, open at
    the New York open, so shut every weekend for about two days. Polling every
    ten seconds through that is ~19,000 requests recording nothing, because the
    underlying metal is not trading either.

    FOUR CONDITIONS, because the failure to avoid is calling an OUTAGE a
    closure. On 2026-09-24 Kalshi listed nothing for two hours while healthy,
    and the operator's only symptom was Telegram going quiet:

      * the instrument DECLARES that it observes sessions. This is not
        inferable from the listing: on 2026-09-25 BTC and SOL both reported
        their next UNOPENED market 5.1 hours out while trading normally,
        because Kalshi creates markets in daily batches and the near-term ones
        were already OPEN, which an `unopened` listing excludes. Inferring a
        closure from that would back off a 24/7 instrument mid-outage and
        silence the alert built for it;
      * no open market for `venue_closed_after_s` - a window flip is seconds;
      * Kalshi lists a next market - unknown is not closed, it is unknown, and
        an unexplained silence must stay an outage;
      * that market opens at least `venue_closed_gap_s` away.

    A failed lookup returns None, which keeps the outage alert armed. The
    conservative direction is to stay noisy.
    """
    if not getattr(settings, "venue_has_sessions", False):
        return None
    if gap_s < settings.venue_closed_after_s:
        return None
    cached = VENUE_SCHEDULE.get("next_open_ms")
    asked = VENUE_SCHEDULE.get("asked_ms", 0)
    if cached and cached > now_ms:
        # A CLOSURE ENDS WHEN THE MARKET OPENS, NOT 30 MINUTES BEFORE.
        # `venue_closed_gap_s` decides whether a gap IS a closure; applying it
        # again here made the closure lapse in its final half hour, and the
        # outage path below would then fire "NO MARKET AT THE EXCHANGE"
        # reporting the whole 48-hour weekend as a fault - a false alarm every
        # Sunday, which is precisely the alert-fatigue this was built to avoid.
        # Once a reopen time is known, the instrument stays closed until it.
        return cached
    if now_ms - asked < settings.venue_closed_poll_seconds * 1000:
        return None
    VENUE_SCHEDULE["asked_ms"] = now_ms
    try:
        nxt = await kalshi.next_open_ms(now_ms)
    except Exception as exc:  # noqa: BLE001 - unknown is not closed
        print(f"next-open lookup failed: {exc!r}", flush=True)
        return None
    if not nxt:
        return None
    VENUE_SCHEDULE["next_open_ms"] = nxt
    if nxt - now_ms < settings.venue_closed_gap_s * 1000:
        return None
    return nxt


# Which window we have already asked the broker to confirm an entry fill for.
# One request per window, and only while the sweep has not delivered it.
ENTRY_CONFIRM: dict[str, int] = {}
# Which window we have already announced the add-on standing down for, because
# the loss step owns the size. Once per window: a line on every poll is how a
# line that matters stops being read.
STEP_STAND_DOWN: dict[str, int] = {}


def mark(name: str) -> None:
    POLL_MARKS[name] = int(time.time() * 1000)


def timing_breakdown() -> str:
    """Milliseconds per phase since the poll began, as JSON. Never raises."""
    try:
        start = POLL_MARKS.get("poll")
        if start is None:
            return ""
        ordered = sorted(POLL_MARKS.items(), key=lambda kv: kv[1])
        out, previous = {}, start
        for name, stamp in ordered:
            if name == "poll":
                continue
            out[name] = stamp - previous
            previous = stamp
        out["_total"] = previous - start
        return json.dumps(out)
    except Exception:  # noqa: BLE001 - instrumentation is never fatal
        return ""


def strategy_version(settings: Settings) -> str:
    """A short digest of the rule in force, so a record says which rule made it.

    Without it the archive mixes decisions taken under different rules and a
    later comparison silently averages across them - which on 2026-09-21 alone
    would have pooled four different bands and two settle timers.
    """
    try:
        rule = Path(settings.strategy_path).read_text(encoding="utf-8")
    except OSError:
        rule = "?"
    payload = (
        rule
        + f"|settle={settings.entry_band_settle_s}"
        + f"|window={settings.entry_to_seconds}-{settings.entry_from_seconds}"
        + f"|slip={settings.entry_slippage}"
    )
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:10]


def decision_record(
    store: Store,
    settings: Settings,
    claimed,
    contract: KalshiMarket,
    snapshot: MarketSnapshot,
    prediction,
    ask: float,
    opened: int,
    remaining: int,
    now_ms: int,
    settled_s: float,
    blocking_level: float | None,
    paid: float,
    fee: float | None,
    action: str = "ENTERED",
    blocked_reason: str | None = None,
    brti_features=None,
) -> list[tuple[str, str]]:
    """Everything that supported this order, recorded AND returned for display.

    Afterwards these facts sit in four different tables joined on timestamps
    that drift, so "why was this trade taken" was a reconstruction rather than
    a record. Written once, at the moment the money moved.

    Never raises: this runs immediately after a fill, and no bookkeeping may
    propagate into the path that just spent money.
    """
    rows: list[tuple[str, str]] = []
    try:
        rule = EntryRule.load(settings.strategy_path)
        hour = datetime.fromtimestamp(opened / 1000, UTC).hour
        weight = weight_for_hour(hour)
        edge = measured_edge_at(ask)
        fee_charged = fee if fee is not None else kalshi_fee_charged(paid, 1)
        net = (edge - fee_charged) if edge is not None else None
        distance = abs(snapshot.price - snapshot.target)
        normalized = prediction.distance_bps / max(snapshot.volatility_5m_bps, 1.0)
        # The Binance rule's `check_detail` compares `prediction.
        # raw_probability >= min_raw_probability`, and that is None on the
        # Kalshi path - no Kalshi-native model exists. It raised a TypeError
        # that `decision_record` caught and logged, so every decision record
        # was silently lost while the signal itself looked healthy.
        if settings.kalshi_only:
            # `(name, passed, detail)` triples, the same shape `check_detail`
            # returns, because the consumer unpacks three.
            gates = [
                (fact["name"], fact["passed"],
                 fact["pass_text"] if fact["passed"] else fact["fail_text"])
                for fact in kalshi_signal.evaluate(
                    KalshiBRTIRule.load(settings.kalshi_strategy_path),
                    brti_features, ask, remaining,
                )[1]
            ] if brti_features is not None else []
        else:
            gates = rule.check_detail(
                prediction, snapshot, ask,
                blocking_level=blocking_level, levels_ready=True,
            )
        read = None
        if COHORTS.ok:
            read = COHORTS.read(
                Fingerprint(
                    remaining_s=remaining, ask=ask,
                    normalized_distance=normalized,
                    volatility_bps=snapshot.volatility_5m_bps,
                    momentum_bps=snapshot.momentum_5m_bps,
                    session=_session(opened),
                    vol_regime=(
                        "low" if snapshot.volatility_5m_bps < 8
                        else "high" if snapshot.volatility_5m_bps > 20 else "mid"
                    ),
                    side_is_up=prediction.side == "UP",
                ),
                fill_rate=store.execution_report().get("fill_rate"),
                as_of_ms=now_ms,
            )

        rows.append(("gates", ", ".join(f"{name} {value}" for name, ok, value
                                        in gates if ok)))
        rows.append(("band settle", f"held {settled_s:.0f}s of "
                                    f"{settings.entry_band_settle_s}s"))
        rows.append((
            "measured edge",
            f"{net:+.4f}/ct after a {fee_charged:.4f} fee"
            if net is not None else "unknown at this price",
        ))
        rows.append(("distance", f"${distance:,.0f} = {normalized:.1f}x vol"))
        rows.append(("volatility", f"{snapshot.volatility_5m_bps:.1f} bps / 5m"))
        rows.append(("momentum", f"{snapshot.momentum_5m_bps:+.1f} bps"))
        rows.append(("spread", f"{snapshot.spread_bps:.1f} bps"))
        if blocking_level is not None:
            rows.append(("protective level", f"${blocking_level:,.0f}"))
        rows.append((
            "regime",
            f"{_session(opened)} {hour:02d}:00 UTC \u00b7 confidence "
            f"{regime_confidence_points(weight):+d}",
        ))
        rows.append(("fill", f"paid {paid:.2f} against a {ask:.2f} ask"))
        if read is not None:
            rows.append((
                "similar markets",
                f"{read.n} comparable \u00b7 p(win) "
                f"{read.win_probability:.0%} \u00b7 says {read.action}",
            ))
            rows.append(("similar says", read.reason))

        store.record_decision_record({
            "observation_id": f"{opened}:{remaining}",
            "signal_id": signal_id_for(contract.ticker),
            "market_id": contract.ticker,
            "proposal_id": claimed.id if claimed is not None else None,
            "strategy_version": strategy_version(settings),
            "action": action,
            "blocked_reason": blocked_reason,
            "level_adjustment": level_points(blocking_level is not None),
            "cohort_win_low": read.win_low if read else None,
            "cohort_win_high": read.win_high if read else None,
            "window_open": opened,
            "created_at": now_ms, "ticker": contract.ticker,
            "side": prediction.side, "remaining_s": remaining, "ask": ask,
            "limit_submitted": (
                min(claimed.entry_limit + settings.entry_slippage, 0.99)
                if claimed is not None else None
            ),
            "count": claimed.count if claimed is not None else None,
            "gates": "; ".join(f"{name}={value}" for name, _ok, value in gates),
            "settled_s": settled_s, "measured_edge": edge, "fee": fee_charged,
            "net_edge": net, "distance_dollars": distance,
            "normalized_distance": normalized,
            "volatility_bps": snapshot.volatility_5m_bps,
            "momentum_bps": snapshot.momentum_5m_bps,
            "spread_bps": snapshot.spread_bps,
            "session": _session(opened), "hour_utc": hour,
            "vol_regime": (
                "low" if snapshot.volatility_5m_bps < 8
                else "high" if snapshot.volatility_5m_bps > 20 else "mid"
            ),
            "regime_weight": weight.weight,
            "confidence_adjustment": regime_confidence_points(weight),
            "protective_level": blocking_level,
            "cohort_n": read.n if read else None,
            "cohort_win_probability": read.win_probability if read else None,
            "cohort_action": read.action if read else None,
            "cohort_reason": read.reason if read else None,
            "fill_price": paid if claimed is not None else None,
            "filled": 1 if claimed is not None else 0, "won": None,
        })
    except Exception as exc:  # noqa: BLE001 - bookkeeping after money moved
        print(f"decision record failed {type(exc).__name__}: {exc}", flush=True)
    return rows


def shadow_read(
    store: Store,
    settings: Settings,
    contract: KalshiMarket,
    snapshot: MarketSnapshot,
    prediction,
    ask: float,
    opened: int,
    remaining: int,
    now_ms: int,
    rule_match: bool,
    traded: bool = False,
) -> str:
    """Retrieve comparable markets and compare the actions. SHADOW ONLY.

    Called from two places, and both of them are after the trading decision
    they describe: where the entry alert is built, and immediately after an
    order has gone to the exchange - so a 95ms corpus query can never delay a
    fill. The second call site exists because this only ever ran with the
    alert, while the auto path returns before the alert is built: every window
    the bot actually TRADED was therefore absent from `shadow_decisions`, and
    the head-to-head in scripts/score_shadow.py had nothing to compare.

    `rule_match` and `traded` are recorded as of THIS call, which is what makes
    the row honest - `rule_qualified` is the rule's verdict at this poll, not
    at the alert poll, and the rule can flip between the two.

    Returns the message text, or "" when the corpus is missing or the cohort is
    too thin to have an opinion. It NEVER trades, gates, or changes a size.
    """
    if not COHORTS.ok:
        return ""
    try:
        fingerprint = Fingerprint(
            remaining_s=remaining,
            ask=ask,
            normalized_distance=(
                prediction.distance_bps / max(snapshot.volatility_5m_bps, 1.0)
            ),
            volatility_bps=snapshot.volatility_5m_bps,
            momentum_bps=snapshot.momentum_5m_bps,
            session=_session(opened),
            vol_regime=(
                "low" if snapshot.volatility_5m_bps < 8
                else "high" if snapshot.volatility_5m_bps > 20 else "mid"
            ),
            side_is_up=prediction.side == "UP",
        )
        fills = store.execution_report()
        # as_of_ms enforces walk-forward: only markets that had already
        # settled may inform this decision.
        read = COHORTS.read(
            fingerprint, fill_rate=fills.get("fill_rate"), as_of_ms=now_ms
        )
        if read is None:
            return ""
        store.record_shadow_decision({
            "window_open": opened, "created_at": now_ms,
            "ticker": contract.ticker, "side": prediction.side,
            "remaining_s": remaining, "ask": ask, "cohort_n": read.n,
            "win_probability": read.win_probability,
            "raw_win_rate": read.raw_win_rate,
            "net_edge_now": read.net_edge_now,
            "enter_now_net": read.enter_now_net,
            "wait_limit_net": read.wait_limit_net,
            "wait_real_net": read.wait_real_net,
            "dip_price": read.dip_price, "dip_rate": read.dip_rate,
            "dip_n": read.dip_n, "win_low": read.win_low,
            "win_high": read.win_high, "edge_low": read.edge_low,
            "prior": read.prior, "wait_limit_low": read.wait_limit_low,
            "mean_drift": read.mean_drift,
            "ran_away_rate": read.ran_away_rate,
            "session": fingerprint.session, "vol_regime": fingerprint.vol_regime,
            "action": read.action, "reason": read.reason,
            # Both as of this call. `traded` was hard-coded 0 here and
            # updated nowhere. `record_fill` now stamps the whole window when
            # the fill is read back; this covers the order's own row even when
            # that read-back fails and never happens.
            "rule_qualified": int(rule_match), "traded": int(traded),
            "won": None,
        })
        return read.as_message()
    except Exception as exc:  # noqa: BLE001 - shadow work is never fatal
        print(f"shadow read failed {type(exc).__name__}: {exc}", flush=True)
        return ""


# One policy object per process, loaded once. Reloaded only by a restart, so
# candidate training can never change what is running.
_POLICY: dict = {"loaded": None}
_CANDIDATES: dict = {"loaded": None}
# The running learner, so `/learning` can report the live loop rather than
# constructing a second one that shares none of its state.
LEARNING: dict = {"runner": None}
# Last window we complained about a missing BRTI context, so the log
# carries one line a window rather than one a poll.
BRTI_CONTEXT_GAP: dict = {"window": None}
# Last (window, reason) we logged a missing Kalshi input for, so a feed outage
# prints once per cause per window rather than once per poll.
KALSHI_GAP: dict = {"window": None}


def kalshi_snapshot(features, contract, opened: int, now_ms: int):
    """A `MarketSnapshot` whose every number comes from Kalshi.

    Same shape as the Binance one so the archive, the messages and the order
    path are unchanged - but price and target are the official reference and
    the strike, momentum and volatility are BRTI's, and the spread is the
    Kalshi book's.

    The three Binance-only microstructure fields are ZERO and nothing gates on
    them: `predict()` does not run on this path (see `kalshi_signal`), and the
    BRTI rule never referenced them. They stay on the dataclass rather than
    being removed so the historical archive keeps one schema, and zero is
    honest here - it means "this instrument does not publish it", which is
    why the value is never read rather than merely never gated.
    """
    from .snapshot import MarketSnapshot

    yes_bid = getattr(contract, "yes_bid", 0.0) or 0.0
    yes_ask = getattr(contract, "yes_ask", 0.0) or 0.0
    mid = (yes_bid + yes_ask) / 2 if yes_bid and yes_ask else 0.0
    spread_bps = ((yes_ask - yes_bid) / mid * 10_000) if mid else 0.0
    return MarketSnapshot(
        price=features.value,
        target=features.target,
        bid_imbalance=0.0,
        taker_imbalance=0.0,
        momentum_5m_bps=features.brti_momentum_bps,
        volatility_5m_bps=features.brti_volatility_bps,
        futures_basis_bps=0.0,
        spread_bps=spread_bps,
        window_high=0.0,
        window_low=0.0,
        elapsed_minutes=max(0, (now_ms - opened) // 60_000),
    )



def _scoped_open(per_ticker: dict | None, owned=()) -> tuple[int, float]:
    """(count, mark) over the series this instance trades, and nothing else.

    `open_mark` reports the account. Everything that presents a figure under an
    instrument's name has to narrow it, or it reports the operator's own
    trading as the strategy's - which the money footer did, preflight did, and
    the open-position line did.

    `surface.asset` returns "" for a ticker no instrument here recognises, so
    an unknown series is excluded rather than silently counted.
    """
    # `owned`: the recovery combos this system bought. Their KXMVE... tickers
    # name no instrument, so they are counted by provenance instead - while
    # the operator's own combos in the same account stay out.
    owned = set(owned or ())
    mine = {t: v for t, v in (per_ticker or {}).items()
            if surface.asset(t) or t in owned}
    return len(mine), round(sum(mine.values()), 6)


def active_policy(settings) -> intel.Policy:
    if _POLICY["loaded"] is None:
        _POLICY["loaded"] = intel.Policy.load(settings.intelligence_policy_path)
        pol = _POLICY["loaded"]
        print(
            f"intelligence policy: version={pol.version} "
            f"model={pol.model_version} arms={len(pol.arms)} "
            f"vetoes={pol.vetoes_enabled} admissions={pol.admissions_enabled}",
            flush=True,
        )
        # WHETHER IT CAN ACT, not merely whether it loaded. The operator's
        # instruction is that this layer is never off; the health state is what
        # makes that checkable, and it is published to the message surface here
        # so every alert carries the truth as of the policy in force.
        state = intel.health(pol, settings, int(time.time() * 1000))
        surface.set_intelligence_state(state)
        print(
            f"intelligence health: ok={state['ok']} mode={state['mode']} "
            f"authorised={state['authorised']} acting_arms="
            f"{state['acting_arms']}/{state['arms']} · {state['reason']}",
            flush=True,
        )
    return _POLICY["loaded"]


def reload_policy(settings=None) -> None:
    """Drop the cached policy so the next decision reads the new artefact.

    The cache exists so a decision does not hit the disk, and it is correct for
    a policy that only ever changes between processes - which stopped being
    true the moment training moved inside the service. A runner that writes a
    new artefact and leaves the process deciding from the old one in memory has
    trained nothing anybody can observe.
    """
    _POLICY["loaded"] = None
    # THE CANDIDATES RELOAD WITH IT. They were frozen for the life of the
    # process because a candidate refitted between making a prediction and its
    # grading would make the record meaningless - which was right while the
    # ids were positional (`c01` meant a different cell after every refit, and
    # `candidate_evaluations` is keyed on the id). The ids are now derived from
    # the context and every row carries the version that wrote it, so a cell
    # keeps its name and an old prediction stays attributable. Holding the old
    # set instead would freeze the forward evaluation on whatever the first run
    # happened to find.
    _CANDIDATES["loaded"] = None
    if settings is not None:
        pol = active_policy(settings)
        print(
            f"intelligence policy reloaded: version={pol.version} "
            f"arms={len(pol.arms)} vetoes={pol.vetoes_enabled} "
            f"admissions={pol.admissions_enabled}",
            flush=True,
        )


def active_candidates(settings) -> CandidateSet:
    """Loaded once per process. A candidate that changed between making a
    prediction and its grading would make the record meaningless, so the
    artefact is frozen for the life of the run."""
    if _CANDIDATES["loaded"] is None:
        _CANDIDATES["loaded"] = CandidateSet.load(
            settings.intelligence_candidates_path
        )
        cs = _CANDIDATES["loaded"]
        print(
            f"intelligence candidates: version={cs.version} "
            f"features={cs.feature_version} n={len(cs.candidates)} "
            f"(forward evaluation only - they control nothing)",
            flush=True,
        )
    return _CANDIDATES["loaded"]


def brti_context_row(snapshot, ask, opened, brti,
                     side: str = "UP") -> tuple[dict | None, str]:
    """The BRTI context row for this decision, or (None, why-not).

    `brti` is the LAST reference poll's features. The reference deliberately
    polls behind the trading path - a slow feed must never delay a fill - so
    these are one poll old, and two things have to be checked before they can
    label a cell:

      * the features must not be stale, and
      * they must belong to THIS market. `target` is the window's strike, so
        a mismatch means the window rolled between the reference poll and
        this decision and the numbers describe the market before it.

    When either fails, this returns None and the caller records no context
    rather than falling back to the Binance-scale numbers. That fallback is
    the trap: the key would still format, it would just name a pocket nothing
    was ever trained on, and the table would look healthy while measuring
    noise. A missing row is visible; a mislabelled one is not.
    """
    if brti is None:
        return None, "no brti features"
    if getattr(brti, "stale", False):
        return None, "brti stale"
    target = getattr(snapshot, "target", None)
    if not target or not getattr(brti, "target", None):
        return None, "no strike"
    if abs(brti.target - target) > 1e-6:
        return None, "brti belongs to another window"
    # THE SETUP, plus the context recorded beside it. `side` signs the
    # momentum, because the gate is applied to the aligned value and an UP and
    # a DOWN setup with identical raw momentum are opposite setups.
    direction = 1 if side == "UP" else -1
    return {
        "brti_normalized_distance": brti.brti_normalized_distance,
        "brti_momentum_bps": brti.brti_momentum_bps,
        "brti_aligned_momentum_bps": direction * brti.brti_momentum_bps,
        "brti_volatility_bps": brti.brti_volatility_bps,
        "our_ask": ask,
        # Context: recorded on every decision, never part of the key.
        "session": _session(opened),
        "vol_regime": brti_vol_regime(brti.brti_volatility_bps),
    }, ""


def _feature(brti, name: str):
    """One recorded BRTI quantity, or None when no reference was in hand."""
    return None if brti is None else getattr(brti, name, None)


def normalise_gates(failed_checks) -> tuple[str, ...]:
    """The failing gate NAMES, whatever shape the caller had them in.

    Both live callers hand this a comma-joined STRING - `", ".join(...)` on the
    Kalshi path, and `rule.matches` returns one on the legacy path. The
    previous expression tested `isinstance(failed_checks, list)`, which a
    string is not, and fell through to `tuple(str(x) for x in failed_checks)`:
    iterating a string yields its CHARACTERS, so "BRTI distance" was stored as
    thirteen separate gates, `B, R, T, I, ...`.

    Nothing raised. The archive simply filled with per-character gate names,
    and an admission - which may only rescue a setup whose failing gates are
    exactly the one it names - could never match, so that path was dead by
    typo rather than by decision.
    """
    if not failed_checks:
        return ()
    if isinstance(failed_checks, str):
        return tuple(part.strip() for part in failed_checks.split(",")
                     if part.strip())
    if isinstance(failed_checks, dict):
        failed_checks = [failed_checks]
    out = []
    for item in failed_checks:
        if isinstance(item, dict):
            name = item.get("name")
            if name:
                out.append(str(name))
        elif item is not None:
            out.append(str(item))
    return tuple(out)


def intelligence_verdict(
    settings, store, prediction, snapshot, ask, rule_match, failed_checks,
    opened, remaining, now_ms, brti=None, ticker=None, model_points=None,
    band_hold_s=None,
):
    """Ask the shared decision function, record the answer, return it.

    Never raises: a failure here falls back to the base strategy explicitly,
    because a layer that can stop trading by breaking is worse than one that
    is switched off.

    `ticker` IS THE CONTRACT'S, NOT THE SNAPSHOT'S. This used to read
    `getattr(snapshot, "ticker", None)` - and `MarketSnapshot` has no `ticker`,
    so every one of the first 1,174 rows stored NULL. Nothing broke: the column
    was simply always empty, which meant the broker's fills and fees could
    never be joined to the decision that caused them, and the learning loop saw
    every executed trade as a simulated one. A field that is silently always
    None is the same failure as a number under the wrong name.
    """
    try:
        policy = active_policy(settings)
        features_ok = (
            snapshot is not None
            and getattr(snapshot, "volatility_5m_bps", None) is not None
        )
        # THE LIVE SNAPSHOT IS NOT THE HISTORICAL ONE. `MarketSnapshot` has no
        # `session` or `vol_regime` - those live on the backtest `Snapshot` -
        # so reading them off it yields "?" for both, and a context key of
        # "? . ? . ..." can never match anything the policy was trained on.
        # The first live decision showed exactly that. Derive them the same
        # way the corpus does, from the same functions, or replay and live are
        # not speaking about the same cells.
        from .features import _session
        vol = getattr(snapshot, "volatility_5m_bps", None) or 0.0
        row = {
            "session": _session(opened),
            "vol_regime": ("high" if vol >= 12 else "low" if vol < 5 else "mid"),
            "normalized_distance": getattr(prediction, "distance_bps", 0.0)
            / max(getattr(snapshot, "volatility_5m_bps", 1.0) or 1.0, 1.0),
            "our_ask": ask,
        }
        # ONE CONTEXT, KEYED ON THE INSTRUMENT ACTUALLY IN USE.
        #
        # Under Kalshi-only there is one feature family, so the policy and
        # the candidates share `brti_context_of` and a mismatch is impossible
        # by construction rather than by convention. The Binance context
        # survives only for the legacy path, and the Binance-trained policy
        # is separately retired and cannot act whatever key it is handed.
        if settings.kalshi_only:
            brti_row, why_not = brti_context_row(
                snapshot, ask, opened, brti,
                getattr(prediction, "side", "UP"),
            )
            if brti_row is None:
                return intel.Verdict(
                    base_qualified=bool(rule_match), failed_gates=(),
                    final_action=intel.NEUTRAL,
                    reason=f"no {intel.FEATURE_VERSION} context ({why_not})",
                )
            context = setup_context_of(brti_row)
        else:
            context = context_of(row)
        key = f"{context}|{'accept' if rule_match else 'reject'}"
        gates = normalise_gates(failed_checks)
        verdict = intel.decide(
            context_key=key, base_qualified=bool(rule_match),
            failed_gates=gates, ask=ask, policy=policy,
            enabled=settings.intelligence_enabled, features_ok=features_ok,
            now_ms=now_ms, max_age_ms=settings.intelligence_max_policy_age_ms,
            # THE SESSION THIS DECISION IS BEING MADE IN. The cell is pooled
            # across sessions by design; this is what lets `decide` refuse to
            # spend another session's evidence here.
            session=_session(opened),
        )
        # THE SECOND GATE, and the one the operator holds. `decide` answered
        # "was this validated?"; this answers "am I allowed to?", from two
        # switches no code path can raise. Confidence, veto and admission are
        # asked for separately, because they are separate risks - a layer
        # trusted to re-rate a label is not thereby trusted to spend money on a
        # trade every deployed gate refused.
        mode, _mode_why = intel_mode.resolve(
            settings.intelligence_mode, settings.intelligence_authorised
        )
        verdict = intel.authorise(
            verdict,
            # CONFIDENCE RIDES ON `intelligence_enabled`, NOT ON THE MODE.
            #
            # The mode ladder governs the retrieval layer, which returns a
            # recommendation and needs authority before it is read as one. A
            # policy confidence delta is not a recommendation: it re-rates the
            # header on a decision the gates have already made, and it is
            # arithmetically incapable of admitting, refusing or resizing
            # anything. Requiring the execution switch for it would mean the
            # only way to see a calibration is to grant the power to trade on
            # one, which is precisely backwards.
            may_confidence=settings.intelligence_enabled,
            may_veto=intel_mode.may_veto(mode),
            may_admit=intel_mode.may_admit(mode),
        )
        store.record_intelligence({
            "window_open": opened,
            "ticker": ticker or getattr(snapshot, "ticker", None),
            # ONE OPPORTUNITY, derived from the ticker so a restart mid-window
            # does not split one path into two. Declared on this table and
            # never written - NULL on every row - which meant the many poll
            # rows for a single opportunity could only be grouped by window,
            # and a window that re-opened after a restart looked like one.
            "signal_id": (ticker or getattr(snapshot, "ticker", None) or
                          f"w{opened}"),
            "decided_ms": now_ms, "remaining_s": remaining,
            "side": getattr(prediction, "side", None), "ask": ask,
            "base_qualified": int(bool(rule_match)),
            "failed_gates": ", ".join(gates) or None,
            "final_action": verdict.final_action,
            "overrides_gate": verdict.overrides_gate,
            "reason": verdict.reason,
            "confidence_delta": verdict.confidence_delta,
            "calibrated_probability": verdict.calibrated_probability,
            "expected_net": verdict.expected_net,
            "evidence_n": verdict.evidence_n,
            "uncertainty": verdict.uncertainty,
            "context_key": verdict.context_key,
            "model_version": verdict.model_version,
            "policy_version": verdict.policy_version,
            "feature_version": verdict.feature_version,
            "training_cutoff_ms": verdict.training_cutoff_ms,
            "features_ok": int(features_ok),
            "evidence_action": verdict.evidence_action or verdict.final_action,
            "evidence_delta": verdict.evidence_delta,
            "authority": verdict.authority or None,
            # WHAT WAS GRANTED, beside what was withheld. `authority` above is
            # NULL whenever nothing was withheld, so on its own the archive
            # cannot distinguish "the evidence said neutral" from "the
            # permission was never given" - opposite facts that look identical.
            "authority_granted": ",".join(
                name for name, granted in (
                    ("confidence", settings.intelligence_enabled),
                    ("veto", intel_mode.may_veto(mode)),
                    ("admit", intel_mode.may_admit(mode)),
                ) if granted
            ) or "none",
            "model_points": model_points,
            # THE RAW FEATURES, so a future re-keying never orphans this row
            # the way `brti-1` orphaned every row written before `brti-2`.
            "brti_normalized_distance": _feature(brti, "brti_normalized_distance"),
            "brti_momentum_bps": _feature(brti, "brti_momentum_bps"),
            "brti_aligned_momentum_bps": (
                None if brti is None else
                (1 if getattr(prediction, "side", "UP") == "UP" else -1)
                * (getattr(brti, "brti_momentum_bps", 0.0) or 0.0)
            ),
            "brti_volatility_bps": _feature(brti, "brti_volatility_bps"),
            # THE TWO INDICATORS. Recorded on every decision, qualified or
            # not, so the reversal threshold can be re-measured against
            # outcomes rather than re-argued. One gates and one does not;
            # both are archived the same way.
            "brti_retrace": _feature(brti, "brti_retrace"),
            "brti_choppiness": _feature(brti, "brti_choppiness"),
            # THE LEVEL-HOLDING MEASURES. Recorded on every decision,
            # qualified or refused, because the refused ones are exactly the
            # counterfactual a threshold has to be judged on.
            "brti_rsi": _feature(brti, "brti_rsi"),
            "brti_accel": _feature(brti, "brti_accel"),
            "brti_held_s": _feature(brti, "brti_held_s"),
            "brti_rejections": _feature(brti, "brti_rejections"),
            "brti_momentum_45m_bps": _feature(brti, "brti_momentum_45m_bps"),
            "brti_volatility_45m_bps": _feature(
                brti, "brti_volatility_45m_bps"),
            # CONTEXT, recorded and not keyed on.
            "session": _session(opened),
            "vol_regime": (
                None if brti is None else
                brti_vol_regime(getattr(brti, "brti_volatility_bps", 0.0) or 0.0)
            ),
            "band_hold_s": band_hold_s,
        })
        # FORWARD EVALUATION, alongside. Every frozen candidate that speaks to
        # this context records what it WOULD have changed, beside what the
        # unchanged strategy actually decided. None of them can alter the
        # order; this is how one earns the right to, on data it was never
        # fitted to.
        try:
            candidates = active_candidates(settings)
            brti_row, why_not = brti_context_row(
                snapshot, ask, opened, brti,
                getattr(prediction, "side", "UP"),
            )
            if brti_row is None:
                # Say so once per window rather than per poll - and say it at
                # all. A forward evaluation that records nothing looks exactly
                # like one where no candidate had an opinion.
                if candidates.candidates and BRTI_CONTEXT_GAP["window"] != opened:
                    BRTI_CONTEXT_GAP["window"] = opened
                    print(
                        f"candidate evaluation skipped [{why_not}] - no "
                        f"{candidates.feature_version} context for this window",
                        flush=True,
                    )
                return verdict
            evaluations = candidates.evaluate(
                context_key=str(setup_context_of(brti_row)),
                qualified=bool(rule_match),
            )
            for item in evaluations:
                item.update({
                    "window_open": opened, "decided_ms": now_ms,
                    "ticker": ticker or getattr(snapshot, "ticker", None),
                    "side": getattr(prediction, "side", None), "ask": ask,
                    "remaining_s": remaining,
                })
            if evaluations:
                store.record_candidate_evaluations(evaluations)
        except Exception as exc:  # noqa: BLE001 - evaluation is never fatal
            print(f"candidate evaluation failed: {exc!r}", flush=True)
        return verdict
    except Exception as exc:  # noqa: BLE001 - never stop trading
        print(f"intelligence failed, falling back to strategy: {exc!r}", flush=True)
        return intel.Verdict(
            base_qualified=bool(rule_match), failed_gates=(),
            final_action=intel.NEUTRAL, reason="intelligence error; base strategy",
        )


async def primary_signal(
    settings: Settings,
    store: Store,
    telegram: Telegram,
    contract: KalshiMarket,
    snapshot: MarketSnapshot,
    opened: int,
    remaining: int,
    now_ms: int,
    trader: KalshiExecutionClient | None = None,
    levels: LevelTracker | None = None,
    capital=None,
    brti=None,
) -> None:
    rule = EntryRule.load(settings.strategy_path)
    # Read from the cache only. The tracker refreshes on its own slow clock
    # after the trading path, because levels need ~25h of bars and that second
    # Binance call on the order path is exactly the latency that cost three
    # fills on 2026-09-21 (FINDINGS section 22).
    pivot = levels.blocking(now_ms, snapshot.price, snapshot.target) if levels else None
    blocking_level = pivot.price if pivot else None
    levels_ready = bool(levels and levels.pivots)
    # Scan the whole window rather than a single minute: take the first minute
    # where the rule qualifies, and fall back to one paper summary at the end if
    # none ever does. Firing only at rule.remaining_minutes meant the service
    # never saw the minutes where the measured edge actually lives.
    if not settings.entry_to_seconds <= remaining <= settings.entry_from_seconds:
        return
    # The spread gate lives in `kalshi_signal.signal_inputs` on the Kalshi
    # path, measured in CENTS of the contract. `max_spread_bps` describes
    # Binance SPOT spread and applying it to a Kalshi book rejects every
    # signal - a 2c spread on a 79c mid is 253 bps against a threshold of 2.
    if not settings.kalshi_only and snapshot.spread_bps > settings.max_spread_bps:
        return
    if settings.kalshi_only:
        # THE ACTIVE RULE IS THE BRTI ONE. The deployed `EntryRule` gates on
        # `min_normalized_distance = 1.5`, measured against Binance RAW
        # volatility; BRTI reads 10-20 on the identical market, so reusing it
        # here would pass the distance gate on everything while still drawing
        # a tick beside it. Different quantity, different rule, and the floor
        # below is the measured 10x (FINDINGS 43).
        #
        # `predict()` does not run: three of its five terms are Binance-only
        # and the two that are not are on the wrong scale. There is no
        # Kalshi-native probability model, so none is reported - see
        # `kalshi_signal`.
        kalshi_rule = KalshiBRTIRule.load(settings.kalshi_strategy_path)
        prediction = kalshi_signal.prediction_from(brti)
        calibration = None
        contract_ask = contract.ask(prediction.side)
        rule_match, facts, failed_names = kalshi_signal.evaluate(
            kalshi_rule, brti, contract_ask, remaining,
        )
        rule_match = rule_match and kalshi_rule.enabled
        failed_checks = ", ".join(failed_names)
    else:
        prediction = predict(snapshot)
        calibration = store.calibration(prediction.bucket)
        contract_ask = contract.ask(prediction.side)
        rule_match, failed_checks = rule.matches(
            prediction, snapshot, contract_ask,
            blocking_level=blocking_level, levels_ready=levels_ready,
        )
    # THE INTELLIGENCE LAYER, on the real decision path. One shared function,
    # the same one historical replay calls, so an evaluation can never
    # describe behaviour the bot does not have.
    #
    # It is recorded whatever it says - including NEUTRAL, and including when
    # no order follows. The interesting cases for "did it help?" are exactly
    # the ones where nothing traded, so they cannot be reconstructed from the
    # orders afterwards.
    # NAMED `intel_verdict`, NOT `verdict`. There is already a local `verdict`
    # further down this function holding the auto-trade log string, and it is
    # assigned on several branches - so binding the intelligence Verdict to the
    # same name meant that by the time the alert was rendered it was sometimes
    # a `str`. The collision is invisible on the branches that do not reassign,
    # which is exactly the kind that survives a test run.
    intel_verdict = intelligence_verdict(
        settings, store, prediction, snapshot, contract_ask,
        rule_match, failed_checks, opened, remaining, now_ms, brti,
        ticker=getattr(contract, "ticker", None),
        # BAND-HOLD STATE, recorded as context. It is order-eligibility
        # rather than a qualification check - the price must have SETTLED in
        # the band, not merely touched it - and it is the condition most often
        # standing between a qualified signal and an order, so a decision row
        # that omits it cannot explain why nothing was bought. Read from the
        # archive, the same call the auto path makes further down.
        # The score the model produced for THIS decision, before the
        # layer touched it. This is the prediction a calibration compares
        # against the outcome.
        #
        # Only on the Kalshi path: `facts` is assembled there and not in the
        # legacy branch until much further down, and a score reconstructed
        # later is a score from a different moment. The learner excludes
        # non-BRTI rows anyway, so a None here costs nothing.
        model_points=(
            model_confidence_points(facts, opened, blocking_level)
            if settings.kalshi_only else None
        ),
        band_hold_s=(
            int(store.band_streak_seconds(
                opened, kalshi_rule.min_ask, kalshi_rule.max_ask, now_ms))
            if settings.kalshi_only else None
        ),
    )
    if intel_verdict.final_action == intel.VETO:
        rule_match = False
    elif intel_verdict.final_action == intel.ADMIT:
        rule_match = True
    qualified = rule.enabled and rule_match and settings.entry_alerts_enabled

    # Three states, not two. The rule's verdict informs the decision without
    # making it, but a setup with no measured edge in either direction is not a
    # judgement call - it is noise, and a button on it only invites a bad trade.
    # Such setups are still recorded: they are evidence even when untradeable.
    worth_offering = contract_ask >= settings.manual_min_ask
    offer_button = qualified or (settings.manual_execution_enabled and worth_offering)

    # Alert on the FIRST actionable minute, not only when automation would fire.
    # Gating on `qualified` meant that with `enabled: false` - which is the whole
    # point of the manual stage - nothing was ever actionable, so every alert
    # fell through to the last look at the bottom of the window. That is not the
    # strategy that was validated: the profit test enters at the first minute the
    # price is in band, and waiting until 5m30s both worsens the entry price and
    # skips the 6-11 minute range where the measured edge actually lives.
    last_look = remaining - settings.poll_seconds < settings.entry_to_seconds
    if not offer_button and not last_look:
        return
    # One alert per window, EXCEPT when a real order was placed and simply did
    # not fill. That costs nothing and changes nothing, so the opportunity is
    # still open - and on 2026-09-21 the 08:45 book gapped 9c in fourteen
    # seconds (0.76 -> 0.85 -> 0.87 -> 0.92) and ran to 96%, a winner missed
    # because a single miss ended the window.
    #
    # This is not chasing: the retry goes through every gate again at the NEW
    # price, so it is taken only if it is still a valid setup on its own terms,
    # and `auto_retry_limit` caps how far a running market can be followed.
    attempts, last_status = store.order_attempts(opened)
    # Band eligibility is NOT a reason to buy again. At 92c the most a contract
    # can make is 8c while 92c is at risk, and the Kalshi fee peaks mid-book, so
    # a setup that still "matches" can easily be worth less than it costs. Every
    # retry must clear expected value after the fee, not just the price band.
    priced_edge = measured_edge_at(contract_ask)
    retry_edge = (
        priced_edge - kalshi_fee_charged(contract_ask, 1)
        if priced_edge is not None
        else -1.0  # no measurement at this price: never a reason to retry
    )
    # Drift from the FIRST order of the window - the price we originally decided
    # to pay - not the last. Measured against the last attempt it is measured
    # incrementally, so a steady climb never trips the cap: on 2026-09-21 the
    # 10:00 window walked 0.85 -> 0.926 -> 0.963 in one-cent-ish steps and every
    # single step looked small while the total was 11 cents.
    #
    # Excludes proposals that never reached the exchange, so a manual button
    # offered far below the band is not the baseline.
    last_order_ms = (
        store.db.execute(
            "SELECT MAX(created_at) FROM trade_proposals WHERE window_open=? "
            "AND strategy='primary' AND entry_order_id IS NOT NULL",
            (opened,),
        ).fetchone()
        or (None,)
    )[0]
    first_order = store.db.execute(
        "SELECT entry_limit FROM trade_proposals WHERE window_open=? "
        "AND strategy='primary' AND entry_order_id IS NOT NULL "
        "ORDER BY attempt ASC LIMIT 1",
        (opened,),
    ).fetchone()
    drift = contract_ask - (first_order[0] if first_order else contract_ask)
    may_retry = (
        # terminal, and terminal in the one way that means nothing was bought:
        # an immediate-or-cancel leaves no resting order behind it
        last_status == "unfilled"
        and attempts < settings.auto_retry_limit
        and rule_match
        and retry_edge > 0
        and drift <= settings.auto_retry_max_drift
        and main_strategy_on(store)
    )
    if may_retry:
        print(
            f"auto: retry {attempts + 1}/{settings.auto_retry_limit} "
            f"{contract.ticker} at {contract_ask:.2f} "
            f"(edge after fee {retry_edge:+.4f}, drift {drift:+.2f})",
            flush=True,
        )
    # The alert fires ONCE per window; trading is evaluated on EVERY poll.
    #
    # These used to be the same gate, and that was the single biggest limiter on
    # the system. The alert fires at the first poll above the manual floor -
    # typically while the price is still walking up through the 60s and 70s -
    # and the gate then locked the window. Measured over 6,428 historical
    # windows: 67% of every window that ever qualified did so only AFTER that
    # lock, and was therefore untradeable. Separating them multiplies tradeable
    # windows by about 3x.
    #
    # Trading is still bounded, just by the right things: auto_block_reason
    # (one position at a time, daily loss floor, rate limits), the attempt cap,
    # and the rule itself re-evaluated at the current price.
    alerting = store.record_alert("primary", opened, now_ms)
    if alerting:
        # EVERY SIGNAL, $1, IN PARALLEL (the all-signal strategy). Before and
        # apart from everything the main strategy does next.
        allsignal_on_alert(store, settings, trader, contract, prediction.side,
                           contract_ask, snapshot, opened, now_ms, telegram)
        # SEND IT NOW: hold the alert until the order has reached Kalshi - it
        # used to wait 1.5-10 s behind the alert's own work (2026-09-30).
        await await_order_sent(store, opened)
    trading_open = rule.enabled and rule_match and auto_is_on(
        store, settings) and main_strategy_on(store)
    if not alerting and not may_retry and not trading_open:
        return
    # Recorded at alert time, not on the first poll of the window, so the stored
    # contract price is the one the alert actually quoted. Settling against a
    # price from five minutes earlier would score a trade nobody was offered.
    store.record(
        (
            opened,
            now_ms,
            snapshot.target,
            snapshot.price,
            prediction.side,
            prediction.bucket,
            prediction.raw_probability,
            contract.ticker,
            contract_ask,
            int(rule_match),
            failed_checks or None,
        )
    )

    # ---- unattended execution -------------------------------------------
    # Only a rule-qualified setup is ever taken automatically: the manual
    # override band exists for a human's judgement, and there is no human here.
    #
    # Two switches must both be on: the strategy file's own `enabled`, and the
    # runtime /auto flag. `enabled: false` reads to an operator as "this rule is
    # off"; honouring it only for alert labelling while still letting it spend
    # money unattended would make the off switch a lie.
    # The main strategy's own pause turns ITS automation off here; the
    # all-signal strategy, fired on the alert above, is not affected.
    auto_on = auto_is_on(store, settings) and main_strategy_on(store)
    # One line per window saying what automation decided and why. Without it a
    # night of no trades is indistinguishable from a night of broken automation:
    # a rule that never matched logs nothing at all, and so does a crash.
    if auto_on:
        if not rule.enabled:
            verdict = "strategy.json enabled=false"
            # Say it out loud, not just in a log nobody is reading at 3am.
            # strategy.json was reset to defaults with enabled=false on
            # 2026-09-21 at 02:02 and automation went silent for four hours
            # while 14 signals passed, 17 of 18 of which went on to win. The
            # log line existed and was useless: auto was "on", so there was
            # nothing to notice. Rate-limited to once an hour so a long outage
            # nags without flooding.
            last = store.get_setting("disabled_alert_ms", 0.0)
            if now_ms - last > 3_600_000:
                store.set_setting("disabled_alert_ms", float(now_ms), now_ms)
                notifier = Notifier(telegram, store, settings)
                await notifier.send_once(
                    "automation_off", str(opened),
                    messages.automation_off_message(
                        ask=contract_ask,
                        snapshot=notifier.snapshot(now_ms),
                        insight=notifier.insight_for(opened, now_ms),
                    ),
                    now_ms,
                )
        elif trader is None:
            verdict = "no execution client"
        elif not rule_match:
            # The numbers, not just the label: "target distance" alone hides
            # whether it missed by a hair or by a mile, and overnight that is
            # the difference between a rule to tune and a rule that is working.
            # The KALSHI facts under kalshi_only. This was the fifth call site
            # into the Binance rule, and it crashed the service every window
            # from 00:21 to 05:04: `check_facts` compares
            # `raw_probability >= min_raw_probability`, and that is None here.
            # It is reached only on the auto path when a market is eligible
            # and the rule then refuses it, which is why the first twenty
            # minutes after the deploy looked clean.
            if settings.kalshi_only:
                detail = [
                    (f["name"], f["passed"],
                     f["pass_text"] if f["passed"] else f["fail_text"])
                    for f in facts
                ]
            else:
                detail = rule.check_detail(
                    prediction, snapshot, contract_ask,
                    blocking_level=blocking_level, levels_ready=levels_ready,
                )
            verdict = "rule: " + "; ".join(
                f"{name}({value})" for name, passed, value in detail if not passed
            )
        else:
            verdict = "eligible"
        print(
            f"auto[{contract.ticker} {prediction.side}@{contract_ask:.2f} "
            f"{remaining}s]: {verdict}",
            flush=True,
        )
    # Why automation did NOT take a setup the rule qualified. The alert and the
    # auto path enforce DIFFERENT gates - the Checks block shows the rule, while
    # the settle timer, the attempt caps and the safety limits live only in the
    # auto path - so a message could show five green ticks and "ENTRY READY"
    # while the bot was refusing to trade it, with the reason nowhere on screen.
    # Computed here rather than inside the auto block so the ALERT can show it.
    # It is one indexed read of at most 60 rows, and it is the single number
    # that explained most refusals on 2026-09-21 while being invisible.
    # THE INSTRUMENT'S OWN BAND - the one its rule qualified on, and the one
    # `band_hold_s` records above. This read strategy.json's 0.70-0.93 for
    # EVERY instance, so gold (0.60-0.80) could only ever enter at 0.70+, and
    # every sub-0.70 setup its rule qualified was refused "price has only held
    # the band" - the range where gold's measured result lived (FINDINGS 108;
    # operator, 2026-09-28: trade gold on the numbers that worked for it).
    # BTC and ETH carry the same 0.70-0.93 in both files, so they are unchanged.
    settle_rule = kalshi_rule if settings.kalshi_only else rule
    settled_s = store.band_streak_seconds(
        opened, settle_rule.min_ask, settle_rule.max_ask, now_ms)

    auto_blocked = ""
    declined_reason: str | None = None
    if rule.enabled and rule_match and qualified:
        if not auto_on:
            auto_blocked = "automation is off (/auto on)"
        elif trader is None:
            auto_blocked = "no execution client configured"
    if rule.enabled and rule_match and auto_on and trader is not None:
        # The attempt and drift caps are enforced HERE, not only in `may_retry`.
        # Separating alerting from trading gave `trading_open` its own way into
        # this block, which bypassed both: the 10:00 window on 2026-09-21 placed
        # SIX orders against a limit of three and chased 0.85 to 0.963 against a
        # cap of 0.08. A limit that only one of several paths respects is not a
        # limit.
        # ORDER MATTERS. Position ownership and the daily floor are the real
        # safety controls and must speak first: after a fill, the honest reason
        # to refuse is "a position is already open", not "too many attempts".
        # The attempt cap is a chase control, not a safety one, and letting it
        # answer first hid whether the position guard was even working.
        limits = auto_limits(store, settings)
        counts = store.auto_state(now_ms)
        state = autotrade.AutoState(*counts)
        blocked = autotrade.auto_block_reason(
            limits, state, contract_ask, enabled=execution_configured(settings)
        )
        # The price must have SETTLED in the band, not merely touched it. This
        # is the single largest improvement measured: entering on the first
        # qualifying minute does not clear zero, entering after two minutes in
        # the band nearly triples the edge.
        if not blocked and settled_s < settings.entry_band_settle_s:
            blocked = (
                f"price has only held the band {settled_s:.0f}s, "
                f"waiting for {settings.entry_band_settle_s}s"
            )
        if not blocked and attempts >= settings.auto_retry_limit:
            blocked = (
                f"{attempts} order(s) already this window, limit "
                f"{settings.auto_retry_limit}"
            )
        # No chasing. Observing is free and there are minutes left, so a retry
        # may not pay more than the first order did - if the price ran away,
        # wait and see whether it comes back.
        if not blocked and attempts and drift > settings.auto_retry_max_drift:
            blocked = (
                f"price is {drift:+.2f} worse than the first order at "
                f"{contract_ask - drift:.2f}; waiting for it to come back"
            )
        if not blocked and attempts and last_order_ms is not None:
            waited = (now_ms - last_order_ms) / 1000
            if waited < settings.auto_retry_cooldown_s:
                blocked = (
                    f"only {waited:.0f}s since the last order, letting the book "
                    f"settle for {settings.auto_retry_cooldown_s}s"
                )
        if blocked:
            auto_blocked = blocked
            # Only the reason is kept here. The record itself is written below,
            # clear of this block - see the comment at that call.
            declined_reason = blocked
            print(f"auto: declined {contract.ticker} - {blocked}", flush=True)
        else:
            # SIZE IS THE OPERATOR'S, AND ONLY THE OPERATOR'S. The regime
            # weight was briefly wired into this budget on 2026-09-21; it was
            # never asked for and is reverted. Regime is intelligence - it is
            # shown in the alert and archived for measurement - and it must not
            # scale, throttle or otherwise touch what gets ordered. The budget
            # comes from settings and nothing else.
            # THE SINGLE SIZING AUTHORITY. The base tier comes from the daily
            # capital review - reconciled capital on the New York day - and is
            # the same object the recovery add-on asks. Two independent rules
            # that both change size is how a cap gets exceeded by the sum of
            # two things that each looked bounded.
            #
            # THE BUDGET MUST NOT SILENTLY CAP THE TIER. `auto_budget` is a
            # per-CONTRACT allowance, so the money available to an order scales
            # with the tier; treating it as a per-ORDER total would pin the size
            # at one contract for ever, and the growth controller would look
            # like it was working while changing nothing. When the operator's
            # budget really is the binding constraint, it is printed rather
            # than applied quietly.
            if capital is not None and settings.capital_sizing_enabled:
                tier = capital.base_contracts(now_ms)
                count = contracts_for_budget(limits.budget * tier, contract_ask)
                if count < tier:
                    print(
                        f"auto: budget caps the tier - {limits.budget:.2f}/contract "
                        f"x {tier} affords {count} at {contract_ask:.2f}",
                        flush=True,
                    )
                count = min(count, tier)
            else:
                count = contracts_for_budget(limits.budget, contract_ask)
            # Size up ONLY inside the measured edge band. Everywhere else the
            # deployed size is unchanged, so this can never trade bigger on a
            # setup the data does not support.
            count, size_reason = confidence_size(
                settings, snapshot, prediction, count
            )
            # Recovery, then the loss step - see `recovery_sizing`. It hands
            # back the base it started from, which the proposal records, and
            # whether the step upsized this entry, which the mirrors act on.
            (count, size_reason, base_count, is_recovery,
             recovery_reason, combo_due) = recovery_sizing(
                store, settings, count, size_reason, contract_ask
            )
            # A plain client ignores the mark; a mirroring one reads it once.
            trader.entry_is_recovery = is_recovery
            if count > 1:
                # At base 2 every entry is above one contract, so an empty
                # reason would print "sizing 2 contracts - " on every trade.
                print(
                    f"auto: sizing {count} contracts - "
                    f"{size_reason or f'base size {base_count}'}",
                    flush=True,
                )
            # THE RECOVERY, AS A COMBO AT BASE SIZE (2026-09-27). When it is
            # bought - or may have been - it IS this window's trade and nothing
            # single-leg follows; otherwise the entry below goes out exactly as
            # it would have, at base size.
            # ONE TRADE PER WINDOW, WHATEVER THE COMBO'S STATE. A combo that
            # is bought, may be bought, is mid-buy after a crash ('executing')
            # or failed on the order call is this window's trade - nothing
            # single-leg goes on top of it. Only a clear "nothing bought"
            # ('unfilled') releases the window, and then without a second
            # quote round: the entry goes out single-leg at base size.
            combo_status = store.combo_status(opened)
            if combo_status is not None and combo_status != "unfilled":
                return
            if combo_due and combo_status == "unfilled":
                combo_due = False
                size_reason = ("recovery due - combo not bought this window; "
                               "single-leg at base size")
            if combo_due:
                stop, note = await place_combo_recovery(
                    store, settings, telegram, trader, contract,
                    prediction.side, contract_ask, base_count, opened,
                    remaining, now_ms,
                )
                if stop:
                    return
                size_reason = note or size_reason
            proposal = create_proposal(
                store, "primary", opened, contract, prediction.side,
                contract_ask, 0, count, now_ms, settings.proposal_seconds,
                base_count=base_count,
            )
            claimed = store.claim_proposal(proposal.id, now_ms)
            if claimed is not None:
                # ONLY the order call may mark the proposal failed. Everything
                # after it - reading the fill back, composing the message,
                # sending it - happens when money has already moved, and a
                # Telegram hiccup there used to rewrite a real filled position
                # to 'failed', erasing it from the daily loss floor and freeing
                # the one-position guard while the contracts were still live.
                result = None
                submitted_ms = int(time.time() * 1000)
                # THE INSTRUMENT'S OWN CEILING. The IOC is sent AT the ceiling,
                # and `max_entry_price` (0.95) was sized for BTC's 0.70-0.93
                # band - for gold (0.60-0.80, negative above 0.85 on every
                # sample) a jumpy book could fill a 0.66 setup at 0.95, and the
                # mirror copies the same ceiling. Bounded by the rule's own top
                # plus the usual slippage: gold 0.85, BTC and ETH unchanged at
                # 0.95 (0.93 + 0.05 is above it). FINDINGS 108.
                entry_ceiling = settings.max_entry_price
                if settings.kalshi_only:
                    entry_ceiling = min(entry_ceiling, round(
                        kalshi_rule.max_ask + settings.entry_slippage, 4))
                try:
                    result = await trader.execute_with_take_profit(
                        claimed, settings.entry_slippage,
                        ceiling=entry_ceiling,
                    )
                except (httpx.HTTPError, OSError, RuntimeError, ValueError) as exc:
                    store.finish_proposal(
                        claimed.id, "failed", f"{type(exc).__name__}: {exc}"
                    )
                    print(f"auto: order failed {type(exc).__name__}", flush=True)

                # Logged whether or not the order threw: a miss is exactly
                # the case the execution archive exists to capture, and the
                # failures are the rows that would otherwise never be written.
                log_execution(
                    store, claimed=claimed, result=result,
                    decision_ask=contract_ask,
                    submitted_ms=submitted_ms, acked_ms=int(time.time() * 1000),
                    attempt=attempts + 1,
                    entry_slippage=settings.entry_slippage,
                )
                # ONE STEP OF THE RECOVERY PLAN IS NOW ON THE BOOK. Only a
                # trade that actually took the upsize spends one, and only on a
                # FILL: an order that bought nothing changed no plan.
                #
                # Here, on the post-order path, for the same reason
                # `log_execution` is here - the contracts are already at the
                # exchange, so this write cannot cost a fill, and
                # `consume_recovery_step` cannot raise.
                if recovery_reason and getattr(result, "filled_count", 0) > 0:
                    store.consume_recovery_step(now_ms, settings.recovery_steps)
                # The shadow row for a window we actually TRADED. `shadow_read`
                # ran only where the entry alert is built, and this branch
                # returns long before that, so `shadow_decisions` held rows for
                # the windows the bot passed on and nothing at all for the ones
                # it took - precisely the half the head-to-head needs. Here for
                # the same reason `log_execution` is here: the order is already
                # at the exchange, so the corpus query cannot cost a fill. And
                # BEFORE `record_fill_detail`, so the `traded` stamp in
                # `record_fill` has a row to land on.
                #
                # `getattr` rather than `result.filled_count`: an argument
                # expression is evaluated outside the callee's try, in the
                # order path - the same trap `log_execution` documents.
                shadow_read(
                    store, settings, contract, snapshot, prediction,
                    contract_ask, opened, remaining, now_ms, rule_match,
                    traded=getattr(result, "filled_count", 0) > 0,
                )
                if result is not None:
                    store.finish_proposal(
                        claimed.id, result.status, result.note,
                        result.entry_order_id, result.take_profit_order_id,
                    )
                    try:
                        # Report the fill, not the limit. They differ - the
                        # first live auto order was limited at 87c and filled
                        # at 84c - and every later number derives from this.
                        paid, filled_count = contract_ask, result.filled_count
                        fee, exact = None, True
                        if result.filled_count > 0:
                            paid, filled_count, fee, exact = await record_fill_detail(
                                store, trader, claimed.id, opened, prediction.side,
                                result.entry_order_id, contract_ask,
                                result.filled_count,
                            )
                            # THE GATES AS THEY WERE WHEN THE ORDER WENT OUT,
                            # from the same `check_facts` the alert renders.
                            # Re-deriving them at report time would describe a
                            # market that has already moved, and the point of
                            # showing them on a fill is the audit trail.
                            fill_facts = (
                                kalshi_signal.evaluate(
                                    KalshiBRTIRule.load(
                                        settings.kalshi_strategy_path),
                                    brti, contract_ask, remaining)[1]
                                if settings.kalshi_only
                                else rule.check_facts(
                                    prediction, snapshot, contract_ask,
                                    blocking_level=blocking_level,
                                    levels_ready=levels_ready,
                                )
                            )
                            # Computed from the FILL's own facts, but through
                            # the one function the signal uses, so the two
                            # messages for a single trade cannot disagree.
                            _fill_chop = (
                                getattr(brti, "brti_choppiness", None)
                                if brti is not None else None
                            )
                            fill_confidence = confidence_breakdown(
                                fill_facts, opened, blocking_level,
                                intel_verdict.confidence_delta,
                                _fill_chop,
                                choppiness_points(
                                    _fill_chop, settings.choppiness_penalty),
                            )
                            why = decision_record(
                                store, settings, claimed, contract,
                                snapshot, prediction, contract_ask,
                                opened, remaining, now_ms, settled_s,
                                blocking_level, paid, fee,
                                brti_features=brti,
                            )
                            # RENDER IT. `decision_record` returns a list of
                            # (label, value) pairs, and handing that straight
                            # to a TEXT column raised ProgrammingError one
                            # second after every fill - the position survived,
                            # because only the order call may mark a proposal
                            # failed, but the service died and the watchdog
                            # restarted it roughly every fifteen minutes.
                            detail_lines = [
                                f"\U0001f4cb <b>WHY THIS TRADE</b> · "
                                f"<code>{contract.ticker}</code>",
                                messages.RULE,
                            ] + [
                                f"  · {label}: <code>{value}</code>"
                                for label, value in (why or [])
                            ]
                            store.save_details(
                                claimed.id, "\n".join(detail_lines), now_ms
                            )
                            # ONE SURFACE. The fill is the second message of
                            # the same market, so it carries the same rotating
                            # insight and the same reconciled money footer as
                            # the signal that preceded it, and it is claimed
                            # before sending so a retry cannot double-report a
                            # position.
                            notifier = Notifier(telegram, store, settings)
                            await notifier.send_once(
                                "fill", f"{opened}:{claimed.id}",
                                messages.fill_message(
                                    side=prediction.side,
                                    ticker=contract.ticker,
                                    contracts=filled_count,
                                    paid=paid,
                                    fee=fee,
                                    remaining=remaining,
                                    # THE SAME ARITHMETIC AS THE SIGNAL. This
                                    # called `confidence_label` with no deltas
                                    # while the signal that preceded it passed
                                    # both the learned and the choppiness
                                    # adjustment, so one trade could alert
                                    # MEDIUM and fill HIGH - exactly what
                                    # `signal_message` promises cannot happen.
                                    confidence=fill_confidence["label"],
                                    confidence_note=surface.confidence_note(
                                        **{k: v for k, v
                                           in fill_confidence.items()
                                           if k != "label"}
                                    ),
                                    # An order the layer ADMITTED past a gate
                                    # must say so on the fill too.
                                    policy_note=messages.policy_line(
                                        intel_verdict
                                    ) if intel_verdict.final_action in (
                                        intel.ADMIT, intel.VETO) else "",
                                    facts=fill_facts,
                                    snapshot=notifier.snapshot(now_ms),
                                    insight=notifier.insight_for(opened, now_ms),
                                    band_hold=(
                                        int(settled_s),
                                        settings.entry_band_settle_s,
                                    ),
                                    # ONLY WHEN THE PRICE IS THE REAL ONE.
                                    # Without per-fill confirmation `paid` is
                                    # the order average, and comparing an
                                    # average against the decision ask reports
                                    # a slippage figure that was never priced.
                                    decision_ask=(
                                        contract_ask if exact else None
                                    ),
                                    priority=surface.priority_lines(
                                        recovery=store.stored_deficit(),
                                        partial=(
                                            "" if exact else
                                            "fill price is the order average, "
                                            "not a confirmed per-fill price"
                                        ),
                                    ),
                                    size_reason=size_reason,
                                ),
                                now_ms,
                                [("\U0001f4cb WHY THIS TRADE", f"details:{claimed.id}")],
                            )
                        else:
                            # Nothing was bought. Announcing a cost here claimed
                            # a position that does not exist.
                            notifier = Notifier(telegram, store, settings)
                            await notifier.send_once(
                                "not_filled", f"{opened}:{claimed.id}",
                                messages.not_filled_message(
                                    ticker=contract.ticker,
                                    note=result.note,
                                    snapshot=notifier.snapshot(now_ms),
                                    insight=notifier.insight_for(
                                        opened, now_ms),
                                    priority=surface.priority_lines(
                                        recovery=store.stored_deficit(),
                                    ),
                                ),
                                now_ms,
                            )
                    except (httpx.HTTPError, OSError, RuntimeError, ValueError) as exc:
                        # The order stands; only the reporting failed.
                        print(
                            f"auto: post-order reporting failed "
                            f"{type(exc).__name__}: {exc}",
                            flush=True,
                        )
            schedule_commentary(
                settings, store, telegram, contract, snapshot, prediction, rule,
                rule_match, failed_checks, contract_ask, remaining, opened, now_ms,
            )
            return
    # A refusal is evidence too, and it has no proposal: recording only the
    # fills left the archive holding one side of every decision the system ever
    # made. But `decision_record` runs a COHORTS.read plus an execution_report
    # - the same ~95ms archive-only corpus query `shadow_read` carries - and it
    # was doing that INSIDE the block that places the order, on the declined
    # path, where the next thing that can happen in this window is a retry
    # attempt at the very next poll. Same row, same data, written here instead:
    # after every branch that can send an order, and still above the
    # `not alerting` return, because most refusals happen on polls that have
    # already alerted.
    if declined_reason is not None:
        decision_record(
            store, settings, None, contract, snapshot, prediction,
            contract_ask, opened, remaining, now_ms, settled_s,
            blocking_level, contract_ask, None, action="DECLINED",
            blocked_reason=declined_reason, brti_features=brti,
        )
    if not alerting:
        # Already alerted this window. Trading was evaluated above; there is
        # nothing further to say until something happens.
        return
    head = head_for(store, settings)
    # ONE SNAPSHOT, COMPUTED ONCE, USED BY EVERY SURFACE BELOW.
    #
    # The gates, the context and the shadow read all describe the same instant,
    # so they are derived here and passed down rather than each recomputing
    # from `snapshot`. When they each did their own arithmetic the same alert
    # printed momentum as +3.3 bps in the checks and -3.3 bps in the context -
    # one signed for our side, one raw - with nothing to say which the rule
    # had actually used.
    # The SAME facts the decision was taken on. Re-running the Binance rule
    # here would not merely render the wrong gates - `check_facts` reads
    # `prediction.raw_probability`, which is None on the Kalshi path because
    # no Kalshi-native model exists, so it would raise on the first alert.
    facts = facts if settings.kalshi_only else rule.check_facts(
        prediction, snapshot, contract_ask,
        blocking_level=blocking_level, levels_ready=levels_ready,
    )
    detail_body = "\n".join(
        [f"\U0001f4cb <b>DETAILS</b> · <code>{contract.ticker}</code>", messages.RULE]
        + [
            f"  · {label}: <code>{value}</code>"
            for label, value in entry_context(
                settings, snapshot, prediction, contract_ask,
                priced_edge, settled_s, opened,
                rule_match=rule_match, blocking_level=blocking_level,
                # None on the Kalshi path: there is no Kalshi-native
                # probability model, and `None >= 0.9` raises. False is the
                # honest reading - "the model does not vouch for this" - and
                # is what "no model" must mean, never an implied yes.
                model_ok=(prediction.raw_probability or 0.0) >= 0.9,
            )
        ]
    )
    shadow = shadow_read(
        store, settings, contract, snapshot, prediction, contract_ask,
        opened, remaining, now_ms, rule_match,
    )
    if shadow:
        detail_body += "\n" + messages.RULE + "\n" + shadow
    # THE LEARNED ADJUSTMENT, in the word and in a line saying why.
    #
    # The delta moves the header; `policy_line` explains it, and returns empty
    # when the layer changed nothing - silence is the right output for "the
    # pattern supports the existing decision", and narrating every neutral
    # trains the reader to skip the one that matters.
    # CHOPPINESS LANDS HERE AND NOWHERE ELSE. It is computed from the same
    # BRTI window the gates read, and it is passed to the LABEL - not to
    # rule_match, not to check_facts, not to sizing. A window that went
    # nowhere lowers the word and refuses nothing.
    window_chop = (
        getattr(brti, "brti_choppiness", None) if brti is not None else None
    )
    breakdown = confidence_breakdown(
        facts, opened, blocking_level, intel_verdict.confidence_delta,
        window_chop,
        choppiness_points(window_chop, settings.choppiness_penalty),
    )
    confidence = breakdown["label"]
    # EVERY TERM BEHIND THE WORD, on the line under it. Three adjustments move
    # this label - the clock, the shield and choppiness - and none of them was
    # visible, so a 5/5 setup reading MEDIUM could only be checked by reading
    # the source. Rendered once and shared with the fill message below, which
    # is how the two are kept from disagreeing.
    confidence_note = surface.confidence_note(
        **{k: v for k, v in breakdown.items() if k != "label"}
    )
    policy_note = messages.policy_line(intel_verdict)
    if policy_note:
        detail_body += "\n" + messages.RULE + "\n" + policy_note
    # AN OVERRIDDEN GATE IS NOT A DETAIL. `policy_line` went only to the
    # DETAILS body, which is behind a button press, so an ADMIT - the layer
    # overruling a gate and letting an order through - would have placed a
    # trade with nothing in the message saying why a refused setup was taken.
    # A veto already reaches the decline list and a confidence delta already
    # reaches the score line; this is the one that was silent, and it is the
    # one that moves money against a gate's judgement.
    #
    # Carried in the ESSENTIALS so it survives whether the setup is alerted,
    # filled or declined.
    admit_note = (
        policy_note
        if intel_verdict.final_action in (intel.ADMIT, intel.VETO)
        else ""
    )
    if offer_button:
        proposal = create_proposal(
            store,
            "primary",
            opened,
            contract,
            prediction.side,
            contract_ask,
            0,
            # The manual budget, always. A button press IS the manual path, and
            # sizing it from the auto budget whenever the rule happened to
            # qualify meant /size was silently ignored on exactly the signals
            # most worth pressing.
            contracts_for_budget(budget_for(store, settings, auto=False), contract_ask),
            now_ms,
            settings.proposal_seconds,
        )
        # The status line says what is standing between this signal and an
        # order - which on 2026-09-21 was a settle timer the message never
        # mentioned while showing five green ticks.
        if qualified and auto_blocked:
            status = f"⏳ Auto waiting: {auto_blocked}"
        elif qualified:
            status = "✅ Auto will take this"
        else:
            # EVERYTHING AUTO RESPECTS, not only the five gates. This counted
            # failed checks and said nothing else, so a signal blocked by the
            # band-hold timer AND two gates reported "2 checks failed" - and
            # KXBTC15M-26SEP241030-30 showed "Band held: 0s of 60s" right
            # above a line that did not mention it. The operator reasonably
            # read the message as saying the five checks were the whole test.
            #
            # `auto_blocked` above is computed only when the gates already
            # passed, because that branch is the order path. The conditions
            # are re-read here - on the ALERTING path, where an extra query
            # cannot cost a fill - so a refused signal names all of them.
            # THE SAME WORDS AS THE TICKS ABOVE. The checks render through
            # `surface.display_name` - "Price", "Distance" - while this list
            # printed the rules' internal names, so a single message showed
            # a failed tick reading "Price" and a decline line naming the same
            # gate "Decision ask". Four names for two gates, on one screen.
            #
            # (The example is paraphrased deliberately: tests locate this
            # branch by searching the source for the decline heading, and a
            # comment quoting it verbatim captures that search.)
            unmet = [surface.display_name(str(fact["name"]))
                     for fact in facts if not fact["passed"]]
            if settled_s < settings.entry_band_settle_s:
                unmet.append(
                    f"band held {settled_s:.0f}s of "
                    f"{settings.entry_band_settle_s}s"
                )
            other = autotrade.auto_block_reason(
                auto_limits(store, settings),
                autotrade.AutoState(*store.auto_state(now_ms)),
                contract_ask, enabled=execution_configured(settings),
            )
            if other:
                unmet.append(other)
            if intel_verdict.final_action == intel.VETO:
                unmet.append("intelligence veto")
            status = (
                f"\U0001f916 Auto declined: {len(unmet)} condition"
                f"{'s' if len(unmet) != 1 else ''} unmet · "
                + " · ".join(unmet)
            )
        missing = missing_for_execution(settings)
        if missing:
            status += f"\n⚙️ A press will be refused — still needed: {missing}"
        # The message itself is assembled once, below, through the shared
        # surface. This branch only decides the status line and the buttons.
        store.save_details(proposal.id, detail_body, now_ms)
        buttons = messages.signal_buttons(
            prediction.side, proposal.id, proposal.id, override=not rule_match
        )
    else:
        key = f"w{opened}"
        status = ""
        store.save_details(key, detail_body, now_ms)
        buttons = messages.signal_buttons(prediction.side, None, key)
    # THE SHARED SURFACE. One reconciled snapshot for the whole message, the
    # market's own rotating insight, and a delivery record so a restart cannot
    # replay an alert the operator has already read.
    #
    # A signal that is merely WAITING on the band-hold timer edits the message
    # already on the screen rather than sending another: at a ten-second poll
    # one window produced dozens of near-identical notifications, and the
    # reader who learns to swipe those away swipes away the one that matters.
    notifier = Notifier(telegram, store, settings)
    # NAMED `money`, NOT `snapshot`. `snapshot` is already this function's
    # MarketSnapshot parameter, and shadowing it made every later read of
    # `snapshot.price` raise - the same collision that put a log string where
    # the intelligence Verdict belonged.
    money = notifier.snapshot(now_ms)
    insight = notifier.insight_for(opened, now_ms)
    surfaced = messages.signal_message(
        side=prediction.side,
        ticker=contract.ticker,
        ask=contract_ask,
        remaining=remaining,
        confidence=confidence,
        confidence_note=confidence_note,
        policy_note=admit_note,
        facts=facts,
        executable=qualified,
        status_line=status if offer_button else "⚪ Paper only · no order placed",
        snapshot=money,
        insight=insight,
        band_hold=(int(settled_s), settings.entry_band_settle_s),
        priority=surface.priority_lines(recovery=store.stored_deficit()),
        verdict=None if offer_button else "NO ENTRY",
    )
    waiting = bool(offer_button and auto_blocked)
    if waiting:
        await notifier.update_status(
            "signal", str(opened), surfaced, now_ms, buttons
        )
    else:
        await notifier.send_once(
            "signal", str(opened), surfaced, now_ms, buttons
        )

    schedule_commentary(
        settings, store, telegram, contract, snapshot, prediction, rule,
        rule_match, failed_checks, contract_ask, remaining, opened, now_ms,
    )


def schedule_commentary(
    settings: Settings,
    store: Store,
    telegram: Telegram,
    contract: KalshiMarket,
    snapshot: MarketSnapshot,
    prediction,
    rule: EntryRule,
    rule_match: bool,
    failed_checks: str,
    contract_ask: float,
    remaining: int,
    opened: int,
    now_ms: int,
) -> None:
    """Ask the local model to narrate the decision, strictly after the fact.

    The alert, the button, or the order are already away; this runs as its own
    task so a slow local model cannot hold up the poll loop, and it is dropped
    silently if the runtime is down, the recorded book is stale, or the reply
    contains a number nobody computed.

    Shared by the manual and unattended paths. It used to sit only after the
    manual alert, so an order placed automatically - the one case with nobody
    watching to form their own view - was the one case that got no commentary.
    """
    if not settings.brain_enabled:
        return
    # ONCE per window. Trading is now evaluated on every poll, and commentary
    # rode along with it - five messages in one minute, each restating the same
    # setup a few seconds apart. The narration is for the decision, not for the
    # poll, so it fires the first time there is something to say and then stops.
    if not store.record_alert("primary-brain", opened, now_ms):
        return
    # Finished conclusions, not raw numbers. The model used to be handed two
    # depths and asked which was deeper; it answered wrongly. Everything it can
    # now say has already been decided here.
    book = brain_mod.raw_book(settings.microstructure_path, contract.ticker)
    position = store.open_position_detail(opened)
    holding = position is not None
    exit_bid = 1 - contract.ask("DOWN" if prediction.side == "UP" else "UP")
    facts = decision_facts(
        ticker=contract.ticker,
        remaining_s=remaining,
        side=prediction.side,
        btc=snapshot.price,
        target=snapshot.target,
        our_ask=contract_ask,
        exit_bid=exit_bid,
        yes_bid=book.get("yes_bid"),
        yes_ask=contract.yes_ask,
        no_ask=contract.no_ask,
        yes_levels=book.get("yes_levels", []),
        no_levels=book.get("no_levels", []),
        momentum_5m_bps=snapshot.momentum_5m_bps,
        volatility_5m_bps=snapshot.volatility_5m_bps,
        futures_basis_bps=snapshot.futures_basis_bps,
        taker_imbalance=snapshot.taker_imbalance,
        spread_bps=snapshot.spread_bps,
        session=_session(opened),
        vol_regime=(
            "low" if snapshot.volatility_5m_bps < 8
            else ("high" if snapshot.volatility_5m_bps > 20 else "mid")
        ),
        book_age_s=book.get("book_age_s"),
        rule_match=rule_match,
        failed_gates=failed_checks or "",
        holding=holding,
        entry_paid=position[1] if position else None,
        unrealised=round((exit_bid - position[1]) * position[2], 4) if position else None,
        model_probability=prediction.raw_probability,
        measured_edge=measured_edge_at(contract_ask),
        slippage=settings.entry_slippage,
        # Time-of-day adjusts the confidence EXPLANATION only. It never
        # reaches polling, evaluation, the order path, alerts or archiving.
        hour_utc=datetime.fromtimestamp(opened / 1000, UTC).hour,
    )
    engine = brain_mod.Brain(
        brain_mod.BrainConfig(
            base_url=settings.brain_url,
            model=settings.brain_model,
            timeout_s=settings.brain_timeout_s,
        )
    )
    if settings.brain_commentary_enabled:
        # Off by default since 2026-09-21: the entry alert now carries the
        # checks, the context, the confidence arithmetic and the similar-regime
        # read, so a second message restated all of it in prose.
        asyncio.create_task(
            brain_mod.decision_commentary(engine, facts, telegram.send)
        )


async def report_combo_results(store: Store, telegram, settings: Settings,
                               now_ms: int) -> None:
    """One result message per settled recovery combo, from the broker's figure.

    Runs on the settlement sweep. `send_once` is keyed on the combo, so a pass
    that finds it again sends nothing. The P&L must already be in the ledger:
    a combo is only announced with money Kalshi has booked. Never raises.
    """
    try:
        # An UNKNOWN or interrupted combo is held until the broker's fills say
        # what it did - settled here, every minute, before anything reports.
        for line in store.reconcile_combos(now_ms):
            print(f"recovery combo reconciled: {line}", flush=True)
        # The ledger holds a combo only once Kalshi has reported it; bring the
        # realised record up to date FIRST - the lookup below joins on it.
        if not store.combo_tickers():
            return
        store.sync_ledger_from_settlements(now_ms)
        combos = store.combos_settled_since(now_ms - 2 * 86_400_000)
        if not combos:
            return
        notifier = Notifier(telegram, store, settings)
        for combo in combos:
            pnl = store.realised_for_ticker(combo["ticker"])
            if pnl is None:
                continue
            await notifier.send_once(
                "combo_result", str(combo["id"]),
                messages.combo_result_message(
                    market=combo["ticker"], legs=combo["legs"],
                    contracts=combo["count"], price=combo["price"], pnl=pnl,
                    snapshot=notifier.snapshot(now_ms),
                ),
                now_ms,
            )
    except Exception as exc:  # noqa: BLE001 - reporting is never fatal
        print(f"combo result report failed: {exc!r}", flush=True)


async def report_settlement(
    store: Store,
    telegram: Telegram,
    pending: tuple,
    result: str,
    settings: Settings,
    sizing: dict | None = None,
    basis: str = "1 contract",
    now_ms: int | None = None,
) -> None:
    """Tell Telegram how the market closed and whether our setup was right.

    A signal that is never scored is just an opinion. This closes the loop on
    every alert: which side actually won, whether ours did, and what it did to
    the account.

    Three cases, which must never be blurred together:

    * **Held to settlement** - money is the real fill, size and fee.
    * **Sold early** - money is the sale, and the market's later outcome is
      reported as information only. Scoring an exited position by who
      eventually won announced realised losses as wins, and disagreed in sign
      with the daily loss floor, which books the same trade at its exit price.
    * **Never traded** - no money figure at all beyond the paper per-contract
      one, because nothing was spent.
    """
    sizing = sizing or {"contracts": 1.0}
    now_ms = int(time.time() * 1000) if now_ms is None else now_ms
    window_open, called_side, ticker, contract_price, qualified, target = pending
    winner = "UP" if result == "yes" else "DOWN"
    # A WINDOW WHOSE TRADE WAS A RECOVERY COMBO is reported by the combo's own
    # result message (`report_combo_results`), once Kalshi has settled the
    # combo - which can be later than its legs. Recapping it here would call
    # the window "not traded" while money was on it.
    if store.combo_for_window(window_open) is not None:
        return

    trade = store.trade_for_window(window_open)
    # THE MONEY BELONGS TO THE SIDE WE HELD, NOT THE SIDE WE CALLED.
    #
    # `predictions` is keyed on `window_open` with INSERT OR IGNORE and written
    # at ALERT time, so it holds the FIRST side the reference named. If the
    # reference flips before the order fills, that row keeps the old side while
    # the position sits on the other one - and this function then computed
    # `won = called_side == winner` and handed the inverted flag to
    # `position_pnl`, which turned a paid-out win into a reported loss.
    #
    # It reached the operator twice on real money, both times as a loss that
    # was actually a win: 2026-09-21 (+$0.2821) and 2026-09-23 (+$0.1271, two
    # contracts of UP at 93.2c, Kalshi paid $2.00). `daily_ledger` was correct
    # throughout because it syncs from the broker, so the account never drifted
    # - only the sentence did.
    side = (trade.get("side") or called_side) if trade else called_side
    won = side == winner
    if trade:
        # One accounting call, the same one the daily loss floor uses, so the
        # message and the safety limit can never state different money for the
        # same trade. It covers a PARTIAL sale too: branching on "exited" alone
        # reported a partly-sold position as if the whole of it had been held,
        # erasing the sale and flipping the verdict's sign.
        size = trade["count"]
        sold = trade["exit_count"] or 0.0
        pnl = store_position_pnl(
            paid=trade["paid"],
            count=size,
            entry_fee=trade["fee"],
            exit_price=trade["exit_price"],
            exit_count=trade["exit_count"],
            won=won,
        )
        if sold >= size:
            basis = f"{sold:g} sold at {trade['exit_price']:.0%}"
        elif sold:
            basis = f"{sold:g} sold at {trade['exit_price']:.0%}, {size - sold:g} held"
        else:
            basis = f"{size:g} contract" + ("s" if size != 1 else "")
        contracts_shown, price_shown = size, trade["paid"]
        # BROKER-RECONCILED, NOT REBUILT. `daily_ledger` is the append-only
        # realised record synced from `/portfolio/settlements`; it counts an
        # early cash-out exactly once and the exchange may revise it. The local
        # reconstruction above stands in only until that row exists.
        booked = store.realised_for_ticker(ticker)
        if booked is not None:
            # The exchange books the market once for BOTH strategies; the
            # all-signal's $1 contract on it is taken back out (FINDINGS 111).
            pnl = booked - store.allsignal_net_for_ticker(ticker)
    else:
        pnl = None  # nothing was bought, so there is no money to report
        contracts_shown, price_shown = None, contract_price

    # TRADED is "did money move", not "was there a proposal". `trade` is the
    # reconciled position; without one nothing was bought and the recap must
    # say so rather than printing a cost nobody paid.
    traded = bool(trade)
    exited_at = trade["exit_price"] if trade and trade.get("exit_count") else None

    notifier = Notifier(store=store, telegram=telegram, settings=settings)
    # RECONCILE FIRST. The snapshot used to be taken the instant the window
    # closed - before Kalshi publishes the settlement and before the ledger
    # books it - so the recap announced a result its own totals did not yet
    # contain. `realised_for_ticker` is the ledger's answer for THIS market:
    # if it is there the totals include it, and if it is not, the footer says
    # the totals are one reconciliation behind instead of pretending
    # otherwise.
    snapshot = notifier.snapshot(now_ms)
    if traded and store.realised_for_ticker(ticker) is None:
        from dataclasses import replace as _replace

        snapshot = _replace(snapshot, pending=True)
    surfaced = messages.result_message(
        side=side,
        ticker=ticker,
        called_side=called_side,
        qualified=bool(qualified),
        contracts=contracts_shown or 0.0,
        # EVERY LEG, reconciled. A recovery add that filled used to be absent
        # from the recap entirely, so the profit beside it - which the broker
        # computes over the whole position - could not be derived from any
        # number the message showed.
        position=store.position_for_window(window_open) if trade else None,
        # The charged entry fee, so the recap's cost is the fill's cost.
        fee=(trade.get("fee") if trade else None),
        winner=winner,
        won=bool(won),
        traded=traded,
        pnl=pnl,
        paid=contract_price,
        exited_at=exited_at,
        # Kalshi's own target and settling value, so the recap says by how much
        # the market finished past the strike and not merely which way. None
        # until the enrichment pass has fetched them, which is a different
        # state from a market that settled level with its target.
        margin=store.settlement_margin(ticker, side),
        snapshot=snapshot,
        insight=notifier.insight_for(window_open, now_ms),
        priority=surface.priority_lines(
            recovery=store.stored_deficit(),
            # MONEY AND CALL SEPARATED, as everywhere else on this surface. A
            # market whose signal said one side and whose position took the
            # other has two verdicts, and hiding the disagreement is what let
            # the inverted recap look ordinary for two days.
            partial=(
                f"Signal called {called_side}; the position held {side}"
                if trade and called_side != side else ""
            ),
        ),
    )
    await notifier.deliver_result(
        window_open, "settlement", str(window_open), surfaced, now_ms,
        # A settled TRADE is money and is always reported; a paper signal's
        # recap is quiet on a shadow instance (it reaches the session summary).
        money=bool(trade),
    )


async def cash_out_exit(
    settings: Settings,
    store: Store,
    telegram: Telegram,
    contract: KalshiMarket,
    opened: int,
    remaining: int,
    now_ms: int,
    trader: KalshiExecutionClient | None = None,
) -> None:
    """Bank a position that has already earned nearly all it can.

    The mirror image of the reversal exit, and the only one of the two worth
    having. Bought at 74c and now bid 97c, the trade has captured 88% of its
    possible profit and is risking 97c to make the last 3c. Selling banks it.

    It does not beat holding - nothing does, because the price IS the
    probability - but measured on 5,753 paired trades, banking 90% of the
    available profit costs -$0.0006/contract, which is noise. Cashing out
    earlier is not free: -$0.0075 at a 0.95 bid, -$0.0127 at 0.90. So the
    threshold is deliberately high, and `cash_out_min_bid` stops it selling
    into a low bid just because the entry was cheap.

    Never sells at a loss: the gate is a fraction of the profit ABOVE the entry
    price, so a position under water simply does not qualify.
    """
    if not settings.cash_out_enabled or remaining < settings.exit_min_seconds:
        return
    if trader is None or not auto_is_on(store, settings):
        return
    position = store.open_position_detail(opened)
    if position is None:
        return
    side, paid, count, ticker, proposal_id = position

    # The quoted bid, then the price we would actually accept. Judging the
    # trade on the quote and then selling at the quote is what failed on
    # 2026-09-21: an IOC at a 0.979 top-of-book filled nothing while the app
    # offered 0.93. Everything below - the gate AND the order - uses the
    # discounted price, so a quote that is not really there declines instead
    # of firing.
    quoted = contract.bid(side)
    if quoted <= 0.0:  # older payloads carried no bid at all
        quoted = 1 - contract.ask("DOWN" if side == "UP" else "UP")
    bid = round(quoted - settings.exit_slippage, 4)
    if not 0.0 < bid < 1.0 or bid < settings.cash_out_min_bid:
        return
    available = 1.0 - paid  # the most this position can still make
    # THE PROPORTIONAL GATE IS UNREACHABLE ON AN EXPENSIVE ENTRY, and that is
    # why a position quoted 99.9% sat open. Banking `capture` of the profit
    # above `paid`, judged after the slippage discount, needs
    #
    #     quoted >= 0.10 * paid + 0.91
    #
    # which RISES with the entry price while the best bid a binary can offer
    # is capped near 0.999. Above paid = 0.89 it demands more than any bid can
    # pay, so it never fires - silently, because a gate that cannot fire looks
    # exactly like a market that never qualified. In the live record the exit
    # rate is 35%/58%/33% for entries below 0.89 and 4.2% (1 of 24) above it.
    #
    # So there is a second, ABSOLUTE trigger: a contract bid this close to
    # 1.00 has earned essentially everything it can, whatever it cost. It is
    # measured at 0.9938 against 0.9932 for holding over 146 cases - the same
    # "inside the noise" as the proportional rule, bought for the same reason.
    # One of the 146 is the case this exists for: KXBTC15M-26SEP220200-00 held
    # a 0.990 bid from 50s to 30s remaining, collapsed to 0.550 at 20s, and
    # settled the other way.
    #
    # Compared against the DISCOUNTED bid like everything else here, so the
    # default 0.98 means a quoted 0.99 - the level the measurement used.
    at_max = bid >= settings.cash_out_at_bid
    if not at_max and (
        available <= 0 or (bid - paid) < settings.cash_out_capture * available
    ):
        return
    # NEVER SELL AT A LOSS. The proportional gate guarantees this implicitly -
    # it is a fraction of the profit above `paid` - but the absolute one does
    # not, so it is stated. An entry above the trigger cannot happen while
    # `max_entry_price` is below it, and a rule that relies on another
    # setting's value is a rule that breaks when that setting moves.
    if bid <= paid:
        return
    if not store.record_alert("primary-cashout", opened, now_ms):
        return

    captured = (bid - paid) / available
    # The entry fee Kalshi actually charged, stored when the fill was read back.
    entry_fee = store.entry_fee(proposal_id)
    exit_fee = None
    try:
        result = await trader.close_position(
            ticker, side, count, bid, floor=settings.min_exit_price
        )
        if result.filled_count > 0:
            store.mark_exited(proposal_id, bid, result.filled_count, result.note)
            try:
                detail = await trader.fill_detail(result.entry_order_id, side)
            except (httpx.HTTPError, OSError, ValueError, KeyError):
                detail = None
            if detail:
                sold_at, sold_count, charged = detail
                store.mark_exited(proposal_id, sold_at, sold_count, result.note)
                bid = sold_at
                exit_fee = charged
        print(f"cash-out[{ticker} {side}]: {result.status} - {result.note}", flush=True)
    except (httpx.HTTPError, OSError, RuntimeError, ValueError) as exc:
        result = None
        print(f"cash-out failed {type(exc).__name__}: {exc}", flush=True)

    sold = bool(result and result.filled_count > 0)
    if sold:
        # BANK IT NOW, before the header is rendered. The proceeds are already
        # in the account; `settlements` will not carry them for another minute,
        # and the stale open mark still shows the position we just sold. Until
        # 2026-09-22 that gap is what let the settlement recap four minutes
        # later report this profit as missing and the next signal put it back.
        filled = result.filled_count if result else count
        banked = partial_exit_pnl(
            paid=paid, bid=bid, filled=filled, held=count,
            entry_fee=entry_fee, exit_fee=exit_fee,
        )
        # REALISED AT THE EXIT FILL, not when this loop noticed. The deficit
        # replays in realisation order, and our own discovery time has been
        # observed 917 seconds behind the broker's.
        store.record_realised(
            ticker, opened, banked, banked > 0, "cash_out", now_ms,
            realised_ms=store.last_exit_fill_ms(ticker) or now_ms,
        )
    notifier = Notifier(telegram, store, settings)
    await notifier.send_once(
        "cash_out", f"{opened}:{ticker}",
        messages.cash_out_message(
            ticker=ticker,
            side=side,
            paid=paid,
            bid=bid,
            count=count,
            captured=captured,
            remaining=remaining,
            note=result.note if result else "cash-out order failed; still holding",
            sold=sold,
            entry_fee=entry_fee,
            exit_fee=exit_fee,
            snapshot=notifier.snapshot(now_ms),
            insight=notifier.insight_for(opened, now_ms),
            priority=surface.priority_lines(recovery=store.stored_deficit()),
        ),
        now_ms,
    )


async def reversal_exit(
    settings: Settings,
    store: Store,
    telegram: Telegram,
    contract: KalshiMarket,
    snapshot: MarketSnapshot,
    opened: int,
    remaining: int,
    now_ms: int,
    trader: KalshiExecutionClient | None = None,
) -> None:
    """Close a position whose thesis has broken.

    The non-return trade is a bet that BTC stays on its current side of the
    strike. Once it crosses back, that bet is simply wrong, and holding to
    settlement only converts a partial loss into a total one. Over 68 days this
    exit raised net profit from $140 to $216 on $10 stakes and roughly halved
    the worst drawdown, from $233 to $116.

    While auto trading is on the exit is placed rather than merely alerted:
    unattended, there is nobody to press the button, and an entry-only
    automation would hold every broken thesis to settlement - which is not the
    strategy that was measured. The sell is immediate-or-cancel and reduce-only,
    so a no-fill simply leaves the position riding to settlement exactly as it
    would have without this path, and a stale count can never flip the position.
    With auto off it stays an alert needing a press.
    """
    if not settings.exit_on_reversal or remaining < settings.exit_min_seconds:
        return
    position = store.open_position_detail(opened)
    if position is None:
        return
    side, _entry, count, ticker, proposal_id = position
    crossed = (
        snapshot.price < snapshot.target if side == "UP" else snapshot.price > snapshot.target
    )
    if not crossed:
        return
    if not store.record_alert("primary-exit", opened, now_ms):
        return
    bid = 1 - contract.ask("DOWN" if side == "UP" else "UP")

    sellable = 0.0 < bid < 1.0
    if auto_is_on(store, settings) and trader is not None and sellable:
        try:
            result = await trader.close_position(
            ticker, side, count, bid, floor=settings.min_exit_price
        )
            if result.filled_count > 0:
                # Record the sale before refining its price. If the fill lookup
                # then fails, the position is still correctly marked closed -
                # the alternative leaves a sold position reading as open, which
                # would block every later entry all night.
                store.mark_exited(proposal_id, bid, result.filled_count, result.note)
                try:
                    detail = await trader.fill_detail(result.entry_order_id, side)
                except (httpx.HTTPError, OSError, ValueError, KeyError):
                    detail = None
                if detail:
                    sold_at, sold_count, _fee = detail
                    store.mark_exited(proposal_id, sold_at, sold_count, result.note)
                    bid = sold_at
            print(f"auto-exit[{ticker} {side}]: {result.status} - {result.note}", flush=True)
        except (httpx.HTTPError, OSError, RuntimeError, ValueError) as exc:
            result = None
            print(f"auto-exit failed {type(exc).__name__}: {exc}", flush=True)
        notifier = Notifier(telegram, store, settings)
        await notifier.send_once(
            "auto_exit", f"{opened}:{ticker}",
            messages.auto_exit_message(
                ticker=ticker,
                side=side,
                price=snapshot.price,
                target=snapshot.target,
                bid=bid,
                remaining=remaining,
                note=result.note if result else "exit order failed; holding to settlement",
                sold=bool(result and result.filled_count > 0),
                snapshot=notifier.snapshot(now_ms),
                insight=notifier.insight_for(opened, now_ms),
                priority=surface.priority_lines(
                    recovery=store.stored_deficit()),
            ),
            now_ms,
        )
        return

    notifier = Notifier(telegram, store, settings)
    await notifier.send_once(
        "exit_warning", f"{opened}:{contract.ticker}",
        messages.exit_warning_message(
            ticker=contract.ticker,
            side=side,
            price=snapshot.price,
            target=snapshot.target,
            remaining=remaining,
            bid=bid,
            snapshot=notifier.snapshot(now_ms),
            insight=notifier.insight_for(opened, now_ms),
            priority=surface.priority_lines(recovery=store.stored_deficit()),
        ),
        now_ms,
    )


async def reversion_signal(
    settings: Settings,
    store: Store,
    telegram: Telegram,
    contract: KalshiMarket,
    snapshot: MarketSnapshot,
    opened: int,
    remaining: int,
    now_ms: int,
) -> None:
    rule = ReversionRule.load(settings.reversion_strategy_path)
    remaining_minutes = round(remaining / 60)
    setup: ReversionSetup | None = rule.setup(snapshot, remaining_minutes)
    if setup is None:
        return
    entry = contract.ask(setup.side)
    setup, failed = rule.matches(snapshot, remaining_minutes, entry)
    if setup is None or not store.record_alert("reversion", opened, now_ms):
        return

    # Reversion runs silently: recorded, never announced. Its measured result
    # over 68 days is -$0.042 per contract, so an alert for it is noise beside
    # the strategy actually being worked on - and a second stream of buttons
    # invites taking a trade the data says loses. The record keeps accruing so
    # the decision can be revisited with evidence rather than reinstated on a
    # hunch.
    store.record_reversion(
        opened,
        now_ms,
        contract.ticker,
        setup.side,
        entry,
        rule.take_profit_price,
        setup.key_level,
        setup.spike_bps,
        setup.rejection_bps,
        remaining,
    )
    if not settings.reversion_alerts_enabled:
        return

    if rule.enabled and settings.entry_alerts_enabled:
        proposal = create_proposal(
            store,
            "reversion",
            opened,
            contract,
            setup.side,
            entry,
            rule.take_profit_price,
            settings.trade_contract_count,
            now_ms,
            55,
        )
        buttons = messages.execute_buttons(proposal.count, proposal.id)
        note = ""
    else:
        buttons = None
        note = failed or "holdout validation not passed"
    await telegram.send(
        messages.reversion_alert(
            head=head_for(store, settings),
            live=buttons is not None,
            side=setup.side,
            entry=entry,
            take_profit=rule.take_profit_price,
            key_level=setup.key_level,
            spike_bps=setup.spike_bps,
            rejection_bps=setup.rejection_bps,
            remaining=remaining,
            note=note,
        ),
        buttons,
    )


async def service() -> None:
    settings = Settings()
    # KALSHI ONLY. The Binance client is not CONSTRUCTED under `kalshi_only`,
    # so no active code path can reach it even by mistake - a flag checked at
    # each call site is a flag someone eventually forgets.
    # NO SECOND PRICE SOURCE EXISTS. This used to construct a Binance client
    # whenever `kalshi_only` was false, so the flag was the only thing between
    # the system and a feed that disagrees with the settlement index on 19% of
    # markets (FINDINGS 42). The client is gone, the flag no longer guards
    # anything, and there is no code path that can fetch a price from anywhere
    # but Kalshi. `market` stays as the name the shadow recorders read.
    market = None
    kalshi = KalshiClient(settings.kalshi_base_url, settings.kalshi_series)
    store = Store(settings.database_path)
    store.configure_instrument(settings)
    store.configure_recovery_exit(settings)
    telegram = Telegram(settings.telegram_bot_token, settings.telegram_chat_id, settings.dry_run)
    trader = None
    if execution_configured(settings):
        try:
            trader = KalshiExecutionClient(
                settings.kalshi_base_url,
                settings.kalshi_api_key_id,
                settings.kalshi_private_key_path,
            )
            # Moves only each order's SHORTFALL from shard 0 (ensure_funds).
            trader.auto_fund = bool(settings.kalshi_auto_fund)
            # COPY TRADING. Wrapping the client here is the whole integration:
            # every order path in this codebase goes through this one object,
            # and recovery_add_runner receives it as a parameter, so the mirrors
            # cover entry, upsize and exit without a single call site changing.
            # Reads still answer for the primary account alone.
            #
            # Three conditions, all required. Dry run must never reach another
            # account, and a mirror block with no key pair is ignored rather
            # than fatal.
            mirror_ok, mirror_why = mirror_allowed(settings)
            if mirror_ok:
                mirror_targets = targets_from_settings(settings)
                if mirror_targets:
                    trader = MirroringExecutionClient(
                        trader, mirror_targets, settings.kalshi_base_url,
                        gate=lambda name: mirror_on(store, name),
                    )
                    print(
                        f"copy switch [{current_instance()}]: " + ", ".join(
                            f"{t.name} {'on' if mirror_on(store, t.name) else 'OFF'}"
                            for t in mirror_targets),
                        flush=True,
                    )
                    print(
                        f"copy trading ON [{current_instance()}] -> "
                        f"{trader.describe()}",
                        flush=True,
                    )
                else:
                    print(
                        "copy trading enabled but no mirror credentials set",
                        flush=True,
                    )
            elif settings.mirror_enabled:
                # Say why on every instance that read the flag and declined it,
                # so a mirror that is off is never silently off.
                print(
                    f"copy trading OFF [{current_instance()}]: {mirror_why}",
                    flush=True,
                )
        except (OSError, ValueError) as exc:
            print(f"Kalshi execution disabled: {exc}", flush=True)
    # The hourly ladder takes its OWN Binance reading (see HourlyShadow.poll),
    # so although it never trades it IS an active Binance request. Under
    # Kalshi-only it does not run. Its archive stays readable as history.
    # THE LADDER IS A KALSHI PRODUCT AND RUNS UNDER KALSHI-ONLY. It was gated
    # on `not kalshi_only` because its spot CONTEXT came from Binance - three
    # numbers the BRTI reference carries natively - so removing Binance
    # silently switched off a Kalshi shadow. It stopped at 09-23 02:59 with 36
    # chains and 35 settlements, which is 34 consecutive hours: not enough to
    # judge any ladder strategy, and the reason FINDINGS 62 could not.
    hourly = HourlyShadow(settings) if settings.hourly_enabled else None
    reference = ReferenceShadow(settings) if settings.reference_enabled else None
    recovery_add = RecoveryAddRunner(settings, store, telegram)
    capital = CapitalController(settings, store)
    profit_task = None
    profit_guards = []
    if trader is not None and settings.execution_entry_series:
        allowed = {s.strip() for s in settings.execution_entry_series.split(",") if s.strip()}
        getattr(trader, "_primary", trader).allowed_entry_series = allowed
        for mirror in getattr(trader, "_mirrors", []):
            mirror.client.allowed_entry_series = allowed
    if (settings.daily_profit_target_enabled and not settings.dry_run
            and trader is not None and settings.kalshi_series == "KXBTC15M"):
        from .daily_profit import DailyProfitGuard, footer, monitor

        profit_path = Path(settings.database_path).resolve().parent / "runtime" / "daily_profit.db"
        clients = [("primary", "Primary", getattr(trader, "_primary", trader))]
        clients += [(m.target.name, mirror_name(settings, m.target.name), m.client)
                    for m in getattr(trader, "_mirrors", [])]
        for account, label, client in clients:
            guard = DailyProfitGuard(profit_path, account, label, client,
                                     settings.daily_profit_target_rate if account == "primary"
                                     else settings.mirror_daily_profit_target_rate)
            guard.entry_budget = (settings.allsignal_stake if account == "primary" else
                                  next(t.allsignal_budget for t in targets_from_settings(settings)
                                       if t.name == account))
            if account == "primary":       # only the primary (operator, 2026-09-30)
                guard.entry_risk_rate = float(
                    getattr(settings, "allsignal_stake_rate", 0.0) or 0.0)
                guard.after_loss_risk_rate = float(
                    getattr(settings, "allsignal_after_loss_stake_rate", 0.0) or 0.0)
                guard.after_target_stake = float(
                    getattr(settings, "allsignal_after_target_stake", 0.0) or 0.0)
                # ...and its DAILY CAP: done for the day at 20% (2026-10-02).
                guard.stop_rate = float(getattr(settings, "daily_profit_stop_rate", 0.0) or 0.0)
            else:                          # mirrors: the target scales with growth
                guard.entry_risk_rate = float(getattr(
                    settings, f"mirror_{account[1:]}_allsignal_risk_rate", 0.0) or 0.0)
                guard.max_target_wins = float(
                    getattr(settings, "mirror_target_max_wins", 0.0) or 0.0)
                guard.target_win_price = float(
                    getattr(settings, "target_win_price", 0.75) or 0.75)
                # ...and the STAKE scales with the account (operator, 2026-10-01):
                # set at each opening and applied to this mirror's copies and
                # pre-funding by swapping its (frozen) MirrorTarget. The primary:
                # never.
                guard.stake_rate = float(
                    getattr(settings, "mirror_stake_scale_rate", 0.0) or 0.0)
                guard.stake_max = float(
                    getattr(settings, "mirror_stake_scale_max", 0.0) or 0.0)
                guard.stake_target = next((m for m in getattr(trader, "_mirrors", [])
                                           if m.target.name == account), None)
                # ...and A MIRROR THAT KEEPS TRADING PAST ITS OWN TARGET at a lower
                # stake (operator, 2026-10-05: Affoue "becomes the account that keeps
                # trading after target hit ... after target hit $3"; FINDINGS 163).
                # 0: paused at its target, as every mirror was.
                guard.after_target_stake = float(getattr(
                    settings, f"mirror_{account[1:]}_allsignal_after_target_stake", 0.0) or 0.0)
            client.daily_profit_guard = guard
            profit_guards.append(guard)
        store.daily_profit_guards = profit_guards
        # EACH MIRROR'S OWN $ STAKE PER COPY (after a loss; past its target) and,
        # once the primary is DONE for the day, its $ signals to the mirrors still
        # trading by their own day (operator, 2026-10-05; FINDINGS 163).
        if getattr(trader, "_mirrors", None):
            trader.stake_for = lambda name, proposal: mirror_stake_now(
                store, settings, name, int(proposal.window_open))
            trader.copy_after_primary_done = bool(
                getattr(settings, "mirror_after_primary_done", False))
            trader.labels = {t.name: mirror_name(settings, t.name)
                             for t in targets_from_settings(settings)}
            if trader.copy_after_primary_done:
                for g in profit_guards:
                    if g.account == "primary":
                        # Built when the cap is said, not now (review 2026-10-05).
                        g.done_note = lambda: mirror_done_note(store, trader)
        # NOT A FOOTER ON EVERY MESSAGE. It appended ~12 lines of capital and
        # target to each entry, result and miss (operator, 2026-09-29: "the
        # telegram messaging is not properly formatted"). Results carry one
        # line (`_with_target`), the session summary the full block, and the
        # monitor still announces each pause/activation once.
        # Record every account before trading can begin. Existing records are
        # immutable on restart; the first activation starts NOW, not midnight.
        for guard in profit_guards:
            await guard.refresh(force=True)
        profit_task = asyncio.create_task(monitor(profit_guards, telegram))
    # CONTINUOUS LEARNING, inside the service. It ingests every settled signal,
    # refits on Kalshi-native features, and activates only what clears the
    # promotion bar. `startup` recovers whatever the previous process was doing
    # - including restoring a valid rollback if the artefact on disk cannot act.
    learner = LearningRunner(
        settings, store, telegram,
        on_activate=lambda: reload_policy(settings),
    )
    try:
        learner.startup(int(time.time() * 1000))
    except Exception as exc:  # noqa: BLE001 - learning never stops trading
        print(f"learning startup failed: {exc!r}", flush=True)
    LEARNING["runner"] = learner
    CAPITAL_DAY = {"ny": None}
    LAST_POLL = {"ms": 0}
    # Support/resistance is computed from Binance klines and the deployed rule
    # has `require_blocking_level: false`, so under Kalshi-only it is not
    # built at all rather than built and ignored.
    levels = None if settings.kalshi_only else LevelTracker()
    if settings.kalshi_only:
        from . import feature_contract
        # Under Kalshi-only the reference IS the signal source, not a shadow
        # recorder. With it off, every poll would record an input gap and the
        # system would go quiet in a way that looks exactly like a flat
        # market. Refuse to start rather than run silently useless.
        if not reference:
            raise SystemExit(
                "kalshi_only requires reference_enabled: BRTI is the signal "
                "source, not a shadow recorder. With it off the bot records "
                "an input gap every poll and never trades."
            )
        print(
            f"KALSHI ONLY: quotes, books, executions, settlements and BRTI "
            f"from Kalshi. Binance client not constructed. "
            f"features {feature_contract.describe()}",
            flush=True,
        )
    # WHICH INSTRUMENT EVERY MESSAGE FROM THIS PROCESS IS ABOUT. Set once,
    # here, so `surface.compose` can label messages that carry no ticker -
    # RECOVERY ARMED and the money summaries - which are exactly the ones
    # that would otherwise be ambiguous between two instances in one chat.
    surface.set_instrument(settings.kalshi_series)
    surface.set_recovery(
        settings.recovery_enabled,
        combo=settings.recovery_combo_enabled and bool(
            combo_recovery.PARTNERS.get(surface.asset(settings.kalshi_series))),
    )
    print(f"instrument: {surface.asset(settings.kalshi_series) or '?'} "
          f"({settings.kalshi_series}) -> messages are labelled",
          flush=True)
    if hourly:
        print(f"hourly ladder recording (shadow) -> {settings.hourly_database_path}",
              flush=True)
    if reference:
        feed = "BRTI live" if reference.brti_configured else "BRTI NOT entitled"
        print(
            f"settlement reference recording (shadow) -> "
            f"{settings.reference_database_path} [{feed}; official 60s averages "
            f"from Kalshi either way]",
            flush=True,
        )
    # WHAT SOURCE IS RUNNING, from the process itself. A deployed trading
    # service has to answer "what code is this?" from its own runtime
    # state, not from whatever the working tree looks like when asked.
    # WHAT THE EXECUTION MODE ACTUALLY IS, read rather than asserted. This line
    # said "execution requires Telegram approval" unconditionally. That was a
    # hardcoded claim, and on 2026-09-25 it became false: SOL runs with auto
    # trading on. A startup banner that states a safety property it never
    # checked is worse than silence - it is the line an operator would quote.
    #
    # AND IT CHECKS EXECUTION IS POSSIBLE, not only that the flag is set. The
    # flag alone would reproduce the same defect in the other direction: a
    # banner announcing "AUTO TRADING ON" on an instance that can place no
    # order, which is the state `/auto on` refuses for exactly this reason -
    # you would go to sleep believing it was trading.
    if auto_is_on(store, settings):
        limits = auto_limits(store, settings)
        mode = (f"AUTO TRADING ON, ${limits.budget:,.2f} per order, stops for "
                f"the day at -${abs(limits.daily_loss_limit):,.2f}")
        if not execution_configured(settings):
            mode = (f"AUTO TRADING ARMED BUT CANNOT EXECUTE - missing "
                    f"{missing_for_execution(settings)}")
    else:
        mode = "execution requires Telegram approval"
    print(f"BTC15 signal started; {revision.line()}; {mode}", flush=True)
    store.set_setting_text("running_revision",
                           json.dumps(revision.REVISION), int(time.time() * 1000))
    last_ticker = None
    try:
        while True:
            now_ms = int(time.time() * 1000)
            POLL_MARKS.clear()
            POLL_MARKS["poll"] = now_ms
            try:
                # TRADING FIRST when a $ entry is imminent: Telegram is read
                # BESIDE the price read (a background task) and the Kalshi sync
                # waits - at most 90 s. A command arriving just before such a
                # poll (/auto off) applies from the next poll, ~10 s later.
                urgent = allsignal_urgent(store, settings, now_ms)
                busy = TELEGRAM_TASK.get("task") is not None \
                    and not TELEGRAM_TASK["task"].done()
                if busy:
                    pass                        # one Telegram reader at a time
                elif not urgent:
                    await process_telegram(telegram, store, kalshi, trader, settings)
                else:
                    task = asyncio.get_running_loop().create_task(
                        process_telegram(telegram, store, kalshi, trader, settings))
                    task.add_done_callback(_report_background_failure)
                    TELEGRAM_TASK["task"] = task
                mark("telegram")

                # RECOVERY STATE, FOLDED ONCE A POLL. This is what keeps
                # `money_snapshot` read-only: the fold happens here, the
                # rendering path just reads the row. It also catches the
                # arm/clear transitions, which went entirely unreported until
                # the operator asked why a -$0.86 loss produced no visible
                # recovery - it had armed, blocked two adds and cleared, all
                # in silence.
                try:
                    event, rstate = store.recovery_transition(now_ms)
                    # NO RECOVERY, NO RECOVERY MESSAGES: the state is still
                    # folded (bookkeeping), nothing is announced.
                    if event and settings.recovery_enabled:
                        notifier = Notifier(telegram, store, settings)
                        # ONE snapshot for the message, taken once.
                        money = notifier.snapshot(now_ms)
                        if event == "armed":
                            last = store.last_realised_loss()
                            body = messages.recovery_armed_message(
                                rstate,
                                f"{last[0]} settled {last[1]:+.2f}"
                                if last else "",
                                snapshot=money,
                                loss_step=(
                                    settings.loss_step_budget
                                    if settings.loss_step_enabled else 0.0
                                ),
                                # The band and the wait come from the SETTINGS
                                # that decide them, so the sentence cannot
                                # describe a rule the service is not running.
                                band=(settings.loss_step_band_lo,
                                      settings.loss_step_band_hi),
                                wait=settings.loss_step_wait_markets,
                                # The step is per BASE contract, so the
                                # contracts it states follow today's tier.
                                base=(
                                    capital.base_contracts(now_ms)
                                    if settings.capital_sizing_enabled else 1
                                ),
                                add_per_base=(
                                    settings.recovery_add_max_contracts
                                    if settings.recovery_add_enabled else 0
                                ),
                                # Since 2026-09-27 the recovery is a combo at
                                # base size; say so, and name the partner.
                                combo=(settings.recovery_combo_enabled
                                       and bool(combo_recovery.PARTNERS.get(
                                           surface.asset(settings.kalshi_series)))),
                                partner="/".join(combo_recovery.PARTNERS.get(
                                    surface.asset(settings.kalshi_series), ())),
                                partner_band=(settings.combo_partner_band_lo,
                                              settings.combo_partner_band_hi),
                            )
                        elif event == "size_ended":
                            body = messages.recovery_size_ended_message(
                                rstate, snapshot=money)
                        else:
                            body = messages.recovery_cleared_message(money)
                        # KEYED ON THE CYCLE AND THE TRANSITION. A deficit can
                        # arm, end sizing and clear more than once in a day,
                        # and keying on the event alone would suppress the
                        # second cycle's arm as a duplicate of the first.
                        await notifier.send_once(
                            "recovery",
                            f"{getattr(rstate, 'cycle_id', '')}:{event}",
                            body, now_ms,
                        )
                except Exception as exc:  # noqa: BLE001 - reporting is never fatal
                    print(f"recovery report failed: {exc!r}", flush=True)

                # SESSION CLOSE REPORTS. Driven off the hour boundary the
                # poll stepped over, not a timer, so a slow cycle or a restart
                # that straddles a close still reports it rather than losing
                # it - and `session_reported` keys on the day and the session
                # so it can never be sent twice.
                for closed in closes_between(LAST_POLL["ms"], now_ms):
                    today_key = ny_day(now_ms)
                    if store.session_reported(today_key, closed):
                        continue
                    try:
                        rows = store.session_rows_for(closed, now_ms)
                        results = session_breakdown(rows)
                        result = results[0] if results else None
                        if result is None:
                            from .sessions import SessionResult

                            result = SessionResult(closed, 0, 0, 0.0)
                        notifier = Notifier(telegram, store, settings)
                        await notifier.send_once(
                            "session_close", f"{today_key}:{closed}",
                            messages.session_close_message(
                                session=result,
                                day_snapshot=notifier.snapshot(now_ms),
                                ny_day=today_key,
                            ),
                            now_ms,
                        )
                        store.mark_session_reported(today_key, closed, now_ms)
                    except Exception as exc:  # noqa: BLE001 - reporting is never fatal
                        print(f"session report failed: {exc!r}", flush=True)
                    # THE SHADOW SUMMARY, from ONE instance - the first on the
                    # alert list (BTC): every shadow instrument's session in one
                    # message, read from their stores read-only. Not keyed on
                    # the command flag, which gates the command loop only.
                    if shadow_summary_sender(settings):
                        try:
                            from . import shadow_summary

                            root = Path(settings.database_path).resolve().parent
                            # The new strategy only (operator, 2026-09-28:
                            # "show only the new stats"); shadow instruments
                            # are still recorded, not sent.
                            rows = []
                            live = shadow_summary.collect_allsignal(
                                root, settings, closed, now_ms)
                            if rows or live:
                                await _refresh_accounts(store)
                                await Notifier(telegram, store, settings).send_once(
                                    "shadow_summary", f"{today_key}:{closed}",
                                    _with_target(store, messages.shadow_summary_message(
                                        session=closed, rows=rows, ny_day=today_key,
                                        allsignal=live,
                                        book=_allsignal_book(settings, store, now_ms),
                                        balances=await allsignal_balances(
                                            store, trader, settings),
                                        copies=_allsignal_copies(settings),
                                        budget=settings.allsignal_stake,
                                        after_target=settings.allsignal_after_target_stake,
                                        after_loss=getattr(settings,
                                                           "allsignal_after_loss_stake", 0.0),
                                        after_loss_trades=getattr(
                                            settings, "allsignal_after_loss_trades", 2)),
                                        full=True),
                                    now_ms,
                                )
                        except Exception as exc:  # noqa: BLE001 - never fatal
                            print(f"shadow summary failed: {exc!r}", flush=True)
                LAST_POLL["ms"] = now_ms

                # THE DAILY CAPITAL REVIEW. At startup, and again the first
                # time a poll lands in a new New York day - the exchange's own
                # reset boundary, so our books and Kalshi's start together.
                today_ny = ny_day(now_ms)
                if CAPITAL_DAY["ny"] != today_ny:
                    # Move the whole accounting day together, carrying any
                    # losses already booked today so the floor is not refunded
                    # by the change itself.
                    store.migrate_day_boundary(now_ms)
                    reviewed = await capital.reconcile(trader, now_ms)
                    if reviewed is not None:
                        CAPITAL_DAY["ny"] = today_ny
                        print(
                            f"capital review [{today_ny}]: cash "
                            f"{reviewed.reconciled_cash:.2f}, base tier "
                            f"{reviewed.base_contracts}",
                            flush=True,
                        )

                # Settle first. A closed market's result does not depend on
                # another market being open, and running this after the lookup
                # meant every gap between 15-minute windows also postponed the
                # settlement reports for the window that had just ended.
                # IN ITS OWN TRY, and each market's read in its own: a market
                # whose result cannot be read, or a bug in the sweep, used to
                # abort the whole poll BEFORE anything was recorded - on every
                # poll, for as long as it lasted (2026-09-30 audit).
                try:
                    for row in store.pending_settlements(now_ms):
                        try:
                            result = await kalshi.result(row[2])
                        except (httpx.HTTPError, ValueError) as exc:
                            print(f"settlement read failed [{row[2]}]: {exc!r}",
                                  flush=True)
                            continue
                        if result:
                            store.settle(row[0], row[1], result)
                            # The research archive settles with the trade, so the
                            # two can never drift out of step. `won` is from OUR
                            # side's point of view, matching predictions.won.
                            winning_side = "UP" if result == "yes" else "DOWN"
                            # row[5] is the STRIKE. Passing it as `final_price`
                            # wrote the target into every settled row and left the
                            # settlement price recorded nowhere. The last observed
                            # BTC print of the window is the settlement price we
                            # actually saw.
                            settled_at = store.last_observed_btc(row[0]) or 0.0
                            store.settle_observations(
                                row[0], winning_side, settled_at
                            )
                            # Score the shadow layer against what actually
                            # happened. Without the outcome attached, a record of
                            # what it WOULD have decided can never be graded, and
                            # an ungradeable shadow can never be promoted.
                            store.settle_shadow(row[0], winning_side)
                            store.settle_decision_records(row[0], winning_side)
                            # Grade every intelligence decision on this market,
                            # including vetoes and refusals - those are the
                            # counterfactuals that say whether an adjustment
                            # helped, and they exist nowhere else.
                            try:
                                # Pass the WINNING SIDE, not a market-level `won`.
                                # Each row is scored against the side it was
                                # recorded on; the flag this loop could form -
                                # `result == ("yes" if winning_side == "UP" ...)` -
                                # is a tautology, and it graded all 21 rows to date
                                # as winners.
                                store.grade_candidates(
                                    row[0], winning_side, now_ms,
                                    lambda ask, won: (1.0 if won else 0.0) - ask
                                    - kalshi_fee_charged(ask, 1),
                                )
                                # None, not 0.0: each row is scored at its own
                                # decision-time ask, like the candidates above. The
                                # constant made every graded row read break-even.
                                store.grade_intelligence(
                                    row[0], winning_side, None, now_ms,
                                )
                            except Exception as exc:  # noqa: BLE001
                                print(f"intelligence grading failed: {exc!r}", flush=True)
                            # BANK IT BEFORE REPORTING IT. The recap renders the
                            # account, and until this market is in the ledger the
                            # count and the dollars describe different instants:
                            # the position has settled, so it is still in the open
                            # mark, but it is not yet a settled market in the
                            # record. On 2026-09-22 that printed "+$3.05 - 30W-5L"
                            # against a ledger holding 30W-6L. Read from the
                            # broker, never rebuilt, and never fatal - a failed
                            # sync leaves the recap on the previous snapshot,
                            # which is stale but internally consistent.
                            if trader is not None:
                                try:
                                    store.record_settlements(
                                        await trader.settlements(), now_ms
                                    )
                                    store.allsignal_grade(now_ms)
                                    # Said the moment it is graded - here, seconds
                                    # after the close - not on the next 60 s sync,
                                    # which put results 60-90 s behind the close.
                                    await report_allsignal_results(
                                        store, settings, telegram, now_ms)
                                    store.sync_ledger_from_settlements(now_ms)
                                    open_n, open_mark, per_ticker = (
                                        await trader.open_mark()
                                    )
                                    open_n, open_mark = _scoped_open(per_ticker, store.combo_tickers())
                                    store.set_setting("open_mark", open_mark, now_ms)
                                    store.set_setting("open_positions", open_n, now_ms)
                                    store.set_setting_text(
                                        "open_mark_detail", json.dumps(per_ticker), now_ms
                                    )
                                    SETTLEMENT_SYNC["at"] = now_ms
                                except Exception as exc:  # noqa: BLE001
                                    print(
                                        f"pre-report settlement sync failed: {exc!r}",
                                        flush=True,
                                    )
                            sizing, basis = report_sizing(settings)
                            await report_settlement(
                                store, telegram, row, result, settings, sizing, basis
                            )
                except (httpx.HTTPError, RuntimeError, ValueError, OSError) as exc:
                    print(f"cycle error: {type(exc).__name__}: {exc}", flush=True)
                except Exception as exc:  # noqa: BLE001 - see report_cycle_error
                    await report_cycle_error(exc, "settlement", settings, store,
                                             telegram, now_ms)

                # Pull the exchange's own settlement record. This is what
                # every money figure is read from - Telegram, the dashboard and
                # the daily floor - so it runs on the same beat as settling,
                # not on a timer that could drift behind a report.
                #
                # Kalshi credits a settlement a minute or two after close, so a
                # sync right after our own sweep may miss the window that just
                # ended; the next pass picks it up. Never fatal: a failed sync
                # leaves the mirror as it was, and the floor falls back to the
                # local reconstruction, which can only be more conservative.
                if trader is not None and (
                        (not urgent and now_ms - SETTLEMENT_SYNC.get("at", 0) >= 60_000)
                        or now_ms - SETTLEMENT_SYNC.get("at", 0) >= 90_000):
                    try:
                        store.record_settlements(await trader.settlements(), now_ms)
                        await report_combo_results(store, telegram, settings, now_ms)
                        # Graded before the broker reads below, which can fail
                        # (FINDINGS 111): ungraded $1 money sits in the floor.
                        store.allsignal_grade(now_ms)
                        await report_allsignal_results(store, settings, telegram, now_ms)
                        await remind_target_review(store, settings, telegram, now_ms)
                        # BY HOW MUCH, not merely whether. The margin between
                        # Kalshi's target and its settling value is the part of
                        # the lifecycle that says an 83c favourite finished
                        # comfortably rather than by a hair - a mean 18.3 bps on
                        # winners against 6.4 bps on losers, on both
                        # instruments. Capped per pass so a backlog is worked
                        # off over several polls instead of stalling one, and
                        # each market is asked once: `facts_synced_ms` marks it
                        # done even when Kalshi publishes neither number.
                        for ticker in store.settlements_missing_facts(8):
                            facts = await trader.settlement_facts(ticker)
                            store.record_settlement_facts(
                                ticker, facts.get("strike"),
                                facts.get("expiration_value"), now_ms,
                            )
                        # Executions come from the broker for the same reason
                        # the money does: `trade_proposals` records what the
                        # bot INTENDED, misses anything filled outside it, and
                        # miscounts anything whose status never went terminal.
                        store.record_fills(await trader.fills(), now_ms)
                        # The app's headline is today's realised PLUS the open
                        # position marked to the bid. Both halves or the number
                        # does not match what the operator is looking at.
                        open_n, open_mark, per_ticker = await trader.open_mark()
                        # SCOPED TO WHAT THIS INSTANCE TRADES. `open_mark` is
                        # the whole Kalshi account, and the operator trades by
                        # hand in it. On 2026-09-25 a BTC alert reported
                        # "Open position: -$2.89" that was ENTIRELY three
                        # KXMVECROSSCATEGORY positions of the operator's -
                        # none of it the bot's, under a BTC heading.
                        #
                        # Third instance of the same scope error, after the
                        # money footer and preflight. Same fix: name the
                        # series, and count only what this system placed.
                        open_n, open_mark = _scoped_open(per_ticker, store.combo_tickers())
                        store.set_setting("open_mark", open_mark, now_ms)
                        store.set_setting("open_positions", open_n, now_ms)
                        # Per ticker, so `open_exposure` can drop anything the
                        # ledger has already banked instead of counting it twice.
                        store.set_setting_text(
                            "open_mark_detail", json.dumps(per_ticker), now_ms
                        )
                        # The exchange is the authority on money, so every
                        # settled market it reports is written into the ledger.
                        # A figure a cash-out banked locally is revised only
                        # here, and the revision is counted, never silent.
                        store.sync_ledger_from_settlements(now_ms)
                        store.allsignal_grade(now_ms)
                        await report_allsignal_results(store, settings, telegram, now_ms)
                        # THE ADD'S OWN LIFECYCLE, closed from the same
                        # settlements the ledger just read. It writes only to
                        # `recovery_adds` - the account total and the deficit
                        # were both settled by the call above, from the whole
                        # position, and this is the per-leg figure the live
                        # test exists to produce. Idempotent, so running it
                        # every sync costs one query once the backlog drains.
                        closed = store.settle_filled_adds(now_ms)
                        if closed:
                            print(f"recovery adds settled: {closed}", flush=True)
                        # DEFERRED rows whose question can no longer be asked.
                        # `step` is not even called once the base position has
                        # exited, and `open_add` is keyed on the current
                        # window, so without this they stay open forever.
                        # Join decision rows to the orders their markets
                        # produced. Idempotent; stops once the archive is
                        # linked.
                        linked = store.link_intelligence_orders()
                        if linked:
                            print(
                                f"intelligence rows linked to orders: {linked}",
                                flush=True,
                            )
                        stale = store.close_stale_deferred_adds(now_ms)
                        if stale:
                            print(
                                f"recovery adds closed (deferred, expired): "
                                f"{stale}", flush=True
                            )
                        SETTLEMENT_SYNC["at"] = now_ms
                        mark("settlement_sync")
                    except Exception as exc:  # noqa: BLE001
                        print(f"settlement sync failed: {exc!r}", flush=True)

                mark("settlements")

                # CONTINUOUS LEARNING. Placed immediately after the settlement
                # sweep because settlements are what it consumes: the markets
                # that just resolved are in the mirror by now, so the due-check
                # sees this poll's evidence rather than the previous poll's.
                #
                # The check itself is throttled and the fit runs in a worker
                # thread, so neither the poll nor an order ever waits on it.
                try:
                    await learner.poll(now_ms)
                    mark("learning")
                except Exception as exc:  # noqa: BLE001 - never stops trading
                    print(f"learning poll failed: {exc!r}", flush=True)

                try:
                    contract = await kalshi.active_market(now_ms)
                    mark("active_market")
                except RuntimeError:
                    # Kalshi takes a few seconds to flip the next window to
                    # open. That is the normal shape of the boundary, not a
                    # failure, and logging it as an error buries real ones.
                    if last_ticker is not None:
                        print("between windows; waiting for the next market", flush=True)
                        last_ticker = None
                    # AN OUTAGE IS NOT A BOUNDARY. Kalshi flips windows in
                    # seconds; on 2026-09-24 it listed nothing for two hours
                    # and nothing said so - the service was healthy, the poll
                    # loop was turning, and the only symptom the operator had
                    # was Telegram going quiet. Reported once per gap.
                    MARKET_GAP.setdefault("since", now_ms)
                    closed_from = MARKET_GAP["since"]
                    if end_closure_at_reopen(now_ms, settings.venue_closed_gap_s * 1000):
                        print(f"{settings.kalshi_series} reopen time reached after "
                              f"{(now_ms - closed_from) / 3_600_000:.1f}h closed; "
                              "polling for the first market", flush=True)
                    gap_s = (now_ms - MARKET_GAP["since"]) / 1000
                    # IS THE VENUE SHUT, OR IS THIS AN OUTAGE? Gold and silver
                    # keep New York hours and are closed every weekend for
                    # about two days. Kalshi answers it: the next market's
                    # `open_time`. Far away means closed; soon or unknown means
                    # the outage alert below still has to fire.
                    closed_until = await _venue_closed_until(
                        kalshi, settings, now_ms, gap_s)
                    if closed_until:
                        if not MARKET_GAP.get("closed_told"):
                            MARKET_GAP["closed_told"] = 1
                            hours = (closed_until - now_ms) / 3_600_000
                            print(f"{settings.kalshi_series} is closed; next "
                                  f"market opens in {hours:.1f}h - polling "
                                  f"every {settings.venue_closed_poll_seconds}s "
                                  f"and not fetching the reference",
                                  flush=True)
                        # The reference recorder is deliberately NOT polled. The
                        # underlying metal is not trading either, so there is no
                        # price to record - this is the fetch the closure exists
                        # to avoid, ~19,000 requests across a weekend.
                        await asyncio.sleep(
                            closed_poll_seconds(settings, closed_until, now_ms))
                        continue
                    if (gap_s >= settings.market_gap_alert_s
                            and not MARKET_GAP.get("told")):
                        MARKET_GAP["told"] = 1
                        try:
                            await Notifier(telegram, store, settings).send_once(
                                "market_gap", str(MARKET_GAP["since"]),
                                messages.market_gap_message(
                                    gap_s, settings.kalshi_series), now_ms,
                            )
                        except Exception as exc:  # noqa: BLE001
                            print(f"market gap alert failed: {exc!r}", flush=True)
                    # The shadow archive still runs between windows - that is
                    # why this block used to sit ahead of the lookup. It now
                    # runs on BOTH paths instead, so the gap is still covered
                    # without the recorder standing in front of a live order.
                    if hourly:
                        # BRTI is the spot context under Kalshi-only, and it
                        # is the reference these contracts settle on.
                        await hourly.poll(
                            now_ms, market,
                            brti=(reference.current_features()
                                  if reference is not None else None),
                        )
                        await hourly.settle(now_ms)
                    # The reference recorder covers the gap between windows
                    # too: the 60 seconds a market settles on straddle the
                    # boundary, so stopping here would blind it to exactly the
                    # minute it exists to measure.
                    if reference:
                        await reference.poll(now_ms, None)
                        await reference.reconcile(now_ms)
                    await asyncio.sleep(settings.poll_seconds)
                    continue
                if MARKET_GAP.get("since"):
                    gap_s = (now_ms - MARKET_GAP["since"]) / 1000
                    told = MARKET_GAP.pop("told", None)
                    started = MARKET_GAP.pop("since")
                    # A closure that has ended must leave nothing behind, or
                    # the next genuine outage is silently treated as a weekend.
                    if MARKET_GAP.pop("closed_told", None):
                        print(f"{settings.kalshi_series} reopened after "
                              f"{gap_s / 3600:.1f}h closed", flush=True)
                    VENUE_SCHEDULE.clear()
                    if told:
                        try:
                            await Notifier(telegram, store, settings).send_once(
                                "market_gap_over", str(started),
                                messages.market_back_message(
                                    gap_s, contract.ticker), now_ms,
                            )
                        except Exception as exc:  # noqa: BLE001
                            print(f"market return alert failed: {exc!r}",
                                  flush=True)
                opened = contract.open_ms
                remaining = (contract.close_ms - now_ms) // 1000
                if settings.kalshi_only:
                    # THE REFERENCE POLL MOVES IN FRONT OF THE DECISION.
                    # It used to sit behind the trading path so a slow feed
                    # could not delay a fill (FINDINGS 22). That reasoning
                    # held while BRTI only labelled a context; now it IS the
                    # signal, and a decision taken before its own inputs are
                    # fetched is a decision on the previous window. This is a
                    # SWAP, not an addition - the Binance round trip it
                    # replaces cost the same.
                    if reference:
                        await reference.poll(now_ms, contract)
                        mark("brti_poll")
                    brti_features = reference.current_features() if reference else None
                    inputs = kalshi_signal.signal_inputs(
                        brti_features, contract, now_ms=now_ms,
                        stale_limit_ms=settings.reference_stale_ms,
                        max_spread_cents=settings.max_contract_spread_cents,
                    )
                    if isinstance(inputs, kalshi_signal.Unavailable):
                        # NO FALLBACK. Record which input failed and move on;
                        # substituting another exchange is how a system trades
                        # one instrument and settles on another.
                        store.record_input_gap(
                            window_open=opened, ticker=contract.ticker,
                            observed_ms=now_ms, remaining_s=remaining,
                            reason=inputs.reason, detail=inputs.detail,
                        )
                        if KALSHI_GAP["window"] != (opened, inputs.reason):
                            KALSHI_GAP["window"] = (opened, inputs.reason)
                            print(f"no signal [{inputs}]", flush=True)
                        # No quote means no ORDER, never no RECORDING.
                        await record_shadows(hourly, reference, market, now_ms)
                        await asyncio.sleep(settings.poll_seconds)
                        continue
                    snapshot = kalshi_snapshot(inputs[2], contract, opened, now_ms)
                    mark("kalshi_snapshot")
                else:
                    snapshot = replace(
                        await market.snapshot(opened), target=contract.target)
                    mark("binance_snapshot")
                if contract.ticker != last_ticker:
                    print(f"Live market data connected: {contract.ticker}", flush=True)
                    last_ticker = contract.ticker
                archive_observation(
                    settings, store, contract, snapshot, opened, remaining,
                    now_ms,
                    # The SAME features the decision is taken on, so the
                    # archive cannot describe a different instrument from the
                    # one that traded.
                    brti=(inputs[2] if settings.kalshi_only else None),
                )
                mark("archive")
                # The last reference poll's BRTI, for the `brti-1` context
                # key only. Reading the cache rather than fetching keeps the
                # recorder behind the trading path, which is the whole reason
                # it sits where it does: on 2026-09-21 three auto orders
                # missed on ~2,000ms of pre-order work against a 200ms round
                # trip. One poll of staleness is the price, and
                # `brti_context_row` checks for it rather than assuming.
                # TRADING IN ITS OWN TRY: an error here costs this poll's
                # orders, never its recording - the hourly ladder, the
                # reference and the exits below still run (2026-09-30).
                try:
                    await primary_signal(
                        settings, store, telegram, contract, snapshot, opened, remaining,
                        now_ms, trader, levels, capital,
                        reference.current_features() if reference else None,
                    )
                    # After a loss the $ signal may be waiting for its cushion -
                    # checked on EVERY poll, not only in the 11-6 min entry range.
                    allsignal_lock_poll(store, settings, trader, contract, snapshot,
                                        opened, remaining, now_ms, telegram)
                    allsignal_cushion_poll(store, settings, trader, contract, snapshot,
                                           opened, remaining, now_ms, telegram)
                    allsignal_retry_poll(store, settings, trader, contract, snapshot,
                                         opened, remaining, now_ms, telegram)
                    await await_order_sent(store, opened)   # a spawned order goes out now
                    prewarm_order_path(trader, settings, contract.ticker, store)
                    if not settings.kalshi_only:
                        # The reversion strategy reads Binance spike/rejection
                        # structure. It has no Kalshi-native equivalent yet, so
                        # under Kalshi-only it does not run rather than running on
                        # numbers that mean something else.
                        await reversion_signal(
                            settings, store, telegram, contract, snapshot,
                            opened, remaining, now_ms,
                        )
                except (httpx.HTTPError, RuntimeError, ValueError, OSError) as exc:
                    print(f"cycle error: {type(exc).__name__}: {exc}", flush=True)
                except Exception as exc:  # noqa: BLE001 - see report_cycle_error
                    await report_cycle_error(exc, "trading", settings, store,
                                             telegram, now_ms)
                # Shadow recording for the hourly ladder, AFTER the trading
                # path. It records and never trades, so it must never sit in
                # front of an order: on 2026-09-21 three auto orders missed
                # with decision_to_submit around 2,000ms against a 200ms
                # round trip to Kalshi, and this block - 188 rungs of quotes -
                # ran before every one of them. Both calls swallow their own
                # errors, so a slow ladder cannot break the trading loop.
                if hourly:
                    await hourly.poll(
                        now_ms, market,
                        brti=(reference.current_features()
                              if reference is not None else None),
                    )
                    await hourly.settle(now_ms)
                # Settlement reference, same contract as the hourly shadow and
                # for the same reason: it records, it never orders, and it sits
                # behind the trading path so a slow feed cannot delay a fill.
                # Both calls swallow their own errors.
                if reference:
                    await reference.poll(now_ms, contract)
                    await reference.reconcile(now_ms)
                    # THE CONDITIONAL RECOVERY ADD-ON. It runs AFTER the
                    # reference poll because it reads that poll's BRTI - one
                    # fetch, one series, one set of numbers, so the recorder
                    # and the order path cannot disagree about the reference
                    # at the same instant. It swallows its own errors, and
                    # with `recovery_add_enabled` off it records the decision
                    # and places nothing.
                    brti = reference.current_features()
                    position = store.open_position_detail(opened)
                    stood_down = False
                    if brti is not None and position is not None:
                        # SINCE THE FILL, not since the window opened. The
                        # first live evaluation vetoed an add on a crossing
                        # that happened 4m42s BEFORE the position existed.
                        #
                        # CONFIRM THE INSTANT FROM THE BROKER. `fills` is
                        # otherwise written only by the 60-second sweep, so for
                        # up to a minute after an entry this measured from the
                        # proposal's timestamp - when we ASKED, not when we
                        # were filled. Asked once per window, only while the
                        # broker's own fill is still missing.
                        if trader is not None and not store.has_broker_fill(
                            position[3]
                        ) and ENTRY_CONFIRM.get("window") != opened:
                            ENTRY_CONFIRM["window"] = opened
                            try:
                                store.record_fills(
                                    await trader.fills_for(position[3]), now_ms
                                )
                            except Exception as exc:  # noqa: BLE001
                                print(
                                    f"entry fill confirm failed: {exc!r}",
                                    flush=True,
                                )
                        entry_ms = store.position_entry_ms(opened, position[3])
                        if entry_ms is None:
                            crossing = Crossing(
                                None, "the entry instant is not established"
                            )
                        else:
                            crossing = reference.crossing_since(
                                entry_ms, position[0], now_ms
                            )
                        crossed = crossing.crossed
                        # NEVER STACK WITH THE LOSS STEP. When the previous
                        # market lost, the base position was already sized to
                        # the full `loss_step_budget` - that is the whole
                        # position the rule was measured as. Resting another
                        # contract behind it would add exposure to a size
                        # nobody chose, which is the failure the single-sizing-
                        # authority rule exists to prevent.
                        #
                        # Read here rather than carried from the order path
                        # because the add-on also runs on a position that
                        # survived a restart, where nothing was carried. While
                        # a position is open the previous market's result
                        # cannot change, so the two reads agree.
                        # STAND DOWN ONLY IF THE STEP ACTUALLY TOOK THIS
                        # POSITION. This used to key on "did the last market
                        # lose", which was the same thing while the step fired
                        # on the very next trade. Since 2026-09-25 it WAITS for
                        # a 0.70-0.79 ask, so a loss no longer implies an
                        # upsize - and standing the add-on down for an upsize
                        # that never happened would remove one mechanism
                        # without engaging the other.
                        #
                        # `position` IS A TUPLE - (side, paid, count, ticker,
                        # id) from `open_position_detail` - and an earlier cut
                        # of this read `position.side`, which is an
                        # AttributeError on every poll. It crash-looped ETH
                        # every 11 seconds on 2026-09-25 until the log was
                        # read. Side is element 0, and the ask is only ever
                        # used to ask "would the step fire at this price".
                        stood_down = add_on_stands_down(
                            store, settings, contract, position
                        )
                        if stood_down and STEP_STAND_DOWN.get("window") != opened:
                            # Said once per window, not every poll: a line on
                            # every beat is how a real message gets lost.
                            STEP_STAND_DOWN["window"] = opened
                            print(
                                "recovery add: standing down - position is "
                                "sized by the loss step",
                                flush=True,
                            )
                    if brti is not None and position is not None and not stood_down:
                        await recovery_add.step(
                            trader=trader,
                            contract=contract,
                            features=brti,
                            crossed=crossed,
                            crossing_reason=crossing.reason,
                            crossing_short_by_ms=crossing.short_by_ms,
                            remaining_s=remaining,
                            now_ms=now_ms,
                            opened=opened,
                        )
                # Same reasoning as the hourly shadow: a second Binance request
                # for a day of bars must never sit in front of an order. It
                # self-throttles and swallows its own errors. Under
                # Kalshi-only neither the tracker nor the client exists.
                if levels is not None and market is not None:
                    await levels.maybe_refresh(market, now_ms)
                await reversal_exit(
                    settings, store, telegram, contract, snapshot, opened, remaining,
                    now_ms, trader,
                )
                await cash_out_exit(
                    settings, store, telegram, contract, opened, remaining,
                    now_ms, trader,
                )
                # AND the $1 strategy's position, on its own book - the rule
                # above cannot see it (operator, 2026-09-28).
                await allsignal_cash_out(
                    settings, store, telegram, contract, opened, remaining,
                    now_ms, trader,
                )
            except (httpx.HTTPError, RuntimeError, ValueError, OSError) as exc:
                print(f"cycle error: {type(exc).__name__}: {exc}", flush=True)
            except Exception as exc:  # noqa: BLE001 - a bug must not stop recording
                await report_cycle_error(exc, "poll", settings, store, telegram, now_ms)
            await asyncio.sleep(settings.poll_seconds)
    finally:
        if profit_task is not None:
            profit_task.cancel()
            await asyncio.gather(profit_task, return_exceptions=True)
        if market is not None:
            await market.close()
        await kalshi.close()
        await telegram.close()
        if hourly:
            await hourly.close()
        if reference:
            await reference.close()
        if trader:
            await trader.close()


def run() -> None:
    asyncio.run(service())


if __name__ == "__main__":
    run()
