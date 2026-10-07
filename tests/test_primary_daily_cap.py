"""THE PRIMARY'S DAILY CAP (operator, 2026-10-02: "I authorize implement the 20% daily
stop on the primary"; FINDINGS 135). $6 below the 8% target, $3 at or above it - and
once the day's realised P&L reaches 20% of the opening, the primary is done until
00:00 New York: latched, every entry refused, exits and recording untouched.
"""

import asyncio
import inspect
import sqlite3
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from btc15_signal import daily_profit, main  # noqa: E402
from btc15_signal.capital import ny_day  # noqa: E402
from btc15_signal.config import Settings  # noqa: E402
from btc15_signal.daily_profit import DailyProfitGuard  # noqa: E402
from btc15_signal.execution import KalshiExecutionClient  # noqa: E402

TICKER = "KXBTC15M-TEST"          # no time in it: counted in the day by its event time


def primary(tmp_path, monkeypatch, events, opening=100.0, stop=0.20):
    g = DailyProfitGuard(tmp_path / "p.db", "primary", "Primary", SimpleNamespace(), 0.08)
    g.entry_budget, g.after_target_stake, g.stop_rate = 6.0, 3.0, stop
    now = int(time.time() * 1000)
    with g.connect() as db:
        db.execute("INSERT INTO profit_days (account,day,label,opening,captured_ms,start_ms,basis,"
                   "target,updated_ms) VALUES ('primary',?,'Primary',?,?,?,'day opening',?,?)",
                   (ny_day(now), opening, now - 3_600_000, now - 3_600_000, 0.08 * opening, now))
    g.pages = AsyncMock(return_value=[])
    box = {"events": events}
    monkeypatch.setattr(daily_profit, "realised_events", lambda f, s: box["events"])
    return g, box, now


def ev(now, *amounts):
    return [(now - 60_000 + i, a, TICKER) for i, a in enumerate(amounts)]


def test_the_ticker_used_here_has_no_time():
    assert KalshiExecutionClient.market_open_ms(TICKER) is None


def test_the_cap_latches_and_refuses_every_entry(tmp_path, monkeypatch):
    g, box, now = primary(tmp_path, monkeypatch, [])
    box["events"] = ev(now, 9.0, 6.0, 5.5)                      # +20.50 >= 20.00
    asyncio.run(g.refresh(force=True))
    st = g.state()
    assert st["capped_ms"] and st["paused_ms"], "the 8% target and the 20% cap both latched"
    reason = asyncio.run(g.block_reason("KXBTC15M-26OCT021015-15", "allsignal", 4, 0.74))
    assert reason == "BTC 20% daily profit target cap reached; done until midnight New York"
    assert "profit target" in reason, "the order path's miss notice stays quiet"
    for strategy in ("", "allsignal"):
        assert asyncio.run(g.block_reason("KXBTC15M-26OCT021015-15", strategy, 1, 0.74))
    box["events"] = ev(now, 9.0, 6.0, 5.5, -8.0)               # a loss after the cap
    asyncio.run(g.refresh(force=True))
    assert g.state()["pnl"] == pytest.approx(12.5)
    assert asyncio.run(g.block_reason("KXBTC15M-26OCT021015-15", "allsignal", 4, 0.74)), \
        "done for the day: a later loss does not reopen it"


def test_between_the_target_and_the_cap_the_day_goes_on_at_3(tmp_path, monkeypatch):
    g, box, now = primary(tmp_path, monkeypatch, [])
    box["events"] = ev(now, 9.0, 6.0)                            # +15: past 8%, under 20%
    asyncio.run(g.refresh(force=True))
    assert g.state()["capped_ms"] is None
    assert asyncio.run(g.block_reason("KXBTC15M-26OCT021015-15", "allsignal", 4, 0.74)) == "", \
        "4 contracts at 0.74 = the $3 stake"
    assert "sized at the base stake" in asyncio.run(
        g.block_reason("KXBTC15M-26OCT021015-15", "allsignal", 8, 0.74)), "$6 is refused"


def test_a_cap_crossed_since_the_last_refresh_still_refuses(tmp_path, monkeypatch):
    g, box, now = primary(tmp_path, monkeypatch, [])
    with g.connect() as db:                                      # fresh figures, no latch yet
        db.execute("UPDATE profit_days SET pnl=21.0, updated_ms=?", (int(time.time() * 1000),))
    g.error = ""
    assert asyncio.run(g.block_reason("KXBTC15M-26OCT021015-15", "allsignal", 4, 0.74))


def test_no_cap_when_off(tmp_path, monkeypatch):
    g, box, now = primary(tmp_path, monkeypatch, [], stop=0.0)
    box["events"] = ev(now, 30.0, 20.0)                          # +50%
    asyncio.run(g.refresh(force=True))
    assert g.state()["capped_ms"] is None
    assert asyncio.run(g.block_reason("KXBTC15M-26OCT021015-15", "allsignal", 4, 0.74)) == ""


def test_the_cap_is_said_once_and_shown(tmp_path, monkeypatch):
    g, box, now = primary(tmp_path, monkeypatch, [])
    box["events"] = ev(now, 9.0, 6.0, 5.5)
    asyncio.run(g.refresh(force=True))
    g.refresh = AsyncMock()
    sent = []

    class TG:
        async def send(self, text):
            sent.append(text)
            return 1

    for _ in range(3):
        asyncio.run(daily_profit._monitor_one(g, TG()))
    capped = [t for t in sent if "DAILY CAP REACHED" in t]
    assert len(capped) == 1, sent
    assert "+20.50</b> of the $20.00 cap (20% of $100.00)" in capped[0]
    assert "Done for the day: no new BTC entries until 00:00 ET" in capped[0]
    assert "CAPPED" in daily_profit.summary([g])
    assert "daily cap reached ($+20.50 of $20.00, 20%)" in daily_profit.summary([g])
    assert "\U0001f3c1" in daily_profit.compact([g])
    assert "DONE FOR THE DAY" in g.line()


def test_the_midnight_notice_names_the_cap(tmp_path, monkeypatch):
    g, box, now = primary(tmp_path, monkeypatch, [], opening=119.67)
    g.refresh, g.error = AsyncMock(), ""          # as after the first broker read
    sent = []

    class TG:
        async def send(self, text):
            sent.append(text)
            return 1

    asyncio.run(daily_profit._monitor_one(g, TG()))
    assert "DAILY TARGET ACTIVE" in sent[0]
    assert "Daily cap <b>$23.93</b> (20%) · done for the day once reached" in sent[0]


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
    for _ in range(2):
        g = DailyProfitGuard(path, "primary", "Primary", SimpleNamespace(), 0.08)
    cols = {r[1] for r in g.connect().execute("PRAGMA table_info(profit_days)")}
    assert {"stake", "capped_ms"} <= cols


def test_only_the_primary_gets_the_cap_in_service():
    src = " ".join(inspect.getsource(main.service).split())
    primary_branch = src.split('if account == "primary":', 1)[1].split(
        "else: # mirrors: the target scales with growth", 1)[0]
    assert ('guard.stop_rate = float(getattr(settings, "daily_profit_stop_rate", 0.0) or 0.0)'
            in primary_branch)
    mirrors_branch = src.split("else: # mirrors: the target scales with growth", 1)[1]
    assert "guard.stop_rate" not in mirrors_branch.split("client.daily_profit_guard", 1)[0]
    assert Settings.model_fields["daily_profit_stop_rate"].default == 0.0
    assert DailyProfitGuard.stop_rate == 0.0


def test_an_outage_on_a_capped_day_never_says_allowed_again(tmp_path, monkeypatch):
    """Review 2026-10-02: the outage pair said "New BTC entries allowed again" while
    the cap still refused every entry."""
    g, box, now = primary(tmp_path, monkeypatch, [])
    box["events"] = ev(now, 9.0, 6.0, 5.5)
    asyncio.run(g.refresh(force=True))
    sent = []

    class TG:
        async def send(self, text):
            sent.append(text)
            return 1

    g.refresh = AsyncMock()
    asyncio.run(daily_profit._monitor_one(g, TG()))           # the cap notice
    g.error = "ConnectError"
    for _ in range(2):
        asyncio.run(daily_profit._monitor_one(g, TG()))
    g.error = ""
    asyncio.run(daily_profit._monitor_one(g, TG()))
    down = next(t for t in sent if "TARGET CHECK UNAVAILABLE" in t)
    back = next(t for t in sent if "TARGET CHECK RESTORED" in t)
    assert "already done at its daily cap" in down and "until it recovers" not in down
    assert "Still done for the day" in back and "allowed again" not in back
    assert asyncio.run(g.block_reason("KXBTC15M-26OCT021015-15", "allsignal", 4, 0.74))


def test_switched_off_mid_day_the_display_follows_the_gate(tmp_path, monkeypatch):
    """Review 2026-10-02: with DAILY_PROFIT_STOP_RATE=0 after a latch, entries went
    through but Telegram still said CAPPED with a $0.00 cap."""
    g, box, now = primary(tmp_path, monkeypatch, [])
    box["events"] = ev(now, 9.0, 6.0, 5.5)
    asyncio.run(g.refresh(force=True))
    g.stop_rate = 0.0                                        # the cap switched off
    assert asyncio.run(g.block_reason("KXBTC15M-26OCT021015-15", "allsignal", 4, 0.74)) == ""
    assert "CAPPED" not in daily_profit.summary([g])
    assert "🏁" not in daily_profit.compact([g])
    assert "DONE FOR THE DAY" not in g.line()
    assert daily_profit.capped(g, g.state()) is False
