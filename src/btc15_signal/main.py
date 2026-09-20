import asyncio
import time
from dataclasses import replace

import httpx

from .binance import BinanceClient, MarketSnapshot
from .config import Settings
from .execution import KalshiExecutionClient
from .kalshi import KalshiClient, KalshiMarket
from .model import predict
from .store import Store, TradeProposal
from .strategy import EntryRule, ReversionRule, ReversionSetup
from .telegram import Telegram


def execution_configured(settings: Settings) -> bool:
    return bool(
        settings.execution_enabled
        and settings.kalshi_api_key_id
        and settings.kalshi_private_key_path
        and settings.telegram_authorized_user_id
    )


def proposal_buttons(proposal: TradeProposal) -> list[tuple[str, str]]:
    return [
        (f"Execute {proposal.count} contract(s)", f"execute:{proposal.id}"),
        ("Skip", f"skip:{proposal.id}"),
    ]


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
        if command in {"/keys", "/status"}:
            if int(message.get("from", {}).get("id", 0)) != settings.telegram_authorized_user_id:
                continue
            ready = "READY" if execution_configured(settings) and trader else "NOT CONFIGURED"
            await telegram.send(
                f"Execution: {ready}\n"
                "Kalshi credentials are managed only in the local .env file. "
                "The private key is never accepted or displayed in Telegram."
            )
            continue
        callback = update.get("callback_query")
        if not callback:
            continue
        callback_id = callback["id"]
        user_id = int(callback.get("from", {}).get("id", 0))
        callback_message = callback.get("message", {})
        if user_id != settings.telegram_authorized_user_id:
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
            if live_market.ticker != proposal.ticker or live_ask > proposal.entry_limit:
                store.finish_proposal(proposal.id, "rejected", "Market changed before approval")
                await telegram.answer_callback(callback_id, "Rejected: market or price changed")
                await telegram.clear_buttons(
                    callback_message["chat"]["id"], callback_message["message_id"]
                )
                continue
            claimed = store.claim_proposal(proposal.id, now_ms)
            if claimed is None:
                await telegram.answer_callback(callback_id, "Already handled")
                continue
            result = await trader.execute_with_take_profit(claimed)
            store.finish_proposal(
                claimed.id,
                result.status,
                result.note,
                result.entry_order_id,
                result.take_profit_order_id,
            )
            await telegram.answer_callback(callback_id, result.note)
            await telegram.clear_buttons(
                callback_message["chat"]["id"], callback_message["message_id"]
            )
            await telegram.send(
                f"ORDER {result.status.upper()}: {claimed.side} {result.filled_count:g} "
                f"contract(s)\n{result.note}\nEntry order: {result.entry_order_id}"
            )
        except (httpx.HTTPError, OSError, RuntimeError, ValueError) as exc:
            store.finish_proposal(proposal.id, "failed", f"{type(exc).__name__}: {exc}")
            await telegram.answer_callback(callback_id, "Order failed; see local logs")
            print(f"execution error: {type(exc).__name__}: {exc}", flush=True)


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


async def primary_signal(
    settings: Settings,
    store: Store,
    telegram: Telegram,
    contract: KalshiMarket,
    snapshot: MarketSnapshot,
    opened: int,
    remaining: int,
    now_ms: int,
) -> None:
    rule = EntryRule.load(settings.strategy_path)
    if abs(remaining - rule.remaining_minutes * 60) > settings.entry_tolerance_seconds:
        return
    if snapshot.spread_bps > settings.max_spread_bps:
        return
    prediction = predict(snapshot)
    calibration = store.calibration(prediction.bucket)
    contract_ask = contract.ask(prediction.side)
    rule_match, failed_checks = rule.matches(prediction, snapshot, contract_ask)
    created = store.record(
        (
            opened,
            now_ms,
            snapshot.target,
            snapshot.price,
            prediction.side,
            prediction.bucket,
            prediction.raw_probability,
            contract.ticker,
        )
    )
    if not created:
        return
    qualified = rule.enabled and rule_match and settings.entry_alerts_enabled
    if qualified:
        status = "ENTRY READY"
        action = f"Buy {prediction.side} at {contract_ask:.0%} or better; hold to settlement"
        proposal = create_proposal(
            store,
            "primary",
            opened,
            contract,
            prediction.side,
            contract_ask,
            0,
            settings.trade_contract_count,
            now_ms,
            settings.entry_tolerance_seconds,
        )
        buttons = proposal_buttons(proposal)
    else:
        status = "PAPER / NO ENTRY"
        action = f"Rule rejected: {failed_checks or 'validated rule is disabled'}"
        buttons = None
    await telegram.send(
        f"{status} — PRIMARY\n"
        f"BTC {prediction.side} | entry {contract_ask:.0%}\n"
        f"BTC ${snapshot.price:,.2f} | target ${snapshot.target:,.2f}\n"
        f"{remaining}s remain | {action}\n"
        f"Model {prediction.raw_probability:.1%} | observed {calibration.observed_rate:.1%} "
        f"({calibration.samples} samples)",
        buttons,
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
    if rule.enabled and settings.entry_alerts_enabled:
        status = "ENTRY READY"
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
        buttons = proposal_buttons(proposal)
        action = f"Entry {entry:.0%}; take profit {rule.take_profit_price:.0%}"
    else:
        status = "PAPER REVERSION"
        buttons = None
        action = f"No execution: {failed or 'holdout validation not passed'}"
    await telegram.send(
        f"{status} — SPIKE REVERSION\n"
        f"Buy {setup.side} | {action}\n"
        f"Key level ${setup.key_level:,.2f} | spike {setup.spike_bps:.1f} bps\n"
        f"Rejection {setup.rejection_bps:.1f} bps | {remaining}s remain\n"
        "The 30-35% value is the contract entry price, not a guaranteed win probability.",
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
    try:
        while True:
            now_ms = int(time.time() * 1000)
            try:
                await process_telegram(telegram, store, kalshi, trader, settings)
                contract = await kalshi.active_market(now_ms)
                opened = contract.open_ms
                remaining = (contract.close_ms - now_ms) // 1000
                snapshot = replace(await market.snapshot(opened), target=contract.target)
                for due_open, due_side, due_ticker in store.pending_settlements(now_ms):
                    result = await kalshi.result(due_ticker)
                    if result:
                        store.settle(due_open, due_side, result)
                await primary_signal(
                    settings, store, telegram, contract, snapshot, opened, remaining, now_ms
                )
                await reversion_signal(
                    settings, store, telegram, contract, snapshot, opened, remaining, now_ms
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
