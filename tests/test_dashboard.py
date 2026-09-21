from btc15_signal.dashboard import load, render, summarise
from btc15_signal.store import Store


def seed(tmp_path, rows):
    """rows: (window_open, side, contract_price, qualified, result)."""
    store = Store(str(tmp_path / "d.db"))
    for open_ms, side, price, qualified, result in rows:
        store.record((open_ms, 1, 100.0, 101.0, side, 9, 0.95, f"T{open_ms}", price, qualified))
        if result is not None:
            store.settle(open_ms, side, result)
    return str(tmp_path / "d.db")


def test_scores_wins_losses_and_dollar_pnl(tmp_path):
    db = seed(
        tmp_path,
        [
            (0, "UP", 0.80, 1, "yes"),  # win:  12 contracts, +2.40 gross
            (900_000, "UP", 0.80, 1, "no"),  # loss: -9.60 gross
        ],
    )
    stats = summarise(load(db, 10.0))
    assert (stats["total"], stats["wins"], stats["losses"]) == (2, 1, 1)
    assert stats["win_rate"] == 0.5
    assert stats["staked"] == 20.0
    # $10 at 80c buys 12 WHOLE contracts, the way the bot sizes an order - not
    # the 12.5 a bare division gives. Fee is charged, not cent-floored:
    # 0.07*12*0.8*0.2 = $0.1344 each way.
    #   win  12*0.20 - 0.1344 = +2.2656
    #   loss 12*-0.80 - 0.1344 = -9.7344
    assert stats["net"] == -7.47


def test_unsettled_signals_are_counted_but_not_scored(tmp_path):
    db = seed(tmp_path, [(0, "UP", 0.90, 1, "yes"), (900_000, "UP", 0.90, 1, None)])
    stats = summarise(load(db, 10.0))
    assert stats["total"] == 1
    assert stats["pending"] == 1


def test_rows_without_a_recorded_price_are_shown_but_not_priced(tmp_path):
    """Signals from before the price column existed must not fake a P&L."""
    db = seed(tmp_path, [(0, "UP", None, 0, "yes")])
    stats = summarise(load(db, 10.0))
    assert stats["total"] == 1 and stats["wins"] == 1
    assert stats["scored"] == 0
    # The gap between the record and the P&L is reported, not left implicit.
    assert stats["unscored"] == 1
    assert stats["net"] == 0.0
    assert stats["avg_price"] is None  # never invent an entry price


def test_missing_pnl_renders_neutral_not_as_a_gain(tmp_path):
    db = seed(tmp_path, [(0, "UP", None, 0, "yes")])
    data = load(db, 10.0)
    html = render(summarise(data), data["signals"])
    assert "class='num dim'>&mdash;" in html
    assert "no priced signals yet" in html


def test_small_samples_carry_the_sample_size_warning(tmp_path):
    db = seed(tmp_path, [(i * 900_000, "UP", 0.90, 1, "yes") for i in range(6)])
    data = load(db, 10.0)
    stats = summarise(data)
    assert stats["win_rate"] == 1.0
    assert stats["win_rate_lower"] < 0.65  # six wins is not proof
    assert stats["trades_needed"] > 3000
    assert "proves nothing" in render(stats, data["signals"])


def test_large_samples_drop_the_warning(tmp_path):
    db = seed(tmp_path, [(i * 900_000, "UP", 0.90, 1, "yes") for i in range(120)])
    data = load(db, 10.0)
    assert "proves nothing" not in render(summarise(data), data["signals"])


def test_splits_by_side_and_by_entry_band(tmp_path):
    db = seed(
        tmp_path,
        [
            (0, "UP", 0.90, 1, "yes"),
            (900_000, "DOWN", 0.90, 1, "no"),
            (1_800_000, "UP", 0.60, 0, "yes"),
        ],
    )
    stats = summarise(load(db, 10.0))
    assert stats["by_side"]["UP"]["n"] == 2
    assert stats["by_side"]["DOWN"]["n"] == 1
    # The label states the range actually filtered (0.85 <= p < 1.00); the old
    # "0.85-0.99" attributed 99c+ trades to a band that excluded them.
    assert stats["by_band"]["0.85-1.00"]["n"] == 2
    assert stats["by_band"]["0.50-0.85"]["n"] == 1
    assert stats["by_qualified"]["rule qualified"]["n"] == 2
    assert stats["by_qualified"]["paper only"]["n"] == 1


def test_render_is_self_contained_and_theme_aware(tmp_path):
    db = seed(tmp_path, [(i * 900_000, "UP", 0.90, 1, "yes") for i in range(3)])
    data = load(db, 10.0)
    html = render(summarise(data), data["signals"])
    assert "<svg" in html
    assert "http://" not in html and "https://" not in html  # no external assets
    assert "prefers-color-scheme: dark" in html
    assert '[data-theme="dark"]' in html
    # outcome is stated in text, never colour alone
    assert ">WIN<" in html


def test_drawdown_tracks_the_worst_peak_to_trough(tmp_path):
    db = seed(
        tmp_path,
        [
            (0, "UP", 0.50, 1, "yes"),  # +10 gross
            (900_000, "UP", 0.50, 1, "no"),  # -10
            (1_800_000, "UP", 0.50, 1, "no"),  # -10
        ],
    )
    stats = summarise(load(db, 10.0))
    assert stats["max_drawdown"] > 19  # two consecutive full losses after a gain
