"""The hourly ladder: parsing, integrity, and the archive.

These are behavioural tests. They assert what the code DOES with a chain, not
how it is spelled, so a refactor that preserves behaviour keeps passing.
"""

from datetime import UTC, datetime

import pytest

from btc15_signal.hourly import Chain, check_integrity, parse_chain
from btc15_signal.hourly_store import HourlyStore

# Derived from the ISO strings the fixture rows actually carry, so the
# constants cannot drift away from what the parser reads.
OPEN_MS = int(datetime(2026, 9, 21, 15, 0, tzinfo=UTC).timestamp() * 1000)
CLOSE_MS = OPEN_MS + 3_600_000
UPDATED_MS = OPEN_MS + 1_800_000


def raw(strike: float, yes_bid: float, yes_ask: float, **over) -> dict:
    row = {
        "ticker": f"KXBTCD-26SEP2112-T{strike}",
        "event_ticker": "KXBTCD-26SEP2112",
        "open_time": "2026-09-21T15:00:00Z",
        "close_time": "2026-09-21T16:00:00Z",
        "strike_type": "greater",
        "floor_strike": strike,
        "yes_bid_dollars": yes_bid,
        "yes_ask_dollars": yes_ask,
        "no_bid_dollars": round(1 - yes_ask, 2),
        "no_ask_dollars": round(1 - yes_bid, 2),
        "yes_bid_size_fp": 100.0,
        "yes_ask_size_fp": 100.0,
        "volume_fp": 10.0,
        "open_interest_fp": 5.0,
        "updated_time": "2026-09-21T15:30:00Z",
    }
    row.update(over)
    return row


def chain_of(rows: list[dict], fetched_ms: int = OPEN_MS + 1_800_000) -> Chain:
    chain = parse_chain(rows, fetched_ms)
    assert chain is not None
    return chain


# --- parsing ---------------------------------------------------------------


def test_rungs_come_back_sorted_by_strike_whatever_order_kalshi_sent():
    chain = chain_of([raw(86000, 0.4, 0.42), raw(85800, 0.7, 0.72), raw(85900, 0.55, 0.57)])
    assert [s.strike for s in chain.strikes] == [85800, 85900, 86000]


def test_non_threshold_rows_are_dropped_and_counted_not_coerced():
    """A bracket contract modelled as a threshold would be silently wrong."""
    rows = [
        raw(85800, 0.7, 0.72),
        raw(85900, 0.55, 0.57, strike_type="between", ticker="KXBTC-26SEP2112-B85950"),
    ]
    chain = chain_of(rows)
    assert len(chain.strikes) == 1
    assert chain.skipped_types == 1


def test_a_chain_of_only_brackets_is_not_a_chain():
    assert parse_chain([raw(85800, 0.7, 0.72, strike_type="between")], OPEN_MS) is None


def test_empty_input_is_none_rather_than_an_empty_chain():
    assert parse_chain([], OPEN_MS) is None


def test_cent_denominated_prices_are_converted_to_dollars():
    """Kalshi returns cents on some fields and dollars on others."""
    chain = chain_of([raw(85800, 70, 72)])
    assert chain.strikes[0].yes_bid == pytest.approx(0.70)
    assert chain.strikes[0].yes_ask == pytest.approx(0.72)


def test_remaining_seconds_counts_down_to_the_hour_close():
    chain = chain_of([raw(85800, 0.7, 0.72)], fetched_ms=CLOSE_MS - 600_000)
    assert chain.remaining_seconds == pytest.approx(600)


# --- what counts as a real market ------------------------------------------


def test_rungs_pinned_at_the_extremes_are_not_quotable():
    """Most of the 188 rungs are the exchange saying the outcome is decided."""
    chain = chain_of([
        raw(80000, 0.99, 1.00),
        raw(85900, 0.55, 0.57),
        raw(95000, 0.00, 0.01),
    ])
    assert [s.strike for s in chain.quotable()] == [85900]


def test_near_selects_rungs_within_a_dollar_window_of_spot():
    chain = chain_of([raw(s, 0.5, 0.52) for s in (84000, 85800, 85900, 86000, 88000)])
    assert [s.strike for s in chain.near(85900, 500)] == [85800, 85900, 86000]


# --- integrity -------------------------------------------------------------


def test_a_well_formed_ladder_passes():
    chain = chain_of([raw(85800, 0.70, 0.72), raw(85900, 0.55, 0.57), raw(86000, 0.40, 0.42)])
    result = check_integrity(chain, spot=85900, now_ms=chain.fetched_ms)
    assert result.ok
    assert result.arbitrage_pairs == 0
    assert result.reasons == ()


def test_a_higher_rung_bid_above_a_lower_rung_ask_is_flagged_as_arbitrage():
    """Buy the low rung, sell the high one: a credit that can never lose.

    The low strike pays whenever the high strike does, so this is free money,
    so it is bad data.
    """
    chain = chain_of([raw(85800, 0.50, 0.52), raw(85900, 0.60, 0.62)])
    result = check_integrity(chain, spot=85850, now_ms=chain.fetched_ms)
    assert not result.ok
    assert result.arbitrage_pairs == 1
    assert result.worst_arbitrage == pytest.approx(0.08)
    assert any("arbitrage" in r for r in result.reasons)


def test_dead_rungs_do_not_manufacture_arbitrage():
    """A 0.99/1.00 rung beside a 0.00/0.01 rung is rounding, not a signal."""
    chain = chain_of([
        raw(80000, 0.99, 1.00), raw(85900, 0.55, 0.57), raw(95000, 0.00, 0.01),
    ])
    assert check_integrity(chain, spot=85900, now_ms=chain.fetched_ms).ok


def test_a_crossed_book_fails_integrity():
    chain = chain_of([raw(85900, 0.60, 0.55)])
    result = check_integrity(chain, spot=85900, now_ms=chain.fetched_ms)
    assert not result.ok
    assert result.crossed_books == 1


def test_a_ladder_whose_quotes_have_not_moved_fails_integrity():
    chain = chain_of([raw(85900, 0.55, 0.57)])
    result = check_integrity(
        chain, spot=85900, now_ms=chain.fetched_ms, quotes_age_s=300, max_stale_s=180
    )
    assert not result.ok
    assert result.stale_seconds == pytest.approx(300)
    assert any("no quote moved" in r for r in result.reasons)


def test_an_unknown_quote_age_is_not_treated_as_stale():
    """The first poll of an hour has nothing to compare against."""
    chain = chain_of([raw(85900, 0.55, 0.57)])
    result = check_integrity(
        chain, spot=85900, now_ms=chain.fetched_ms, quotes_age_s=None
    )
    assert result.ok


def test_the_record_stamp_is_never_used_as_a_quote_clock():
    """Measured: all 188 rungs share a stamp from chain open and it never moves.

    Reading freshness off it marked every live snapshot stale. The stamp is
    still archived; it just must not gate anything.
    """
    stale_stamp = raw(85900, 0.55, 0.57, updated_time="2026-09-21T15:00:01Z")
    chain = chain_of([stale_stamp], fetched_ms=OPEN_MS + 3_000_000)
    assert check_integrity(chain, spot=85900, now_ms=chain.fetched_ms).ok


def test_a_ladder_with_no_quotable_rung_is_not_tradeable():
    chain = chain_of([raw(80000, 0.99, 1.00), raw(95000, 0.00, 0.01)])
    result = check_integrity(chain, spot=85900, now_ms=chain.fetched_ms)
    assert not result.ok
    assert result.quotable_count == 0


def test_spot_far_outside_the_quotable_band_is_reported():
    chain = chain_of([raw(85900, 0.55, 0.57)])
    result = check_integrity(chain, spot=120_000, now_ms=chain.fetched_ms)
    assert any("outside" in r for r in result.reasons)


def test_non_monotonic_mids_are_counted_but_do_not_fail_the_chain():
    """Wide spreads on thin rungs do this routinely; it is not bad data."""
    chain = chain_of([raw(85800, 0.50, 0.70), raw(85900, 0.55, 0.69)])
    result = check_integrity(chain, spot=85850, now_ms=chain.fetched_ms)
    assert result.non_monotonic == 1
    assert result.ok


# --- archive ---------------------------------------------------------------


@pytest.fixture
def store(tmp_path):
    s = HourlyStore(str(tmp_path / "hourly.db"))
    yield s
    s.close()


def snapshot_into(store, chain, spot=85900.0, vol=3.0, archived=None):
    integrity = check_integrity(chain, spot, chain.fetched_ms)
    return store.record_snapshot(
        chain, integrity, spot=spot, momentum_5m_bps=1.0, volatility_5m_bps=vol,
        archived=archived if archived is not None else chain.strikes,
        session_id="test-session",
    )


def test_every_archived_rung_becomes_a_row(store):
    chain = chain_of([raw(85800, 0.7, 0.72), raw(85900, 0.55, 0.57)])
    assert snapshot_into(store, chain) == 2
    assert store.coverage()["strike_rows"] == 2
    assert store.coverage()["chains"] == 1


def test_trimming_is_never_silent(store):
    """n_strikes vs n_archived must show what the window left out."""
    chain = chain_of([raw(s, 0.5, 0.52) for s in (80000, 85800, 85900, 95000)])
    snapshot_into(store, chain, archived=chain.near(85900, 500))
    row = store._db.execute(
        "SELECT n_strikes, n_archived FROM hourly_chains"
    ).fetchone()
    assert row["n_strikes"] == 4
    assert row["n_archived"] == 2


def test_distance_and_normalised_distance_are_derived_per_rung(store):
    chain = chain_of([raw(86000, 0.4, 0.42)])
    snapshot_into(store, chain, spot=85_900.0, vol=5.0)
    row = store._db.execute(
        "SELECT distance_bps, normalized_distance FROM hourly_strikes"
    ).fetchone()
    # (86000 - 85900) / 85900 * 10000
    assert row["distance_bps"] == pytest.approx(11.64, abs=0.01)
    assert row["normalized_distance"] == pytest.approx(11.64 / 5.0, abs=0.01)


def test_every_row_is_stamped_shadow_so_the_boundary_survives(store):
    """Live rows must never be indistinguishable from shadow rows later."""
    snapshot_into(store, chain_of([raw(85900, 0.55, 0.57)]))
    assert store._db.execute("SELECT mode FROM hourly_chains").fetchone()["mode"] == "shadow"


def test_a_repeated_snapshot_does_not_raise_or_double_count(store):
    chain = chain_of([raw(85900, 0.55, 0.57)])
    snapshot_into(store, chain)
    snapshot_into(store, chain)
    assert store.coverage()["snapshots"] == 1
    assert store.coverage()["strike_rows"] == 1


def test_an_unclean_chain_is_still_archived(store):
    """A chain that disagrees with itself is evidence, not garbage."""
    chain = chain_of([raw(85800, 0.50, 0.52), raw(85900, 0.60, 0.62)])
    snapshot_into(store, chain)
    cover = store.coverage()
    assert cover["snapshots"] == 1
    assert cover["clean_snapshots"] == 0


def test_settlement_closes_a_chain_out(store):
    chain = chain_of([raw(85900, 0.55, 0.57)])
    snapshot_into(store, chain)
    later = chain.close_ms + 300_000
    assert [c for c, _ in store.unsettled_chains(later)] == ["KXBTCD-26SEP2112"]
    store.record_settlement("KXBTCD-26SEP2112", chain.close_ms, 85_812.34, later)
    assert store.unsettled_chains(later) == []
    assert store.coverage()["settled_chains"] == 1


def test_a_chain_whose_hour_has_not_ended_is_not_awaiting_settlement(store):
    chain = chain_of([raw(85900, 0.55, 0.57)])
    snapshot_into(store, chain)
    assert store.unsettled_chains(chain.fetched_ms) == []


def test_one_settled_price_decides_every_rung(store):
    """188 outcomes from one number - no per-contract result fetch is needed."""
    strikes = [85_800, 85_900, 86_000]
    chain = chain_of([raw(s, 0.5, 0.52) for s in strikes])
    snapshot_into(store, chain)
    store.record_settlement("KXBTCD-26SEP2112", chain.close_ms, 85_912.50, chain.close_ms)
    value = store._db.execute(
        "SELECT expiration_value FROM hourly_settlements"
    ).fetchone()["expiration_value"]
    assert [s < value for s in strikes] == [True, True, False]


# --- staleness is measured across polls, so test it across polls -----------


@pytest.fixture
def shadow(tmp_path):
    from btc15_signal.config import Settings
    from btc15_signal.hourly_shadow import HourlyShadow

    settings = Settings()
    object.__setattr__(settings, "hourly_database_path", str(tmp_path / "h.db"))
    s = HourlyShadow(settings)
    yield s
    s._store.close()


def test_the_first_poll_of_a_chain_has_no_quote_age(shadow):
    chain = chain_of([raw(85900, 0.55, 0.57)])
    assert shadow._quote_age(chain, OPEN_MS) is None


def test_quotes_that_do_not_move_accumulate_age(shadow):
    chain = chain_of([raw(85900, 0.55, 0.57)])
    shadow._quote_age(chain, OPEN_MS)
    shadow._last_chain = chain.chain_id
    assert shadow._quote_age(chain, OPEN_MS + 120_000) == pytest.approx(120)


def test_a_moved_quote_resets_the_age(shadow):
    shadow._quote_age(chain_of([raw(85900, 0.55, 0.57)]), OPEN_MS)
    shadow._last_chain = "KXBTCD-26SEP2112"
    moved = chain_of([raw(85900, 0.56, 0.58)])
    assert shadow._quote_age(moved, OPEN_MS + 120_000) == pytest.approx(0)


def test_a_new_chain_starts_the_measurement_over(shadow):
    """Across an hour boundary, 'nothing changed yet' proves nothing."""
    shadow._quote_age(chain_of([raw(85900, 0.55, 0.57)]), OPEN_MS)
    shadow._last_chain = "KXBTCD-26SEP2112"
    next_hour = chain_of([
        raw(85900, 0.55, 0.57, event_ticker="KXBTCD-26SEP2113",
            open_time="2026-09-21T16:00:00Z", close_time="2026-09-21T17:00:00Z")
    ])
    assert shadow._quote_age(next_hour, OPEN_MS + 3_600_000) is None
