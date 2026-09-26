"""The lifecycle records BY HOW MUCH a market finished past the target.

THE GAP THIS FILLS. The record said whether a trade won and what it paid. It
never said the MARGIN - and that is what separates an 83c favourite settling
comfortably from one that settled by a hair. Measured across 143 BTC and 14 ETH
executed trades:

    BTC winners  n=120   mean +$155.95   median +$127.50   18.3 bps
    BTC losers   n= 23   mean  -$54.39   median  -$27.48    6.4 bps
    ETH winners  n=  9   mean   +$4.91   median   +$4.37   18.3 bps
    ETH losers   n=  5   mean   -$1.74   median   -$0.96    6.5 bps

Winners finish a mean 18.3 bps past the target on BOTH instruments and losers
fall 6.4/6.5 bps short - the same figures in bps on assets 20x apart in price.
That is why the margin is stored raw and reported in bps: a $128 move on BTC and
a $4.37 move on ETH are the same event, and only one of those units says so.

FROM THE BROKER. `floor_strike` and `expiration_value` come off Kalshi's own
market object. `settlement_reference.db` holds equivalents and would have been
quicker to read, but the archive is ours and the settlement is Kalshi's; where
they disagree the broker is right by definition, which is the rule that already
governs P&L.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from btc15_signal import messages  # noqa: E402
from btc15_signal.store import Store  # noqa: E402

TICKER = "KXBTC15M-26SEP241745-45"


def settled(store, ticker=TICKER, result="yes", strike=None, value=None):
    store.db.execute(
        "INSERT OR REPLACE INTO settlements "
        "(ticker, market_result, pnl, settled_ms, window_ms) "
        "VALUES (?,?,?,?,?)", (ticker, result, 0.2, 1, 1))
    if strike is not None or value is not None:
        store.record_settlement_facts(ticker, strike, value, 99)
    store.db.commit()


# --------------------------------------------------------- the stored facts

def test_the_columns_exist_on_an_old_database(tmp_path):
    """`CREATE TABLE IF NOT EXISTS` never widens an existing table, so this
    has to arrive by migration or it reaches only fresh installs."""
    path = tmp_path / "t.db"
    Store(str(path)).db.close()
    cols = {r[1] for r in Store(str(path)).db.execute(
        "PRAGMA table_info(settlements)")}
    for needed in ("strike", "expiration_value", "facts_synced_ms"):
        assert needed in cols


def test_an_up_trade_wants_the_value_above_the_target(tmp_path):
    store = Store(str(tmp_path / "t.db"))
    settled(store, strike=84_411.33, value=84_538.83)
    m = store.settlement_margin(TICKER, "UP")
    assert round(m["favourable"], 2) == 127.50
    assert m["bps"] == 15.1


def test_a_down_trade_wants_the_value_below_the_target(tmp_path):
    store = Store(str(tmp_path / "t.db"))
    settled(store, strike=84_411.33, value=84_283.83)
    assert round(store.settlement_margin(TICKER, "DOWN")["favourable"], 2) == 127.50
    assert round(store.settlement_margin(TICKER, "UP")["favourable"], 2) == -127.50


def test_bps_makes_the_two_instruments_comparable(tmp_path):
    """The whole reason bps leads: these are the same event."""
    store = Store(str(tmp_path / "t.db"))
    settled(store, ticker="KXBTC15M-A", strike=84_411.33, value=84_411.33 + 154.5)
    settled(store, ticker="KXETH15M-A", strike=2_680.65, value=2_680.65 + 4.906)
    btc = store.settlement_margin("KXBTC15M-A", "UP")
    eth = store.settlement_margin("KXETH15M-A", "UP")
    assert abs(btc["bps"] - eth["bps"]) <= 0.2
    assert btc["favourable"] > 30 * eth["favourable"]


def test_a_missing_fact_is_not_a_zero_margin(tmp_path):
    """Un-fetched and finished-level-with-the-target are different facts."""
    store = Store(str(tmp_path / "t.db"))
    settled(store)
    assert store.settlement_margin(TICKER, "UP") is None


def test_a_zero_strike_cannot_produce_a_100_percent_move(tmp_path):
    store = Store(str(tmp_path / "t.db"))
    settled(store, strike=0.0, value=84_538.83)
    assert store.settlement_margin(TICKER, "UP") is None


# ------------------------------------------------------- the enrichment pass

def test_only_unenriched_settled_markets_are_queued(tmp_path):
    store = Store(str(tmp_path / "t.db"))
    settled(store, ticker="DONE-1", strike=1.0, value=2.0)
    settled(store, ticker="TODO-1")
    todo = store.settlements_missing_facts(10)
    assert "TODO-1" in todo and "DONE-1" not in todo


def test_a_market_kalshi_will_not_answer_is_not_retried_forever(tmp_path):
    """Marked as asked even when both numbers come back absent - otherwise the
    queue never drains and every poll re-fetches the same dead market."""
    store = Store(str(tmp_path / "t.db"))
    settled(store, ticker="BLANK-1")
    store.record_settlement_facts("BLANK-1", None, None, 123)
    assert "BLANK-1" not in store.settlements_missing_facts(10)
    assert store.settlement_margin("BLANK-1", "UP") is None


def test_a_pnl_resync_does_not_wipe_the_facts(tmp_path):
    """`record_settlements` upserts by ticker. It used to be a positional
    INSERT OR REPLACE, which would blank every column it did not list - so a
    routine P&L re-sync would have silently discarded the enrichment."""
    store = Store(str(tmp_path / "t.db"))
    settled(store, strike=84_411.33, value=84_538.83)
    store.record_settlements([{
        "ticker": TICKER, "market_result": "yes", "revenue": 100,
        "settled_time": "2026-09-24T21:45:08Z",
    }], 1_790_000_000_000)
    assert store.settlement_margin(TICKER, "UP") is not None, \
        "the resync destroyed the settlement facts"


# ------------------------------------------------------------- the rendering

def test_a_favourable_finish_reads_as_past_the_target():
    line = messages._margin_lines({"favourable": 127.5, "bps": 18.3})[0]
    assert "past the target" in line and "18.3 bps" in line
    assert "$127.50" in line


def test_an_adverse_finish_reads_as_short_of_it():
    line = messages._margin_lines({"favourable": -27.48, "bps": 6.4})[0]
    assert "short of the target" in line
    assert "-" not in line.split("Settled")[1].split()[0], \
        "the sign is carried by the wording, not by a minus in the amount"


def test_landing_on_the_strike_is_named_not_rounded_away():
    line = messages._margin_lines({"favourable": 0.0, "bps": 0.0})[0]
    assert "exactly ON the target" in line


def test_an_absent_margin_renders_nothing():
    assert messages._margin_lines(None) == []
    assert messages._margin_lines({}) == []


def test_the_recap_carries_it_and_the_service_supplies_it():
    text = messages.result_message(
        side="UP", ticker=TICKER, winner="UP", won=True, traded=True,
        pnl=0.19, contracts=1, paid=0.78,
        margin={"favourable": 127.5, "bps": 18.3},
    )
    assert "past the target" in text

    source = (Path(__file__).resolve().parents[1] / "src" / "btc15_signal"
              / "main.py").read_text(encoding="utf-8")
    assert "margin=store.settlement_margin(ticker, side)" in source
    assert "store.settlements_missing_facts(" in source
