"""Money reported under an instrument's name must be that instrument's money.

THE CASE, 2026-09-24. Both live instances reconcile the WHOLE Kalshi account
into their own ledger - deliberately, because the account is one pot and the
capital controller has to see all of it. But the performance footer read from
the same unscoped table, so the BTC message said:

    Live since 19 Sep: -$8.20 · 231 closed · 173W-58L

while KXBTC15M itself was **+$0.11 over 211 markets**. The difference was ETH
(-$1.44, 14 markets) and seven markets nothing in this system placed - five
KXBTCD and two KXAAAGASD, -$6.76 between them, with no trade_proposals row and
no manual_trades row to account for them.

A strategy cannot be judged against a number that is mostly not its own, and
the direction of the error is the dangerous one: it made a flat strategy look
like a losing one, which is an invitation to change something that is working.

The account total is not hidden - it is shown as what it is. What is refused
is letting foreign P&L wear this strategy's label.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from btc15_signal.store import Store  # noqa: E402


class FakeSettings:
    def __init__(self, series):
        self.kalshi_series = series


def ledger(store, rows):
    for ticker, window_ms, pnl in rows:
        store.db.execute(
            "INSERT OR REPLACE INTO daily_ledger "
            "(ticker, window_ms, pnl, won, source, first_ms, updated_ms) "
            "VALUES (?,?,?,?,?,?,?)",
            (ticker, window_ms, pnl, 1 if pnl > 0 else 0, "test",
             window_ms, window_ms),
        )
    store.db.commit()


ROWS = [
    ("KXBTC15M-26SEP241000-00", 1_790_000_000_000, +1.00),
    ("KXBTC15M-26SEP241015-15", 1_790_000_900_000, -0.50),
    ("KXETH15M-26SEP241000-00", 1_790_000_000_000, -1.44),
    ("KXBTCD-26SEP2000-T80099.99", 1_789_900_000_000, -2.00),
    ("KXAAAGASD-26SEP21-4.4750", 1_789_900_000_000, -1.26),
]


def build(tmp_path, series=None):
    store = Store(str(tmp_path / "t.db"))
    ledger(store, ROWS)
    if series is not None:
        store.configure_instrument(FakeSettings(series))
    return store


def test_unconfigured_store_still_reports_the_whole_ledger(tmp_path):
    """A test or a single-instance deployment must not change behaviour."""
    record = build(tmp_path).lifetime_record()
    assert record.markets == 5
    assert round(record.dollars, 2) == round(sum(r[2] for r in ROWS), 2)
    assert record.series is None


def test_btc_reports_only_btc(tmp_path):
    record = build(tmp_path, "KXBTC15M").lifetime_record()
    assert record.markets == 2
    assert round(record.dollars, 2) == 0.50
    assert record.series == "KXBTC15M"


def test_eth_reports_only_eth(tmp_path):
    record = build(tmp_path, "KXETH15M").lifetime_record()
    assert record.markets == 1
    assert round(record.dollars, 2) == -1.44


def test_the_rest_of_the_account_is_reported_not_absorbed(tmp_path):
    """Foreign P&L is real money. It may not be silent, and it may not be
    counted as this strategy's."""
    record = build(tmp_path, "KXBTC15M").lifetime_record()
    assert record.foreign_markets == 3          # ETH + 2 hand-placed
    assert round(record.foreign_dollars, 2) == -4.70
    assert record.dollars != record.dollars + record.foreign_dollars


def test_a_prefix_cannot_match_a_different_series(tmp_path):
    """KXBTC15M must not swallow KXBTCD - they share a prefix up to the D."""
    record = build(tmp_path, "KXBTC15M").lifetime_record()
    assert record.markets == 2, "KXBTCD leaked into the KXBTC15M total"


def test_the_footer_names_the_instrument_and_shows_the_remainder(tmp_path):
    from btc15_signal import surface
    record = build(tmp_path, "KXBTC15M").lifetime_record()

    class Snap:
        realised = 0.0
        markets = 0
        winners = 0
        losers = 0
        lifetime = record
        open_position = None
        sessions = ()
    text = "\n".join(surface.money_footer(Snap()))
    assert "KXBTC15M" in text
    assert "did not place" in text


def test_the_service_binds_the_store_at_startup():
    """A scoping that exists but is never called is worse than none."""
    source = (Path(__file__).resolve().parents[1]
              / "src" / "btc15_signal" / "main.py").read_text(encoding="utf-8")
    assert "store.configure_instrument(settings)" in source
