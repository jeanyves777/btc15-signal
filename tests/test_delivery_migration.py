"""Delivery tracking, upgraded onto the schema that was actually live.

Three faults of one shape, each of which looked fine in every test that built
its database from scratch:

  * `status` shipped inside `CREATE TABLE IF NOT EXISTS`, which adds nothing
    to an existing table, so it reached every fresh install and no live one;
  * `begin_delivery` - the first statement `send_once` runs - sat OUTSIDE
    that method's try block;
  * the fill site catches `(httpx.HTTPError, OSError, RuntimeError,
    ValueError)`, and `sqlite3.OperationalError` is none of those, so the
    exception went straight through the order-reporting path and out of the
    poll loop, with the position already open.

Every test here starts from THE EXACT PRE-MIGRATION SCHEMA, byte for byte,
because a test that starts from the current DDL cannot see any of this.
"""

import asyncio
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from btc15_signal.config import Settings  # noqa: E402
from btc15_signal.notify import Notifier  # noqa: E402
from btc15_signal.store import Store  # noqa: E402

# The table as it stood on the live database at 14:45 on 2026-09-23, read
# back from `PRAGMA table_info` before anything was changed.
OLD_SCHEMA = """
    CREATE TABLE notifications (
        kind TEXT NOT NULL,
        event_key TEXT NOT NULL,
        message_id INTEGER,
        body TEXT,
        first_ms INTEGER NOT NULL,
        updated_ms INTEGER NOT NULL,
        PRIMARY KEY (kind, event_key)
    )
"""
OLD_COLUMNS = ["kind", "event_key", "message_id", "body",
               "first_ms", "updated_ms"]


def legacy_db(path: Path, rows: list[tuple] = ()) -> Path:
    db = sqlite3.connect(str(path))
    db.execute(OLD_SCHEMA)
    for row in rows:
        db.execute(
            "INSERT INTO notifications (kind, event_key, message_id, body, "
            "first_ms, updated_ms) VALUES (?, ?, ?, ?, ?, ?)", row)
    db.commit()
    db.close()
    return path


def columns(store: Store) -> list[str]:
    return [r[1] for r in store.db.execute("PRAGMA table_info(notifications)")]


# ------------------------------------------------- the schema it upgrades

def test_the_legacy_schema_really_is_missing_the_column(tmp_path):
    """If this ever fails the fixture has drifted and the rest proves
    nothing."""
    db = sqlite3.connect(str(legacy_db(tmp_path / "a.db")))
    cols = [r[1] for r in db.execute("PRAGMA table_info(notifications)")]
    assert cols == OLD_COLUMNS
    assert "status" not in cols


def test_opening_a_legacy_database_migrates_it(tmp_path):
    store = Store(str(legacy_db(tmp_path / "b.db")))
    assert "status" in columns(store)


def test_every_column_delivery_needs_is_present(tmp_path):
    store = Store(str(legacy_db(tmp_path / "c.db")))
    present = set(columns(store))
    for name in ("kind", "event_key", "message_id", "body", "status",
                 "first_ms", "updated_ms"):
        assert name in present, f"delivery tracking needs {name}"
    assert set(Store.DELIVERY_COLUMNS) <= present


def test_the_insight_table_is_created_too(tmp_path):
    """The rotation persists in `settings_text`; a message that cannot read
    its variant silently stops rotating."""
    store = Store(str(legacy_db(tmp_path / "d.db")))
    cols = [r[1] for r in store.db.execute("PRAGMA table_info(settings_text)")]
    assert {"key", "text_value", "updated_at"} <= set(cols)


# ----------------------------------------------------------- idempotence

def test_running_the_migration_twice_changes_nothing(tmp_path):
    path = legacy_db(tmp_path / "e.db")
    first = Store(str(path))
    before = columns(first)
    first.db.close()

    second = Store(str(path))               # a restart
    assert columns(second) == before
    second._migrate_delivery()              # and again, in-process
    second._migrate_delivery()
    assert columns(second) == before


def test_the_migration_is_a_no_op_on_a_fresh_database(tmp_path):
    fresh = Store(str(tmp_path / "f.db"))
    before = columns(fresh)
    fresh._migrate_delivery()
    assert columns(fresh) == before


# -------------------------------------------------------- rows are kept

def test_existing_rows_survive_the_upgrade(tmp_path):
    path = legacy_db(tmp_path / "g.db", rows=[
        ("signal", "w1", 42, "first body", 1_000, 1_000),
        ("settlement", "w1", 43, "second body", 2_000, 2_000),
    ])
    store = Store(str(path))
    rows = store._dicts("SELECT * FROM notifications ORDER BY event_key, kind")
    assert len(rows) == 2
    assert {r["message_id"] for r in rows} == {42, 43}
    assert {r["body"] for r in rows} == {"first body", "second body"}


def test_rows_written_before_the_column_existed_count_as_sent(tmp_path):
    """They DID go out. Defaulting them to 'pending' would make every one an
    unresolved claim and resend the lot at the next startup."""
    path = legacy_db(tmp_path / "h.db", rows=[
        ("settlement", "w1", 42, "x", 1_000, 1_000),
    ])
    store = Store(str(path))
    assert store._dicts("SELECT status FROM notifications")[0]["status"] == "sent"
    assert store.pending_deliveries() == []


def test_a_legacy_row_still_suppresses_its_duplicate(tmp_path):
    """The dedupe the old table existed for must keep working across the
    upgrade, or every pre-migration event is announced a second time."""
    path = legacy_db(tmp_path / "i.db", rows=[
        ("signal", "w1", 42, "x", 1_000, 1_000),
    ])
    store = Store(str(path))
    assert store.begin_delivery("signal", "w1", 3_000) is False
    assert store.delivered("signal", "w1")["message_id"] == 42


# ------------------------------------- claiming, persistence, recovery

def test_claiming_works_on_the_upgraded_schema(tmp_path):
    store = Store(str(legacy_db(tmp_path / "j.db")))
    assert store.begin_delivery("fill", "w1:p1", 1_000) is True
    assert store.begin_delivery("fill", "w1:p1", 1_100) is False
    claim = store.delivered("fill", "w1:p1")
    assert claim["status"] == "pending"
    assert claim["first_ms"] == 1_000


def test_a_successful_send_is_persisted(tmp_path):
    store = Store(str(legacy_db(tmp_path / "k.db")))
    store.begin_delivery("fill", "w1:p1", 1_000)
    store.confirm_delivery("fill", "w1:p1", 2_000, 777, "the body")
    row = store.delivered("fill", "w1:p1")
    assert row["status"] == "sent"
    assert row["message_id"] == 777
    assert row["body"] == "the body"
    assert row["updated_ms"] == 2_000
    assert store.pending_deliveries() == []


def test_a_claim_that_was_never_confirmed_is_the_crash_window(tmp_path):
    store = Store(str(legacy_db(tmp_path / "l.db")))
    store.begin_delivery("fill", "w1:p1", 1_000)
    pending = store.pending_deliveries()
    assert [(r["kind"], r["event_key"]) for r in pending] == [("fill", "w1:p1")]


def test_restart_recovery_separates_money_from_the_rest(tmp_path):
    """A position the operator does not know about is worse than one they are
    told about twice; a stale signal is worse than a gap.

    The two directions are OPPOSITE operations on the row, which is the part
    worth pinning:

      resend kinds  the claim is CLEARED, so the next poll is free to send it
                    again - the row's continued existence would block it;
      drop kinds    the claim is MARKED SENT, so nothing sends it again and it
                    never reappears as pending.
    """
    path = tmp_path / "m.db"
    store = Store(str(legacy_db(path)))
    for kind in ("fill", "settlement", "signal", "automation_off"):
        store.begin_delivery(kind, "w1", 1_000)
    store.db.close()

    restarted = Store(str(path))
    notifier = Notifier(telegram=None, store=restarted, settings=Settings())
    resolved = notifier.resolve_crash_window(5_000)

    assert {r["kind"] for r in resolved if r["resolution"] == "resend"} \
        == {"fill", "settlement"}
    assert {r["kind"] for r in resolved if r["resolution"] != "resend"} \
        == {"signal", "automation_off"}
    # Nothing is left ambiguous either way.
    assert restarted.pending_deliveries() == []

    left = {r["kind"] for r in
            restarted._dicts("SELECT kind FROM notifications")}
    assert left == {"signal", "automation_off"}, \
        "a dropped claim stays, marked sent, so it cannot fire again"
    for kind in ("fill", "settlement"):
        assert restarted.delivered(kind, "w1") is None, \
            "a resend claim must be cleared, or it blocks its own resend"


def test_a_cleared_claim_can_actually_be_sent_again(tmp_path):
    """Clearing is only correct if it unblocks the next attempt. If the row
    survived, `begin_delivery` would refuse and the resend policy would be a
    comment rather than a behaviour."""
    path = tmp_path / "n.db"
    store = Store(str(legacy_db(path)))
    store.begin_delivery("settlement", "w1", 1_000)
    store.db.close()

    restarted = Store(str(path))
    Notifier(None, restarted, Settings()).resolve_crash_window(5_000)
    assert restarted.begin_delivery("settlement", "w1", 6_000) is True


def test_a_dropped_claim_cannot_be_sent_again(tmp_path):
    """The mirror image: marking it sent is only correct if it BLOCKS the
    next attempt."""
    path = tmp_path / "n2.db"
    store = Store(str(legacy_db(path)))
    store.begin_delivery("signal", "w1", 1_000)
    store.db.close()

    restarted = Store(str(path))
    Notifier(None, restarted, Settings()).resolve_crash_window(5_000)
    assert restarted.begin_delivery("signal", "w1", 6_000) is False
    assert restarted.delivered("signal", "w1")["status"] == "sent"


# ---------------------------- reporting cannot interrupt trading

class _Broken:
    """A store whose delivery calls fail the way the live one did."""

    def __init__(self, real):
        self._real = real

    def __getattr__(self, name):
        return getattr(self._real, name)

    def begin_delivery(self, *a, **k):
        raise sqlite3.OperationalError("no such column: status")


class _BrokenTelegram:
    async def send(self, text, buttons=None):
        raise ConnectionError("telegram unreachable")

    async def edit(self, message_id, text, buttons=None):
        raise ConnectionError("telegram unreachable")


class _Telegram:
    def __init__(self):
        self.sent = []

    async def send(self, text, buttons=None):
        self.sent.append(text)
        return 900 + len(self.sent)

    async def edit(self, message_id, text, buttons=None):
        return True


def test_a_database_error_does_not_escape_the_notifier(tmp_path):
    """THE ORIGINAL FAULT. `sqlite3.OperationalError` is not in the fill
    site's except tuple, so it left the order-reporting path entirely - with
    the position already open and the code that manages it downstream."""
    store = Store(str(tmp_path / "o.db"))
    notifier = Notifier(_Telegram(), _Broken(store), Settings())
    sent = asyncio.run(notifier.send_once("fill", "w1:p1", "x", 1_000))
    assert sent is False


def test_a_telegram_failure_does_not_escape_the_notifier(tmp_path):
    store = Store(str(tmp_path / "p.db"))
    notifier = Notifier(_BrokenTelegram(), store, Settings())
    assert asyncio.run(notifier.send_once("fill", "w1:p1", "x", 1_000)) is False


def test_a_failed_send_leaves_the_claim_pending(tmp_path):
    """Which is the honest record: startup resolves it by policy rather than
    this path guessing whether it arrived."""
    store = Store(str(tmp_path / "q.db"))
    notifier = Notifier(_BrokenTelegram(), store, Settings())
    asyncio.run(notifier.send_once("fill", "w1:p1", "x", 1_000))
    assert [r["kind"] for r in store.pending_deliveries()] == ["fill"]


def test_an_edit_failure_does_not_escape_either(tmp_path):
    """`update_status` runs on the poll that is deciding whether to order."""
    store = Store(str(tmp_path / "r.db"))
    store.begin_delivery("signal", "w1", 1_000)
    store.confirm_delivery("signal", "w1", 1_000, 55, "old")
    notifier = Notifier(_BrokenTelegram(), store, Settings())
    assert asyncio.run(
        notifier.update_status("signal", "w1", "new", 2_000)) is False


def test_a_broken_crash_window_does_not_block_startup(tmp_path):
    store = Store(str(tmp_path / "s.db"))
    notifier = Notifier(_Telegram(), _Broken(store), Settings())

    def boom(*a, **k):
        raise sqlite3.OperationalError("no such column: status")

    notifier.store.resolve_pending = boom
    assert notifier.resolve_crash_window(1_000) == []


def test_a_send_that_went_out_is_reported_as_sent_even_if_the_write_fails(
        tmp_path):
    """It DID go out. Returning False would make the caller believe nothing
    was announced."""
    store = Store(str(tmp_path / "t.db"))
    tg = _Telegram()
    notifier = Notifier(tg, store, Settings())

    def boom(*a, **k):
        raise sqlite3.OperationalError("disk I/O error")

    store.confirm_delivery = boom
    assert asyncio.run(notifier.send_once("fill", "w1:p1", "x", 1_000)) is True
    assert len(tg.sent) == 1


# ------------------------------------ the migration runs before any send

def test_the_migration_runs_inside_store_construction():
    """Not on first use, and not from a caller that might be skipped: every
    notifier, runner and poll receives an already-migrated Store, because the
    constructor itself runs it."""
    import ast

    src = (ROOT / "src" / "btc15_signal" / "store.py").read_text(
        encoding="utf-8")
    tree = ast.parse(src)
    store_cls = next(n for n in ast.walk(tree)
                     if isinstance(n, ast.ClassDef) and n.name == "Store")
    init = next(n for n in store_cls.body
                if isinstance(n, ast.FunctionDef) and n.name == "__init__")
    body = ast.get_source_segment(src, init) or ""
    assert "self._migrate_delivery()" in body

    # And it is reached unconditionally - not behind a "new database" branch,
    # which is exactly how the column came to exist only on fresh installs.
    call_line = next(i for i, line in enumerate(body.split("\n"))
                     if "self._migrate_delivery()" in line)
    indent = len(body.split("\n")[call_line]) - len(
        body.split("\n")[call_line].lstrip())
    assert indent == 8, "the migration sits at method level, not inside a branch"


def test_a_notifier_cannot_be_built_on_an_unmigrated_store(tmp_path):
    """The end-to-end version of the same guarantee: hand the legacy schema
    straight to a notifier and claim on it."""
    store = Store(str(legacy_db(tmp_path / "u.db")))
    notifier = Notifier(_Telegram(), store, Settings())
    assert asyncio.run(
        notifier.send_once("fill", "w1:p1", "body", 1_000)) is True
    assert store.delivered("fill", "w1:p1")["status"] == "sent"
