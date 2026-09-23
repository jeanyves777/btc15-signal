"""Realised P&L is the exchange's number, and `revenue` alone is not it.

On 2026-09-22 Telegram reported **+1.06 on an account that was down -1.62** and
counted 47 of 88 settled markets. The figure was rebuilt locally from
`trade_proposals`, which gets it wrong four separate ways: it priced rows at
`entry_limit` when no fill was stored, modelled the fee instead of reading the
charged one, settled by our own `predictions.won`, and excluded any proposal
still sitting at `pending`.

The trap in reading Kalshi's own fields is `revenue`. Closing a position early
is booked as buying the OPPOSITE side, and the offsetting pair is netted at $1
immediately - outside `revenue`, which then reads 0 on a market that WON.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from btc15_signal.execution import KalshiExecutionClient  # noqa: E402

pnl = KalshiExecutionClient.settlement_pnl


def todays_ticker(hhmm: str) -> str:
    """A ticker on the CURRENT New York accounting day.

    `realised_record` reports TODAY, and the day comes from the ticker via
    `market_open_ms`. Hard-coded `26SEP22` tickers therefore made these tests
    pass only on 2026-09-22 and fail silently at the next midnight - which is
    exactly what happened. The date is derived so the test asserts the
    behaviour rather than the calendar.
    """
    import datetime as dt

    from btc15_signal.capital import ny_day

    today = ny_day(int(dt.datetime.now(dt.UTC).timestamp() * 1000))
    stamp = dt.datetime.strptime(today, "%Y-%m-%d").strftime("%y%b%d").upper()
    return f"KXBTC15M-{stamp}{hhmm}"

# KXBTC15M-26SEP220615-15, exactly as the API returned it. Bought 2 NO for
# $1.60, closed by buying 2 YES for $0.012 (i.e. sold the NO at 0.994).
# `market_result` is "no" and `revenue` is 0, because nothing survived to
# settle - the pair was netted at $1 each when the closing trade filled.
CLOSED_EARLY = {
    "ticker": todays_ticker("0615-15"), "market_result": "no",
    "yes_count_fp": "2.00", "yes_total_cost_dollars": "0.012000",
    "no_count_fp": "2.00", "no_total_cost_dollars": "1.600000",
    "revenue": 0, "value": 0, "fee_cost": "0.023300",
}
# KXBTC15M-26SEP220415-15: 2 YES for $1.78, held to settlement, result "yes".
HELD_WINNER = {
    "ticker": todays_ticker("0415-15"), "market_result": "yes",
    "yes_count_fp": "2.00", "yes_total_cost_dollars": "1.780000",
    "no_count_fp": "0.00", "no_total_cost_dollars": "0.000000",
    "revenue": 200, "value": 100, "fee_cost": "0.013800",
}
# A held loser: 1 YES for $0.79, result "no", nothing paid back.
HELD_LOSER = {
    "ticker": todays_ticker("0630-30"), "market_result": "no",
    "yes_count_fp": "1.00", "yes_total_cost_dollars": "0.790000",
    "no_count_fp": "0.00", "no_total_cost_dollars": "0.000000",
    "revenue": 0, "value": 0, "fee_cost": "0.011700",
}


def test_a_position_closed_early_is_a_profit_not_a_total_loss():
    """The whole bug in one row: `revenue` is 0 and the trade made money."""
    assert CLOSED_EARLY["revenue"] == 0
    # 2 netted pairs pay $2.00; $1.612 went out in costs; $0.0233 in fees.
    assert pnl(CLOSED_EARLY) == __import__("pytest").approx(0.3647, abs=5e-4)
    naive = CLOSED_EARLY["revenue"] / 100.0 - 1.612 - 0.0233
    assert naive < -1.6, "scoring by `revenue` alone books a $1.63 loss"


def test_a_held_winner_uses_revenue_because_nothing_was_netted():
    assert pnl(HELD_WINNER) == __import__("pytest").approx(0.2062, abs=5e-4)


def test_a_held_loser_loses_the_stake_and_the_fee():
    assert pnl(HELD_LOSER) == __import__("pytest").approx(-0.8017, abs=5e-4)


def test_the_mirror_reports_exactly_what_the_api_computes(tmp_path):
    """`exchange_record` must not drift from `settlement_pnl`."""
    from btc15_signal.store import Store

    store = Store(str(tmp_path / "t.db"))
    rows = [CLOSED_EARLY, HELD_WINNER, HELD_LOSER]
    assert store.record_settlements(rows, 1_790_000_000_000) == 3
    markets, winners, dollars = store.exchange_record()
    assert markets == 3
    assert winners == 2
    assert dollars == __import__("pytest").approx(sum(pnl(r) for r in rows), abs=1e-9)


def test_resyncing_the_same_settlement_does_not_double_count(tmp_path):
    """The service syncs every minute; the history must not compound."""
    from btc15_signal.store import Store

    store = Store(str(tmp_path / "t.db"))
    for _ in range(4):
        store.record_settlements([CLOSED_EARLY, HELD_WINNER], 1_790_000_000_000)
    markets, _winners, dollars = store.exchange_record()
    assert markets == 2
    assert dollars == __import__("pytest").approx(
        pnl(CLOSED_EARLY) + pnl(HELD_WINNER), abs=1e-9
    )


def test_realised_record_prefers_the_exchange_over_the_local_rebuild(tmp_path):
    from btc15_signal.store import Store

    store = Store(str(tmp_path / "t.db"))
    store.record_settlements([HELD_LOSER], 1_790_000_000_000)
    trades, _wins, dollars = store.realised_record()
    assert trades == 1
    assert dollars == __import__("pytest").approx(pnl(HELD_LOSER), abs=1e-9)


# --------------------------------------------------- the market's own clock

def test_the_ticker_carries_the_market_time_not_the_settlement_time():
    """Kalshi settles in batches hours late, so `settled_time` files a trade
    under the wrong day. KXBTC15M-26SEP220445-45 settled at 08:45 UTC - four
    hours after its own window - and grouping by settlement read 2026-09-21 as
    -3.3256 where the market's own clock reads +0.0022."""
    from datetime import UTC, datetime

    got = KalshiExecutionClient.market_open_ms("KXBTC15M-26SEP220445-45")
    assert got == int(datetime(2026, 9, 22, 4, 45, tzinfo=UTC).timestamp() * 1000)


def test_the_hourly_ladder_ticker_parses_too():
    from datetime import UTC, datetime

    got = KalshiExecutionClient.market_open_ms("KXBTCD-26SEP2207-T80099.99")
    assert got == int(datetime(2026, 9, 22, 7, 0, tzinfo=UTC).timestamp() * 1000)


def test_an_unparseable_ticker_returns_none_rather_than_a_wrong_day():
    for ticker in ("", "NOTATICKER", "KX-99XXX9999-1", "KXAAAGASD-26SEP21-4.4750"):
        got = KalshiExecutionClient.market_open_ms(ticker)
        assert got is None or got > 0


def test_the_daily_window_uses_market_time(tmp_path):
    """The loss floor must see today's markets, not today's settlements."""
    from datetime import UTC, datetime

    from btc15_signal.store import Store

    store = Store(str(tmp_path / "t.db"))
    day = datetime(2026, 9, 22, tzinfo=UTC).timestamp() * 1000
    # A market from 2026-09-22 that Kalshi stamps as settled the NEXT day.
    row = dict(HELD_LOSER, ticker="KXBTC15M-26SEP220445-45",
               settled_time="2026-09-23T02:00:00Z")
    store.record_settlements([row], int(day))
    markets, _wins, _dollars = store.exchange_record(int(day))
    assert markets == 1, "a 04:45 market settling next day still belongs to today"
