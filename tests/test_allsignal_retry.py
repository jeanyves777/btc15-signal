"""A $ order that missed is retried 60 s later if everything still lines up, and
the order goes out without waiting behind Kalshi calls (operator, 2026-09-30:
"retry 60 after check is everything still aligned", "fix the cause")."""

import asyncio
import inspect
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from btc15_signal import main  # noqa: E402
from btc15_signal.config import Settings  # noqa: E402
from btc15_signal.daily_profit import DailyProfitGuard  # noqa: E402
from btc15_signal.execution import ExecutionResult, KalshiExecutionClient  # noqa: E402
from btc15_signal.store import Store  # noqa: E402

W = 1_790_600_400_000
TICKER = "KXBTC15M-26SEP281215-15"


class Client:
    def __init__(self, fills=(0,)):
        self.orders, self.fills = [], list(fills)

    async def execute_with_take_profit(self, order, slippage, ceiling=None):
        self.orders.append((order.side, order.count, ceiling))
        n = self.fills.pop(0) if self.fills else order.count
        status = "filled" if n else "unfilled"
        note = "filled" if n else "No fill at 73%; the book moved before the order landed"
        return ExecutionResult(status, n, f"oid-{len(self.orders)}", None, note)

    async def fill_detail(self, order_id, side):
        return None


@pytest.fixture(autouse=True)
def _fast(monkeypatch):
    async def instant(_s):
        return None
    monkeypatch.setattr(main.asyncio, "sleep", instant)
    main.ALLSIGNAL_WAIT.clear()
    main.ALLSIGNAL_WAIT_NOTE.clear()


def setup(tmp_path, fills=(0,)):
    store = Store(str(tmp_path / "btc15.db"))
    store.set_setting("auto_trade_enabled", 1.0, 1)
    settings = Settings(kalshi_series="KXBTC15M", database_path=str(tmp_path / "btc15.db"),
                        allsignal_instruments="BTC", allsignal_stake=5.0)
    client = Client(fills)
    # the first attempt, decided at W + 240 s, missed
    main.spawn_allsignal(store, settings, None, TICKER, "DOWN", 0.68, W, W + 240_000)
    return store, settings, client


def miss_first(store, settings, client):
    async def go():
        main.spawn_allsignal(store, settings, client, TICKER, "DOWN", 0.68, W, W + 240_000)
        await asyncio.gather(*list(main.ALLSIGNAL_TASKS))
    asyncio.run(go())


def market(ask=0.68):
    return SimpleNamespace(ticker=TICKER, ask=lambda side: ask)


def snap(bps_down):
    """The reference price `bps_down` below the target: DOWN's cushion."""
    return SimpleNamespace(price=100_000.0 * (1 - bps_down / 10_000), target=100_000.0)


def poll(store, settings, client, ask, bps, now, remaining):
    async def go():
        main.allsignal_retry_poll(store, settings, client, market(ask), snap(bps), W,
                                  remaining, now)
        await asyncio.gather(*list(main.ALLSIGNAL_TASKS))
    asyncio.run(go())


def test_a_miss_is_retried_60s_later_at_the_same_cap_once_aligned(tmp_path):
    store, settings, client = setup(tmp_path, fills=(0, 7))
    miss_first(store, settings, client)
    assert store.db.execute("SELECT status FROM allsignal_trades").fetchone()[0] == "unfilled"
    poll(store, settings, client, 0.68, 3.0, W + 280_000, 620)     # only 40 s later
    assert len(client.orders) == 1
    poll(store, settings, client, 0.80, 3.0, W + 305_000, 595)     # 65 s, but above the cap
    assert len(client.orders) == 1
    poll(store, settings, client, 0.70, -1.0, W + 315_000, 585)    # price crossed the line
    assert len(client.orders) == 1
    poll(store, settings, client, 0.70, 3.0, W + 325_000, 575)     # aligned: send again
    assert len(client.orders) == 2 and client.orders[1][2] == client.orders[0][2], "same cap"
    status, filled = store.db.execute("SELECT status, filled FROM allsignal_trades").fetchone()
    assert status == "filled" and filled == 7
    assert "filled on retry 1" in main.ALLSIGNAL_WAIT_NOTE[W]


def test_each_further_miss_waits_another_60s_and_stops_at_2_min(tmp_path):
    store, settings, client = setup(tmp_path, fills=(0, 0, 0))
    miss_first(store, settings, client)
    poll(store, settings, client, 0.68, 3.0, W + 300_000, 600)
    poll(store, settings, client, 0.68, 3.0, W + 330_000, 570)     # 30 s after retry 1
    assert len(client.orders) == 2
    poll(store, settings, client, 0.68, 3.0, W + 361_000, 539)
    assert len(client.orders) == 3
    poll(store, settings, client, 0.68, 3.0, W + 790_000, 110)     # under 2 min: done
    assert len(client.orders) == 3


def test_failed_paused_or_skipped_orders_are_not_retried(tmp_path):
    for status in ("failed", "paused", "skipped"):
        (tmp_path / status).mkdir()
        store, settings, client = setup(tmp_path / status)
        store.allsignal_finish(W, status, order_id="x", note="whatever")
        poll(store, settings, client, 0.68, 3.0, W + 400_000, 500)
        assert client.orders == [], status


def test_the_order_path_reads_no_balance_when_the_background_read_is_fresh():
    c = object.__new__(KalshiExecutionClient)
    c.auto_fund, c.fund_source_shard = True, 0
    c._shard_of = {TICKER: 2}
    c._balance_cache = (time.time(), {0: 50.0, 2: 20.0})
    c.shard_balances = AsyncMock(side_effect=AssertionError("no Kalshi call in front of an order"))
    ok, note = asyncio.run(c.ensure_funds(TICKER, 7, 0.70))
    assert ok and note == ""
    assert c._balance_cache[1][2] < 20.0, "reserved locally"


def test_the_target_check_uses_fresh_figures_without_a_broker_read(tmp_path):
    client = SimpleNamespace(account_value=AsyncMock(return_value=100))
    g = DailyProfitGuard(tmp_path / "g.db", "primary", "You", client, 0.08)
    g.pages = AsyncMock(return_value=[])
    asyncio.run(g.refresh(force=True))
    g.pages = AsyncMock(side_effect=AssertionError("no broker read on the order path"))
    assert asyncio.run(g.block_reason(TICKER)) == ""


def test_the_order_goes_out_before_the_rest_of_the_poll():
    path = " ".join(inspect.getsource(main.primary_signal).split())
    i = path.index("allsignal_on_alert(store, settings, trader, contract, prediction.side,")
    assert path.index("await await_order_sent(store, opened)", i) - i < 400
    loop = " ".join(inspect.getsource(main.service).split())
    assert "allsignal_retry_poll(store, settings, trader, contract, snapshot," in loop
    assert "prewarm_order_path(trader, settings, contract.ticker, store)" in loop
    assert "await await_order_sent(store, opened)" in loop


def test_the_alert_waits_for_the_order_to_reach_kalshi_not_one_loop_turn(tmp_path):
    """Review 2026-09-30: httpx needs several loop turns to send; one sleep(0)
    left the order behind the alert's synchronous work."""
    from btc15_signal import execution

    store = Store(str(tmp_path / "btc15.db"))
    store.allsignal_claim(W, TICKER, "DOWN", 0.68, 7, 0.73, W + 240_000)
    order = []

    async def slow_client():
        for _ in range(7):                  # httpx's own yields before the bytes leave
            await asyncio.sleep(0)
        order.append("sent")
        execution.ORDER_SENT["allsignal:%d" % W].set()

    async def go():
        task = asyncio.get_running_loop().create_task(slow_client())
        await main.await_order_sent(store, W)
        order.append("alert continues")
        await task
    asyncio.run(go())
    assert order == ["sent", "alert continues"]


def test_a_retry_fill_is_found_by_the_reconcile_around_its_own_time(tmp_path):
    store = Store(str(tmp_path / "btc15.db"))
    store.allsignal_claim(W, TICKER, "DOWN", 0.68, 7, 0.73, W + 240_000)
    store.allsignal_finish(W, "failed", note="ReadTimeout on retry 1")
    store.allsignal_mark_attempt(W, W + 305_000, 1)
    store.db.execute(
        "INSERT INTO fills (fill_id, ticker, order_id, action, side, count, yes_price, "
        "no_price, fee_cost, is_taker, filled_ms, window_ms, synced_at) "
        "VALUES ('f1', ?, 'oid-retry', 'sell', 'no', 7, 0.31, 0.69, 0.1, 1, ?, ?, ?)",
        (TICKER, W + 306_000, W, W + 400_000))
    store.db.commit()
    assert store.allsignal_reconcile(W + 1_000_000) == 1
    status, filled = store.db.execute(
        "SELECT status, filled FROM allsignal_trades").fetchone()
    assert status == "filled" and filled == 7


def test_a_refused_order_on_a_stale_cache_tops_up_and_is_sent_once_more():
    import httpx

    c = object.__new__(KalshiExecutionClient)
    c.auto_fund, c.fund_source_shard = True, 0
    c._shard_of = {TICKER: 2}
    c._balance_cache = (time.time(), {0: 50.0, 2: 20.0})   # stale: really $1
    c.daily_profit_guard = None
    c.allowed_entry_series = None
    c.ensure_funds = AsyncMock(return_value=(True, ""))
    calls = []

    async def post(path, body):
        calls.append(body["client_order_id"])
        if len(calls) == 1:
            raise httpx.HTTPStatusError(
                '400 {"error":{"code":"insufficient_balance"}}',
                request=httpx.Request("POST", "http://x"), response=httpx.Response(400))
        return {"fill_count": "7", "order_id": "oid"}
    c._post = post
    order = SimpleNamespace(id="allsignal:1", ticker=TICKER, side="DOWN", entry_limit=0.68,
                            count=7, take_profit=0.0)
    result = asyncio.run(c.execute_with_take_profit(order, 0.05, ceiling=0.73))
    assert result.filled_count == 7 and len(calls) == 2 and calls[0] != calls[1]
    assert c.ensure_funds.await_count == 2 and c._balance_cache is None


def test_a_background_read_does_not_erase_a_newer_reservation():
    """An order reserved while the balance read is in flight is not in Kalshi's
    answer yet; the cache must still subtract it."""
    c = object.__new__(KalshiExecutionClient)
    c._reservations = []
    c._balance_cache = None

    class Resp:
        def raise_for_status(self):
            return None

        def json(self):
            return {"balance_breakdown": [{"exchange_index": 2, "balance": "20.0"}]}

    async def get(*a, **k):
        c._reserve(2, 5.0)              # an order goes out mid-read
        return Resp()
    c.client = SimpleNamespace(get=get)
    c.base_url = "http://x"
    c._headers = lambda *a: {}
    raw = asyncio.run(c.shard_balances())
    assert raw[2] == 20.0                      # what Kalshi said
    assert c._balance_cache[1][2] == 15.0      # what the order path may spend


def test_prewarm_remembers_windows_in_order_not_by_name(monkeypatch):
    """Re-check 2026-09-30: sorted() put 'OCT' before 'SEP' and re-warmed every poll."""
    main.PREWARMED.clear()
    monkeypatch.setattr(main, "allsignal_on", lambda store, settings: True)
    runs = []

    class C:
        auto_fund = False

        async def market_shard(self, ticker):
            runs.append(ticker)

    async def go():
        for t in ("KXBTC15M-26SEP302330-30", "KXBTC15M-26SEP302345-45",
                  "KXBTC15M-26OCT010000-00", "KXBTC15M-26OCT010015-15"):
            for _ in range(5):                       # five polls in each window
                main.prewarm_order_path(C(), SimpleNamespace(allsignal_stake=5.0), t, None)
            await asyncio.gather(*list(main.ALLSIGNAL_TASKS))
    asyncio.run(go())
    assert len(runs) == 4, runs


def test_prewarm_only_where_the_dollar_strategy_is_on(monkeypatch):
    main.PREWARMED.clear()
    monkeypatch.setattr(main, "allsignal_on", lambda store, settings: False)
    main.prewarm_order_path(object(), SimpleNamespace(allsignal_stake=5.0), TICKER, None)
    assert TICKER not in main.PREWARMED


def test_the_reconcile_never_takes_an_exit_or_a_refused_order(tmp_path):
    store = Store(str(tmp_path / "btc15.db"))
    for window, note in ((W, "retry 1"), (W + 900_000, '400 from /portfolio/events/orders: insufficient_balance')):
        tk = TICKER if window == W else "KXBTC15M-26SEP281230-30"
        store.allsignal_claim(window, tk, "DOWN", 0.68, 7, 0.73, window + 240_000)
        store.allsignal_finish(window, "failed", note=note)
        store.allsignal_mark_attempt(window, window + 305_000, 1)
        # an EXIT of a DOWN position is booked buy/yes - not the $ entry
        store.db.execute(
            "INSERT INTO fills (fill_id, ticker, order_id, action, side, count, yes_price, "
            "no_price, fee_cost, is_taker, filled_ms, window_ms, synced_at) "
            "VALUES (?, ?, 'main-exit', 'buy', 'yes', 3, 0.03, 0.97, 0.0, 1, ?, ?, ?)",
            (f"x{window}", tk, window + 320_000, window, window + 400_000))
    store.db.commit()
    store.allsignal_reconcile(W + 3_000_000)
    statuses = [r[0] for r in store.db.execute(
        "SELECT status FROM allsignal_trades ORDER BY window_open")]
    assert statuses == ["unfilled", "unfilled"]


# ------------------------------------ trading first when an entry is imminent

def _urgent_setup(tmp_path):
    store = Store(str(tmp_path / "btc15.db"))
    store.set_setting("auto_trade_enabled", 1.0, 1)
    settings = Settings(kalshi_series="KXBTC15M", database_path=str(tmp_path / "btc15.db"),
                        allsignal_instruments="BTC")
    return store, settings


def test_urgent_while_the_alert_is_pending_in_the_entry_range(tmp_path):
    store, settings = _urgent_setup(tmp_path)
    assert main.allsignal_urgent(store, settings, W + 250_000) is True     # 650 s left
    assert main.allsignal_urgent(store, settings, W + 60_000) is False     # 840 s left
    assert main.allsignal_urgent(store, settings, W + 700_000) is False    # 200 s left
    store.record_alert("primary", W, W + 250_000)
    assert main.allsignal_urgent(store, settings, W + 260_000) is False, "alert already fired"


def test_urgent_while_a_retry_is_due_or_a_wait_runs(tmp_path):
    store, settings = _urgent_setup(tmp_path)
    store.allsignal_claim(W, TICKER, "DOWN", 0.68, 7, 0.73, W + 250_000)
    store.allsignal_finish(W, "unfilled", order_id="o", note="No fill")
    assert main.allsignal_urgent(store, settings, W + 270_000) is False    # 20 s later
    assert main.allsignal_urgent(store, settings, W + 300_000) is True     # retry nearly due
    store.allsignal_finish(W, "filled", order_id="o", filled=7, fill_price=0.7)
    assert main.allsignal_urgent(store, settings, W + 300_000) is False
    main.ALLSIGNAL_WAIT[W + 900_000] = {"opened": W + 900_000}
    assert main.allsignal_urgent(store, settings, W + 900_000 + 500_000) is True
    main.ALLSIGNAL_WAIT.clear()


def test_not_urgent_where_the_dollar_strategy_is_off(tmp_path):
    store, settings = _urgent_setup(tmp_path)
    store.set_setting("auto_trade_enabled", 0.0, 2)
    assert main.allsignal_urgent(store, settings, W + 250_000) is False


def test_the_poll_reads_the_price_before_its_chores_when_urgent():
    loop = " ".join(inspect.getsource(main.service).split())
    assert "urgent = allsignal_urgent(store, settings, now_ms)" in loop
    assert "(not urgent and now_ms - SETTLEMENT_SYNC.get(\"at\", 0) >= 60_000)" in loop


def test_telegram_reuses_one_connection():
    """2026-09-30: a new client per call loaded the certificate bundle each time
    (270-470 ms of blocked event loop per Telegram call)."""
    from btc15_signal.telegram import Telegram

    tg = Telegram("token", "chat", False)
    first = tg._http()
    assert tg._http() is first
    asyncio.run(tg.close())
    assert tg._http() is not first, "a closed client is replaced"
    asyncio.run(tg.close())


def test_one_telegram_reader_at_a_time_and_a_bounded_sync_delay():
    loop = " ".join(inspect.getsource(main.service).split())
    assert "if busy: pass" in loop
    assert "task.add_done_callback(_report_background_failure)" in loop
    assert ">= 90_000" in loop, "the sync waits at most 90 s"
    assert "await telegram.close()" in loop
