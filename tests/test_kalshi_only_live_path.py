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


def test_kalshi_only_requires_the_reference_to_be_enabled():
    """With `kalshi_only` the reference is the SIGNAL SOURCE, not a shadow
    recorder. Disabling it would make every poll record an input gap and the
    bot go quiet in a way indistinguishable from a flat market, so the
    service refuses to start instead."""
    import inspect

    source = inspect.getsource(m.service)
    assert "kalshi_only requires reference_enabled" in source
    assert "raise SystemExit" in source


# ------------------------------------------- nothing may touch the client

def test_no_component_that_needs_binance_is_constructed_under_kalshi_only():
    """The 23:00 deploy died here. `market` is None under kalshi_only, and
    three call sites still used it unconditionally: two `hourly.poll(now_ms,
    market)` and `await market.close()`. The hourly ladder takes its OWN
    Binance reading, so although it never trades it IS an active Binance
    request and must not run either.

    Read from the source because constructing the real service needs network
    and credentials; what matters is that every use is guarded.
    """
    import inspect
    import re

    source = inspect.getsource(m.service)
    # The client itself
    assert "None if settings.kalshi_only" in source
    # Its shutdown
    assert "if market is not None:" in source
    # Everything that consumes it
    for line in source.splitlines():
        stripped = line.strip()
        if re.search(r"\bmarket\.", stripped) and "live_market" not in stripped:
            assert "if market is not None" in source, stripped
        if "hourly.poll(now_ms, market)" in stripped:
            assert "if hourly:" in source, "hourly.poll must be guarded"
    # And the ladder is not built at all
    assert "settings.hourly_enabled and not settings.kalshi_only" in source


def test_the_crash_handler_reports_where_not_only_the_type():
    """"Service stopped: AttributeError" cost a diagnosis. The traceback
    carries no credentials - only the exception MESSAGE might - so frames are
    logged and the message is still withheld."""
    handler = Path(__file__).resolve().parents[1] / "scripts" / "run_service.py"
    source = handler.read_text()
    assert "extract_tb" in source
    assert "Service stopped: %s at %s" in source


# ------------------- no Binance-rule call may be reached under kalshi_only

def test_every_binance_rule_call_in_primary_signal_is_guarded():
    """Five call sites, found one at a time, the last by a crash loop.

    `rule` is the BINANCE EntryRule. Its `matches`, `check_facts` and
    `check_detail` all compare `prediction.raw_probability >=
    min_raw_probability`, and that is None on the Kalshi path. Every one is a
    TypeError waiting for the right market state:

      main.py:1766  matches       - the decision itself
      main.py:1952  check_detail  - the auto-path refusal line. Reached ONLY
                                    when a market is eligible and the rule
                                    then refuses it, which is why the first
                                    twenty minutes after deploy looked clean
                                    and the service then crashed every window
                                    from 00:21 to 05:04.
      main.py:2196  check_facts   - the fill record
      main.py:2305  check_facts   - the alert

    Hunting them one at a time is not a method. This scans instead.
    """
    import inspect
    import re

    source = inspect.getsource(m.primary_signal)
    lines = source.splitlines()

    def guarded(index: int) -> bool:
        """Is this call reachable only when kalshi_only is False?

        Two shapes count and nothing else does:
          * an inline conditional on this line or the one above
          * an `else:` whose matching `if` tests kalshi_only, located by
            walking back to the nearest line at LOWER indentation

        Indentation rather than a fixed window, so a long branch cannot make
        a genuinely unguarded call look safe merely by sitting near the word.
        """
        line = lines[index]
        if "kalshi_only" in line or "kalshi_only" in lines[index - 1]:
            return True
        indent = len(line) - len(line.lstrip())
        for j in range(index - 1, -1, -1):
            candidate = lines[j]
            if not candidate.strip():
                continue
            here = len(candidate) - len(candidate.lstrip())
            if here >= indent:
                continue
            if candidate.strip() != "else:":
                return False
            for k in range(j - 1, -1, -1):          # the matching `if`
                probe = lines[k]
                if not probe.strip():
                    continue
                probe_indent = len(probe) - len(probe.lstrip())
                if probe_indent == here and probe.strip().startswith("if "):
                    return "kalshi_only" in probe
                if probe_indent < here:
                    return False
            return False
        return False

    unguarded = [
        (i, line.strip()) for i, line in enumerate(lines)
        if re.search(r"\brule\.(matches|check_facts|check_detail)\b", line)
        and not guarded(i)
    ]
    assert not unguarded, f"unguarded Binance-rule calls: {unguarded}"


def test_the_auto_refusal_line_renders_from_kalshi_facts(tmp_path):
    """The exact state that crashed: a market inside the actionable band that
    the rule then refuses. Drive it and require no exception."""
    settings, store, telegram = run(
        tmp_path, dist=2.0,                    # below the 10x floor -> refused
        contract=Contract(0.78, 0.80),         # but inside the price band
        auto_trade_enabled=True,               # so the auto path is taken
    )
    rows = store._dicts("SELECT * FROM predictions WHERE window_open=?", (OPENED,))
    assert rows and rows[0]["qualified"] == 0
    assert rows[0]["failed_gates"], "the refusal reason must be recorded"
