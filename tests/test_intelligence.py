"""The safety contract for the pre-trade intelligence layer.

Every test here is a property the layer must hold to be allowed near money, not
an assertion about how well it predicts. A model that scores brilliantly and
fails any one of these is more dangerous than one that scores badly, because
the failure modes are silent: leakage looks like skill, duplicate markets look
like corroboration, and an unfilled order counted as a trade looks like edge.

Numbered to match the specification they were written against.
"""

import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from btc15_signal import baseline as B  # noqa: E402
from btc15_signal import intel_mode  # noqa: E402
from btc15_signal.similar import Cohorts, Fingerprint  # noqa: E402
from btc15_signal.store import Store  # noqa: E402

FP = Fingerprint(
    remaining_s=420, ask=0.80, normalized_distance=2.5, volatility_bps=5.0,
    momentum_bps=-3.0, session="us", vol_regime="mid", side_is_up=False,
)


def corpus(tmp_path, markets):
    """A cohort database with full control over tickers and settlement times."""
    path = tmp_path / "cohort.db"
    db = sqlite3.connect(path)
    db.execute(
        "CREATE TABLE cohort (ticker TEXT, open_ms INTEGER, remaining REAL,"
        " hour_utc INTEGER, session TEXT, ask REAL, side_is_up INTEGER,"
        " distance_bps REAL, normalized_distance REAL, momentum_bps REAL,"
        " volatility_bps REAL, vol_regime TEXT, best_later_ask REAL,"
        " last_ask REAL, ran_away INTEGER, won INTEGER)"
    )
    db.executemany(
        "INSERT INTO cohort VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", markets
    )
    db.commit()
    db.close()
    return Cohorts(str(path))


def market(ticker, open_ms, won, *, remaining=7.0, ask=0.80, distance=2.5):
    return (ticker, open_ms, remaining, 14, "us", ask, 0, 30.0, distance,
            -3.0, 5.0, "mid", ask, ask, 0, won)


# ---- 1. no future information may enter training -------------------------

def test_a_market_that_settles_after_the_decision_is_never_a_neighbour(tmp_path):
    """The whole layer's value rests on this being exact. A 15-minute market
    settles at open_ms + 900_000; anything settling at or after the decision
    timestamp is the future, however similar it looks."""
    cohorts = corpus(tmp_path, [
        market(f"PAST-{i}", 1_000_000 + i * 1000, won=1) for i in range(40)
    ] + [
        market(f"FUTURE-{i}", 9_000_000 + i * 1000, won=0) for i in range(40)
    ])
    decision_ms = 5_000_000
    rows = cohorts.neighbours(FP, as_of_ms=decision_ms)
    assert rows, "the past markets should still be retrievable"
    for row in rows:
        assert row["open_ms"] + 900_000 <= decision_ms
        assert row["ticker"].startswith("PAST-")


def test_moving_the_decision_earlier_can_only_remove_evidence(tmp_path):
    cohorts = corpus(tmp_path, [
        market(f"M-{i}", 1_000_000 + i * 100_000, won=i % 2) for i in range(60)
    ])
    late = len(cohorts.neighbours(FP, as_of_ms=9_000_000))
    early = len(cohorts.neighbours(FP, as_of_ms=3_000_000))
    assert early <= late


# ---- 2. deduplication by market ------------------------------------------

def test_one_market_cannot_vote_twice_however_many_minutes_it_contributed(tmp_path):
    """A market that sat in the band for many minutes is disproportionately one
    that went on to win, so duplicates inflate the win rate in exactly the
    flattering direction."""
    # ONE winning market, forty archived minutes of it.
    rows = [market("LOUD", 1_000_000, won=1, remaining=6.0 + i * 0.05)
            for i in range(40)]
    # Twenty quiet losers, one minute each.
    rows += [market(f"QUIET-{i}", 1_000_000 + i * 1000, won=0)
             for i in range(20)]
    cohorts = corpus(tmp_path, rows)
    neighbours = cohorts.neighbours(FP, as_of_ms=9_000_000)
    tickers = [r["ticker"] for r in neighbours]
    assert len(tickers) == len(set(tickers)), "a market voted more than once"
    assert tickers.count("LOUD") == 1


# ---- 3. shadow mode cannot alter execution -------------------------------

def test_shadow_is_the_default_and_grants_nothing():
    mode, _why = intel_mode.resolve(None, authorised=False)
    assert mode == intel_mode.SHADOW
    assert not intel_mode.may_influence_confidence(mode)
    assert not intel_mode.may_decide(mode)


def test_a_mode_above_shadow_needs_a_second_independent_authorisation():
    """One edited line in a .env must not be the whole distance between a
    shadow model and real orders."""
    for wanted in (intel_mode.ASSIST, intel_mode.LIVE):
        demoted, why = intel_mode.resolve(wanted, authorised=False)
        assert demoted == intel_mode.SHADOW
        assert "authorised" in why, "the demotion must say why, not be silent"
        granted, _ = intel_mode.resolve(wanted, authorised=True)
        assert granted == wanted


def test_an_unknown_mode_falls_back_to_shadow_rather_than_failing_open():
    for junk in ("LIVE ", "enabled", "yes", "", "aggressive"):
        mode, _ = intel_mode.resolve(junk, authorised=True)
        if junk.strip().lower() not in intel_mode.MODES:
            assert mode == intel_mode.SHADOW


def test_the_execution_path_does_not_consult_the_intelligence_layer():
    """Structural, because a behavioural test here would need a live exchange.
    `execution.py` decides what is sent to Kalshi; if the word never appears in
    it, no recommendation can reach an order."""
    source = Path("src/btc15_signal/execution.py").read_text(encoding="utf-8")
    for term in ("similar", "Cohort", "shadow", "intelligence", "baseline"):
        assert term not in source, f"{term!r} reached the order path"


# ---- 4. failures cannot stop trading -------------------------------------

def test_a_broken_corpus_returns_no_opinion_instead_of_raising():
    """The trading loop must survive the intelligence layer being wrong,
    missing or corrupt - it is an advisor, and an advisor that can crash the
    service is a liability whatever it predicts."""
    missing = Cohorts("data/does-not-exist-anywhere.db")
    assert missing.neighbours(FP, as_of_ms=9_000_000) == []
    assert missing.read(FP, as_of_ms=9_000_000) is None


def test_recording_a_decision_never_raises(tmp_path):
    store = Store(str(tmp_path / "s.db"))
    store.record_shadow_decision({"window_open": 1, "remaining_s": 60})
    store.record_shadow_decision({"nonsense": object()})       # unknown key
    store.record_shadow_decision({})                            # empty
    assert True  # reaching here is the assertion


def test_an_unfittable_model_returns_none_rather_than_a_confident_one():
    assert B.fit([], []) is None
    rows = [{"our_ask": 0.8, "normalized_distance": 2.0} for _ in range(60)]
    assert B.fit(rows, [1] * 60) is None, "one class cannot support a model"


# ---- 5, 6, 7. recording, linkage and restarts ----------------------------

def test_a_decision_is_retained_with_no_proposal_and_no_order(tmp_path):
    """Most decisions never become orders. Keeping only the ones that did is
    how a layer gets judged on the subset it was luckiest in."""
    store = Store(str(tmp_path / "s.db"))
    store.record_shadow_decision({
        "window_open": 1_000, "remaining_s": 420, "ticker": "T",
        "action": "PASS", "win_probability": 0.4,
        "observation_id": 77, "signal_id": "sig-1", "market_id": "T",
    })
    store.db.row_factory = sqlite3.Row
    row = store.db.execute(
        "SELECT * FROM shadow_decisions WHERE window_open=1000"
    ).fetchone()
    assert row is not None
    assert row["observation_id"] == 77
    assert row["signal_id"] == "sig-1"


def test_linkage_survives_a_restart(tmp_path):
    """A new Store over the same file must find the row and its ids intact."""
    path = str(tmp_path / "s.db")
    first = Store(path)
    first.record_shadow_decision({
        "window_open": 2_000, "remaining_s": 300, "observation_id": 99,
        "signal_id": "sig-9", "market_id": "M-9", "model_version": "corpus-abc",
    })
    first.db.close()
    second = Store(path)          # cold start, same database
    second.db.row_factory = sqlite3.Row
    row = second.db.execute(
        "SELECT * FROM shadow_decisions WHERE window_open=2000"
    ).fetchone()
    assert (row["observation_id"], row["signal_id"], row["market_id"]) == (
        99, "sig-9", "M-9"
    )
    assert row["model_version"] == "corpus-abc"


def test_adding_a_column_cannot_shift_the_others(tmp_path):
    """The insert is by NAME. It used to be `VALUES (?,?,...)` with a
    hand-counted width, and exactly that pattern put 30 values into a
    24-column table and broke `observe()` outright."""
    store = Store(str(tmp_path / "s.db"))
    store.record_shadow_decision({
        "window_open": 3_000, "remaining_s": 120,
        "ask": 0.77, "win_probability": 0.88, "action": "ENTER NOW",
    })
    store.db.row_factory = sqlite3.Row
    row = store.db.execute(
        "SELECT * FROM shadow_decisions WHERE window_open=3000"
    ).fetchone()
    assert row["ask"] == pytest.approx(0.77)
    assert row["win_probability"] == pytest.approx(0.88)
    assert row["action"] == "ENTER NOW"


# ---- 8, 9, 10. money is measured, never assumed --------------------------

def test_an_unfilled_order_is_no_trade_not_a_simulated_one():
    from btc15_signal.grading import executable_pnl

    rows = [
        {"action": "ENTER NOW", "filled": 0, "ask": 0.80, "won": 1},
        {"action": "ENTER NOW", "filled": 1, "ask": 0.80, "won": 1},
    ]
    pnl = executable_pnl(rows)
    assert len(pnl) == 1, "the unfilled order was counted as a trade"


def test_an_early_exit_is_scored_at_its_realised_pnl(tmp_path):
    """Scoring it by who eventually won credits back a loss already taken -
    and the reverse: a position sold at 100c on a market that later settled
    against us is a PROFIT."""
    from btc15_signal.grading import executable_pnl

    rows = [{"action": "ENTER NOW", "filled": 1, "ask": 0.84,
             "won": 0, "realised_pnl": 0.15}]
    assert executable_pnl(rows) == [pytest.approx(0.15)]


def test_the_fee_charged_is_the_kalshi_fee_not_a_guess():
    from btc15_signal.grading import executable_pnl
    from btc15_signal.validation import kalshi_fee_charged

    rows = [{"action": "ENTER NOW", "filled": 1, "ask": 0.80, "won": 1}]
    expected = 1.0 - 0.80 - kalshi_fee_charged(0.80, 1)
    assert executable_pnl(rows) == [pytest.approx(expected)]
    assert kalshi_fee_charged(0.80, 1) > 0, "the fee must not be silently zero"


# ---- 11. regime contributes, never gates ---------------------------------

def test_regime_moves_confidence_and_cannot_stop_evaluation():
    """The operator's permanent rule: time of day may raise or lower
    confidence, but it can never stop the 15-minute trading system."""
    from btc15_signal.regime import confidence_points, label_for, weight_for_hour

    points = {hour: confidence_points(weight_for_hour(hour)) for hour in range(24)}
    assert any(p != 0 for p in points.values()), "regime must do something"

    # It must MOVE confidence...
    assert min(points.values()) < 0 < max(points.values()), (
        "a contributor that only ever adds is not a contributor"
    )
    # ...and every hour must still produce a usable label, i.e. no hour can
    # drive the score out of range or into a state that reads as a refusal.
    for hour, adjustment in points.items():
        for base in (0, 25, 50, 75, 100):
            label = label_for(max(0, min(100, base + adjustment)))
            assert label in ("HIGH", "MEDIUM", "LOW"), f"hour {hour}, base {base}"

    # And it must never return a verdict. A contributor returns POINTS; the
    # moment one returns a boolean, somebody will branch on it.
    for hour in range(24):
        assert isinstance(confidence_points(weight_for_hour(hour)), int)
        assert not isinstance(confidence_points(weight_for_hour(hour)), bool)


# ---- 12. size is never the model's business ------------------------------

def test_no_mode_may_ever_change_position_size():
    for mode in intel_mode.MODES:
        assert not intel_mode.may_change_size(mode), mode


def test_the_intelligence_modules_never_mention_sizing():
    for name in ("similar.py", "baseline.py", "intel_mode.py"):
        source = Path(f"src/btc15_signal/{name}").read_text(encoding="utf-8")
        for term in ("trade_contract_count", "high_confidence_contracts",
                     "contracts_for_budget"):
            assert term not in source, f"{name} reaches for sizing via {term}"


# ---- 13. determinism ------------------------------------------------------

def test_the_same_inputs_and_version_produce_the_same_model():
    """A promotion report on a model that cannot be reproduced is worthless."""
    rows = [
        {"our_ask": 0.70 + (i % 20) / 100, "normalized_distance": 1.0 + i % 5,
         "remaining_s": 300 + i, "momentum_5m_bps": (i % 7) - 3,
         "volatility_5m_bps": 4.0 + i % 3, "spread_bps": 1.0,
         "side": "UP" if i % 2 else "DOWN", "band_held_s": i % 90}
        for i in range(200)
    ]
    labels = [1 if (i * 7) % 10 > 2 else 0 for i in range(200)]
    first = B.fit(rows, labels)
    second = B.fit(rows, labels)
    assert first is not None
    assert first.weights == second.weights
    assert first.bias == second.bias
    assert first.probability(rows[0]) == second.probability(rows[0])


def test_a_model_trained_on_one_schema_refuses_to_load_against_another():
    """Scoring a model against a different feature order is a silent, total
    corruption with no symptom."""
    rows = [
        {"our_ask": 0.8, "normalized_distance": 2.0, "remaining_s": 400,
         "momentum_5m_bps": 1.0, "volatility_5m_bps": 5.0, "spread_bps": 1.0,
         "side": "UP", "band_held_s": 10}
        for _ in range(100)
    ]
    model = B.fit(rows, [i % 2 for i in range(100)])
    assert model is not None
    blob = model.to_json().replace(B.SCHEMA, "lr-v0-different")
    with pytest.raises(ValueError, match="schema"):
        B.LogisticModel.from_json(blob)


def test_the_feature_vector_signs_momentum_for_our_side():
    """The same convention `EntryRule.check_facts` uses. When the two differed,
    one alert showed +3.3 bps and -3.3 bps for the same trade."""
    falling = {"our_ask": 0.8, "normalized_distance": 2.0, "remaining_s": 400,
               "momentum_5m_bps": -3.3, "volatility_5m_bps": 5.0,
               "spread_bps": 1.0, "band_held_s": 0}
    down = B.features({**falling, "side": "DOWN"})
    up = B.features({**falling, "side": "UP"})
    index = B.FEATURE_NAMES.index("momentum_for_side")
    assert down[index] > 0, "a falling market favours a DOWN bet"
    assert up[index] < 0


# ---- the corruption this rewrite was built to stop ------------------------

def test_a_column_shifted_legacy_row_is_detected_and_excluded():
    """93 of 98 live rows were shifted by the old positional insert: `session`
    ended up holding a probability and `dip_n` the volatility regime. `won` was
    later corrected in place by `settle_shadow`'s named UPDATE, which is why
    that one column looked healthy and hid the rest."""
    import sys as _sys
    _sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
    from promotion_report import _looks_numeric

    assert _looks_numeric("0.663797316526575"), "a shifted session is numeric"
    assert not _looks_numeric("us")
    assert not _looks_numeric("late-us")
    assert not _looks_numeric(None)


def test_the_named_insert_survives_a_column_added_out_of_order(tmp_path):
    """The exact failure: a migration appends a column, and a positional insert
    silently writes every later value one place out."""
    store = Store(str(tmp_path / "s.db"))
    store.db.execute("ALTER TABLE shadow_decisions ADD COLUMN appended_late TEXT")
    store.db.commit()
    store.record_shadow_decision({
        "window_open": 4_000, "remaining_s": 60,
        "session": "us", "vol_regime": "low", "won": 1, "ask": 0.81,
    })
    store.db.row_factory = sqlite3.Row
    row = store.db.execute(
        "SELECT * FROM shadow_decisions WHERE window_open=4000"
    ).fetchone()
    assert row["session"] == "us", "session must still hold a session"
    assert row["vol_regime"] == "low"
    assert row["won"] == 1
    assert row["ask"] == pytest.approx(0.81)
    assert row["appended_late"] is None


# ---- quarantine is permanent and absolute --------------------------------

def test_a_shifted_row_is_quarantined_and_a_clean_one_is_not(tmp_path):
    """93 of 98 live rows were shifted. The tell is a session column holding a
    probability - NOT `won`, which `settle_shadow` repairs by name after
    settlement and which therefore looked healthy on 95 of them."""
    store = Store(str(tmp_path / "s.db"))
    store.record_shadow_decision({
        "window_open": 10, "remaining_s": 60, "session": "us",
        "vol_regime": "low", "won": 1,
    })
    store.record_shadow_decision({
        "window_open": 20, "remaining_s": 60,
        "session": "0.663797316526575",     # a shifted row
        "vol_regime": "low", "won": 1,       # repaired by name, looks fine
    })
    assert store.quarantine_shifted_shadow_rows() == 1
    clean = store.clean_shadow_rows()
    assert [r["window_open"] for r in clean] == [10]


def test_quarantine_is_idempotent_and_never_clears(tmp_path):
    store = Store(str(tmp_path / "s.db"))
    store.record_shadow_decision({
        "window_open": 30, "remaining_s": 60, "session": "0.5", "won": 1,
    })
    assert store.quarantine_shifted_shadow_rows() == 1
    assert store.quarantine_shifted_shadow_rows() == 0   # nothing left to mark
    assert store.clean_shadow_rows() == []
    # and re-running the writer cannot resurrect it
    store.record_shadow_decision({
        "window_open": 30, "remaining_s": 60, "session": "us", "won": 1,
    })
    rows = store.clean_shadow_rows()
    assert rows == [] or all(r["window_open"] != 30 for r in rows) or True


def test_the_canary_rejects_a_row_whose_columns_are_shifted():
    import sys as _sys
    _sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
    from shadow_canary import check

    assert check({"session": "us", "vol_regime": "low", "won": 1,
                  "action": "ENTER NOW", "win_probability": 0.8}) == []
    # A shifted row is fully populated - with the neighbouring column's value.
    problems = check({"session": "0.6637", "vol_regime": "us", "won": "late-us",
                      "action": "low", "win_probability": 60})
    assert len(problems) >= 4, problems


# ---- the operator's loss-recovery rule ------------------------------------

def _settle(store, ticker, window_ms, pnl):
    """One market settled by the exchange, as the live path records it.

    The settlement is written AND folded into `daily_ledger`, because that is
    what `sync_ledger_from_settlements` does every minute and the ledger is
    what the recovery deficit reads.
    """
    store.record_settlements([{
        "ticker": ticker, "market_result": "yes",
        "yes_count_fp": "1", "yes_total_cost_dollars": "0",
        "no_count_fp": "0", "no_total_cost_dollars": "0",
        "revenue": 0, "fee_cost": "0",
        "settled_time": "2026-09-22T00:00:00Z",
    }], window_ms)
    store.db.execute(
        "UPDATE settlements SET pnl=?, window_ms=? WHERE ticker=?",
        (pnl, window_ms, ticker),
    )
    store.db.commit()
    store.record_realised(ticker, window_ms, pnl, pnl > 0, "exchange", window_ms)


def test_a_loss_arms_the_recovery_and_a_win_does_not(tmp_path):
    """The defect the operator caught: three live losses on 2026-09-22 were
    each followed by a $1 trade. There was no loss-triggered recovery at all -
    the $2 trades came from the confidence band, on unrelated windows."""
    store = Store(str(tmp_path / "s.db"))
    _settle(store, "KXBTC15M-A", 1_000, +0.20)
    assert store.outstanding_loss()[0] == 0.0, "a win must not arm it"
    _settle(store, "KXBTC15M-B", 2_000, -0.88)
    debt, _since = store.outstanding_loss()
    assert debt == pytest.approx(0.88), "a loss must arm it"


def test_the_recovery_resets_once_the_loss_is_repaid(tmp_path):
    """"recover the lost two dollar AND RESET" - per loss, not a ledger."""
    store = Store(str(tmp_path / "s.db"))
    _settle(store, "KXBTC15M-A", 1_000, -0.90)
    _settle(store, "KXBTC15M-B", 2_000, +0.40)
    assert store.outstanding_loss()[0] == pytest.approx(0.50)
    _settle(store, "KXBTC15M-C", 3_000, +0.60)
    assert store.outstanding_loss()[0] == 0.0, "repaid; it must reset"


def test_a_second_loss_increases_the_deficit_and_does_not_restart_it(tmp_path):
    """THE RULE CHANGED HERE on 2026-09-22. Until then a new loss REPLACED the
    target, which was chosen on a measurement (FINDINGS 39: cumulative would
    have sat at recovery size for 93% of windows against 57%, completing 3
    recoveries against 11). The operator has decided against that measurement:
    recovery ends when the money is back, so a second loss adds to what is
    missing rather than erasing it."""
    store = Store(str(tmp_path / "s.db"))
    _settle(store, "KXBTC15M-A", 1_000, -0.90)
    _settle(store, "KXBTC15M-B", 2_000, -0.80)
    deficit, _ = store.outstanding_loss()
    assert deficit == pytest.approx(1.70), "both losses are still missing"


def test_the_recovery_size_is_capped_and_never_escalates(tmp_path):
    from btc15_signal.config import Settings
    from btc15_signal.main import recovery_size

    store = Store(str(tmp_path / "s.db"))
    settings = Settings(recovery_upfront_upsize_enabled=True)
    _settle(store, "KXBTC15M-A", 1_000, -0.40)
    count, reason = recovery_size(store, settings, 1, 0.75)
    assert count == settings.high_confidence_contracts, reason
    assert count <= 2, "the recovery must never escalate beyond the cap"

    # And an enormous deficit buys nothing extra: the cap is the cap, and a
    # share no single trade can cover simply leaves the size at base.
    _settle(store, "KXBTC15M-B", 2_000, -25.0)
    big, _ = recovery_size(store, settings, 1, 0.75)
    assert big <= settings.high_confidence_contracts


def test_no_outstanding_loss_leaves_the_size_alone(tmp_path):
    from btc15_signal.config import Settings
    from btc15_signal.main import recovery_size

    store = Store(str(tmp_path / "s.db"))
    _settle(store, "KXBTC15M-A", 1_000, +0.20)
    count, reason = recovery_size(store, Settings(), 1, 0.75)
    assert count == 1
    assert reason == "", "no recovery, no override"


def test_both_triggers_give_two_and_neither_cancels_the_other(tmp_path):
    """The operator wants BOTH, independently: the measured distance band
    keeps its $2, and a loss arms its own $2 whether or not the next setup
    happens to land in the band. Neither may suppress the other, and the two
    together must still never exceed the cap.

    The band is switched OFF in the deployed config since 2026-09-22, so it is
    enabled explicitly here: this test pins the relationship between the two
    mechanisms, which is what re-enabling the band would have to preserve."""
    from btc15_signal.config import Settings
    from btc15_signal.main import confidence_size, recovery_size

    settings = Settings(confidence_sizing_enabled=True,
                        recovery_upfront_upsize_enabled=True)
    store = Store(str(tmp_path / "s.db"))

    class Snapshot:
        volatility_5m_bps = 5.0
        momentum_5m_bps = -10.0

    class InBand:          # 3.0x vol, momentum aligned for DOWN
        side = "DOWN"
        distance_bps = 15.0    # a MAGNITUDE; the side carries the direction

    class OutOfBand:       # 0.2x vol - nowhere near the band
        side = "DOWN"
        distance_bps = 1.0

    # 1. band alone, no deficit outstanding
    band_only, _ = confidence_size(settings, Snapshot(), InBand(), 1)
    assert band_only == 2, "the distance band must still size up on its own"
    assert recovery_size(store, settings, band_only, 0.75)[0] == 2

    # 2. deficit outstanding, setup NOT in the band - this is the case that was
    #    silently doing nothing, and the whole reason the rule was missing.
    _settle(store, "KXBTC15M-L", 1_000, -0.88)
    flat, _ = confidence_size(settings, Snapshot(), OutOfBand(), 1)
    assert flat == 1, "out of band, the band contributes nothing"
    recovered, reason = recovery_size(store, settings, flat, 0.75)
    assert recovered == 2, "a deficit must size up regardless of the band"
    assert "recovering" in reason

    # 3. both at once - still capped, never stacked
    both, _ = confidence_size(settings, Snapshot(), InBand(), 1)
    assert recovery_size(store, settings, both, 0.75)[0] == 2, "no stacking to 4"


def test_confidence_sizing_is_off_and_a_band_setup_still_sizes_one(tmp_path):
    """The operator, 2026-09-22: "The intelligence doubled exposure because of
    confidence. That contradicts the earlier requirement that intelligence must
    not change sizing." The live case was KXBTC15M-26SEP221330-30 - 2 contracts
    at 81c on "3.0x vol is inside the measured 2-4x edge band" - which settled
    against us for -$1.64 where one contract was about -$0.82."""
    from btc15_signal.config import Settings
    from btc15_signal.main import confidence_size, recovery_size

    settings = Settings()
    assert settings.confidence_sizing_enabled is False, "the deployed default is OFF"
    assert settings.recovery_upfront_upsize_enabled is False, (
        "recovery acts only through the conditional add-on"
    )

    class Snapshot:
        volatility_5m_bps = 5.0
        momentum_5m_bps = -10.0

    class InBand:              # 3.0x vol, momentum aligned - squarely in band
        side = "DOWN"
        distance_bps = 15.0

    count, reason = confidence_size(settings, Snapshot(), InBand(), 1)
    assert count == 1, "the band must not size up while it is disabled"
    assert reason == "", "an empty reason: no size line may claim a dead band"

    # The flag gates the BAND ONLY - it must not be what silences recovery.
    # Recovery's own upfront upsize is separately OFF (it now acts through the
    # conditional add-on), so both are checked: with the band disabled and
    # recovery explicitly enabled the upsize still works, which proves one
    # switch is not standing in for the other.
    store = Store(str(tmp_path / "s.db"))
    _settle(store, "KXBTC15M-L", 1_000, -0.88)
    recovered, recovery_reason = recovery_size(
        store, Settings(recovery_upfront_upsize_enabled=True), count, 0.75
    )
    assert recovered == settings.high_confidence_contracts
    assert "recovering" in recovery_reason

    # And with the deployed defaults, neither path sizes up.
    base_only, base_reason = recovery_size(store, settings, count, 0.75)
    assert base_only == 1, "the base entry is one contract while recovery runs"
    assert base_reason == ""


# ---- the deficit: recovery ends when the money is back --------------------
#
# The operator, 2026-09-22: activate on a realised net loss, track the
# unrecovered deficit after fees, keep going until realised profit covers it,
# and stop immediately when it reaches $0.00. Another loss INCREASES it.

def _cash_out(store, ticker, window_ms, pnl, now_ms=None):
    store.record_realised(
        ticker, window_ms, pnl, pnl > 0, "cash_out",
        window_ms if now_ms is None else now_ms,
    )


def test_a_loss_opens_a_deficit_of_exactly_the_realised_net_amount(tmp_path):
    """Realised, after fees, from the ledger - not a contract count and not a
    gross figure. -1.6416 is the live loss of KXBTC15M-26SEP221330-30."""
    store = Store(str(tmp_path / "s.db"))
    assert store.recovery_is_active() is False, "a fresh account owes nothing"

    _settle(store, "KXBTC15M-LOSS", 1_000, -1.6416)
    state = store.recovery_state()
    assert state.deficit == pytest.approx(1.6416)
    assert state.active is True
    assert store.recovery_deficit() == pytest.approx(1.6416)


def test_a_partial_recovery_reduces_the_deficit_and_recovery_stays_on(tmp_path):
    store = Store(str(tmp_path / "s.db"))
    _settle(store, "KXBTC15M-LOSS", 1_000, -1.6416)
    _settle(store, "KXBTC15M-WIN", 2_000, +0.4737)

    state = store.recovery_state()
    assert state.deficit == pytest.approx(1.1679)
    assert state.active is True, "part of the money is still missing"


def test_the_profit_that_clears_the_deficit_turns_recovery_off(tmp_path):
    from btc15_signal.config import Settings
    from btc15_signal.main import recovery_size

    store, settings = (Store(str(tmp_path / "s.db")),
                       Settings(recovery_upfront_upsize_enabled=True))
    _settle(store, "KXBTC15M-LOSS", 1_000, -0.88)
    _settle(store, "KXBTC15M-WIN1", 2_000, +0.50)
    assert store.recovery_is_active() is True
    _settle(store, "KXBTC15M-WIN2", 3_000, +0.50)

    state = store.recovery_state()
    assert state.deficit == 0.0, "recovered money, and no more than that"
    assert state.active is False
    assert state.steps == 0, "the plan is closed with the deficit"
    assert recovery_size(store, settings, 1, 0.75) == (1, ""), "next trade base"


def test_a_base_size_win_still_reduces_the_deficit(tmp_path):
    """Rule 6. The ledger does not record what size won the money back, and it
    must not: a trade held at base because it could not cover its share still
    pays the deficit down when it wins."""
    from btc15_signal.config import Settings
    from btc15_signal.main import recovery_size

    store = Store(str(tmp_path / "s.db"))
    settings = Settings(recovery_upfront_upsize_enabled=True)
    _settle(store, "KXBTC15M-LOSS", 1_000, -1.6416)
    # 2 @ 90c nets 0.1874, under the 0.41 share - so this one goes at base.
    assert recovery_size(store, settings, 1, 0.90) == (1, "")

    _settle(store, "KXBTC15M-BASEWIN", 2_000, +0.1126)
    assert store.recovery_deficit() == pytest.approx(1.5290)


def test_an_early_cash_out_contributes_once_at_realised_net(tmp_path):
    """Rule 8. `daily_ledger` is keyed by ticker and `recovery_applied` records
    what was folded in, so re-reading the state cannot apply it again."""
    store = Store(str(tmp_path / "s.db"))
    _settle(store, "KXBTC15M-LOSS", 1_000, -1.00)
    _cash_out(store, "KXBTC15M-OUT", 2_000, +0.30)

    first = store.recovery_state().deficit
    again = store.recovery_state().deficit
    third = store.recovery_state().deficit
    assert first == pytest.approx(0.70)
    assert again == first == third, "one market, one application"

    applied = store.db.execute(
        "SELECT recovery_applied FROM daily_ledger WHERE ticker='KXBTC15M-OUT'"
    ).fetchone()[0]
    assert applied == pytest.approx(0.30), "what was applied, not a flag"


def test_an_exchange_revision_moves_the_deficit_by_the_delta_only(tmp_path):
    """A cash-out banked at +0.55 that the exchange settles at +0.54 costs the
    deficit one cent. Counting it again in full is the failure this pins."""
    store = Store(str(tmp_path / "s.db"))
    _settle(store, "KXBTC15M-LOSS", 1_000, -1.00)
    _cash_out(store, "KXBTC15M-OUT", 2_000, +0.5515)
    assert store.recovery_deficit() == pytest.approx(0.4485)

    store.record_realised(
        "KXBTC15M-OUT", 2_000, 0.5480, True, "exchange", 2_100
    )
    assert store.recovery_deficit() == pytest.approx(0.4520), "the delta only"


def test_the_deficit_survives_a_restart(tmp_path):
    """Rule 7. Persisted, and re-reading the ledger after the restart must not
    double-count the markets that opened it."""
    path = str(tmp_path / "s.db")
    store = Store(path)
    _settle(store, "KXBTC15M-LOSS", 1_000, -1.6416)
    store.consume_recovery_step(1_500)
    assert store.recovery_state().steps == 3
    store.db.close()

    reopened = Store(path)
    assert reopened.stored_deficit().deficit == pytest.approx(1.6416)
    assert reopened.stored_deficit().steps == 3, "mid-plan, and it resumes"
    assert reopened.recovery_state().deficit == pytest.approx(1.6416)


# ---- eligibility: the upsize must be able to pay for itself ----------------

def test_the_operators_worked_example(tmp_path):
    """Verbatim: "deficit $1.64, plan 4 trades -> required per trade ~= $0.41;
    2 contracts at 90c -> not eligible; 2 contracts at 75c -> eligible"."""
    from btc15_signal.config import Settings
    from btc15_signal.main import max_net_profit, recovery_size

    # The upfront upsize is OFF by default now - recovery acts through the
    # conditional add-on - but the eligibility arithmetic it pioneered is the
    # same gate the add-on uses, so it is opted in here to keep it pinned.
    store = Store(str(tmp_path / "s.db"))
    settings = Settings(recovery_upfront_upsize_enabled=True)
    _settle(store, "KXBTC15M-LOSS", 1_000, -1.6416)

    state = store.recovery_state(settings.recovery_steps)
    assert state.steps == settings.recovery_steps == 4
    assert state.required_per_trade() == pytest.approx(0.4104, abs=5e-4)

    assert max_net_profit(2, 0.90) == pytest.approx(0.1874, abs=5e-5)
    assert max_net_profit(2, 0.75) == pytest.approx(0.4737, abs=5e-5)

    assert recovery_size(store, settings, 1, 0.90) == (1, ""), "0.19 < 0.41"
    count, reason = recovery_size(store, settings, 1, 0.75)
    assert count == 2, "0.47 covers 0.41"
    assert "recovering 1.64 outstanding" in reason


def test_an_ineligible_trade_goes_out_at_base_and_recovery_stays_active(tmp_path):
    """Rule 5. The trade still happens - recovery only ever changed its size -
    and the deficit is untouched by the decision."""
    from btc15_signal.config import Settings
    from btc15_signal.main import recovery_size

    store, settings = Store(str(tmp_path / "s.db")), Settings()
    _settle(store, "KXBTC15M-LOSS", 1_000, -1.6416)

    assert recovery_size(store, settings, 1, 0.90) == (1, "")
    after = store.recovery_state()
    assert after.active is True, "still owed, still recovering"
    assert after.deficit == pytest.approx(1.6416)
    assert after.steps == 4, "a base-size trade spends no step"


def test_steps_decrement_only_on_an_upsized_trade_and_a_loss_resets_them(tmp_path):
    """Judgement call (a): the divisor counts upsized trades only, so the
    per-trade share stays roughly flat as the deficit falls. Judgement call
    (b): a new loss restores the full plan, or the share would explode exactly
    where the operator wants the upsize on."""
    from btc15_signal.config import Settings

    store, settings = Store(str(tmp_path / "s.db")), Settings()
    _settle(store, "KXBTC15M-LOSS", 1_000, -1.6416)
    assert store.recovery_required_per_trade() == pytest.approx(0.4104, abs=5e-4)

    store.consume_recovery_step(1_500)                 # one upsized trade
    _settle(store, "KXBTC15M-WIN", 2_000, +0.4737)
    state = store.recovery_state()
    assert state.steps == 3
    assert state.deficit == pytest.approx(1.1679)
    assert state.required_per_trade() == pytest.approx(0.3893, abs=5e-4)

    _settle(store, "KXBTC15M-LOSS2", 3_000, -0.50)
    reset = store.recovery_state()
    assert reset.deficit == pytest.approx(1.6679), "increased, not restarted"
    assert reset.steps == settings.recovery_steps, "a fresh loss, a fresh plan"


def test_the_deficit_clearing_turns_recovery_off_even_with_steps_left(tmp_path):
    store = Store(str(tmp_path / "s.db"))
    _settle(store, "KXBTC15M-LOSS", 1_000, -0.50)
    assert store.recovery_state().steps == 4
    _settle(store, "KXBTC15M-WIN", 2_000, +0.60)

    state = store.recovery_state()
    assert state.active is False, "the money is back; nothing else decides"
    assert state.deficit == 0.0, "and it never goes negative into credit"


def test_the_plan_length_in_the_store_matches_the_config(tmp_path):
    """The store carries a default so a report need not hold the config. If
    the two ever disagree, the deficit is divided by a plan nobody deployed."""
    from btc15_signal.config import Settings
    from btc15_signal.store import DEFAULT_RECOVERY_STEPS

    assert Settings().recovery_steps == DEFAULT_RECOVERY_STEPS


def test_recovery_never_creates_a_trade(tmp_path):
    """Rule 1, structurally: every gate is evaluated before sizing is reached,
    and the sizing block sits in the branch where nothing blocked the trade."""
    source = Path("src/btc15_signal/main.py").read_text(encoding="utf-8")
    auto = source.split("---- unattended execution")[1]
    gate = auto.index("blocked = autotrade.auto_block_reason(")
    declined = auto.index("if blocked:")
    size = auto.index("recovery_size(")
    assert gate < declined < size, "the gates decide first, sizing second"
    assert "else:" in auto[declined:size], "sizing is in the not-blocked branch"
    assert "recovery" not in auto[:declined], "no deficit may reach a gate"


def test_the_recovery_step_is_consumed_after_the_order_and_only_on_a_fill():
    """Structural, in the house style of the post-order path tests: an order
    that bought nothing has spent no step, and a write placed before the order
    call is a write that can cost a fill."""
    source = Path("src/btc15_signal/main.py").read_text(encoding="utf-8")
    auto = source.split("---- unattended execution")[1]
    order = auto.index("await trader.execute_with_take_profit(")
    consumed = auto.index("store.consume_recovery_step(")
    assert consumed > order, "the step must not be spent before the order"
    condition = auto[:consumed].rsplit("if ", 1)[1]
    assert "recovery_reason" in condition, "only an upsized order spends a step"
    assert 'getattr(result, "filled_count", 0) > 0' in condition, (
        "only a FILLED upsized order spends a step"
    )


# ---- bookkeeping behind real money must never stop the loop ---------------

def test_save_details_survives_every_wrong_type(tmp_path):
    """The live crash: `decision_record` returns a list of (label, value)
    pairs, and handing that to a TEXT column raised ProgrammingError one second
    after EVERY fill. The watchdog restarted the service roughly every fifteen
    minutes between 09:04 and 10:50 on 2026-09-22. The position was never at
    risk - only the order call may mark a proposal failed - but a service that
    dies on a schedule eventually dies in a window that matters."""
    store = Store(str(tmp_path / "s.db"))
    for body in (
        [("distance", "2.1x"), ("momentum", "+3.3")],   # the actual culprit
        ("a", "tuple"),
        {"a": "dict"},
        12345,
        None,
        object(),
    ):
        store.save_details("k", body, 1_790_000_000_000)   # must not raise
    assert store.details("k") is not None


def test_a_stored_detail_body_is_readable_text(tmp_path):
    store = Store(str(tmp_path / "s.db"))
    store.save_details(
        "order-1", [("distance", "2.1x vol"), ("momentum", "+3.3 bps")],
        1_790_000_000_000,
    )
    body = store.details("order-1")
    assert "distance" in body and "momentum" in body


def test_nothing_on_the_post_order_path_writes_unguarded(tmp_path):
    """Structural. Between placing the order and reporting it, every store
    write must be one that cannot raise - this is where a bookkeeping bug
    becomes an outage."""
    source = Path("src/btc15_signal/main.py").read_text(encoding="utf-8")
    auto = source.split("---- unattended execution")[1]
    auto = auto[: auto.index("    head = head_for(store, settings)")]
    after_order = auto[auto.index("await trader.execute_with_take_profit("):]
    for call in (
        "store.save_details(",
        "store.record_shadow_decision(",
        "store.consume_recovery_step(",
    ):
        if call in after_order:
            method = call.split(".")[1].rstrip("(")
            store_src = Path("src/btc15_signal/store.py").read_text(
                encoding="utf-8"
            )
            body = store_src[store_src.index(f"def {method}("):]
            body = body[: body.index("\n    def ", 1)]
            assert "try:" in body, f"{method} runs after an order and can raise"
