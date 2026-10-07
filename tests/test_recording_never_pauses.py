"""A pause stops ORDERS, never RECORDING (operator, 2026-09-30).

"Even if the target is hit, the shadow system should still continue collecting
data and signal outcomes ... Every system collecting data in the shadow still
continue collecting their data." The 2,000-signal review is built from what the
recorders write while trading is paused.

The daily-target pause was already harmless (it lives inside the order call).
The audit that confirmed it (wf_067657f0-648) found other ways recording
stopped, none of them a pause. These pin the fixes.
"""

import asyncio
import importlib.util
import inspect
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from btc15_signal import main  # noqa: E402
from btc15_signal.config import Settings  # noqa: E402
from btc15_signal.daily_profit import DailyProfitGuard  # noqa: E402
from btc15_signal.reference_shadow import RECONCILE_PAGES, ReferenceShadow  # noqa: E402
from btc15_signal.store import Store  # noqa: E402

W = 1_790_769_600_000          # 2026-09-30 08:00 New York, a window open


def loop_source() -> str:
    return " ".join(inspect.getsource(main.service).split())


# ------------------------------------------------ 1. no quote, still recording

def test_a_no_quote_poll_still_records_the_ladder_and_reconciles():
    """204 of 211 hourly.db gaps over 90 s since 09-24 were no-quote polls in
    the last 1-3 minutes of a window; 35 of 143 chains lost their last snapshot."""
    loop = loop_source()
    i = loop.index("if isinstance(inputs, kalshi_signal.Unavailable):")
    j = loop.index("continue", i)
    assert "await record_shadows(hourly, reference, market, now_ms)" in loop[i:j]


def test_record_shadows_runs_every_recorder_and_tolerates_none():
    hourly = SimpleNamespace(poll=AsyncMock(), settle=AsyncMock())
    reference = SimpleNamespace(reconcile=AsyncMock(), current_features=lambda: "brti")
    asyncio.run(main.record_shadows(hourly, reference, None, 5))
    hourly.poll.assert_awaited_once_with(5, None, brti="brti")
    hourly.settle.assert_awaited_once_with(5)
    reference.reconcile.assert_awaited_once_with(5)
    asyncio.run(main.record_shadows(None, None, None, 5))       # nothing to run


# ------------------------------------ 2. a bug in trading cannot stop recording

def test_trading_runs_in_its_own_try_ahead_of_the_recorders():
    loop = loop_source()
    trade = loop.index("await primary_signal(")
    caught = loop.index('await report_cycle_error(exc, "trading"', trade)
    assert trade < caught < loop.index("await hourly.poll(", trade)
    assert caught < loop.index("await reference.poll(now_ms, contract)", trade)
    # the order path is unchanged inside it
    assert "allsignal_cushion_poll(store, settings, trader, contract, snapshot," in loop
    assert "allsignal_retry_poll(store, settings, trader, contract, snapshot," in loop


def test_the_poll_loop_survives_an_unexpected_error():
    """It exited the process: ETH lost 20 minutes of observations on 09-25."""
    loop = loop_source()
    assert ('except Exception as exc: # noqa: BLE001 - a bug must not stop recording '
            'await report_cycle_error(exc, "poll", settings, store, telegram, now_ms)') in loop


def test_one_market_cannot_stop_the_settlement_sweep_or_the_poll():
    loop = loop_source()
    i = loop.index("for row in store.pending_settlements(now_ms):")
    j = loop.index("if result:", i)
    assert "except (httpx.HTTPError, ValueError) as exc:" in loop[i:j]
    assert "continue" in loop[i:j]
    assert 'await report_cycle_error(exc, "settlement"' in loop[j:]
    assert loop.index('await report_cycle_error(exc, "settlement"') \
        < loop.index("await kalshi.active_market(now_ms)")


class FakeTelegram:
    def __init__(self, fail=False):
        self.sent, self.fail = [], fail

    async def send(self, text, buttons=None, reply_to=None):
        if self.fail:
            raise RuntimeError("telegram down")
        self.sent.append(text)
        return len(self.sent)


def _bug():
    try:
        main.closed_poll_seconds(None, 0, 0)      # AttributeError inside main.py
    except AttributeError as exc:
        return exc
    raise AssertionError("expected a bug")


def test_an_unexpected_error_is_said_at_once_and_again_hourly_while_it_recurs(capsys):
    """Said at once, from any instance, with what did not run; again every hour
    with the count while it keeps happening; logged once per 15-minute window."""
    main.CYCLE_ERRORS.clear()
    settings = Settings(_env_file=None, kalshi_series="KXETH15M",
                        telegram_alert_instruments="BTC,ETH")    # an alerting instance
    tg = FakeTelegram()
    exc = _bug()
    for now in (W, W + 10_000, W + 900_000):
        asyncio.run(main.report_cycle_error(exc, "trading", settings, None, tg, now))
    assert len(tg.sent) == 1
    assert "ETH" in tg.sent[0] and "SOFTWARE ERROR" in tg.sent[0]
    assert "trading:AttributeError:main.py:" in tg.sent[0], "it names where the bug is"
    assert "Orders did NOT run" in tg.sent[0], "it says what did not run"
    out = capsys.readouterr().out
    assert out.count("cycle error (unexpected)") == 2, "once per 15-minute window"
    assert "Traceback" in out
    asyncio.run(main.report_cycle_error(exc, "trading", settings, None, tg, W + 3_600_000))
    assert len(tg.sent) == 2 and "STILL HAPPENING" in tg.sent[1]
    assert "4 times since 08:00 ET" in tg.sent[1]


def test_the_alert_does_not_depend_on_the_store_and_a_failed_send_is_retried():
    """Review 2026-09-30: through btc15.db, a DB fault - or one failed send -
    meant no Telegram at all."""
    main.CYCLE_ERRORS.clear()
    settings = Settings(_env_file=None, kalshi_series="KXBTC15M")
    down = FakeTelegram(fail=True)
    asyncio.run(main.report_cycle_error(_bug(), "poll", settings, None, down, W))
    up = FakeTelegram()
    asyncio.run(main.report_cycle_error(_bug(), "poll", settings, None, up, W + 60_000))
    assert up.sent == [], "not retried on every poll"
    asyncio.run(main.report_cycle_error(_bug(), "poll", settings, None, up, W + 300_000))
    assert len(up.sent) == 1 and "exits and cash-outs" in up.sent[0]


def test_the_error_reporter_never_raises():
    main.CYCLE_ERRORS.clear()
    settings = Settings(_env_file=None, kalshi_series="KXBTC15M")
    asyncio.run(main.report_cycle_error(_bug(), "poll", settings, None,
                                        FakeTelegram(fail=True), W))
    asyncio.run(main.report_cycle_error(_bug(), "poll", None, None, None, W))
    asyncio.run(main.report_cycle_error(_bug(), "poll", None, None, FakeTelegram(), W + 10**8))


# --------------------------------------- 2b. a failed command is never dropped

class ReplayTelegram:
    """getUpdates semantics: returns updates from `offset`, then moves it past them."""

    def __init__(self, batch, fail_answer=False):
        self.batch, self.offset, self.sent, self.fail_answer = batch, 0, [], fail_answer

    async def updates(self):
        out = [u for u in self.batch if u["update_id"] >= self.offset]
        if out:
            self.offset = max(u["update_id"] for u in out) + 1
        return out

    async def send(self, text, buttons=None, reply_to=None):
        self.sent.append(text)
        return len(self.sent)

    async def answer_callback(self, callback_id, text):
        if self.fail_answer:
            raise RuntimeError("telegram hiccup")


def _command(update_id, text):
    return {"update_id": update_id, "message": {"text": text, "from": {"id": 42}}}


def _commands_settings():
    return Settings(_env_file=None, telegram_commands_enabled=True,
                    telegram_authorized_user_id=42)


def test_a_failed_auto_off_is_read_again_until_it_applies(tmp_path, monkeypatch):
    """Review 2026-09-30: the offset had moved past it, and the error no longer
    restarted the process (which re-read it) - the kill switch was dropped."""
    main.COMMAND_FAILURES.clear()
    fails = [sqlite3.OperationalError("database is locked")] * 2

    def auto(store, settings, command, now_ms, ready):
        if fails:
            raise fails.pop()
        return "AUTO TRADING OFF"

    monkeypatch.setattr(main, "handle_auto_command", auto)
    tg = ReplayTelegram([_command(10, "/auto off"), _command(11, "/id")])
    store = Store(str(tmp_path / "btc15.db"))
    for _ in range(2):
        asyncio.run(main.process_telegram(tg, store, None, None, _commands_settings()))
        assert tg.offset == 10 and tg.sent == [], "rewound to the failed command"
    asyncio.run(main.process_telegram(tg, store, None, None, _commands_settings()))
    assert tg.sent[0] == "AUTO TRADING OFF"
    assert "Your Telegram user id" in tg.sent[1], "and the rest of the batch runs"
    assert tg.offset == 12


def test_a_command_that_keeps_failing_is_said_to_have_failed(tmp_path, monkeypatch):
    main.COMMAND_FAILURES.clear()

    def auto(*_a):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(main, "handle_auto_command", auto)
    tg = ReplayTelegram([_command(20, "/auto off"), _command(21, "/id")])
    store = Store(str(tmp_path / "btc15.db"))
    for _ in range(3):
        asyncio.run(main.process_telegram(tg, store, None, None, _commands_settings()))
    assert len(tg.sent) == 1 and "COMMAND FAILED" in tg.sent[0] and "/auto off" in tg.sent[0]
    assert tg.offset == 21, "skipped after 3 tries; the rest of the batch comes again"
    asyncio.run(main.process_telegram(tg, store, None, None, _commands_settings()))
    assert "Your Telegram user id" in tg.sent[1]


def test_a_failed_button_press_is_never_replayed(tmp_path):
    """A replayed Execute could order twice."""
    main.COMMAND_FAILURES.clear()
    press = {"update_id": 30, "callback_query": {"id": "cb", "from": {"id": 42},
                                                 "data": "bogus", "message": {}}}
    tg = ReplayTelegram([press], fail_answer=True)
    store = Store(str(tmp_path / "btc15.db"))
    asyncio.run(main.process_telegram(tg, store, None, None, _commands_settings()))
    assert tg.offset == 31
    assert "COMMAND FAILED" in tg.sent[0] and "not repeated" in tg.sent[0]


# ----------------------------------------------- 3. the hourly recorder's word

def _hourly(tmp_path):
    from btc15_signal.hourly_shadow import HourlyShadow

    settings = Settings(_env_file=None)
    object.__setattr__(settings, "hourly_database_path", str(tmp_path / "h.db"))
    object.__setattr__(settings, "hourly_enabled", True)
    return HourlyShadow(settings)


def test_the_hourly_recorder_never_raises_on_a_database_error(tmp_path):
    """Its docstring promised "Never raises" and let sqlite errors out."""
    shadow = _hourly(tmp_path)

    async def locked(*_a, **_k):
        raise sqlite3.OperationalError("database is locked")

    shadow._client.active_chain = locked
    shadow._store.unsettled_chains = lambda *_a: (_ for _ in ()).throw(
        sqlite3.OperationalError("database is locked"))
    asyncio.run(shadow.poll(W, None))                 # must not raise
    asyncio.run(shadow.settle(W))                     # must not raise
    asyncio.run(shadow.close())


# ------------------------------------------ 4. the daily-target gate fails closed

def test_the_daily_target_gate_pauses_when_its_own_store_is_unreadable(tmp_path):
    client = SimpleNamespace(account_value=AsyncMock(return_value=100))
    g = DailyProfitGuard(tmp_path / "g.db", "primary", "You", client)

    def locked(*_a, **_k):
        raise sqlite3.OperationalError("database is locked")

    g.state = locked
    reason = asyncio.run(g.block_reason("KXBTC15M-26SEP300815-15"))
    assert "paused" in reason, "unknown figures pause the entry, and nothing raises"
    assert asyncio.run(g.block_reason("KXGOLD15M-26SEP300815-15")) == ""


# ------------------------------------------- 5. the metals wake at the reopen

def test_a_closed_venue_is_polled_again_at_the_reopen_not_ten_minutes_later():
    settings = Settings(_env_file=None, venue_closed_poll_seconds=600)
    now = W
    assert main.closed_poll_seconds(settings, now + 120_000, now) == 125
    assert main.closed_poll_seconds(settings, now + 3 * 3_600_000, now) == 600
    assert main.closed_poll_seconds(settings, now - 60_000, now) == 1.0
    loop = loop_source()
    assert "closed_poll_seconds(settings, closed_until, now_ms)" in loop
    assert "await asyncio.sleep(settings.venue_closed_poll_seconds)" not in loop


def test_a_passed_reopen_ends_the_closure_so_the_gap_counts_from_the_reopen():
    """Review 2026-09-30: Kalshi lists the reopen market ~35-55 s after the open.
    Timed from Friday, the first poll saw a 48 h gap and slept another 600 s or
    raised a false NO MARKET alert."""
    friday, reopen = W - 48 * 3_600_000, W
    main.MARKET_GAP.clear()
    main.VENUE_SCHEDULE.clear()
    main.MARKET_GAP.update(since=friday, closed_told=1)
    main.VENUE_SCHEDULE["next_open_ms"] = reopen
    assert main.end_closure_at_reopen(reopen - 1) is False, "not before the reopen"
    assert main.end_closure_at_reopen(reopen + 5_000) is True
    assert main.MARKET_GAP["since"] == reopen and "closed_told" not in main.MARKET_GAP
    assert main.VENUE_SCHEDULE == {}
    assert main.end_closure_at_reopen(reopen + 15_000) is False, "once"
    main.MARKET_GAP.clear()
    main.VENUE_SCHEDULE["next_open_ms"] = reopen
    assert main.end_closure_at_reopen(reopen + 5_000) is False, "only after a closure"
    main.VENUE_SCHEDULE.clear()
    loop = loop_source()
    assert loop.index("if end_closure_at_reopen(now_ms, settings.venue_closed_gap_s * 1000):") \
        < loop.index("closed_until = await _venue_closed_until(")


# ---------------------------------- 6. reconciliation pages back past 50 markets

def _market(i):
    return {"ticker": f"T{i:04d}", "expiration_value": "100.0", "floor_strike": 99.0,
            "close_time": "2026-09-30T12:00:00Z", "open_time": "2026-09-30T11:45:00Z",
            "result": "yes"}


def _reference(tmp_path, pages, done):
    shadow = ReferenceShadow(Settings(
        reference_database_path=str(tmp_path / "ref.db"), reference_poll_seconds=0,
        cfb_api_key=""))
    calls = []

    async def settled(limit=200, cursor=None):
        calls.append(cursor)
        page, nxt = pages[len(calls) - 1]
        return page, nxt

    shadow._kalshi.settled = settled
    shadow._store.reconciled_tickers = lambda: set(done)
    shadow._reconcile_one = AsyncMock()
    return shadow, calls


def test_after_a_long_outage_reconciliation_pages_back_to_what_it_has(tmp_path):
    """One page of 50 was the whole horizon: 12.5 h, then lost for good."""
    page1 = [_market(i) for i in range(200)]
    page2 = [_market(i) for i in range(200, 260)] + [_market(9000)]
    shadow, calls = _reference(tmp_path, [(page1, "c1"), (page2, "c2"),
                                          ([_market(9999)], "c3")], {"T9000"})
    asyncio.run(shadow._reconcile(W))
    assert calls == [None, "c1"], "stops at the page that reaches a reconciled market"
    assert shadow._reconcile_one.await_count == 260
    asyncio.run(shadow.close())


def test_normal_operation_still_reads_one_page(tmp_path):
    page = [_market(0), _market(1)] + [_market(i) for i in range(2, 200)]
    shadow, calls = _reference(tmp_path, [(page, "c1")], {f"T{i:04d}" for i in range(2, 200)})
    asyncio.run(shadow._reconcile(W))
    assert calls == [None]
    assert shadow._reconcile_one.await_count == 2
    asyncio.run(shadow.close())


def test_the_page_walk_is_bounded(tmp_path):
    pages = [([_market(p * 200 + i) for i in range(200)], f"c{p}") for p in range(10)]
    shadow, calls = _reference(tmp_path, pages, set())
    asyncio.run(shadow._reconcile(W))
    assert len(calls) == RECONCILE_PAGES
    asyncio.run(shadow.close())


# ------------------------------------- 7. the watchdog never stops for good

class _Stop(Exception):
    pass


def _watchdog(monkeypatch, runs):
    """Load scripts/watchdog.py with a fake clock, fake service runs and a
    Telegram that records. `runs` is a list of (seconds, exit code)."""
    spec = importlib.util.spec_from_file_location("watchdog_t", ROOT / "scripts" / "watchdog.py")
    wd = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(wd)
    clock, sent, sleeps, held = [1_000_000.0], [], [], [False]
    it = iter(runs)

    def run(*_a, **_k):
        try:
            seconds, code = next(it)
        except StopIteration:
            raise _Stop from None
        clock[0] += seconds
        return SimpleNamespace(returncode=code)

    def sleep(seconds):
        sleeps.append(seconds)
        clock[0] += seconds

    def notify(text):
        sent.append(text)
        return delivered.pop(0) if delivered else True

    delivered: list = []
    import tempfile

    monkeypatch.setattr(wd, "LOG", Path(tempfile.mkdtemp()) / "watchdog.log")
    monkeypatch.setattr(wd, "lock_held", lambda: held[0])
    wd._held = held
    monkeypatch.setattr(wd.subprocess, "run", run)
    monkeypatch.setattr(wd.time, "time", lambda: clock[0])
    monkeypatch.setattr(wd.time, "sleep", sleep)
    monkeypatch.setattr(wd, "notify", notify)
    wd._delivered = delivered
    return wd, sent, sleeps


def test_the_watchdog_backs_off_instead_of_giving_up(monkeypatch):
    """A stopped watchdog stopped every shadow recorder on the instance."""
    wd, sent, sleeps = _watchdog(monkeypatch, [(3, 1)] * 12)
    with pytest.raises(_Stop):
        wd.main()                                   # it only ends when runs do
    keeps = [t for t in sent if "KEEPS FAILING" in t]
    assert len(keeps) == 1, "said once, not on every attempt"
    assert sleeps.count(wd.BACKOFF_SECONDS) == 12 - wd.MAX_RESTARTS_PER_HOUR
    assert sum("RESTARTED" in t for t in sent) == wd.MAX_RESTARTS_PER_HOUR


def test_a_hand_started_service_ends_the_backoff_so_a_new_failure_is_announced(monkeypatch):
    """Review 2026-09-30: after a service started by hand, later failures backed
    off silently with no message at all."""
    runs = [(3, 1)] * 7 + [(1, 0)] * 30 + [(3, 1)]
    wd, sent, sleeps = _watchdog(monkeypatch, runs)
    with pytest.raises(_Stop):
        wd.main()
    assert sleeps[-1] == 5 and "RESTARTED" in sent[-1]


def test_a_healthy_run_ends_the_backoff(monkeypatch):
    runs = [(3, 1)] * 7 + [(3_600, 1)] + [(3, 1)]
    wd, sent, sleeps = _watchdog(monkeypatch, runs)
    with pytest.raises(_Stop):
        wd.main()
    assert sleeps[-1] == 5, "after a long healthy run, a death is an ordinary restart"
    assert sent[-1].startswith("♻")


def test_an_undelivered_keeps_failing_is_sent_again(monkeypatch):
    """At logon the network may not be up: one lost message left it silent."""
    wd, sent, sleeps = _watchdog(monkeypatch, [(3, 1)] * 10)
    wd._delivered.extend([True] * 6 + [False, False])     # KEEPS FAILING lost twice
    with pytest.raises(_Stop):
        wd.main()
    assert sum("KEEPS FAILING" in t for t in sent) == 3


def test_a_failed_launch_does_not_end_the_watchdog(monkeypatch):
    wd, sent, sleeps = _watchdog(monkeypatch, [])
    calls = []

    def run(*_a, **_k):
        calls.append(1)
        if len(calls) < 3:
            raise OSError("CreateProcess failed")
        raise _Stop

    monkeypatch.setattr(wd.subprocess, "run", run)
    with pytest.raises(_Stop):
        wd.main()
    assert len(calls) == 3 and sleeps[:2] == [wd.CHECK_SECONDS] * 2


def test_failure_texts_never_carry_the_bot_token():
    tg = SimpleNamespace(token="123:SECRET")
    exc = "Client error for url 'https://api.telegram.org/bot123:SECRET/sendMessage'"
    assert "SECRET" not in main._redacted(tg, exc)
    assert main._redacted(SimpleNamespace(), "x") == "x"


def test_shadow_instances_repeat_an_error_once_a_day_not_hourly():
    main.CYCLE_ERRORS.clear()
    shadow = Settings(_env_file=None, kalshi_series="KXSOL15M", telegram_alert_instruments="BTC")
    tg = FakeTelegram()
    exc = _bug()
    for hour in range(0, 6):
        asyncio.run(main.report_cycle_error(exc, "trading", shadow, None, tg,
                                            W + hour * 3_600_000 - 1))
    assert len(tg.sent) == 1, "a shadow instance does not remind hourly"
    live = Settings(_env_file=None, kalshi_series="KXBTC15M", telegram_alert_instruments="BTC")
    tg2 = FakeTelegram()
    main.CYCLE_ERRORS.clear()
    for hour in range(0, 3):
        asyncio.run(main.report_cycle_error(exc, "trading", live, None, tg2,
                                            W + hour * 3_600_000))
    assert len(tg2.sent) == 3, "the trading instance does"


def test_an_error_quiet_for_an_hour_counts_as_new():
    main.CYCLE_ERRORS.clear()
    settings = Settings(_env_file=None, kalshi_series="KXBTC15M")
    tg = FakeTelegram()
    asyncio.run(main.report_cycle_error(_bug(), "poll", settings, None, tg, W))
    asyncio.run(main.report_cycle_error(_bug(), "poll", settings, None, tg, W + 5 * 3_600_000))
    assert len(tg.sent) == 2 and "STILL HAPPENING" not in tg.sent[1]
    assert "times since" not in tg.sent[1]


def test_a_short_phantom_closure_does_not_restart_an_outage():
    main.MARKET_GAP.clear()
    main.VENUE_SCHEDULE.clear()
    main.MARKET_GAP.update(since=W - 600_000, closed_told=1)
    main.VENUE_SCHEDULE["next_open_ms"] = W
    assert main.end_closure_at_reopen(W + 5_000, 1_800_000) is False
    assert main.MARKET_GAP["since"] == W - 600_000
    main.MARKET_GAP.clear()
    main.VENUE_SCHEDULE.clear()


def test_a_failed_check_while_the_service_runs_is_not_a_restart(monkeypatch):
    """2026-09-30 10:50: 'SERVICE RESTARTED - died on startup after 0 min'
    while every service ran on. The check failed; nothing restarted."""
    wd, sent, sleeps = _watchdog(monkeypatch, [(1, 1)] * 12)
    wd._held[0] = True
    with pytest.raises(_Stop):
        wd.main()
    assert not any("RESTARTED" in t for t in sent)
    assert sum("CHECK FAILING" in t for t in sent) == 1, "said once, at 10 in a row"
    assert set(sleeps) == {wd.CHECK_SECONDS}
    assert "not a restart" in wd.LOG.read_text(encoding="utf-8")


def test_a_real_restart_names_the_instance_and_keeps_its_output(monkeypatch):
    wd, sent, sleeps = _watchdog(monkeypatch, [(3, 1)])
    with pytest.raises(_Stop):
        wd.main()
    assert f"SERVICE RESTARTED \u00b7 {wd.NAME}" in sent[0]
    assert "died on startup" in wd.LOG.read_text(encoding="utf-8")
