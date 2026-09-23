"""`primary_signal` end to end under Kalshi-only.

The unit tests around this covered every module and still missed that the
alert path re-ran the BINANCE rule's `check_facts`, which reads
`prediction.raw_probability` - None here, because there is no Kalshi-native
model - and would have raised on the first live alert. 726 tests passed with
that bug present.

So this drives the real function with fakes and asserts it completes, records
and produces Kalshi-only facts. A module that works in isolation and raises
when assembled is the failure mode worth a slower test.
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from btc15_signal import kalshi_signal as ks  # noqa: E402
from btc15_signal import main as m  # noqa: E402
from btc15_signal.brti import BRTIFeatures  # noqa: E402
from btc15_signal.config import Settings  # noqa: E402
from btc15_signal.store import Store  # noqa: E402

NOW = 1_790_000_000_000
OPENED = NOW - 240_000


def features(distance_bps=15.0, **over) -> BRTIFeatures:
    base = dict(
        event_ticker="KXBTC15M-TEST", ts_ms=NOW, target=100_000.0,
        value=100_000.0 * (1 + distance_bps / 10_000),
        signed_distance_bps=distance_bps, brti_momentum_bps=4.0,
        brti_volatility_bps=1.0, brti_normalized_distance=abs(distance_bps),
        samples=900, span_ms=900_000, stale=False,
        settlement_projection=100_150.0,
    )
    base.update(over)
    return BRTIFeatures(**base)


class Contract:
    """A Kalshi book. `no_ask = 1 - yes_bid` exactly, as Kalshi's does."""

    ticker = "KXBTC15M-TEST-00"
    open_ms = OPENED
    close_ms = OPENED + 900_000
    target = 100_000.0

    def __init__(self, yes_bid=0.78, yes_ask=0.80):
        self.yes_bid, self.yes_ask = yes_bid, yes_ask
        self.no_ask = round(1 - yes_bid, 4)
        self.no_bid = round(1 - yes_ask, 4)

    def ask(self, side):
        return self.yes_ask if side == "UP" else self.no_ask

    def bid(self, side):
        return self.yes_bid if side == "UP" else self.no_bid


class Telegram:
    """Captures instead of sending."""

    def __init__(self):
        self.sent = []
        self.buttons = []

    async def send(self, text, buttons=None):
        self.sent.append(text)
        self.buttons.append(buttons)
        return 1


def run(tmp_path, dist=15.0, contract=None, **setting_over):
    settings = Settings()
    settings.kalshi_only = True
    settings.database_path = str(tmp_path / "t.db")
    settings.kalshi_strategy_path = str(
        Path(__file__).resolve().parents[1] / "strategy_kalshi.json")
    settings.dry_run = True
    settings.auto_trade_enabled = False
    for k, v in setting_over.items():
        setattr(settings, k, v)
    store = Store(settings.database_path)
    telegram = Telegram()
    contract = contract or Contract()
    brti = features(dist)
    snapshot = m.kalshi_snapshot(brti, contract, OPENED, NOW)
    asyncio.run(m.primary_signal(
        settings, store, telegram, contract, snapshot, OPENED,
        remaining=500, now_ms=NOW, trader=None, levels=None, capital=None,
        brti=brti,
    ))
    return settings, store, telegram


# ------------------------------------------------------ it does not raise

def test_a_qualifying_signal_completes_without_raising(tmp_path):
    _, store, telegram = run(tmp_path, dist=15.0)
    rows = store._dicts("SELECT * FROM predictions WHERE window_open=?", (OPENED,))
    assert rows, "the decision must be recorded"


def test_a_rejected_signal_completes_without_raising(tmp_path):
    _, store, _ = run(tmp_path, dist=2.0)   # below the measured 10x floor
    rows = store._dicts("SELECT * FROM predictions WHERE window_open=?", (OPENED,))
    assert rows and rows[0]["qualified"] == 0


# ------------------------------------------------- what it records is Kalshi

def test_no_model_probability_is_recorded_rather_than_invented(tmp_path):
    """NULL means "this build has no Kalshi-native model". A fabricated
    number here would be inventing exactly what the exercise measures."""
    _, store, _ = run(tmp_path)
    row = store._dicts("SELECT * FROM predictions WHERE window_open=?", (OPENED,))[0]
    assert row["raw_probability"] is None
    assert row["bucket"] == -1


def test_the_recorded_price_is_the_reference_and_the_strike(tmp_path):
    _, store, _ = run(tmp_path, dist=15.0)
    row = store._dicts("SELECT * FROM predictions WHERE window_open=?", (OPENED,))[0]
    assert row["target"] == 100_000.0
    assert abs(row["entry_price"] - 100_150.0) < 0.01, "BRTI value, not a spot tick"


def test_the_side_is_the_references_side(tmp_path):
    """A DOWN signal prices off `1 - yes_bid`, so the book is flipped to put
    that side in the actionable band. At a 0.22 no-ask the setup is simply
    not offered mid-window, which is existing and correct behaviour."""
    _, store, _ = run(tmp_path, dist=-15.0, contract=Contract(0.20, 0.22))
    row = store._dicts("SELECT * FROM predictions WHERE window_open=?", (OPENED,))[0]
    assert row["side"] == "DOWN"
    assert abs(row["contract_price"] - 0.80) < 1e-6, "1 - yes_bid"


# ------------------------------------------------------ the rendered facts

def test_the_alert_shows_kalshi_gates_only(tmp_path):
    _, _, telegram = run(tmp_path, dist=15.0)
    blob = " ".join(telegram.sent).lower()
    assert blob, "a qualifying signal should produce a message"
    assert "binance" not in blob
    assert "brti" in blob


def test_the_failing_gate_is_named(tmp_path):
    _, store, _ = run(tmp_path, dist=2.0)
    row = store._dicts("SELECT * FROM predictions WHERE window_open=?", (OPENED,))[0]
    assert row["failed_gates"] and "BRTI distance" in row["failed_gates"]


# ------------------------------------------------- intelligence still runs

def test_the_intelligence_decision_is_recorded_before_the_outcome(tmp_path):
    _, store, _ = run(tmp_path)
    rows = store._dicts("SELECT * FROM intelligence_decisions WHERE window_open=?",
                        (OPENED,))
    assert rows, "every decision is recorded, including neutral ones"
    assert rows[0]["won"] is None, "the outcome is not known yet"
    assert rows[0]["graded_ms"] is None


def test_the_recorded_context_is_a_brti_context(tmp_path):
    _, store, _ = run(tmp_path)
    row = store._dicts("SELECT * FROM intelligence_decisions WHERE window_open=?",
                       (OPENED,))[0]
    key = row["context_key"] or ""
    assert not any(b in key for b in ("dist<1.5", "dist1.5-3", "dist3+")), key


# -------------------------------------------------- unavailable, no fallback

def test_an_unavailable_reference_yields_no_signal_and_a_named_reason():
    out = ks.signal_inputs(None, Contract(), now_ms=NOW, stale_limit_ms=15_000)
    assert isinstance(out, ks.Unavailable)
    assert "binance" not in str(out).lower()


def test_the_gap_is_recorded_so_silence_is_explicable(tmp_path):
    store = Store(str(tmp_path / "g.db"))
    store.record_input_gap(
        window_open=OPENED, ticker="T", observed_ms=NOW, remaining_s=500,
        reason="kalshi brti stale", detail="60000ms old",
    )
    summary = store.input_gap_summary()
    assert summary and summary[0]["reason"] == "kalshi brti stale"
    assert summary[0]["markets"] == 1
