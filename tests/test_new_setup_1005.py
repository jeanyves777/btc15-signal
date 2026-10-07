"""THE OPERATOR'S NEW SETUP (2026-10-05, FINDINGS 163).

"Let implement I $30 boost only below 8%" and "Mirrors: boost all three by $1 // and
stop for the day at 8% while we make one more change to Affoue mirror account that
account become the account that keep trading after target hit, and for that account
set it to $6 base and $8 after 1 loss and the after target hit $3."

  PRIMARY  $25; $30 for the 2 taken trades after a known loss, below the target; done
           for the day at 8% (the daily cap, an existing mechanism).
  MIRRORS  the same after-a-loss window: the day's stake + $1 (Wife $4, George $3);
           Affoue $6, $8 after a loss, $3 at/above its OWN target, never paused there.
  Once the primary is done for the day its $ signals go to the mirrors still trading
  by their own day ('copied' rows), and those rows keep the cushion, the trend skip
  and the boost reading one sequence. A copied row is never the primary's money.

REAL Store, DailyProfitGuard, MirrorTarget, _Mirror and MirroringExecutionClient - a
stand-in once hid a frozen dataclass (tests-use-the-real-classes, 2026-10-01).
"""

import asyncio
import inspect
import sys
import time
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from btc15_signal import daily_profit, main, shadow_summary  # noqa: E402
from btc15_signal import mirror as mirror_mod  # noqa: E402
from btc15_signal.capital import ny_day  # noqa: E402
from btc15_signal.config import Settings  # noqa: E402
from btc15_signal.daily_profit import DailyProfitGuard  # noqa: E402
from btc15_signal.execution import ExecutionResult  # noqa: E402
from btc15_signal.mirror import MirroringExecutionClient, MirrorTarget  # noqa: E402
from btc15_signal.store import Store, TradeProposal  # noqa: E402
from btc15_signal.validation import contracts_for_budget  # noqa: E402

NY = ZoneInfo("America/New_York")
OPEN = int(datetime(2026, 10, 5, 12, 0, tzinfo=NY).timestamp() * 1000)


def W(k):
    """The window k windows before OPEN, the same New York day."""
    return OPEN - k * 900_000


def settings(tmp_path, **kw):
    base = dict(_env_file=None, kalshi_series="KXBTC15M", allsignal_instruments="BTC",
                allsignal_stake=25.0, allsignal_after_target_stake=0.0,
                allsignal_after_loss_stake=30.0, allsignal_after_loss_trades=2,
                mirror_allsignal_after_loss_add=1.0, mirror_3_allsignal_after_loss_stake=8.0,
                mirror_3_allsignal_after_target_stake=3.0, mirror_after_primary_done=True,
                database_path=str(tmp_path / "btc15.db"))
    base.update(kw)
    return Settings(**base)


def trade(store, wo, won=None, status="filled", side="UP", pred=None):
    """A $ row in window `wo`; `pred` = (side, won) of the alert's prediction."""
    store.allsignal_claim(wo, f"KXBTC15M-T{wo}", side, 0.75, 33, 0.77, wo + 60_000, stake=25.0)
    store.allsignal_finish(wo, status, filled=33 if status == "filled" else 0,
                           fill_price=0.75 if status == "filled" else None)
    if won is not None:
        store.db.execute("UPDATE allsignal_trades SET won=?, pnl=? WHERE window_open=?",
                         (int(won), 8.25 if won else -24.75, wo))
    if pred is not None:
        store.db.execute(
            "INSERT INTO predictions (window_open, created_at, target, entry_price, side, "
            "bucket, raw_probability, won) VALUES (?,?,?,?,?,?,?,?)",
            (wo, wo + 60_000, 1.0, 0.75, pred[0], 0, 0.8, pred[1]))
    store.db.commit()


def guard(tmp_path, account, *, budget, opening, target, pnl=0.0, after=0.0, stop=0.0,
          rate=0.15, stale=False, capped=False):
    g = DailyProfitGuard(tmp_path / f"{account}.db", account, account, SimpleNamespace(), rate)
    g.entry_budget, g.after_target_stake, g.stop_rate, g.error = budget, after, stop, ""
    if account != "primary":
        g.max_target_wins, g.stake_rate, g.stake_max = 7.0, 0.02, 6.0
    now = int(time.time() * 1000)
    at = pnl + 1e-8 >= target
    with g.connect() as db:
        db.execute(
            "INSERT INTO profit_days (account,day,label,opening,captured_ms,start_ms,basis,"
            "target,pnl,peak,paused_ms,capped_ms,updated_ms) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (account, ny_day(now), account, opening, now - 1000, now - 1000, "day opening",
             target, pnl, max(pnl, 0.0), now - 500 if at else None,
             now - 500 if capped else None, now - (120_000 if stale else 0)))
    return g


def sub(tmp_path, name):
    (tmp_path / name).mkdir(exist_ok=True)
    return tmp_path / name


def primary_guard(tmp_path, pnl=10.0, **kw):
    return guard(tmp_path, "primary", budget=25.0, opening=995.93, target=79.67, pnl=pnl,
                 rate=0.08, **kw)


# --------------------------------------------------------- the primary's stake

def test_thirty_for_the_two_taken_trades_after_a_known_loss(tmp_path):
    store, s = Store(str(tmp_path / "btc15.db")), settings(tmp_path)
    store.daily_profit_guards = [primary_guard(tmp_path)]
    assert main.allsignal_stake_now(store, s, W(6)) == 25.0, "no trade yet"
    trade(store, W(6), won=True)
    trade(store, W(5), won=False)
    assert main.allsignal_stake_now(store, s, W(4)) == 30.0, "1st after the loss"
    trade(store, W(4), won=True)
    assert main.allsignal_stake_now(store, s, W(3)) == 30.0, "2nd after the loss"
    trade(store, W(3), won=True)
    assert main.allsignal_stake_now(store, s, W(2)) == 25.0, "then the base again"


def test_a_loss_inside_restarts_the_two(tmp_path):
    store, s = Store(str(tmp_path / "btc15.db")), settings(tmp_path)
    store.daily_profit_guards = [primary_guard(tmp_path)]
    trade(store, W(6), won=False)
    trade(store, W(5), won=True)
    trade(store, W(4), won=False)                 # inside the two: restarts
    trade(store, W(3), won=True)
    assert main.allsignal_stake_now(store, s, W(2)) == 30.0
    trade(store, W(2), won=True)
    assert main.allsignal_stake_now(store, s, W(1)) == 25.0


def test_skipped_unfilled_and_unknown_are_not_trades_or_losses(tmp_path):
    store, s = Store(str(tmp_path / "btc15.db")), settings(tmp_path)
    store.daily_profit_guards = [primary_guard(tmp_path)]
    trade(store, W(6), won=False)
    trade(store, W(5), status="skipped")
    trade(store, W(4), status="unfilled")
    assert main.allsignal_stake_now(store, s, W(3)) == 30.0, "still the 1st after the loss"
    store2 = Store(str(tmp_path / "b2.db"))
    store2.daily_profit_guards = store.daily_profit_guards
    trade(store2, W(6), won=None)                  # not settled yet: not a loss
    assert main.allsignal_stake_now(store2, s, W(5)) == 25.0


def test_never_at_or_above_the_target(tmp_path):
    store = Store(str(tmp_path / "btc15.db"))
    trade(store, W(6), won=False)
    store.daily_profit_guards = [primary_guard(tmp_path, pnl=80.0)]
    assert main.allsignal_stake_now(store, settings(tmp_path), W(5)) == 25.0, "no boost"
    s10 = settings(tmp_path, allsignal_after_target_stake=10.0)
    assert main.allsignal_stake_now(store, s10, W(5)) == 10.0


def test_off_is_the_old_rule(tmp_path):
    store = Store(str(tmp_path / "btc15.db"))
    store.daily_profit_guards = [primary_guard(tmp_path)]
    trade(store, W(6), won=False)
    off = settings(tmp_path, allsignal_after_loss_stake=0.0)
    assert main.allsignal_stake_now(store, off, W(5)) == 25.0
    assert main.allsignal_stake_now(store, off) == 25.0, "the old call still works"


def test_spawn_and_the_retry_size_for_their_own_window():
    assert "stake = allsignal_stake_now(store, settings, opened)" in inspect.getsource(
        main.spawn_allsignal)
    assert "stake = allsignal_stake_now(store, settings, opened)" in inspect.getsource(
        main.allsignal_retry_poll)


# ------------------------------------------------- copied rows carry the rules

def test_a_copied_loss_drives_the_cushion_the_streak_and_the_boost(tmp_path):
    store, s = Store(str(tmp_path / "btc15.db")), settings(tmp_path)
    trade(store, W(6), status="copied", pred=("UP", 0))
    trade(store, W(5), status="copied", pred=("DOWN", 0), side="DOWN")
    assert main.allsignal_after_loss(store, W(4)) is True
    assert main.allsignal_loss_streak(store, W(4), 2) is True
    assert main.allsignal_after_loss_boost(store, s, W(4)) is True
    trade(store, W(4), status="copied", pred=("UP", 1))
    assert main.allsignal_after_loss(store, W(3)) is False
    assert main.allsignal_loss_streak(store, W(3), 2) is False


def test_a_copied_row_with_no_readable_result_is_unknown(tmp_path):
    store, s = Store(str(tmp_path / "btc15.db")), settings(tmp_path)
    trade(store, W(6), status="copied", pred=("DOWN", 1))   # the other side: not ours
    trade(store, W(5), status="copied")                     # no prediction at all
    assert main.allsignal_after_loss(store, W(4)) is True, "the cushion waits when unsure"
    assert main.allsignal_loss_streak(store, W(4), 1) is False, "not a KNOWN loss"
    assert main.allsignal_after_loss_boost(store, s, W(4)) is False, "the smaller stake"


def test_a_prediction_on_the_other_side_never_decides_a_copied_row(tmp_path):
    store, s = Store(str(tmp_path / "btc15.db")), settings(tmp_path)
    trade(store, W(6), status="copied", side="UP", pred=("DOWN", 0))
    assert main.allsignal_loss_streak(store, W(5), 1) is False
    assert main.allsignal_after_loss_boost(store, s, W(5)) is False


def test_filled_rows_read_exactly_as_before(tmp_path):
    """The prediction never decides a FILLED row: its own result or its settlement."""
    store = Store(str(tmp_path / "btc15.db"))
    trade(store, W(6), won=None, pred=("UP", 1))             # unsettled, prediction says won
    assert main.allsignal_after_loss(store, W(5)) is True, "unknown counts, as before"
    assert main.allsignal_loss_streak(store, W(5), 1) is False


def test_a_copied_row_is_never_the_primarys_money(tmp_path):
    store = Store(str(tmp_path / "btc15.db"))
    trade(store, W(6), won=True)
    trade(store, W(5), status="copied", pred=("UP", 0))
    store.db.execute("INSERT INTO settlements (ticker, market_result) VALUES (?, 'no')",
                     (f"KXBTC15M-T{W(5)}",))
    store.db.commit()
    store.allsignal_grade(OPEN + 3_600_000)
    row = store._dicts("SELECT * FROM allsignal_trades WHERE window_open=?", (W(5),))[0]
    assert row["won"] is None and row["pnl"] is None, "never graded"
    assert [r["window_open"] for r in store.allsignal_unannounced_misses(0)] == []
    rec = shadow_summary.allsignal_record(Path(tmp_path / "btc15.db"))
    assert rec["n"] == 1, rec
    stats = shadow_summary.allsignal_stats(Path(tmp_path / "btc15.db"), W(10), OPEN)
    assert stats["n"] == 1 and stats["missed"] == 0 and stats["open"] == 0, stats


def test_the_order_path_books_a_copied_signal(tmp_path):
    store, s = Store(str(tmp_path / "btc15.db")), settings(tmp_path)
    store.allsignal_claim(W(1), "KXBTC15M-X", "UP", 0.75, 33, 0.77, W(1) + 1, stake=25.0)
    trader = SimpleNamespace(execute_with_take_profit=AsyncMock(return_value=ExecutionResult(
        "copied", 0, "", None, "primary done for the day - copied to m3")))
    asyncio.run(main._allsignal_order(store, s, trader, "KXBTC15M-X", "UP", 0.75, 33, 0.77,
                                      W(1)))
    row = store._dicts("SELECT * FROM allsignal_trades WHERE window_open=?", (W(1),))[0]
    assert row["status"] == "copied" and "copied to m3" in row["note"]
    assert not row["filled"] and row["won"] is None


# ------------------------------------------------------- the mirrors' stakes

def mirrors(tmp_path, *, m1_pnl=0.0, m3_pnl=0.0):
    d = sub(tmp_path, "g")
    return [guard(d, "m1", budget=3.0, opening=37.93, target=5.69, pnl=m1_pnl),
            guard(d, "m2", budget=2.0, opening=29.39, target=3.50),
            guard(d, "m3", budget=6.0, opening=119.51, target=14.0, pnl=m3_pnl, after=3.0)]


def test_mirror_day_stakes_without_a_loss(tmp_path):
    store, s = Store(str(tmp_path / "btc15.db")), settings(tmp_path)
    store.daily_profit_guards = mirrors(tmp_path)
    trade(store, W(6), won=True)
    assert [main.mirror_stake_now(store, s, n, W(5)) for n in ("m1", "m2", "m3")] == [3, 2, 6]


def test_after_a_loss_one_dollar_more_and_affoue_eight(tmp_path):
    store, s = Store(str(tmp_path / "btc15.db")), settings(tmp_path)
    store.daily_profit_guards = mirrors(tmp_path)
    trade(store, W(6), won=False)
    assert [main.mirror_stake_now(store, s, n, W(5)) for n in ("m1", "m2", "m3")] == [4, 3, 8]
    trade(store, W(5), won=True)
    trade(store, W(4), won=True)
    assert [main.mirror_stake_now(store, s, n, W(3)) for n in ("m1", "m2", "m3")] == [3, 2, 6]


def test_affoue_at_its_own_target_goes_at_three_even_after_a_loss(tmp_path):
    store, s = Store(str(tmp_path / "btc15.db")), settings(tmp_path)
    store.daily_profit_guards = mirrors(tmp_path, m3_pnl=14.5, m1_pnl=6.0)
    trade(store, W(6), won=False)
    assert main.mirror_stake_now(store, s, "m3", W(5)) == 3.0
    # Wife at her target trades nothing anyway (paused); the stake is not lowered.
    assert main.mirror_stake_now(store, s, "m1", W(5)) == 4.0


def test_no_rule_or_no_guard_is_the_days_stake(tmp_path):
    store = Store(str(tmp_path / "btc15.db"))
    store.daily_profit_guards = mirrors(tmp_path)
    trade(store, W(6), won=False)
    off = settings(tmp_path, mirror_allsignal_after_loss_add=0.0,
                   mirror_3_allsignal_after_loss_stake=0.0)
    assert [main.mirror_stake_now(store, off, n, W(5)) for n in ("m1", "m2", "m3")] == [3, 2, 6]
    assert main.mirror_stake_now(store, off, "m9", W(5)) is None


# -------------------------------------------- Affoue's own day past its target

def test_affoue_trades_on_at_three_past_its_target_and_wife_still_pauses(tmp_path):
    t = "KXBTC15M-26OCT051215-15"
    g1, _, g3 = mirrors(tmp_path, m1_pnl=6.0, m3_pnl=14.5)
    three, six = contracts_for_budget(3.0, 0.75), contracts_for_budget(6.0, 0.75)
    assert asyncio.run(g3.block_reason(t, "allsignal", count=three, price=0.75)) == ""
    assert "skipped" in asyncio.run(g3.block_reason(t, "allsignal", count=six, price=0.75))
    assert "paused" in asyncio.run(g1.block_reason(t, "allsignal", count=4, price=0.75))
    assert g3.taking_entries("allsignal") and not g1.taking_entries("allsignal")
    assert not g3.taking_entries("main"), "only the $ strategy trades on past it"


def test_taking_entries_and_done_for_day_fail_closed(tmp_path):
    g = guard(tmp_path, "m2", budget=2.0, opening=29.39, target=3.5)
    assert g.taking_entries("allsignal")
    stale = guard(sub(tmp_path, "s"), "m2", budget=2.0, opening=29.39, target=3.5, stale=True)
    assert not stale.taking_entries("allsignal")
    def locked(*a, **k):
        raise RuntimeError("database is locked")

    stale.state = locked
    assert not stale.taking_entries("allsignal") and not stale.done_for_day()
    p = primary_guard(sub(tmp_path, "p"), pnl=80.0, stop=0.08)
    assert p.done_for_day() and not p.taking_entries("allsignal")
    q = primary_guard(sub(tmp_path, "q"), pnl=10.0, stop=0.08, capped=True)
    assert q.done_for_day(), "latched"
    r = primary_guard(sub(tmp_path, "r"), pnl=80.0, stop=0.0)
    assert not r.done_for_day(), "no cap, never done"


# ------------------------------------------------------ the copying client

class Fake:
    """A broker account. A MIRROR's (checks=True) runs its own real guard's order
    check first, as KalshiExecutionClient.entry_block_reason does."""

    def __init__(self, status="filled", checks=True):
        self.counts, self.status, self.checks, self.calls = [], status, checks, 0
        self.daily_profit_guard = None
        self.last_funding_note = ""

    async def execute_with_take_profit(self, prop, slippage=0.0, ceiling=None):
        self.calls += 1
        if self.checks and self.daily_profit_guard is not None:
            reason = await self.daily_profit_guard.block_reason(
                prop.ticker, prop.strategy, count=prop.count, price=prop.entry_limit)
            if reason:
                return ExecutionResult("paused", 0, "", None, reason)
        self.counts.append(prop.count)
        if self.status == "paused":
            return ExecutionResult("paused", 0, "", None, "blocked")
        if self.status == "unfilled":
            return ExecutionResult("unfilled", 0, "oid", None, "No fill; the book moved")
        return ExecutionResult("filled", prop.count, "oid", None, "")

    async def close(self):
        pass


def wired(monkeypatch, tmp_path, primary, guards, flags=None):
    made = []

    def ctor(base_url, key_id, key_path):
        made.append(Fake())
        return made[-1]

    monkeypatch.setattr(mirror_mod, "KalshiExecutionClient", ctor)
    targets = [MirrorTarget("m1", "k", "p", allsignal_budget=3.0),
               MirrorTarget("m2", "k", "p", allsignal_budget=2.0),
               MirrorTarget("m3", "k", "p", allsignal_budget=6.0)]
    w = MirroringExecutionClient(primary, targets, "https://x",
                                 log_path=str(tmp_path / "mirror.jsonl"),
                                 gate=(lambda n: flags[n]) if flags else None)
    for m, g in zip(w._mirrors, guards):
        m.client.daily_profit_guard = g
    store, s = Store(str(tmp_path / "btc15.db")), settings(tmp_path)
    store.daily_profit_guards = [primary.daily_profit_guard, *guards]
    w.stake_for = lambda name, p: main.mirror_stake_now(store, s, name, int(p.window_open))
    w.copy_after_primary_done = True
    return w, made, store


def order(wo=OPEN, ask=0.75):
    return TradeProposal(id=f"allsignal:{wo}", strategy="allsignal", window_open=wo,
                         ticker="KXBTC15M-26OCT051215-15", side="UP", entry_limit=ask,
                         take_profit=0.0, count=33, expires_at=wo + 900_000,
                         close_ms=wo + 900_000, status="claimed")


def run(w, prop):
    async def go():
        result = await w.execute_with_take_profit(prop, 0.02, 0.77)
        for m in w._mirrors:
            await m.queue.join()
        return result
    return asyncio.run(go())


def test_once_the_primary_is_done_the_signal_goes_to_the_mirrors_still_trading(
        tmp_path, monkeypatch):
    primary = Fake("paused", checks=False)
    primary.daily_profit_guard = primary_guard(tmp_path, pnl=80.0, stop=0.08)
    guards = mirrors(tmp_path, m1_pnl=6.0, m3_pnl=14.5)   # Wife paused, George and Affoue on
    w, (wife, george, affoue), store = wired(monkeypatch, tmp_path, primary, guards)
    result = run(w, order())
    assert result.status == "copied", result
    assert result.note == (main.COPY_NOTE + f"m2 {contracts_for_budget(2.0, 0.75)}/"
                           f"{contracts_for_budget(2.0, 0.75)} ($2); m3 "
                           f"{contracts_for_budget(3.0, 0.75)}/{contracts_for_budget(3.0, 0.75)}"
                           " ($3)"), result.note
    assert wife.calls == 0, "paused at her own target: never even sent"
    assert george.counts == [contracts_for_budget(2.0, 0.75)]
    assert affoue.counts == [contracts_for_budget(3.0, 0.75)], "past its target: $3"


def test_a_primary_blocked_for_anything_else_copies_nothing(tmp_path, monkeypatch):
    primary = Fake("paused", checks=False)
    primary.daily_profit_guard = primary_guard(tmp_path, pnl=10.0, stop=0.08)  # not done
    w, made, _ = wired(monkeypatch, tmp_path, primary, mirrors(tmp_path))
    assert run(w, order()).status == "paused"
    assert all(m.counts == [] for m in made)


def test_switched_off_or_nobody_trading_stays_paused(tmp_path, monkeypatch):
    primary = Fake("paused", checks=False)
    primary.daily_profit_guard = primary_guard(tmp_path, pnl=80.0, stop=0.08)
    w, made, _ = wired(monkeypatch, tmp_path, primary, mirrors(tmp_path),
                       flags={"m1": True, "m2": True, "m3": True})
    w.copy_after_primary_done = False
    assert run(w, order()).status == "paused" and all(m.counts == [] for m in made)
    x = sub(tmp_path, "x")
    w2, made2, _ = wired(monkeypatch, x, primary, mirrors(x, m1_pnl=6.0),
                         flags={"m1": True, "m2": False, "m3": False})
    assert run(w2, order()).status == "paused" and all(m.counts == [] for m in made2)


def test_a_normal_copy_is_sized_by_each_mirrors_rule(tmp_path, monkeypatch):
    primary = Fake("filled", checks=False)
    primary.daily_profit_guard = primary_guard(tmp_path, pnl=10.0, stop=0.08)
    w, (wife, george, affoue), store = wired(monkeypatch, tmp_path, primary, mirrors(tmp_path))
    trade(store, W(1), won=False)
    assert run(w, order()).status == "filled"
    assert wife.counts == [contracts_for_budget(4.0, 0.75)]
    assert george.counts == [contracts_for_budget(3.0, 0.75)]
    assert affoue.counts == [contracts_for_budget(8.0, 0.75)]


def test_a_stake_rule_that_fails_leaves_the_days_stake(tmp_path, monkeypatch):
    primary = Fake("filled", checks=False)
    primary.daily_profit_guard = primary_guard(tmp_path, pnl=10.0, stop=0.08)
    w, (wife, george, affoue), _ = wired(monkeypatch, tmp_path, primary, mirrors(tmp_path))
    w.stake_for = lambda name, p: 1 / 0
    assert run(w, order()).status == "filled"
    assert affoue.counts == [contracts_for_budget(6.0, 0.75)]
    assert wife.counts == [contracts_for_budget(3.0, 0.75)]


# ------------------------------------------------------------- the service

def test_the_service_wires_it():
    src = " ".join(inspect.getsource(main.service).split())
    mirrors_branch = src.split("else: # mirrors: the target scales with growth", 1)[1].split(
        "client.daily_profit_guard = guard", 1)[0]
    assert ('guard.after_target_stake = float(getattr( settings, '
            'f"mirror_{account[1:]}_allsignal_after_target_stake", 0.0) or 0.0)') in mirrors_branch
    assert "trader.stake_for = lambda name, proposal: mirror_stake_now(" in src
    assert ('trader.copy_after_primary_done = bool( getattr(settings, '
            '"mirror_after_primary_done", False))') in src


def test_everything_is_off_by_default():
    d = Settings.model_fields
    assert d["allsignal_after_loss_stake"].default == 0.0
    assert d["allsignal_after_loss_trades"].default == 2
    assert d["mirror_allsignal_after_loss_add"].default == 0.0
    assert d["mirror_after_primary_done"].default is False
    for n in (1, 2, 3):
        assert d[f"mirror_{n}_allsignal_after_loss_stake"].default == 0.0
        assert d[f"mirror_{n}_allsignal_after_target_stake"].default == 0.0
    assert MirroringExecutionClient.stake_for is None
    assert MirroringExecutionClient.copy_after_primary_done is False


def test_the_cap_message_says_who_still_trades(tmp_path):
    g = primary_guard(tmp_path, pnl=80.0, stop=0.08, capped=True)
    g.done_note = "▶️ The mirrors keep taking the signals to their own targets"
    g.refresh = AsyncMock()
    sent = []

    class TG:
        async def send(self, text):
            sent.append(text)
            return 1

    asyncio.run(daily_profit._monitor_one(g, TG()))
    assert len(sent) == 1 and "DAILY CAP REACHED" in sent[0]
    assert sent[0].endswith("The mirrors keep taking the signals to their own targets")


def test_the_summary_names_the_after_loss_stake():
    from btc15_signal import messages

    text = messages.shadow_summary_message(session="ny", rows=[], ny_day="2026-10-05",
                                           allsignal=[("BTC", {"n": 0, "wins": 0, "pnl": 0.0,
                                                               "fee": 0.0, "open": 0,
                                                               "missed": 0})], budget=25.0,
                                           after_loss=30.0, after_loss_trades=2)
    assert "$25 · $30 for 2 after a loss" in text


# ------------------------------------------- review fixes (wf_83445e67-f9f, 10-05)

def test_a_copy_only_counts_when_a_mirror_bought_it(tmp_path, monkeypatch):
    """Every copy misses: booked 'unfilled' under COPY_MISSED_ID, so it is not a
    taken trade and the retry/chase finds it; one fill is enough for 'copied'."""
    primary = Fake("paused", checks=False)
    primary.daily_profit_guard = primary_guard(tmp_path, pnl=80.0, stop=0.08)
    guards = mirrors(tmp_path, m1_pnl=6.0, m3_pnl=14.5)
    w, (wife, george, affoue), _ = wired(monkeypatch, tmp_path, primary, guards)
    w.labels = {"m2": "Uncle George", "m3": "Affoue"}
    george.status = affoue.status = "unfilled"

    async def both():                    # one loop: the mirrors' workers live in it
        first = await w.execute_with_take_profit(order(), 0.02, 0.77)
        affoue.status = "filled"
        second = await w.execute_with_take_profit(order(), 0.02, 0.77)
        return first, second
    first, second = asyncio.run(both())
    assert first.status == "unfilled" and first.entry_order_id == main.COPY_MISSED_ID
    assert "Uncle George unfilled ($2); Affoue unfilled ($3)" in first.note, first.note
    assert second.status == "copied" and "Affoue 4/4 ($3)" in second.note, second.note


def test_each_mirrors_own_order_check_still_decides(tmp_path, monkeypatch):
    """A copy sized above $3 reaching Affoue past its target is refused by ITS
    guard (the real block_reason), and a refused copy is not a taken trade."""
    primary = Fake("paused", checks=False)
    primary.daily_profit_guard = primary_guard(tmp_path, pnl=80.0, stop=0.08)
    guards = mirrors(tmp_path, m1_pnl=6.0, m3_pnl=14.5)
    w, (wife, george, affoue), _ = wired(monkeypatch, tmp_path, primary, guards,
                                         flags={"m1": True, "m2": False, "m3": True})
    w.stake_for = lambda name, p: 6.0              # a stale $6 decision
    result = run(w, order())
    assert affoue.counts == [], "refused before any order"
    assert result.status == "unfilled" and "m3 paused ($6)" in result.note, result.note


def test_the_order_path_books_a_missed_copy_for_the_chase(tmp_path):
    store, s = Store(str(tmp_path / "btc15.db")), settings(tmp_path)
    store.allsignal_claim(W(1), "KXBTC15M-X", "UP", 0.75, 33, 0.77, W(1) + 1, stake=25.0)
    trader = SimpleNamespace(execute_with_take_profit=AsyncMock(return_value=ExecutionResult(
        "unfilled", 0, main.COPY_MISSED_ID, None, main.COPY_NOTE + "Affoue unfilled ($3)")))
    asyncio.run(main._allsignal_order(store, s, trader, "KXBTC15M-X", "UP", 0.75, 33, 0.77,
                                      W(1)))
    row = store._dicts("SELECT * FROM allsignal_trades WHERE window_open=?", (W(1),))[0]
    assert row["status"] == "unfilled" and row["order_id"] == main.COPY_MISSED_ID
    assert main.allsignal_after_loss(store, W(0)) is False, "not a taken trade"


def test_a_missed_copy_is_chased_through_the_copy_path(tmp_path, monkeypatch):
    async def instant(_s):
        return None
    monkeypatch.setattr(main.asyncio, "sleep", instant)
    store = Store(str(tmp_path / "btc15.db"))
    store.set_setting("auto_trade_enabled", 1.0, 1)
    s = settings(tmp_path, allsignal_chase_max=0.93)
    ticker, opened = "KXBTC15M-26OCT051200-00", OPEN
    store.allsignal_claim(opened, ticker, "UP", 0.70, 35, 0.75, opened + 240_000, stake=25.0)
    store.allsignal_finish(opened, "unfilled", order_id=main.COPY_MISSED_ID,
                           note=main.COPY_NOTE + "Affoue unfilled ($3)")
    sent = []

    async def execute(order, slippage, ceiling=None):
        sent.append((order.entry_limit, ceiling))
        return ExecutionResult("copied", 0, "", None, main.COPY_NOTE + "Affoue 3/3 ($3)")

    trader = SimpleNamespace(execute_with_take_profit=execute)

    async def go():
        main.allsignal_retry_poll(
            store, s, trader, SimpleNamespace(ticker=ticker, ask=lambda side: 0.80),
            SimpleNamespace(price=100_060.0, target=100_000.0), opened, 600,
            opened + 260_000)
        await asyncio.gather(*list(main.ALLSIGNAL_TASKS))
    asyncio.run(go())
    assert sent == [(0.80, 0.85)], "re-sent at the moved price, under the 93c ceiling"
    assert store._dicts("SELECT status FROM allsignal_trades")[0]["status"] == "copied"


class TG:
    def __init__(self):
        self.sent = []

    async def send(self, text, buttons=None, reply_to=None):
        self.sent.append(text)
        return 100 + len(self.sent)

    async def edit(self, message_id, text, buttons=None):
        return True


def test_copied_windows_are_said_entry_result_and_miss(tmp_path):
    store, s = Store(str(tmp_path / "btc15.db")), settings(tmp_path)
    store.set_setting("auto_trade_enabled", 1.0, 1)
    tg = TG()
    store.allsignal_claim(W(2), "KXBTC15M-A", "UP", 0.74, 33, 0.79, W(2) + 240_000)
    store.allsignal_finish(W(2), "copied", note=main.COPY_NOTE + "Affoue 4/4 ($3)")
    assert asyncio.run(main.announce_allsignal_copy(store, s, tg, W(2), "allsignal_copied"))
    assert "COPIED TO THE MIRRORS" in tg.sent[-1] and "Affoue 4/4 ($3)" in tg.sent[-1]
    store.db.execute("INSERT INTO predictions (window_open, created_at, target, entry_price, "
                     "side, bucket, raw_probability, won) VALUES (?,?,?,?,?,?,?,?)",
                     (W(2), W(2) + 1, 1.0, 0.74, "UP", 0, 0.8, 1))
    store.allsignal_claim(W(1), "KXBTC15M-B", "DOWN", 0.70, 35, 0.75, W(1) + 240_000)
    store.allsignal_finish(W(1), "unfilled", order_id=main.COPY_MISSED_ID,
                           note=main.COPY_NOTE + "Affoue unfilled ($3)")
    store.db.commit()
    asyncio.run(main.report_allsignal_results(store, s, tg, W(1) + 900_000))
    said = "\n".join(tg.sent[1:])
    assert "COPY WON" in said and "COPY NOT FILLED" in said, tg.sent
    assert "$25" not in said, "never the primary's stake"
    n = len(tg.sent)
    asyncio.run(main.report_allsignal_results(store, s, tg, W(1) + 960_000))
    assert len(tg.sent) == n, "each said once"


def test_the_mirrors_line_keeps_an_account_that_trades_on_past_its_target(tmp_path,
                                                                           monkeypatch):
    monkeypatch.setenv("BTC15_INSTANCE", "")
    store = Store(str(tmp_path / "btc15.db"))
    s = settings(tmp_path, mirror_enabled=True, mirror_instances="btc", dry_run=False,
                 **{f"mirror_{n}_{k}": v for n in (1, 2, 3)
                    for k, v in (("api_key_id", "k"), ("private_key_path", "p"))},
                 mirror_1_label="Wife", mirror_2_label="Uncle George", mirror_3_label="Affoue")
    store.daily_profit_guards = mirrors(tmp_path, m1_pnl=6.0, m3_pnl=14.5)
    assert main.mirror_label(store, s) == "Uncle George + Affoue"


def test_the_cap_note_says_who_still_trades_when_said(tmp_path):
    store = Store(str(tmp_path / "btc15.db"))
    store.daily_profit_guards = mirrors(tmp_path, m1_pnl=6.0, m3_pnl=14.5)
    trader = SimpleNamespace(_still_trading=lambda: ["m2", "m3"],
                             labels={"m2": "Uncle George", "m3": "Affoue"})
    note = main.mirror_done_note(store, trader)
    assert note.endswith("Uncle George until its own target, Affoue at $3 past its target")
    trader._still_trading = lambda: []
    assert "No mirror is taking signals now" in main.mirror_done_note(store, trader)
    trader._still_trading = lambda: 1 / 0
    assert main.mirror_done_note(store, trader) == ""
    g = primary_guard(sub(tmp_path, "p"), pnl=80.0, stop=0.08, capped=True)
    g.done_note = lambda: "LIVE NOTE"
    g.refresh = AsyncMock()
    tg = TG()
    asyncio.run(daily_profit._monitor_one(g, tg))
    assert tg.sent[-1].endswith("LIVE NOTE")


def test_prewarm_funds_the_after_loss_stake(monkeypatch):
    main.PREWARMED.clear()
    monkeypatch.setattr(main, "allsignal_on", lambda store, settings: True)
    funded = []

    class C:
        auto_fund = True

        def __init__(self, name):
            self.name = name

        async def market_shard(self, ticker):
            return 2

        async def ensure_funds(self, ticker, count, price, reserve=False):
            funded.append((self.name, count))

    trader = SimpleNamespace(_primary=C("primary"), _mirrors=[
        SimpleNamespace(client=C("m1"), target=MirrorTarget("m1", "k", "p", allsignal_budget=3.0)),
        SimpleNamespace(client=C("m3"), target=MirrorTarget("m3", "k", "p", allsignal_budget=6.0))])
    s = SimpleNamespace(allsignal_stake=25.0, allsignal_after_loss_stake=30.0,
                        mirror_allsignal_after_loss_add=1.0,
                        mirror_3_allsignal_after_loss_stake=8.0)

    async def go():
        main.prewarm_order_path(trader, s, "KXBTC15M-26OCT051215-15", None)
        await asyncio.gather(*list(main.ALLSIGNAL_TASKS))
    asyncio.run(go())
    assert sorted(funded) == sorted([("primary", contracts_for_budget(30.0, 0.70)),
                                     ("m1", contracts_for_budget(4.0, 0.70)),
                                     ("m3", contracts_for_budget(8.0, 0.70))])
