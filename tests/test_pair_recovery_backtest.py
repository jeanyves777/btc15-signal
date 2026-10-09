"""The paired BTC/GOLD recovery backtest, against hand-worked windows."""

import csv
from datetime import datetime

import pytest

from btc15_signal import pair_recovery_backtest as prb
from btc15_signal.pair_recovery_backtest import (
    NY,
    WINDOW_MS,
    BtcTrade,
    Outcome,
    Quote,
    btc_direction,
    run_backtest,
    summarise,
)

EVAL = prb.EVAL_BEFORE_CLOSE_MS


def close_at(day: str, hh: int, mm: int) -> int:
    """Close time of the window that OPENS at hh:mm New York."""
    y, m, d = map(int, day.split("-"))
    open_ms = int(datetime(y, m, d, hh, mm, tzinfo=NY).timestamp() * 1000)
    return open_ms + WINDOW_MS


def trade(close_ms, pnl, cost=5.0, ticker=None):
    return BtcTrade(ticker=ticker or f"KXBTC15M-{close_ms}", close_ms=close_ms, side="yes",
                    entry_ms=close_ms - 10 * 60_000, contracts=10, cost=cost, net_pnl=pnl,
                    outcome_ms=close_ms, paid_ms=close_ms + 60_000)


def quote(market, close_ms, yes_bid, yes_ask, age_ms=5_000, bid_size=None, ask_size=None):
    return Quote(market=market, ticker=f"KX{market}15M-{close_ms}", close_ms=close_ms,
                 captured_ms=close_ms - EVAL - age_ms, yes_bid=yes_bid, yes_ask=yes_ask,
                 yes_bid_size=bid_size, yes_ask_size=ask_size)


def outcome(market, close_ms, result):
    return Outcome(market=market, ticker=f"KX{market}15M-{close_ms}", close_ms=close_ms,
                   result=result, settled_ms=close_ms + 120_000)


def rows_by_close(res):
    return {r.close_ms: r for r in res["rows"]}


# ---------------------------------------------------------------- rotation

def test_btc_is_up_on_the_first_new_york_window_and_alternates():
    assert btc_direction(close_at("2026-10-01", 0, 0)) == "UP"
    assert btc_direction(close_at("2026-10-01", 0, 15)) == "DOWN"
    assert btc_direction(close_at("2026-10-01", 0, 30)) == "UP"
    assert btc_direction(close_at("2026-10-01", 23, 45)) == "DOWN"
    # The next New York day re-anchors on UP.
    assert btc_direction(close_at("2026-10-02", 0, 0)) == "UP"


def test_rotation_is_anchored_across_a_dst_change():
    # 2026-11-01 is the fall-back day in New York: 25 hours, 100 windows.
    assert btc_direction(close_at("2026-11-01", 0, 0)) == "UP"
    assert btc_direction(close_at("2026-11-02", 0, 0)) == "UP"


# ---------------------------------------------------------------- one full cycle

def _one_cycle():
    w1 = close_at("2026-10-01", 10, 0)    # loss here
    w2 = w1 + WINDOW_MS                   # recovery entry (window opening 10:15 -> DOWN)
    w3 = w2 + WINDOW_MS                   # back to normal
    trades = [trade(w1, -2.00), trade(w2, +9.0), trade(w3, +0.40)]
    # w2 BTC is DOWN (10:15 is an odd index), GOLD is UP.
    # BTC DOWN ask = 1 - yes_bid = 1 - 0.80 = 0.20; GOLD UP ask = 0.25.
    quotes = [quote("BTC", w2, 0.80, 0.82), quote("GOLD", w2, 0.20, 0.25)]
    # BTC settled NO -> DOWN wins; GOLD settled NO -> UP loses.
    outs = [outcome("BTC", w2, "no"), outcome("GOLD", w2, "no")]
    return w1, w2, w3, run_backtest(trades, quotes, outs)


def test_a_loss_triggers_one_paired_entry_with_the_hand_worked_arithmetic():
    w1, w2, w3, res = _one_cycle()
    r = rows_by_close(res)[w2]
    assert r.mode == "recovery" and r.action == "paired_entry"
    assert (r.btc.direction, r.gold.direction) == ("DOWN", "UP")
    assert r.btc.ask == pytest.approx(0.20) and r.btc.ask_field == "1-yes_bid"
    assert r.combined_ask == pytest.approx(0.45)
    assert r.btc.contracts == r.gold.contracts == 55          # floor(25 / 0.45)
    assert r.btc.cost == pytest.approx(11.00)                 # 55 x 0.20
    assert r.gold.cost == pytest.approx(13.75)                # 55 x 0.25
    assert r.unused_cash == pytest.approx(0.25)
    assert (r.btc.payout, r.btc.pnl) == (55.0, pytest.approx(44.00))
    assert (r.gold.payout, r.gold.pnl) == (0.0, pytest.approx(-13.75))
    assert r.combined_pnl == pytest.approx(30.25)
    assert r.outcome_class == "btc_only_win"
    assert r.balance_before == pytest.approx(-2.0)
    assert r.balance_after == pytest.approx(28.25)
    assert r.fill_basis == "quote_based_unverified"


def test_recovery_suppresses_the_recorded_trade_and_resumes_the_next_window():
    w1, w2, w3, res = _one_cycle()
    rows = rows_by_close(res)
    assert rows[w2].suppressed_trade.endswith(str(w2))
    assert rows[w3].mode == "normal" and rows[w3].action == "btc_trade"
    s = summarise(res)
    assert s["A"]["net_pnl"] == pytest.approx(-2.0 + 9.0 + 0.40)
    assert s["B"]["normal_net_pnl"] == pytest.approx(-2.0 + 0.40)
    assert s["B"]["recovery_net_pnl"] == pytest.approx(30.25)
    assert s["B"]["net_pnl"] == pytest.approx(-1.6 + 30.25)
    assert s["B"]["suppressed_recorded_pnl"] == pytest.approx(9.0)
    assert s["cycles"]["completed"] == 1 and s["cycles"]["unfinished"] == 0
    assert prb.invariants(res, s) == []


def test_capital_counts_both_legs_once_and_releases_them_at_settlement():
    w1, w2, w3, res = _one_cycle()
    s = summarise(res)
    # Bankroll needed = realised losses so far + stakes still unsettled.
    # B: the trigger already cost 2.00, then 24.75 is staked on the pair.
    assert s["B"]["peak_capital"] == pytest.approx(2.00 + 24.75)
    # A: after w1's -2.00, w2's 5.00 stake is out until it settles.
    assert s["A"]["peak_capital"] == pytest.approx(2.00 + 5.0)


# ---------------------------------------------------------------- rejections

def _recovery_window(quotes_for_w2, outs=None, extra_trades=()):
    w1 = close_at("2026-10-01", 10, 0)
    w2 = w1 + WINDOW_MS
    trades = [trade(w1, -1.0), *extra_trades]
    res = run_backtest(trades, quotes_for_w2(w2), outs(w2) if outs else [],
                       end_ms=w2 + 3 * WINDOW_MS)
    return w2, res


def test_the_ceiling_is_combined_not_per_leg():
    # BTC DOWN ask 0.30 + GOLD UP ask 0.21 = 0.51 > 0.50 -> skip.
    w2, res = _recovery_window(lambda w: [quote("BTC", w, 0.70, 0.72),
                                          quote("GOLD", w, 0.19, 0.21)])
    r = rows_by_close(res)[w2]
    assert r.action == "skip" and r.reasons == ["combined_ask_above_ceiling"]
    # 0.49 + 0.01 = 0.50 passes: one leg far above 25c is fine.
    w2, res = _recovery_window(lambda w: [quote("BTC", w, 0.51, 0.53),
                                          quote("GOLD", w, 0.00, 0.01)])
    r = rows_by_close(res)[w2]
    assert r.action == "paired_entry" and r.btc.contracts == 50


def test_stale_missing_and_future_quotes_are_rejected():
    w2, res = _recovery_window(lambda w: [quote("BTC", w, 0.80, 0.82, age_ms=31_000),
                                          quote("GOLD", w, 0.20, 0.25)])
    assert rows_by_close(res)[w2].reasons == ["btc_quote_stale"]

    w2, res = _recovery_window(lambda w: [quote("BTC", w, 0.80, 0.82)])
    assert rows_by_close(res)[w2].reasons == ["gold_quote_missing"]

    # A quote captured AFTER the decision instant must not be used.
    w2, res = _recovery_window(lambda w: [quote("BTC", w, 0.80, 0.82),
                                          quote("GOLD", w, 0.20, 0.25, age_ms=-1_000)])
    assert rows_by_close(res)[w2].reasons == ["gold_quote_missing"]


def test_a_missing_ask_is_never_replaced_by_anything_else():
    w2, res = _recovery_window(lambda w: [quote("BTC", w, None, 0.82),
                                          quote("GOLD", w, 0.20, 0.25)])
    assert rows_by_close(res)[w2].reasons == ["btc_ask_unavailable"]
    s = summarise(res)
    assert s["B"]["rejections_primary"]["btc_ask_unavailable"] == 1
    assert s["B"]["rejections_all_conditions"]["btc_ask_unavailable"] == 1


def test_skipped_windows_leave_the_balance_and_rotation_untouched():
    w1 = close_at("2026-10-01", 10, 0)
    w2, w3 = w1 + WINDOW_MS, w1 + 2 * WINDOW_MS
    quotes = [quote("BTC", w3, 0.20, 0.22), quote("GOLD", w3, 0.75, 0.78)]  # w2 has none
    outs = [outcome("BTC", w3, "yes"), outcome("GOLD", w3, "no")]
    res = run_backtest([trade(w1, -1.0)], quotes, outs)
    rows = rows_by_close(res)
    assert rows[w2].action == "skip"
    r = rows[w3]
    # w3 opens 10:30 -> even index -> BTC UP, GOLD DOWN regardless of the skip.
    assert (r.btc.direction, r.gold.direction) == ("UP", "DOWN")
    assert r.btc.ask == pytest.approx(0.22) and r.gold.ask == pytest.approx(0.25)
    assert r.balance_before == pytest.approx(-1.0)
    assert r.outcome_class == "both_win"


# ---------------------------------------------------------------- persistence

def test_debt_carries_across_midnight_and_needs_a_strict_surplus():
    w1 = close_at("2026-10-01", 23, 30)
    w2 = w1 + WINDOW_MS        # opens 23:45 -> DOWN
    w3 = w2 + WINDOW_MS        # opens 00:00 next day -> UP (re-anchored)
    w4 = w3 + WINDOW_MS
    quotes = [quote("BTC", w2, 0.80, 0.82), quote("GOLD", w2, 0.20, 0.25),
              quote("BTC", w3, 0.20, 0.25), quote("GOLD", w3, 0.75, 0.80)]
    outs = [outcome("BTC", w2, "yes"), outcome("GOLD", w2, "no"),   # both lose
            outcome("BTC", w3, "yes"), outcome("GOLD", w3, "no")]   # both win
    res = run_backtest([trade(w1, -3.0), trade(w4, 1.0)], quotes, outs)
    rows = rows_by_close(res)
    assert rows[w2].outcome_class == "both_loss"
    assert rows[w2].balance_after == pytest.approx(-3.0 - 24.75)
    assert rows[w3].btc.direction == "UP"
    assert rows[w3].balance_before == pytest.approx(-27.75)
    # UP yes_ask 0.25 + GOLD DOWN 1 - 0.75 = 0.50 -> 50 each; both win:
    # payout 100 - cost 25 = +75.00.
    assert rows[w3].combined_ask == pytest.approx(0.50)
    assert rows[w3].combined_pnl == pytest.approx(75.0)
    assert rows[w4].mode == "normal"


def test_a_balance_of_exactly_zero_does_not_exit():
    w1 = close_at("2026-10-01", 10, 0)
    w2 = w1 + WINDOW_MS
    # 50 contracts at 0.25 + 0.25: BTC-only win pays 50, cost 25 -> +25.00.
    quotes = [quote("BTC", w2, 0.75, 0.77), quote("GOLD", w2, 0.20, 0.25)]
    outs = [outcome("BTC", w2, "no"), outcome("GOLD", w2, "no")]
    res = run_backtest([trade(w1, -25.0)], quotes, outs, end_ms=w2 + 2 * WINDOW_MS)
    c = res["cycles"][0]
    assert c.balance == pytest.approx(0.0) and not c.exited
    assert rows_by_close(res)[w2 + WINDOW_MS].mode == "recovery"


# ---------------------------------------------------------------- depth

def test_thin_depth_is_reported_as_unmatched_exposure():
    w2, res = _recovery_window(
        lambda w: [quote("BTC", w, 0.80, 0.82, bid_size=100, ask_size=5),
                   quote("GOLD", w, 0.20, 0.25, bid_size=5, ask_size=30)],
        outs=lambda w: [outcome("BTC", w, "no"), outcome("GOLD", w, "yes")])
    r = rows_by_close(res)[w2]
    # Wanted 55 each; BTC DOWN depth is yes_bid_size=100, GOLD UP depth is 30.
    assert r.fill_basis == "partial_depth"
    assert (r.btc.contracts, r.gold.contracts, r.matched) == (55, 30, 30)
    assert (r.unmatched_market, r.unmatched_contracts) == ("BTC", 25)
    s = summarise(res)
    assert s["B"]["unmatched_contracts"] == 25


def test_full_depth_is_labelled_verified():
    w2, res = _recovery_window(
        lambda w: [quote("BTC", w, 0.80, 0.82, bid_size=500, ask_size=500),
                   quote("GOLD", w, 0.20, 0.25, bid_size=500, ask_size=500)],
        outs=lambda w: [outcome("BTC", w, "no"), outcome("GOLD", w, "yes")])
    r = rows_by_close(res)[w2]
    assert r.fill_basis == "depth_verified" and r.outcome_class == "both_win"


# ---------------------------------------------------------------- look-ahead

def test_entry_decisions_do_not_depend_on_outcomes():
    w1 = close_at("2026-10-01", 10, 0)
    ws = [w1 + k * WINDOW_MS for k in range(1, 6)]
    def book(w):   # an uncrossed book whose selected sides sum to 0.50 either way
        if btc_direction(w) == "UP":
            return quote("BTC", w, 0.24, 0.26), quote("GOLD", w, 0.76, 0.78)
        return quote("BTC", w, 0.74, 0.76), quote("GOLD", w, 0.22, 0.24)
    quotes = [q for w in ws for q in book(w)]
    lose = [o for w in ws for o in (outcome("BTC", w, "yes"), outcome("GOLD", w, "no"))]
    res = run_backtest([trade(w1, -500.0)], quotes, lose)   # never recovers
    decisions = [(r.close_ms, r.btc.direction, r.btc.ask, r.btc.contracts)
                 for r in res["rows"] if r.btc]
    flipped = [Outcome(o.market, o.ticker, o.close_ms, "no" if o.result == "yes" else "yes",
                       o.settled_ms) for o in lose]
    res2 = run_backtest([trade(w1, -500.0)], quotes, flipped)
    decisions2 = [(r.close_ms, r.btc.direction, r.btc.ask, r.btc.contracts)
                  for r in res2["rows"] if r.btc]
    assert decisions == decisions2 and len(decisions) == 5


def test_an_unsettled_leg_is_unresolved_not_guessed():
    w2, res = _recovery_window(lambda w: [quote("BTC", w, 0.80, 0.82),
                                          quote("GOLD", w, 0.20, 0.25)],
                               outs=lambda w: [outcome("BTC", w, "no")])
    r = rows_by_close(res)[w2]
    assert r.outcome_class == "unresolved" and r.combined_pnl is None
    assert res["cycles"][0].balance == pytest.approx(-1.0)


# ---------------------------------------------------------------- end to end

def test_csv_round_trip_writes_report_and_ledger(tmp_path):
    w1, w2, w3, res = _one_cycle()
    src = tmp_path / "in"
    src.mkdir()
    with open(src / "btc_trades.csv", "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["ticker", "close_ms", "side", "entry_ms", "contracts", "cost", "net_pnl",
                    "outcome_ms", "paid_ms"])
        for t in res["trades"]:
            w.writerow([t.ticker, t.close_ms, t.side, t.entry_ms, t.contracts, t.cost,
                        t.net_pnl, t.outcome_ms, t.paid_ms])
    with open(src / "quotes.csv", "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["market", "ticker", "close_ms", "captured_ms", "yes_bid", "yes_ask",
                    "yes_bid_size", "yes_ask_size"])
        w.writerow(["BTC", "B", w2, w2 - EVAL - 5000, 0.80, 0.82, "", ""])
        w.writerow(["GOLD", "G", w2, w2 - EVAL - 5000, 0.20, 0.25, "", ""])
    with open(src / "outcomes.csv", "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["market", "ticker", "close_ms", "result", "settled_ms"])
        w.writerow(["BTC", "B", w2, "no", ""])
        w.writerow(["GOLD", "G", w2, "no", ""])
    trades, quotes, outs = prb.load_inputs(src)
    out = prb.write_outputs(run_backtest(trades, quotes, outs), tmp_path / "out", "synthetic")
    assert out["invariant_failures"] == []
    report = (tmp_path / "out" / "report.md").read_text()
    assert "btc_only_win" in report and "ALL CHECKS PASSED" in report
    with open(tmp_path / "out" / "ledger.csv") as fh:
        ledger = list(csv.DictReader(fh))
    entry = next(x for x in ledger if x["action"] == "paired_entry")
    assert float(entry["combined_pnl"]) == pytest.approx(30.25)
