from btc15_signal.backtest import Candle, build_trials, metrics, select_threshold


def candles_for_window(opened: int, rising: bool = True) -> list[Candle]:
    result = []
    for minute in range(15):
        move = minute if rising else -minute
        price = 100 + move * 0.1
        result.append(Candle(opened + minute * 60_000, price, price, price, price, 10, 6))
    return result


def test_build_trial_uses_tenth_minute_entry_and_fifteenth_minute_close():
    trial = build_trials(candles_for_window(0))[0]
    assert trial.target == 100
    assert trial.entry == 100.9
    assert trial.final == 101.4
    assert trial.won


def test_incomplete_window_is_rejected():
    assert build_trials(candles_for_window(0)[:-1]) == []


def test_threshold_requires_conservative_bound():
    trials = build_trials(
        [candle for n in range(300) for candle in candles_for_window(n * 900_000)]
    )
    assert metrics(trials).win_rate == 1
    assert select_threshold(trials, 0.80, 200) is not None
