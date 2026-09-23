import asyncio
import hashlib
import json
import time
from dataclasses import replace
from datetime import UTC, datetime
from html import escape
from pathlib import Path
from uuid import NAMESPACE_URL, uuid4, uuid5

import httpx

from . import autotrade, intel_mode, messages, revision
from . import brain as brain_mod
from . import intelligence_policy as intel
from . import kalshi_signal
from .adaptive import brti_context_of, context_of
from .binance import BinanceClient, MarketSnapshot
from .candidates import CandidateSet
from .capital import CapitalController, ny_day
from .config import Settings
from .decision import decision_facts
from .execution import KalshiExecutionClient
from .features import _session
from .hourly_shadow import HourlyShadow
from .kalshi import KalshiClient, KalshiMarket
from .kalshi_brti import KalshiBRTIRule
from .levels import LevelTracker
from .levels import confidence_points as level_points
from .model import predict
from .learning_runner import LearningRunner
from .recovery_add_runner import RecoveryAddRunner
from .reference_shadow import ReferenceShadow
from .regime import base_points as regime_base_points
from .regime import confidence_points as regime_confidence_points
from .regime import label_for as regime_label
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


def confidence_label(facts: list[dict], opened: int, blocking_level: float | None,
                     intelligence_delta: int = 0) -> str:
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
    """
    agreeing = sum(1 for fact in facts if fact["passed"])
    base = regime_base_points(agreeing)
    clock = regime_confidence_points(weight_at(opened))
    level = level_points(blocking_level is not None)
    return regime_label(
        max(0, min(100, base + clock + level + int(intelligence_delta or 0)))
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


def auto_limits(store: Store, settings: Settings) -> autotrade.AutoLimits:
    """Limits with any Telegram override applied."""
    return autotrade.AutoLimits(
        daily_loss_limit=store.get_setting(
            "auto_daily_loss_limit", settings.auto_daily_loss_limit
        ),
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


async def process_telegram(
    telegram: Telegram,
    store: Store,
    market_client: KalshiClient,
    trader: KalshiExecutionClient | None,
    settings: Settings,
) -> None:
    for update in await telegram.updates():
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
            "side": prediction.side, "raw_probability": prediction.raw_probability,
            "bucket": prediction.bucket, "our_ask": our_ask,
            "yes_ask": contract.yes_ask, "no_ask": contract.no_ask,
            "exit_bid": exit_bid,
            "momentum_5m_bps": snapshot.momentum_5m_bps,
            "volatility_5m_bps": snapshot.volatility_5m_bps,
            "distance_bps": prediction.distance_bps,
            "normalized_distance": prediction.distance_bps / volatility,
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
    from .binance import MarketSnapshot

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


def brti_context_row(snapshot, ask, opened, brti) -> tuple[dict | None, str]:
    """The `brti-1` context row for this decision, or (None, why-not).

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
    return {
        "session": _session(opened),
        "brti_volatility_bps": brti.brti_volatility_bps,
        "brti_normalized_distance": brti.brti_normalized_distance,
        "our_ask": ask,
    }, ""


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
    opened, remaining, now_ms, brti=None, ticker=None,
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
            brti_row, why_not = brti_context_row(snapshot, ask, opened, brti)
            if brti_row is None:
                return intel.Verdict(
                    base_qualified=bool(rule_match), failed_gates=(),
                    final_action=intel.NEUTRAL,
                    reason=f"no {intel.FEATURE_VERSION} context ({why_not})",
                )
            context = brti_context_of(brti_row)
        else:
            context = context_of(row)
        key = f"{context}|{'accept' if rule_match else 'reject'}"
        gates = normalise_gates(failed_checks)
        verdict = intel.decide(
            context_key=key, base_qualified=bool(rule_match),
            failed_gates=gates, ask=ask, policy=policy,
            enabled=settings.intelligence_enabled, features_ok=features_ok,
            now_ms=now_ms, max_age_ms=settings.intelligence_max_policy_age_ms,
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
        })
        # FORWARD EVALUATION, alongside. Every frozen candidate that speaks to
        # this context records what it WOULD have changed, beside what the
        # unchanged strategy actually decided. None of them can alter the
        # order; this is how one earns the right to, on data it was never
        # fitted to.
        try:
            candidates = active_candidates(settings)
            brti_row, why_not = brti_context_row(snapshot, ask, opened, brti)
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
                context_key=str(brti_context_of(brti_row)),
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
    trading_open = rule.enabled and rule_match and auto_is_on(store, settings)
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
    auto_on = auto_is_on(store, settings)
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
                await telegram.send(
                    "⚠️ <b>AUTOMATION IS OFF AT THE STRATEGY</b>\n"
                    f"<i>/auto is ON, but strategy.json has "
                    f"<code>enabled: false</code>, so no order will be placed - "
                    f"this one at {contract_ask:.0%} included.</i>\n"
                    "<i>Both switches must be on. Set enabled: true to resume.</i>"
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
    settled_s = store.band_streak_seconds(opened, rule.min_ask, rule.max_ask, now_ms)

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
            # RECOVERY WINS OVER THE BAND. A realised deficit is a fact about
            # the account; the confidence band is an opinion about the setup.
            # Checked second so its reason is the one reported when both apply.
            #
            # NOTHING HERE CAN CAUSE A TRADE. Every gate above has already
            # passed - this block only decides the size of an order that is
            # going out anyway, and a deficit is never a reason to enter.
            #
            # The state is read ONCE and carried to the step below, so the plan
            # that is charged for this order is the plan its size was chosen
            # from.
            recovery = store.recovery_state(settings.recovery_steps)
            recovered, recovery_reason = recovery_size(
                store, settings, count, contract_ask, state=recovery
            )
            if recovery_reason:
                count, size_reason = recovered, recovery_reason
            elif recovery.active:
                # Said out loud, because a silent non-upsize during recovery
                # looks exactly like recovery not working - which is how the
                # last sizing defect stayed invisible for a day.
                upsized = max(count, settings.high_confidence_contracts)
                print(
                    f"auto: recovery holding at base - "
                    f"{max_net_profit(upsized, contract_ask):.2f} max net does "
                    f"not cover {recovery.required_per_trade():.2f} of "
                    f"{recovery.deficit:.2f} outstanding",
                    flush=True,
                )
            if count > 1:
                print(f"auto: sizing {count} contracts - {size_reason}", flush=True)
            proposal = create_proposal(
                store, "primary", opened, contract, prediction.side,
                contract_ask, 0, count, now_ms, settings.proposal_seconds,
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
                try:
                    result = await trader.execute_with_take_profit(
                        claimed, settings.entry_slippage,
                        ceiling=settings.max_entry_price,
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
                            await telegram.send(
                                messages.order_filled(
                                    side=prediction.side,
                                    ticker=contract.ticker,
                                    contracts=filled_count,
                                    paid=paid,
                                    confidence=confidence_label(
                                        fill_facts, opened, blocking_level
                                    ),
                                    facts=fill_facts,
                                    band_held=(
                                        f"{settled_s:.0f}/"
                                        f"{settings.entry_band_settle_s}s"
                                    ),
                                    exact=exact,
                                    remaining=remaining,
                                    target=snapshot.target,
                                    price=snapshot.price,
                                    decision_ask=contract_ask,
                                    size_reason=size_reason,
                                ),
                                [("\U0001f4cb WHY THIS TRADE", f"details:{claimed.id}")],
                            )
                        else:
                            # Nothing was bought. Announcing a cost here claimed
                            # a position that does not exist.
                            await telegram.send(
                                f"⚠️ <b>AUTO ORDER NOT FILLED</b> "
                                f"· {escape(contract.ticker)}\n"
                                f"<i>{escape(result.note)}</i>\n"
                                "<i>No contracts were bought and nothing was spent.</i>"
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
    confidence = confidence_label(
        facts, opened, blocking_level, intel_verdict.confidence_delta
    )
    policy_note = messages.policy_line(intel_verdict)
    if policy_note:
        detail_body += "\n" + messages.RULE + "\n" + policy_note
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
            failed = sum(1 for fact in facts if not fact["passed"])
            status = f"\U0001f916 Auto declined: {failed} check{'s' if failed != 1 else ''} failed"
        missing = missing_for_execution(settings)
        if missing:
            status += f"\n⚙️ A press will be refused — still needed: {missing}"
        text = messages.signal_alert(
            side=prediction.side,
            ticker=contract.ticker,
            ask=contract_ask,
            price=snapshot.price,
            target=snapshot.target,
            remaining=remaining,
            confidence=confidence,
            facts=facts,
            executable=qualified,
            status_line=status,
            record=record_block(store, settings),
        )
        store.save_details(proposal.id, detail_body, now_ms)
        buttons = messages.signal_buttons(
            prediction.side, proposal.id, proposal.id, override=not rule_match
        )
    else:
        key = f"w{opened}"
        text = messages.signal_alert(
            side=prediction.side,
            ticker=contract.ticker,
            ask=contract_ask,
            price=snapshot.price,
            target=snapshot.target,
            remaining=remaining,
            confidence=confidence,
            facts=facts,
            executable=False,
            verdict="NO ENTRY",
            status_line="⚪ Paper only · no order placed",
            record=record_block(store, settings),
        )
        store.save_details(key, detail_body, now_ms)
        buttons = messages.signal_buttons(prediction.side, None, key)
    await telegram.send(text, buttons)

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


async def report_settlement(
    store: Store,
    telegram: Telegram,
    pending: tuple,
    result: str,
    settings: Settings,
    sizing: dict | None = None,
    basis: str = "1 contract",
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
    window_open, side, ticker, contract_price, qualified, target = pending
    winner = "UP" if result == "yes" else "DOWN"
    won = side == winner

    trade = store.trade_for_window(window_open)
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
    else:
        pnl = None  # nothing was bought, so there is no money to report
        contracts_shown, price_shown = None, contract_price

    # settle() has already run, so this outcome is inside the scoreboard.
    text = messages.settlement(
        head=head_for(store, settings),
        ticker=ticker or "",
        side=side,
        winner=winner,
        won=won,
        target=target,
        contract_price=price_shown,
        pnl=pnl,
        qualified=bool(qualified),
        basis=basis,
        contracts=contracts_shown,
        exited_at=trade["exit_price"] if trade and trade["exit_price"] else None,
        paper=trade is None,
        exact=trade["confirmed"] if trade else True,
        # The compact money line, from the broker-backed record. The full
        # scoreboard header no longer leads a result message: what the account
        # did is the point, and three lines of paper statistics above it is
        # what made the real figure the easiest thing on screen to miss.
        record=record_block(store, settings),
    )
    # "Qualified" means the rule liked the setup, NOT that an order was placed:
    # it is `int(rule_match)` recorded at alert time, and most of these were
    # never bought. Labelled paper so it cannot be read as the trading record -
    # that one is the Live line in the header.
    qualified_n, qualified_wins, qualified_pnl = store.scoreboard(
        **sizing, qualified_only=True
    )
    if qualified_n:
        text += (
            f"\n\U0001f4cc <i>Rule-qualified signals: {qualified_wins}/{qualified_n} "
            f"({qualified_wins / qualified_n:.0%}) · paper {qualified_pnl:+,.2f}</i>"
        )
    await telegram.send(text)


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
    if available <= 0 or (bid - paid) < settings.cash_out_capture * available:
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
    await telegram.send(
        messages.cash_out(
            head=head_for(store, settings),
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
        )
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
        await telegram.send(
            messages.auto_exit(
                head=head_for(store, settings),
                ticker=ticker,
                side=side,
                price=snapshot.price,
                target=snapshot.target,
                bid=bid,
                remaining=remaining,
                note=result.note if result else "exit order failed; holding to settlement",
                sold=bool(result and result.filled_count > 0),
            )
        )
        return

    await telegram.send(
        messages.exit_alert(
            head=head_for(store, settings),
            ticker=contract.ticker,
            side=side,
            price=snapshot.price,
            target=snapshot.target,
            remaining=remaining,
            bid=bid,
        )
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
    market = (
        None if settings.kalshi_only
        else BinanceClient(settings.symbol, settings.spot_base_url,
                           settings.futures_base_url)
    )
    kalshi = KalshiClient(settings.kalshi_base_url, settings.kalshi_series)
    store = Store(settings.database_path)
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
        except (OSError, ValueError) as exc:
            print(f"Kalshi execution disabled: {exc}", flush=True)
    # The hourly ladder takes its OWN Binance reading (see HourlyShadow.poll),
    # so although it never trades it IS an active Binance request. Under
    # Kalshi-only it does not run. Its archive stays readable as history.
    hourly = (
        HourlyShadow(settings)
        if settings.hourly_enabled and not settings.kalshi_only else None
    )
    reference = ReferenceShadow(settings) if settings.reference_enabled else None
    recovery_add = RecoveryAddRunner(settings, store, telegram)
    capital = CapitalController(settings, store)
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
    print(f"BTC15 signal started; {revision.line()}; execution requires "
          "Telegram approval", flush=True)
    store.set_setting_text("running_revision",
                           json.dumps(revision.REVISION), int(time.time() * 1000))
    last_ticker = None
    try:
        while True:
            now_ms = int(time.time() * 1000)
            POLL_MARKS.clear()
            POLL_MARKS["poll"] = now_ms
            try:
                await process_telegram(telegram, store, kalshi, trader, settings)
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
                    if event == "armed":
                        last = store.last_realised_loss()
                        await telegram.send(
                            messages.recovery_armed(
                                rstate,
                                f"{last[0]} settled {last[1]:+.2f}" if last else "",
                            )
                        )
                    elif event == "size_ended":
                        await telegram.send(messages.recovery_size_ended(rstate))
                    elif event == "cleared":
                        await telegram.send(
                            messages.recovery_cleared(store.money_snapshot(now_ms))
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
                        await telegram.send(
                            messages.session_close(
                                session=result,
                                day_snapshot=store.money_snapshot(now_ms),
                                ny_day=today_key,
                            )
                        )
                        store.mark_session_reported(today_key, closed, now_ms)
                    except Exception as exc:  # noqa: BLE001 - reporting is never fatal
                        print(f"session report failed: {exc!r}", flush=True)
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
                for row in store.pending_settlements(now_ms):
                    result = await kalshi.result(row[2])
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
                            store.grade_intelligence(
                                row[0], winning_side, 0.0, now_ms,
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
                                store.sync_ledger_from_settlements(now_ms)
                                open_n, open_mark, per_ticker = (
                                    await trader.open_mark()
                                )
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
                if trader is not None and now_ms - SETTLEMENT_SYNC.get("at", 0) >= 60_000:
                    try:
                        store.record_settlements(await trader.settlements(), now_ms)
                        # Executions come from the broker for the same reason
                        # the money does: `trade_proposals` records what the
                        # bot INTENDED, misses anything filled outside it, and
                        # miscounts anything whose status never went terminal.
                        store.record_fills(await trader.fills(), now_ms)
                        # The app's headline is today's realised PLUS the open
                        # position marked to the bid. Both halves or the number
                        # does not match what the operator is looking at.
                        open_n, open_mark, per_ticker = await trader.open_mark()
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
                    # The shadow archive still runs between windows - that is
                    # why this block used to sit ahead of the lookup. It now
                    # runs on BOTH paths instead, so the gap is still covered
                    # without the recorder standing in front of a live order.
                    if hourly:
                        await hourly.poll(now_ms, market)
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
                    settings, store, contract, snapshot, opened, remaining, now_ms
                )
                mark("archive")
                # The last reference poll's BRTI, for the `brti-1` context
                # key only. Reading the cache rather than fetching keeps the
                # recorder behind the trading path, which is the whole reason
                # it sits where it does: on 2026-09-21 three auto orders
                # missed on ~2,000ms of pre-order work against a 200ms round
                # trip. One poll of staleness is the price, and
                # `brti_context_row` checks for it rather than assuming.
                await primary_signal(
                    settings, store, telegram, contract, snapshot, opened, remaining,
                    now_ms, trader, levels, capital,
                    reference.current_features() if reference else None,
                )
                if not settings.kalshi_only:
                    # The reversion strategy reads Binance spike/rejection
                    # structure. It has no Kalshi-native equivalent yet, so
                    # under Kalshi-only it does not run rather than running on
                    # numbers that mean something else.
                    await reversion_signal(
                        settings, store, telegram, contract, snapshot,
                        opened, remaining, now_ms,
                    )
                # Shadow recording for the hourly ladder, AFTER the trading
                # path. It records and never trades, so it must never sit in
                # front of an order: on 2026-09-21 three auto orders missed
                # with decision_to_submit around 2,000ms against a 200ms
                # round trip to Kalshi, and this block - 188 rungs of quotes -
                # ran before every one of them. Both calls swallow their own
                # errors, so a slow ladder cannot break the trading loop.
                if hourly:
                    await hourly.poll(now_ms, market)
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
                    if brti is not None and position is not None:
                        # SINCE THE FILL, not since the window opened. The
                        # first live evaluation vetoed an add on a crossing
                        # that happened 4m42s BEFORE the position existed.
                        entry_ms = store.position_entry_ms(opened, position[3])
                        crossed = (
                            None if entry_ms is None
                            else reference.crossed_since(entry_ms, position[0])
                        )
                        await recovery_add.step(
                            trader=trader,
                            contract=contract,
                            features=brti,
                            crossed=crossed,
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
            except (httpx.HTTPError, RuntimeError, ValueError, OSError) as exc:
                print(f"cycle error: {type(exc).__name__}: {exc}", flush=True)
            await asyncio.sleep(settings.poll_seconds)
    finally:
        if market is not None:
            await market.close()
        await kalshi.close()
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
