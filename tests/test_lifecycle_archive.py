"""The archive must be good enough to DISCOVER a strategy, not just review alerts.

Reviewing alerts needs the rows we already alerted on. Discovery needs the ones
we did not: the polls after the entry scan closed, the setups the rule rejected,
the moments the spread was too wide to trade. Each test below pins one of those.
"""

from pathlib import Path

from btc15_signal.snapshot import MarketSnapshot
from btc15_signal.config import Settings
from btc15_signal.kalshi import KalshiMarket
from btc15_signal.main import archive_observation
from btc15_signal.store import Store

# Source-text assertions are a secondary safeguard only. They catch a gate being
# reintroduced by name, but harmless refactoring breaks them, so every property
# below is proved by DRIVING the archiver and inspecting what it wrote.
SOURCE = Path("src/btc15_signal/main.py").read_text(encoding="utf-8")
ARCHIVER = SOURCE.split("def archive_observation(")[1].split("async def primary_signal(")[0]

WINDOW_OPEN = 1_700_000_000_000
CLOSE_MS = WINDOW_OPEN + 900_000


def reference(target=84_000.0, value=84_100.0):
    """The BRTI features the service always has on this path.

    `archive_observation` will not fall back to the Binance model for a side,
    a distance or a rule verdict, so under `kalshi_only` it needs these - and
    the live call site has them, because the snapshot it passes was built
    from them a few lines earlier.
    """
    from btc15_signal.brti import BRTIFeatures

    return BRTIFeatures(
        event_ticker="KXBTC15M-DRIVE", ts_ms=CLOSE_MS - 600_000,
        target=target, value=value,
        signed_distance_bps=11.9, brti_momentum_bps=5.0,
        brti_volatility_bps=1.0, brti_normalized_distance=11.9,
        samples=300, span_ms=300_000, stale=False,
        settlement_projection=value,
    )


def drive(store, *, remaining, spread_bps=1.0, ticker="KXBTC15M-DRIVE",
          brti=..., **snap):
    """Run the real archiver once and return the row it wrote, if any."""
    settings = Settings(microstructure_path="does-not-exist.db")
    contract = KalshiMarket(
        ticker=ticker, target=84_000.0, open_ms=WINDOW_OPEN, close_ms=CLOSE_MS,
        yes_ask=0.85, no_ask=0.16,
    )
    fields = {
        "price": 84_100.0, "target": 84_000.0, "bid_imbalance": 0.0,
        "taker_imbalance": 0.1, "momentum_5m_bps": 5.0, "volatility_5m_bps": 10.0,
        "futures_basis_bps": 2.0, "spread_bps": spread_bps,
        "window_high": 84_200.0, "window_low": 83_900.0, "elapsed_minutes": 8.0,
    }
    fields.update(snap)
    archive_observation(
        settings, store, contract, MarketSnapshot(**fields),
        WINDOW_OPEN, remaining, CLOSE_MS - remaining * 1000,
        brti=(reference() if brti is ... else brti),
    )
    rows = store.lifecycle(WINDOW_OPEN)
    return next((r for r in rows if r["remaining_s"] == remaining), None)


def observation(**overrides):
    row = {
        "window_open": 1000, "remaining_s": 600, "observed_ms": 1_700_000_000_000,
        "ticker": "KXBTC15M-TEST", "market_id": "KXBTC15M-TEST",
        "session_id": "run-a", "signal_id": "sig-1",
        "target": 84_000.0, "btc": 84_100.0, "side": "UP",
        "raw_probability": 0.9, "bucket": 8, "our_ask": 0.85,
        "yes_ask": 0.85, "no_ask": 0.16, "exit_bid": 0.84,
        "momentum_5m_bps": 5.0, "volatility_5m_bps": 10.0,
        "distance_bps": 12.0, "normalized_distance": 1.2, "spread_bps": 1.0,
        "book_yes_depth": 2000.0, "book_no_depth": 3000.0, "book_yes_share": 0.4,
        "session": "europe", "vol_regime": "mid", "hour_utc": 10,
        "rule_match": 1, "alerted": 1, "order_state": "none",
    }
    row.update(overrides)
    return row


# --- 1. recording continues after the entry scan closes ----------------------


def test_recording_continues_after_the_entry_scan_closes(tmp_path):
    """The exit path lives entirely below entry_to_seconds. Stopping there threw
    away every observation of whether a position was ever in profit."""
    settings = Settings()
    store = Store(str(tmp_path / "a.db"))
    for remaining in (settings.entry_from_seconds + 120, 600, settings.entry_to_seconds, 200, 30):
        store.observe_full(observation(remaining_s=remaining))

    kept = [r["remaining_s"] for r in store.lifecycle(1000)]
    assert 30 in kept and 200 in kept, "post-entry observations were dropped"
    assert max(kept) > settings.entry_from_seconds, "pre-entry observations were dropped"


def test_the_real_archiver_records_outside_the_entry_window(tmp_path):
    """Behavioural: drive it above, inside and below the scan and check the rows."""
    settings = Settings()
    store = Store(str(tmp_path / "drive.db"))
    before = settings.entry_from_seconds + 200   # scan has not opened
    inside = (settings.entry_from_seconds + settings.entry_to_seconds) // 2
    after = settings.exit_min_seconds // 2       # past even the exit guard

    for remaining in (before, inside, after):
        assert drive(store, remaining=remaining) is not None, remaining

    kept = {r["remaining_s"] for r in store.lifecycle(WINDOW_OPEN)}
    assert kept == {before, inside, after}


# --- 2. rejected and non-alerting polls are retained -------------------------


def test_rejected_and_silent_polls_are_retained(tmp_path):
    """A strategy we do not have will not be found in the minutes we alert on."""
    store = Store(str(tmp_path / "a.db"))
    store.observe_full(observation(remaining_s=600, rule_match=0, alerted=0,
                                   failed_gates="contract price band"))
    store.observe_full(observation(remaining_s=590, rule_match=1, alerted=1))

    rows = store.lifecycle(1000)
    assert len(rows) == 2
    silent = [r for r in rows if not r["alerted"]]
    assert len(silent) == 1
    assert silent[0]["failed_gates"] == "contract price band"
    assert silent[0]["rule_match"] == 0


# --- 3. wide-spread periods are retained rather than skipped -----------------


def test_wide_spread_polls_are_retained(tmp_path):
    """primary_signal returns early on a wide spread. The archiver must not:
    those are conditions worth studying, not conditions worth discarding."""
    settings = Settings()
    store = Store(str(tmp_path / "a.db"))
    wide = settings.max_spread_bps * 20
    store.observe_full(observation(remaining_s=600, spread_bps=wide))

    assert store.lifecycle(1000)[0]["spread_bps"] == wide


def test_the_real_archiver_records_a_spread_too_wide_to_trade(tmp_path):
    """Behavioural: primary_signal refuses this spread; the archiver must keep it."""
    settings = Settings()
    store = Store(str(tmp_path / "drive.db"))
    wide = settings.max_spread_bps * 50
    assert wide > settings.max_spread_bps

    row = drive(store, remaining=600, spread_bps=wide)
    assert row is not None, "a wide-spread poll was discarded"
    assert row["spread_bps"] == wide


# --- 4. book and regime fields are present -----------------------------------


def test_book_and_regime_are_captured(tmp_path):
    store = Store(str(tmp_path / "a.db"))
    columns = {r[1] for r in store.db.execute("PRAGMA table_info(observations)")}
    for needed in (
        "book_yes_depth", "book_no_depth", "book_yes_share", "book_bid_size",
        "book_ask_size", "book_levels", "depth_bid_qty", "depth_ask_qty",
        "trade_count", "buy_volume", "sell_volume", "vwap", "book_age_s",
        "session", "weekday", "hour_utc", "vol_regime",
        "momentum_5m_bps", "volatility_5m_bps", "futures_basis_bps",
        "taker_imbalance", "normalized_distance", "exit_bid", "unrealised",
    ):
        assert needed in columns, needed

    store.observe_full(observation())
    row = store.lifecycle(1000)[0]
    assert row["book_yes_share"] == 0.4
    assert row["vol_regime"] == "mid" and row["session"] == "europe"


def test_a_stale_book_is_marked_rather_than_silently_trusted():
    """Depth from three minutes ago is not depth now."""
    metrics = SOURCE.split("def book_metrics(")[1].split("def archive_observation(")[0]
    assert "book_age_s" in metrics
    assert "stale" in metrics


# --- 5. capture stops only at settlement or confirmed exit -------------------


def test_capture_is_bounded_only_by_the_market_being_live():
    """No gate of its own: it runs for as long as the loop has a live market,
    which ends at settlement. The order state is recorded per poll so an exit
    shows as a transition rather than only as an end state."""
    loop = SOURCE.split("while True:")[1]
    assert loop.index("archive_observation(") < loop.index("await primary_signal(")


def test_a_broken_archiver_cannot_stop_the_service(tmp_path):
    """Behavioural: hand it a store whose write always fails and check it
    swallows the failure rather than propagating it into the trading loop."""
    store = Store(str(tmp_path / "drive.db"))

    def explode(_row):
        raise RuntimeError("disk on fire")

    store.observe_full = explode
    drive(store, remaining=600)  # must not raise


def test_the_order_state_transition_is_visible_in_the_path(tmp_path):
    store = Store(str(tmp_path / "a.db"))
    for remaining, state in ((600, "none"), (500, "proposed"), (400, "filled"),
                             (300, "exited")):
        store.observe_full(observation(remaining_s=remaining, order_state=state))

    states = [r["order_state"] for r in store.lifecycle(1000)]
    assert states == ["none", "proposed", "filled", "exited"]


# --- 6. settlement updates the same lifecycle, no mismatched records ---------


def test_settlement_updates_the_same_rows_and_creates_none(tmp_path):
    store = Store(str(tmp_path / "a.db"))
    for remaining in (600, 500, 400, 300):
        store.observe_full(observation(remaining_s=remaining))
    before = store.observation_coverage()["rows"]

    updated = store.settle_observations(1000, "UP", 84_150.0)

    after = store.observation_coverage()
    assert updated == before, "settlement must touch every row of the window"
    assert after["rows"] == before, "settlement must not create rows"
    assert after["settled"] == before
    assert {r["won"] for r in store.lifecycle(1000)} == {1}


def test_settlement_never_rewrites_an_outcome(tmp_path):
    store = Store(str(tmp_path / "a.db"))
    store.observe_full(observation())
    store.settle_observations(1000, "UP", 1.0)
    assert store.settle_observations(1000, "DOWN", 2.0) == 0
    assert store.lifecycle(1000)[0]["won"] == 1


def test_settlement_leaves_other_windows_alone(tmp_path):
    store = Store(str(tmp_path / "a.db"))
    store.observe_full(observation(window_open=1000))
    store.observe_full(observation(window_open=2000, signal_id="sig-2"))
    store.settle_observations(1000, "UP", 1.0)
    assert store.lifecycle(2000)[0]["won"] is None


# --- linkage -----------------------------------------------------------------


def test_a_path_survives_a_restart_because_the_signal_id_is_derived(tmp_path):
    """A service restart mid-window must not split one opportunity into two
    unjoinable halves."""
    from btc15_signal.main import signal_id_for

    ticker = "KXBTC15M-26SEP210745-45"
    assert signal_id_for(ticker) == signal_id_for(ticker)  # stable, not random
    assert signal_id_for(ticker) != signal_id_for("KXBTC15M-26SEP210800-00")

    store = Store(str(tmp_path / "a.db"))
    sid = signal_id_for(ticker)
    store.observe_full(observation(remaining_s=600, session_id="run-a", signal_id=sid))
    store.observe_full(observation(remaining_s=400, session_id="run-b", signal_id=sid))

    path = store.lifecycle_by_signal(sid)
    assert len(path) == 2
    assert {r["session_id"] for r in path} == {"run-a", "run-b"}


def test_every_observation_carries_all_three_identifiers(tmp_path):
    """Behavioural: drive the archiver and read the identifiers back."""
    from btc15_signal.main import SESSION_ID, signal_id_for

    store = Store(str(tmp_path / "drive.db"))
    row = drive(store, remaining=600, ticker="KXBTC15M-IDS")

    assert row["session_id"] == SESSION_ID
    assert row["market_id"] == "KXBTC15M-IDS"
    assert row["signal_id"] == signal_id_for("KXBTC15M-IDS")
    assert row["order_state"] == "none"  # no order exists yet
    assert row["alerted"] == 0


def test_a_missing_recorder_does_not_stop_the_observation(tmp_path):
    """The book is additive. If the recorder is down the row is still written,
    just without depth - losing the whole observation would be far worse."""
    store = Store(str(tmp_path / "drive.db"))
    row = drive(store, remaining=600)  # microstructure_path points at nothing
    assert row is not None
    assert row["book_yes_depth"] is None
    assert row["momentum_5m_bps"] == 5.0  # everything else survived


# --- the legacy boundary, enforced rather than remembered --------------------


def test_pre_linkage_rows_are_reported_not_hidden(tmp_path):
    """Rows written before signal_id existed are real observations and fine to
    keep. What must never happen is pooling them with linked rows, because that
    compares reconstructable paths against loose rows from the same period."""
    store = Store(str(tmp_path / "b.db"))
    store.observe_full(observation(remaining_s=600, signal_id=None, observed_ms=100))
    store.observe_full(observation(remaining_s=500, signal_id="sig-1", observed_ms=200))

    coverage = store.observation_coverage()
    assert coverage["rows"] == 2
    assert coverage["legacy_rows"] == 1
    assert coverage["linked_rows"] == 1
    # The boundary is the first LINKED row, not the first row.
    assert coverage["research_from_ms"] == 200


def test_research_rows_exclude_legacy_and_unsettled(tmp_path):
    store = Store(str(tmp_path / "b.db"))
    store.observe_full(observation(remaining_s=600, signal_id=None))       # legacy
    store.observe_full(observation(remaining_s=500, signal_id="sig-1"))    # unsettled
    store.observe_full(observation(remaining_s=400, signal_id="sig-1"))
    store.settle_observations(1000, "UP", 84_150.0)
    store.observe_full(observation(remaining_s=300, signal_id="sig-1"))    # after settle

    usable = store.research_rows()
    assert all(r["signal_id"] for r in usable), "a legacy row reached analysis"
    assert all(r["won"] is not None for r in usable), "an unsettled row reached analysis"
    assert {r["remaining_s"] for r in usable} == {500, 400}

    assert len(store.research_rows(settled_only=False)) == 3  # still excludes legacy


def test_a_legacy_row_can_never_join_a_reconstructed_path(tmp_path):
    """The whole point of the boundary: no lifecycle may span it."""
    store = Store(str(tmp_path / "b.db"))
    store.observe_full(observation(remaining_s=600, signal_id=None))
    store.observe_full(observation(remaining_s=500, signal_id="sig-1"))

    path = store.lifecycle_by_signal("sig-1")
    assert len(path) == 1
    assert path[0]["remaining_s"] == 500


def test_a_window_holding_both_sides_settles_each_row_on_its_own_side(tmp_path):
    """The model's side flips whenever BTC crosses the strike mid-window, so one
    window holds both UP and DOWN rows. Stamping a single boolean across all of
    them wrote the PREDICTION's outcome onto rows that had bet the other way -
    432 of 2,527 settled rows, 17.1% of the live archive, carried an inverted
    `won`, and every live-tape study that read the column inherited it."""
    store = Store(str(tmp_path / "a.db"))
    store.observe_full(observation(remaining_s=600, side="UP"))
    store.observe_full(observation(remaining_s=500, side="DOWN"))
    store.observe_full(observation(remaining_s=400, side="UP"))

    assert store.settle_observations(1000, "UP", 84_150.0) == 3

    rows = {r["remaining_s"]: r for r in store.lifecycle(1000)}
    assert rows[600]["won"] == 1, "UP row, UP won"
    assert rows[500]["won"] == 0, "DOWN row in the same window must NOT win"
    assert rows[400]["won"] == 1


def test_without_a_reference_no_row_is_invented(tmp_path):
    """Under `kalshi_only` there is no Kalshi-native side, distance or rule
    verdict without the reference, and the Binance model must not supply
    them. Nothing is written rather than a row whose columns mean something
    other than what they are named.

    This does not occur in the service: the call site builds its snapshot
    from these same features, so by the time the archiver runs they exist.
    """
    store = Store(str(tmp_path / "noref.db"))
    assert drive(store, remaining=600, brti=None) is None


def test_the_archived_features_are_the_reference_not_the_spot(tmp_path):
    """The snapshot carries `volatility_5m_bps=10.0` and the reference
    carries 1.0. A threshold measured on one does not transfer to the other,
    so the archive must say which it holds."""
    store = Store(str(tmp_path / "ref.db"))
    row = drive(store, remaining=600)
    assert row is not None
    assert row["volatility_5m_bps"] == 1.0, "spot volatility leaked in"
    assert row["raw_probability"] is None, "there is no Kalshi-native model"
    # -1 is `KalshiPrediction`'s documented no-model sentinel, and it is not a
    # bucket index - calibration is not consulted for it. What matters is that
    # it is not a Binance bucket wearing a neutral column name.
    assert row["bucket"] == -1
    assert row["bucket"] not in range(0, 10)
