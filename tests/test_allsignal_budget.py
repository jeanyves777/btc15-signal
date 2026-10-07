import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from btc15_signal import messages
from btc15_signal.execution import ExecutionResult
from btc15_signal.mirror import _Mirror, targets_from_settings
from btc15_signal.store import TradeProposal
from btc15_signal.validation import contracts_for_budget


def test_two_dollar_mirror_budget_reaches_actual_order_without_old_two_contract_cap():
    settings = SimpleNamespace(
        mirror_1_api_key_id="test", mirror_1_private_key_path="test",
        mirror_1_allsignal_budget=2, mirror_1_max_contracts=0,
        mirror_2_api_key_id="test2", mirror_2_private_key_path="test2",
        mirror_2_allsignal_budget=2, mirror_2_max_contracts=0,
    )
    for target in targets_from_settings(settings):
        worker = object.__new__(_Mirror)
        worker.target = target
        worker.held = {}
        worker._log = lambda *args: None
        worker.client = SimpleNamespace(execute_with_take_profit=AsyncMock(
            return_value=ExecutionResult("filled", 4, "id", None, "filled")))
        proposal = TradeProposal(
            id="test", strategy="allsignal", ticker="KXBTC15M-test", side="UP",
            entry_limit=.45, take_profit=0, count=11, window_open=0,
            expires_at=1000, close_ms=1000, status="claimed")
        asyncio.run(worker._apply("entry", dict(proposal=proposal, slippage=.01,
                                               ceiling=.46, filled_count=11)))
        submitted = worker.client.execute_with_take_profit.call_args.args[0]
        assert submitted.count == 4
        assert contracts_for_budget(5, .45) == 11


def test_telegram_names_the_configured_primary_budget():
    text = messages.allsignal_trade_message(
        asset="BTC", window_open=1790719200000, side="UP", signal_ask=.75,
        fill_price=.75, count=6, mirrored="Wife + Uncle George", budget=5)
    assert "BTC $5" in text
    assert "BTC $1" not in text
