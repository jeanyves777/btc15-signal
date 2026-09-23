"""The footer's totals, and the schedule that survives a deployment.

A live recap headlined `LOSS -$1.72` above `Today: +$3.71 / 26 closed` and
`Live since 19 Sep: +$6.61 / 164 closed`. Both totals predated the loss they
sat under, and `Today` was not realised P&L at all - it was realised plus the
open position marked to the bid. So on the poll where the position marked to
zero the dollars moved while the count did not, and one label carried two
quantities with nothing to tell them apart.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from btc15_signal import messages, surface  # noqa: E402
from btc15_signal.config import Settings  # noqa: E402
from btc15_signal.learning_runner import LearningRunner  # noqa: E402
from btc15_signal.store import LifetimeRecord, MoneySnapshot, Store  # noqa: E402

LIFE = LifetimeRecord(markets=165, winners=126, dollars=4.89,
                      since_ms=1_789_000_000_000, complete=False)


def snap(**over) -> MoneySnapshot:
    base = dict(markets=27, winners=25, realised=3.71, open_mark=0.0,
                taken_ms=1_790_000_000_000, has_mirror=True, lifetime=LIFE)
    base.update(over)
    return MoneySnapshot(**base)


def plain(text: str) -> str:
    import re

    return re.sub(r"<[^>]+>", "", text)


# ------------------------------------------------- Today is one quantity

def test_today_is_realised_and_says_so():
    lines = plain("\n".join(surface.money_footer(snap(realised=3.71))))
    assert "Today: +$3.71" in lines
    assert "(realised)" in lines


def test_an_open_position_is_not_folded_into_today():
    """It used to be. `Today` then moved when a position marked, with no
    market having settled, while the count beside it stood still."""
    lines = plain("\n".join(surface.money_footer(
        snap(realised=3.71, open_mark=1.64))))
    assert "Today: +$3.71" in lines, "the mark must not move the realised total"
    assert "Open position: +$1.64" in lines
    assert "not yet realised" in lines
    assert "+$5.35" not in lines, "the two must never be summed under one label"


def test_no_open_line_when_there_is_no_open_position():
    lines = plain("\n".join(surface.money_footer(snap(open_mark=0.0))))
    assert "Open position" not in lines


def test_a_sub_cent_mark_is_not_announced_as_a_position():
    lines = plain("\n".join(surface.money_footer(snap(open_mark=0.002))))
    assert "Open position" not in lines


# ------------------------------------------ totals that lag say they lag

def test_an_unreconciled_recap_says_the_totals_are_behind():
    """The alternative is what shipped: a recap announcing a loss above
    totals that silently did not contain it."""
    text = plain(messages.result_message(
        side="DOWN", ticker="T", winner="UP", won=False, traded=True,
        pnl=-1.72, contracts=2, paid=0.85, fee=0.02, called_side="DOWN",
        qualified=True, snapshot=snap(markets=26, realised=5.43, pending=True)))
    assert "last broker reconciliation" in text
    assert "not in them yet" in text


def test_a_reconciled_recap_says_nothing_extra():
    text = plain(messages.result_message(
        side="DOWN", ticker="T", winner="UP", won=False, traded=True,
        pnl=-1.72, contracts=2, paid=0.85, fee=0.02, called_side="DOWN",
        qualified=True, snapshot=snap()))
    assert "reconciliation" not in text


def test_the_recap_checks_the_ledger_before_it_renders():
    """`realised_for_ticker` is the ledger's answer for THIS market; the flag
    is set from it rather than from a guess about timing."""
    src = (ROOT / "src" / "btc15_signal" / "main.py").read_text(encoding="utf-8")
    block = src[src.index("def report_settlement"):]
    block = block[:block.index("\nasync def ", 1)]
    assert "store.realised_for_ticker(ticker) is None" in block
    assert "pending=True" in block


def test_an_untraded_market_is_never_flagged_pending():
    """Nothing was bought, so there is nothing for the broker to reconcile."""
    src = (ROOT / "src" / "btc15_signal" / "main.py").read_text(encoding="utf-8")
    block = src[src.index("def report_settlement"):]
    block = block[:block.index("\nasync def ", 1)]
    assert "if traded and store.realised_for_ticker" in block


# -------------------------------- the schedule outlives the deployment

def test_a_restart_does_not_move_the_next_training_time(tmp_path):
    """Re-anchoring the schedule on every start would let a day of deploys
    postpone training indefinitely - which is exactly what a deploy-heavy
    day looks like."""
    settings = Settings()
    store = Store(str(tmp_path / "s.db"))
    runner = LearningRunner(settings, store)
    runner.learning.set("next_due_ms", 1_790_210_747_167, 1_790_189_147_167)

    # A restart: a fresh runner over the same database.
    again = LearningRunner(settings, Store(str(tmp_path / "s.db")))
    again.startup(1_790_191_545_000)
    assert int(again.learning.get("next_due_ms", 0)) == 1_790_210_747_167


def test_a_fresh_install_schedules_rather_than_fires(tmp_path):
    """Otherwise a restart loop becomes a training loop."""
    settings = Settings()
    store = Store(str(tmp_path / "f.db"))
    runner = LearningRunner(settings, store)
    now = 1_790_191_545_000
    before = int(runner.learning.get("next_due_ms", 0) or 0)
    assert before == 0
    runner.due(now)
    after = int(runner.learning.get("next_due_ms", 0) or 0)
    # Either it scheduled one, or it is due for bootstrap - never neither.
    assert after > now or runner.due(now)[1] == "bootstrap"
