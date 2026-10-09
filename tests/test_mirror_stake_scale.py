"""The mirrors' STAKE scales with the balance at each midnight opening (operator,
2026-10-01: "I thought you had the auto scale for the mirrored account as the
account balance changes every day at midnight but a nice safe scale. Only my
primary is controlled manually on aggressive").

2% of the opening in whole dollars, never below the operator's own stake for that
mirror, never above $6; the 7-win target cap follows the same stake. FINDINGS 129.

THE REAL MirrorTarget AND _Mirror, NOT STAND-INS: the first version passed every
test on a SimpleNamespace while the real, frozen MirrorTarget refused the new
stake and every copy stayed at the floor (review 2026-10-01).
"""

import asyncio
import inspect
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from btc15_signal import daily_profit, main  # noqa: E402
from btc15_signal.capital import ny_day  # noqa: E402
from btc15_signal.config import Settings  # noqa: E402
from btc15_signal.daily_profit import DailyProfitGuard, fetch_since  # noqa: E402
from btc15_signal.mirror import MirrorTarget, _Mirror  # noqa: E402
from btc15_signal.store import TradeProposal  # noqa: E402
from btc15_signal.validation import contracts_for_budget  # noqa: E402

NY = ZoneInfo("America/New_York")


def guard(tmp_path, floor, rate=0.02, top=6.0, name="m3"):
    g = DailyProfitGuard(tmp_path / f"{name}.db", name, name, SimpleNamespace(), 0.15)
    g.entry_budget, g.max_target_wins, g.error = floor, 7.0, ""
    g.stake_rate, g.stake_max = rate, top
    return g


def live_mirror(budget=2.0):
    """A real _Mirror around a real (frozen) MirrorTarget; the broker call records
    the size it was sent."""
    m = object.__new__(_Mirror)
    m.target = MirrorTarget(name="m3", api_key_id="k", private_key_path="p",
                            allsignal_budget=budget)
    m.sent = []

    async def execute(proposal, slippage, ceiling):
        m.sent.append(proposal.count)
        return SimpleNamespace(status="filled", filled_count=proposal.count, note="")

    m.client = SimpleNamespace(execute_with_take_profit=execute, last_funding_note="")
    m.held, m.order_ids, m._log = {}, {}, (lambda *a: None)
    return m


def copy(m, ask=0.74):
    p = TradeProposal(id="allsignal:1", strategy="allsignal", window_open=1,
                      ticker="KXBTC15M-26OCT020015-15", side="UP", entry_limit=ask,
                      take_profit=0.0, count=8, expires_at=1, close_ms=1, status="claimed")
    asyncio.run(m._apply("entry", {"proposal": p, "slippage": 0.0, "ceiling": 0.99,
                                   "filled_count": 8}))
    return m.sent[-1]


def row(g, opening, stake=None):
    now = int(time.time() * 1000)
    with g.connect() as db:
        db.execute("INSERT INTO profit_days (account,day,label,opening,captured_ms,start_ms,basis,"
                   "target,stake,pnl,peak,updated_ms) VALUES (?,?,?,?,?,?,'day opening',?,?,0,0,?)",
                   (g.account, ny_day(now), g.label, opening, now, now,
                    g.day_target(opening), stake, now))


def test_the_stake_follows_the_opening_in_whole_dollars(tmp_path):
    affoue = guard(tmp_path, 2.0)
    for opening, stake in ((8.10, 2), (100.0, 2), (149.99, 2), (150.0, 3), (199.99, 3),
                           (200.0, 4), (300.0, 6), (5000.0, 6)):
        assert affoue.day_stake(opening) == stake, opening
    wife = guard(tmp_path, 3.0, name="m1")
    assert wife.day_stake(35.0) == 3, "the operator's $3 stays the floor"
    assert wife.day_stake(199.99) == 3 and wife.day_stake(200.0) == 4


def test_off_mis_set_and_the_primary_keep_their_own_stake(tmp_path):
    assert guard(tmp_path, 2.0, rate=0.0).day_stake(5000.0) == 2.0
    assert guard(tmp_path, 2.0, rate=2.0).day_stake(5000.0) == 2.0, "'2' for 2%: no scaling"
    assert guard(tmp_path, 2.0, top=0.0).day_stake(5000.0) == 2.0, "no ceiling: no scaling"
    primary = DailyProfitGuard(tmp_path / "p.db", "primary", "Primary", SimpleNamespace(), 0.08)
    primary.entry_budget = 6.0
    assert primary.day_stake(5000.0) == 6.0, "the primary is manual"
    assert primary.day_target(500.0) == pytest.approx(40.0)


def test_the_target_cap_follows_the_days_stake(tmp_path):
    g = guard(tmp_path, 2.0)
    assert g.day_target(100.0) == pytest.approx(3.5), "$2: 7 wins x $0.50"
    assert g.day_target(150.0) == pytest.approx(7.0), "$3: 7 wins x $1.00"
    assert g.day_target(23.16) == pytest.approx(3.474), "below the cap: 15%"


def test_the_midnight_capture_reaches_the_real_mirrors_orders(tmp_path, monkeypatch):
    g = guard(tmp_path, 2.0)
    g.client = SimpleNamespace(account_value=AsyncMock(return_value=200.0))
    g.pages = AsyncMock(return_value=[])
    m = live_mirror(2.0)
    assert copy(m) == contracts_for_budget(2.0, 0.74), "before: the floor"
    g.stake_target = m
    monkeypatch.setattr("btc15_signal.daily_profit.ny_day_start_ms", lambda now: now - 60_000)
    asyncio.run(g.refresh(force=True))
    state = g.state()
    assert state["stake"] == 4.0 and state["target"] == pytest.approx(8.75), "7 x 5 x 0.25"
    assert isinstance(m.target, MirrorTarget) and m.target.allsignal_budget == 4.0
    assert copy(m) == contracts_for_budget(4.0, 0.74), "the copy is sent at $4"


def test_a_restart_applies_the_stake_before_any_broker_read(tmp_path):
    g = guard(tmp_path, 2.0)
    row(g, 250.0, stake=5.0)
    g.stake_target = m = live_mirror(2.0)
    g.last_attempt = int(time.time() * 1000)           # the throttled path: no broker read
    g.pages = AsyncMock(side_effect=AssertionError("no broker read on this path"))
    asyncio.run(g.refresh())
    assert m.target.allsignal_budget == 5.0
    assert copy(m) == contracts_for_budget(5.0, 0.74)


def test_the_operators_change_beats_the_stored_stake(tmp_path):
    g = guard(tmp_path, 2.0)
    row(g, 250.0, stake=5.0)
    g.stake_target = m = live_mirror(2.0)
    g.sync_stake(g.state())
    assert m.target.allsignal_budget == 5.0
    g.stake_rate = 0.0                                   # the scale switched off
    g.sync_stake(g.state())
    assert m.target.allsignal_budget == 2.0
    g.entry_budget = 3.0                                 # the operator's stake raised
    g.sync_stake(g.state())
    assert m.target.allsignal_budget == 3.0


def test_the_target_follows_the_stake_until_the_day_pauses(tmp_path):
    """A mid-day change + restart moved the stake but not the stored target: from
    $6 to $2 a $14 target was 28 wins - no pause that day (review 2026-10-01)."""
    g = guard(tmp_path, 2.0)
    row(g, 300.0, stake=6.0)
    assert g.state()["target"] == pytest.approx(14.0), "7 wins x 8 contracts x 0.25"
    with g.connect() as db:
        db.execute("UPDATE profit_days SET notified='active'")
    g.pages = AsyncMock(return_value=[])
    g.stake_rate = 0.0                                   # the scale switched off
    asyncio.run(g.refresh(force=True))
    st = g.state()
    assert st["stake"] == 2.0 and st["target"] == pytest.approx(3.5), "7 wins at $2"
    assert st["notified"] == "", "the new figures are announced again"
    with g.connect() as db:                              # once paused, the day is latched
        db.execute("UPDATE profit_days SET paused_ms=1, notified='paused'")
    g.entry_budget = 6.0
    asyncio.run(g.refresh(force=True))
    st = g.state()
    assert st["target"] == pytest.approx(3.5) and st["notified"] == "paused"


def test_the_primarys_record_is_never_rederived(tmp_path):
    p = DailyProfitGuard(tmp_path / "p.db", "primary", "Primary", SimpleNamespace(), 0.08)
    p.entry_budget, p.error = 6.0, ""
    row(p, 100.0)
    p.rate = 0.10                                        # a changed rate mid-day
    p.pages = AsyncMock(return_value=[])
    asyncio.run(p.refresh(force=True))
    assert p.state()["target"] == pytest.approx(8.0)


def test_an_old_table_gains_the_column(tmp_path):
    path = tmp_path / "old.db"
    with sqlite3.connect(path) as db:
        db.execute("""CREATE TABLE profit_days (
            account TEXT NOT NULL, day TEXT NOT NULL, label TEXT NOT NULL,
            opening REAL NOT NULL, captured_ms INTEGER NOT NULL,
            start_ms INTEGER NOT NULL, basis TEXT NOT NULL, target REAL NOT NULL,
            pnl REAL NOT NULL DEFAULT 0, peak REAL NOT NULL DEFAULT 0,
            paused_ms INTEGER, updated_ms INTEGER NOT NULL DEFAULT 0,
            notified TEXT NOT NULL DEFAULT '', PRIMARY KEY(account,day))""")
    for _ in range(2):          # twice: the second sees the column and leaves it
        g = DailyProfitGuard(path, "m3", "m3", SimpleNamespace(), 0.15)
    cols = {r[1] for r in g.connect().execute("PRAGMA table_info(profit_days)")}
    assert "stake" in cols


def test_the_midnight_notice_says_the_stake_and_the_primarys_does_not(tmp_path):
    sent = []

    class TG:
        async def send(self, text):
            sent.append(text)
            return 1

    g = guard(tmp_path, 2.0)
    row(g, 150.0, stake=3.0)
    g.refresh = AsyncMock()
    asyncio.run(daily_profit._monitor_one(g, TG()))
    assert "capped at 7 wins at $3)" in sent[0]
    assert "<b>$3</b> per signal today · 2% of the opening, never below $2 or above $6" in sent[0]
    p = DailyProfitGuard(tmp_path / "p.db", "primary", "Primary", SimpleNamespace(), 0.08)
    p.entry_budget, p.error = 6.0, ""
    row(p, 120.0)
    p.refresh = AsyncMock()
    asyncio.run(daily_profit._monitor_one(p, TG()))
    assert "DAILY TARGET ACTIVE" in sent[1] and "per signal today" not in sent[1]


def test_the_broker_read_covers_the_whole_previous_day_across_dst():
    def ms(*a):
        return int(datetime(*a, tzinfo=NY).timestamp() * 1000)

    assert fetch_since(ms(2026, 10, 1)) == ms(2026, 9, 30)
    # 11-01 is 25 hours (fall back): a flat 24 h started at its 01:00.
    assert fetch_since(ms(2026, 11, 2)) == ms(2026, 11, 1) == ms(2026, 11, 2) - 25 * 3_600_000
    # 2027-03-14 is 23 hours: 24 h already covers it.
    assert fetch_since(ms(2027, 3, 15)) == ms(2027, 3, 15) - 86_400_000 <= ms(2027, 3, 14)
    src = " ".join(inspect.getsource(DailyProfitGuard.refresh).split())
    assert "since = fetch_since(midnight)" in src and "86_400_000" not in src


def test_legacy_whole_dollar_scale_stays_mirror_only_in_service():
    src = " ".join(inspect.getsource(main.service).split())
    assert ("guard.stake_target = next((m for m in getattr(trader, \"_mirrors\", []) "
            "if m.target.name == account), None)") in src
    head, _ = src.split("else: # mirrors: the target scales with growth", 1)
    assert "guard.stake_rate" not in head.split("if account == \"primary\":")[-1]
    assert Settings.model_fields["mirror_stake_scale_rate"].default == 0.0
    assert Settings.model_fields["mirror_stake_scale_max"].default == 6.0
