"""Mirror targets scale with the account (operator, 2026-10-01: "keep her at $3,
just design a scale mechanic that auto adjusts the % based on account growth").
A mirror's target = its rate x the opening, capped at N wins at its own stake."""

import asyncio
import inspect
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


def mirror(tmp_path, stake, wins=7.0, rate=0.15, name="m1"):
    g = DailyProfitGuard(tmp_path / f"{name}.db", name, name, SimpleNamespace(), rate)
    g.entry_budget, g.max_target_wins, g.error = stake, wins, ""
    return g


def test_the_cap_holds_the_target_as_the_account_grows(tmp_path):
    wife = mirror(tmp_path, 3.0)
    assert wife.day_target(30.40) == pytest.approx(4.56), "below the cap: 15% as before"
    assert wife.day_target(60.0) == pytest.approx(7.0), "7 wins x $1.00 at $3"
    george = mirror(tmp_path, 2.0, name="m2")
    assert george.day_target(23.16) == pytest.approx(3.474)
    assert george.day_target(40.0) == pytest.approx(3.5), "7 wins x $0.50 at $2"


def test_off_and_the_primary_keep_the_plain_rate(tmp_path):
    assert mirror(tmp_path, 3.0, wins=0).day_target(60.0) == pytest.approx(9.0)
    primary = DailyProfitGuard(tmp_path / "p.db", "primary", "Primary", SimpleNamespace(), 0.08)
    primary.entry_budget = 6.0
    assert primary.day_target(500.0) == pytest.approx(40.0), "the primary has no cap"


def test_the_opening_capture_stores_the_capped_target(tmp_path, monkeypatch):
    midnight = int(time.time() * 1000) // 86_400_000 * 86_400_000
    g = mirror(tmp_path, 3.0)
    g.client = SimpleNamespace(account_value=AsyncMock(return_value=60.0))
    g.pages = AsyncMock(return_value=[])
    monkeypatch.setattr("btc15_signal.daily_profit.ny_day_start_ms", lambda now: now - 60_000)
    asyncio.run(g.refresh(force=True))
    assert g.state()["target"] == pytest.approx(7.0)


def test_the_table_and_notice_show_the_effective_percent(tmp_path):
    g = mirror(tmp_path, 3.0)
    now = int(time.time() * 1000)
    with g.connect() as db:
        db.execute("INSERT INTO profit_days (account,day,label,opening,captured_ms,start_ms,basis,"
                   "target,pnl,peak,updated_ms) VALUES ('m1',?,'m1',60,?,?,'day opening',7,1,1,?)",
                   (ny_day(now), now, now, now))
    assert "m1 11.7%" in daily_profit.summary([g]) or "11.7% daily target" in daily_profit.summary([g])
    g.refresh = AsyncMock()
    sent = []

    class TG:
        async def send(self, text):
            sent.append(text)
            return 1

    asyncio.run(daily_profit._monitor_one(g, TG()))
    assert "(11.7%, net of fees; capped at 7 wins at $3)" in sent[0]


def test_only_the_mirrors_get_the_cap_in_service():
    src = " ".join(inspect.getsource(main.service).split())
    assert "else: # mirrors: the target scales with growth guard.max_target_wins = float(" in src
    assert Settings.model_fields["mirror_target_max_wins"].default == 7.0
