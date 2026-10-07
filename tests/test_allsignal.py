"""The ALL-SIGNAL $1 strategy, beside the main one (operator, 2026-09-28).

"This one should be running alongside with the main strategy already running...
trade at a pace one dollar... execute all their generated signal every 15
minutes. And the main strategy running right now should keep running as it is.
So basically, you will be running two strategies in parallel."
"""

import asyncio
import inspect
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from btc15_signal import main, messages  # noqa: E402
from btc15_signal import shadow_summary as S  # noqa: E402
from btc15_signal.config import Settings  # noqa: E402
from btc15_signal.execution import ExecutionResult  # noqa: E402
from btc15_signal.store import Store  # noqa: E402
from btc15_signal.validation import contracts_for_budget  # noqa: E402

W = 1_790_600_400_000
TICKER = "KXBTC15M-26SEP281215-15"


class FakeClient:
    def __init__(self, filled=1):
        self.orders, self.filled = [], filled

    async def execute_with_take_profit(self, order, slippage, ceiling=None):
        self.orders.append((order, ceiling))
        status = "filled" if self.filled else "unfilled"
        return ExecutionResult(status, self.filled, "oid-1", None, "note")

    async def fill_detail(self, order_id, side):
        return (0.74, float(self.filled), 0.01)


class Mirrors:
    """What the service holds: a wrapper that copies a FILLED order to the
    wife's account (since 2026-09-28 the all-signal strategy goes through it)."""

    def __init__(self, primary):
        self._primary = primary
        self.mirrored = []

    def __getattr__(self, name):          # as MirroringExecutionClient does
        return getattr(self._primary, name)

    async def execute_with_take_profit(self, order, slippage, ceiling=None):
        result = await self._primary.execute_with_take_profit(order, slippage, ceiling)
        if result.filled_count > 0:
            self.mirrored.append(order)
        return result


@pytest.fixture(autouse=True)
def _no_wait(monkeypatch):
    # Unit fixtures exercise both configurable instruments at the original
    # budget, independently of the operator's live .env configuration.
    monkeypatch.setenv("ALLSIGNAL_INSTRUMENTS", "BTC,GOLD")
    monkeypatch.setenv("ALLSIGNAL_STAKE", "1")
    async def instant(_s):
        return None
    monkeypatch.setattr(main.asyncio, "sleep", instant)


def setup(tmp_path, series="KXBTC15M", auto=True, filled=1):
    store = Store(str(tmp_path / f"{series}.db"))
    store.set_setting("auto_trade_enabled", 1.0 if auto else 0.0, 1)
    settings = Settings(kalshi_series=series, database_path=str(tmp_path / "x.db"))
    client = FakeClient(filled)
    return store, settings, client, Mirrors(client)


def fire(store, settings, trader, ask=0.73, window=W, ticker=TICKER, side="UP"):
    async def go():
        main.spawn_allsignal(store, settings, trader, ticker, side, ask, window, window + 60_000)
        await asyncio.gather(*list(main.ALLSIGNAL_TASKS))
    asyncio.run(go())


def test_every_signal_buys_one_dollar_and_is_copied_to_the_mirror(tmp_path):
    """Operator, 2026-09-28: "run on both my wife and mine with the $1"."""
    store, settings, client, trader = setup(tmp_path)
    fire(store, settings, trader, ask=0.73)
    (copied,) = trader.mirrored
    assert copied.strategy == "allsignal", "the mirror sizes it by its own $1 budget"
    (order, ceiling), = client.orders
    assert order.count == contracts_for_budget(1.00, 0.73) == 1
    assert order.side == "UP" and order.ticker == TICKER
    assert ceiling == round(0.73 + settings.entry_slippage, 2)
    row = store.db.execute("SELECT status, filled, fill_price, fee FROM allsignal_trades").fetchone()
    assert row == ("filled", 1.0, 0.74, 0.01)
    assert store.db.execute("SELECT COUNT(*) FROM trade_proposals").fetchone()[0] == 0, \
        "the main strategy's book never sees it"


def test_a_cheap_signal_buys_as_many_as_one_dollar_does(tmp_path):
    store, settings, client, trader = setup(tmp_path)
    fire(store, settings, trader, ask=0.26)
    assert client.orders[0][0].count == 3


def test_once_per_window(tmp_path):
    store, settings, client, trader = setup(tmp_path)
    fire(store, settings, trader)
    fire(store, settings, trader)
    assert len(client.orders) == 1


def test_only_btc_and_gold_and_only_while_switched_on(tmp_path):
    for series, auto, switch, expected in (
            ("KXETH15M", True, None, 0),        # not listed
            ("KXBTC15M", False, None, 0),       # /auto off stops everything
            ("KXBTC15M", True, 0.0, 0),         # its own switch off
            ("KXGOLD15M", True, None, 1)):
        d = tmp_path / f"{series}{auto}{switch}"
        d.mkdir()
        store, settings, client, trader = setup(d, series=series, auto=auto)
        if switch is not None:
            store.set_setting("allsignal_enabled", switch, 1)
        fire(store, settings, trader, ticker=f"{series}-26SEP281215-15")
        assert len(client.orders) == expected, (series, auto, switch)


def test_an_unfilled_signal_is_recorded_as_such(tmp_path):
    store, settings, client, trader = setup(tmp_path, filled=0)
    fire(store, settings, trader)
    assert store.db.execute("SELECT status FROM allsignal_trades").fetchone()[0] == "unfilled"


def settlement(result):
    return {"ticker": TICKER, "market_result": result,
            "yes_count_fp": "1.00", "yes_total_cost_dollars": "0.740000",
            "no_count_fp": "0.00", "no_total_cost_dollars": "0.000000",
            "revenue": 100 if result == "yes" else 0, "value": 0, "fee_cost": "0.010000"}


def test_graded_from_the_brokers_result_and_kept_out_of_the_main_floor(tmp_path):
    from types import SimpleNamespace

    store, settings, client, trader = setup(tmp_path)
    store.configure_instrument(SimpleNamespace(kalshi_series="KXBTC15M"))
    now = store.day_start_ms(W) + 3_600_000
    window = store.day_start_ms(W) + 1_800_000
    fire(store, settings, trader, window=window)
    store.record_settlements([settlement("no")], now)     # the $1 UP lost
    assert store.allsignal_grade(now) == 1
    won, pnl = store.db.execute("SELECT won, pnl FROM allsignal_trades").fetchone()
    assert won == 0 and round(pnl, 2) == -0.74
    # The exchange books the market once; the main strategy's floor takes the
    # all-signal money back out, so its figure is exactly its own (nothing).
    assert store.exchange_record(store.day_start_ms(now), series="KXBTC15M")[2] < 0
    assert store.auto_state(now)[3] == 0.0


def test_the_session_summary_reports_it_as_real_money(tmp_path):
    text = messages.shadow_summary_message(
        session="us", ny_day="2026-09-28", rows=[],
        allsignal=[("BTC", {"n": 20, "wins": 16, "pnl": 1.25, "fee": 0.2, "open": 1, "missed": 2})])
    assert "ALL-SIGNAL $1" in text and "<b>BTC</b> this session: 16W" in text
    assert "1 open" in text and "2 not filled" in text
    assert "mirrors use their own budgets" in text and "after fees" in text


def test_the_service_fires_it_on_the_alert_and_grades_it():
    src = " ".join(inspect.getsource(main).split())
    path = " ".join(inspect.getsource(main.primary_signal).split())
    fired = path.index('alerting = store.record_alert("primary", opened, now_ms) if alerting:')
    # Since 09-29 through the after-loss cushion gate, which then spawns it.
    assert path.index("allsignal_on_alert(store, settings, trader, contract, prediction.side,") > fired
    assert "store.allsignal_grade(now_ms)" in src
    assert "collect_allsignal(" in src
    assert hasattr(S, "allsignal_stats")


# ------------------------------------------------ review fixes, 2026-09-28

def fill_row(store, order_id, ticker=TICKER, side="yes", price=0.73, fee=0.01, at=W + 60_000):
    store.db.execute(
        "INSERT INTO fills (fill_id, ticker, order_id, action, side, count, yes_price, "
        "no_price, fee_cost, is_taker, filled_ms, window_ms, synced_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,1,?,?,?)",
        (f"f-{order_id}", ticker, order_id, "buy", side, 1.0, price, 1 - price, fee,
         at, W, at))
    store.db.commit()


def test_open_dollar_positions_count_as_committed_capital(tmp_path):
    """The midnight review runs while the 23:45 $1 trade is open; its cost is
    out of the cash, so leaving it out cut the main tier ($120.50 -> base 3)."""
    store, settings, client, trader = setup(tmp_path)
    fire(store, settings, trader, ask=0.73)
    assert round(store.open_position_cost(), 2) == 0.74
    store.record_settlements([settlement("yes")], W + 1_000_000)
    assert store.open_position_cost() == 0.0, "settled is not open"


def test_an_interrupted_order_is_settled_from_the_brokers_fills(tmp_path):
    store, settings, client, trader = setup(tmp_path)
    store.allsignal_claim(W, TICKER, "UP", 0.73, 1, 0.78, W + 60_000)   # never finished
    fill_row(store, "ours")
    main_row = store.create_proposal("primary", W, TICKER, "UP", 0.75, 0.0, 4,
                                     W + 900_000, W + 900_000, W + 70_000)
    store.db.execute("UPDATE trade_proposals SET entry_order_id='main', status='filled' "
                     "WHERE id=?", (main_row.id,))
    fill_row(store, "main", price=0.75, at=W + 70_000)
    store.record_settlements([settlement("no")], W + 1_000_000)
    store.allsignal_grade(W + 1_000_000)
    got = store.db.execute(
        "SELECT status, order_id, fill_price, fee, won FROM allsignal_trades").fetchone()
    assert got == ("filled", "ours", 0.73, 0.01, 0), "never the main strategy's order"


def test_a_fallback_price_is_corrected_from_the_fills(tmp_path):
    store, settings, client, trader = setup(tmp_path)
    store.allsignal_claim(W, TICKER, "UP", 0.73, 1, 0.78, W + 60_000)
    store.allsignal_finish(W, "filled", order_id="ours", filled=1.0, fill_price=0.78)
    fill_row(store, "ours", price=0.73, fee=0.01)
    store.allsignal_grade(W + 1_000_000)
    assert store.db.execute("SELECT fill_price, fee FROM allsignal_trades").fetchone() == (0.73, 0.01)


def test_no_fill_on_record_ten_minutes_after_close_is_unfilled(tmp_path):
    store, settings, client, trader = setup(tmp_path)
    store.allsignal_claim(W, TICKER, "UP", 0.73, 1, 0.78, W + 60_000)
    store.allsignal_grade(W + 900_000 + 60_000)
    assert store.db.execute("SELECT status FROM allsignal_trades").fetchone()[0] == "claimed"
    store.allsignal_grade(W + 900_000 + 700_000)
    assert store.db.execute("SELECT status FROM allsignal_trades").fetchone()[0] == "unfilled"


def test_the_main_recap_and_the_sweeps_keep_the_strategies_apart():
    src = " ".join(inspect.getsource(main).split())
    assert "pnl = booked - store.allsignal_net_for_ticker(ticker)" in src
    assert src.count("store.allsignal_grade(now_ms)") >= 2
    sweep = src.index("store.record_settlements(await trader.settlements(), now_ms)")
    assert src.index("store.allsignal_grade(now_ms)", sweep) - sweep < 400, \
        "graded straight after the sync, before the broker reads that can fail"


def test_a_window_is_reported_in_the_session_it_settled(tmp_path):
    import datetime as dt
    from types import SimpleNamespace

    end = int(dt.datetime(2026, 9, 28, 13, 0, 5, tzinfo=dt.UTC).timestamp() * 1000)
    store = Store(str(tmp_path / "btc15.db"))
    last = end - 5_000 - 900_000                       # closes AT the europe boundary
    store.allsignal_claim(last, TICKER, "UP", 0.73, 1, 0.78, last + 60_000)
    store.allsignal_finish(last, "filled", order_id="o", filled=1.0, fill_price=0.73, fee=0.01)
    store.db.execute("UPDATE allsignal_trades SET won=1, pnl=0.27")
    store.db.commit()
    settings = SimpleNamespace(allsignal_instruments="BTC")
    # Still open when the europe summary is built at 13:00: not in it...
    assert "BTC" not in dict(S.collect_allsignal(tmp_path, settings, "europe", end))
    # ...and reported, settled, in the us summary at 21:00.
    us_end = end + 8 * 3_600_000
    got = dict(S.collect_allsignal(tmp_path, settings, "us", us_end))
    assert got["BTC"]["n"] == 1 and got["BTC"]["open"] == 0


def test_with_the_mirror_off_it_uses_the_primary_account_only(tmp_path):
    store, settings, client, trader = setup(tmp_path)
    settings = Settings(kalshi_series="KXBTC15M", database_path=str(tmp_path / "x.db"),
                        allsignal_mirror=False)
    fire(store, settings, trader)
    assert trader.mirrored == [] and len(client.orders) == 1


def test_the_mirror_sizes_a_dollar_signal_by_its_own_budget():
    from btc15_signal.mirror import MirrorTarget

    her = MirrorTarget(name="m1", api_key_id="k", private_key_path="p",
                       base_contracts=1, max_contracts=2, allsignal_budget=1.0)
    assert her._cap(contracts_for_budget(her.allsignal_budget, 0.73)) == 1
    assert her._cap(contracts_for_budget(her.allsignal_budget, 0.26)) == 2, "her cap holds"
    src = " ".join(inspect.getsource(__import__("btc15_signal.mirror", fromlist=["x"])).split())
    assert 'if getattr(proposal, "strategy", "") == "allsignal":' in src


# ------------------------------------------ the main strategy's own pause

def test_the_main_strategy_pauses_without_stopping_the_dollar_strategy(tmp_path):
    store, settings, client, trader = setup(tmp_path)
    store.set_setting("main_enabled", 0.0, 1)
    assert main.main_strategy_on(store) is False
    fire(store, settings, trader)
    assert len(client.orders) == 1, "the $1 strategy keeps trading"
    path = " ".join(inspect.getsource(main.primary_signal).split())
    assert "auto_on = auto_is_on(store, settings) and main_strategy_on(store)" in path
    assert "auto_is_on( store, settings) and main_strategy_on(store)" in path  # trading_open
    assert "and main_strategy_on(store) )" in path             # may_retry


def test_auto_off_still_stops_both(tmp_path):
    store, settings, client, trader = setup(tmp_path, auto=False)
    fire(store, settings, trader)
    assert client.orders == []


# ------------------------------- the new strategy's own messages (09-28)

class FakeTelegram:
    def __init__(self):
        self.sent, self.edits = [], []

    async def send(self, text, buttons=None, reply_to=None):
        self.sent.append(text)
        self.replies = getattr(self, "replies", []) + [reply_to]
        return 100 + len(self.sent)

    async def edit(self, message_id, text, buttons=None):
        self.edits.append((message_id, text))
        return True


def test_an_entry_message_then_a_result_reply_in_time_order(tmp_path):
    """Operator: "a cleaner version that track it execution and overall and
    daily win rate". Then (19:0x): "the messaging is messed up" - the entry was
    rewritten to the result AND the result replied, so each showed twice. Now:
    the entry, never rewritten; the result as a reply, carrying the record."""
    # The stats read each instrument's store by its real name, as in service.
    store = Store(str(tmp_path / "btc15.db"))
    store.set_setting("auto_trade_enabled", 1.0, 1)
    settings = Settings(kalshi_series="KXBTC15M", database_path=str(tmp_path / "btc15.db"))
    client = FakeClient(1)
    trader = Mirrors(client)
    tg = FakeTelegram()

    async def go():
        main.spawn_allsignal(store, settings, trader, TICKER, "UP", 0.73, W,
                             W + 60_000, tg)
        await asyncio.gather(*list(main.ALLSIGNAL_TASKS))
    asyncio.run(go())
    (opened,) = tg.sent
    assert opened.startswith("🟢 <b>BTC $1 · NEW TRADE</b>") and "filled <b>74¢" in opened
    assert "Today:" not in opened, "the record belongs to the result"
    msg_id = store.db.execute("SELECT tg_message_id FROM allsignal_trades").fetchone()[0]
    assert msg_id == 101

    store.record_settlements([settlement("yes")], W + 1_000_000)   # UP won
    store.allsignal_grade(W + 1_000_000)
    asyncio.run(main.report_allsignal_results(store, settings, tg, W + 1_000_000))
    assert tg.edits == [], "the entry is never rewritten"
    # The result is a NEW message replying to it - an edit alone is silent in
    # Telegram, and the operator saw no result at 18:00 on 09-28.
    text = tg.sent[-1]
    assert text.startswith("✅ <b>BTC $1 · WON +$0.25</b>")
    assert "Since start (net):" in text and "Today (net):" in text
    assert tg.replies[-1] == 101
    asyncio.run(main.report_allsignal_results(store, settings, tg, W + 1_060_000))
    assert tg.edits == [] and len(tg.sent) == 2, "reported once"


def test_the_book_lines_show_this_instrument_and_the_combined_book():
    book = [("BTC", {"n": 6, "wins": 6, "pnl": 1.43, "fee": 0.1},
             {"n": 6, "wins": 6, "pnl": 1.43, "fee": 0.1}),
            ("GOLD", {"n": 6, "wins": 5, "pnl": 0.61, "fee": 0.1},
             {"n": 6, "wins": 5, "pnl": 0.61, "fee": 0.1}),
            ("BTC+GOLD", {"n": 12, "wins": 11, "pnl": 2.04, "fee": 0.2},
             {"n": 12, "wins": 11, "pnl": 2.04, "fee": 0.2})]
    lines = messages._book_lines(book, "GOLD")
    assert lines[0].startswith("📅 Today (net): GOLD 5W–1L <b>83%</b> +$0.61")
    assert "BTC+GOLD 11W–1L <b>92%</b> +$2.04" in lines[1]


def test_the_message_columns_are_added_to_an_existing_book(tmp_path):
    import sqlite3

    path = tmp_path / "old.db"
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE allsignal_trades (window_open INTEGER PRIMARY KEY, "
                "ticker TEXT NOT NULL, side TEXT NOT NULL, ask REAL NOT NULL, "
                "count REAL NOT NULL, limit_price REAL NOT NULL, created_ms INTEGER NOT NULL, "
                "status TEXT NOT NULL DEFAULT 'claimed', order_id TEXT, filled REAL, "
                "fill_price REAL, fee REAL, note TEXT, won INTEGER, pnl REAL, graded_ms INTEGER)")
    con.commit()
    con.close()
    store = Store(str(path))
    cols = {r[1] for r in store.db.execute("PRAGMA table_info(allsignal_trades)")}
    assert {"tg_message_id", "reported_ms"} <= cols


def test_the_old_systems_messages_are_quiet_while_it_is_paused(tmp_path):
    from types import SimpleNamespace

    from btc15_signal.notify import Notifier

    store = Store(str(tmp_path / "q.db"))
    store.set_setting("auto_trade_enabled", 1.0, 1)
    store.set_setting("main_enabled", 0.0, 1)
    n = Notifier(FakeTelegram(), store, SimpleNamespace(
        telegram_alert_instruments="BTC,GOLD", kalshi_series="KXBTC15M",
        auto_trade_enabled=True))
    assert n.alerts_on() is False
    assert asyncio.run(n.send_once("signal", "k", "x", 1)) is False
    assert asyncio.run(n.send_once("allsignal_trade", "k", "x", 1)) is True
    assert asyncio.run(n.send_once("cash_out", "k2", "x", 1)) is True, "money still speaks"
    store.set_setting("main_enabled", 1.0, 1)
    assert n.alerts_on() is True


# ------------------------------------ account balances in the summary (09-28)

def test_the_summary_tracks_each_accounts_start_and_current_balance(tmp_path):
    import json
    from types import SimpleNamespace

    store = Store(str(tmp_path / "btc15.db"))
    store.set_setting_text("allsignal_start_balances", json.dumps({
        "You": {"value": 112.12, "at_ms": 1790624700000},
        "Wife": {"value": 28.56, "at_ms": 1790624700000}}), 1)

    class Acct:
        def __init__(self, value):
            self.value = value

        async def account_value(self):
            return self.value

    trader = SimpleNamespace(_primary=Acct(112.45), _mirrors=[
        SimpleNamespace(target=SimpleNamespace(name="m1"), client=Acct(None))])
    got = asyncio.run(main.allsignal_balances(store, trader))
    assert got == [("You", 112.12, 1790624700000, 112.45),
                   ("Wife", 28.56, 1790624700000, None)]
    lines = messages._balance_lines(got)
    assert lines[0] == ("\U0001f3e6 You: $112.12 → <b>$112.45</b> (+$0.33) "
                        "since 09-28 15:45")
    assert "now unreadable" in lines[1], "a missing read is said, not guessed"
    assert asyncio.run(main.allsignal_balances(Store(str(tmp_path / "none.db")), trader)) == []


# ---------------------------------------- the cash-out, on this book (09-28)
#
# Operator, 2026-09-28 18:1x: "cash out must be part of the system at all
# levels ... it cashes out at max profit, no need to wait for expiry". The main
# rule (`cash_out_exit`) reads trade_proposals and never saw a $1 trade.

class Market:
    def __init__(self, ticker=TICKER, yes_bid=0.99, no_bid=0.0, yes_ask=1.0, no_ask=0.02):
        self.ticker = ticker
        self.yes_bid, self.no_bid, self.yes_ask, self.no_ask = yes_bid, no_bid, yes_ask, no_ask

    def bid(self, side):
        return self.yes_bid if side == "UP" else self.no_bid

    def ask(self, side):
        return self.yes_ask if side == "UP" else self.no_ask


class SellingClient(FakeClient):
    def __init__(self, filled=1, sells=None, sold_at=0.99, lag=0):
        super().__init__(filled)
        self.closes, self.sells, self.sold_at = [], sells, sold_at
        self.lag = lag          # empty exit reads before the feed catches up

    async def close_position(self, ticker, side, count, limit_price, floor=None):
        self.closes.append((ticker, side, count, limit_price, floor))
        n = count if self.sells is None else self.sells
        return ExecutionResult("exited" if n else "exit-unfilled", n, "exit-1", None, "sold")

    async def fill_detail(self, order_id, side):
        if order_id == "exit-1" and self.lag:
            self.lag -= 1
            return None
        if order_id == "exit-1":
            return (self.sold_at, float(self.closes[-1][2] if self.sells is None
                                        else self.sells), 0.0)
        return (0.74, float(self.filled), 0.01)


class SellingMirrors(Mirrors):
    async def close_position(self, ticker, side, count, limit_price, floor=None):
        result = await self._primary.close_position(ticker, side, count, limit_price, floor)
        self.mirrored.append(("exit", ticker, side))      # forwarded, as the real one
        return result


def cash_setup(tmp_path, filled=1, sells=None, sold_at=0.99):
    store = Store(str(tmp_path / "btc15.db"))
    store.set_setting("auto_trade_enabled", 1.0, 1)
    settings = Settings(kalshi_series="KXBTC15M", database_path=str(tmp_path / "btc15.db"))
    client = SellingClient(filled, sells, sold_at)
    trader = SellingMirrors(client)
    tg = FakeTelegram()

    async def go():
        main.spawn_allsignal(store, settings, trader, TICKER, "UP", 0.73, W, W + 60_000, tg)
        await asyncio.gather(*list(main.ALLSIGNAL_TASKS))
    asyncio.run(go())
    return store, settings, client, trader, tg


def cash(store, settings, tg, trader, market=None, remaining=240, now=W + 660_000):
    asyncio.run(main.allsignal_cash_out(settings, store, tg, market or Market(),
                                        W, remaining, now, trader))


def test_a_dollar_position_cashes_out_at_max_profit_on_both_accounts(tmp_path):
    store, settings, client, trader, tg = cash_setup(tmp_path)
    cash(store, settings, tg, trader)                  # quoted 0.99 bid, 4m left
    # The main rule's order: the discounted bid, crossing down to the floor.
    assert client.closes == [(TICKER, "UP", 1.0, 0.98, 0.90)]
    assert ("exit", TICKER, "UP") in trader.mirrored, "her account sells too"
    won, pnl, price, n = store.db.execute(
        "SELECT won, pnl, exit_price, exit_count FROM allsignal_trades").fetchone()
    assert (won, round(pnl, 2), price, n) == (1, 0.25, 0.99, 1.0)
    # SAID AS A CASH-OUT, the moment it happens - never dressed as a settlement,
    # as a reply to the entry, which is left as it was.
    assert tg.edits == []
    assert tg.sent[-1].startswith("💰 <b>BTC $1 · CASHED OUT +$0.24</b>")
    assert "sold 99¢ with 4m00s left" in tg.sent[-1] and "Since start (net):" in tg.sent[-1]
    assert tg.replies[-1] == 101
    # The market then settles the other way: the sale stands, nothing re-sent.
    store.record_settlements([settlement("no")], W + 1_000_000)
    store.allsignal_grade(W + 1_000_000)
    asyncio.run(main.report_allsignal_results(store, settings, tg, W + 1_000_000))
    assert round(store.db.execute("SELECT pnl FROM allsignal_trades").fetchone()[0], 2) == 0.25
    assert len(tg.sent) == 2
    assert store.open_position_cost() == 0.0, "sold is not committed capital"


def test_it_is_the_main_rule_and_fires_once(tmp_path):
    store, settings, client, trader, tg = cash_setup(tmp_path)
    # 0.95 quoted on a 0.74 entry: 0.20 of the 0.26 available is short of 90%.
    cash(store, settings, tg, trader, Market(yes_bid=0.95))
    cash(store, settings, tg, trader, remaining=50)             # last minute
    store.set_setting("auto_trade_enabled", 0.0, 2)
    cash(store, settings, tg, trader)                           # kill switch
    assert client.closes == []
    store.set_setting("auto_trade_enabled", 1.0, 3)
    # Switching the $1 strategy off stops new entries, not the management of
    # a position it already holds - as with the main strategy.
    store.set_setting("allsignal_enabled", 0.0, 4)
    cash(store, settings, tg, trader)
    cash(store, settings, tg, trader)
    assert len(client.closes) == 1


def test_a_missed_cash_out_holds_and_says_so(tmp_path):
    store, settings, client, trader, tg = cash_setup(tmp_path, sells=0)
    cash(store, settings, tg, trader)
    assert len(client.closes) == 1
    assert store.allsignal_open(W) is not None, "still held"
    assert "CASH-OUT MISSED" in tg.sent[-1] and "holding to settlement" in tg.sent[-1]
    assert tg.replies[-1] == 101
    cash(store, settings, tg, trader)
    assert len(client.closes) == 1, "one attempt per window, as the main rule"
    store.record_settlements([settlement("yes")], W + 1_000_000)
    store.allsignal_grade(W + 1_000_000)
    asyncio.run(main.report_allsignal_results(store, settings, tg, W + 1_000_000))
    assert tg.sent[-1].startswith("✅ <b>BTC $1 · WON +$0.25</b>")


def test_a_partial_cash_out_is_graded_with_the_rest_at_settlement(tmp_path):
    store, settings, client, trader, tg = cash_setup(tmp_path, filled=2, sells=1, sold_at=0.98)
    cash(store, settings, tg, trader)
    assert "CASHED OUT 1 of 2" in tg.sent[-1]
    assert store.open_position_cost() == 0.74, "one still held"
    store.record_settlements([settlement("no")], W + 1_000_000)
    store.allsignal_grade(W + 1_000_000)
    won, pnl = store.db.execute("SELECT won, pnl FROM allsignal_trades").fetchone()
    assert (won, round(pnl, 2)) == (0, -0.50)          # +0.24 sold, -0.74 held


def test_the_money_carries_the_exit_fee(tmp_path):
    store, settings, client, trader, tg = cash_setup(tmp_path)
    store.allsignal_mark_exited(W, price=0.99, count=1, fee=0.01, order_id="x",
                                now_ms=W + 700_000)
    # Gross +0.25 (fee-free, as reported); net of the entry and exit fees.
    assert round(store.allsignal_net_since(0), 2) == 0.23
    assert S.allsignal_record(tmp_path / "btc15.db")["fee"] == 0.02


def test_the_service_runs_it_every_poll_beside_the_main_rule():
    src = " ".join(inspect.getsource(main.service).split())
    main_rule = src.index("await cash_out_exit(")
    assert src.index("await allsignal_cash_out(", main_rule) - main_rule < 400


def test_a_signal_that_bought_nothing_is_said_once_and_old_ones_are_not(tmp_path):
    """GOLD 18:45 on 09-28 bought nothing and said nothing: the window looked
    skipped. A restart must not replay the afternoon's misses either."""
    store = Store(str(tmp_path / "btc15.db"))
    store.set_setting("auto_trade_enabled", 1.0, 1)
    settings = Settings(kalshi_series="KXBTC15M", database_path=str(tmp_path / "btc15.db"))
    tg = FakeTelegram()
    old = W - 4 * 900_000
    for window, created in ((old, old + 60_000), (W, W + 60_000)):
        store.allsignal_claim(window, TICKER, "DOWN", 0.73, 1, 0.78, created)
        store.allsignal_finish(window, "unfilled", note="book moved")
    # Held while a retry is still possible (2026-09-30), said once under 2 min left.
    asyncio.run(main.report_allsignal_results(store, settings, tg, W + 120_000))
    assert tg.sent == [], "still retryable"
    now = W + 800_000
    asyncio.run(main.report_allsignal_results(store, settings, tg, now))
    (text,) = tg.sent
    assert text.startswith("⚪ <b>BTC $1 · NOT FILLED</b>") and "limit 78¢" in text
    asyncio.run(main.report_allsignal_results(store, settings, tg, now + 60_000))
    assert len(tg.sent) == 1, "said once"


# --------------------------- the sale price, from the broker (09-28 19:4x)

def test_a_lagging_fills_feed_is_retried_like_the_entry(tmp_path):
    """BTC 18:45 on 09-28: one empty read booked and announced the 0.974 quote;
    the broker filled 0.99."""
    store, settings, client, trader, tg = cash_setup(tmp_path)
    client.lag = 2
    cash(store, settings, tg, trader)
    price, fee, pnl = store.db.execute(
        "SELECT exit_price, exit_fee, pnl FROM allsignal_trades").fetchone()
    assert (price, fee, round(pnl, 2)) == (0.99, 0.0, 0.25)
    assert tg.sent[-1].startswith("💰 <b>BTC $1 · CASHED OUT +$0.24</b>")


def test_a_price_still_missing_is_corrected_from_the_brokers_fills(tmp_path):
    store, settings, client, trader, tg = cash_setup(tmp_path)
    client.lag = 99                                # the feed never caught up
    cash(store, settings, tg, trader)
    price, fee = store.db.execute(
        "SELECT exit_price, exit_fee FROM allsignal_trades").fetchone()
    assert (price, fee) == (0.98, None), "the discounted quote, unconfirmed"
    # The broker's record, as synced: the sale in three pieces, all at 0.99.
    for i, n in enumerate((0.98, 0.01, 0.01)):
        store.db.execute(
            "INSERT INTO fills (fill_id, ticker, order_id, action, side, count, "
            "yes_price, no_price, fee_cost, is_taker, filled_ms, window_ms, synced_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,1,?,?,?)",
            (f"x-{i}", TICKER, "exit-1", "sell", "no", n, 0.99, 0.01, 0.0002,
             W + 700_000, W, W + 760_000))
    store.db.commit()
    store.allsignal_grade(W + 760_000)
    price, count, fee, won, pnl = store.db.execute(
        "SELECT exit_price, exit_count, exit_fee, won, pnl FROM allsignal_trades").fetchone()
    assert (round(price, 4), round(count, 4), won, round(pnl, 2)) == (0.99, 1.0, 1, 0.25)
    assert round(fee, 4) == 0.0006
    assert store.allsignal_reconcile_exits() == 0, "confirmed once"


def test_with_the_cash_out_switched_off_the_position_is_held(tmp_path):
    """Operator, 2026-10-05: "disable cash out for now" (CASH_OUT_ENABLED=false in the live
    .env): at the same 0.99 bid nothing is sold, on either account - held to settlement."""
    store, settings, client, trader, tg = cash_setup(tmp_path)
    settings.cash_out_enabled = False
    cash(store, settings, tg, trader)
    assert client.closes == [], "no sale"
    assert ("exit", TICKER, "UP") not in trader.mirrored, "no mirror exit"
    assert store.db.execute("SELECT exit_price FROM allsignal_trades").fetchone()[0] is None
