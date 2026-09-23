"""The learning notification, and the two defects that kept it off the screen.

The v2 builder existed and shipped; the send site went on calling the v1 one,
so the operator read policy ids, feature fingerprints, the retired method that
was superseded and the train/validate/holdout split - none of which answer the
only question the message exists to answer.
"""

import ast
import asyncio
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from btc15_signal import messages  # noqa: E402
from btc15_signal.config import Settings  # noqa: E402
from btc15_signal.store import Store  # noqa: E402

RUNNER = (ROOT / "src" / "btc15_signal" / "learning_runner.py").read_text(
    encoding="utf-8")


def plain(text: str) -> str:
    return re.sub(r"<[^>]+>", "", text)


# ------------------------------------------------------------- the wording

def test_the_notification_reads_exactly_as_specified():
    text = plain(messages.learning_update(
        markets=6428, confidence_changes=3, entry_changes=0))
    assert text.splitlines() == [
        "\U0001f9e0 LEARNING UPDATE",
        "\U0001f4da Reviewed: 6,428 historical markets",
        "\U0001f3af Confidence: 3 setup adjustments enabled",
        "⚙️ Entry rules: Unchanged",
        "\U0001f504 Automatic learning continues.",
    ]


def test_one_adjustment_is_singular():
    text = plain(messages.learning_update(
        markets=100, confidence_changes=1, entry_changes=0))
    assert "1 setup adjustment enabled" in text
    assert "adjustments" not in text


def test_no_adjustment_reads_unchanged():
    text = plain(messages.learning_update(
        markets=100, confidence_changes=0, entry_changes=0))
    assert "Confidence: Unchanged" in text
    assert "Entry rules: Unchanged" in text


def test_confidence_and_entry_rules_are_never_one_count():
    """A confidence adjustment re-rates a word; an entry adjustment changes
    what is ordered. One number for both makes a label-only change read as a
    change to how money is spent."""
    text = plain(messages.learning_update(
        markets=100, confidence_changes=3, entry_changes=2))
    assert "Confidence: 3 setup adjustments enabled" in text
    assert "Entry rules: 2 adjustments enabled" in text


def test_no_identifier_reaches_the_operator():
    """Policy ids, fingerprints, methods and splits stay in the log."""
    text = messages.learning_update(
        markets=6428, confidence_changes=3, entry_changes=0)
    for leak in ("kalshi-brti", "arms-nested", "brti-2", "fingerprint",
                 "holdout", "validate", "policy", "retired", "superseded",
                 "9e41a3df"):
        assert leak not in text.lower(), f"{leak} leaked into the alert"


# ------------------------------------------------- the send site calls it

def test_the_send_site_uses_the_shared_builder():
    assert "messages.learning_update(" in RUNNER
    assert "messages.learning_activated(" not in RUNNER, \
        "the v1 formatter is still wired in"


def test_the_activation_is_claimed_before_it_is_sent():
    """A policy change the operator never saw is a change they cannot
    question, so it goes through the notifier like every money event."""
    assert 'notifier.send_once(\n                    "learning"' in RUNNER


def test_the_arm_counts_are_passed_as_counts():
    """`_confidence_arms` and `_promoted_arms` return ints. Wrapping them in
    len() raises TypeError inside the one handler that swallows it, so the
    notification goes missing and leaves only a line in the log."""
    tree = ast.parse(RUNNER)
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue
        if node.name not in ("_confidence_arms", "_promoted_arms"):
            continue
        assert isinstance(node.returns, ast.Name) and node.returns.id == "int"
    assert "len(outcome.confidence_arms" not in RUNNER
    assert "len(outcome.promoted_arms" not in RUNNER


# -------------------------------------------- the column it is stored in

def test_the_delivery_claim_works_on_a_database_that_predates_it(tmp_path):
    """`status` shipped inside CREATE TABLE IF NOT EXISTS, which adds nothing
    to an existing table. `begin_delivery` is the first statement `send_once`
    runs and sits OUTSIDE its try block, so on every live database the first
    message after the deploy raised `no such column: status` straight out of
    the poll loop."""
    import sqlite3

    path = tmp_path / "old.db"
    db = sqlite3.connect(str(path))
    db.execute("""
        CREATE TABLE notifications (
            kind TEXT NOT NULL, event_key TEXT NOT NULL, message_id INTEGER,
            body TEXT, first_ms INTEGER NOT NULL, updated_ms INTEGER NOT NULL,
            PRIMARY KEY (kind, event_key)
        )
    """)
    db.commit()
    db.close()

    store = Store(str(path))
    cols = {r[1] for r in store.db.execute("PRAGMA table_info(notifications)")}
    assert "status" in cols, "the migration never ran"
    assert store.begin_delivery("learning", "v1", 1_000) is True
    assert store.begin_delivery("learning", "v1", 1_000) is False, \
        "a claimed event must not be claimed twice"


def test_an_existing_row_keeps_working_after_the_migration(tmp_path):
    """Rows written before the column existed default to 'sent' - they DID go
    out, and treating them as pending would resend every one of them."""
    import sqlite3

    path = tmp_path / "rows.db"
    db = sqlite3.connect(str(path))
    db.execute("""
        CREATE TABLE notifications (
            kind TEXT NOT NULL, event_key TEXT NOT NULL, message_id INTEGER,
            body TEXT, first_ms INTEGER NOT NULL, updated_ms INTEGER NOT NULL,
            PRIMARY KEY (kind, event_key)
        )
    """)
    db.execute(
        "INSERT INTO notifications (kind, event_key, message_id, body, "
        "first_ms, updated_ms) VALUES ('signal', 'w1', 42, 'x', 1, 1)"
    )
    db.commit()
    db.close()

    store = Store(str(path))
    assert store.pending_deliveries() == [], \
        "an already-sent row must not read as an unresolved claim"


# ----------------------------------------- it is sent, end to end, once

class _Telegram:
    def __init__(self):
        self.sent = []

    async def send(self, text, buttons=None):
        self.sent.append(text)
        return 1000 + len(self.sent)


def test_the_notification_is_sent_once_per_policy_version(tmp_path):
    from btc15_signal.notify import Notifier

    store = Store(str(tmp_path / "s.db"))
    tg = _Telegram()
    notifier = Notifier(tg, store, Settings())
    text = messages.learning_update(
        markets=6428, confidence_changes=3, entry_changes=0)

    async def run():
        first = await notifier.send_once("learning", "v-1", text, 1_000)
        again = await notifier.send_once("learning", "v-1", text, 2_000)
        other = await notifier.send_once("learning", "v-2", text, 3_000)
        return first, again, other

    first, again, other = asyncio.run(run())
    assert first is True
    assert again is False, "one activation, one notification"
    assert other is True, "a new policy version is a new event"
    assert len(tg.sent) == 2
    assert "LEARNING UPDATE" in tg.sent[0]
