"""THE CHASE (operator, 2026-10-05: "whether we take small win or not it changes nothing,
it's better to take it than just letting it go"; FINDINGS 152-153). A $ order that missed
because the price ran the signal's way past its cap is bought at the MOVED price: from 10 s
after the miss, up to 93c, at most 3 tries, while the price is still on the signal's side
with >= 2 min left - instead of waiting 60 s for the old cap to come back.
"""

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from btc15_signal import main  # noqa: E402
from btc15_signal.config import Settings  # noqa: E402
from btc15_signal.execution import ExecutionResult  # noqa: E402
from btc15_signal.store import Store  # noqa: E402
from btc15_signal.validation import contracts_for_budget  # noqa: E402

W = 1_790_600_400_000
TICKER = "KXBTC15M-26SEP281215-15"
MISS = W + 240_000                       # the first order, decided 4 min in, missed


class Client:
    def __init__(self, fills):
        self.orders, self.fills = [], list(fills)

    async def execute_with_take_profit(self, order, slippage, ceiling=None):
        self.orders.append((order.side, order.count, ceiling))
        self.prices = getattr(self, "prices", []) + [order.entry_limit]
        n = self.fills.pop(0) if self.fills else order.count
        return ExecutionResult("filled" if n else "unfilled", n, f"oid-{len(self.orders)}",
                               None, "filled" if n else "No fill; the book moved")

    async def fill_detail(self, order_id, side):
        return None


@pytest.fixture(autouse=True)
def _fast(monkeypatch):
    async def instant(_s):
        return None
    monkeypatch.setattr(main.asyncio, "sleep", instant)
    main.ALLSIGNAL_WAIT.clear()
    main.ALLSIGNAL_WAIT_NOTE.clear()


def setup(tmp_path, fills, chase=0.93):
    store = Store(str(tmp_path / "btc15.db"))
    store.set_setting("auto_trade_enabled", 1.0, 1)
    settings = Settings(kalshi_series="KXBTC15M", database_path=str(tmp_path / "btc15.db"),
                        allsignal_instruments="BTC", allsignal_stake=25.0,
                        allsignal_chase_max=chase)
    client = Client(fills)

    async def go():                          # DOWN at 0.68: cap 0.73, and it misses
        main.spawn_allsignal(store, settings, client, TICKER, "DOWN", 0.68, W, MISS)
        await asyncio.gather(*list(main.ALLSIGNAL_TASKS))
    asyncio.run(go())
    assert store.db.execute("SELECT status FROM allsignal_trades").fetchone()[0] == "unfilled"
    return store, settings, client


def poll(store, settings, client, ask, bps, now, remaining=600):
    """`bps`: how far the reference sits below the target (DOWN's side)."""
    async def go():
        main.allsignal_retry_poll(
            store, settings, client, SimpleNamespace(ticker=TICKER, ask=lambda side: ask),
            SimpleNamespace(price=100_000.0 * (1 - bps / 10_000), target=100_000.0),
            W, remaining, now)
        await asyncio.gather(*list(main.ALLSIGNAL_TASKS))
    asyncio.run(go())


def test_a_miss_is_bought_at_the_moved_price_10s_later(tmp_path):
    store, settings, client = setup(tmp_path, fills=(0, 31))
    poll(store, settings, client, 0.80, 6.0, MISS + 5_000)          # only 5 s: wait
    assert len(client.orders) == 1
    poll(store, settings, client, 0.80, 6.0, MISS + 11_000)
    assert len(client.orders) == 2
    side, count, cap = client.orders[1]
    assert side == "DOWN" and cap == 0.85, "the moved price + the usual 5c, under 93c"
    assert count == contracts_for_budget(25.0, 0.80), "the same $25 at the moved price"
    status, limit = store.db.execute("SELECT status, limit_price FROM allsignal_trades").fetchone()
    assert status == "filled" and limit == 0.85
    assert "chased: bought at the moved price 11s after the signal" in main.ALLSIGNAL_WAIT_NOTE[W]


def test_never_above_the_ceiling(tmp_path):
    store, settings, client = setup(tmp_path, fills=(0, 31))
    poll(store, settings, client, 0.94, 6.0, MISS + 11_000)
    poll(store, settings, client, 0.94, 6.0, MISS + 70_000)
    assert len(client.orders) == 1, "94c is over the 93c ceiling - let it go"
    poll(store, settings, client, 0.92, 6.0, MISS + 80_000)
    assert client.orders[-1][2] == 0.93, "a cap never above the ceiling"


def test_only_while_still_on_the_signals_side_with_time_left(tmp_path):
    store, settings, client = setup(tmp_path, fills=(0, 31))
    poll(store, settings, client, 0.80, -2.0, MISS + 11_000)         # crossed the line
    poll(store, settings, client, 0.80, 6.0, MISS + 20_000, remaining=110)   # < 2 min left
    assert len(client.orders) == 1


def test_at_most_three_chases(tmp_path):
    """A chase only misses when the price ran past ITS cap too: each try follows the price
    up - 76c, 82c, 88c - and a fourth (94c) is never sent."""
    store, settings, client = setup(tmp_path, fills=(0, 0, 0, 0, 0), chase=0.99)
    for k, ask in enumerate((0.76, 0.82, 0.88, 0.94), start=1):
        poll(store, settings, client, ask, 6.0, MISS + 11_000 * k)
    assert [o[2] for o in client.orders] == [0.73, 0.81, 0.87, 0.93], "the miss and three chases"


def test_off_it_is_the_old_retry(tmp_path):
    store, settings, client = setup(tmp_path, fills=(0, 7), chase=0.0)
    poll(store, settings, client, 0.80, 6.0, MISS + 11_000)
    poll(store, settings, client, 0.80, 6.0, MISS + 70_000)
    assert len(client.orders) == 1, "above the cap: no retry without the chase"
    poll(store, settings, client, 0.70, 6.0, MISS + 80_000)           # back under the cap
    assert len(client.orders) == 2 and client.orders[1][2] == 0.73, "the same cap, as before"


def test_a_price_back_under_the_cap_keeps_the_old_cap_and_timing(tmp_path):
    store, settings, client = setup(tmp_path, fills=(0, 7))
    poll(store, settings, client, 0.70, 6.0, MISS + 11_000)
    assert len(client.orders) == 1, "under the cap it is the 60 s retry, not a chase"
    poll(store, settings, client, 0.70, 6.0, MISS + 61_000)
    assert len(client.orders) == 2 and client.orders[1][2] == 0.73


def test_after_a_loss_the_cushion_still_decides(tmp_path):
    store, settings, client = setup(tmp_path, fills=(0, 31))
    prev = W - 900_000
    store.allsignal_claim(prev, "KXBTC15M-PREV", "UP", 0.7, 1, 0.75, prev + 60_000)
    store.allsignal_finish(prev, "filled", filled=1, fill_price=0.7)
    store.db.execute("UPDATE allsignal_trades SET won=0 WHERE window_open=?", (prev,))
    store.db.commit()
    poll(store, settings, client, 0.80, 3.0, MISS + 11_000)          # 3 bps < the 5 needed
    assert len(client.orders) == 1
    poll(store, settings, client, 0.80, 6.0, MISS + 22_000)
    assert len(client.orders) == 2


def test_the_settings():
    d = Settings.model_fields
    assert d["allsignal_chase_max"].default == 0.0, "off unless set"
    assert d["allsignal_chase_after_s"].default == 10
    assert d["allsignal_chase_attempts"].default == 3


def test_the_chase_is_sent_at_the_moved_price_so_every_account_sizes_right(tmp_path):
    """Review 2026-10-05: the mirrors size their copies by the order's entry price; sent
    at the signal's old 68c a chased order at 80c over-spent their stakes by ~50%."""
    store, settings, client = setup(tmp_path, fills=(0, 31))
    poll(store, settings, client, 0.80, 6.0, MISS + 11_000)
    assert client.prices[-1] == 0.80, "the entry price every account sizes by"
    assert client.orders[-1][1] == contracts_for_budget(25.0, 0.80)
    for budget in (3.0, 2.0):                                   # each mirror's own size
        assert contracts_for_budget(budget, client.prices[-1]) * 0.85 <= budget + 0.25


def test_a_chase_due_poll_is_urgent_from_the_miss(tmp_path):
    store, settings, client = setup(tmp_path, fills=(0, 31))
    assert main.allsignal_urgent(store, settings, MISS + 5_000) is True
    off = Settings(kalshi_series="KXBTC15M", database_path=settings.database_path,
                   allsignal_instruments="BTC", allsignal_stake=25.0, allsignal_chase_max=0.0)
    assert main.allsignal_urgent(store, off, MISS + 5_000) is False, "without it: from 45 s"
    assert main.allsignal_urgent(store, off, MISS + 46_000) is True
