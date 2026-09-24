from btc15_signal.snapshot import MarketSnapshot
from btc15_signal.model import predict
from btc15_signal.store import Store, wilson_lower
from btc15_signal.strategy import ReversionRule


def test_direction_and_probability_increase_with_supporting_flow():
    weak = MarketSnapshot(100.01, 100, 0, 0, 0, 5, 0, 0.1)
    strong = MarketSnapshot(100.03, 100, 0.7, 0.7, 8, 5, 1, 0.1)
    assert predict(weak).side == "UP"
    assert predict(strong).raw_probability > predict(weak).raw_probability


def test_down_direction():
    snapshot = MarketSnapshot(99, 100, -0.6, -0.5, -5, 4, -1, 0.1)
    assert predict(snapshot).side == "DOWN"


def test_wilson_bound_is_conservative():
    assert wilson_lower(90, 100) < 0.90
    assert wilson_lower(0, 0) == 0


def test_prediction_is_settled_against_exact_window_close(tmp_path):
    store = Store(str(tmp_path / "test.db"))
    store.record((0, 1, 100.0, 101.0, "UP", 8, 0.85, "KXBTC15M-TEST", 0.90, 1))
    assert store.pending_settlements(900_000) == [
        (0, "UP", "KXBTC15M-TEST", 0.90, 1, 100.0)
    ]
    store.settle(0, "UP", "no")
    calibration = store.calibration(8)
    assert calibration.samples == 1
    assert calibration.wins == 0
    # settled rows must not be offered for settlement twice
    assert store.pending_settlements(900_000) == []


def test_scoreboard_scores_every_settled_signal_in_dollars(tmp_path):
    store = Store(str(tmp_path / "s.db"))
    # win at 0.80 -> +0.25 per $1; loss at 0.80 -> -1.00 per $1
    store.record((0, 1, 100.0, 101.0, "UP", 8, 0.85, "A", 0.80, 1))
    store.record((900_000, 1, 100.0, 101.0, "UP", 8, 0.85, "B", 0.80, 0))
    store.settle(0, "UP", "yes")
    store.settle(900_000, "UP", "no")
    # One contract at 0.80: a win pays +0.20 gross, a loss -0.80, each charged
    # floor(1.12c) = 1c, matching the exchange ticket rather than the published
    # ceiling.
    settled, wins, pnl = store.scoreboard(contracts=1)
    assert (settled, wins) == (2, 1)
    assert round(pnl, 2) == round(0.20 - 0.01 - 0.80 - 0.01, 2)

    # Cash sizing is a different amount of money for the same ROI: $10 spent
    # at 80c buys 12.5 contracts, so the loss is the whole $10.
    # 12.5 contracts: raw fee 14.0c floors to 14c, same as the ceiling here
    # because the amount lands on a whole cent.
    _, _, cash_pnl = store.scoreboard(cash=10.0)
    assert round(cash_pnl, 2) == round(2.50 - 0.14 - 10.00 - 0.14, 2)

    # restricting to rule-qualified rows keeps only the first
    assert store.scoreboard(contracts=1, qualified_only=True)[:2] == (1, 1)


def test_reversion_requires_spike_rejection_and_cheap_opposite_contract():
    snapshot = MarketSnapshot(100.10, 100, 0, 0, -2, 4, 0, 0.1, window_high=100.20, window_low=100)
    setup, reason = ReversionRule().matches(snapshot, 11, 0.33)
    assert reason == ""
    assert setup is not None
    assert setup.side == "DOWN"
    assert round(setup.spike_bps, 1) == 20.0
    assert round(setup.rejection_bps, 1) == 10.0


def test_reversion_rejects_price_outside_entry_band():
    snapshot = MarketSnapshot(100.10, 100, 0, 0, -2, 4, 0, 0.1, window_high=100.20, window_low=100)
    setup, reason = ReversionRule().matches(snapshot, 11, 0.42)
    assert setup is None
    assert "30-35 cent" in reason
