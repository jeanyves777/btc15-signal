"""ETH runs as a second INSTANCE, and BTC is untouched by it.

WHY A SECOND PROCESS RATHER THAN A SECOND SERIES IN ONE LOOP. Every
window-keyed query in `store.py` assumes one series - 153 of them - and BTC
and ETH windows close at the same instants, so they share `window_open`.
`predictions` and `strategy_alerts` carry no ticker column at all. Two series
in one database would cross-attribute positions to the wrong instrument,
share the per-window alert throttle, and join the wrong `predictions` row
into the daily loss reconstruction. Two processes share nothing.

The most important test in this file is the first one: with no instance set,
every path is exactly what it was, because the live BTC service must not move
when ETH is added beside it.
"""

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

ROOT = Path(__file__).resolve().parents[1]


# ------------------------------------------------- BTC must not move

def test_no_instance_keeps_the_original_runtime_paths():
    """The live service is running out of `runtime/`. If this changes, a
    restart silently starts a SECOND service against a lock nobody holds."""
    source = (ROOT / "scripts" / "run_service.py").read_text(encoding="utf-8")
    assert 'runtime = project / ("runtime" if not instance' in source
    assert 'os.environ.get("BTC15_INSTANCE")' in source


def test_the_btc_strategy_file_is_unchanged():
    rule = json.loads((ROOT / "strategy_kalshi.json").read_text())
    assert rule["min_brti_normalized_distance"] == 10.0
    assert rule["min_ask"] == 0.70
    assert rule["max_ask"] == 0.93


# ------------------------------------------------- the ETH rule

def test_the_eth_rule_exists_and_loads():
    from btc15_signal.kalshi_brti import KalshiBRTIRule

    rule = KalshiBRTIRule.load(str(ROOT / "strategy_kalshi_eth.json"))
    assert rule.enabled is True
    assert rule.min_ask == 0.70
    assert rule.max_ask == 0.93


def test_the_eth_floor_is_measured_not_inherited():
    """10x is a statement about BTC's volatility distribution. On ETH it
    admits 1.8% of markets against BTC's 4.0% - a rarer tail, not the same
    setup. 8x was swept on ETH's own 6,395 settled markets."""
    from btc15_signal.kalshi_brti import KalshiBRTIRule

    eth = KalshiBRTIRule.load(str(ROOT / "strategy_kalshi_eth.json"))
    btc = KalshiBRTIRule.load(str(ROOT / "strategy_kalshi.json"))
    assert eth.min_brti_normalized_distance == 8.0
    assert btc.min_brti_normalized_distance == 10.0
    assert eth.min_brti_normalized_distance != btc.min_brti_normalized_distance


def test_the_reversal_gate_is_carried_over_deliberately():
    """It is UNVALIDATED on ETH - median retrace was 0.000 at minute
    granularity, so the backtest could not see it. Kept because it only ever
    refuses: the risk is a missed trade, not a bad one. Pinned so that
    carrying it stays a decision rather than an accident."""
    from btc15_signal.kalshi_brti import KalshiBRTIRule

    eth = KalshiBRTIRule.load(str(ROOT / "strategy_kalshi_eth.json"))
    assert eth.max_brti_retrace == 0.6


def test_the_entry_window_matches_the_backtest():
    from btc15_signal.kalshi_brti import KalshiBRTIRule

    eth = KalshiBRTIRule.load(str(ROOT / "strategy_kalshi_eth.json"))
    assert eth.entry_from_seconds == 660
    assert eth.entry_to_seconds == 360


# ------------------------------------------------- isolation

def test_every_per_instance_value_is_a_setting():
    """The isolation depends on these being overridable from the environment.
    If any became a constant, the two instances would share it."""
    from btc15_signal.config import Settings

    s = Settings()
    for name in ("kalshi_series", "database_path", "kalshi_strategy_path",
                 "reference_database_path", "hourly_database_path",
                 "auto_daily_loss_limit"):
        assert hasattr(s, name), name


def test_eth_artefacts_are_gitignored():
    """`.env.eth` carries the same credentials as `.env`. Three .env backups
    with live keys reached a public repo on 2026-09-24."""
    ignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
    for pattern in (".env.eth", "runtime-*/", "eth15.db*"):
        assert pattern in ignore, pattern


def test_the_exposure_change_is_documented_accurately():
    """The real exposure change is CONCURRENT POSITIONS, not the loss floor.

    The first version of this doc said two instances meant two $20 floors and
    therefore $40. That was wrong: `auto_state` takes the daily realised
    figure as the more negative of a local reconstruction and
    `exchange_record`, and `exchange_record` reads `settlements` - the whole
    Kalshi account. Both instances see the SAME number, verified live with
    both databases reporting -5.2883 while ETH had never traded. The floors
    do not add; the lower one simply stands down first."""
    doc = (ROOT / "ETH.md").read_text(encoding="utf-8")
    assert "TWO" in doc and "concurrent positions" in doc
    assert "exchange_record" in doc, "the shared-floor mechanism must be named"
    assert "do not add up" in doc or "do not add" in doc
    assert "$40" not in doc, "the additive-floor claim was wrong"


def test_the_shared_floor_claim_matches_the_code():
    """Documentation that drifts from the code is worse than none. If
    `exchange_record` stopped being account-wide, the doc above would be
    describing a system that no longer exists."""
    import inspect

    from btc15_signal.store import Store

    source = inspect.getsource(Store.exchange_record)
    assert "FROM settlements" in source
    assert "strategy" not in source, "it must not filter to one instrument"


# ------------------------------------- only one instance owns the commands

def test_only_one_instance_consumes_the_command_stream():
    """Telegram getUpdates is DESTRUCTIVE: it acknowledges with an offset, so
    two processes on one bot token race for every message and the loser never
    sees it. The message they race for could be the kill switch. Found live
    on 2026-09-24 within minutes of starting the ETH instance beside BTC."""
    import inspect

    from btc15_signal import main

    source = inspect.getsource(main.process_telegram)
    assert "settings.telegram_commands_enabled" in source
    # the guard must come BEFORE anything consumes an update
    assert source.index("telegram_commands_enabled") < source.index(
        "telegram.updates()")


def test_btc_keeps_the_command_stream_by_default():
    from btc15_signal.config import Settings

    assert Settings().telegram_commands_enabled is True


def test_the_eth_launcher_gives_up_the_command_stream():
    launcher = (ROOT / "scripts" / "run_eth.ps1").read_text(encoding="utf-8")
    assert 'TELEGRAM_COMMANDS_ENABLED = "false"' in launcher


def test_a_silent_instance_still_sends_alerts():
    """It gives up LISTENING, not reporting. An instance that traded without
    saying so is the failure mode this whole system is built against.

    Asserted structurally: the flag is read in exactly ONE place - the
    command loop - so no send path can be gated by it however the file is
    later edited."""
    import inspect

    from btc15_signal import main

    whole = inspect.getsource(main)
    uses = whole.count("settings.telegram_commands_enabled")
    assert uses == 1, f"read in {uses} places; it must gate only the commands"
    command_loop = inspect.getsource(main.process_telegram)
    assert "settings.telegram_commands_enabled" in command_loop
    # and the entry/fill alert paths are untouched by it
    for fn in (main.primary_signal, main.cash_out_exit):
        assert "telegram_commands_enabled" not in inspect.getsource(fn)


def test_the_eth_launcher_halves_the_daily_floor():
    """Two processes do not share account guards, so leaving this out would
    silently turn a $20 floor into $40."""
    launcher = (ROOT / "scripts" / "run_eth.ps1").read_text(encoding="utf-8")
    assert 'AUTO_DAILY_LOSS_LIMIT = "10"' in launcher
