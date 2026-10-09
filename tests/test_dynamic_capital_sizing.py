import asyncio
import inspect
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from btc15_signal import main
from btc15_signal.config import Settings
from btc15_signal.daily_profit import DailyProfitGuard
from btc15_signal.execution import ExecutionResult
from btc15_signal.mirror import MirrorTarget, _Mirror
from btc15_signal.store import Store, TradeProposal
from btc15_signal.validation import contracts_for_risk_cap, kalshi_fee_charged


def add_day(guard, opening=100.0):
    with guard.connect() as db:
        db.execute(
            "INSERT INTO profit_days (account,day,label,opening,captured_ms,start_ms,basis,"
            "target,updated_ms,stake) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (guard.account, "2026-10-08", guard.label, opening, 1, 1, "day opening",
             opening * .03, 9_999_999_999_999, opening * guard.entry_risk_rate),
        )
    guard.error = ""


def test_fee_inclusive_risk_cap_rounds_down_with_one_contract_floor():
    n = contracts_for_risk_cap(40.75, .05, .85)
    assert n == 2
    assert n * .85 + kalshi_fee_charged(.85, n) <= 40.75 * .05
    assert (n + 1) * .85 + kalshi_fee_charged(.85, n + 1) > 40.75 * .05
    assert contracts_for_risk_cap(10, .05, .85) == 1


def test_guard_uses_daily_opening_and_distinct_primary_after_loss_rate(tmp_path):
    g = DailyProfitGuard(tmp_path / "p.db", "primary", "Primary", SimpleNamespace(), .03)
    g.entry_budget = 25
    g.entry_risk_rate = 25 / 708.75
    g.after_loss_risk_rate = 30 / 708.75
    add_day(g, 800)
    assert g.stake_today() == pytest.approx(800 * 25 / 708.75)
    base = g.entry_count(.85)
    boosted = g.entry_count(.85, after_loss=True)
    assert base == contracts_for_risk_cap(800, 25 / 708.75, .85)
    assert boosted == contracts_for_risk_cap(800, 30 / 708.75, .85)
    assert boosted > base


def test_primary_dynamic_count_uses_final_limit_and_keeps_one_when_stale(tmp_path):
    store = Store(str(tmp_path / "btc.db"))
    g = DailyProfitGuard(tmp_path / "p.db", "primary", "Primary", SimpleNamespace(), .03)
    g.entry_risk_rate = .05
    add_day(g, 40.75)
    store.daily_profit_guards = [g]
    settings = SimpleNamespace(allsignal_after_loss_trades=0, allsignal_stake=25,
                               allsignal_after_target_stake=0,
                               allsignal_after_loss_stake=0,
                               allsignal_stake_rate=.05,
                               allsignal_after_loss_stake_rate=.05)
    assert main.allsignal_count_now(store, settings, .85, 1, quote=.75) == 2
    with g.connect() as db:
        db.execute("UPDATE profit_days SET updated_ms=0")
    assert main.allsignal_count_now(store, settings, .85, 1, quote=.75) == 1


def test_mirror_sizes_from_own_opening_and_actual_order_ceiling(tmp_path):
    worker = object.__new__(_Mirror)
    worker.target = MirrorTarget("m1", "key", "path", allsignal_budget=99)
    worker.held = {}
    logs = []
    worker._log = lambda *args: logs.append(args)
    guard = DailyProfitGuard(tmp_path / "m.db", "m1", "Wife", SimpleNamespace(), .03)
    guard.entry_risk_rate = .075
    add_day(guard, 40.75)
    worker.client = SimpleNamespace(
        daily_profit_guard=guard,
        execute_with_take_profit=AsyncMock(return_value=ExecutionResult(
            "filled", 3, "id", None, "filled")),
        last_funding_note="",
    )
    proposal = TradeProposal(
        id="x", strategy="allsignal", ticker="KXBTC15M-X", side="UP",
        entry_limit=.75, take_profit=0, count=20, window_open=1,
        expires_at=2, close_ms=2, status="claimed")
    asyncio.run(worker._apply("entry", dict(
        proposal=proposal, slippage=.01, ceiling=.85, filled_count=20, stake=3)))
    sent = worker.client.execute_with_take_profit.call_args.args[0]
    assert sent.count == contracts_for_risk_cap(40.75, .075, .85) == 3
    assert sent.count * .85 + kalshi_fee_charged(.85, sent.count) <= 40.75 * .075


def test_dynamic_line_and_start_notice_name_fee_inclusive_cap(tmp_path):
    from btc15_signal import daily_profit

    g = DailyProfitGuard(tmp_path / "m.db", "m1", "Wife", SimpleNamespace(), .03)
    g.entry_budget = 3
    g.entry_risk_rate = .075
    add_day(g, 40.75)
    assert "max 7.5%/entry incl fees" in g.line()

    sent = []
    g.refresh = AsyncMock()
    asyncio.run(daily_profit._monitor_one(
        g, SimpleNamespace(send=AsyncMock(side_effect=lambda text: sent.append(text) or 1))))
    assert "Entry risk cap <b>7.5%</b> of opening capital, including fee" in sent[0]


def test_dynamic_rates_default_off_and_service_wires_every_account():
    fields = Settings.model_fields
    assert fields["allsignal_stake_rate"].default == 0
    assert fields["allsignal_after_loss_stake_rate"].default == 0
    for n in (1, 2, 3):
        assert fields[f"mirror_{n}_allsignal_risk_rate"].default == 0
    src = inspect.getsource(main.service)
    assert 'getattr(settings, "allsignal_stake_rate", 0.0)' in src
    assert 'getattr(settings, "allsignal_after_loss_stake_rate", 0.0)' in src
    assert 'f"mirror_{account[1:]}_allsignal_risk_rate"' in src
