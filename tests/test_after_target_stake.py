"""The primary's $6 base, $3 after its daily target until midnight (operator,
2026-09-30: "make primary account base size 6 and apply $3 after the 8% target
hit only to mine the primary; the mirrors stay at the pause when hit target").

Only the primary: the mirrors size from their own budgets and are paused by
their own targets. FINDINGS 117.
"""

import asyncio
import inspect
import sqlite3
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from btc15_signal import daily_profit, main  # noqa: E402
from btc15_signal.capital import ny_day  # noqa: E402
from btc15_signal.config import Settings  # noqa: E402
from btc15_signal.daily_profit import DailyProfitGuard  # noqa: E402
from btc15_signal.execution import ExecutionResult  # noqa: E402
from btc15_signal.store import Store  # noqa: E402
from btc15_signal.validation import contracts_for_budget  # noqa: E402

TICKER = "KXBTC15M-26SEP301215-15"


def settings(tmp_path, after=3.0):
    return Settings(_env_file=None, kalshi_series="KXBTC15M", allsignal_instruments="BTC",
                    allsignal_stake=6.0, allsignal_after_target_stake=after,
                    database_path=str(tmp_path / "btc15.db"))


def guard(tmp_path, account="primary", paused=False, after=0.0, stale=False, budget=6.0,
          pnl=None):
    g = DailyProfitGuard(tmp_path / "g.db", account, account, SimpleNamespace(), 0.08)
    g.after_target_stake = after
    g.entry_budget = budget
    g.error = ""                  # as after its first broker read
    now = int(time.time() * 1000)
    with g.connect() as db:
        db.execute(
            "INSERT OR REPLACE INTO profit_days (account,day,label,opening,captured_ms,"
            "start_ms,basis,target,pnl,peak,paused_ms,updated_ms) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (account, ny_day(now), account, 106.05, now - 1000, now - 1000, "day opening",
             8.48, pnl if pnl is not None else (9.33 if paused else 2.0), 9.33,
             now - 500 if paused else None,
             now - (120_000 if stale else 0)))
    return g


# ------------------------------------------------------------------ the stake

def test_six_until_the_target_then_three(tmp_path):
    store = Store(str(tmp_path / "btc15.db"))
    s = settings(tmp_path)
    store.daily_profit_guards = [guard(tmp_path, paused=False, after=3.0)]
    assert main.allsignal_stake_now(store, s) == 6.0
    store.daily_profit_guards = [guard(tmp_path, paused=True, after=3.0)]
    assert main.allsignal_stake_now(store, s) == 3.0


def test_a_mirror_reaching_its_target_does_not_change_the_primary_stake(tmp_path):
    store = Store(str(tmp_path / "btc15.db"))
    (tmp_path / "m").mkdir()
    store.daily_profit_guards = [guard(tmp_path, "primary", paused=False, after=3.0),
                                 guard(tmp_path / "m", "m1", paused=True)]
    assert main.allsignal_stake_now(store, settings(tmp_path)) == 6.0


def test_zero_keeps_the_old_pause(tmp_path):
    store = Store(str(tmp_path / "btc15.db"))
    store.daily_profit_guards = [guard(tmp_path, paused=True, after=0.0)]
    assert main.allsignal_stake_now(store, settings(tmp_path, after=0.0)) == 6.0


def test_unreadable_figures_give_the_lower_stake(tmp_path):
    store = Store(str(tmp_path / "btc15.db"))
    g = guard(tmp_path, paused=False, after=3.0)

    def locked(*_a):
        raise sqlite3.OperationalError("database is locked")

    g.state = locked
    store.daily_profit_guards = [g]
    assert main.allsignal_stake_now(store, settings(tmp_path)) == 3.0


def test_no_guard_no_change(tmp_path):
    assert main.allsignal_stake_now(Store(str(tmp_path / "b.db")), settings(tmp_path)) == 6.0


# ------------------------------------------------------- the target check

def test_the_primary_keeps_trading_past_its_target_the_mirrors_pause(tmp_path):
    primary = guard(tmp_path, "primary", paused=True, after=3.0)
    assert asyncio.run(primary.block_reason(TICKER, strategy="allsignal")) == ""
    (tmp_path / "m").mkdir()
    mirror = guard(tmp_path / "m", "m1", paused=True, after=0.0)
    assert "paused until midnight" in asyncio.run(
        mirror.block_reason(TICKER, strategy="allsignal"))


def test_unknown_figures_still_block_the_primary_past_its_target(tmp_path):
    primary = guard(tmp_path, "primary", paused=True, after=3.0, stale=True)
    primary.pages = AsyncMock(side_effect=RuntimeError("kalshi down"))
    assert "unavailable" in asyncio.run(primary.block_reason(TICKER, strategy="allsignal"))


def test_only_the_primary_guard_gets_the_lower_stake_in_service():
    src = " ".join(inspect.getsource(main.service).split())
    primary = src.split('if account == "primary":', 1)[1].split("else: # mirrors", 1)[0]
    assert "guard.after_target_stake = float(" in primary
    assert DailyProfitGuard.after_target_stake == 0.0, "every other account pauses"


# ------------------------------------------------------------ the order

class FakeClient:
    def __init__(self):
        self.orders = []

    async def execute_with_take_profit(self, order, slippage, ceiling=None):
        self.orders.append(order)
        return ExecutionResult("filled", order.count, "oid", None, "")

    async def fill_detail(self, order_id, side):
        return (0.73, 1.0, 0.01)


def _fire(store, s, client, window):
    async def go():
        main.spawn_allsignal(store, s, client, TICKER, "UP", 0.73, window, window + 60_000)
        await asyncio.gather(*list(main.ALLSIGNAL_TASKS))
    asyncio.run(go())


def test_the_order_is_sized_and_recorded_at_its_stake(tmp_path):
    store = Store(str(tmp_path / "btc15.db"))
    store.set_setting("auto_trade_enabled", 1.0, 1)
    s = settings(tmp_path)
    client = FakeClient()
    store.daily_profit_guards = [guard(tmp_path, paused=False, after=3.0)]
    _fire(store, s, client, 1_790_769_600_000)
    store.daily_profit_guards = [guard(tmp_path, paused=True, after=3.0)]
    _fire(store, s, client, 1_790_770_500_000)
    assert [o.count for o in client.orders] == [contracts_for_budget(6.0, 0.73),
                                                 contracts_for_budget(3.0, 0.73)] == [8, 4]
    stakes = [r[0] for r in store.db.execute(
        "SELECT stake FROM allsignal_trades ORDER BY window_open")]
    assert stakes == [6.0, 3.0]


def test_every_message_names_the_stake_its_trade_was_sized_at(tmp_path):
    s = settings(tmp_path)
    assert main._row_stake({"stake": 3.0}, s) == 3.0
    assert main._row_stake({"stake": None}, s) == 6.0, "rows from before carry none"
    assert main._row_stake({}, s) == 6.0


def test_an_old_book_gets_the_stake_column(tmp_path):
    path = tmp_path / "old.db"
    db = sqlite3.connect(path)
    db.execute("""CREATE TABLE allsignal_trades (
        window_open INTEGER PRIMARY KEY, ticker TEXT NOT NULL, side TEXT NOT NULL,
        ask REAL NOT NULL, count REAL NOT NULL, limit_price REAL NOT NULL,
        created_ms INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'claimed',
        order_id TEXT, filled REAL, fill_price REAL, fee REAL, note TEXT,
        won INTEGER, pnl REAL, graded_ms INTEGER)""")
    db.execute("INSERT INTO allsignal_trades (window_open,ticker,side,ask,count,limit_price,"
               "created_ms) VALUES (1,'T','UP',0.7,7,0.75,1)")
    db.commit()
    db.close()
    store = Store(str(path))
    assert store.allsignal_claim(2, "T", "UP", 0.7, 4, 0.75, 2, stake=3.0)
    rows = dict(store.db.execute("SELECT window_open, stake FROM allsignal_trades"))
    assert rows == {1: None, 2: 3.0}


# ------------------------------------------------------------ what it says

def test_the_accounts_table_says_target_hit_and_the_lower_stake(tmp_path):
    (tmp_path / "m").mkdir()
    text = daily_profit.summary([guard(tmp_path, "primary", paused=True, after=3.0),
                                 guard(tmp_path / "m", "m1", paused=True)])
    assert "HIT $3" in text and "PAUSED" in text
    # WHO is paused and who continues, by name (operator, 2026-09-30)
    assert ("\u2b07\ufe0f <b>primary</b> \u00b7 at or above the target \u00b7 <b>$3</b> per "
            "signal \u00b7 back to $6 if the day drops below it \u00b7 resets 00:00 ET") in text
    assert ("\u23f8 <b>m1</b> \u00b7 target hit \u00b7 no new BTC entries until 00:00 ET; "
            "open positions still exit") in text
    assert "Paused accounts" not in text
    alone = daily_profit.summary([guard(tmp_path, "primary", paused=True, after=3.0)])
    assert "\u23f8" not in alone
    assert "⬇" in daily_profit.compact([guard(tmp_path, "primary", paused=True, after=3.0)])


def test_the_target_message_says_it_continues_at_three(tmp_path):
    g = guard(tmp_path, "primary", paused=True, after=3.0, budget=6.0)
    g.refresh = AsyncMock()
    sent = []

    class TG:
        async def send(self, text):
            sent.append(text)
            return 1

    asyncio.run(daily_profit._monitor_one(g, TG()))
    assert "TARGET REACHED" in sent[0]
    assert ("<b>$3</b> per signal while the day stays at or above it \u00b7 back to $6 "
            "if it drops below") in sent[0]
    assert "paused" not in sent[0].lower()
    m = guard(tmp_path / "x", "m1", paused=True) if (tmp_path / "x").mkdir() is None else None
    m.refresh = AsyncMock()
    asyncio.run(daily_profit._monitor_one(m, TG()))
    assert "New BTC entries paused until 00:00 ET" in sent[1], "a mirror still pauses"


def test_the_setting_is_off_by_default():
    assert Settings.model_fields["allsignal_after_target_stake"].default == 0.0


# --------------------------------------- review fixes (wf_2f6943e5-bac, 09-30)

def test_only_the_dollar_strategy_passes_the_primary_target(tmp_path):
    """Main strategy, recovery adds and manual presses still pause at it."""
    primary = guard(tmp_path, "primary", paused=True, after=3.0)
    assert asyncio.run(primary.block_reason(TICKER, strategy="allsignal")) == ""
    for other in ("", "primary", "recovery"):
        assert "paused until midnight" in asyncio.run(primary.block_reason(TICKER, strategy=other))


def test_the_order_passes_its_strategy_to_the_target_check():
    from btc15_signal.execution import KalshiExecutionClient

    client = object.__new__(KalshiExecutionClient)
    client.daily_profit_guard = SimpleNamespace(block_reason=AsyncMock(return_value="paused"))
    client._post = AsyncMock()
    asyncio.run(client.execute_with_take_profit(SimpleNamespace(
        ticker=TICKER, strategy="allsignal", count=4, entry_limit=0.73)))
    client.daily_profit_guard.block_reason.assert_awaited_with(
        TICKER, strategy="allsignal", count=4, price=0.73)
    asyncio.run(client.place_resting_buy(TICKER, "UP", 0.7, 1, 1, "primary"))
    client.daily_profit_guard.block_reason.assert_awaited_with(
        TICKER, strategy="", count=None, price=None)


def test_an_entry_sized_before_the_target_was_known_is_skipped_not_sent_at_six(tmp_path):
    """The order-time refresh found the target: that entry was sized at $6."""
    g = guard(tmp_path, "primary", paused=False, after=3.0, stale=True)
    g.error = "ConnectError"

    async def refresh(force=False):
        with g.connect() as db:
            # reaching the target: the hit time AND the day's P&L at/above it
            db.execute("UPDATE profit_days SET paused_ms=?, updated_ms=?, pnl=9.33",
                       (int(time.time() * 1000), int(time.time() * 1000)))
        g.error = ""

    g.refresh = refresh
    six = contracts_for_budget(6.0, 0.73)
    reason = asyncio.run(g.block_reason(TICKER, strategy="allsignal", count=six, price=0.73))
    assert "sized at the base stake - skipped" in reason and "$3" in reason
    assert "profit target" not in reason, "announced, not silently marked"
    three = contracts_for_budget(3.0, 0.73)
    assert asyncio.run(g.block_reason(TICKER, strategy="allsignal", count=three, price=0.73)) == ""


def test_a_six_dollar_entry_never_passes_once_the_target_is_known(tmp_path):
    """Second check (09-30): sized at $6 an instant before the monitor set the
    target, it must still be refused - the size decides, not the timing."""
    g = guard(tmp_path, "primary", paused=True, after=3.0)
    six = contracts_for_budget(6.0, 0.70)
    assert "skipped" in asyncio.run(g.block_reason(TICKER, strategy="allsignal",
                                                   count=six, price=0.70))
    assert asyncio.run(g.block_reason(TICKER, strategy="allsignal",
                                      count=contracts_for_budget(3.0, 0.70), price=0.70)) == ""


def test_a_retry_after_the_target_goes_at_the_lower_stake():
    src = inspect.getsource(main.allsignal_retry_poll)
    # (2026-10-05, FINDINGS 163: and for its own window - the after-a-loss stake.)
    assert "stake = allsignal_stake_now(store, settings, opened)" in src
    # Sized at the stake NOW - and, since 2026-10-05, at the price now (the chase review:
    # every account sizes its copy by the order's entry price).
    assert "dynamic_count = allsignal_count_now(store, settings, cap, opened, quote=ask)" in src
    assert 'count = min(int(row["count"]), dynamic_count)' in src
    assert "count, cap, opened, telegram))" in src


def test_the_switch_to_three_is_announced_once_even_after_a_pause_notice(tmp_path):
    """Today's deploy: the 03:00 notice said 'paused'; the change says $3 once."""
    g = guard(tmp_path, "primary", paused=True, after=3.0, budget=6.0)
    with g.connect() as db:
        db.execute("UPDATE profit_days SET notified='paused'")
    g.refresh = AsyncMock()
    sent = []

    class TG:
        async def send(self, text):
            sent.append(text)
            return 1

    asyncio.run(daily_profit._monitor_one(g, TG()))
    asyncio.run(daily_profit._monitor_one(g, TG()))
    assert len(sent) == 1 and "<b>$3</b> per signal while the day stays" in sent[0]
    assert g.state()["notified"] == "lowered"
    (tmp_path / "m").mkdir()
    m = guard(tmp_path / "m", "m1", paused=True)
    with m.connect() as db:
        db.execute("UPDATE profit_days SET notified='paused'")
    m.refresh = AsyncMock()
    asyncio.run(daily_profit._monitor_one(m, TG()))
    assert len(sent) == 1, "a paused mirror says nothing new"


def test_the_table_says_no_data_when_the_lower_stake_is_blocked_anyway(tmp_path):
    g = guard(tmp_path, "primary", paused=True, after=3.0, stale=True)
    g.error = "ConnectError"
    assert "NO DATA" in daily_profit.summary([g]) and "HIT $3" not in daily_profit.summary([g])


def test_the_lower_stake_can_never_raise_the_stake(tmp_path):
    store = Store(str(tmp_path / "btc15.db"))
    store.daily_profit_guards = [guard(tmp_path, paused=True, after=9.0)]
    assert main.allsignal_stake_now(store, settings(tmp_path, after=9.0)) == 6.0


def test_labels_for_skipped_rows_and_the_session_summary():
    src = inspect.getsource(main.report_allsignal_results)
    assert 'if row["status"] == "skipped" and not row.get("stake")' in src
    from btc15_signal import messages

    text = messages.shadow_summary_message(
        session="us", ny_day="2026-09-30", rows=[], budget=6.0, after_target=3.0,
        allsignal=[("BTC", {"n": 4, "wins": 3, "pnl": 1.0, "fee": 0.1, "open": 0, "missed": 0})])
    assert "ALL-SIGNAL $6 ($3 while at the target)" in text
    assert "$6 ($3 while at the target) per primary signal" in text


def test_the_table_names_both_paused_mirrors_and_a_blocked_account(tmp_path):
    for sub in ("a", "b", "c"):
        (tmp_path / sub).mkdir()
    g1 = guard(tmp_path / "a", "m1", paused=True)
    g1.label = "Wife"
    g2 = guard(tmp_path / "b", "m2", paused=True)
    g2.label = "Uncle George"
    g3 = guard(tmp_path / "c", "primary", paused=True, after=3.0, stale=True)
    g3.label, g3.error = "Primary", "ConnectError"
    text = daily_profit.summary([g3, g1, g2])
    assert "<b>Wife and Uncle George</b> \u00b7 target hit \u00b7 no new BTC entries" in text
    assert "<b>Primary</b> \u00b7 Kalshi figures unavailable" in text



# ----------------- the stake follows the day (operator, 2026-09-30 17:5x)

def test_losses_back_below_the_target_bring_the_stake_back_to_six(tmp_path):
    store = Store(str(tmp_path / "btc15.db"))
    s = settings(tmp_path)
    store.daily_profit_guards = [guard(tmp_path, paused=True, after=3.0, pnl=4.67)]
    assert main.allsignal_stake_now(store, s) == 6.0, "hit earlier, below now: $6"
    store.daily_profit_guards = [guard(tmp_path, paused=True, after=3.0, pnl=8.48)]
    assert main.allsignal_stake_now(store, s) == 3.0, "at the target again: $3"


def test_below_the_target_again_a_six_dollar_entry_is_allowed(tmp_path):
    g = guard(tmp_path, "primary", paused=True, after=3.0, pnl=4.67)
    six = contracts_for_budget(6.0, 0.70)
    assert asyncio.run(g.block_reason(TICKER, strategy="allsignal", count=six, price=0.70)) == ""
    assert "paused until midnight" in asyncio.run(g.block_reason(TICKER, strategy="primary")), \
        "other strategies still pause once the target was hit"


def test_each_change_of_side_is_announced_once(tmp_path):
    g = guard(tmp_path, "primary", paused=True, after=3.0, budget=6.0, pnl=9.33)
    g.refresh = AsyncMock()
    sent = []

    class TG:
        async def send(self, text):
            sent.append(text)
            return 1

    def at(pnl):
        with g.connect() as db:
            db.execute("UPDATE profit_days SET pnl=?", (pnl,))
        asyncio.run(daily_profit._monitor_one(g, TG()))
        asyncio.run(daily_profit._monitor_one(g, TG()))

    at(9.33)
    at(4.67)
    at(8.60)
    assert len(sent) == 3
    assert "TARGET REACHED" in sent[0] and "AGAIN" not in sent[0]
    assert "BACK BELOW TARGET" in sent[1] and "Back to <b>$6</b> per signal" in sent[1]
    assert "TARGET REACHED AGAIN" in sent[2]


def test_the_table_says_back_to_six(tmp_path):
    text = daily_profit.summary([guard(tmp_path, "primary", paused=True, after=3.0, pnl=4.67)])
    assert "BACK $6" in text
    assert ("\u2b06\ufe0f <b>primary</b> \u00b7 back below the target \u00b7 <b>$6</b> per signal "
            "until it is reached again") in text
