import asyncio
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from btc15_signal.capital import NY
from btc15_signal.daily_profit import DailyProfitGuard, footer, realised_events, timestamp
from btc15_signal.execution import KalshiExecutionClient
from btc15_signal.telegram import Telegram

TICKER = "KXBTC15M-26SEP291830-30"


def fill(at, side, price, count=1, fee=0.01, action="buy"):
    return dict(
        ticker=TICKER,
        created_time=at,
        side=side,
        action=action,
        count_fp=str(count),
        fee_cost=str(fee),
        yes_price_dollars=str(price if side == "yes" else 1 - price),
        no_price_dollars=str(price if side == "no" else 1 - price),
    )


def test_cashout_and_settlement_are_not_double_counted():
    fills = [fill("2026-09-29T22:15:00Z", "yes", 0.7), fill("2026-09-29T22:20:00Z", "no", 0.02)]
    settlements = [
        dict(
            ticker=TICKER,
            settled_time="2026-09-29T22:30:08Z",
            yes_count_fp="1",
            no_count_fp="1",
            revenue=0,
            yes_total_cost_dollars=".7",
            no_total_cost_dollars=".02",
            fee_cost=".02",
        )
    ]
    events = realised_events(fills, settlements)
    assert events[0][1] == pytest.approx(0.26)
    assert sum(e[1] for e in events) == pytest.approx(0.26)


def test_partial_exit_losing_remainder_and_fees():
    fills = [
        fill("2026-09-29T22:15:00Z", "yes", 0.7, 2, 0.02),
        fill("2026-09-29T22:20:00Z", "yes", 0.98, action="sell"),
    ]
    settlements = [
        dict(
            ticker=TICKER,
            settled_time="2026-09-29T22:30:08Z",
            yes_count_fp="2",
            no_count_fp="1",
            revenue=0,
            yes_total_cost_dollars="1.4",
            no_total_cost_dollars=".02",
            fee_cost=".03",
        )
    ]
    events = realised_events(fills, settlements)
    assert events[0][1] == pytest.approx(0.26)
    assert events[1][1] == pytest.approx(-0.71)
    assert sum(e[1] for e in events) == pytest.approx(-0.45)


def test_v2_sell_means_acquire_no_not_sell_no():
    entry = fill("2026-09-29T22:15:00Z", "no", 0.85, action="sell")
    entry.update(book_side="ask", outcome_side="no")
    exit_fill = fill("2026-09-29T22:20:00Z", "yes", 0.005)
    exit_fill.update(book_side="bid", outcome_side="yes")
    events = realised_events([entry, exit_fill], [])
    assert sum(e[1] for e in events) == pytest.approx(0.125)


def test_pause_latches_across_restart_and_resets_next_day(tmp_path, monkeypatch):
    clock = [timestamp("2026-09-29T22:00:00Z")]
    monkeypatch.setattr("btc15_signal.daily_profit.time.time", lambda: clock[0] / 1000)
    client = SimpleNamespace(account_value=AsyncMock(return_value=100))
    path = tmp_path / "guard.db"
    g = DailyProfitGuard(path, "primary", "You", client)
    g.pages = AsyncMock(return_value=[])
    asyncio.run(g.refresh(force=True))
    first = g.state()
    assert first["basis"] == "activation"
    assert first["start_ms"] == clock[0]
    clock[0] += 60000
    monkeypatch.setattr(
        "btc15_signal.daily_profit.realised_events", lambda *_: [(clock[0] - 10, 3.1, TICKER)]
    )
    asyncio.run(g.refresh(force=True))
    assert g.state()["paused_ms"]
    restart = DailyProfitGuard(path, "primary", "You", client)
    restart.pages = AsyncMock(return_value=[])
    monkeypatch.setattr(
        "btc15_signal.daily_profit.realised_events", lambda *_: [(clock[0] - 10, -1, TICKER)]
    )
    asyncio.run(restart.refresh(force=True))
    assert restart.state()["opening"] == 100
    assert asyncio.run(restart.block_reason(TICKER)).startswith("BTC 3%")
    # 00:00:05 is too early - the opening waits for the 00:00 market to settle.
    clock[0] = int(datetime(2026, 9, 30, 0, 0, 5, tzinfo=NY).timestamp() * 1000)
    monkeypatch.setattr("btc15_signal.daily_profit.realised_events", lambda *_: [])
    asyncio.run(restart.refresh(force=True))
    assert restart.state() is None, "no opening before 00:00:30"
    clock[0] = int(datetime(2026, 9, 30, 0, 0, 35, tzinfo=NY).timestamp() * 1000)
    monkeypatch.setattr("btc15_signal.daily_profit.realised_events", lambda *_: [])
    client.account_value.return_value = 102
    asyncio.run(restart.refresh(force=True))
    assert restart.state()["opening"] == 102
    assert restart.state()["paused_ms"] is None
    assert restart.state()["basis"] == "day opening"


def test_read_failure_blocks_entry_and_does_not_reset_capital(tmp_path):
    client = SimpleNamespace(account_value=AsyncMock(return_value=100))
    g = DailyProfitGuard(tmp_path / "g.db", "m1", "Wife", client)
    g.pages = AsyncMock(side_effect=RuntimeError("unavailable"))
    assert "unavailable" in asyncio.run(g.block_reason(TICKER))
    assert g.state() is None


def test_guard_blocks_broker_submission_for_primary_and_mirror():
    for account in ("primary", "m1", "m2"):
        client = object.__new__(KalshiExecutionClient)
        client.daily_profit_guard = SimpleNamespace(block_reason=AsyncMock(return_value="paused"))
        client._post = AsyncMock()
        result = asyncio.run(client.execute_with_take_profit(SimpleNamespace(ticker=TICKER)))
        assert result.status == "paused"
        result = asyncio.run(client.place_resting_buy(TICKER, "UP", 0.7, 1, 1, account))
        assert result["status"] == "paused"
        client._post.assert_not_called()


def test_btc_only_blocks_gold_but_still_allows_existing_gold_exit():
    client = object.__new__(KalshiExecutionClient)
    client.allowed_entry_series = {"KXBTC15M"}
    client.daily_profit_guard = SimpleNamespace(
        block_reason=AsyncMock(side_effect=AssertionError("exit gated"))
    )
    client._post = AsyncMock(return_value={"fill_count": 1, "order_id": "exit"})
    gold = "KXGOLD15M-26SEP291830-30"
    result = asyncio.run(client.execute_with_take_profit(SimpleNamespace(ticker=gold)))
    assert result.status == "paused"
    client._post.assert_not_called()
    result = asyncio.run(client.close_position(gold, "UP", 1, 0.98))
    assert result.status == "exited"
    assert client._post.call_args.args[1]["reduce_only"] is True
    client.daily_profit_guard.block_reason.assert_not_called()


def test_actual_v2_records_reconcile_to_settlement_for_both_sides():
    for side, entry_price in [("yes", 0.74), ("no", 0.85)]:
        other = "no" if side == "yes" else "yes"
        entry = fill(
            "2026-09-29T22:15:00Z", side, entry_price, action="buy" if side == "yes" else "sell"
        )
        entry.update(book_side="bid" if side == "yes" else "ask", outcome_side=side)
        exit_fill = fill(
            "2026-09-29T22:20:00Z", other, 0.005, action="buy" if other == "yes" else "sell"
        )
        exit_fill.update(book_side="bid" if other == "yes" else "ask", outcome_side=other)
        s = dict(
            ticker=TICKER,
            settled_time="2026-09-29T22:30:08Z",
            yes_count_fp="1",
            no_count_fp="1",
            revenue=0,
            fee_cost=".02",
        )
        s[side + "_total_cost_dollars"] = str(entry_price)
        s[other + "_total_cost_dollars"] = ".005"
        events = realised_events([entry, exit_fill], [s])
        assert events[0][1] == pytest.approx(1 - entry_price - 0.005 - 0.02)
        assert events[1][1] == pytest.approx(0)


def test_telegram_footer_has_start_target_and_pause(tmp_path):
    client = SimpleNamespace(account_value=AsyncMock(return_value=100))
    g = DailyProfitGuard(tmp_path / "g.db", "primary", "You", client)
    g.pages = AsyncMock(return_value=[])
    asyncio.run(g.refresh(force=True))
    tg = Telegram("", "", True)
    tg.status_footer = lambda: footer([g])
    text = tg.with_status("BTC result")
    assert "$100.00" in text and "target $3.00" in text
    assert "after fees" in text and "ACTIVE" in text
    assert tg.with_status(text) == text


def test_fresh_statistics_include_entry_in_already_open_window(tmp_path):
    import sqlite3

    from btc15_signal.shadow_summary import allsignal_record, allsignal_stats

    client = SimpleNamespace(account_value=AsyncMock(return_value=100))
    g = DailyProfitGuard(tmp_path / "runtime" / "daily_profit.db", "primary", "You", client)
    g.pages = AsyncMock(return_value=[])
    asyncio.run(g.refresh(force=True))
    start = g.state()["start_ms"]
    path = tmp_path / "btc15.db"
    with sqlite3.connect(path) as db:
        db.execute(
            "CREATE TABLE allsignal_trades (window_open,created_ms,status,won,pnl,fee,exit_fee)"
        )
        db.execute(
            "INSERT INTO allsignal_trades VALUES (?,?,?,?,?,?,?)",
            (start - 10000, start + 1000, "filled", 1, 0.2, 0.01, 0.01),
        )
        db.execute(
            "INSERT INTO allsignal_trades VALUES (?,?,?,?,?,?,?)",
            (start - 100000, start - 90000, "filled", 1, 10, 0.01, 0.01),
        )
    record = allsignal_record(path)
    assert record["n"] == 1 and record["pnl"] == pytest.approx(0.18)
    session = allsignal_stats(path, start - 10000, start + 900000)
    assert session["n"] == 1 and session["pnl"] == pytest.approx(0.18)


def test_the_00_00_market_belongs_to_the_day_it_closed(tmp_path, monkeypatch):
    """Review 2026-09-29: the 23:45-00:00 market settles ~00:00:06.5. Its result
    is in the new opening (captured at 00:00:30), never in the new day's P&L."""
    clock = [int(datetime(2026, 9, 30, 0, 0, 35, tzinfo=NY).timestamp() * 1000)]
    monkeypatch.setattr("btc15_signal.daily_profit.time.time", lambda: clock[0] / 1000)
    client = SimpleNamespace(account_value=AsyncMock(return_value=99.48))
    g = DailyProfitGuard(tmp_path / "g.db", "primary", "You", client)
    g.pages = AsyncMock(return_value=[])
    settled = int(datetime(2026, 9, 30, 0, 0, 6, tzinfo=NY).timestamp() * 1000)
    later = clock[0] + 600_000
    monkeypatch.setattr("btc15_signal.daily_profit.realised_events", lambda *_: [
        (settled, -0.52, "KXBTC15M-26SEP300000-00"),     # closed at midnight
        (later - 1, 0.40, "KXBTC15M-26SEP300015-15"),    # the new day's own
    ])
    asyncio.run(g.refresh(force=True))
    assert g.state()["basis"] == "day opening" and g.state()["opening"] == 99.48
    clock[0] = later
    asyncio.run(g.refresh(force=True))
    assert g.state()["pnl"] == pytest.approx(0.40)


def test_a_failed_read_is_retried_at_once_not_after_15_s(tmp_path):
    client = SimpleNamespace(account_value=AsyncMock(return_value=100))
    g = DailyProfitGuard(tmp_path / "g.db", "primary", "You", client)
    g.pages = AsyncMock(return_value=[])
    asyncio.run(g.refresh(force=True))
    g.pages = AsyncMock(side_effect=RuntimeError("timeout"))
    asyncio.run(g.refresh(force=True))
    assert g.error
    g.pages = AsyncMock(return_value=[])
    assert asyncio.run(g.block_reason(TICKER)) == "", "retried, not blocked for 15 s"


def test_the_monitor_survives_and_says_an_outage_once(tmp_path, monkeypatch):
    from btc15_signal import daily_profit as dp

    client = SimpleNamespace(account_value=AsyncMock(return_value=100))
    g = DailyProfitGuard(tmp_path / "g.db", "primary", "You", client)
    g.pages = AsyncMock(side_effect=RuntimeError("down"))
    sent = []

    class Tg:
        async def send(self, text):
            sent.append(text)
            return 1

    for _ in range(4):
        asyncio.run(dp._monitor_one(g, Tg()))
    assert sum("UNAVAILABLE" in t for t in sent) == 1
    g.pages = AsyncMock(return_value=[])
    g.last_attempt = 0
    asyncio.run(dp._monitor_one(g, Tg()))
    assert sum("RESTORED" in t for t in sent) == 1

    async def boom(*_a, **_k):
        raise RuntimeError("x")

    class Stop(BaseException):       # not an Exception: the monitor must not swallow it
        pass

    monkeypatch.setattr(dp, "_monitor_one", boom)
    monkeypatch.setattr(dp.asyncio, "sleep", AsyncMock(side_effect=[None, Stop()]))
    with pytest.raises(Stop):
        asyncio.run(dp.monitor([g], Tg()))   # two passes: an exception did not end the loop


def test_each_account_has_its_own_rate_and_the_table_names_them(tmp_path):
    """Operator, 2026-09-29: 8% on the primary, 15% on each mirror."""
    from btc15_signal.config import Settings
    from btc15_signal.daily_profit import summary

    d = Settings.model_fields
    assert d["daily_profit_target_rate"].default == 0.08
    assert d["mirror_daily_profit_target_rate"].default == 0.15
    guards = []
    for account, label, rate, opening in (("primary", "You", 0.08, 104.77),
                                          ("m1", "Wife", 0.15, 25.39)):
        client = SimpleNamespace(account_value=AsyncMock(return_value=opening))
        g = DailyProfitGuard(tmp_path / "g.db", account, label, client, rate)
        g.pages = AsyncMock(return_value=[])
        asyncio.run(g.refresh(force=True))
        guards.append(g)
    assert guards[0].state()["target"] == pytest.approx(8.3816)
    assert guards[1].state()["target"] == pytest.approx(3.8085)
    text = summary(guards)
    assert "daily targets You 8% · Wife 15%" in text
    assert "8.38" in text and "3.81" in text


def test_the_2000_signal_review_reminder_fires_once(tmp_path):
    from btc15_signal import main
    from btc15_signal.config import Settings
    from btc15_signal.store import Store

    store = Store(str(tmp_path / "btc15.db"))
    settings = Settings(kalshi_series="KXBTC15M", database_path=str(tmp_path / "btc15.db"))
    sent = []

    class Tg:
        async def send(self, text, buttons=None, reply_to=None):
            sent.append(text)
            return len(sent)

    base = main.TARGET_REVIEW_FROM
    for i in range(main.TARGET_REVIEW_AT - 1):
        store.record_alert("primary", base + i * 900_000, base + i * 900_000)
    asyncio.run(main.remind_target_review(store, settings, Tg(), base))
    assert sent == [], "1,999 is not yet"
    store.record_alert("primary", base + 10**10, base + 10**10)
    asyncio.run(main.remind_target_review(store, settings, Tg(), base))
    asyncio.run(main.remind_target_review(store, settings, Tg(), base + 60_000))
    assert len(sent) == 1 and "2,000 BTC SIGNALS RECORDED" in sent[0]
