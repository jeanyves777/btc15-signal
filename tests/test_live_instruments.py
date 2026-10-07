"""Only BTC and GOLD trade live (operator, 2026-09-28; FINDINGS 108).

"Only gold and BTC are allowed to trade live." ETH and SOL went back to
shadow, BTC's recovery lost its SOL leg ("BTC AND GOLD ONLY I SAID"), and gold
trades on its OWN band - the settle timer read strategy.json's 0.70-0.93 for
every instance, so gold's rule qualified setups at 0.60-0.70 that automation
could never take ("use the numbers that work for Gold").

The switch itself is the stored `auto_trade_enabled` row (scripts/auto_switch.py)
and lives in each store, not here; these pin the code and the launchers.
"""

import inspect
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from btc15_signal import combo_recovery  # noqa: E402
from btc15_signal import main  # noqa: E402


def test_the_settle_timer_measures_the_instruments_own_band():
    src = " ".join(inspect.getsource(main).split())
    assert "settle_rule = kalshi_rule if settings.kalshi_only else rule" in src
    assert ("settled_s = store.band_streak_seconds( opened, settle_rule.min_ask, "
            "settle_rule.max_ask, now_ms)") in src
    assert "band_streak_seconds(opened, rule.min_ask, rule.max_ask" not in src


def band(name):
    data = json.loads((ROOT / name).read_text(encoding="utf-8"))
    return data["min_ask"], data["max_ask"]


def test_btc_and_eth_are_unchanged_by_it_and_gold_gets_its_own_band():
    generic = band("strategy.json")
    assert band("strategy_kalshi.json") == generic == (0.7, 0.93), "BTC unchanged"
    assert band("strategy_kalshi_eth.json") == generic, "ETH unchanged"
    # gold: its own 0.60-0.80 until the operator set it to BTC's (FINDINGS 110)
    assert band("strategy_kalshi_gold.json") == (0.7, 0.93)


def test_btc_has_no_combo_partner():
    assert "BTC" not in combo_recovery.PARTNERS
    assert "GOLD" not in combo_recovery.PARTNERS


def launcher(name):
    return (ROOT / "scripts" / name).read_text(encoding="utf-8")


def auto_default_on(text):
    return bool(re.search(r'^\s*\$env:AUTO_TRADE_ENABLED\s*=\s*"true"', text, re.M))


def test_the_launchers_default_only_gold_on_among_the_instances_that_changed():
    assert auto_default_on(launcher("run_gold.ps1"))
    for shadow in ("run_eth.ps1", "run_sol.ps1", "run_xrp.ps1", "run_near.ps1",
                   "run_silver.ps1"):
        assert not auto_default_on(launcher(shadow)), shadow


def test_gold_orders_are_capped_at_its_own_band_and_btc_is_unchanged():
    """The IOC is sent AT the ceiling. 0.95 was BTC's; gold's band tops out at
    0.80 and it is negative above 0.85 on every sample (review, 2026-09-28)."""
    src = " ".join(inspect.getsource(main.primary_signal).split())
    assert ("entry_ceiling = min(entry_ceiling, round( kalshi_rule.max_ask + "
            "settings.entry_slippage, 4))") in src
    assert "ceiling=entry_ceiling," in src
    from btc15_signal.config import Settings
    d = Settings.model_fields
    top, slip = d["max_entry_price"].default, d["entry_slippage"].default
    assert min(top, round(band("strategy_kalshi.json")[1] + slip, 4)) == top == 0.95
    assert min(top, round(band("strategy_kalshi_gold.json")[1] + slip, 4)) == 0.95


def test_btc_and_gold_entries_carry_no_recovery_label(tmp_path):
    """No partner, no recovery: after a loss every BTC or gold entry is an
    ordinary base-size entry, and says nothing about a recovery that cannot
    happen - "recovery due" repeated for five markets and re-armed."""
    sys.path.insert(0, str(Path(__file__).parent))
    from test_recovery_follows_base import DAY, entry, tiered

    from btc15_signal.config import Settings

    for series in ("KXBTC15M", "KXGOLD15M"):
        store = tiered(tmp_path, 2, name=f"{series}.db")
        entry(store, "loss", DAY, 2, won=False)
        step = Settings(loss_step_enabled=True, loss_step_budget=2.0,
                        kalshi_series=series)
        for ask in (0.72, 0.75, 0.88):
            count, why, base, is_recovery, _, due = main.recovery_sizing(
                store, step, 2, "", ask)
            assert (count, base, is_recovery, due) == (2, 2, False, False), series
            assert "recovery" not in why, (series, ask, why)


def test_the_standing_recovery_line_says_what_runs():
    from types import SimpleNamespace

    from btc15_signal import surface

    owed = SimpleNamespace(owes=True, base_only=False, deficit=36.63)
    surface.set_recovery(True, combo=False)          # BTC, gold: no partner
    line = surface.recovery_line(owed)
    assert "no combo" in line and "by combo" not in line
    surface.set_recovery(True, combo=True)           # ETH -> SOL
    assert "recovery by combo at base size" in surface.recovery_line(owed)
    src = " ".join(inspect.getsource(main.service).split())
    assert ("combo=settings.recovery_combo_enabled and bool( "
            "combo_recovery.PARTNERS.get(surface.asset(settings.kalshi_series)))") in src


# --------------------------------------------------- no recovery at all

def test_recovery_is_off_by_default_in_service():
    """Operator, 2026-09-28: "we need no recovery at all". The suite opts back
    in (conftest) to keep testing the mechanics; the service default is OFF."""
    from btc15_signal.config import Settings

    assert Settings.model_fields["recovery_enabled"].default is False


def test_with_recovery_off_every_entry_is_base_with_no_step(tmp_path):
    sys.path.insert(0, str(Path(__file__).parent))
    from test_recovery_follows_base import DAY, entry, tiered

    from btc15_signal.config import Settings

    store = tiered(tmp_path, 2)
    entry(store, "loss", DAY, 2, won=False)
    off = Settings(recovery_enabled=False, loss_step_enabled=True,
                   loss_step_budget=2.0, kalshi_series="KXETH15M")
    assert main.loss_step_size(store, off, 2, 0.75, lost=True) == (2, "")
    for ask in (0.72, 0.75, 0.88):
        count, why, base, is_recovery, _, due = main.recovery_sizing(
            store, off, 2, "", ask)
        assert (count, base, is_recovery, due) == (2, 2, False, False)
        assert "recovery" not in why


def test_with_recovery_off_nothing_is_announced_or_shown():
    from types import SimpleNamespace

    from btc15_signal import messages, surface

    src = " ".join(inspect.getsource(main.service).split())
    assert "if event and settings.recovery_enabled:" in src
    assert "surface.set_recovery( settings.recovery_enabled," in src
    owed = SimpleNamespace(owes=True, base_only=False, deficit=36.63)
    surface.set_recovery(False)
    assert surface.recovery_line(owed) == ""
    money = inspect.getsource(messages)
    assert ") if surface.recovery_on() else \"\"" in money
