import asyncio
import time
from dataclasses import replace
from datetime import UTC, datetime
from html import escape
from uuid import NAMESPACE_URL, uuid4, uuid5

import httpx

from . import autotrade, messages
from . import brain as brain_mod
from .binance import BinanceClient, MarketSnapshot
from .config import Settings
from .decision import decision_facts
from .execution import KalshiExecutionClient
from .features import _session
from .kalshi import KalshiClient, KalshiMarket
from .model import predict
from .store import Store, TradeProposal
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
        *store.scoreboard(**sizing), basis=basis, live=store.realised_record()
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
        if not separator or action not in {"execute", "skip"}:
            await telegram.answer_callback(callback_id, "Invalid action")
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
            result = await trader.execute_with_take_profit(claimed, settings.entry_slippage)
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
            "hour_utc": datetime.fromtimestamp(opened / 1000, UTC).hour,
            "vol_regime": (
                "low" if snapshot.volatility_5m_bps < 8
                else ("high" if snapshot.volatility_5m_bps > 20 else "mid")
            ),
            "holding": holding, "entry_paid": paid, "unrealised": unrealised,
        }
        row.update(book_metrics(settings, contract.ticker))
        store.observe_full(row)
    except Exception as exc:  # noqa: BLE001 - archiving must never stop trading
        print(f"archive failed: {type(exc).__name__}: {exc}", flush=True)


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
) -> None:
    rule = EntryRule.load(settings.strategy_path)
    # Scan the whole window rather than a single minute: take the first minute
    # where the rule qualifies, and fall back to one paper summary at the end if
    # none ever does. Firing only at rule.remaining_minutes meant the service
    # never saw the minutes where the measured edge actually lives.
    if not settings.entry_to_seconds <= remaining <= settings.entry_from_seconds:
        return
    if snapshot.spread_bps > settings.max_spread_bps:
        return
    prediction = predict(snapshot)
    calibration = store.calibration(prediction.bucket)
    contract_ask = contract.ask(prediction.side)
    rule_match, failed_checks = rule.matches(prediction, snapshot, contract_ask)
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
    retry_edge = MEASURED_BAND_EDGE - kalshi_fee_charged(contract_ask, 1)
    # Drift is measured against the last attempt that actually REACHED the
    # exchange, not the first proposal of the window - which may be a manual
    # button offered far below the band and would make every drift look huge.
    drift = contract_ask - (
        store.db.execute(
            "SELECT entry_limit FROM trade_proposals WHERE window_open=? "
            "AND strategy='primary' AND entry_order_id IS NOT NULL "
            "ORDER BY attempt DESC LIMIT 1",
            (opened,),
        ).fetchone()
        or (contract_ask,)
    )[0]
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
            detail = rule.check_detail(prediction, snapshot, contract_ask)
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
    if rule.enabled and rule_match and auto_on and trader is not None:
        limits = auto_limits(store, settings)
        counts = store.auto_state(now_ms)
        state = autotrade.AutoState(*counts)
        blocked = autotrade.auto_block_reason(
            limits, state, contract_ask, enabled=execution_configured(settings)
        )
        if blocked:
            print(f"auto: declined {contract.ticker} - {blocked}", flush=True)
        else:
            count = contracts_for_budget(limits.budget, contract_ask)
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
                try:
                    result = await trader.execute_with_take_profit(claimed, settings.entry_slippage)
                except (httpx.HTTPError, OSError, RuntimeError, ValueError) as exc:
                    store.finish_proposal(
                        claimed.id, "failed", f"{type(exc).__name__}: {exc}"
                    )
                    print(f"auto: order failed {type(exc).__name__}", flush=True)

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
                            await telegram.send(
                                messages.auto_filled(
                                    head=head_for(store, settings),
                                    ticker=contract.ticker,
                                    side=prediction.side,
                                    price=paid,
                                    count=filled_count,
                                    note=result.note,
                                    limit=contract_ask,
                                    fee=fee,
                                    exact=exact,
                                )
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
    if not alerting:
        # Already alerted this window. Trading was evaluated above; there is
        # nothing further to say until something happens.
        return
    head = head_for(store, settings)
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
        text = messages.entry_alert(
            head=head,
            live=qualified,
            side=prediction.side,
            ask=contract_ask,
            price=snapshot.price,
            target=snapshot.target,
            remaining=remaining,
            ticker=contract.ticker,
            model=prediction.raw_probability,
            observed=calibration.observed_rate,
            samples=calibration.samples,
            rule_ok=rule_match,
            rule_reason=failed_checks,
            checks=rule.check_detail(prediction, snapshot, contract_ask),
            missing=missing_for_execution(settings),
        )
        buttons = messages.execute_buttons(
            proposal.count, proposal.id, override=not rule_match
        )
    else:
        text = messages.no_entry_alert(
            head=head,
            side=prediction.side,
            ask=contract_ask,
            price=snapshot.price,
            target=snapshot.target,
            remaining=remaining,
            reason=failed_checks or "validated rule is disabled",
            model=prediction.raw_probability,
            observed=calibration.observed_rate,
            samples=calibration.samples,
        )
        buttons = None
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
        measured_edge=MEASURED_BAND_EDGE,
        slippage=settings.entry_slippage,
    )
    engine = brain_mod.Brain(
        brain_mod.BrainConfig(
            base_url=settings.brain_url,
            model=settings.brain_model,
            timeout_s=settings.brain_timeout_s,
        )
    )
    asyncio.create_task(brain_mod.decision_commentary(engine, facts, telegram.send))


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

    bid = 1 - contract.ask("DOWN" if side == "UP" else "UP")
    if not 0.0 < bid < 1.0 or bid < settings.cash_out_min_bid:
        return
    available = 1.0 - paid  # the most this position can still make
    if available <= 0 or (bid - paid) < settings.cash_out_capture * available:
        return
    if not store.record_alert("primary-cashout", opened, now_ms):
        return

    captured = (bid - paid) / available
    try:
        result = await trader.close_position(ticker, side, count, bid)
        if result.filled_count > 0:
            store.mark_exited(proposal_id, bid, result.filled_count, result.note)
            try:
                detail = await trader.fill_detail(result.entry_order_id, side)
            except (httpx.HTTPError, OSError, ValueError, KeyError):
                detail = None
            if detail:
                sold_at, sold_count, _fee = detail
                store.mark_exited(proposal_id, sold_at, sold_count, result.note)
                bid = sold_at
        print(f"cash-out[{ticker} {side}]: {result.status} - {result.note}", flush=True)
    except (httpx.HTTPError, OSError, RuntimeError, ValueError) as exc:
        result = None
        print(f"cash-out failed {type(exc).__name__}: {exc}", flush=True)

    sold = bool(result and result.filled_count > 0)
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
            result = await trader.close_position(ticker, side, count, bid)
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
    market = BinanceClient(settings.symbol, settings.spot_base_url, settings.futures_base_url)
    kalshi = KalshiClient(settings.kalshi_base_url, settings.kalshi_series)
    store = Store(settings.database_path)
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
    print("BTC15 signal started; execution requires Telegram approval", flush=True)
    last_ticker = None
    try:
        while True:
            now_ms = int(time.time() * 1000)
            try:
                await process_telegram(telegram, store, kalshi, trader, settings)

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
                        store.settle_observations(
                            row[0], row[1] == winning_side, row[5] or 0.0
                        )
                        sizing, basis = report_sizing(settings)
                        await report_settlement(
                            store, telegram, row, result, settings, sizing, basis
                        )

                try:
                    contract = await kalshi.active_market(now_ms)
                except RuntimeError:
                    # Kalshi takes a few seconds to flip the next window to
                    # open. That is the normal shape of the boundary, not a
                    # failure, and logging it as an error buries real ones.
                    if last_ticker is not None:
                        print("between windows; waiting for the next market", flush=True)
                        last_ticker = None
                    await asyncio.sleep(settings.poll_seconds)
                    continue
                opened = contract.open_ms
                remaining = (contract.close_ms - now_ms) // 1000
                snapshot = replace(await market.snapshot(opened), target=contract.target)
                if contract.ticker != last_ticker:
                    print(f"Live market data connected: {contract.ticker}", flush=True)
                    last_ticker = contract.ticker
                archive_observation(
                    settings, store, contract, snapshot, opened, remaining, now_ms
                )
                await primary_signal(
                    settings, store, telegram, contract, snapshot, opened, remaining,
                    now_ms, trader,
                )
                await reversion_signal(
                    settings, store, telegram, contract, snapshot, opened, remaining, now_ms
                )
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
        await market.close()
        await kalshi.close()
        if trader:
            await trader.close()


def run() -> None:
    asyncio.run(service())


if __name__ == "__main__":
    run()
