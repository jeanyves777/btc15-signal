"""The mirror funds its market's exchange shard before trading, like the app does.

What happened, 2026-09-26: the operator's wife's mirror account held $30 in
exchange shard 0 and $0.09 in shard 2, where every 15-minute crypto market
lives. An API order can only spend its market's shard, so every mirror entry
for ~15 hours came back `insufficient_balance` - logged only as "400 Bad
Request". The Kalshi app does not have this problem because it moves exactly
the order's cost from shard 0 in the same second; her manual DOGE order at
04:13:39 carried an automatic $1.0092 transfer.

These tests run the real `KalshiExecutionClient` against a fake exchange
(httpx.MockTransport) and pin down the behaviour, including the cases that
must NOT move money.
"""

import asyncio
import json
import sys
from pathlib import Path

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from btc15_signal import execution as ex  # noqa: E402
from btc15_signal.mirror import MirrorTarget, _Mirror, targets_from_settings  # noqa: E402
from btc15_signal.store import TradeProposal  # noqa: E402

BASE = "https://external-api.kalshi.com/trade-api/v2"
TICKER = "KXBTC15M-26SEP270015-15"


class Exchange:
    """A fake Kalshi: balances per shard, transfers, orders."""

    def __init__(self, balances, shard=2, transfer_status="complete",
                 transfer_refused=False):
        self.balances = dict(balances)
        self.shard = shard
        self.transfer_status = transfer_status
        self.transfer_refused = transfer_refused
        self.transfers = []
        self.orders = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.replace("/trade-api/v2", "")
        if path.startswith("/markets/"):
            return httpx.Response(200, json={"market": {
                "ticker": TICKER, "exchange_index": self.shard}})
        if path == "/portfolio/balance":
            return httpx.Response(200, json={"balance_breakdown": [
                {"exchange_index": k, "balance": f"{v:.4f}"}
                for k, v in self.balances.items()]})
        if path == ex.FUND_TRANSFER_PATH and request.method == "POST":
            if self.transfer_refused:
                return httpx.Response(400, json={"error": {"code": "nope"}})
            body = json.loads(request.content)
            self.transfers.append(body)
            dollars = body["amount"] / 10000
            src, dst = body["source_exchange_shard"], body["destination_exchange_shard"]
            self.balances[src] = self.balances.get(src, 0.0) - dollars
            self.balances[dst] = self.balances.get(dst, 0.0) + dollars
            return httpx.Response(200, json={"transfer_id": "t-1"})
        if path.startswith(ex.FUND_STATUS_PATH):
            return httpx.Response(200, json={"transfer": {
                "transfer_id": "t-1", "status": self.transfer_status}})
        if path == "/portfolio/events/orders" and request.method == "POST":
            body = json.loads(request.content)
            self.orders.append(body)
            cost = float(body["count"]) * float(body["price"])
            if cost > self.balances.get(self.shard, 0.0) + 1e-9:
                return httpx.Response(400, json={"error": {
                    "code": "insufficient_balance",
                    "message": "insufficient balance"}})
            return httpx.Response(201, json={
                "order_id": "o-1", "fill_count": body["count"],
                "remaining_count": "0.00"})
        return httpx.Response(404, json={"error": {"code": "not_found"}})


@pytest.fixture
def key_path(tmp_path):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    p = tmp_path / "k.pem"
    p.write_bytes(key.private_bytes(serialization.Encoding.PEM,
                                    serialization.PrivateFormat.PKCS8,
                                    serialization.NoEncryption()))
    return str(p)


@pytest.fixture(autouse=True)
def instant_sleep(monkeypatch):
    async def no_wait(_s):
        return None
    monkeypatch.setattr(ex, "_sleep", no_wait)


def client_for(exchange, key_path, auto_fund=True):
    c = ex.KalshiExecutionClient(BASE, "key", key_path)
    c.client = httpx.AsyncClient(transport=httpx.MockTransport(exchange.handler))
    c.auto_fund = auto_fund
    return c


def proposal(side="UP", limit=0.85, count=1):
    return TradeProposal(id="p", strategy="primary", window_open=0,
                         ticker=TICKER, side=side, entry_limit=limit,
                         take_profit=0.0, count=count, expires_at=0,
                         close_ms=0, status="executing")


def run(coro):
    return asyncio.run(coro)


def test_the_real_failure_is_reproduced_without_funding(key_path):
    """The bug: $30 in shard 0, $0.09 in shard 2, and the order is refused."""
    xc = Exchange({0: 30.37, 2: 0.09})
    c = client_for(xc, key_path, auto_fund=False)
    with pytest.raises(httpx.HTTPStatusError) as err:
        run(c.execute_with_take_profit(proposal(), 0.05, 0.95))
    assert "insufficient_balance" in str(err.value), \
        "Kalshi's reason must survive into the error, not just '400'"
    assert xc.transfers == []
    run(c.close())


def test_the_mirror_funds_the_shortfall_and_the_order_fills(key_path):
    xc = Exchange({0: 30.37, 2: 0.09})
    c = client_for(xc, key_path)
    result = run(c.execute_with_take_profit(proposal(), 0.05, 0.95))
    assert result.filled_count == 1
    assert len(xc.transfers) == 1
    t = xc.transfers[0]
    assert (t["source_exchange_shard"], t["destination_exchange_shard"]) == (0, 2)
    # 1 contract at the 0.95 ceiling + 0.02 fee allowance = 0.97 needed,
    # 0.09 already there -> move 0.88, rounded UP to the cent.
    assert t["amount"] == 8800
    assert "moved $0.88" in c.last_funding_note
    run(c.close())


def test_nothing_moves_when_the_shard_can_already_pay(key_path):
    xc = Exchange({0: 30.0, 2: 10.11})
    c = client_for(xc, key_path)
    run(c.execute_with_take_profit(proposal(), 0.05, 0.95))
    assert xc.transfers == [], "a funded shard must not trigger a transfer"
    assert c.last_funding_note == ""
    run(c.close())


def test_only_the_shortfall_moves_never_a_float(key_path):
    xc = Exchange({0: 30.0, 2: 1.50})
    c = client_for(xc, key_path)
    run(c.execute_with_take_profit(proposal(count=2), 0.05, 0.95))
    # 2 x (0.95 + 0.02) = 1.94 needed, 1.50 there -> 0.44
    assert xc.transfers[0]["amount"] == 4400
    run(c.close())


def test_a_down_order_is_funded_at_its_own_price_not_the_yes_complement(key_path):
    """DOWN at a 0.90 ceiling costs 0.90 a contract, although the order it
    sends is an ASK on YES at 0.10. Funding off the YES price would move a
    tenth of what is needed and the order would still be refused."""
    xc = Exchange({0: 30.0, 2: 0.0})
    c = client_for(xc, key_path)
    run(c.execute_with_take_profit(proposal(side="DOWN", limit=0.85), 0.05, 0.90))
    assert xc.transfers[0]["amount"] == 9200     # 0.90 + 0.02
    run(c.close())


def test_it_will_not_move_money_the_source_shard_does_not_have(key_path):
    xc = Exchange({0: 0.30, 2: 0.09})
    c = client_for(xc, key_path)
    with pytest.raises(httpx.HTTPStatusError) as err:
        run(c.execute_with_take_profit(proposal(), 0.05, 0.95))
    assert xc.transfers == []
    assert "insufficient_balance" in str(err.value)
    assert "has only $0.30 to move" in c.last_funding_note
    run(c.close())


def test_an_unconfirmed_transfer_is_not_trusted(key_path):
    xc = Exchange({0: 30.0, 2: 0.09}, transfer_status="pending")
    c = client_for(xc, key_path)
    ok, note = run(c.ensure_funds(TICKER, 1, 0.95))
    assert ok is False and "still pending" in note
    run(c.close())


def test_a_refused_transfer_is_reported_not_raised(key_path):
    xc = Exchange({0: 30.0, 2: 0.09}, transfer_refused=True)
    c = client_for(xc, key_path)
    ok, note = run(c.ensure_funds(TICKER, 1, 0.95))
    assert ok is False and "transfer refused" in note
    run(c.close())


def test_the_primary_default_is_off(key_path):
    """What the bot may reach on the operator's own account is a sizing call,
    so a plain client never moves money."""
    c = ex.KalshiExecutionClient(BASE, "key", key_path)
    assert c.auto_fund is False
    run(c.close())


def test_resting_adds_are_funded_too(key_path):
    xc = Exchange({0: 30.0, 2: 0.0})
    c = client_for(xc, key_path)
    run(c.place_resting_buy(TICKER, "UP", 0.80, 1, 2_000_000_000, "coid"))
    assert xc.transfers[0]["amount"] == 8200     # 0.80 + 0.02
    run(c.close())


def test_mirror_targets_default_to_funding_on_and_can_be_turned_off():
    class S:
        mirror_1_api_key_id = "k"
        mirror_1_private_key_path = "p"
    assert targets_from_settings(S())[0].auto_fund is True

    class Off(S):
        mirror_1_auto_fund = False
    assert targets_from_settings(Off())[0].auto_fund is False


def test_the_mirror_worker_switches_funding_on_for_its_client(key_path):
    m = _Mirror(MirrorTarget(name="m1", api_key_id="k",
                             private_key_path=key_path), BASE, lambda *a: None)
    assert m.client.auto_fund is True
    run(m.client.close())


# ------------------------------------------------ the $2 recovery on the mirror

def test_a_recovery_entry_is_sized_from_the_mirrors_own_two_dollar_budget():
    t = MirrorTarget(name="m1", api_key_id="k", private_key_path="p",
                     base_budget=1.0, base_contracts=1, max_contracts=2,
                     recovery_budget=2.0)
    assert t.entry_count(0.75) == 1          # an ordinary entry stays at base
    assert t.recovery_count(0.75) == 2       # $2 at 0.75 buys 2
    assert t.recovery_count(0.70) == 2       # the band floor: still 2
    assert t.recovery_count(0.30) == 2       # the account's cap still binds


def test_the_mirror_follows_the_recovery_mark_and_only_then(key_path):
    class FakeClient:
        last_funding_note = ""

        def __init__(self):
            self.counts = []

        async def execute_with_take_profit(self, proposal, slippage, ceiling):
            self.counts.append(proposal.count)
            return ex.ExecutionResult("filled", proposal.count, "o", None, "ok")

    m = _Mirror(MirrorTarget(name="m1", api_key_id="k", private_key_path=key_path,
                             base_budget=1.0, base_contracts=1, max_contracts=2,
                             recovery_budget=2.0), BASE, lambda *a: None)
    run(m.client.close())
    m.client = FakeClient()
    args = {"proposal": proposal(limit=0.75), "filled_count": 2,
            "slippage": 0.05, "ceiling": 0.95}
    run(m._apply("entry", dict(args, recovery=True)))
    run(m._apply("entry", dict(args, recovery=False)))
    run(m._apply("entry", args))            # no mark at all = ordinary entry
    assert m.client.counts == [2, 1, 1]


def test_the_recovery_mark_is_read_once_and_cannot_leak_to_the_next_entry():
    from btc15_signal.mirror import MirroringExecutionClient

    class Primary:
        async def execute_with_take_profit(self, proposal, slippage, ceiling):
            return ex.ExecutionResult("filled", 1, "o", None, "ok")

    mc = MirroringExecutionClient(Primary(), [], BASE)
    seen = []
    mc._dispatch = lambda kind, a: seen.append(a["recovery"])
    mc.entry_is_recovery = True
    run(mc.execute_with_take_profit(proposal(), 0.05, 0.95))
    run(mc.execute_with_take_profit(proposal(), 0.05, 0.95))
    assert seen == [True, False]


def test_the_wifes_account_is_configured_for_a_two_dollar_recovery():
    from btc15_signal.config import Settings
    assert Settings().mirror_1_recovery_budget == 2.0
