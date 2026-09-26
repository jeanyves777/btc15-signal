"""Copy trading to other accounts.

The invariants under test are the ones that cost real money if they break: the
primary is never affected, an unfilled entry is never mirrored, each account
sizes itself, and an exit closes what THAT account bought.
"""
import asyncio

import pytest

from btc15_signal import mirror as mirror_mod
from btc15_signal.execution import ExecutionResult
from btc15_signal.mirror import (
    MirroringExecutionClient,
    MirrorTarget,
    targets_from_settings,
)
from btc15_signal.store import TradeProposal


def proposal(count=1, limit=0.80, ticker="KXBTC15M-T", side="yes"):
    return TradeProposal(
        id="p1", strategy="auto", window_open=0, ticker=ticker, side=side,
        entry_limit=limit, take_profit=0.99, count=count, expires_at=0,
        close_ms=0, status="pending",
    )


class FakeClient:
    """Stands in for KalshiExecutionClient on both the primary and the mirrors."""

    def __init__(self, fill=True, boom=False):
        self.fill = fill
        self.boom = boom
        self.calls = []
        self.closed = False

    async def execute_with_take_profit(self, prop, slippage=0.0, ceiling=None):
        if self.boom:
            raise RuntimeError("mirror account is broken")
        self.calls.append(("entry", prop.ticker, prop.side, prop.count))
        if not self.fill:
            return ExecutionResult("nofill", 0, "", None, "book moved")
        return ExecutionResult("filled", prop.count, "oid-entry", None, "")

    async def close_position(self, ticker, side, count, limit_price, floor=None):
        if self.boom:
            raise RuntimeError("mirror account is broken")
        self.calls.append(("exit", ticker, side, count))
        return ExecutionResult("filled", count, "oid-exit", None, "")

    async def place_resting_buy(self, ticker, side, price, count,
                                expiration_ts, client_order_id):
        self.calls.append(("add", ticker, side, count, client_order_id))
        return {"order_id": f"oid-add-{client_order_id}"}

    async def cancel_order(self, order_id):
        self.calls.append(("cancel", order_id))
        return True, "cancelled"

    async def balance_dollars(self):
        return 37.0

    async def close(self):
        self.closed = True


def build(monkeypatch, targets, primary=None, mirrors=None):
    """Wire a MirroringExecutionClient whose mirrors are FakeClients."""
    made = []
    supplied = list(mirrors or [])

    def fake_ctor(base_url, key_id, key_path):
        client = supplied.pop(0) if supplied else FakeClient()
        made.append(client)
        return client

    monkeypatch.setattr(mirror_mod, "KalshiExecutionClient", fake_ctor)
    primary = primary or FakeClient()
    wrapper = MirroringExecutionClient(
        primary, targets, "https://x", log_path="runtime/test-mirror.jsonl"
    )
    return wrapper, primary, made


async def drain(wrapper):
    for m in wrapper._mirrors:
        await m.queue.join()


T1 = MirrorTarget("m1", "k1", "p1", base_budget=0.0, base_contracts=3)
T2 = MirrorTarget("m2", "k2", "p2", base_budget=0.0, base_contracts=1)


def test_each_account_uses_its_own_size(monkeypatch):
    """The mirror's count comes from its own config, not the primary's."""
    async def run():
        wrapper, primary, made = build(monkeypatch, [T1, T2])
        # primary buys 7; the mirrors must not copy 7
        await wrapper.execute_with_take_profit(proposal(count=7))
        await drain(wrapper)
        assert made[0].calls == [("entry", "KXBTC15M-T", "yes", 3)]
        assert made[1].calls == [("entry", "KXBTC15M-T", "yes", 1)]
    asyncio.run(run())


def test_budget_can_shrink_but_never_grow_the_tier(monkeypatch):
    t = MirrorTarget("m1", "k", "p", base_budget=0.50, base_contracts=4)
    # $0.50/contract x 4 = $2.00; at 0.80 that affords 2, below the tier of 4
    assert t.entry_count(0.80) == 2
    # cheap contract: budget affords far more, tier still caps it
    assert t.entry_count(0.02) == 4


def test_max_contracts_is_a_hard_ceiling():
    t = MirrorTarget("m1", "k", "p", base_contracts=10, max_contracts=2)
    assert t.entry_count(0.80) == 2
    assert t.add_count(9) == 2


def test_upsize_is_per_account_or_follows_primary():
    own = MirrorTarget("m1", "k", "p", add_contracts=5)
    assert own.add_count(1) == 5
    follow = MirrorTarget("m2", "k", "p")
    assert follow.add_count(4) == 4


def test_unfilled_primary_entry_is_not_mirrored(monkeypatch):
    """The dangerous case: a position on another account we would never close."""
    async def run():
        wrapper, primary, made = build(
            monkeypatch, [T1], primary=FakeClient(fill=False)
        )
        result = await wrapper.execute_with_take_profit(proposal())
        assert result.status == "nofill"
        await drain(wrapper)
        assert made[0].calls == []
    asyncio.run(run())


def test_exit_closes_what_the_mirror_bought(monkeypatch):
    """Primary holds 7 and exits 7; the mirror holds 3 and must exit 3."""
    async def run():
        wrapper, primary, made = build(monkeypatch, [T1])
        await wrapper.execute_with_take_profit(proposal(count=7))
        await drain(wrapper)
        await wrapper.close_position("KXBTC15M-T", "yes", 7, 0.95, floor=0.90)
        await drain(wrapper)
        assert made[0].calls[-1] == ("exit", "KXBTC15M-T", "yes", 3)
    asyncio.run(run())


def test_exit_falls_back_to_the_ceiling_when_nothing_is_remembered(monkeypatch):
    """After a restart the held map is empty; reduce-only clamps the truth."""
    async def run():
        t = MirrorTarget("m1", "k", "p", base_contracts=2, max_contracts=6)
        wrapper, primary, made = build(monkeypatch, [t])
        await wrapper.close_position("KXBTC15M-T", "yes", 1, 0.95)
        await drain(wrapper)
        assert made[0].calls == [("exit", "KXBTC15M-T", "yes", 6)]
    asyncio.run(run())


def test_a_broken_mirror_cannot_break_the_primary(monkeypatch):
    async def run():
        good, bad = FakeClient(), FakeClient(boom=True)
        wrapper, primary, made = build(
            monkeypatch, [T1, T2], mirrors=[bad, good]
        )
        result = await wrapper.execute_with_take_profit(proposal(count=1))
        assert result.status == "filled"          # primary unaffected
        await drain(wrapper)
        assert good.calls == [("entry", "KXBTC15M-T", "yes", 1)]
    asyncio.run(run())


def test_entry_is_applied_before_its_exit(monkeypatch):
    """One worker per mirror, so an exit can never overtake its entry."""
    async def run():
        wrapper, primary, made = build(monkeypatch, [T1])
        await wrapper.execute_with_take_profit(proposal(count=1))
        await wrapper.close_position("KXBTC15M-T", "yes", 1, 0.95)
        await drain(wrapper)
        kinds = [c[0] for c in made[0].calls]
        assert kinds == ["entry", "exit"]
    asyncio.run(run())


def test_add_client_order_id_stays_deterministic_but_namespaced(monkeypatch):
    async def run():
        wrapper, primary, made = build(monkeypatch, [T1])
        await wrapper.place_resting_buy(
            "KXBTC15M-T", "yes", 0.78, 1, 123, "add:999"
        )
        await drain(wrapper)
        call = made[0].calls[0]
        assert call[0] == "add"
        assert call[4] == "add:999-m1"     # deterministic, per-account
    asyncio.run(run())


def test_reads_never_reach_a_mirror(monkeypatch):
    """Money reporting must answer for the primary account alone."""
    async def run():
        wrapper, primary, made = build(monkeypatch, [T1])
        assert await wrapper.balance_dollars() == 37.0
        await drain(wrapper)
        assert made[0].calls == []
    asyncio.run(run())


def test_cancel_forwards_the_mirrors_own_order_id(monkeypatch):
    async def run():
        wrapper, primary, made = build(monkeypatch, [T1])
        await wrapper.place_resting_buy("T", "yes", 0.78, 1, 123, "add:1")
        await drain(wrapper)
        await wrapper.cancel_order("oid-add-add:1")
        await drain(wrapper)
        assert ("cancel", "oid-add-add:1-m1") in made[0].calls
    asyncio.run(run())


def test_close_drains_then_closes_everything(monkeypatch):
    async def run():
        wrapper, primary, made = build(monkeypatch, [T1, T2])
        await wrapper.execute_with_take_profit(proposal())
        await wrapper.close()
        assert primary.closed
        assert all(m.closed for m in made)
    asyncio.run(run())


class Cfg:
    mirror_1_api_key_id = "  key-1  "
    mirror_1_private_key_path = " /keys/a.pem "
    mirror_1_base_budget = 2.0
    mirror_1_base_contracts = 3
    mirror_1_add_contracts = 2
    mirror_1_max_contracts = 4
    # half-filled block: ignored, not fatal
    mirror_2_api_key_id = "key-2"
    mirror_2_private_key_path = ""


def test_targets_from_settings_ignores_a_half_filled_block():
    targets = targets_from_settings(Cfg())
    assert len(targets) == 1
    t = targets[0]
    assert (t.name, t.api_key_id, t.private_key_path) == (
        "m1", "key-1", "/keys/a.pem"
    )
    assert (t.base_budget, t.base_contracts, t.add_contracts, t.max_contracts) == (
        2.0, 3, 2, 4
    )


def test_no_targets_means_no_mirrors(monkeypatch):
    async def run():
        wrapper, primary, made = build(monkeypatch, [])
        assert wrapper.mirror_names == []
        await wrapper.execute_with_take_profit(proposal())
        assert primary.calls == [("entry", "KXBTC15M-T", "yes", 1)]
    asyncio.run(run())
